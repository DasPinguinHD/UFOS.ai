using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.MarketResearch;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund
{
    public partial class HedgeFundWindow : Window
    {
        // Live dashboard of the autonomous PAPER-TRADING loop (backend/fund, contract:
        // backend/fund/SPEC.md §11/§13). Everything here is simulated — the backend has no
        // live-trading code path at all. Paper only (notice: AiContent.PaperExecutionNotice).
        //
        // - Polls GET /fund/state every 5 s while the window is visible; engine settings
        //   (GET/PUT /fund/params, debounced 600 ms), run history, the equity curve and the
        //   exports are loaded on demand. Every call goes through Fund/FundClient.cs (session
        //   token, never throws); offline / 401 / errors are shown in a banner, the last known
        //   state stays visible.
        // - AI labelling (CLAUDE.md): the judge's verdict and the sentiment agent carry the
        //   "✦ AI-GENERATED · model · time" badge, caveat, violet container, cost line and a
        //   screen-reader name starting with "AI-generated" whenever their provenance marks
        //   them as LLM output; "Rule-based · no AI" only for old rows of the former rule-based
        //   judge, which no longer exists (no-fallback rule, D9 2026-10-02).
        // - D9 (2026-10-02): symbols[].judge_unavailable / data_unavailable and
        //   recommendation.not_updated are shown as status (amber/red notice, chip marker), never as a
        //   verdict; an older recommendation keeps its own time and says "not updated: …".
        // - Backend lifetime (SPEC §14): closing this window no longer stops the backend; it
        //   lives as long as the UFOS.ai app (Job Object) or, opt-in, detached.
        // - The Financial Newsroom (toolbar button, Newsroom/) is unchanged.
        // - Review fixes 2026-09-26: toolbar (Run now / Resume / kill switch) pinned above the
        //   ScrollViewer; "Execute" whenever the autopilot is off (open/skipped, latest only);
        //   queued state refresh after actions; AI markers on chips/history; "Stop engine…"
        //   (kill + /shutdown); dead-man switch (autopilot_heartbeat_minutes, "autopilot" events).
        // - 24/7 crypto (2026-09-26): crypto pairs (BTC-USD …) with fractional quantities and perp
        //   shorts (funding every 8 h, own margin, liquidation fee). Outside the US session the engine
        //   runs no scheduled cycles (schedule_mode "manual_only": only ▶ Run now) and a light
        //   "risk watch" (no agents, no AI, no new orders) while crypto positions are open. Every new
        //   field is optional — an older backend simply hides the related UI.
        private static readonly TimeSpan StatePollInterval = TimeSpan.FromSeconds(5);
        private static readonly TimeSpan EquityReloadInterval = TimeSpan.FromSeconds(60);
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        private DispatcherTimer? _clockTimer;
        private DispatcherTimer? _stateTimer;
        private readonly DispatcherTimer _paramsDebounce;
        private FinancialNewsroomWindow? _newsroomWindow;
        private KeysAndDataWindow? _keysWindow;
        private LibraryView? _libraryView; // Strategy Platform stage 2: created on first use of the rail's "Library"
        // Market Research MR1: the ONE research view, created on first use of the rail's "Research"; it lives in
        // ResearchViewHost or, while popped out, in _researchPopout (never both, never a second instance).
        private ResearchView? _researchView;
        private ResearchPopoutWindow? _researchPopout;
        private readonly EquityChart _equityChart = new();
        private readonly Dictionary<Slider, (string Key, bool IsInt)> _sliderKeys = new();
        private readonly bool _uiReady;

        // Engine state
        private FundState? _state;
        private bool _connected;
        private bool _stateLoading;
        // A refresh requested while one is in flight (e.g. right after kill / resume / an order)
        // must not be dropped: it is remembered and run once the current request has finished.
        private bool _refreshAgain;
        private bool _polling;
        private bool _closed;
        private bool _stoppingEngine;
        private string? _selectedSymbol;
        private string _chipSignature = "";
        private bool _rebuildingChips;
        private bool _modeTabInitialized;

        // Params (engine settings)
        private FundParamsResponse? _params;
        private int? _paramsVersion;
        private bool _applyingParams;
        private bool _paramsLoading;
        private bool _paramsSaving;
        private readonly Dictionary<string, object?> _pendingParams = new();
        private int? _savedVersion;
        private DateTimeOffset? _paramsEffectiveFrom;
        private string? _paramsError;
        private double _budgetMax = 50;
        private bool? _heartbeatSupported; // null = unknown (params not loaded yet)
        private bool? _cryptoSupported;    // null = unknown; true once GET /fund/params serves crypto_* keys
        private bool? _runNowProminent;    // last applied "manual_only" look of ▶ Run now (null = not applied yet)
        private string _runNowTooltip = "";
        private static readonly Brush RunNowProminentBackground = FundPalette.Make(0x667C8CF8);

        // Latest "autopilot" event (the engine's dead-man switch turned the autopilot off).
        private FundEvent? _autopilotOffEvent;

        // Runs / history / equity
        private long? _lastRunIdSeen;
        private bool _historyLoadedOnce;
        private bool _historyLoading;
        private DateTime _lastEquityLoad = DateTime.MinValue;
        private bool _runRequested;
        private long? _runRequestedFromId;
        private DateTime _runRequestedAt;

        public HedgeFundWindow()
        {
            InitializeComponent();
            FundUi.EnsureWheelScrolling();   // retest 2026-10-23: one wheel notch ≈ 48 px in every Hedge Fund page
            // user request 2026-10-23: start maximized on the monitor it opens on, within the work area (taskbar visible); the
            // maximized look is applied once (WPF raises no StateChanged for the initial state)
            UFOS.ai.Shared.WindowMaximize.ConstrainToWorkArea(this);
            UFOS.ai.Shared.WindowMaximize.StartMaximized(this);
            _lastNonMinimizedState = WindowState.Maximized;
            Window_StateChanged(this, EventArgs.Empty);

            _paramsDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(600) };
            _paramsDebounce.Tick += async (_, _) =>
            {
                _paramsDebounce.Stop();
                await FlushParamsAsync();
            };
            EquityChartHost.Child = _equityChart;
            FullScreenHost.Wire(EquityFullScreenButton, () => EquityChartHost, "Equity curve vs SPY", EquityFullScreenHeader);
            FullScreenHost.RegisterF11(EquityCurvePanel, () => EquityChartHost, "Equity curve vs SPY", EquityFullScreenHeader);
            InitCouncil();   // S10-C R3: Council view at the top of the Overview (HedgeFundWindow.Council.cs)

            // "▶ Run now" can call the AI judge / sentiment agent -> AI trigger tooltip.
            _runNowTooltip = "Runs one engine cycle now (Council of AIs: agents + judge). " + AiContent.TriggerTooltip +
                             " Stays within the AI daily budget; when it is reached, no new verdicts are made (shown as \"not updated\").";
            RunNowButton.ToolTip = _runNowTooltip;
            AutomationProperties.SetHelpText(RunNowButton, AiContent.TriggerTooltip);

            // Slider -> params key (SPEC §3); agent weights are handled together.
            _sliderKeys[ConfidenceSlider] = ("min_confidence", true);
            _sliderKeys[MaxSizeSlider] = ("max_position_pct", false);
            _sliderKeys[MaxTradesSlider] = ("max_trades_per_day", true);
            _sliderKeys[DrawdownStopSlider] = ("drawdown_stop_pct", false);
            _sliderKeys[IntervalSlider] = ("interval_minutes", true);
            _sliderKeys[ClosedIntervalSlider] = ("closed_interval_minutes", true);
            _sliderKeys[ShortCapSlider] = ("short_exposure_max_pct", false);
            _sliderKeys[FeesSlider] = ("fees_bps", false);
            _sliderKeys[HeartbeatSlider] = ("autopilot_heartbeat_minutes", true);
            // Crypto (24/7) — IsInt follows limits[key].type once GET /fund/params has been read.
            _sliderKeys[CryptoFeesSlider] = ("crypto_fees_bps", false);
            _sliderKeys[CryptoMaxPosSlider] = ("crypto_max_position_pct", false);
            _sliderKeys[CryptoInitialMarginSlider] = ("crypto_initial_margin_pct", false);
            _sliderKeys[CryptoMaintMarginSlider] = ("crypto_maintenance_margin_pct", false);
            _sliderKeys[CryptoFundingSlider] = ("crypto_funding_rate_pct_8h", false);
            _sliderKeys[CryptoLiqFeeSlider] = ("crypto_liquidation_fee_pct", false);
            SetSliderStep(ConfidenceSlider, 1);
            SetSliderStep(MaxSizeSlider, 0.5);
            SetSliderStep(MaxTradesSlider, 1);
            SetSliderStep(DrawdownStopSlider, 0.5);
            SetSliderStep(IntervalSlider, 1);
            SetSliderStep(ClosedIntervalSlider, 5);
            SetSliderStep(ShortCapSlider, 5);
            SetSliderStep(FeesSlider, 0.5);
            SetSliderStep(HeartbeatSlider, 5);
            SetSliderStep(CryptoFeesSlider, 1);
            SetSliderStep(CryptoMaxPosSlider, 0.5);
            SetSliderStep(CryptoInitialMarginSlider, 1);
            SetSliderStep(CryptoMaintMarginSlider, 1);
            SetSliderStep(CryptoFundingSlider, 0.005);
            SetSliderStep(CryptoLiqFeeSlider, 0.1);
            // Enabled once GET /fund/params shows the running backend knows the setting.
            HeartbeatSlider.IsEnabled = false;
            foreach (var (control, _) in CryptoControls()) control.IsEnabled = false;
            SetSliderStep(WeightTechSlider, 0.05);
            SetSliderStep(WeightMacroSlider, 0.05);
            SetSliderStep(WeightSentSlider, 0.05);

            MuteToastsCheck.IsChecked = FundSettings.MuteNotifications;
            KeepRunningCheck.IsChecked = FundSettings.IsDetached;
            StartWithAppCheck.IsChecked = FundSettings.StartWithApp;   // stage 9b (SPEC §20.8)
            SetConnectedUi(false);

            _uiReady = true;
            UpdateSliderLabels();
        }

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            _clockTimer = new DispatcherTimer
            {
                Interval = TimeSpan.FromSeconds(1)
            };
            _clockTimer.Tick += (_, _) =>
            {
                UpdateClock();
                UpdateStatusLine();
                UpdateParamsSaveText();
            };
            _clockTimer.Start();
            UpdateClock();

            // Financial Newsroom + paper-trading engine share one backend. It is started
            // with the window so the collectors (SEC EDGAR needs 1–2 min for its first
            // fetch) have data ready by the time the newsroom is opened.
            NewsroomBackend.StatusChanged += OnNewsroomBackendStatusChanged;
            UpdateNewsroomStatus();
            _ = NewsroomBackend.EnsureStartedAsync();

            // Windows toasts for engine events: app-wide, keeps running after this window closes.
            FundNotifications.EnsureStarted();
            ResearchAlertNotifier.EnsureStarted(); // Market Research MR7: alert toasts while UFOS.ai runs (MarketResearch/ResearchAlertNotifier.cs)
            FundNotifications.AutopilotSwitchedOff += OnAutopilotSwitchedOff;
            _autopilotOffEvent = FundNotifications.LastAutopilotEvent;
            // Strategy Platform stage 9 (SPEC §19.13): monitoring alert toasts, app-wide (Platform/MonitoringNotifier.cs); the rail
            // badge shows the alerts not yet seen; a toast clicked while no Hedge Fund window was open opens Monitoring here.
            MonitoringNotifier.EnsureStarted();
            MonitoringNotifier.Changed += MonitoringNotifier_Changed;
            UpdateMonitoringBadge(MonitoringNotifier.Unacked);
            if (MonitoringNotifier.TakePendingOpen(out var pendingAlertId))
                Dispatcher.BeginInvoke(new Action(() => ShowMonitoring(pendingAlertId)), DispatcherPriority.Loaded);

            _stateTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = StatePollInterval };
            _stateTimer.Tick += async (_, _) => await PollTickAsync();
            _stateTimer.Start();
            IsVisibleChanged += async (_, args) =>
            {
                if (args.NewValue is true) await PollTickAsync();
            };
            _ = PollTickAsync();
        }

        private void Window_Closing(object? sender, CancelEventArgs e)
        {
            _clockTimer?.Stop();
            _clockTimer = null;
            _stateTimer?.Stop();
            _stateTimer = null;

            // Save settings the user changed within the last 600 ms (fire and forget). Stop the
            // debounce FIRST: FlushParamsAsync re-arms it when a save is still running, and the
            // pending change must not be dropped by a Stop() after it. Once nothing is pending
            // after close, FlushParamsAsync stops the timer for good (see its finally).
            _closed = true;
            _paramsDebounce.Stop();
            if (_pendingParams.Count > 0) _ = FlushParamsAsync();

            NewsroomBackend.StatusChanged -= OnNewsroomBackendStatusChanged;
            FundNotifications.AutopilotSwitchedOff -= OnAutopilotSwitchedOff;
            MonitoringNotifier.Changed -= MonitoringNotifier_Changed;
            _newsroomWindow?.Close();
            _newsroomWindow = null;
            _keysWindow?.Close();
            _keysWindow = null;
            _libraryView?.CloseEditors();
            _studioView?.Shutdown();   // Strategy Studio: stops its run-status poll
            _loopView?.Shutdown();     // Discovery loop (stage 6): stops its 2 s poll
            _tradingView?.Shutdown();  // Portfolio & Trading (stage 7): stops its 5 s poll
            _monitoringView?.Shutdown(); // Monitoring (stage 9): stops its 10 s poll (the app-wide toast notifier keeps running)
            _shadowView?.Shutdown();     // Shadow Account (stage 9): no poll; ignores late answers
            // Market Research: stop its polling for good, then close the pop-out (its Closed handler sees _closed
            // and does not move the view back).
            _researchView?.Shutdown();
            _researchPopout?.Close();
            _researchPopout = null;
            // SPEC §14 (2026-09-26): closing this window no longer stops the backend — the
            // paper-trading engine keeps running as long as the UFOS.ai app does (Job Object in
            // NewsroomBackend), or beyond that in the opt-in "detached" mode.
        }

        private void UpdateClock()
        {
            ClockText.Text = DateTime.UtcNow.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " UTC";
        }

        // Dragging the title bar of a MAXIMIZED window: DragMove() does nothing there,
        // so the window used to be stuck in full screen. Like a native title bar, the
        // window is restored as soon as the mouse actually moves with the button held
        // (a plain click does nothing), placed so the cursor keeps its relative spot on
        // the title bar, and then dragged.
        private bool _restoreOnDrag;
        private Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (WindowState == WindowState.Maximized)
            {
                _restoreOnDrag = true;
                _restoreDragStart = e.GetPosition(this);
                ((UIElement)sender).CaptureMouse();
                return;
            }
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseMove(object sender, MouseEventArgs e)
        {
            if (!_restoreOnDrag || e.LeftButton != MouseButtonState.Pressed) return;
            var pos = e.GetPosition(this);
            if (Math.Abs(pos.X - _restoreDragStart.X) < SystemParameters.MinimumHorizontalDragDistance &&
                Math.Abs(pos.Y - _restoreDragStart.Y) < SystemParameters.MinimumVerticalDragDistance) return;

            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();

            var ratioX = ActualWidth > 0 ? pos.X / ActualWidth : 0.5;
            var restoreWidth = RestoreBounds.Width;
            // Cursor position in screen DIPs (PointToScreen returns device pixels).
            var toDip = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice ?? Matrix.Identity;
            var cursor = toDip.Transform(PointToScreen(pos));

            WindowState = WindowState.Normal;
            Left = cursor.X - restoreWidth * ratioX;
            Top = cursor.Y - 9; // middle of the 18 px title bar
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
        {
            if (!_restoreOnDrag) return;
            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();
        }

        // A maximized borderless window (WindowStyle=None) is placed by Windows so that
        // its invisible resize frame hangs off the screen edges, cutting off the title
        // bar and the top corners. Pad the content by that frame while maximized and
        // drop the rounded corners there (same fix as Newsroom/FinancialNewsroomWindow).
        /// <summary>True once the window is closing (Window_Closing ran) — the main app then opens a new window instead.</summary>
        public bool IsClosingOrClosed => _closed;

        /// <summary>The last state that was not minimized (Normal / Maximized) — restored by <see cref="BringToFront"/>.</summary>
        private WindowState _lastNonMinimizedState = WindowState.Normal;

        /// <summary>
        /// Live click-test 2026-10-22: the MainWindow tile and the insight card bring an open Hedge Fund window back — a minimized
        /// window returns to its last non-minimized state (Normal or Maximized), then it is activated and focused. Never throws.
        /// </summary>
        public void BringToFront()
        {
            if (_closed) return;
            try
            {
                if (!IsVisible) Show();
                if (WindowState == WindowState.Minimized) WindowState = _lastNonMinimizedState;
                Activate();
                Focus();
            }
            catch (Exception ex)
            {
                try { UFOS.ai.Logging.AppLog.Warn("HedgeFund", "Could not bring the Hedge Fund window to the front.", ex); } catch { }
            }
        }

        private void Window_StateChanged(object? sender, EventArgs e)
        {
            if (WindowState != WindowState.Minimized) _lastNonMinimizedState = WindowState;
            if (WindowState == WindowState.Maximized)
            {
                // 2026-10-23: the WM_GETMINMAXINFO hook (Shared/WindowMaximize.cs) fits the maximized window to the work area —
                // no frame padding (the old padding left a gap and the window covered the taskbar)
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(0);
                TitleBarBorder.CornerRadius = new CornerRadius(0);
            }
            else
            {
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(8);
                TitleBarBorder.CornerRadius = new CornerRadius(8, 8, 0, 0);
            }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        // ---------- Financial Newsroom submenu ----------

        // Opens the newsroom on its first section (NewsroomSections.All[0]); the user
        // navigates from there. Replaced the dropdown submenu on 2026-09-25.
        private void NewsroomMenuButton_Click(object sender, RoutedEventArgs e)
        {
            OpenNewsroom(null);
        }

        // ---------- Keys & data sources (Strategy Platform stage 1, Platform/KeysAndDataWindow) ----------

        /// <summary>Opens (or brings to front) the single "Keys &amp; data sources" window.</summary>
        private void KeysAndDataButton_Click(object sender, RoutedEventArgs e)
        {
            OpenKeysWindow(brokers: false);
        }

        /// <summary>
        /// Opens (or brings to front) the single Keys &amp; data window — "Settings"; with <paramref name="brokers"/> it scrolls to its
        /// Brokers section (Strategy Platform stage 8, SPEC §18.12 — "Brokers…" in Portfolio &amp; Trading).
        /// </summary>
        private void OpenKeysWindow(bool brokers)
        {
            if (_keysWindow == null)
            {
                var window = new KeysAndDataWindow { Owner = this };
                window.Closed += (_, _) =>
                {
                    if (ReferenceEquals(_keysWindow, window)) _keysWindow = null;
                };
                // live click-test 2026-10-22: a connect / resolve / route change in Settings reloads Portfolio & Trading at once
                window.BrokersChanged += () => _tradingView?.RequestRefresh();
                _keysWindow = window;
                if (brokers) window.Loaded += (_, _) => window.Dispatcher.BeginInvoke(new Action(window.ShowBrokersSection), System.Windows.Threading.DispatcherPriority.Loaded);
                window.Show();
            }
            else
            {
                if (_keysWindow.WindowState == WindowState.Minimized) _keysWindow.WindowState = WindowState.Normal;
                _keysWindow.Activate();
                if (brokers) _keysWindow.ShowBrokersSection();
            }
        }

        // ---------- Navigation rail (Strategy Platform stage 2, SPEC §7.12) ----------

        // Overview = the dashboard (OverviewScroll), Library = Platform/LibraryView (created lazily).
        // Only visibility changes: the clock, the 5 s state poll and the params debounce keep running
        // while the Library is shown (PollTickAsync depends on the WINDOW being visible), so the
        // Overview — banners, kill-switch state, positions — is current whenever the user returns.
        // The toolbar row (Newsroom, Keys & data, Run now, Kill switch) is outside this area and
        // stays pinned in both views.
        // Research (Market Research MR1) = MarketResearch/ResearchView, created lazily like the Library; it polls its
        // feed status only while it is visible, so switching away stops that poll and nothing else.
        // Loop (Strategy Platform stage 6) = Platform/LoopView, created lazily; it polls the selected loop only while visible.
        // Portfolio & Trading (Strategy Platform stage 7) = Platform/TradingView, created lazily; it polls only while visible.
        private async void NavButton_Checked(object sender, RoutedEventArgs e)
        {
            if (OverviewScroll == null || LibraryHost == null || ResearchHost == null || StudioHost == null || LoopHost == null || TradingHost == null
                || MonitoringHost == null || ShadowHost == null) return; // raised during InitializeComponent
            if (!ReferenceEquals(sender, NavTradingButton)) TradingHost.Visibility = Visibility.Collapsed;
            // Strategy Platform stage 9 (SPEC §19.13): Monitoring + Shadow Account (HedgeFundWindow.Monitoring.cs)
            if (!ReferenceEquals(sender, NavMonitoringButton)) MonitoringHost.Visibility = Visibility.Collapsed;
            if (!ReferenceEquals(sender, NavShadowButton)) ShadowHost.Visibility = Visibility.Collapsed;
            if (ReferenceEquals(sender, NavLibraryButton))
            {
                if (_libraryView == null)
                {
                    _libraryView = new LibraryView();
                    LibraryHost.Child = _libraryView;
                }
                OverviewScroll.Visibility = Visibility.Collapsed;
                ResearchHost.Visibility = Visibility.Collapsed;
                StudioHost.Visibility = Visibility.Collapsed;
                LoopHost.Visibility = Visibility.Collapsed;
                LibraryHost.Visibility = Visibility.Visible;
                // Studio "Open in Library" (SPEC §13.3): select that strategy instead of the last selection
                var pending = _pendingLibraryStrategyId;
                _pendingLibraryStrategyId = null;
                await (pending != null ? _libraryView.ShowStrategyAsync(pending) : _libraryView.ActivateAsync());
            }
            else if (ReferenceEquals(sender, NavStudioButton))
            {
                EnsureStudioView();
                OverviewScroll.Visibility = Visibility.Collapsed;
                LibraryHost.Visibility = Visibility.Collapsed;
                ResearchHost.Visibility = Visibility.Collapsed;
                LoopHost.Visibility = Visibility.Collapsed;
                StudioHost.Visibility = Visibility.Visible;
                await _studioView!.ActivateAsync();
            }
            else if (ReferenceEquals(sender, NavLoopButton))
            {
                EnsureLoopView();
                OverviewScroll.Visibility = Visibility.Collapsed;
                LibraryHost.Visibility = Visibility.Collapsed;
                ResearchHost.Visibility = Visibility.Collapsed;
                StudioHost.Visibility = Visibility.Collapsed;
                LoopHost.Visibility = Visibility.Visible;
                await _loopView!.ActivateAsync();
            }
            else if (ReferenceEquals(sender, NavTradingButton))
            {
                EnsureTradingView();
                OverviewScroll.Visibility = Visibility.Collapsed;
                LibraryHost.Visibility = Visibility.Collapsed;
                ResearchHost.Visibility = Visibility.Collapsed;
                StudioHost.Visibility = Visibility.Collapsed;
                LoopHost.Visibility = Visibility.Collapsed;
                TradingHost.Visibility = Visibility.Visible;
                await _tradingView!.ActivateAsync();
            }
            else if (ReferenceEquals(sender, NavMonitoringButton))
            {
                EnsureMonitoringView();
                OverviewScroll.Visibility = Visibility.Collapsed;
                LibraryHost.Visibility = Visibility.Collapsed;
                ResearchHost.Visibility = Visibility.Collapsed;
                StudioHost.Visibility = Visibility.Collapsed;
                LoopHost.Visibility = Visibility.Collapsed;
                MonitoringHost.Visibility = Visibility.Visible;
                await _monitoringView!.ActivateAsync();
            }
            else if (ReferenceEquals(sender, NavShadowButton))
            {
                EnsureShadowView();
                OverviewScroll.Visibility = Visibility.Collapsed;
                LibraryHost.Visibility = Visibility.Collapsed;
                ResearchHost.Visibility = Visibility.Collapsed;
                StudioHost.Visibility = Visibility.Collapsed;
                LoopHost.Visibility = Visibility.Collapsed;
                ShadowHost.Visibility = Visibility.Visible;
                await _shadowView!.ActivateAsync();
            }
            else if (ReferenceEquals(sender, NavResearchButton))
            {
                EnsureResearchView();
                OverviewScroll.Visibility = Visibility.Collapsed;
                LibraryHost.Visibility = Visibility.Collapsed;
                StudioHost.Visibility = Visibility.Collapsed;
                LoopHost.Visibility = Visibility.Collapsed;
                ResearchHost.Visibility = Visibility.Visible;
            }
            else
            {
                LibraryHost.Visibility = Visibility.Collapsed;
                ResearchHost.Visibility = Visibility.Collapsed;
                StudioHost.Visibility = Visibility.Collapsed;
                LoopHost.Visibility = Visibility.Collapsed;
                OverviewScroll.Visibility = Visibility.Visible;
            }
        }

        // ---------- Market Research section + pop-out (MR1, plan "Integrated architecture") ----------

        private void EnsureResearchView()
        {
            if (_researchView != null) return;
            _researchView = new ResearchView();
            _researchView.PopOutToggleRequested += ResearchView_PopOutToggleRequested;
            _researchView.OrderHandOverRequested += ResearchView_OrderHandOverRequested; // MR8: Buy/Sell → manual paper order panel
            ResearchViewHost.Child = _researchView;
        }

        // "⧉ Pop out" in the section moves the SAME view into the pop-out window; the same button in the pop-out
        // ("Return to Hedge Fund window") closes it, and ResearchPopout_Closed moves the view back.
        private void ResearchView_PopOutToggleRequested(object? sender, EventArgs e)
        {
            if (_researchPopout != null)
            {
                _researchPopout.Close();
                return;
            }
            if (_researchView == null || _closed) return;

            ResearchViewHost.Child = null; // a visual has one parent: detach before re-parenting
            var window = new ResearchPopoutWindow { Owner = this };
            window.Attach(_researchView);
            _researchView.IsPoppedOut = true;
            window.Closed += ResearchPopout_Closed;
            _researchPopout = window;
            ResearchPoppedOutPanel.Visibility = Visibility.Visible;
            window.Show();
        }

        private void ResearchPopout_Closed(object? sender, EventArgs e)
        {
            if (sender is not ResearchPopoutWindow window) return;
            window.Closed -= ResearchPopout_Closed;
            var view = window.ReleaseView();
            if (ReferenceEquals(_researchPopout, window)) _researchPopout = null;
            ResearchPoppedOutPanel.Visibility = Visibility.Collapsed;
            if (view == null) return;
            view.IsPoppedOut = false;
            if (_closed) return; // this window is closing: the view was shut down in Window_Closing
            ResearchViewHost.Child = view; // polls again only if the Research entry is the selected one (visible)
        }

        // MR8 (Market Research SPEC "MR8 backend notes", plan Q4): Buy/Sell/Short/Cover in Research only PRE-FILL the manual
        // paper order panel (ticker; size left empty) and show it — nothing is submitted. The panel's own validation,
        // confirmation dialog, POST /fund/orders and audit apply unchanged. Paper only, never live.
        private void ResearchView_OrderHandOverRequested(object? sender, MarketResearch.ResearchOrderHandOverEventArgs e)
        {
            if (_closed || !FundAssets.IsValidSymbol(e.Symbol)) return;
            var side = e.Side switch { "buy" => "Buy", "sell" => "Sell", "short" => "Short", "cover" => "Cover", _ => null };
            if (side == null) return;
            // S10 review C1: the hand-over opens Portfolio & Trading's order dialog (POST /v2/oms/orders), pre-filled — not /fund/orders
            if (WindowState == WindowState.Minimized) WindowState = _lastNonMinimizedState;
            Activate();
            NavTradingButton.IsChecked = true;
            EnsureTradingView();
            var symbol = e.Symbol;
            var sideKey = e.Side;
            Dispatcher.BeginInvoke(new Action(async () =>
            {
                try { if (_tradingView != null && !_closed) await _tradingView.OpenOrderFromResearchAsync(symbol, sideKey); }
                catch (Exception ex) { try { AppLog.Warn("HedgeFund", "Research order hand-over failed.", ex); } catch { } }
            }), DispatcherPriority.Loaded);
        }

        // MR8: "Open in Market Research" from a backtest report (Platform/BacktestReportView): shows the Research section
        // (or the pop-out) with the symbol and the run's markers.
        internal void OpenInResearch(string symbol, string? runId)
        {
            if (_closed) return;
            EnsureResearchView();
            if (_researchPopout != null)
            {
                if (_researchPopout.WindowState == WindowState.Minimized) _researchPopout.WindowState = WindowState.Normal;
                _researchPopout.Activate();
            }
            else
            {
                NavResearchButton.IsChecked = true;
                if (WindowState == WindowState.Minimized) WindowState = _lastNonMinimizedState;
                Activate();
            }
            _researchView?.OpenFromBacktest(symbol, runId);
        }

        private void ResearchShowPopout_Click(object sender, RoutedEventArgs e)
        {
            if (_researchPopout == null) return;
            if (_researchPopout.WindowState == WindowState.Minimized) _researchPopout.WindowState = WindowState.Normal;
            _researchPopout.Activate();
        }

        private void ResearchReturn_Click(object sender, RoutedEventArgs e)
        {
            _researchPopout?.Close();
        }

        /// <summary>Switches the rail to Overview (e.g. before scrolling to a dashboard control).</summary>
        private void ShowOverview()
        {
            if (NavOverviewButton.IsChecked != true) NavOverviewButton.IsChecked = true; // raises NavButton_Checked
            UpdateLayout(); // so a following BringIntoView finds the dashboard laid out
        }

        /// <summary>Opens (or brings to front) the single Financial Newsroom window on the given section.</summary>
        private void OpenNewsroom(string? sectionId)
        {
            if (_newsroomWindow == null)
            {
                var window = new FinancialNewsroomWindow { Owner = this };
                window.Closed += (_, _) =>
                {
                    if (ReferenceEquals(_newsroomWindow, window)) _newsroomWindow = null;
                };
                _newsroomWindow = window;
                window.Show();
            }
            else
            {
                if (_newsroomWindow.WindowState == WindowState.Minimized) _newsroomWindow.WindowState = WindowState.Normal;
                _newsroomWindow.Activate();
            }
            _newsroomWindow.ShowSection(sectionId);
            _ = NewsroomBackend.EnsureStartedAsync(); // no-op while running; restarts it if it exited
        }

        private void OnNewsroomBackendStatusChanged()
        {
            // Raised on whatever thread changed the status.
            Dispatcher.BeginInvoke(new Action(UpdateNewsroomStatus));
        }

        private void UpdateNewsroomStatus()
        {
            var status = NewsroomBackend.Status;
            var brush = new SolidColorBrush(FinancialNewsroomWindow.StatusColor(status));
            brush.Freeze();
            NewsroomStatusDot.Fill = brush;
            NewsroomStatusText.Text = "Newsroom backend: " + status;
        }

        // =====================================================================================
        // Polling / engine state
        // =====================================================================================

        private async Task PollTickAsync()
        {
            if (_polling || !IsVisible || WindowState == WindowState.Minimized) return;
            _polling = true;
            try
            {
                await RefreshStateAsync();
                if (_state == null || !_connected) return;

                if (_params == null ||
                    (_state.ParamsVersion is int v && v != _paramsVersion && _pendingParams.Count == 0 && !_paramsSaving))
                {
                    await LoadParamsAsync();
                }
                // Strategy Platform §11.2: the explanations' own daily budget + today's spend (refreshed every 30 s)
                if (DateTime.UtcNow - _explainSettingsLoadedAt > ExplainSettingsReloadInterval) await LoadExplainSettingsAsync();
                // Strategy Platform §13 (stage 5a): the Studio's ideation budget + this month's / today's spend (every 30 s)
                if (DateTime.UtcNow - _ideationSettingsLoadedAt > IdeationSettingsReloadInterval) await LoadIdeationSettingsAsync();
                // Strategy Platform §14 (stage 5b): committee auto-run setting (every 30 s)
                if (DateTime.UtcNow - _committeeSettingsLoadedAt > CommitteeSettingsReloadInterval) await LoadCommitteeSettingsAsync();

                var lastId = _state.Engine?.LastRun?.Id;
                if (lastId != _lastRunIdSeen || !_historyLoadedOnce)
                {
                    _lastRunIdSeen = lastId;
                    await LoadHistoryAsync();
                    await LoadEquityAsync();
                }
                else if (DateTime.UtcNow - _lastEquityLoad > EquityReloadInterval)
                {
                    await LoadEquityAsync();
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("HedgeFund", "Hedge Fund dashboard refresh failed.", ex); } catch { }
            }
            finally
            {
                _polling = false;
            }
        }

        private async Task RefreshStateAsync()
        {
            if (_stateLoading)
            {
                // The request in flight may have been sent before the action that asks for this
                // refresh (kill / resume / order) — fetch once more when it is done.
                _refreshAgain = true;
                return;
            }
            _stateLoading = true;
            try
            {
                do
                {
                    _refreshAgain = false;
                    await RefreshStateOnceAsync();
                } while (_refreshAgain && !_closed);
            }
            finally
            {
                _stateLoading = false;
                _refreshAgain = false;
            }
        }

        private async Task RefreshStateOnceAsync()
        {
            // S10-C R3: after the switch-over the old engine is replaced — its /fund routes answer 410, nothing to poll
            if (_councilReplaced) return;
            try
            {
                var result = await FundClient.GetStateAsync();
                if (!result.Ok && result.HttpStatus == 410)
                {
                    // replaced while this window was open (or before the Council view's first load): let the Council view decide the layout
                    if (_councilView != null) await _councilView.RefreshAsync();
                    return;
                }
                if (!result.Ok || result.Value == null)
                {
                    ShowConnectionProblem(result.Status, result.Describe());
                    return;
                }
                _state = result.Value;
                SetConnectedUi(true);
                ApplyState();
            }
            catch (Exception ex)
            {
                ShowConnectionProblem(FundCallStatus.Error, "Could not show the engine state (" + ex.GetType().Name + ").");
            }
        }

        private void SetConnectedUi(bool connected)
        {
            _connected = connected;
            if (connected) ConnectionBanner.Visibility = Visibility.Collapsed;
            RunNowButton.IsEnabled = connected;
            RunNowButton.Opacity = connected ? 1 : 0.5;
            AutoEngageToggle.IsEnabled = connected && _state?.Halted?.Active != true;
            UpdateExecuteButton();
        }

        private void ShowConnectionProblem(FundCallStatus status, string message)
        {
            SetConnectedUi(false);
            var backend = NewsroomBackend.Status;
            string text = status switch
            {
                FundCallStatus.Offline when !backend.StartsWith("online", StringComparison.OrdinalIgnoreCase)
                    => "Engine offline — Hedge Fund backend: " + backend + ".",
                FundCallStatus.Offline => "Engine not reachable — " + message,
                FundCallStatus.NotFound => "The running backend has no paper-trading API (/fund) — it is probably outdated. " +
                                           "Close UFOS.ai (or end python.exe in Task Manager) and start again.",
                _ => message,
            };
            if (_state != null) text += " Showing the last known state.";
            ConnectionBannerText.Text = text;
            ConnectionBanner.Visibility = Visibility.Visible;
            UpdateStatusLine();
        }

        private void ApplyState()
        {
            var s = _state;
            if (s == null) return;

            // "Run now" was clicked: show the AI loading label until that run has finished.
            if (_runRequested)
            {
                var lastRun = s.Engine?.LastRun;
                var lastId = lastRun?.Id;
                var finished = lastId != null && lastId != _runRequestedFromId && s.Engine?.CycleInProgress != true &&
                               IsRunForRequest(lastRun!);
                if (finished || DateTime.UtcNow - _runRequestedAt > TimeSpan.FromMinutes(5)) _runRequested = false;
            }

            UpdateChips(s);
            UpdateHalt(s);
            UpdateAccountBanner(s);
            UpdateLegacyBanner(s);
            UpdateSandboxStrip(s);
            UpdateStatusLine();
            UpdateAgentsAndVerdict();
            UpdatePortfolio(s);
            UpdateAutopilot(s);
            UpdateHeartbeatHint();
            UpdateOrderUnitLabel();
        }

        /// <summary>
        /// Does this finished run answer the user's "▶ Run now"? A "risk-watch" run never does (it
        /// runs on its own timer outside US hours); a "manual" run always does; any other trigger only
        /// when it started after the click. Older backends without a trigger: any new run (as before).
        /// </summary>
        private bool IsRunForRequest(FundRunInfo run)
        {
            if (FundRunDetailsWindow.IsRiskWatch(run.Trigger)) return false;
            if (string.IsNullOrWhiteSpace(run.Trigger) || string.Equals(run.Trigger, "manual", StringComparison.OrdinalIgnoreCase)) return true;
            var started = FundFormat.ParseTs(run.StartedAt);
            var clicked = new DateTimeOffset(DateTime.SpecifyKind(_runRequestedAt, DateTimeKind.Utc));
            return started.HasValue && started.Value >= clicked.AddSeconds(-2);
        }

        private FundSymbolState? CurrentSymbolState()
            => _state?.Symbols?.FirstOrDefault(x => string.Equals(x.Symbol, _selectedSymbol, StringComparison.OrdinalIgnoreCase));

        /// <summary>Same rule as the verdict panel: AI provenance present, or source "llm".</summary>
        private static bool IsAiRecommendation(FundRecommendation rec, out string? model, out DateTimeOffset? at)
            => FundAi.TryRead(rec.Provenance, rec.Model, rec.Ts, out model, out at) ||
               string.Equals(rec.Source, "llm", StringComparison.OrdinalIgnoreCase);

        private bool IsAiLoading()
        {
            var s = _state;
            if (s == null) return false;
            if (!_runRequested && s.Engine?.CycleInProgress != true) return false;
            var b = s.AiBudget;
            return b != null && !b.Exhausted && b.SpendUnreadable != true && (b.LimitUsd ?? 0) > 0;
        }

        // ---------- header: chips, halt, status line ----------

        private void UpdateChips(FundState s)
        {
            var symbols = (s.Symbols ?? new List<FundSymbolState>())
                .Select(x => x.Symbol).Where(x => !string.IsNullOrWhiteSpace(x)).Distinct().ToList();
            if (symbols.Count == 0) symbols = WatchlistFromParams();
            if (_selectedSymbol == null || !symbols.Contains(_selectedSymbol))
            {
                // Keep a symbol selected via a position row even if it is not on the watchlist.
                if (_selectedSymbol == null || (s.Positions?.Any(p => p.Symbol == _selectedSymbol) != true))
                    _selectedSymbol = symbols.FirstOrDefault();
            }

            var signature = string.Join(",", symbols);
            if (signature != _chipSignature)
            {
                _chipSignature = signature;
                _rebuildingChips = true;
                try
                {
                    WatchlistChipsPanel.Children.Clear();
                    var style = WatchlistChipsPanel.TryFindResource("ChipStyle") as Style;
                    foreach (var sym in symbols)
                    {
                        var chip = new RadioButton { GroupName = "Watchlist", Tag = sym, IsChecked = sym == _selectedSymbol };
                        if (style != null) chip.Style = style;
                        chip.Checked += Chip_Checked;
                        WatchlistChipsPanel.Children.Add(chip);
                    }
                }
                finally
                {
                    _rebuildingChips = false;
                }
            }

            foreach (var chip in WatchlistChipsPanel.Children.OfType<RadioButton>())
            {
                if (chip.Tag is not string sym) continue;
                var symState = s.Symbols?.FirstOrDefault(x => x.Symbol == sym);
                var rec = symState?.Recommendation;
                var action = (rec?.Action ?? "").ToUpperInvariant();
                var panel = new StackPanel { Orientation = Orientation.Horizontal };
                panel.Children.Add(new TextBlock { Text = sym, VerticalAlignment = VerticalAlignment.Center });
                var marker = action switch { "LONG" => "▲", "SHORT" => "▼", "FLAT" => "•", _ => "" };
                if (marker.Length > 0)
                {
                    panel.Children.Add(new TextBlock
                    {
                        Text = marker,
                        Margin = new Thickness(5, 0, 0, 0),
                        Foreground = FundPalette.ForAction(action),
                        VerticalAlignment = VerticalAlignment.Center,
                    });
                }
                // 24/7 crypto (optional fields): "24/7" marker for crypto, dimmed "closed" while the symbol
                // cannot fill right now (equities outside the US session).
                var isCrypto = symState?.IsCrypto == true;
                var closedNow = symState?.TradableNow == false;
                if (isCrypto) panel.Children.Add(ChipMarker("24/7", FundPalette.Focus));
                if (closedNow) panel.Children.Add(ChipMarker("closed", FundPalette.Faint));
                // D9 (2026-10-02): status of the last cycle — data missing / no new verdict. Status, not a verdict.
                var dataNa = string.IsNullOrWhiteSpace(symState?.DataUnavailable) ? null : DataUnavailableText(symState!.DataUnavailable!);
                var judgeNa = string.IsNullOrWhiteSpace(symState?.JudgeUnavailable) ? null : symState!.JudgeUnavailable!.Trim();
                var notUpdated = string.IsNullOrWhiteSpace(rec?.NotUpdated) ? null : NotUpdatedText(rec!.NotUpdated!);
                if (dataNa != null) panel.Children.Add(ChipMarker("no data", FundPalette.Kill));
                else if (judgeNa != null || notUpdated != null) panel.Children.Add(ChipMarker("not updated", FundPalette.Risk));
                var statusNote = string.Join("\n", new[] { dataNa, dataNa == null ? judgeNa : null, notUpdated }
                    .Where(x => !string.IsNullOrWhiteSpace(x)).Distinct());
                string tradeNote = isCrypto ? (closedNow ? "crypto · currently not tradable" : "crypto · trades 24/7, fractional quantities")
                    : closedNow ? "market closed — orders queue until the next open" : "";
                if (statusNote.Length > 0) tradeNote = tradeNote.Length > 0 ? tradeNote + "\n" + statusNote : statusNote;
                chip.Content = panel;
                // AI labelling (CLAUDE.md rule 6): a verdict from the language model says so in the
                // tooltip and the screen-reader name; everything else is marked rule-based.
                if (rec == null)
                {
                    AutomationProperties.SetName(chip, sym + ", no verdict yet" + (tradeNote.Length > 0 ? ", " + tradeNote : ""));
                    chip.ToolTip = sym + " — no verdict yet" + (tradeNote.Length > 0 ? "\n" + tradeNote : "");
                    continue;
                }
                var isAi = IsAiRecommendation(rec, out var model, out var at);
                var origin = isAi
                    ? "AI-generated verdict" + (AiContent.ShortModelName(model) is { Length: > 0 } sm ? " (" + sm + (at.HasValue ? ", " + at.Value.ToLocalTime().ToString("HH:mm", Inv) : "") + ")" : "")
                    : string.Equals(rec.Source, "rules", StringComparison.OrdinalIgnoreCase)
                        ? "rule-based verdict (no AI, older engine version)"
                        : "verdict source: " + (rec.Source ?? "unknown") + " (not AI-labelled)";
                if (notUpdated != null) origin += " · from " + FundFormat.LocalTime(rec.Ts, "MM-dd HH:mm");
                if (string.Equals(rec.Source, "carried", StringComparison.OrdinalIgnoreCase))
                    origin += " · carried" + (string.IsNullOrWhiteSpace(rec.JudgedAt) ? "" : " · judged " + FundFormat.LocalTime(rec.JudgedAt, "HH:mm"));
                var conf = FundFormat.Fraction(rec.Confidence, 0);
                AutomationProperties.SetName(chip, (isAi
                    ? $"AI-generated verdict for {sym}: {(action.Length > 0 ? action : "none")}, confidence {conf}"
                    : $"{sym}, verdict {(action.Length > 0 ? action : "none")} (not AI-generated), confidence {conf}") +
                    (tradeNote.Length > 0 ? ", " + tradeNote : ""));
                chip.ToolTip = $"{sym} — {(action.Length > 0 ? action : "—")}, confidence {conf}\n{origin}" +
                               (isAi ? "\n" + AiContent.Caveat : "") +
                               (tradeNote.Length > 0 ? "\n" + tradeNote : "");
            }
        }

        private static TextBlock ChipMarker(string text, Brush brush) => new()
        {
            Text = text,
            FontSize = 8.5,
            Margin = new Thickness(5, 1, 0, 0),
            Foreground = brush,
            VerticalAlignment = VerticalAlignment.Center,
        };

        /// <summary>
        /// Crypto pair the engine trades 24/7 with fractional quantities? Uses asset_class from the
        /// state when known; otherwise the crypto symbol pattern — but only when the running backend
        /// supports crypto at all (an older one would treat "BTC-USD" as a stock).
        /// </summary>
        private bool IsCryptoSymbol(string? symbol)
        {
            if (string.IsNullOrWhiteSpace(symbol)) return false;
            var known = _state?.Symbols?.FirstOrDefault(x => string.Equals(x.Symbol, symbol, StringComparison.OrdinalIgnoreCase));
            if (!string.IsNullOrWhiteSpace(known?.AssetClass)) return known!.IsCrypto;
            var pos = _state?.Positions?.FirstOrDefault(x => string.Equals(x.Symbol, symbol, StringComparison.OrdinalIgnoreCase));
            if (!string.IsNullOrWhiteSpace(pos?.AssetClass)) return pos!.IsCrypto;
            return BackendSupportsCrypto() && FundAssets.LooksLikeCrypto(symbol);
        }

        private bool BackendSupportsCrypto()
            => _cryptoSupported == true ||
               !string.IsNullOrWhiteSpace(_state?.Engine?.ScheduleMode) ||
               _state?.Symbols?.Any(x => !string.IsNullOrWhiteSpace(x.AssetClass)) == true;

        private void Chip_Checked(object sender, RoutedEventArgs e)
        {
            if (_rebuildingChips) return;
            if (sender is RadioButton rb && rb.Tag is string sym) SelectSymbol(sym, fromChip: true);
        }

        private void SelectSymbol(string symbol, bool fromChip = false)
        {
            _selectedSymbol = symbol;
            if (!fromChip)
            {
                _rebuildingChips = true;
                try
                {
                    foreach (var chip in WatchlistChipsPanel.Children.OfType<RadioButton>())
                        chip.IsChecked = chip.Tag is string t && t == symbol;
                }
                finally
                {
                    _rebuildingChips = false;
                }
            }
            if (!TickerInput.IsKeyboardFocusWithin) TickerInput.Text = symbol;
            UpdateAgentsAndVerdict();
        }

        private List<string> WatchlistFromParams()
        {
            var list = new List<string>();
            if (_params?.Params is JsonElement p && p.ValueKind == JsonValueKind.Object &&
                p.TryGetProperty("watchlist", out var wl) && wl.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in wl.EnumerateArray())
                    if (item.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(item.GetString())) list.Add(item.GetString()!);
            }
            return list;
        }

        private void UpdateHalt(FundState s)
        {
            var halted = s.Halted?.Active == true;
            ResumeButton.Visibility = halted ? Visibility.Visible : Visibility.Collapsed;
            if (!halted)
            {
                HaltBanner.Visibility = Visibility.Collapsed;
                return;
            }
            var reason = string.IsNullOrWhiteSpace(s.Halted?.Reason) ? "no reason given" : s.Halted!.Reason;
            HaltBannerText.Text = $"■ Engine halted since {FundFormat.LocalTime(s.Halted?.At, "yyyy-MM-dd HH:mm")} — {reason}. " +
                                  "The autopilot is off and all simulated positions were closed (queued until the next open while the market is closed). " +
                                  "Use \"Resume engine\" to allow trading again.";
            HaltBanner.Visibility = Visibility.Visible;
        }

        private void UpdateStatusLine()
        {
            var s = _state;
            if (s == null)
            {
                EngineStatusText.Text = _connected ? "Engine: loading…" : "Engine: waiting for the Hedge Fund backend… (" + NewsroomBackend.Status + ")";
                EngineStatusDot.Fill = FundPalette.Faint;
                return;
            }

            var parts = new List<string>();
            if (!_connected) parts.Add("OFFLINE — last known state");
            var engine = s.Engine;
            var nextRun = FundFormat.ParseTs(engine?.NextRunAt);
            var manualOnly = engine?.IsManualOnly == true;
            if (engine?.CycleInProgress == true) parts.Add("Run in progress…");
            else if (_runRequested) parts.Add("Run queued…");
            if (manualOnly)
            {
                // Outside the US session (schedule_mode "manual_only"): no scheduled cycles at all.
                parts.Add("US market closed — trading only via ▶ Run now (crypto 24/7, stock orders wait for the open)");
                // Risk watch: engine.crypto_risk_watch / risk_watch_interval_minutes (optional fields).
                var watchEvery = engine!.RiskWatchIntervalMinutes is double wm && wm > 0 ? MinutesLabel(wm) : null;
                var nextWatch = FundFormat.ParseTs(engine.NextRiskWatchAt);
                if (engine.CryptoRiskWatch == false)
                    parts.Add("crypto risk watch off");
                else if (nextWatch.HasValue)
                    parts.Add("next risk watch " + nextWatch.Value.ToLocalTime().ToString("HH:mm", Inv) +
                              " (in " + FundFormat.Countdown(nextWatch.Value - DateTimeOffset.UtcNow) +
                              (watchEvery != null ? ", every " + watchEvery : "") + ")");
                else
                    parts.Add(watchEvery != null
                        ? "risk watch idle — no open crypto positions (checks every " + watchEvery + " while there are)"
                        : "no risk watch pending");
            }
            else if (engine?.CycleInProgress == true || _runRequested) { /* shown above */ }
            else if (nextRun.HasValue) parts.Add("Next run in " + FundFormat.Countdown(nextRun.Value - DateTimeOffset.UtcNow));
            else parts.Add(engine?.Running == false ? "Engine loop stopped" : "Next run: —");

            var last = engine?.LastRun;
            parts.Add(last == null ? "no runs yet"
                : $"last run {FundFormat.LocalTime(last.StartedAt)} {last.Status}" + (string.IsNullOrWhiteSpace(last.JudgeMode) ? "" : " (" + last.JudgeMode + ")"));

            var budget = s.AiBudget;
            if (budget != null)
            {
                parts.Add(budget.SpendUnreadable == true
                    ? "AI spend couldn't be read — AI calls paused (fails closed)"
                    : $"AI today {FundFormat.Usd2(budget.SpentTodayUsd)} / {FundFormat.Usd2(budget.LimitUsd)}" +
                      (budget.Exhausted ? " (budget reached → no new AI verdicts)" : ""));
            }

            var market = s.Market;
            if (market != null)
            {
                if (manualOnly)
                {
                    // next_session_run_at: the next scheduled full cycle (first run of the next session).
                    var nextSession = FundFormat.ParseTs(engine?.NextSessionRunAt);
                    var opens = FundFormat.ParseTs(market.NextOpen).HasValue ? "US opens " + FundFormat.LocalTime(market.NextOpen, "ddd HH:mm") : null;
                    var sessionRun = nextSession.HasValue
                        ? "next scheduled run " + nextSession.Value.ToLocalTime().ToString("ddd HH:mm", Inv) +
                          ", in " + FundFormat.Countdown(nextSession.Value - DateTimeOffset.UtcNow)
                        : null;
                    if (opens != null) parts.Add(opens + (sessionRun != null ? " (" + sessionRun + ")" : ""));
                    else if (sessionRun != null) parts.Add(sessionRun);
                }
                else
                {
                    parts.Add(market.Open ? "market open"
                        : "market closed" + (FundFormat.ParseTs(market.NextOpen).HasValue ? " · opens " + FundFormat.LocalTime(market.NextOpen, "ddd HH:mm") : ""));
                }
                if (market.DelayedQuotes) parts.Add("quotes ~15 min delayed");
            }
            parts.Add(s.Autopilot ? "autopilot ON" + AutopilotCountdownText(s)
                : (RecentAutopilotOffEvent() is FundEvent off) ? "autopilot off (switched off by the heartbeat at " + FundFormat.LocalTime(off.Ts) + ")"
                : "autopilot off");
            if (s.PendingParams) parts.Add("new settings apply from the next run");

            EngineStatusText.Text = string.Join(" · ", parts);
            EngineStatusDot.Fill = !_connected ? FundPalette.Kill
                : s.Halted?.Active == true ? FundPalette.Kill
                : engine?.CycleInProgress == true ? FundPalette.Focus
                : string.Equals(last?.Status, "error", StringComparison.OrdinalIgnoreCase) ? FundPalette.Short
                : FundPalette.Long;
            AutomationProperties.SetName(EngineStatusText, "Engine status: " + EngineStatusText.Text);
            UpdateRunNowProminence(manualOnly && _connected);
        }

        /// <summary>
        /// schedule_mode "manual_only": ▶ Run now is the only way to trade until the US open, so it
        /// is made prominent (brighter fill, thicker border, hint text). Applied on changes only.
        /// </summary>
        private void UpdateRunNowProminence(bool prominent)
        {
            if (_runNowProminent == prominent) return;
            _runNowProminent = prominent;
            if (prominent)
            {
                RunNowButton.Background = RunNowProminentBackground;
                RunNowButton.Foreground = FundPalette.TextBright;
                RunNowButton.BorderThickness = new Thickness(2);
                RunNowButton.ToolTip = "US market closed: there are no scheduled runs until the open. ▶ Run now runs one full cycle " +
                                       "(Council of AIs: agents + judge) — crypto orders fill immediately, stock/ETF orders are queued for the next open. " +
                                       AiContent.TriggerTooltip;
                RunNowHintText.Visibility = Visibility.Visible;
            }
            else
            {
                RunNowButton.ClearValue(BackgroundProperty);
                RunNowButton.ClearValue(ForegroundProperty);
                RunNowButton.ClearValue(BorderThicknessProperty);
                RunNowButton.ToolTip = _runNowTooltip;
                RunNowHintText.Visibility = Visibility.Collapsed;
            }
        }

        /// <summary>" · switches off in m:ss without the app" from engine.autopilot_off_at (dead-man switch).</summary>
        private static string AutopilotCountdownText(FundState s)
        {
            var offAt = FundFormat.ParseTs(s.Engine?.AutopilotOffAt);
            if (!s.Autopilot || !offAt.HasValue) return "";
            return " · switches off in " + FundFormat.Countdown(offAt.Value - DateTimeOffset.UtcNow) + " without the app";
        }

        /// <summary>account.unreadable: red banner with the error; the figures show "—" (all null).</summary>
        private void UpdateAccountBanner(FundState s)
        {
            var a = s.Account;
            if (a?.Unreadable != true)
            {
                AccountBanner.Visibility = Visibility.Collapsed;
                return;
            }
            AccountBannerText.Text = "Account state unreadable — autopilot, drawdown checks and snapshots paused this run" +
                                     (string.IsNullOrWhiteSpace(a!.Error) ? "." : ": " + a.Error!.Trim());
            AccountBanner.Visibility = Visibility.Visible;
            AutomationProperties.SetName(AccountBannerText, AccountBannerText.Text);
        }

        /// <summary>
        /// Strategy Platform stage 7 (SPEC §17.1, coexistence): after the migration of this paper account the engine is
        /// advisory only (it analyses; every trading path answers 409 with the backend's text) — say so where the user
        /// trades. Absent on older backends (no banner).
        /// </summary>
        /// <summary>
        /// Sandbox profile (strategy_platform/SPEC.md "Sandbox profile" S2): the amber strip under the title bar stays visible while
        /// the state says mode == "sandbox"; a state with mode "normal" (or an older backend without the field) hides it. Text =
        /// the payload's banner + "Brokers blocked — data: &lt;data_dir&gt;" (plain text, verbatim; missing parts say so).
        /// </summary>
        private void UpdateSandboxStrip(FundState s)
        {
            if (!s.IsSandbox)
            {
                SandboxStrip.Visibility = Visibility.Collapsed;
                return;
            }
            var sb = s.Sandbox;
            var banner = string.IsNullOrWhiteSpace(sb?.Banner)
                ? "Sandbox mode — banner text not available (the state payload has no sandbox banner)"
                : sb!.Banner!.Trim();
            var dataDir = string.IsNullOrWhiteSpace(sb?.DataDir) ? "not available (not in the state payload)" : sb!.DataDir!.Trim();
            SandboxStripText.Text = banner + "  ·  Brokers blocked — data: " + dataDir
                                    + (string.IsNullOrWhiteSpace(sb?.PricesNote) ? "" : "  ·  " + sb!.PricesNote!.Trim());   // fix1023 B6
            SandboxStrip.ToolTip = sb?.Since is string since && !string.IsNullOrWhiteSpace(since)
                ? "Sandbox active since " + FundFormat.LocalTime(since, "yyyy-MM-dd HH:mm") + " · " + (sb.Brokers ?? "brokers: not reported")
                : sb?.Brokers;
            SandboxStrip.Visibility = Visibility.Visible;
            AutomationProperties.SetName(SandboxStrip, "Sandbox mode: " + SandboxStripText.Text);
        }

        private void UpdateLegacyBanner(FundState s)
        {
            var la = s.LegacyAccount;
            if (la == null || la.Status == null || la.Status == "active")
            {
                LegacyBanner.Visibility = Visibility.Collapsed;
                return;
            }
            LegacyBannerText.Text = la.Status == "migrating"
                ? "This paper account is being migrated to the Strategy Platform — it is frozen (no trading) until the copy is complete."
                : "This paper account was migrated to the Strategy Platform on " + FundFormat.LocalTime(la.MigratedAt, "yyyy-MM-dd HH:mm") +
                  " — it is traded in Portfolio & Trading now (account paper-council, source \"Council of AIs\"). The engine here keeps analysing " +
                  "(agents, AI judge, recommendations) but places no orders; the kill switch still covers both.";
            LegacyBanner.Visibility = Visibility.Visible;
            AutomationProperties.SetName(LegacyBannerText, LegacyBannerText.Text);
        }

        // ---------- agent cards + verdict ----------

        private void UpdateAgentsAndVerdict()
        {
            var sym = CurrentSymbolState();
            var rec = sym?.Recommendation;
            AgentsSubtitleText.Text = sym == null ? (_selectedSymbol ?? "—")
                : sym.Symbol + (rec?.RunId is long runId ? " · run #" + runId.ToString(Inv) : "");

            FundAgentSignal? Find(string agent)
                => sym?.Agents?.FirstOrDefault(a => string.Equals(a.Agent, agent, StringComparison.OrdinalIgnoreCase));

            UpdateDirectionalCard(Find("technical"), TechCard, TechStripe, TechStance, TechBarNeg, TechBarPos, TechSummary, TechConf, TechMeta, "Technical");
            UpdateDirectionalCard(Find("macro"), MacroCard, MacroStripe, MacroStance, MacroBarNeg, MacroBarPos, MacroSummary, MacroConf, MacroMeta, "Macro");
            UpdateDirectionalCard(Find("sentiment"), SentCard, SentStripe, SentStance, SentBarNeg, SentBarPos, SentSummary, SentConf, SentMeta, "Sentiment", isSentiment: true);
            UpdateRiskCard(Find("risk"));
            UpdateVerdict(sym);

            // D9: market data missing in the last cycle → this symbol was not analysed (no fallback).
            var dataNa = string.IsNullOrWhiteSpace(sym?.DataUnavailable) ? null : DataUnavailableText(sym!.DataUnavailable!);
            AgentsDataBannerText.Text = dataNa ?? "";
            AgentsDataBanner.Visibility = dataNa == null ? Visibility.Collapsed : Visibility.Visible;
            AutomationProperties.SetName(AgentsDataBannerText, dataNa ?? "");
        }

        /// <summary>"Market data unavailable: … — not analysed this run" (backend text kept when it already says so).</summary>
        private static string DataUnavailableText(string reason)
        {
            var r = reason.Trim();
            return r.StartsWith("Market data unavailable", StringComparison.OrdinalIgnoreCase)
                ? r
                : "Market data unavailable: " + r.TrimEnd('.') + " — not analysed this run.";
        }

        /// <summary>"not updated: &lt;reason&gt;" (backend text kept when it already starts that way).</summary>
        private static string NotUpdatedText(string reason)
        {
            var r = reason.Trim();
            return r.StartsWith("not updated", StringComparison.OrdinalIgnoreCase) ? r : "not updated: " + r;
        }

        /// <summary>Notice above the verdict rationale (status, not AI output) + the matching settings button.</summary>
        private void SetVerdictNotice(string? text)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                VerdictNoticeBorder.Visibility = Visibility.Collapsed;
                VerdictNoticeText.Text = "";
                VerdictNoticeButton.Visibility = Visibility.Collapsed;
                VerdictNoticeButton.Tag = null;
                return;
            }
            VerdictNoticeText.Text = text;
            VerdictNoticeBorder.Visibility = Visibility.Visible;
            AutomationProperties.SetName(VerdictNoticeText, "Status: " + text);
            if (text.Contains("budget", StringComparison.OrdinalIgnoreCase))
            {
                VerdictNoticeButton.Content = "Open AI budget setting";
                VerdictNoticeButton.Tag = "budget";
                VerdictNoticeButton.ToolTip = "Scrolls to the \"AI daily budget (USD)\" input in the engine settings.";
                VerdictNoticeButton.Visibility = Visibility.Visible;
            }
            else if (text.Contains("Settings → Keys", StringComparison.OrdinalIgnoreCase) ||
                     text.Contains("is not set", StringComparison.OrdinalIgnoreCase) ||
                     text.Contains("Credential Manager", StringComparison.OrdinalIgnoreCase))
            {
                VerdictNoticeButton.Content = "Open Keys & data";
                VerdictNoticeButton.Tag = "keys";
                VerdictNoticeButton.ToolTip = "Opens Keys & data sources to set the missing key.";
                VerdictNoticeButton.Visibility = Visibility.Visible;
            }
            else
            {
                VerdictNoticeButton.Visibility = Visibility.Collapsed;
                VerdictNoticeButton.Tag = null;
            }
        }

        private DispatcherTimer? _budgetHighlightTimer;

        private void VerdictNoticeButton_Click(object sender, RoutedEventArgs e)
        {
            if (VerdictNoticeButton.Tag is "keys")
            {
                KeysAndDataButton_Click(sender, e);
                return;
            }
            if (VerdictNoticeButton.Tag is not "budget") return;
            ShowOverview(); // the AI budget input lives on the Overview
            // Scroll to and highlight the AI budget input (engine settings, always visible).
            AiBudgetInput.BringIntoView();
            AiBudgetInput.Focus();
            AiBudgetInput.SelectAll();
            AiBudgetInput.BorderBrush = FundPalette.Risk;
            AiBudgetInput.BorderThickness = new Thickness(2);
            _budgetHighlightTimer?.Stop();
            _budgetHighlightTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(4) };
            _budgetHighlightTimer.Tick += (_, _) =>
            {
                _budgetHighlightTimer?.Stop();
                AiBudgetInput.ClearValue(Control.BorderBrushProperty);
                AiBudgetInput.ClearValue(Control.BorderThicknessProperty);
            };
            _budgetHighlightTimer.Start();
        }

        private void UpdateDirectionalCard(FundAgentSignal? a, Border card, Border stripe, TextBlock stance, Border barNeg, Border barPos,
                                           TextBlock summary, TextBlock conf, TextBlock meta, string title, bool isSentiment = false)
        {
            if (a == null)
            {
                stance.Text = "—";
                stance.Foreground = FundPalette.Neutral;
                stripe.Background = FundPalette.Neutral;
                barNeg.RenderTransform = new ScaleTransform(0, 1);
                barPos.RenderTransform = new ScaleTransform(0, 1);
                summary.Text = _state == null ? "Waiting for the engine…" : "No signal yet — waiting for the first run with this symbol.";
                if (_summaryForeground.TryGetValue(summary, out var originalForeground)) summary.Foreground = originalForeground;
                conf.Text = "conf —";
                meta.Text = isSentiment ? "—" : "rule-based";
                card.ToolTip = null;
                AutomationProperties.SetName(summary, title + ": " + summary.Text);
                if (isSentiment) SetSentimentLabel(null);
                return;
            }

            var score = Math.Clamp(a.Score ?? 0, -1, 1);
            var confidence = Math.Clamp(a.Confidence ?? 0, 0, 1);
            string stanceText;
            Brush brush;
            if (confidence <= 0) { stanceText = "N/A"; brush = FundPalette.Neutral; }
            else if (score >= 0.15) { stanceText = "LONG"; brush = FundPalette.Long; }
            else if (score <= -0.15) { stanceText = "SHORT"; brush = FundPalette.Short; }
            else { stanceText = "NEUTRAL"; brush = FundPalette.Neutral; }

            stance.Text = stanceText;
            stance.Foreground = brush;
            stripe.Background = brush;
            barNeg.RenderTransform = new ScaleTransform(score < 0 ? -score : 0, 1);
            barPos.RenderTransform = new ScaleTransform(score > 0 ? score : 0, 1);
            summary.Text = string.IsNullOrWhiteSpace(a.Summary) ? "(no summary)" : a.Summary;
            conf.Text = "score " + (a.Score.HasValue ? score.ToString("+0.00;-0.00;0.00", Inv) : "—") +
                        " · conf " + (a.Confidence.HasValue ? (confidence * 100).ToString("0", Inv) + "%" : "—");
            meta.Text = SourceLabel(a.Source);
            card.ToolTip = DetailsTooltip(title, a);
            // D9 (2026-10-02): an unavailable rule-based agent (e.g. macro data missing) shows its reason as
            // a status (amber, no stance) — never as a signal. No AI styling (it is not AI output).
            if (!isSentiment)
            {
                if (!_summaryForeground.ContainsKey(summary)) _summaryForeground[summary] = summary.Foreground;
                var unavailable = string.Equals(a.Source, "unavailable", StringComparison.OrdinalIgnoreCase);
                summary.Foreground = unavailable ? FundPalette.Risk : _summaryForeground[summary];
                if (unavailable)
                {
                    stance.Text = "N/A";
                    stance.Foreground = FundPalette.Neutral;
                    stripe.Background = FundPalette.Risk;
                    barNeg.RenderTransform = new ScaleTransform(0, 1);
                    barPos.RenderTransform = new ScaleTransform(0, 1);
                    summary.Text = "Unavailable: " + (string.IsNullOrWhiteSpace(a.Summary) ? "no reason given" : a.Summary!.Trim());
                    conf.Text = "not analysed";
                }
            }
            if (isSentiment) SetSentimentLabel(a);
            else AutomationProperties.SetName(summary, title + " agent (rule-based)" +
                (string.Equals(a.Source, "unavailable", StringComparison.OrdinalIgnoreCase) ? ", status: " : ": ") + summary.Text);
        }

        private readonly Dictionary<TextBlock, Brush> _summaryForeground = new();

        private static string SourceLabel(string? source) => (source ?? "").ToLowerInvariant() switch
        {
            "rules" => "rule-based",
            "llm" => AiContent.Glyph + " AI",
            "cached" => AiContent.Glyph + " AI · cached",
            "unavailable" => "unavailable",
            "" => "—",
            _ => source!,
        };

        /// <summary>AI label on the sentiment card when its text came from the language model (or its cache).</summary>
        private void SetSentimentLabel(FundAgentSignal? a)
        {
            // Backend 2026-10-02: insider data could not be loaded → details.insider_unavailable holds the
            // reason (the summary ends "(insider data unavailable)"). Shown as a status hint line.
            string? insiderNa = null;
            if (a?.Details is JsonElement sd && sd.ValueKind == JsonValueKind.Object &&
                sd.TryGetProperty("insider_unavailable", out var iu) && iu.ValueKind == JsonValueKind.String &&
                !string.IsNullOrWhiteSpace(iu.GetString()))
                insiderNa = iu.GetString()!.Trim();
            SentInsiderHint.Text = insiderNa == null ? "" : "Insider data unavailable: " + insiderNa;
            SentInsiderHint.Visibility = insiderNa == null ? Visibility.Collapsed : Visibility.Visible;
            AutomationProperties.SetName(SentInsiderHint, "Status: " + SentInsiderHint.Text);

            var source = (a?.Source ?? "").ToLowerInvariant();
            var isAi = source == "llm" || source == "cached";
            var summaryText = SentSummary.Text;
            if (isAi)
            {
                FundAi.TryRead(a!.Provenance, null, null, out var model, out var at);
                SentBadge.Text = AiContent.Badge(model, at) + (source == "cached" ? " · CACHED" : "");
                SentBadge.Foreground = FundPalette.Ai;
                SentBadge.Visibility = Visibility.Visible;
                SentCaveat.Text = AiContent.Caveat;
                SentCaveat.Visibility = Visibility.Visible;
                SentCard.Background = FundPalette.AiSurface;
                SentCard.BorderBrush = FundPalette.Ai;
                AutomationProperties.SetName(SentSummary, AiContent.AutomationName(model, at) + " Sentiment: " + summaryText);
            }
            else if (source == "unavailable")
            {
                // An error is never dressed up as analysis (labelling rule 7). D9 (2026-10-02): shown as a
                // plain status — no ✦ glyph, no caveat, no violet container.
                SentBadge.Text = "Sentiment unavailable — status, not an AI result";
                SentBadge.Foreground = FundPalette.Risk;
                SentBadge.Visibility = Visibility.Visible;
                SentCaveat.Visibility = Visibility.Collapsed;
                SentCard.Background = FundPalette.Panel;
                SentCard.BorderBrush = FundPalette.PanelBorder;
                AutomationProperties.SetName(SentSummary, "Status: sentiment unavailable. " + summaryText);
            }
            else
            {
                SentBadge.Visibility = Visibility.Collapsed;
                SentCaveat.Visibility = Visibility.Collapsed;
                SentCard.Background = FundPalette.Panel;
                SentCard.BorderBrush = FundPalette.PanelBorder;
                AutomationProperties.SetName(SentSummary, "Sentiment: " + summaryText);
            }
            AutomationProperties.SetName(SentBadge, isAi ? "AI-generated sentiment label: " + SentBadge.Text : SentBadge.Text);
            ApplySentimentLoading();
        }

        /// <summary>
        /// While a cycle that may call the sentiment model is running, the card is labelled like the
        /// verdict (AiContent.LoadingBadge + "AI-generated sentiment, loading"), so the first exposure
        /// already carries the AI label (EU AI Act Art. 50(5)). The previous text stays dimmed.
        /// </summary>
        private void ApplySentimentLoading()
        {
            var weight = SentimentWeight();
            if (!IsAiLoading() || (weight.HasValue && weight.Value < 0.005))
            {
                SentSummary.Opacity = 1;
                return;
            }
            SentBadge.Text = AiContent.LoadingBadge;
            SentBadge.Foreground = FundPalette.Ai;
            SentBadge.Visibility = Visibility.Visible;
            SentCard.Background = FundPalette.AiSurface;
            SentCard.BorderBrush = FundPalette.Ai;
            SentSummary.Opacity = 0.55;
            var loading = AiContent.LoadingAutomationName("sentiment");
            AutomationProperties.SetName(SentBadge, loading);
            AutomationProperties.SetName(SentSummary, loading + ". Previous: " + SentSummary.Text);
        }

        private double? SentimentWeight()
        {
            if (_params?.Params is JsonElement p && p.ValueKind == JsonValueKind.Object &&
                p.TryGetProperty("agent_weights", out var w) && TryNumber(w, "sentiment", out var v)) return v;
            return null;
        }

        private void UpdateRiskCard(FundAgentSignal? a)
        {
            if (a == null)
            {
                RiskStance.Text = "—";
                RiskStance.Foreground = FundPalette.Neutral;
                RiskStripe.Background = FundPalette.Focus;
                RiskBar.RenderTransform = new ScaleTransform(0, 1);
                RiskSummary.Text = _state == null ? "Waiting for the engine…" : "No risk assessment yet.";
                if (_summaryForeground.TryGetValue(RiskSummary, out var riskForeground)) RiskSummary.Foreground = riskForeground;
                RiskConf.Text = "size —";
                RiskMeta.Text = "rule-based";
                RiskCard.ToolTip = null;
                return;
            }

            double multiplier = 1;
            bool veto = false, vetoShort = false;
            if (a.Details is JsonElement d && d.ValueKind == JsonValueKind.Object)
            {
                if (TryNumber(d, "size_multiplier", out var m)) multiplier = Math.Clamp(m, 0, 1);
                if (TryBool(d, "veto", out var v)) veto = v;
                if (TryBool(d, "veto_short", out var vs)) vetoShort = vs;
            }
            string stanceText;
            Brush brush;
            if (veto) { stanceText = "VETO"; brush = FundPalette.Short; }
            else if (vetoShort) { stanceText = "NO SHORTS"; brush = FundPalette.Risk; }
            else if (multiplier < 0.999) { stanceText = "×" + multiplier.ToString("0.00", Inv); brush = FundPalette.Risk; }
            else { stanceText = "OK"; brush = FundPalette.Focus; }

            RiskStance.Text = stanceText;
            RiskStance.Foreground = brush;
            RiskStripe.Background = brush;
            RiskBar.RenderTransform = new ScaleTransform(veto ? 0 : multiplier, 1);
            RiskSummary.Text = string.IsNullOrWhiteSpace(a.Summary) ? "(no summary)" : a.Summary;
            RiskConf.Text = "size ×" + (veto ? 0 : multiplier).ToString("0.00", Inv);
            RiskMeta.Text = SourceLabel(a.Source);
            RiskCard.ToolTip = DetailsTooltip("Risk Manager", a);
            // Backend 2026-10-02: source "unavailable" (e.g. calendar data could not be loaded) → the risk
            // card is incomplete and the engine places no orders for the symbol. Amber status, no size.
            if (!_summaryForeground.ContainsKey(RiskSummary)) _summaryForeground[RiskSummary] = RiskSummary.Foreground;
            var riskUnavailable = string.Equals(a.Source, "unavailable", StringComparison.OrdinalIgnoreCase);
            RiskSummary.Foreground = riskUnavailable ? FundPalette.Risk : _summaryForeground[RiskSummary];
            if (riskUnavailable)
            {
                string? why = null;
                if (a.Details is JsonElement rd && rd.ValueKind == JsonValueKind.Object &&
                    rd.TryGetProperty("unavailable", out var ru) && ru.ValueKind == JsonValueKind.String)
                    why = ru.GetString();
                RiskStance.Text = "UNAVAILABLE";
                RiskStance.Foreground = FundPalette.Risk;
                RiskStripe.Background = FundPalette.Risk;
                RiskBar.RenderTransform = new ScaleTransform(0, 1);
                RiskSummary.Text = string.IsNullOrWhiteSpace(a.Summary)
                    ? "Risk card " + (string.IsNullOrWhiteSpace(why) ? "unavailable" : why!.Trim()) + " — no orders for this symbol."
                    : a.Summary!;
                RiskConf.Text = "no orders";
            }
            AutomationProperties.SetName(RiskSummary, "Risk manager (rule-based)" + (riskUnavailable ? ", status: " : ": ") + RiskSummary.Text);
        }

        private static ToolTip DetailsTooltip(string title, FundAgentSignal a)
        {
            var lines = new List<string> { title + " — details" };
            if (a.Details is JsonElement d && d.ValueKind == JsonValueKind.Object)
            {
                foreach (var p in d.EnumerateObject()) lines.Add(p.Name + ": " + FundFormat.Plain(p.Value));
            }
            lines.Add("source: " + (a.Source ?? "—"));
            if (FundAi.TryRead(a.Provenance, null, null, out var model, out var at))
            {
                lines.Add("provenance: AI-generated · " + (AiContent.ShortModelName(model) is { Length: > 0 } sm ? sm : "unknown model") +
                          (at.HasValue ? " · " + at.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm", Inv) : ""));
            }
            return new ToolTip
            {
                Content = new TextBlock { Text = string.Join("\n", lines), TextWrapping = TextWrapping.Wrap, MaxWidth = 380, FontFamily = FundUi.Mono, FontSize = 11 },
            };
        }

        private void UpdateVerdict(FundSymbolState? sym)
        {
            var rec = sym?.Recommendation;
            VerdictSymbolText.Text = sym?.Symbol ?? _selectedSymbol ?? "—";
            var q = sym?.Quote;
            VerdictQuoteText.Text = q?.Price is double price
                ? price.ToString("N2", Inv) + (q.ChangePct is double ch ? "  " + FundFormat.Pct(ch, 2, signed: true) : "") + (q.Delayed ? " · delayed" : "")
                : "";
            VerdictStatusText.Foreground = FundPalette.Faint;

            if (rec == null)
            {
                VerdictActionText.Text = "—";
                VerdictActionText.Foreground = FundPalette.Neutral;
                VerdictConfText.Text = "—";
                VerdictArc.Data = Geometry.Empty;
                VerdictSizeText.Text = "";
                VerdictRuleText.Text = "";
                var judgeNa = string.IsNullOrWhiteSpace(sym?.JudgeUnavailable) ? null : sym!.JudgeUnavailable!.Trim();
                var dataNa = string.IsNullOrWhiteSpace(sym?.DataUnavailable) ? null : DataUnavailableText(sym!.DataUnavailable!);
                VerdictRationaleText.Text = _state == null ? "Waiting for the engine…"
                    : judgeNa != null || dataNa != null ? "No verdict for this symbol in the last run."
                    : "No verdict yet — the judge runs with the next engine cycle.";
                VerdictRationaleText.Opacity = 1;
                SetVerdictBadge(judgeNa != null || dataNa != null ? "Verdict unavailable" : null, isAi: false);
                SetVerdictNotice(judgeNa ?? dataNa);
                VerdictCaveatText.Text = "";
                VerdictCostText.Visibility = Visibility.Collapsed;
                VerdictStatusText.Text = "";
                AutomationProperties.SetName(VerdictRationaleText, VerdictRationaleText.Text);
                ApplyVerdictLoading();
                UpdateExecuteButton();
                return;
            }

            var action = (rec.Action ?? "FLAT").ToUpperInvariant();
            var confidence = Math.Clamp(rec.Confidence ?? 0, 0, 1);
            VerdictActionText.Text = action;
            VerdictActionText.Foreground = FundPalette.ForAction(action);
            VerdictConfText.Text = rec.Confidence.HasValue ? (confidence * 100).ToString("0", Inv) + "%" : "—";
            VerdictArc.Data = ArcGeometry(confidence);
            VerdictSizeText.Text = "Target " + FundFormat.Fraction(rec.TargetWeight, 1, signed: true) + " of equity · now " +
                                   FundFormat.Fraction(rec.CurrentWeight, 1, signed: true);
            VerdictRuleText.Text = "rule score " + (rec.RuleScore ?? 0).ToString("+0.00;-0.00;0.00", Inv) + " · " +
                                   FundFormat.LocalTime(rec.Ts) + (rec.RunId is long id ? " · run #" + id.ToString(Inv) : "");

            var source = (rec.Source ?? "").ToLowerInvariant();
            var carried = source == "carried";
            var isAi = IsAiRecommendation(rec, out var model, out var at);
            var rationale = string.IsNullOrWhiteSpace(rec.Rationale) ? "(no rationale given)" : rec.Rationale!;
            VerdictRationaleText.Text = rationale;

            if (isAi)
            {
                SetVerdictBadge(AiContent.Badge(model, at) + (carried
                    ? " · CARRIED" + (string.IsNullOrWhiteSpace(rec.JudgedAt) ? "" : " · JUDGED " + FundFormat.LocalTime(rec.JudgedAt, "HH:mm"))
                    : ""), isAi: true);
                VerdictCaveatText.Text = AiContent.Caveat;
                AutomationProperties.SetName(VerdictRationaleText, AiContent.AutomationName(model, at) + " Verdict rationale: " + rationale);
                var cost = rec.Usage?.EffectiveCost;
                if (carried)
                {
                    VerdictCostText.Text = "Carried" + (string.IsNullOrWhiteSpace(rec.JudgedAt) ? "" : " · judged " + FundFormat.LocalTime(rec.JudgedAt, "HH:mm")) +
                                           " — reused from an earlier judgement, no new AI cost.";
                    VerdictCostText.Visibility = Visibility.Visible;
                }
                else if (cost.HasValue)
                {
                    // usage.cost_usd is this symbol's share of ONE batch call for all symbols
                    // (fund/llm.py split_usage); batch_cost_usd is the whole call when sent.
                    var usage = rec.Usage!;
                    var batch = usage.BatchCostUsd;
                    var estimated = usage.CostEstimated == true ? " (estimated)" : "";
                    var batchNote = batch.HasValue
                        ? $" (share of a {FundFormat.Usd(batch)}{estimated} batch call" +
                          (usage.BatchSize is int n && n > 1 ? " for " + n.ToString(Inv) + " symbols)" : ")")
                        : " (one batch call for all symbols)" + estimated;
                    VerdictCostText.Text = $"Cost: {FundFormat.Usd(cost)} · {(usage.PromptTokens ?? 0).ToString(Inv)} in / {(usage.CompletionTokens ?? 0).ToString(Inv)} out tokens" +
                                           batchNote;
                    VerdictCostText.Visibility = Visibility.Visible;
                }
                else
                {
                    VerdictCostText.Visibility = Visibility.Collapsed;
                }
            }
            else
            {
                if (source == "rules")
                {
                    // Old row of the former rule-based judge (removed 2026-10-02, D9).
                    SetVerdictBadge("Rule-based · no AI · older verdict", isAi: false);
                    VerdictCaveatText.Text = "Rule-based verdict from an earlier engine version (no AI) — information for your own decision.";
                    AutomationProperties.SetName(VerdictRationaleText, "Rule-based verdict, older engine version: " + rationale);
                }
                else
                {
                    SetVerdictBadge("Verdict · source " + (string.IsNullOrWhiteSpace(rec.Source) ? "unknown" : rec.Source), isAi: false);
                    VerdictCaveatText.Text = "Rule-based verdict (no AI) — information for your own decision.";
                    AutomationProperties.SetName(VerdictRationaleText, "Verdict rationale: " + rationale);
                }
                VerdictCostText.Visibility = Visibility.Collapsed;
            }

            // D9: the last cycle made no new verdict → this one is older and says so (keeps its own time).
            var notUpdated = string.IsNullOrWhiteSpace(rec.NotUpdated) ? null
                : NotUpdatedText(rec.NotUpdated!) + " — showing the verdict from " + FundFormat.LocalTime(rec.Ts, "MM-dd HH:mm") +
                  (rec.RunId is long oldRun ? " (run #" + oldRun.ToString(Inv) + ")" : "") + ".";
            var symJudgeNa = string.IsNullOrWhiteSpace(sym?.JudgeUnavailable) ? null : sym!.JudgeUnavailable!.Trim();
            var symDataNa = string.IsNullOrWhiteSpace(sym?.DataUnavailable) ? null : DataUnavailableText(sym!.DataUnavailable!);
            SetVerdictNotice(notUpdated ?? symJudgeNa ?? symDataNa);

            var status = (rec.Status ?? "open").ToLowerInvariant();
            VerdictStatusText.Text = status switch
            {
                "executed" => "Executed" + (rec.OrderId is long oid ? " · order #" + oid.ToString(Inv) : ""),
                "skipped" => "Not traded" + (string.IsNullOrWhiteSpace(rec.SkipReason) ? "" : " — " + rec.SkipReason),
                "superseded" => "Superseded by a newer verdict",
                _ => _state?.Autopilot == true ? "Open · the autopilot acts on it within its limits" : "Open · not executed (autopilot off)",
            };
            if (status == "skipped") VerdictStatusText.Foreground = FundPalette.Risk;
            ApplyVerdictLoading();
            UpdateExecuteButton();
        }

        private void SetVerdictBadge(string? text, bool isAi)
        {
            VerdictBadgeText.Text = text ?? "No verdict yet";
            VerdictBadgeText.Foreground = isAi ? FundPalette.Ai : FundPalette.Faint;
            VerdictBadgeBorder.Background = isAi ? FundPalette.AiSurface : Brushes.Transparent;
            VerdictBadgeBorder.BorderBrush = isAi ? FundPalette.Ai : FundPalette.Hairline;
            VerdictRationaleBorder.Background = isAi ? FundPalette.AiSurface : Brushes.Transparent;
            VerdictRationaleBorder.BorderBrush = isAi ? FundPalette.Ai : FundPalette.Hairline;
            AutomationProperties.SetName(VerdictBadgeText, isAi ? "AI-generated verdict label: " + VerdictBadgeText.Text : VerdictBadgeText.Text);
        }

        /// <summary>While a run that may call the AI judge is in progress, the badge says so (AiContent.LoadingBadge).</summary>
        private void ApplyVerdictLoading()
        {
            if (!IsAiLoading())
            {
                VerdictRationaleText.Opacity = 1;
                return;
            }
            VerdictBadgeText.Text = AiContent.LoadingBadge;
            VerdictBadgeText.Foreground = FundPalette.Ai;
            VerdictBadgeBorder.Background = FundPalette.AiSurface;
            VerdictBadgeBorder.BorderBrush = FundPalette.Ai;
            VerdictRationaleText.Opacity = 0.55;
            AutomationProperties.SetName(VerdictBadgeText, AiContent.LoadingAutomationName("verdict"));
        }

        private void UpdateExecuteButton()
        {
            if (!_uiReady) return;
            // Offered whenever the autopilot is off (not tied to the Manual tab) for an "open" or
            // "skipped" recommendation — the backend accepts only those, and only the symbol's
            // latest one (409 "superseded…" otherwise, handled in ExecuteButton_Click).
            var rec = CurrentSymbolState()?.Recommendation;
            var status = (rec?.Status ?? "open").ToLowerInvariant();
            var show = rec != null && _connected && _state != null && !_state.Autopilot &&
                       (status == "open" || status == "skipped") &&
                       _state.Halted?.Active != true;
            ExecuteButton.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        }

        /// <summary>Ring arc from 12 o'clock, clockwise, for a confidence 0..1 (centre 88,88, radius 74 — the 148 px ellipse).</summary>
        private static Geometry ArcGeometry(double fraction)
        {
            fraction = Math.Clamp(fraction, 0, 0.9999);
            if (fraction < 0.005) return Geometry.Empty;
            const double cx = 88, cy = 88, r = 74;
            var angle = fraction * 2 * Math.PI;
            var end = new Point(cx + r * Math.Sin(angle), cy - r * Math.Cos(angle));
            var figure = new PathFigure { StartPoint = new Point(cx, cy - r), IsClosed = false, IsFilled = false };
            figure.Segments.Add(new ArcSegment(end, new Size(r, r), 0, angle > Math.PI, SweepDirection.Clockwise, true));
            var geometry = new PathGeometry();
            geometry.Figures.Add(figure);
            geometry.Freeze();
            return geometry;
        }

        // ---------- portfolio ----------

        private void UpdatePortfolio(FundState s)
        {
            var a = s.Account;
            // D9 (2026-10-02): valuation paused → figures that depend on current prices are "—" with the
            // reason (tooltip + amber banner), never the last known (stale) numbers.
            var paused = string.IsNullOrWhiteSpace(a?.ValuationPaused) ? null : a!.ValuationPaused!.Trim();
            ValuationBannerText.Text = paused == null ? "" : char.ToUpperInvariant(paused[0]) + paused[1..] +
                " — equity, P&L, drawdown and margin figures are not current and are not shown.";
            ValuationBanner.Visibility = paused == null ? Visibility.Collapsed : Visibility.Visible;
            AutomationProperties.SetName(ValuationBannerText, ValuationBannerText.Text);
            if (paused != null)
            {
                a = new FundAccount
                {
                    Cash = a?.Cash,
                    DrawdownStopPct = a?.DrawdownStopPct,
                    FundingTotal = a?.FundingTotal,
                    StartingCapital = a?.StartingCapital,
                    ResetAt = a?.ResetAt,
                    Unreadable = a?.Unreadable,
                    Error = a?.Error,
                    ValuationPaused = paused,
                };
            }
            object? pausedTip = paused == null ? null : "Not shown: " + paused;
            foreach (var t in new[] { EquityValueText, DayPnlText, TotalPnlText, BuyingPowerText, DrawdownText, MarginExcessText, ShortExposureText, MaintenanceText, CryptoMarginExcessText })
                t.ToolTip = pausedTip;
            EquityValueText.Text = FundFormat.Money(a?.Equity);
            CashText.Text = FundFormat.Money(a?.Cash);
            BuyingPowerText.Text = FundFormat.Money(a?.BuyingPower);
            DayPnlText.Text = FundFormat.Money(a?.DayPnl, signed: true);
            DayPnlText.Foreground = FundPalette.ForSigned(a?.DayPnl);
            TotalPnlText.Text = FundFormat.Money(a?.TotalPnl, signed: true) +
                                (a?.TotalPnlPct is double tp ? " (" + FundFormat.Pct(tp, 2, signed: true) + ")" : "");
            TotalPnlText.Foreground = FundPalette.ForSigned(a?.TotalPnl);

            var accountUnreadable = a?.Unreadable == true || a?.DrawdownPct == null;
            var dd = Math.Max(0, a?.DrawdownPct ?? 0);
            var limit = a?.DrawdownStopPct ?? ParamNumber("drawdown_stop_pct") ?? 0;
            var ratio = limit > 0 ? Math.Clamp(dd / limit, 0, 1) : 0;
            DrawdownBar.RenderTransform = new ScaleTransform(ratio, 1);
            DrawdownBar.Background = ratio >= 0.75 ? FundPalette.Short : FundPalette.Risk;
            DrawdownText.Text = paused != null ? "— from peak (valuation paused)" : accountUnreadable ? "— from peak" : FundFormat.Pct(dd, 2) + " from peak";
            DrawdownLimitText.Text = limit > 0 ? "Stop at " + FundFormat.Pct(limit, 1) : "Stop —";

            MarginExcessText.Text = FundFormat.Money(a?.MarginExcess);
            MarginExcessText.Foreground = a?.MarginExcess is double me && me < 0 ? FundPalette.Short : FundPalette.Text;
            var cap = ParamNumber("short_exposure_max_pct");
            ShortExposureText.Text = FundFormat.Pct(a?.ShortExposurePct, 1) + (cap.HasValue ? " / " + FundFormat.Pct(cap, 0) : "");
            MaintenanceText.Text = FundFormat.Money(a?.MaintenanceRequired);

            // Crypto perps (optional fields): rows stay hidden while the backend does not send them.
            // funding_total is always sent (0.0): show it only with crypto positions or booked funding;
            // the crypto margin excess only while perp shorts are open.
            var openPositions = (s.Positions ?? new List<FundPosition>()).Where(x => x.Qty is not double q0 || Math.Abs(q0) > 1e-9).ToList();
            var anyCrypto = openPositions.Any(x => x.IsCrypto || x.IsPerp);
            var anyPerpShort = openPositions.Any(x => x.IsPerp && x.IsShort);
            FundingRow.Visibility = a?.FundingTotal is double ft && (anyCrypto || Math.Abs(ft) >= 0.005) ? Visibility.Visible : Visibility.Collapsed;
            FundingTotalText.Text = FundFormat.Funding(a?.FundingTotal);
            FundingTotalText.Foreground = FundPalette.ForSigned(a?.FundingTotal);
            AutomationProperties.SetName(FundingTotalText, "Crypto funding " + FundingTotalText.Text);
            CryptoMarginRow.Visibility = a?.CryptoMarginExcess.HasValue == true && anyPerpShort ? Visibility.Visible : Visibility.Collapsed;
            CryptoMarginExcessText.Text = FundFormat.Money(a?.CryptoMarginExcess);
            CryptoMarginExcessText.Foreground = a?.CryptoMarginExcess is double cme && cme < 0 ? FundPalette.Short : FundPalette.Text;

            // Positions
            PositionsPanel.Children.Clear();
            // A position with a null qty is shown (qty "—"), not hidden.
            var positions = (s.Positions ?? new List<FundPosition>()).Where(p => p.Qty is not double q || Math.Abs(q) > 1e-9).OrderBy(p => p.Symbol).ToList();
            PositionsEmptyText.Visibility = positions.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            var longs = positions.Count(p => !p.IsShort);
            PositionsCountText.Text = positions.Count == 0 ? "" : $"{longs.ToString(Inv)} long · {(positions.Count - longs).ToString(Inv)} short";
            var first = true;
            foreach (var p in positions)
            {
                PositionsPanel.Children.Add(BuildPositionRow(p, first));
                first = false;
            }

            var pending = s.PendingOrders ?? new List<FundOrder>();
            if (pending.Count > 0)
            {
                PendingOrdersText.Text = $"{pending.Count.ToString(Inv)} order(s) pending — fill at the next open: " +
                                         string.Join(", ", pending.Take(5).Select(o => $"{(o.Side ?? "?").ToUpperInvariant()} {FundFormat.Qty(o.Qty)} {o.Symbol}")) +
                                         (pending.Count > 5 ? ", …" : "");
                PendingOrdersText.Visibility = Visibility.Visible;
            }
            else
            {
                PendingOrdersText.Visibility = Visibility.Collapsed;
            }
        }

        private UIElement BuildPositionRow(FundPosition p, bool first)
        {
            var isShort = p.IsShort;
            var sideBrush = isShort ? FundPalette.Short : FundPalette.Long;
            // D9: no current quote → these are null; "—" + tooltip, never a stale number.
            var unvalued = p.LastPrice == null || p.MarketValue == null || p.UnrealizedPnl == null || p.Weight == null;

            var line1 = new Grid();
            line1.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            line1.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var left = new StackPanel { Orientation = Orientation.Horizontal };
            left.Children.Add(FundUi.Text(p.Symbol, 12, FundPalette.Text, bold: true, mono: true, wrap: false));
            // Instrument label (2026-09-26, optional): crypto shorts are perpetual futures ("PERP SHORT").
            var sideLabel = (p.IsPerp ? "PERP " : "") + (isShort ? "SHORT" : "LONG");
            var side = FundUi.Text(sideLabel, 10, sideBrush, bold: true, mono: true, wrap: false);
            side.Margin = new Thickness(7, 1, 0, 0);
            left.Children.Add(side);
            var qty = FundUi.Text(FundFormat.Qty(p.Qty.HasValue ? (double?)Math.Abs(p.Qty.Value) : null), 11, FundPalette.Dim, mono: true, wrap: false);
            qty.Margin = new Thickness(7, 1, 0, 0);
            left.Children.Add(qty);
            if (p.IsCrypto)
            {
                var unit = FundUi.Text(FundAssets.BaseAsset(p.Symbol), 10, FundPalette.Faint, mono: true, wrap: false);
                unit.Margin = new Thickness(4, 1, 0, 0);
                left.Children.Add(unit);
                var tag = FundUi.Text("24/7", 9, FundPalette.Focus, mono: true, wrap: false);
                tag.Margin = new Thickness(7, 2, 0, 0);
                left.Children.Add(tag);
            }
            line1.Children.Add(left);
            var pnl = FundUi.Text(FundFormat.Money(p.UnrealizedPnl, signed: true), 12, FundPalette.ForSigned(p.UnrealizedPnl), mono: true, wrap: false);
            if (p.UnrealizedPnl == null) pnl.ToolTip = "Not valued right now — no current quote";
            Grid.SetColumn(pnl, 1);
            line1.Children.Add(pnl);

            var details = "avg " + FundFormat.Num(p.AvgPrice, 2) + " · last " + FundFormat.Num(p.LastPrice, 2) +
                          " · " + FundFormat.Fraction(p.Weight.HasValue ? Math.Abs(p.Weight.Value) : null, 1);
            if (isShort && (p.IsPerp || p.IsCrypto))
            {
                // Perp short: funding every 8 h instead of a borrow fee (positive rate = our short receives).
                var rate = ParamNumber("crypto_funding_rate_pct_8h");
                details += " · funding every 8 h" + (rate.HasValue ? " (" + FundingRateLabel(rate.Value) + ")" : "");
            }
            else if (isShort) details += " · borrow " + (p.BorrowRatePct.HasValue ? FundFormat.Pct(p.BorrowRatePct, 2) + "/yr" : "—");
            if (p.RealizedPnl is double rp && Math.Abs(rp) >= 0.005) details += " · realized " + FundFormat.Money(rp, signed: true);
            var line2 = FundUi.Text(details, 10.5, FundPalette.Faint, mono: true);
            line2.Margin = new Thickness(0, 2, 0, 0);

            var stack = new StackPanel();
            stack.Children.Add(line1);
            stack.Children.Add(line2);
            var row = new Border
            {
                Child = stack,
                Padding = new Thickness(2, 6, 2, 6),
                Background = Brushes.Transparent,
                BorderBrush = FundPalette.Hairline,
                BorderThickness = first ? new Thickness(0) : new Thickness(0, 1, 0, 0),
                Cursor = Cursors.Hand,
                ToolTip = "Opened " + FundFormat.LocalTime(p.OpenedAt, "yyyy-MM-dd HH:mm") + " · market value " +
                          FundFormat.Money(p.MarketValue) +
                          (unvalued ? "\nNot valued right now (last price, market value, P&L and weight shown as —)" +
                                      (string.IsNullOrWhiteSpace(_state?.Account?.ValuationPaused) ? "" : ": " + _state!.Account!.ValuationPaused!.Trim()) : "") +
                          (p.IsPerp ? "\nPerpetual future (simulated): funding every 8 h, own margin, liquidated below the maintenance margin (liquidation fee)"
                           : p.IsCrypto ? "\nCrypto spot · trades 24/7" : "") +
                          "\nClick to show its signals",
            };
            var unitName = p.IsCrypto ? FundAssets.BaseAsset(p.Symbol) : "shares";
            AutomationProperties.SetName(row, $"{p.Symbol} {(p.IsPerp ? "perpetual future " : "")}{(isShort ? "short" : "long")} {FundFormat.Qty(p.Qty.HasValue ? (double?)Math.Abs(p.Qty.Value) : null)} {unitName}, unrealized P&L {FundFormat.Money(p.UnrealizedPnl, signed: true)}");
            var symbol = p.Symbol;
            row.MouseLeftButtonUp += (_, _) => SelectSymbol(symbol);
            return row;
        }

        // ---------- autopilot ----------

        private void UpdateAutopilot(FundState s)
        {
            var halted = s.Halted?.Active == true;
            AutoEngageToggle.IsChecked = s.Autopilot; // Click (user) handler only — this doesn't trigger it
            AutoEngageToggle.IsEnabled = _connected && !halted;
            if (halted)
            {
                AutoStatusText.Text = "Halted — resume the engine before switching the autopilot on.";
                AutoStatusDot.Fill = FundPalette.Kill;
            }
            else if (s.Autopilot)
            {
                var minConf = ParamNumber("min_confidence");
                AutoStatusText.Text = "On · acts on verdicts" + (minConf.HasValue ? " ≥ " + minConf.Value.ToString("0", Inv) + "%" : "") +
                                      " within the limits below · simulated paper orders only" + AutopilotCountdownText(s);
                AutoStatusDot.Fill = FundPalette.Focus;
            }
            else if (RecentAutopilotOffEvent() is FundEvent off)
            {
                AutoStatusText.Text = "Off · switched off by the engine at " + FundFormat.LocalTime(off.Ts, "ddd HH:mm") +
                                      " — " + AutopilotEventText(off) + " Switch it on again if wanted.";
                AutoStatusDot.Fill = FundPalette.Risk;
            }
            else
            {
                AutoStatusText.Text = "Off · verdicts are shown, no orders are placed automatically";
                AutoStatusDot.Fill = FundPalette.Faint;
            }
            if (!_modeTabInitialized)
            {
                _modeTabInitialized = true;
                if (s.Autopilot) ModeAutoTab.IsChecked = true;
            }
        }

        /// <summary>The engine's dead-man switch fired (event kind "autopilot", FundNotifications) — show why.</summary>
        private void OnAutopilotSwitchedOff(FundEvent e)
        {
            _autopilotOffEvent = e;
            if (_state != null)
            {
                _state.Autopilot = false;
                UpdateAutopilot(_state);
                UpdateExecuteButton();
            }
            UpdateStatusLine();
            _ = RefreshStateAsync();
        }

        /// <summary>The last "autopilot" event, if it is from the last 24 h and the autopilot is off now.</summary>
        private FundEvent? RecentAutopilotOffEvent()
        {
            var e = _autopilotOffEvent;
            if (e == null || _state?.Autopilot == true) return null;
            var ts = FundFormat.ParseTs(e.Ts);
            if (ts.HasValue && DateTimeOffset.UtcNow - ts.Value > TimeSpan.FromHours(24)) return null;
            return e;
        }

        private string AutopilotEventText(FundEvent e)
        {
            if (!string.IsNullOrWhiteSpace(e.Body)) return e.Body!.Trim().TrimEnd('.') + ".";
            if (!string.IsNullOrWhiteSpace(e.Title)) return e.Title!.Trim().TrimEnd('.') + ".";
            var minutes = ParamNumber("autopilot_heartbeat_minutes");
            return "no contact from UFOS.ai" + (minutes.HasValue ? " for " + MinutesLabel(minutes.Value) : "") + " (heartbeat).";
        }

        private async void AutoEngageToggle_Click(object sender, RoutedEventArgs e)
        {
            var wanted = AutoEngageToggle.IsChecked == true;
            AutoEngageToggle.IsChecked = _state?.Autopilot == true; // show the real state until the engine confirms
            if (wanted)
            {
                var limits = $"Minimum confidence {ConfidenceValueText.Text} · max position {MaxSizeValueText.Text} · " +
                             $"max {MaxTradesValueText.Text} trades/day · drawdown stop {DrawdownStopValueText.Text}.";
                if (!FundDialogs.Confirm(this, "Switch the autopilot on?",
                        "The engine will place simulated paper orders on its own at every run, following the judge's verdicts " +
                        "(partly AI-generated) within these limits:\n" + limits +
                        "\n\nThe kill switch stays available at all times. The autopilot is switched off at every backend start" +
                        (_heartbeatSupported == true ? " and after " + MinutesLabel(HeartbeatSlider.Value) + " without contact from UFOS.ai (heartbeat)" : "") + ".",
                        "Switch on", footnote: "Paper trading · simulated money"))
                    return;
            }
            AutoEngageToggle.IsEnabled = false;
            try
            {
                var result = await FundClient.SetAutopilotAsync(wanted);
                if (result.Ok && result.Value != null)
                {
                    AutoEngageToggle.IsChecked = result.Value.Autopilot;
                    if (_state != null) _state.Autopilot = result.Value.Autopilot;
                    if (result.Value.Autopilot) _autopilotOffEvent = null;
                }
                else
                {
                    AutoStatusText.Text = result.Status == FundCallStatus.Conflict
                        ? "Refused: the engine is halted. Resume it first."
                        : "Autopilot not changed: " + result.Describe();
                    AutoStatusDot.Fill = FundPalette.Short;
                }
            }
            finally
            {
                AutoEngageToggle.IsEnabled = _connected && _state?.Halted?.Active != true;
            }
            await RefreshStateAsync();
        }

        // ---------- engine settings (PUT /fund/params, debounced) ----------

        private async Task LoadParamsAsync()
        {
            if (_paramsLoading) return;
            _paramsLoading = true;
            try
            {
                var result = await FundClient.GetParamsAsync();
                if (!result.Ok || result.Value == null)
                {
                    if (result.Status != FundCallStatus.Offline) _paramsError = "Settings not loaded: " + result.Describe();
                    UpdateParamsSaveText();
                    return;
                }
                _params = result.Value;
                _paramsVersion = result.Value.Version;
                if (_paramsError != null && _paramsError.StartsWith("Settings not loaded", StringComparison.Ordinal)) _paramsError = null;
                if (_pendingParams.Count == 0 && !_paramsSaving) ApplyParamsToUi(result.Value.Params, result.Value.Limits);
                ParamsVersionText.Text = _paramsVersion.HasValue ? "v" + _paramsVersion.Value.ToString(Inv) : "";
                UpdateParamsSaveText();
            }
            finally
            {
                _paramsLoading = false;
            }
        }

        private void ApplyParamsToUi(JsonElement? parameters, JsonElement? limits)
        {
            if (parameters is not JsonElement p || p.ValueKind != JsonValueKind.Object) return;
            _applyingParams = true;
            try
            {
                ApplyLimits(limits);
                foreach (var (slider, info) in _sliderKeys)
                {
                    if (TryNumber(p, info.Key, out var value)) slider.Value = Math.Clamp(value, slider.Minimum, slider.Maximum);
                }
                if (p.TryGetProperty("agent_weights", out var w) && w.ValueKind == JsonValueKind.Object)
                {
                    if (TryNumber(w, "technical", out var wt)) WeightTechSlider.Value = Math.Clamp(wt, 0, 1);
                    if (TryNumber(w, "macro", out var wm)) WeightMacroSlider.Value = Math.Clamp(wm, 0, 1);
                    if (TryNumber(w, "sentiment", out var ws)) WeightSentSlider.Value = Math.Clamp(ws, 0, 1);
                }
                // Dead-man switch: only offered when the running backend knows the setting.
                _heartbeatSupported = p.TryGetProperty("autopilot_heartbeat_minutes", out _) ||
                                      (limits is JsonElement lim && lim.ValueKind == JsonValueKind.Object && lim.TryGetProperty("autopilot_heartbeat_minutes", out _));
                HeartbeatSlider.IsEnabled = _heartbeatSupported == true;
                // Crypto (24/7): each control only when the running backend knows its key.
                var anyCrypto = false;
                foreach (var (control, key) in CryptoControls())
                {
                    var known = HasParamKey(p, limits, key);
                    control.IsEnabled = known;
                    anyCrypto |= known;
                }
                _cryptoSupported = anyCrypto;
                CryptoSupportHintText.Visibility = anyCrypto ? Visibility.Collapsed : Visibility.Visible;
                if (TryBool(p, "crypto_risk_watch", out var riskWatch)) CryptoRiskWatchToggle.IsChecked = riskWatch;
                ApplyClosedIntervalLabel();
                if (TryBool(p, "risk_enabled", out var risk)) RiskEnabledToggle.IsChecked = risk;
                if (TryBool(p, "allow_shorts", out var shorts)) AllowShortsToggle.IsChecked = shorts;
                if (TryString(p, "judge_model_mode", out var mode))
                {
                    JudgeStandardRadio.IsChecked = mode == "standard";
                    JudgePremiumRadio.IsChecked = mode == "premium";
                    JudgeFlipRadio.IsChecked = mode == "premium_on_flip";
                }
                if (!AiBudgetInput.IsKeyboardFocusWithin && TryNumber(p, "ai_daily_budget_usd", out var budget))
                {
                    AiBudgetInput.Text = budget.ToString("0.00", Inv);
                    SetBudgetHint(null);
                }
                if (!WatchlistInput.IsKeyboardFocusWithin && p.TryGetProperty("watchlist", out var wl) && wl.ValueKind == JsonValueKind.Array)
                {
                    WatchlistInput.Text = string.Join(", ", wl.EnumerateArray().Where(x => x.ValueKind == JsonValueKind.String).Select(x => x.GetString()));
                    SetWatchlistHint(null);
                }
            }
            finally
            {
                _applyingParams = false;
            }
            UpdateSliderLabels();
        }

        /// <summary>The crypto (24/7) settings controls and their params keys.</summary>
        private IEnumerable<(Control Control, string Key)> CryptoControls()
        {
            yield return (CryptoFeesSlider, "crypto_fees_bps");
            yield return (CryptoMaxPosSlider, "crypto_max_position_pct");
            yield return (CryptoInitialMarginSlider, "crypto_initial_margin_pct");
            yield return (CryptoMaintMarginSlider, "crypto_maintenance_margin_pct");
            yield return (CryptoFundingSlider, "crypto_funding_rate_pct_8h");
            yield return (CryptoLiqFeeSlider, "crypto_liquidation_fee_pct");
            yield return (CryptoRiskWatchToggle, "crypto_risk_watch");
        }

        private static bool HasParamKey(JsonElement parameters, JsonElement? limits, string key)
            => (parameters.ValueKind == JsonValueKind.Object && parameters.TryGetProperty(key, out _)) ||
               (limits is JsonElement lim && lim.ValueKind == JsonValueKind.Object && lim.TryGetProperty(key, out _));

        /// <summary>
        /// closed_interval_minutes is the crypto risk-watch interval on a crypto-aware backend; an older
        /// backend still uses it as the run interval outside the regular session.
        /// </summary>
        private void ApplyClosedIntervalLabel()
        {
            var riskWatch = _cryptoSupported != false;
            ClosedIntervalLabelText.Text = riskWatch ? "Risk watch interval (outside US hours)" : "Run interval (market closed)";
            ClosedIntervalPanel.ToolTip = riskWatch
                ? "Outside US hours there is no automatic trading (only ▶ Run now). While crypto positions are open, a light risk watch " +
                  "(no agents, no AI, no new orders) checks marks, funding, margin/liquidation and drawdown at this interval."
                : "Minutes between runs outside the regular session. Orders placed then are queued until the next open.";
            AutomationProperties.SetName(ClosedIntervalSlider, ClosedIntervalLabelText.Text);
        }

        /// <summary>"+0.010% / 8 h" (signed, % per 8 hours).</summary>
        private static string FundingRateLabel(double pct)
            => pct.ToString("+0.000;" + FundFormat.Minus + "0.000;0.000", Inv) + "% / 8 h";

        /// <summary>Slider ranges/steps from GET /fund/params "limits" ({key: {min, max, step}}), when present.</summary>
        private void ApplyLimits(JsonElement? limits)
        {
            if (limits is not JsonElement l || l.ValueKind != JsonValueKind.Object) return;
            var intChanges = new List<(Slider Slider, string Key, bool IsInt)>();
            foreach (var (slider, info) in _sliderKeys)
            {
                if (!l.TryGetProperty(info.Key, out var lim) || lim.ValueKind != JsonValueKind.Object) continue;
                if (TryString(lim, "type", out var type) && (type == "int" || type == "float") && (type == "int") != info.IsInt)
                    intChanges.Add((slider, info.Key, type == "int"));
                if (TryNumber(lim, "min", out var min) && TryNumber(lim, "max", out var max) && max > min)
                {
                    slider.Minimum = min;
                    slider.Maximum = max;
                }
                if (TryNumber(lim, "step", out var step) && step > 0) SetSliderStep(slider, step);
            }
            foreach (var (slider, key, isInt) in intChanges) _sliderKeys[slider] = (key, isInt);
            if (l.TryGetProperty("ai_daily_budget_usd", out var b) && b.ValueKind == JsonValueKind.Object && TryNumber(b, "max", out var bmax) && bmax > 0)
                _budgetMax = bmax;
        }

        private static void SetSliderStep(Slider slider, double step)
        {
            slider.TickFrequency = step;
            slider.SmallChange = step;
            slider.LargeChange = step * 5;
            slider.IsSnapToTickEnabled = true;
        }

        private void ParamSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
        {
            if (!_uiReady) return;
            UpdateSliderLabels();
            if (_applyingParams) return;
            if (ReferenceEquals(sender, WeightTechSlider) || ReferenceEquals(sender, WeightMacroSlider) || ReferenceEquals(sender, WeightSentSlider))
            {
                QueueParam("agent_weights", new Dictionary<string, object?>
                {
                    ["technical"] = Math.Round(WeightTechSlider.Value, 2),
                    ["macro"] = Math.Round(WeightMacroSlider.Value, 2),
                    ["sentiment"] = Math.Round(WeightSentSlider.Value, 2),
                });
                return;
            }
            // Crypto perps: maintenance margin <= initial margin (the engine rejects anything else).
            if (ReferenceEquals(sender, CryptoMaintMarginSlider) && CryptoMaintMarginSlider.Value > CryptoInitialMarginSlider.Value + 1e-9)
            {
                CryptoMaintMarginSlider.Value = CryptoInitialMarginSlider.Value; // re-enters this handler and queues the clamped value
                return;
            }
            if (ReferenceEquals(sender, CryptoInitialMarginSlider) && CryptoMaintMarginSlider.IsEnabled &&
                CryptoMaintMarginSlider.Value > CryptoInitialMarginSlider.Value + 1e-9)
            {
                CryptoMaintMarginSlider.Value = CryptoInitialMarginSlider.Value; // queued via its own ValueChanged
            }
            if (sender is Slider slider && _sliderKeys.TryGetValue(slider, out var info))
            {
                // 4 decimals: the funding rate moves in 0.005 % steps.
                QueueParam(info.Key, info.IsInt ? (object)(int)Math.Round(slider.Value) : Math.Round(slider.Value, 4));
            }
        }

        private void JudgeMode_Checked(object sender, RoutedEventArgs e)
        {
            if (!_uiReady || _applyingParams) return;
            string? mode = null;
            if (ReferenceEquals(sender, JudgeStandardRadio)) mode = "standard";
            else if (ReferenceEquals(sender, JudgePremiumRadio)) mode = "premium";
            else if (ReferenceEquals(sender, JudgeFlipRadio)) mode = "premium_on_flip";
            if (mode != null) QueueParam("judge_model_mode", mode);
        }

        private void ParamToggle_Click(object sender, RoutedEventArgs e)
        {
            if (!_uiReady || _applyingParams) return;
            if (ReferenceEquals(sender, RiskEnabledToggle)) QueueParam("risk_enabled", RiskEnabledToggle.IsChecked == true);
            else if (ReferenceEquals(sender, AllowShortsToggle)) QueueParam("allow_shorts", AllowShortsToggle.IsChecked == true);
            else if (ReferenceEquals(sender, CryptoRiskWatchToggle))
            {
                QueueParam("crypto_risk_watch", CryptoRiskWatchToggle.IsChecked == true);
                UpdateSliderLabels();
            }
        }

        private void AiBudgetInput_TextChanged(object sender, TextChangedEventArgs e)
        {
            if (!_uiReady || _applyingParams) return;
            var text = (AiBudgetInput.Text ?? "").Trim().Replace(',', '.');
            if (double.TryParse(text, NumberStyles.Float, Inv, out var value) && value >= 0 && value <= _budgetMax)
            {
                SetBudgetHint(null);
                QueueParam("ai_daily_budget_usd", Math.Round(value, 2));
            }
            else
            {
                SetBudgetHint("Enter 0–" + _budgetMax.ToString("0.##", Inv));
            }
        }

        private void SetBudgetHint(string? error)
        {
            AiBudgetHintText.Text = error ?? "0 = no AI calls, no new verdicts";
            AiBudgetHintText.Foreground = error == null ? FundPalette.Faint : FundPalette.Short;
        }

        private void WatchlistInput_KeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key != Key.Enter) return;
            e.Handled = true;
            WatchlistApply_Click(sender, e);
        }

        private async void WatchlistApply_Click(object sender, RoutedEventArgs e)
        {
            var raw = (WatchlistInput.Text ?? "").Split(new[] { ',', ';', ' ', '\t', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries);
            var symbols = new List<string>();
            var invalid = new List<string>();
            foreach (var item in raw)
            {
                var sym = item.Trim().ToUpperInvariant();
                if (!FundAssets.IsValidSymbol(sym)) invalid.Add(sym);
                else if (!symbols.Contains(sym)) symbols.Add(sym);
            }
            if (invalid.Count > 0) { SetWatchlistHint("Invalid symbol(s): " + string.Join(", ", invalid.Take(5)) + " — use tickers like SPY or crypto pairs like BTC-USD."); return; }
            if (symbols.Count < 1 || symbols.Count > 25) { SetWatchlistHint("Enter 1–25 symbols."); return; }
            SetWatchlistHint(null);
            QueueParam("watchlist", symbols);
            _paramsDebounce.Stop();
            await FlushParamsAsync();
        }

        private void SetWatchlistHint(string? error)
        {
            WatchlistHintText.Text = error ?? "1–25 symbols, comma separated (e.g. SPY, QQQ, BTC-USD). Crypto pairs end in -USD and trade 24/7. Press Enter or Apply.";
            WatchlistHintText.Foreground = error == null ? FundPalette.Faint : FundPalette.Short;
        }

        private void QueueParam(string key, object? value)
        {
            _pendingParams[key] = value;
            _paramsError = null;
            _paramsDebounce.Stop();
            _paramsDebounce.Start();
            UpdateParamsSaveText();
        }

        private async Task FlushParamsAsync()
        {
            if (_pendingParams.Count == 0) return;
            if (_paramsSaving)
            {
                _paramsDebounce.Start(); // retry once the running save is done
                return;
            }
            var body = new Dictionary<string, object?>(_pendingParams);
            _pendingParams.Clear();
            _paramsSaving = true;
            UpdateParamsSaveText();
            try
            {
                var result = await FundClient.PutParamsAsync(body);
                if (result.Ok && result.Value != null)
                {
                    _savedVersion = result.Value.Version;
                    if (result.Value.Version.HasValue) _paramsVersion = result.Value.Version;
                    _paramsEffectiveFrom = FundFormat.ParseTs(result.Value.EffectiveFrom) ?? FundFormat.ParseTs(_state?.Engine?.NextRunAt);
                    _paramsError = null;
                    if (_params != null && result.Value.Params.HasValue) _params.Params = result.Value.Params;
                    if (_pendingParams.Count == 0 && result.Value.Params.HasValue)
                        ApplyParamsToUi(result.Value.Params, _params?.Limits);
                    ParamsVersionText.Text = _paramsVersion.HasValue ? "v" + _paramsVersion.Value.ToString(Inv) : "";
                }
                else
                {
                    _savedVersion = null;
                    _paramsError = (result.Status == FundCallStatus.BadRequest ? "Not saved: " : "Not saved — ") + result.Describe();
                    // Show the engine's actual values again.
                    _paramsSaving = false;
                    if (result.Status != FundCallStatus.Offline && _pendingParams.Count == 0) await LoadParamsAsync();
                }
            }
            finally
            {
                _paramsSaving = false;
                UpdateParamsSaveText();
                if (_closed)
                {
                    // Window closed: flush what is still pending, then leave the timer stopped.
                    if (_pendingParams.Count == 0) _paramsDebounce.Stop();
                    else _paramsDebounce.Start();
                }
            }
        }

        private void UpdateParamsSaveText()
        {
            if (!_uiReady) return;
            string text;
            Brush brush = FundPalette.Faint;
            if (_paramsError != null)
            {
                text = _paramsError;
                brush = FundPalette.Short;
            }
            else if (_paramsSaving) text = "Saving…";
            else if (_pendingParams.Count > 0) text = "Unsaved changes…";
            else if (_savedVersion.HasValue)
            {
                var version = "Saved as v" + _savedVersion.Value.ToString(Inv);
                var eff = _paramsEffectiveFrom;
                var lastStart = FundFormat.ParseTs(_state?.Engine?.LastRun?.StartedAt);
                if (eff.HasValue && lastStart.HasValue && lastStart.Value >= eff.Value.AddSeconds(-2)) text = version + " · in effect since the run at " + lastStart.Value.ToLocalTime().ToString("HH:mm", Inv);
                else if (eff.HasValue && eff.Value > DateTimeOffset.UtcNow) text = version + " · applies from the next run (in " + FundFormat.Countdown(eff.Value - DateTimeOffset.UtcNow) + ")";
                else text = version + " · applies from the next run" +
                            (_state?.Engine?.IsManualOnly == true ? " (▶ Run now, or the scheduled runs from the US open)" : "");
                brush = FundPalette.Long;
            }
            else text = "Changes are saved automatically and apply from the next run.";
            ParamsSaveText.Text = text;
            ParamsSaveText.Foreground = brush;
        }

        private void UpdateSliderLabels()
        {
            if (!_uiReady) return;
            ConfidenceValueText.Text = ((int)Math.Round(ConfidenceSlider.Value)).ToString(Inv) + "%";
            MaxSizeValueText.Text = MaxSizeSlider.Value.ToString("0.#", Inv) + "% of equity";
            MaxTradesValueText.Text = ((int)Math.Round(MaxTradesSlider.Value)).ToString(Inv);
            DrawdownStopValueText.Text = DrawdownStopSlider.Value.ToString("0.#", Inv) + "%";
            IntervalValueText.Text = MinutesLabel(IntervalSlider.Value);
            ClosedIntervalValueText.Text = MinutesLabel(ClosedIntervalSlider.Value);
            WeightTechValueText.Text = WeightLabel(WeightTechSlider.Value);
            WeightMacroValueText.Text = WeightLabel(WeightMacroSlider.Value);
            WeightSentValueText.Text = WeightLabel(WeightSentSlider.Value);
            ShortCapValueText.Text = ShortCapSlider.Value.ToString("0", Inv) + "% of equity";
            FeesValueText.Text = FeesSlider.Value.ToString("0.0", Inv) + " bps";
            CryptoFeesValueText.Text = CryptoFeesSlider.Value.ToString("0.0", Inv) + " bps";
            CryptoMaxPosValueText.Text = CryptoMaxPosSlider.Value.ToString("0.#", Inv) + "% of equity";
            CryptoInitialMarginValueText.Text = CryptoInitialMarginSlider.Value.ToString("0", Inv) + "%";
            CryptoMaintMarginValueText.Text = CryptoMaintMarginSlider.Value.ToString("0", Inv) + "%";
            CryptoLiqFeeValueText.Text = CryptoLiqFeeSlider.Value.ToString("0.0#", Inv) + "%";
            var rate = Math.Round(CryptoFundingSlider.Value, 4);
            CryptoFundingValueText.Text = FundingRateLabel(rate);
            CryptoFundingHintText.Text = "Shorts are simulated as perpetual futures: funding every 8 h — positive rate means longs pay shorts. " +
                                         (rate > 0 ? "Now: our perp shorts receive funding."
                                          : rate < 0 ? "Now: our perp shorts pay funding."
                                          : "Now: no funding.");
            var watchEvery = MinutesLabel(ClosedIntervalSlider.Value);
            CryptoRiskWatchHintText.Text = CryptoRiskWatchToggle.IsChecked == true
                ? "Risk watch: outside US hours no automatic trading — only ▶ Run now; open crypto positions are still checked for margin/funding every " + watchEvery + "."
                : "Risk watch off: outside US hours no automatic trading — only ▶ Run now; open crypto positions are NOT checked between runs (margin, funding, liquidation).";
            UpdateHeartbeatHint();
        }

        private void UpdateHeartbeatHint()
        {
            if (!_uiReady) return;
            var label = MinutesLabel(HeartbeatSlider.Value);
            HeartbeatValueText.Text = _heartbeatSupported == false ? "—" : label;
            string text;
            if (_heartbeatSupported == false)
                text = "Not supported by the running backend (older version) — restart the Hedge Fund backend to update it.";
            else
            {
                text = "Autopilot switches itself off after " + label + " without the app.";
                if (_state?.LastClientSeenAt is DateTimeOffset seen)
                    text += " Engine last heard from UFOS.ai at " + seen.ToLocalTime().ToString("HH:mm:ss", Inv) + ".";
            }
            HeartbeatHintText.Text = text;
        }

        private static string MinutesLabel(double minutes)
        {
            var m = (int)Math.Round(minutes);
            if (m < 60) return m.ToString(Inv) + " min";
            return (m / 60).ToString(Inv) + " h" + (m % 60 == 0 ? "" : " " + (m % 60).ToString(Inv) + " min");
        }

        private static string WeightLabel(double value) => value < 0.005 ? "off" : value.ToString("0.00", Inv);

        private double? ParamNumber(string key)
            => _params?.Params is JsonElement p && p.ValueKind == JsonValueKind.Object && TryNumber(p, key, out var v) ? v : null;

        // =====================================================================================
        // Actions (every state-changing call is confirmed first; the backend audits it)
        // =====================================================================================

        private async void RunNowButton_Click(object sender, RoutedEventArgs e)
        {
            RunNowButton.IsEnabled = false;
            try
            {
                var result = await FundClient.RunNowAsync();
                if (result.Ok)
                {
                    _runRequested = true;
                    _runRequestedAt = DateTime.UtcNow;
                    _runRequestedFromId = _state?.Engine?.LastRun?.Id;
                    UpdateStatusLine();
                    UpdateAgentsAndVerdict();
                }
                else
                {
                    FundDialogs.Info(this, "Run now", "The run could not be queued. " + result.Describe());
                }
            }
            finally
            {
                RunNowButton.IsEnabled = _connected;
            }
            await Task.Delay(1500);
            await RefreshStateAsync();
        }

        private async void KillSwitchButton_Click(object sender, RoutedEventArgs e)
        {
            // S10 review C1 (2026-10-24): POST /v2/oms/kill (control.kill_all) runs the platform kill (all platform accounts, brokers
            // included) AND the old engine's kill; each half reports its own result — this one button covers both.
            var values = FundDialogs.Form(this, "Kill switch",
                (_councilReplaced
                    ? "This halts all paper trading immediately.\n\n" +
                      "Council of AIs:\n" +
                      "• the Council orders are switched off and its working orders are cancelled (paper-council's positions are closed with the platform accounts below).\n\n"
                    : "This halts BOTH engines immediately.\n\n" +
                      "Old engine (Overview):\n" +
                      "• the autopilot is switched off,\n" +
                      "• every simulated position is closed with market orders (queued until the next open while the market is closed),\n" +
                      "• no autopilot orders until you resume the engine.\n\n") +
                "Strategy Platform (Portfolio & Trading — every platform paper account):\n" +
                "• auto trading is switched off,\n" +
                "• all working orders are cancelled and every position is closed,\n" +
                "• on connected broker accounts (Alpaca paper, Binance Spot demo) every open order AT THE BROKER is cancelled (Binance demo: only the platform's UFOS_ orders) and the platform's positions there are closed,\n" +
                "• each account stays halted until you resume it in Portfolio & Trading.\n\n" +
                "Paper / demo trading only — simulated money, no real orders are sent anywhere.",
                new[] { ("Reason (optional, stored in the audit log)", "") },
                "■ Halt and close all", danger: true);
            if (values == null) return;

            KillSwitchButton.IsEnabled = false;
            try
            {
                var result = await PlatformClient.KillAllAsync(values.Length > 0 ? values[0] : null);
                if (result.Ok)
                {
                    var (title, text) = KillResultText(result.Value);
                    FundDialogs.Info(this, title, text);
                }
                else
                {
                    FundDialogs.Info(this, "Kill switch failed",
                        (result.TimedOut
                            ? "No answer in time — the kill may be running or done (it waits for the broker accounts to confirm flat). Check the Overview and "
                              + "Portfolio & Trading before pressing it again."
                            : "The backend did not confirm the halt: " + PlatformClient.DescribeFailure(result)) +
                        "\n\nNeither half is confirmed — check the Overview and Portfolio & Trading. While the backend is unreachable it cannot place any simulated " +
                        "orders either, and the autopilot and the platform's auto trading are always off after a backend start.");
                }
            }
            finally
            {
                KillSwitchButton.IsEnabled = true;
            }
            await RefreshStateAsync();
        }

        /// <summary>
        /// The two halves of a /fund/kill result (old engine + Strategy Platform, SPEC §17.1/§17.12), each with its own outcome.
        /// Title: "Both engines halted" only when both halves confirmed AND no closing order failed (state "error" / "rejected").
        /// </summary>
        private static (string Title, string Text) KillResultText(TradingKillAllResult? r)
        {
            var lines = new List<string>();
            var ok = true;
            var ordersFailed = 0;
            var notFlat = false;
            // S10 C1 / S10-C (§22.8): /v2/oms/kill reports the old engine's half as {ok, error, halted, orders} before the switch-over and as
            // "not applicable" after it; the Council half {applicable, ok, orders_enabled, cancelled_orders, error} after it.
            var legacy = r?.Legacy;
            var council = r?.Council;
            var switchedOver = council?.Applicable == true || legacy?.Applicable == false;
            if (switchedOver)
            {
                lines.Add("Old engine: not applicable — switched over to the Council.");
                if (council?.Applicable != true)
                {
                    ok = false;
                    lines.Add("Council: no result reported — check the Council view (its orders switch must be off).");
                }
                else if (council.Ok != true)
                {
                    ok = false;
                    lines.Add("Council: NOT confirmed — " + (string.IsNullOrWhiteSpace(council.Error) ? "the orders switch is still on" : council.Error!.Trim()));
                }
                else
                {
                    lines.Add("Council: orders switched off, " + (council.CancelledOrders ?? 0).ToString(Inv) + " working Council order(s) cancelled "
                              + "(paper-council's positions are closed with the platform accounts below).");
                }
            }
            else if (legacy == null)
            {
                ok = false;
                lines.Add("Old engine: no result reported — check the Overview.");
            }
            else if (!legacy.Ok)
            {
                ok = false;
                lines.Add("Old engine: the halt was NOT confirmed — " + (string.IsNullOrWhiteSpace(legacy.Error) ? "no reason given" : legacy.Error!.Trim()));
            }
            else
            {
                var oldOrders = legacy.Orders?.Count ?? 0;
                lines.Add(legacy.Halted == true
                    ? "Old engine: halted; " + (oldOrders == 0 ? "no positions to close." : oldOrders.ToString(Inv) + " closing order(s) placed (Overview → Orders).")
                    : "Old engine: the kill ran, but the halt is not reported as active — check the Overview.");
                if (legacy.Halted != true) ok = false;
            }
            var half = PlatformClient.ReadKillHalf(r?.Platform);
            if (half == null)
            {
                ok = false;
                lines.Add("Strategy Platform: no result reported — the running backend may be older than stage 7; check Portfolio & Trading.");
            }
            else
            {
                if (!half.Ok) ok = false;
                lines.Add(half.Ok ? "Strategy Platform: auto trading off, accounts halted." : "Strategy Platform: NOT fully confirmed — " + (half.Error ?? "see the accounts below"));
                var accounts = half.Accounts ?? new List<TradingKillAccount>();
                if (accounts.Count == 0 && half.Ok) lines.Add("• no platform paper account is open.");
                foreach (var a in accounts)
                {
                    var closing = a.Orders ?? new List<OmsOrder>();
                    var line = "• " + (a.AccountId ?? "?") + ": " + (a.Cancelled ?? 0).ToString(Inv) + " working order(s) cancelled, "
                               + (closing.Count == 0 ? "no positions to close" : closing.Count.ToString(Inv) + " closing order(s)");
                    if (!string.IsNullOrWhiteSpace(a.Error)) { ok = false; line += " — ERROR: " + a.Error!.Trim(); }
                    if (a.Broker != null)
                    {
                        // stage 8 (SPEC §18.6): the broker half of a broker account (cancel all at the broker, flatten from its positions)
                        line += " (" + (a.Broker.CancelledAtBroker ?? 0).ToString(Inv) + " of them at the broker)";
                        if (a.Broker.Ok == false) { ok = false; line += " — BROKER: " + (string.IsNullOrWhiteSpace(a.Broker.Error) ? "not confirmed" : a.Broker.Error!.Trim()); }
                        if (!string.IsNullOrWhiteSpace(a.Broker.Note)) line += " — " + a.Broker.Note!.Trim();
                        // §18.13 verified kill: never "halted" without flat == true
                        var flatText = BrokerText.KillFlatText(a.Broker.Flat, a.Broker.Pending, a.Broker.NotFlat, null);
                        if (flatText != null)
                        {
                            ok = false;
                            notFlat = true;
                            line += "\n   " + flatText.Replace("\n", "\n   ");
                        }
                        // §18.14: orders that are not the platform's (Binance: no UFOS_ prefix) — informational only (ok / notFlat unchanged)
                        if (BrokerText.UntouchedText(a.Broker.UntouchedOrders, a.Broker.UntouchedNote) is string untouched)
                            line += "\n   ℹ " + untouched;
                    }
                    var failed = closing.Count(o => o.State == "error" || o.State == "rejected");
                    if (failed > 0) line += " — " + failed.ToString(Inv) + " closing order(s) FAILED";
                    ordersFailed += failed;
                    lines.Add(line);
                    foreach (var o in closing.Take(8)) lines.Add("   " + TradingText.OrderLine(o));
                    if (closing.Count > 8) lines.Add("   …");
                }
            }
            if (ordersFailed > 0)
                lines.Add(ordersFailed.ToString(Inv) + " closing order(s) failed (state error / rejected, reasons above) — those positions are still open; "
                          + "check the Overview and Portfolio & Trading and close them manually.");
            var title = notFlat ? "Kill switch — a broker is NOT verified flat, check the results"
                : !ok ? "Kill switch — check the results"
                : ordersFailed > 0 ? "Halted — some closing orders failed"
                : switchedOver ? "All paper trading halted"
                : "Both engines halted";
            return (title, string.Join("\n\n", lines));
        }

        private async void ResumeButton_Click(object sender, RoutedEventArgs e)
        {
            // S10-C R3 (SPEC §22.8): one dialog — the halted platform accounts (paper-main, paper-council, broker accounts) and, only while
            // the old engine still runs and is halted, the old engine. One POST /v2/oms/resume per chosen account, /fund/resume for the old engine.
            ResumeButton.IsEnabled = false;
            try
            {
                await OpenResumeDialogAsync();
            }
            catch (Exception ex)
            {
                AppLog.Warn("HedgeFund", "Resume dialog failed.", ex);
                FundDialogs.Info(this, "Resume", "The resume dialog failed (" + ex.GetType().Name + "). Resume single accounts in Portfolio & Trading.");
            }
            finally
            {
                ResumeButton.IsEnabled = true;
            }
        }

        private async void ExecuteButton_Click(object sender, RoutedEventArgs e)
        {
            var sym = CurrentSymbolState();
            var rec = sym?.Recommendation;
            if (sym == null || rec == null) return;
            var isAi = IsAiRecommendation(rec, out _, out _);
            var message = $"Execute the {(isAi ? "AI-generated " : "rule-based ")}recommendation for {sym.Symbol}: {rec.Action}, " +
                          $"target {FundFormat.Fraction(rec.TargetWeight, 1, signed: true)} of equity (now {FundFormat.Fraction(rec.CurrentWeight, 1, signed: true)}).\n\n" +
                          "The paper broker places the simulated market order(s) needed to reach the target — queued until the next open while the market is closed. " +
                          "The minimum holding period is ignored for this manual acceptance.";
            if (!FundDialogs.Confirm(this, "Execute recommendation", message, "Execute", footnote: "Paper trading · simulated money"))
                return;

            ExecuteButton.IsEnabled = false;
            try
            {
                var result = await FundClient.ExecuteRecommendationAsync(rec.Id);
                if (result.Ok)
                {
                    var orders = result.Value ?? new List<FundOrder>();
                    FundDialogs.Info(this, "Recommendation executed", orders.Count == 0
                        ? "Nothing to trade — the position is already at the target."
                        : string.Join("\n", orders.Select(FundOrderText.Result)));
                }
                else if (result.Status == FundCallStatus.Conflict &&
                         result.Describe().Contains("supersed", StringComparison.OrdinalIgnoreCase))
                {
                    FundDialogs.Info(this, "Not executed — newer verdict",
                        $"This recommendation for {sym.Symbol} is no longer the latest one: a newer engine run has replaced it. " +
                        "Only the symbol's latest recommendation can be executed. The dashboard now shows the current verdict — review it and execute that one if wanted.");
                }
                else if (result.Status == FundCallStatus.Conflict)
                {
                    FundDialogs.Info(this, "Not executed", "The engine refused: " + result.Describe());
                }
                else
                {
                    FundDialogs.Info(this, "Not executed", result.Describe());
                }
            }
            finally
            {
                ExecuteButton.IsEnabled = true;
            }
            await RefreshStateAsync();
        }

        // ---------- manual orders ----------

        private async void ManualBuy_Click(object sender, RoutedEventArgs e) => await SubmitManualAsync("buy");
        private async void ManualSell_Click(object sender, RoutedEventArgs e) => await SubmitManualAsync("sell");
        private async void ManualShort_Click(object sender, RoutedEventArgs e) => await SubmitManualAsync("short");
        private async void ManualCover_Click(object sender, RoutedEventArgs e) => await SubmitManualAsync("cover");

        private async void ManualFlatten_Click(object sender, RoutedEventArgs e)
        {
            var ticker = (TickerInput.Text ?? "").Trim().ToUpperInvariant();
            var position = _state?.Positions?.FirstOrDefault(p => string.Equals(p.Symbol, ticker, StringComparison.OrdinalIgnoreCase) && p.Qty is double q && Math.Abs(q) > 1e-9);
            if (position == null)
            {
                ManualStatusText.Text = string.IsNullOrEmpty(ticker) ? "Enter a ticker." : "No open position in " + ticker + ".";
                return;
            }
            await PlaceOrderAsync(ticker, position.IsShort ? "cover" : "sell", Math.Abs(position.Qty ?? 0), null);
        }

        private async Task SubmitManualAsync(string side)
        {
            var ticker = (TickerInput.Text ?? "").Trim().ToUpperInvariant();
            if (!FundAssets.IsValidSymbol(ticker))
            {
                ManualStatusText.Text = "Enter a valid ticker (e.g. SPY) or crypto pair (e.g. BTC-USD).";
                return;
            }
            var isCrypto = IsCryptoSymbol(ticker);
            double? qty = null, notional = null;
            if (UnitDollarRadio.IsChecked == true)
            {
                // Notional ($) orders: unchanged — commas are thousands separators.
                var text = (SizeInput.Text ?? "").Trim().Replace(",", "").Replace("$", "");
                if (!double.TryParse(text, NumberStyles.Float, Inv, out var amount) || amount <= 0 || double.IsInfinity(amount) || double.IsNaN(amount))
                {
                    ManualStatusText.Text = "Enter a size greater than 0.";
                    return;
                }
                notional = Math.Round(amount, 2);
            }
            else
            {
                if (!TryParseQuantity(SizeInput.Text, isCrypto, out var amount) || amount <= 0)
                {
                    ManualStatusText.Text = isCrypto ? "Enter a quantity greater than 0 (e.g. 0.05 or 0,05)." : "Enter a quantity greater than 0.";
                    return;
                }
                if (isCrypto)
                {
                    qty = Math.Round(amount, 8);
                    if (qty <= 0)
                    {
                        ManualStatusText.Text = "Quantity too small — crypto quantities have at most 8 decimals (minimum 0.00000001).";
                        return;
                    }
                }
                else
                {
                    if (Math.Abs(amount - Math.Round(amount)) > 1e-9)
                    {
                        ManualStatusText.Text = "Stocks and ETFs trade in whole shares — enter a whole number. Fractional quantities are only for crypto pairs (e.g. BTC-USD).";
                        return;
                    }
                    qty = Math.Round(amount);
                    if (qty < 1)
                    {
                        ManualStatusText.Text = "Quantity in whole shares, at least 1.";
                        return;
                    }
                }
            }
            await PlaceOrderAsync(ticker, side, qty, notional);
        }

        /// <summary>
        /// Quantity input, culture-invariant. '.' is the decimal point; a ',' is accepted too:
        /// with a '.' present commas are thousands separators; otherwise a single comma is the
        /// decimal separator for crypto ("0,05"), and for stocks only "1,000"-style groups count as
        /// thousands (anything else becomes a decimal and is refused as a fractional share).
        /// </summary>
        private static bool TryParseQuantity(string? raw, bool commaIsDecimal, out double value)
        {
            value = 0;
            var t = (raw ?? "").Trim().Replace(" ", "").Replace("\u00A0", "").Replace("'", "");
            if (t.Length == 0 || t.Length > 40) return false;
            if (t.Contains(','))
            {
                if (t.Contains('.')) t = t.Replace(",", "");
                else
                {
                    var parts = t.Split(',');
                    var thousands = parts[0].Length is >= 1 and <= 3 && parts[0][0] != '0' &&
                                    parts.Skip(1).All(x => x.Length == 3 && x.All(char.IsDigit));
                    if (parts.Length == 2 && (commaIsDecimal || !thousands)) t = parts[0] + "." + parts[1];
                    else if (thousands) t = t.Replace(",", "");
                    else return false;
                }
            }
            return double.TryParse(t, NumberStyles.AllowDecimalPoint, Inv, out value) && !double.IsInfinity(value) && !double.IsNaN(value);
        }

        /// <summary>"shares" / "units" next to the size box, following the ticker.</summary>
        private void UpdateOrderUnitLabel()
        {
            if (!_uiReady || UnitSharesRadio == null || TickerInput == null) return;
            var crypto = IsCryptoSymbol((TickerInput.Text ?? "").Trim().ToUpperInvariant());
            var label = crypto ? "units" : "shares";
            if (!Equals(UnitSharesRadio.Content, label)) UnitSharesRadio.Content = label;
        }

        private void TickerInput_TextChanged(object sender, TextChangedEventArgs e) => UpdateOrderUnitLabel();

        private async Task PlaceOrderAsync(string ticker, string side, double? qty, double? notional)
        {
            var isCrypto = IsCryptoSymbol(ticker);
            var sizeText = qty.HasValue
                ? FundFormat.Qty(qty) + (isCrypto ? " " + FundAssets.BaseAsset(ticker) : " shares")
                : FundFormat.Money(notional) + (isCrypto ? " (fractional quantity at the current quote)" : " (whole shares at the current quote)");
            var sideText = side switch
            {
                "buy" => "BUY",
                "sell" => "SELL",
                "short" => "SELL SHORT",
                "cover" => "BUY TO COVER",
                _ => side.ToUpperInvariant(),
            };
            var symState = _state?.Symbols?.FirstOrDefault(x => string.Equals(x.Symbol, ticker, StringComparison.OrdinalIgnoreCase));
            var tradableNow = symState?.TradableNow ?? (_state?.Market?.Open == true);
            string market;
            if (isCrypto)
            {
                market = (symState?.TradableNow == false
                             ? "Crypto normally trades 24/7, but the engine reports this pair as not tradable right now — the order may be rejected or queued."
                             : "Crypto trades 24/7: the order fills now at the (delayed) quote plus slippage and fees.") +
                         (side == "short" ? "\n\nCrypto shorts are simulated as perpetual futures: own margin, funding every 8 h " +
                                            "(positive rate: longs pay shorts), liquidation with a fee below the maintenance margin." : "");
            }
            else
            {
                market = tradableNow
                    ? "The market is open: the order fills at the (delayed) quote plus slippage and fees."
                    : "The market is closed (or its state is unknown): the order is queued and fills at the next open.";
            }
            if (!FundDialogs.Confirm(this, "Confirm paper order", $"{sideText} {sizeText} of {ticker} — market order.\n\n{market}",
                    "Place paper order", footnote: "Paper trading · simulated money"))
                return;

            ManualStatusText.Text = "Placing order…";
            var result = await FundClient.SubmitOrderAsync(ticker, side, qty, notional);
            ManualStatusText.Text = result.Ok && result.Value != null
                ? "Last manual order: " + FundOrderText.Result(result.Value) + " · " + DateTime.Now.ToString("HH:mm", Inv)
                : "Order not placed: " + result.Describe();
            await RefreshStateAsync();
        }

        // ---------- history / details ----------

        private async Task LoadHistoryAsync()
        {
            if (_historyLoading) return;
            _historyLoading = true;
            try
            {
                var result = await FundClient.GetRunsAsync(30);
                if (!result.Ok || result.Value == null)
                {
                    HistoryStatusText.Text = result.Status == FundCallStatus.Offline ? "offline" : result.Describe();
                    return;
                }
                _historyLoadedOnce = true;
                BuildHistory(result.Value);
                HistoryStatusText.Text = result.Value.Count == 0 ? "" : $"latest {result.Value.Count.ToString(Inv)} · click a run for details";
            }
            finally
            {
                _historyLoading = false;
            }
        }

        private void BuildHistory(List<FundRunSummary> runs)
        {
            HistoryPanel.Children.Clear();
            HistoryEmptyText.Visibility = runs.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            var first = true;
            foreach (var run in runs)
            {
                var grid = new Grid();
                foreach (var width in new[] { 120.0, 70, 80, 110, 80 })
                    grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(width) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

                void Cell(int column, string text, Brush brush)
                {
                    var t = FundUi.Text(text, 11, brush, mono: true, wrap: false);
                    Grid.SetColumn(t, column);
                    grid.Children.Add(t);
                }

                // trigger "risk-watch" (2026-09-26): light check outside US hours — no agents, no AI, no new orders.
                var riskWatch = FundRunDetailsWindow.IsRiskWatch(run.Trigger);
                Cell(0, FundFormat.LocalTime(run.StartedAt, "MM-dd HH:mm:ss"), FundPalette.Text);
                Cell(1, run.Status ?? "?", FundRunDetailsWindow.StatusBrush(run.Status));
                Cell(2, riskWatch ? "risk watch" : run.Trigger ?? "", riskWatch ? FundPalette.Risk : FundPalette.Faint);
                var judgeMode = (run.JudgeMode ?? "").ToLowerInvariant();
                Cell(3, riskWatch ? "no AI" : judgeMode == "unavailable" ? "judge unavailable" : "judge " + (run.JudgeMode ?? "—"),
                     riskWatch || judgeMode == "none" ? FundPalette.Faint
                     : judgeMode == "unavailable" || judgeMode == "mixed" ? FundPalette.Risk : FundPalette.Dim);
                Cell(4, (run.CostUsd ?? 0) > 0 ? FundFormat.Usd(run.CostUsd) : "$0", FundPalette.Dim);
                var flips = run.Flips?.Count ?? 0;
                var summary = riskWatch
                    ? "risk watch · no AI · marks, funding, margin & drawdown" +
                      ((run.Orders ?? 0) > 0 ? $" · {(run.Orders ?? 0).ToString(Inv)} order{(run.Orders == 1 ? "" : "s")}" : "") +
                      (string.IsNullOrWhiteSpace(run.Error) ? "" : " · " + run.Error)
                    : $"{flips.ToString(Inv)} flip{(flips == 1 ? "" : "s")} · {(run.Orders ?? 0).ToString(Inv)} order{(run.Orders == 1 ? "" : "s")} · {(run.Recommendations ?? 0).ToString(Inv)} verdicts" +
                      (run.MarketOpen == false ? " · market closed" : "") +
                      (string.IsNullOrWhiteSpace(run.Error) ? "" : " · " + run.Error);
                Cell(5, summary, string.IsNullOrWhiteSpace(run.Error) ? FundPalette.Dim : FundPalette.Short);

                var row = new Border
                {
                    Child = grid,
                    Padding = new Thickness(8, 5, 8, 5),
                    Background = Brushes.Transparent,
                    BorderBrush = FundPalette.Hairline,
                    BorderThickness = first ? new Thickness(0) : new Thickness(0, 1, 0, 0),
                    Cursor = Cursors.Hand,
                    ToolTip = riskWatch
                        ? "Risk watch outside US hours: no agents, no AI, no new orders — marks crypto positions, books funding, " +
                          "checks margin/liquidation and the drawdown stop.\nClick for details"
                        : (flips > 0 ? "Flips: " + string.Join(", ", run.Flips!.Take(8).Select(FundFormat.Plain)) + "\n" : "") +
                          JudgeOrigin(run.JudgeMode) + "\nClick for details",
                };
                first = false;
                var id = run.Id;
                row.MouseEnter += (_, _) => row.Background = FundPalette.Hairline;
                row.MouseLeave += (_, _) => row.Background = Brushes.Transparent;
                row.MouseLeftButtonUp += async (_, _) => await OpenRunDetailsAsync(id);
                AutomationProperties.SetName(row, (riskWatch ? "Risk watch, no AI. " : JudgeAutomationPrefix(run.JudgeMode)) + $"Run {id.ToString(Inv)}, {run.Status}, {summary}");
                HistoryPanel.Children.Add(row);
            }
        }

        /// <summary>judge_mode llm|rules|mixed|none (fund/judge.py summarize) → who produced the run's verdicts.</summary>
        private static string JudgeOrigin(string? judgeMode) => (judgeMode ?? "").ToLowerInvariant() switch
        {
            "llm" => "Verdicts: AI-generated (language model judge) — " + AiContent.Caveat,
            "mixed" => "Verdicts: partly AI-generated (language model judge); for the other symbols the judge was unavailable — no verdict — " + AiContent.Caveat,
            "unavailable" => "Judge unavailable — no new verdicts in this run (see the run details for the reason)",
            "rules" => "Verdicts: rule-based (no AI, older engine version)",
            "none" => "No verdicts in this run",
            _ => "Verdicts: origin unknown",
        };

        private static string JudgeAutomationPrefix(string? judgeMode) => (judgeMode ?? "").ToLowerInvariant() switch
        {
            "llm" => "AI-generated verdicts. ",
            "mixed" => "AI-generated verdicts for some symbols, judge unavailable for others. ",
            "unavailable" => "Judge unavailable, no new verdicts. ",
            "rules" => "Rule-based verdicts, older engine version. ",
            _ => "",
        };

        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        private Task OpenRunDetailsAsync(long id) => DialogGuard.RunAsync("fund-run-details:" + id.ToString(Inv), () => OpenRunDetailsCoreAsync(id));

        private async Task OpenRunDetailsCoreAsync(long id)
        {
            HistoryStatusText.Text = "Loading run #" + id.ToString(Inv) + "…";
            var result = await FundClient.GetRunAsync(id);
            HistoryStatusText.Text = "";
            if (!result.Ok || result.Value == null)
            {
                FundDialogs.Info(this, "Run #" + id.ToString(Inv), "The run details could not be loaded. " + result.Describe());
                return;
            }
            try
            {
                new FundRunDetailsWindow(this, result.Value).Show();
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("HedgeFund", "Could not show the run details.", ex); } catch { }
            }
        }

        private async void RefreshHistory_Click(object sender, RoutedEventArgs e)
        {
            await LoadHistoryAsync();
            await LoadEquityAsync();
        }

        // ---------- equity curve ----------

        /// <summary>Full-screen bar text of the equity curve: equity, day and total P&amp;L as shown in the Overview.</summary>
        private string EquityFullScreenHeader()
            => "Equity " + EquityValueText.Text + " · day " + DayPnlText.Text + " · total " + TotalPnlText.Text + " · paper";

        private async Task LoadEquityAsync()
        {
            _lastEquityLoad = DateTime.UtcNow;
            var result = await FundClient.GetEquityAsync();
            if (!result.Ok || result.Value == null) return;
            var points = (result.Value.Points ?? new List<FundEquityPoint>())
                .Select(p => (Ts: FundFormat.ParseTs(p.Ts), Point: p))
                .Where(x => x.Ts.HasValue)
                .ToList();
            _equityChart.SetData(
                points.Select(x => (x.Ts!.Value.UtcDateTime, x.Point.Equity)),
                points.Select(x => (x.Ts!.Value.UtcDateTime, x.Point.BenchmarkEquity)),
                result.Value.StartingCapital ?? _state?.Account?.StartingCapital);
            EquityChartNote.Text = "Blue: paper equity · grey dashed: SPY scaled to the starting capital" +
                                   (points.Count >= 2 ? " · since " + points[0].Ts!.Value.ToLocalTime().ToString("yyyy-MM-dd", Inv) : "");
        }

        // ---------- backtest / exports ----------

        // Strategy Platform stage 3 (SPEC §8.6): the Overview's Backtest button opens the new backtest
        // dialog for the library strategy "Council Rules (no AI)" (renamed from "Agentic Committee (rules)", 2026-10-06). The old rule-strategy backtest
        // (LegacyBacktest_Click → POST /fund/backtest, FundBacktestResultWindow) stays in the code but is
        // no longer wired to any control; it is removed in stage 10.
        private const string OverviewBacktestLibraryKey = "agentic_committee_rules";

        private async void Backtest_Click(object sender, RoutedEventArgs e)
        {
            BacktestButton.IsEnabled = false;
            try
            {
                var list = await PlatformClient.GetStrategiesAsync();
                if (!list.Ok)
                {
                    FundDialogs.Info(this, "Backtest", list.Message);
                    return;
                }
                var strategy = list.Value?.Strategies?.FirstOrDefault(s =>
                    string.Equals(s.LibraryKey, OverviewBacktestLibraryKey, StringComparison.Ordinal) ||
                    string.Equals(s.Library?.Key, OverviewBacktestLibraryKey, StringComparison.Ordinal));
                if (strategy == null)
                {
                    var seed = list.Value?.LibraryStatus?.Message;
                    FundDialogs.Info(this, "Backtest",
                        "The library strategy \"Council Rules (no AI)\" (library key " + OverviewBacktestLibraryKey + ") was not found in the strategy list."
                        + (string.IsNullOrWhiteSpace(seed) ? "" : "\n\nAlpha library status: " + seed));
                    return;
                }
                string? benchmark = null;
                if (strategy.CurrentVersion is int version)
                {
                    var ver = await PlatformClient.GetStrategyVersionAsync(strategy.Id, version);
                    if (!ver.Ok)
                    {
                        FundDialogs.Info(this, "Backtest", ver.Message);
                        return;
                    }
                    if (ver.Value?.Spec is JsonElement spec && spec.ValueKind == JsonValueKind.Object)
                        benchmark = StrategyJson.Str(spec, "benchmark");
                }
                var name = string.IsNullOrWhiteSpace(strategy.Name) ? "Council Rules (no AI)" : strategy.Name!;
                await BacktestLauncher.RunAsync(this, strategy.Id, name, strategy.CurrentVersion, benchmark);
            }
            finally
            {
                BacktestButton.IsEnabled = true;
            }
        }

        // Unused since stage 3 (see above) — kept unchanged until stage 10.
        // dlgguard (2026-10-23): the button is disabled while the form + backtest run; queued clicks return at once
        private async void LegacyBacktest_Click(object sender, RoutedEventArgs e)
            => await DialogGuard.RunAsync("fund-legacy-backtest", () => LegacyBacktestCoreAsync(sender, e), sender, "Running…");

        private async Task LegacyBacktestCoreAsync(object sender, RoutedEventArgs e)
        {
            var defaults = string.Join(", ", WatchlistFromParams());
            var values = FundDialogs.Form(this, "Backtest rule strategy",
                "Backtest rule strategy on daily bars: technical + macro agents with risk sizing, fills at the next open, fees and borrow cost included, no dividends. " +
                "No AI is used (a language model could know the outcome).",
                new[] { ("Symbols (comma separated, max 10, empty = watchlist)", defaults), ("Years (1–20)", "5") },
                "Run backtest", footnote: "Hypothetical, simulated results — they do not indicate future results.");
            if (values == null) return;

            var symbols = new List<string>();
            foreach (var item in (values.Length > 0 ? values[0] : "").Split(new[] { ',', ';', ' ' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var sym = item.Trim().ToUpperInvariant();
                if (!FundAssets.IsValidSymbol(sym))
                {
                    FundDialogs.Info(this, "Backtest", "Invalid symbol: " + sym);
                    return;
                }
                if (!symbols.Contains(sym)) symbols.Add(sym);
            }
            // Engine limits (backend 2026-09-26): ≤ 10 symbols, ≤ 20 years — checked here first.
            if (symbols.Count > 10)
            {
                FundDialogs.Info(this, "Backtest", "At most 10 symbols per backtest (you entered " + symbols.Count.ToString(Inv) + ").");
                return;
            }
            if (symbols.Count == 0 && WatchlistFromParams().Count > 10)
            {
                FundDialogs.Info(this, "Backtest", "The watchlist has more than 10 symbols — enter up to 10 symbols for the backtest.");
                return;
            }
            if (!int.TryParse(values.Length > 1 ? values[1].Trim() : "5", NumberStyles.Integer, Inv, out var years) || years < 1 || years > 20)
            {
                FundDialogs.Info(this, "Backtest", "Years must be a whole number from 1 to 20.");
                return;
            }
            var end = DateTime.UtcNow.Date;
            var start = end.AddYears(-years);

            BacktestButton.IsEnabled = false;
            BacktestButton.Content = "Backtest running…";
            try
            {
                var result = await FundClient.RunBacktestAsync(symbols.Count == 0 ? null : symbols,
                    start.ToString("yyyy-MM-dd", Inv), end.ToString("yyyy-MM-dd", Inv));
                if (result.Ok && result.Value != null)
                    new FundBacktestResultWindow(this, result.Value).Show();
                else
                    FundDialogs.Info(this, "Backtest failed", result.Status switch
                    {
                        FundCallStatus.Conflict => "Not started: " + result.Describe(),              // e.g. "a backtest is already running — …"
                        FundCallStatus.BadRequest => "Rejected by the engine: " + result.Describe(),  // e.g. symbol / year limits
                        _ => result.Describe(),
                    });
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("HedgeFund", "Backtest display failed.", ex); } catch { }
            }
            finally
            {
                BacktestButton.IsEnabled = true;
                BacktestButton.Content = "Backtest…";
            }
        }

        private async void ExportOrders_Click(object sender, RoutedEventArgs e) => await ExportAsync("orders.csv", ExportOrdersButton);
        private async void ExportDecisions_Click(object sender, RoutedEventArgs e) => await ExportAsync("decisions.csv", ExportDecisionsButton);

        private async Task ExportAsync(string name, Button button)
        {
            var dialog = new Microsoft.Win32.SaveFileDialog
            {
                Title = "Export " + name,
                FileName = Path.GetFileNameWithoutExtension(name) + "-" + DateTime.Now.ToString("yyyyMMdd-HHmm", Inv) + ".csv",
                DefaultExt = ".csv",
                AddExtension = true,
                OverwritePrompt = true,
                Filter = "CSV files (*.csv)|*.csv|All files (*.*)|*.*",
            };
            if (dialog.ShowDialog(this) != true) return;

            button.IsEnabled = false;
            try
            {
                var result = await FundClient.DownloadCsvAsync(name);
                if (!result.Ok || result.Value == null)
                {
                    FundDialogs.Info(this, "Export failed", result.Describe());
                    return;
                }
                await File.WriteAllBytesAsync(dialog.FileName, result.Value);
                HistoryStatusText.Text = "Exported " + name + " → " + Path.GetFileName(dialog.FileName);
            }
            catch (Exception ex)
            {
                FundDialogs.Info(this, "Export failed", "The file could not be written (" + ex.GetType().Name + ").");
            }
            finally
            {
                button.IsEnabled = true;
            }
        }

        // ---------- this computer: notifications + backend lifetime ----------

        private void MuteToastsCheck_Click(object sender, RoutedEventArgs e)
        {
            FundSettings.MuteNotifications = MuteToastsCheck.IsChecked == true;
        }

        /// <summary>Stage 9b (SPEC §20.8): start the backend together with UFOS.ai (applies from the next app start).</summary>
        private void StartWithAppCheck_Click(object sender, RoutedEventArgs e)
        {
            FundSettings.StartWithApp = StartWithAppCheck.IsChecked == true;
            StartWithAppCheck.IsChecked = FundSettings.StartWithApp;   // shows what was actually stored
        }

        private void KeepRunningCheck_Click(object sender, RoutedEventArgs e)
        {
            var wanted = KeepRunningCheck.IsChecked == true;
            if (wanted)
            {
                var ok = FundDialogs.Confirm(this, "Keep the engine running after UFOS.ai closes?",
                    "Experimental. From the next backend start on, the Hedge Fund backend (paper-trading engine + Financial Newsroom) " +
                    "is no longer bound to UFOS.ai: it keeps running — and keeps trading on paper if the autopilot is on — after the app closes. " +
                    "Its output then goes to backend/data/logs/process.log instead of the Backend Log window. " +
                    "UFOS.ai reuses the running engine on its next start.\n\n" +
                    "To stop a detached engine, use \"Stop engine…\" below (or end its python.exe in Task Manager). Switching this option off only affects the next backend start. " +
                    "Safety net: the engine switches the autopilot off by itself after the heartbeat time without UFOS.ai.",
                    "Keep running", footnote: "The autopilot is switched off at every backend start.");
                if (!ok)
                {
                    KeepRunningCheck.IsChecked = false;
                    return;
                }
            }
            FundSettings.BackendLifetime = wanted ? FundSettings.LifetimeDetached : FundSettings.LifetimeApp;
            if (!wanted)
            {
                FundDialogs.Info(this, "Engine lifetime",
                    "From the next backend start on, the engine is bound to UFOS.ai again and stops when the app closes. " +
                    "A detached engine that is already running (if any) keeps running until you stop it with \"Stop engine…\" (or end its python.exe in Task Manager).");
            }
        }

        /// <summary>
        /// "Stop engine…": kill switch (POST /v2/oms/kill — both engines, S10 C1; reason "engine stopped by user") and then
        /// POST /shutdown (token-authenticated, loopback only) — the in-app way to end a detached
        /// engine. If the halt is not confirmed, the user decides whether to shut down anyway.
        /// </summary>
        private async void StopEngineButton_Click(object sender, RoutedEventArgs e)
        {
            if (_stoppingEngine) return;
            var detached = FundSettings.IsDetached;
            if (!FundDialogs.Confirm(this, "Stop the engine?",
                    "1. Kill switch: the autopilot is switched off and every simulated position is closed with market orders " +
                    "(queued until the next open while the market is closed).\n" +
                    "2. The Hedge Fund backend process is shut down" +
                    (detached ? " — including an engine that keeps running after UFOS.ai closes (detached mode)" : "") + ".\n\n" +
                    "The Financial Newsroom goes offline too. The backend starts again the next time the Hedge Fund window is opened; " +
                    "the halt stays active until you resume the engine.",
                    "■ Halt and stop engine", danger: true, footnote: "Paper trading · simulated money"))
                return;

            _stoppingEngine = true;
            StopEngineButton.IsEnabled = false;
            StopEngineHintText.Text = "Stopping… (kill switch)";
            try
            {
                var kill = await PlatformClient.KillAllAsync("engine stopped by user");
                if (!kill.Ok)
                {
                    if (!await NewsroomBackend.IsRunningAsync())
                    {
                        FundDialogs.Info(this, "Stop engine", "The Hedge Fund backend is not running — there is nothing to stop.");
                        return;
                    }
                    if (!FundDialogs.Confirm(this, "Kill switch failed",
                            "The engine did not confirm the halt: " + PlatformClient.DescribeFailure(kill) +
                            "\n\nShut the backend down anyway? Open simulated positions then stay as they are until the next start " +
                            "(the autopilot is always off after a backend start).",
                            "Shut down anyway", danger: true))
                        return;
                }

                StopEngineHintText.Text = "Stopping… (shutting the backend down)";
                var (ok, message) = await NewsroomBackend.ShutdownAsync();
                var killText = !kill.Ok ? "The halt was not confirmed. " : KillResultText(kill.Value).Text + "\n\n";
                FundDialogs.Info(this, ok ? "Engine stopped" : "Engine not stopped", killText + message);
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("HedgeFund", "Stop engine failed.", ex); } catch { }
                FundDialogs.Info(this, "Engine not stopped", "Stopping the engine failed (" + ex.GetType().Name + "). See the Backend Log.");
            }
            finally
            {
                _stoppingEngine = false;
                StopEngineButton.IsEnabled = true;
                StopEngineHintText.Text = "Halts trading (kill switch), then ends the backend process — also a detached one";
            }
            await RefreshStateAsync();
        }

        // ---------- Execution tabs ----------

        private void ModeManualTab_Checked(object sender, RoutedEventArgs e)
        {
            if (ManualPanel == null || AutoPanel == null) return;
            ManualPanel.Visibility = Visibility.Visible;
            AutoPanel.Visibility = Visibility.Collapsed;
            UpdateExecuteButton();
        }

        private void ModeAutoTab_Checked(object sender, RoutedEventArgs e)
        {
            if (ManualPanel == null || AutoPanel == null) return;
            ManualPanel.Visibility = Visibility.Collapsed;
            AutoPanel.Visibility = Visibility.Visible;
            UpdateExecuteButton();
        }

        // ---------- JSON helpers ----------

        private static bool TryNumber(JsonElement obj, string key, out double value)
        {
            value = 0;
            if (obj.ValueKind != JsonValueKind.Object || !obj.TryGetProperty(key, out var e)) return false;
            if (e.ValueKind == JsonValueKind.Number) return e.TryGetDouble(out value);
            if (e.ValueKind == JsonValueKind.String) return double.TryParse(e.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out value);
            return false;
        }

        private static bool TryBool(JsonElement obj, string key, out bool value)
        {
            value = false;
            if (obj.ValueKind != JsonValueKind.Object || !obj.TryGetProperty(key, out var e)) return false;
            if (e.ValueKind == JsonValueKind.True || e.ValueKind == JsonValueKind.False) { value = e.GetBoolean(); return true; }
            if (e.ValueKind == JsonValueKind.Number && e.TryGetDouble(out var d)) { value = Math.Abs(d) > 0.5; return true; }
            return false;
        }

        private static bool TryString(JsonElement obj, string key, out string value)
        {
            value = "";
            if (obj.ValueKind != JsonValueKind.Object || !obj.TryGetProperty(key, out var e) || e.ValueKind != JsonValueKind.String) return false;
            value = e.GetString() ?? "";
            return true;
        }
    }
}

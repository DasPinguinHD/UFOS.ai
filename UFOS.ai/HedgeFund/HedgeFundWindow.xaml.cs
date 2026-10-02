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
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund
{
    public partial class HedgeFundWindow : Window
    {
        // Live dashboard of the autonomous PAPER-TRADING loop (backend/fund, contract:
        // backend/fund/SPEC.md §11/§13). Everything here is simulated — the backend has no
        // live-trading code path at all. Proof of concept, not investment advice.
        //
        // - Polls GET /fund/state every 5 s while the window is visible; engine settings
        //   (GET/PUT /fund/params, debounced 600 ms), run history, the equity curve and the
        //   exports are loaded on demand. Every call goes through Fund/FundClient.cs (session
        //   token, never throws); offline / 401 / errors are shown in a banner, the last known
        //   state stays visible.
        // - AI labelling (CLAUDE.md): the judge's verdict and the sentiment agent carry the
        //   "✦ AI-GENERATED · model · time" badge, caveat, violet container, cost line and a
        //   screen-reader name starting with "AI-generated" whenever their provenance marks
        //   them as LLM output; rule output is shown as "Rule-based · no AI".
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

            _paramsDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(600) };
            _paramsDebounce.Tick += async (_, _) =>
            {
                _paramsDebounce.Stop();
                await FlushParamsAsync();
            };
            EquityChartHost.Child = _equityChart;

            // "▶ Run now" can call the AI judge / sentiment agent -> AI trigger tooltip.
            _runNowTooltip = "Runs one engine cycle now (agents + judge). " + AiContent.TriggerTooltip +
                             " Stays within the AI daily budget; without budget the judge is rule-based.";
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
            FundNotifications.AutopilotSwitchedOff += OnAutopilotSwitchedOff;
            _autopilotOffEvent = FundNotifications.LastAutopilotEvent;

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
            _newsroomWindow?.Close();
            _newsroomWindow = null;
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
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            if (WindowState == WindowState.Maximized)
            {
                var frame = SystemParameters.WindowResizeBorderThickness;
                const double paddedBorder = 4; // SM_CXPADDEDBORDER, not exposed by SystemParameters
                RootBorder.Margin = new Thickness(frame.Left + paddedBorder, frame.Top + paddedBorder,
                                                  frame.Right + paddedBorder, frame.Bottom + paddedBorder);
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
            try
            {
                var result = await FundClient.GetStateAsync();
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
                var tradeNote = isCrypto ? (closedNow ? "crypto · currently not tradable" : "crypto · trades 24/7, fractional quantities")
                    : closedNow ? "market closed — orders queue until the next open" : "";
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
                    : "rule-based verdict (no AI)";
                var conf = FundFormat.Fraction(rec.Confidence, 0);
                AutomationProperties.SetName(chip, (isAi
                    ? $"AI-generated verdict for {sym}: {(action.Length > 0 ? action : "none")}, confidence {conf}"
                    : $"{sym}, rule-based verdict {(action.Length > 0 ? action : "none")}, confidence {conf}") +
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
                      (budget.Exhausted ? " (budget reached → rule-based)" : ""));
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
                                       "(agents + judge) — crypto orders fill immediately, stock/ETF orders are queued for the next open. " +
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
            if (isSentiment) SetSentimentLabel(a);
            else AutomationProperties.SetName(summary, title + " agent (rule-based): " + summary.Text);
        }

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
                // An error is never dressed up as analysis (labelling rule 7).
                SentBadge.Text = AiContent.UnavailableBadge;
                SentBadge.Foreground = FundPalette.Faint;
                SentBadge.Visibility = Visibility.Visible;
                SentCaveat.Visibility = Visibility.Collapsed;
                SentCard.Background = FundPalette.Panel;
                SentCard.BorderBrush = FundPalette.PanelBorder;
                AutomationProperties.SetName(SentSummary, "AI sentiment unavailable: " + summaryText);
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
            AutomationProperties.SetName(RiskSummary, "Risk manager (rule-based): " + RiskSummary.Text);
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
                VerdictRationaleText.Text = _state == null ? "Waiting for the engine…" : "No verdict yet — the judge runs with the next engine cycle.";
                VerdictRationaleText.Opacity = 1;
                SetVerdictBadge(null, isAi: false);
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
                                   FundFormat.Fraction(rec.CurrentWeight ?? 0, 1, signed: true);
            VerdictRuleText.Text = "rule score " + (rec.RuleScore ?? 0).ToString("+0.00;-0.00;0.00", Inv) + " · " +
                                   FundFormat.LocalTime(rec.Ts) + (rec.RunId is long id ? " · run #" + id.ToString(Inv) : "");

            var source = (rec.Source ?? "").ToLowerInvariant();
            var carried = source == "carried";
            var isAi = IsAiRecommendation(rec, out var model, out var at);
            var rationale = string.IsNullOrWhiteSpace(rec.Rationale) ? "(no rationale given)" : rec.Rationale!;
            VerdictRationaleText.Text = rationale;

            if (isAi)
            {
                SetVerdictBadge(AiContent.Badge(model, at) + (carried ? " · CARRIED" : ""), isAi: true);
                VerdictCaveatText.Text = AiContent.Caveat;
                AutomationProperties.SetName(VerdictRationaleText, AiContent.AutomationName(model, at) + " Verdict rationale: " + rationale);
                var cost = rec.Usage?.EffectiveCost;
                if (carried)
                {
                    VerdictCostText.Text = "Carried from an earlier judgement — no new AI cost.";
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
                SetVerdictBadge("Rule-based · no AI", isAi: false);
                VerdictCaveatText.Text = "Rule-based verdict from the agents' weighted scores (no AI). Not investment advice.";
                VerdictCostText.Visibility = Visibility.Collapsed;
                AutomationProperties.SetName(VerdictRationaleText, "Rule-based verdict: " + rationale);
            }

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
            DrawdownText.Text = accountUnreadable ? "— from peak" : FundFormat.Pct(dd, 2) + " from peak";
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
                        "Switch on", footnote: "Paper trading · simulated · not investment advice"))
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
            AiBudgetHintText.Text = error ?? "0 = rule-based only";
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
            var values = FundDialogs.Form(this, "Kill switch",
                "This halts the engine immediately:\n" +
                "• the autopilot is switched off,\n" +
                "• every simulated position is closed with market orders (queued until the next open while the market is closed),\n" +
                "• no autopilot orders until you resume the engine.\n\n" +
                "Paper trading only — no real orders are sent anywhere.",
                new[] { ("Reason (optional, stored in the audit log)", "") },
                "■ Halt and close all", danger: true);
            if (values == null) return;

            KillSwitchButton.IsEnabled = false;
            try
            {
                var result = await FundClient.KillAsync(values.Length > 0 ? values[0] : null);
                if (result.Ok)
                {
                    var orders = result.Value?.Orders ?? new List<FundOrder>();
                    FundDialogs.Info(this, "Engine halted", orders.Count == 0
                        ? "The engine is halted. There were no positions to close."
                        : "The engine is halted. Closing orders:\n" + string.Join("\n", orders.Take(12).Select(FundOrderText.Result)) +
                          (orders.Count > 12 ? "\n…" : ""));
                }
                else
                {
                    FundDialogs.Info(this, "Kill switch failed",
                        "The engine did not confirm the halt: " + result.Describe() +
                        "\n\nWhile the backend is unreachable it cannot place any simulated orders either, and the autopilot is always off after a backend start.");
                }
            }
            finally
            {
                KillSwitchButton.IsEnabled = true;
            }
            await RefreshStateAsync();
        }

        private async void ResumeButton_Click(object sender, RoutedEventArgs e)
        {
            if (!FundDialogs.Confirm(this, "Resume the engine?",
                    "Clears the halt so runs may trade again. The autopilot stays off — switch it on in Execution → Autonomous if wanted.",
                    "Resume"))
                return;
            ResumeButton.IsEnabled = false;
            try
            {
                var result = await FundClient.ResumeAsync();
                if (!result.Ok) FundDialogs.Info(this, "Resume failed", result.Describe());
            }
            finally
            {
                ResumeButton.IsEnabled = true;
            }
            await RefreshStateAsync();
        }

        private async void ExecuteButton_Click(object sender, RoutedEventArgs e)
        {
            var sym = CurrentSymbolState();
            var rec = sym?.Recommendation;
            if (sym == null || rec == null) return;
            var isAi = IsAiRecommendation(rec, out _, out _);
            var message = $"Execute the {(isAi ? "AI-generated " : "rule-based ")}recommendation for {sym.Symbol}: {rec.Action}, " +
                          $"target {FundFormat.Fraction(rec.TargetWeight, 1, signed: true)} of equity (now {FundFormat.Fraction(rec.CurrentWeight ?? 0, 1, signed: true)}).\n\n" +
                          "The paper broker places the simulated market order(s) needed to reach the target — queued until the next open while the market is closed. " +
                          "The minimum holding period is ignored for this manual acceptance.";
            if (!FundDialogs.Confirm(this, "Execute recommendation", message, "Execute", footnote: "Paper trading · simulated · not investment advice"))
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
                    "Place paper order", footnote: "Paper trading · simulated · not investment advice"))
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
                Cell(3, riskWatch ? "no AI" : "judge " + (run.JudgeMode ?? "—"),
                     riskWatch || string.Equals(run.JudgeMode, "none", StringComparison.OrdinalIgnoreCase) ? FundPalette.Faint : FundPalette.Dim);
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
            "mixed" => "Verdicts: partly AI-generated (language model judge), partly rule-based — " + AiContent.Caveat,
            "rules" => "Verdicts: rule-based (no AI)",
            "none" => "No verdicts in this run",
            _ => "Verdicts: origin unknown",
        };

        private static string JudgeAutomationPrefix(string? judgeMode) => (judgeMode ?? "").ToLowerInvariant() switch
        {
            "llm" => "AI-generated verdicts. ",
            "mixed" => "AI-generated verdicts (partly rule-based). ",
            "rules" => "Rule-based verdicts. ",
            _ => "",
        };

        private async Task OpenRunDetailsAsync(long id)
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

        private async void Backtest_Click(object sender, RoutedEventArgs e)
        {
            var defaults = string.Join(", ", WatchlistFromParams());
            var values = FundDialogs.Form(this, "Backtest",
                "Rule-based backtest on daily bars: technical + macro agents with risk sizing, fills at the next open, fees and borrow cost included, no dividends. " +
                "No AI is used (a language model could know the outcome).",
                new[] { ("Symbols (comma separated, max 10, empty = watchlist)", defaults), ("Years (1–20)", "5") },
                "Run backtest", footnote: "Hypothetical, simulated results — not investment advice.");
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
        /// "Stop engine…": kill switch (POST /fund/kill, reason "engine stopped by user") and then
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
                    "■ Halt and stop engine", danger: true, footnote: "Paper trading · simulated · not investment advice"))
                return;

            _stoppingEngine = true;
            StopEngineButton.IsEnabled = false;
            StopEngineHintText.Text = "Stopping… (kill switch)";
            try
            {
                var kill = await FundClient.KillAsync("engine stopped by user");
                if (!kill.Ok)
                {
                    if (!await NewsroomBackend.IsRunningAsync())
                    {
                        FundDialogs.Info(this, "Stop engine", "The Hedge Fund backend is not running — there is nothing to stop.");
                        return;
                    }
                    if (!FundDialogs.Confirm(this, "Kill switch failed",
                            "The engine did not confirm the halt: " + kill.Describe() +
                            "\n\nShut the backend down anyway? Open simulated positions then stay as they are until the next start " +
                            "(the autopilot is always off after a backend start).",
                            "Shut down anyway", danger: true))
                        return;
                }

                StopEngineHintText.Text = "Stopping… (shutting the backend down)";
                var (ok, message) = await NewsroomBackend.ShutdownAsync();
                var orders = kill.Value?.Orders ?? new List<FundOrder>();
                var killText = !kill.Ok ? "The halt was not confirmed. "
                    : orders.Count == 0 ? "The engine was halted (no positions to close). "
                    : "The engine was halted; closing orders:\n" + string.Join("\n", orders.Take(8).Select(FundOrderText.Result)) +
                      (orders.Count > 8 ? "\n…" : "") + "\n\n";
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

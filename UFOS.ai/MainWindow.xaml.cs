using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Globalization;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using GlobalMonitoringWindow = UFOS.ai.GlobalMonitoring.MainWindow;
using UFOS.ai.HedgeFund;
using UFOS.ai.Logging;
using UFOS.ai.Services;
using UFOS.ai.Shared;

namespace UFOS.ai
{
    public partial class MainWindow : Window
    {
        private static readonly HttpClient OverviewHttpClient = CreateOverviewHttpClient();

        private readonly MarketQuoteService _marketQuoteService = new(OverviewHttpClient);
        private readonly NewsFeedService _newsFeedService = new(OverviewHttpClient);
        // Portfolio Insight card (HedgeFund stage 9b, SPEC §20.3): the HedgeFund backend's insights over the user's imported
        // broker statements; reached like the AI-budget window (BackendAuth token, port 8867). Refreshed by the 5-min overview
        // timer and on window activation (at most once a minute), never while the window is hidden or minimized.
        private readonly IPortfolioInsightProvider _portfolioInsightProvider = new HedgeFundPortfolioInsightProvider();
        private bool _portfolioInsightBusy;
        private DateTime _portfolioInsightLastRefreshUtc = DateTime.MinValue;
        // §20.8: the card starts the backend when it is not running (no window); the last card state decides what a click does.
        private PortfolioInsightState? _portfolioInsightLastState;
        private bool _hedgeFundStartRequested;
        private bool _portfolioInsightRefreshPending;   // the backend came online while a refresh was running
        private string? _hedgeFundLastFailure;           // §20.8: why the last start from the card ended — kept until a start succeeds

        // Kept from the last successful refresh so a card click can open the popup
        // with real data without re-fetching.
        private NewsHeadline? _marketsHeadline;
        private NewsHeadline? _moneyFlowsHeadline;

        private DispatcherTimer? _clockTimer;
        private DispatcherTimer? _settingsCloseTimer;
        private bool _syncingAiNoticeCheckBox;
        private DispatcherTimer? _launchCaptionTimer;
        private DispatcherTimer? _overviewRefreshTimer;
        private bool _ufoFlightStarted;
        private AiBudgetsWindow? _aiBudgetsWindow;

        public MainWindow()
        {
            InitializeComponent();
            ApplyScreenSizeSafeguards();
            // user request 2026-10-23: the main content windows start maximized (within the work area: WM_GETMINMAXINFO hook);
            // WPF raises no StateChanged for the initial state, so the maximized look (square corners) is applied once here
            WindowMaximize.StartMaximized(this);
            Window_StateChanged(this, EventArgs.Empty);
        }

        // MinWidth/MinHeight (and the default Width/Height) are declared in XAML as
        // fixed logical-pixel values, but the logical work area actually available
        // shrinks with both screen resolution AND DPI scaling - e.g. a common
        // 1920x1080 display at 175% scaling only has ~1097x617 logical pixels, less
        // than this window's declared MinHeight="640". When that happens WPF simply
        // refuses to size the window below its Min* constraints, so it ends up bigger
        // than the screen with edges (the title bar's Close button, or the bottom
        // module row) permanently off-screen and unreachable. Clamping Min*/initial
        // size to the actual work area at startup - and never raising them - fixes
        // that without changing behavior on any screen that already fits comfortably.
        private void ApplyScreenSizeSafeguards()
        {
            var workArea = SystemParameters.WorkArea;

            MinWidth = Math.Min(MinWidth, workArea.Width);
            MinHeight = Math.Min(MinHeight, workArea.Height);

            if (Width > workArea.Width) Width = workArea.Width;
            if (Height > workArea.Height) Height = workArea.Height;
        }

        // WindowStyle="None" + AllowsTransparency="True" windows overflow past the
        // monitor's actual work area when maximized (Windows sizes them to the raw
        // monitor rect instead, which is larger than the visible desktop by the hidden
        // resize-border thickness and covers the taskbar) - that overflow is exactly
        // what clips the title bar's Minimize/Close buttons off-screen when fullscreened,
        // and the exact amount varies by monitor/DPI, which is why it only shows up on
        // some screens. Overriding WM_GETMINMAXINFO constrains the maximized bounds to
        // the real work area instead.
        protected override void OnSourceInitialized(EventArgs e)
        {
            base.OnSourceInitialized(e);
            if (PresentationSource.FromVisual(this) is HwndSource hwndSource)
            {
                hwndSource.AddHook(WindowProc);
            }
        }

        private static IntPtr WindowProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            const int WM_GETMINMAXINFO = 0x0024;
            if (msg == WM_GETMINMAXINFO)
            {
                ConstrainMaximizedBoundsToWorkArea(hwnd, lParam);
                handled = true;
            }

            return IntPtr.Zero;
        }

        private static void ConstrainMaximizedBoundsToWorkArea(IntPtr hwnd, IntPtr lParam)
        {
            var mmi = Marshal.PtrToStructure<MINMAXINFO>(lParam);

            const int MONITOR_DEFAULTTONEAREST = 2;
            var monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
            if (monitor != IntPtr.Zero)
            {
                var monitorInfo = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
                GetMonitorInfo(monitor, ref monitorInfo);

                var workArea = monitorInfo.rcWork;
                var monitorArea = monitorInfo.rcMonitor;

                mmi.ptMaxPosition.X = Math.Abs(workArea.Left - monitorArea.Left);
                mmi.ptMaxPosition.Y = Math.Abs(workArea.Top - monitorArea.Top);
                mmi.ptMaxSize.X = Math.Abs(workArea.Right - workArea.Left);
                mmi.ptMaxSize.Y = Math.Abs(workArea.Bottom - workArea.Top);
            }

            Marshal.StructureToPtr(mmi, lParam, true);
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct POINT
        {
            public int X;
            public int Y;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MINMAXINFO
        {
            public POINT ptReserved;
            public POINT ptMaxSize;
            public POINT ptMaxPosition;
            public POINT ptMinTrackSize;
            public POINT ptMaxTrackSize;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct RECT
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MONITORINFO
        {
            public int cbSize;
            public RECT rcMonitor;
            public RECT rcWork;
            public int dwFlags;
        }

        [DllImport("user32.dll")]
        private static extern IntPtr MonitorFromWindow(IntPtr handle, int flags);

        [DllImport("user32.dll")]
        private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO lpmi);

        private static HttpClient CreateOverviewHttpClient()
        {
            var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            client.DefaultRequestHeaders.UserAgent.ParseAdd("Mozilla/5.0 (compatible; UFOSai/1.0)");
            return client;
        }

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            // Reflect the persisted startup-notice opt-out without writing it back.
            _syncingAiNoticeCheckBox = true;
            ShowAiNoticeCheckBox.IsChecked = AiDisclaimerWindow.IsShownAtStartup;
            _syncingAiNoticeCheckBox = false;

            _clockTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _clockTimer.Tick += (_, __) => UpdateClock();
            _clockTimer.Start();
            UpdateClock();

            // Wait one render pass so HeadingText has a real ActualWidth/ActualHeight
            // before we build the UFO's flight path from it (mirrors the reference
            // design's JS, which measures the rendered heading before animating).
            Dispatcher.BeginInvoke(new Action(SetupUfoFlight), DispatcherPriority.Loaded);

            _overviewRefreshTimer = new DispatcherTimer { Interval = TimeSpan.FromMinutes(5) };
            _overviewRefreshTimer.Tick += (_, __) => _ = RefreshOverviewCardsAsync();
            _overviewRefreshTimer.Start();
            _ = RefreshOverviewCardsAsync();
            Activated += MainWindow_Activated;   // Portfolio Insight card: refresh on activation (throttled)
            // §20.8: Hedge Fund backend status → the card (raised on any thread, marshalled below); optional start with the app
            HedgeFundBackendHost.StatusChanged += OnHedgeFundBackendStatusChanged;
            if (HedgeFundBackendHost.StartWithApp)
            {
                // in the background, never awaited: the UI does not wait for the backend (same start as the Hedge Fund window)
                _hedgeFundStartRequested = true;
                Dispatcher.BeginInvoke(new Action(() => _ = HedgeFundBackendHost.EnsureStartedAsync()), DispatcherPriority.ApplicationIdle);
            }

            // Prepare the module backends' Python environments once, in the background
            // (Shared/PythonEnvironment.cs): create missing venvs, pip install only when a
            // requirements.txt changed. Module launchers wait for it before starting a backend.
            PythonEnvironment.StateChanged += OnPythonEnvironmentStateChanged;
            PythonEnvironment.StartAppPreparation();
            RenderPythonEnvBanner();

            // Python packages of the module backends: fast offline check now, online
            // "pip list --outdated" in the background at most once a day.
            PythonEnvManager.Changed += OnPythonEnvChanged;
            RenderPythonEnvStatus();
            _ = InitPythonEnvAsync();
        }

        private void Window_Closing(object? sender, CancelEventArgs e)
        {
            PythonEnvManager.Changed -= OnPythonEnvChanged;
            PythonEnvironment.StateChanged -= OnPythonEnvironmentStateChanged;
            _pythonEnvSpinnerTimer?.Stop();
            _clockTimer?.Stop();
            _settingsCloseTimer?.Stop();
            _launchCaptionTimer?.Stop();
            _overviewRefreshTimer?.Stop();
            Activated -= MainWindow_Activated;
            HedgeFundBackendHost.StatusChanged -= OnHedgeFundBackendStatusChanged;
            _aiBudgetsWindow?.Close();
        }

        private void UpdateClock()
        {
            ClockText.Text = DateTime.Now.ToString("HH:mm:ss");
        }

        // ===================== Overview bar (Markets / Finance / Money Flows / =====================
        // ===================== Verse of the Day / Portfolio Insight cards)      =====================

        private async Task RefreshOverviewCardsAsync()
        {
            RefreshVerseOfTheDayCard();
            await RefreshFinanceCardAsync();
            await RefreshNewsCardsAsync();
            await RefreshPortfolioInsightCardAsync();
        }

        private void RefreshVerseOfTheDayCard()
        {
            var verse = VerseOfTheDay.GetForToday();
            VerseReferenceText.Text = verse.Reference;
            VerseQuoteText.Text = $"\"{verse.Text}\"";
        }

        private async Task RefreshFinanceCardAsync()
        {
            try
            {
                var quote = await _marketQuoteService.GetQuoteAsync("^GSPC");
                if (quote is null) return;

                FinanceValueText.Text = quote.Price.ToString("N2", CultureInfo.InvariantCulture);

                if (quote.ChangePercent is double changePercent)
                {
                    string sign = changePercent >= 0 ? "+" : string.Empty;
                    FinanceChangeText.Text = $"{sign}{changePercent.ToString("N2", CultureInfo.InvariantCulture)}% today · S&P 500";
                    FinanceValueText.Foreground = (Brush)FindResource(changePercent >= 0 ? "PositiveGreenBrush" : "AccentRedBrush");
                }
                else
                {
                    FinanceChangeText.Text = $"S&P 500 · {FormatRelativeAgo(quote.FetchedAt)}";
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("Overview", "Failed to refresh Finance card", ex); } catch { }
            }
        }

        private async Task RefreshNewsCardsAsync()
        {
            try
            {
                var headlines = await _newsFeedService.GetTopStoriesAsync();
                if (headlines.Count == 0) return;

                var marketsHeadline = headlines[0];
                _marketsHeadline = marketsHeadline;
                MarketsHeadlineText.Text = marketsHeadline.Title;
                MarketsSourceText.Text = $"{marketsHeadline.Source} · {FormatRelativeAgo(marketsHeadline.PublishedAt)}";

                var moneyFlowsHeadline = NewsFeedService.PickMoneyFlowsHeadline(headlines);
                if (moneyFlowsHeadline is not null)
                {
                    _moneyFlowsHeadline = moneyFlowsHeadline;
                    MoneyFlowsHeadlineText.Text = moneyFlowsHeadline.Title;
                    MoneyFlowsSourceText.Text = $"{moneyFlowsHeadline.Source} · {FormatRelativeAgo(moneyFlowsHeadline.PublishedAt)}";
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("Overview", "Failed to refresh Markets/Money Flows cards", ex); } catch { }
            }
        }

        private async Task RefreshPortfolioInsightCardAsync()
        {
            // Low frequency (5-min overview timer + activation ≥ 60 s apart); nothing while the window is hidden or minimized.
            if (_portfolioInsightBusy) { _portfolioInsightRefreshPending = true; return; }
            if (!IsVisible || WindowState == WindowState.Minimized) return;
            _portfolioInsightBusy = true;
            _portfolioInsightLastRefreshUtc = DateTime.UtcNow;
            try
            {
                var card = await _portfolioInsightProvider.GetCardAsync();
                ApplyPortfolioInsightCard(card);
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("Overview", "Failed to refresh Portfolio Insight card", ex); } catch { }
                ApplyPortfolioInsightCard(new PortfolioInsightCard(PortfolioInsightState.Unavailable, Array.Empty<string>(), 0, null, null, null,
                    "not available (" + ex.GetType().Name + ")", Array.Empty<string>()));
            }
            finally
            {
                _portfolioInsightBusy = false;
            }
            if (_portfolioInsightRefreshPending)
            {
                _portfolioInsightRefreshPending = false;
                _ = RefreshPortfolioInsightCardAsync();
            }
        }

        /// <summary>
        /// Shows the card from what the provider built from GET /v2/insights/summary — no stand-in numbers: not running /
        /// no imports / not available (reason) are short texts only. Compact layout (user feedback 2026-10-19): per currency one
        /// "Realised" and one "Unrealised" row (number left, coloured by sign; label right, muted), never summed across currencies,
        /// at most 2 currencies (+N more); an amber row only when incomplete; one short label line. Depot count, price dates,
        /// computed time, the full label and the per-depot reasons are in the ToolTip. The figures are the provider's own texts
        /// (structured fields filled from the summary JSON, formatted with Signed) — nothing is recomputed or parsed from text.
        /// </summary>
        private void ApplyPortfolioInsightCard(PortfolioInsightCard card)
        {
            _portfolioInsightLastState = card.State;
            PortfolioInsightSandboxChip.Visibility = card.Sandbox ? Visibility.Visible : Visibility.Collapsed;   // sandbox profile (GET /health mode)
            if (card.State != PortfolioInsightState.BackendNotRunning && card.State != PortfolioInsightState.Unavailable) _hedgeFundLastFailure = null;   // the backend answers again
            // §20.8: while the backend this app is starting is not answering yet, the card keeps showing the start progress
            if (card.State == PortfolioInsightState.BackendNotRunning && _hedgeFundStartRequested && HedgeFundBackendHost.IsStarting)
            {
                ShowHedgeFundStarting();
                return;
            }
            if (card.State != PortfolioInsightState.Ok && card.State != PortfolioInsightState.Incomplete)
            {
                var second = card.State == PortfolioInsightState.BackendNotRunning && _hedgeFundLastFailure != null ? "Hedge Fund backend: " + _hedgeFundLastFailure : null;
                ShowPortfolioInsightText(card.Message ?? "not available", second,
                    card.State == PortfolioInsightState.BackendNotRunning ? ". Press to start the backend." : ". Press to open Hedge Fund, Monitoring, Portfolio insights.");
                return;
            }

            // ----- structured fields only (provider: straight from the summary JSON; review 2026-10-19 — no text parsing) -----
            var year = card.Year?.ToString(CultureInfo.InvariantCulture);
            var realised = card.Realised ?? Array.Empty<PortfolioRealised>();
            var unrealised = card.Unrealised ?? Array.Empty<PortfolioUnrealised>();
            var currencies = realised.Select(x => x.Currency).Concat(unrealised.Select(x => x.Currency)).Distinct(StringComparer.Ordinal).ToList();
            var shown = currencies.Take(2).ToList();
            var more = currencies.Count - shown.Count;

            _portfolioInsightDefaultToolTip ??= PortfolioInsightCardButton.ToolTip as string;
            PortfolioInsightCaptionText.Text = "PORTFOLIO INSIGHT" + (year != null ? " · " + year : "");
            var rows = PortfolioInsightRowsPanel;
            rows.Children.Clear();
            var spoken = new List<string>();
            var realisedShown = 0;
            foreach (var cur in shown)
            {
                var r = realised.FirstOrDefault(x => string.Equals(x.Currency, cur, StringComparison.Ordinal));
                if (r == null) continue;
                var amount = HedgeFundPortfolioInsightProvider.Signed(r.Amount, r.Currency);
                rows.Children.Add(InsightFigureRow(amount, Math.Round(r.Amount, 2), "Realised", 17, true));
                spoken.Add("Realised " + amount);
                realisedShown++;
            }
            if (realisedShown == 0)
            {
                var text = "Realised" + (year != null ? " " + year : "") + " — nothing realised yet";
                rows.Children.Add(InsightTextRow(text));
                spoken.Add(text);
            }
            foreach (var cur in shown)
            {
                var u = unrealised.FirstOrDefault(x => string.Equals(x.Currency, cur, StringComparison.Ordinal));
                if (u != null && !u.NothingValued && u.Amount is double ua && u.CoveragePct is int cov && cov > 0)
                {
                    var amount = HedgeFundPortfolioInsightProvider.Signed(ua, u.Currency);
                    var label = "Unrealised · " + cov.ToString(CultureInfo.InvariantCulture) + " % priced";
                    rows.Children.Add(InsightFigureRow(amount, Math.Round(ua, 2), label, 13, false));
                    spoken.Add(label + " " + amount);
                }
                else
                {
                    var text = "Unrealised" + (currencies.Count > 1 ? " " + cur : "") + " — not available";
                    rows.Children.Add(InsightTextRow(text));
                    spoken.Add(text);
                }
            }
            if (shown.Count == 0)
            {
                const string text = "Unrealised — not available";
                rows.Children.Add(InsightTextRow(text));
                spoken.Add(text);
            }
            if (more > 0)
            {
                var text = "+" + more.ToString(CultureInfo.InvariantCulture) + " more currenc" + (more == 1 ? "y" : "ies") + " — open insights";
                rows.Children.Add(InsightTextRow(text));
                spoken.Add(text);
            }
            rows.Visibility = Visibility.Visible;
            PortfolioInsightHeadlineText.Visibility = Visibility.Collapsed;
            PortfolioInsightDetailText.Visibility = Visibility.Collapsed;

            // ----- amber row: only when incomplete — the backend's position counts (positions − valued, summed over currencies) -----
            if (card.State == PortfolioInsightState.Incomplete)
            {
                var unpriced = card.UnpricedCount ?? 0;
                PortfolioInsightWarningText.Text = unpriced > 0
                    ? "⚠ " + unpriced.ToString(CultureInfo.InvariantCulture) + " position" + (unpriced == 1 ? "" : "s") + " without a price"
                    : "⚠ Incomplete — hover for the reasons";
                PortfolioInsightWarningText.Visibility = Visibility.Visible;
                spoken.Add(PortfolioInsightWarningText.Text.TrimStart('⚠', ' '));
            }
            else PortfolioInsightWarningText.Visibility = Visibility.Collapsed;
            PortfolioInsightLabelText.Visibility = Visibility.Visible;

            // ----- tooltip: everything that left the face -----
            var tip = new List<string>();
            if (card.DepotCount is int depots) tip.Add(depots.ToString(CultureInfo.InvariantCulture) + " depot" + (depots == 1 ? "" : "s"));
            var dates = string.Join(" · ", new[] { card.PricesAsOf, card.ComputedAt }.Where(x => !string.IsNullOrWhiteSpace(x)));
            if (dates.Length > 0) tip.Add(dates);
            if (card.Reasons.Count > 0) tip.Add("Incomplete:\n" + string.Join("\n", card.Reasons.Select(x => "• " + x)));
            var disclaimer = card.Disclaimer ?? card.Label;
            if (!string.IsNullOrWhiteSpace(disclaimer)) tip.Add(disclaimer);
            tip.Add("Click for the details in Hedge Fund → Monitoring → Portfolio insights.");
            PortfolioInsightCardButton.ToolTip = string.Join("\n\n", tip);

            System.Windows.Automation.AutomationProperties.SetName(PortfolioInsightCardButton,
                "Portfolio Insight" + (year != null ? " " + year : "") + ": " + string.Join(". ", spoken)
                + (string.IsNullOrWhiteSpace(disclaimer) ? "" : ". " + disclaimer)
                + ". Press to open Hedge Fund, Monitoring, Portfolio insights.");
        }

        private string? _portfolioInsightDefaultToolTip;

        /// <summary>A state of the card (not running / starting / no imports / not available): at most two short lines, no figures.</summary>
        private void ShowPortfolioInsightText(string line1, string? line2, string pressHint)
        {
            _portfolioInsightDefaultToolTip ??= PortfolioInsightCardButton.ToolTip as string;
            PortfolioInsightCaptionText.Text = "PORTFOLIO INSIGHT";
            PortfolioInsightRowsPanel.Children.Clear();
            PortfolioInsightRowsPanel.Visibility = Visibility.Collapsed;
            PortfolioInsightHeadlineText.Text = line1;
            PortfolioInsightHeadlineText.Visibility = Visibility.Visible;
            PortfolioInsightDetailText.Text = line2 ?? "";
            PortfolioInsightDetailText.Visibility = string.IsNullOrWhiteSpace(line2) ? Visibility.Collapsed : Visibility.Visible;
            PortfolioInsightWarningText.Visibility = Visibility.Collapsed;
            PortfolioInsightLabelText.Visibility = Visibility.Collapsed;
            PortfolioInsightCardButton.ToolTip = string.IsNullOrWhiteSpace(line2) ? _portfolioInsightDefaultToolTip : line1 + "\n" + line2 + "\n\n" + _portfolioInsightDefaultToolTip;
            System.Windows.Automation.AutomationProperties.SetName(PortfolioInsightCardButton,
                "Portfolio Insight: " + line1 + (string.IsNullOrWhiteSpace(line2) ? "" : ". " + line2) + pressHint);
        }

        /// <summary>One figure row: the amount (Signed) left — green / red by the value's sign, neutral at 0 — the label right, muted.</summary>
        private Grid InsightFigureRow(string amount, double rounded, string label, double size, bool bold)
        {
            var row = new Grid { Margin = new Thickness(0, 0, 0, 2) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var brushKey = rounded > 0 ? "PositiveGreenBrush" : rounded < 0 ? "AccentRedBrush" : "PrimaryForegroundBrush";
            var number = new TextBlock
            {
                Text = amount,
                FontSize = size,
                FontFamily = new FontFamily("Consolas"),
                FontWeight = bold ? FontWeights.SemiBold : FontWeights.Normal,
                Foreground = (Brush)FindResource(brushKey),
                VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            var caption = new TextBlock
            {
                Text = label,
                FontSize = 10.5,
                Foreground = (Brush)FindResource("MutedForegroundBrush"),
                HorizontalAlignment = HorizontalAlignment.Right,
                VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
                Margin = new Thickness(10, 0, 0, 0),
            };
            Grid.SetColumn(caption, 1);
            row.Children.Add(number);
            row.Children.Add(caption);
            return row;
        }

        /// <summary>A muted text row (no number): "Unrealised — not available", "+N more currencies — open insights".</summary>
        private TextBlock InsightTextRow(string text) => new()
        {
            Text = text,
            FontSize = 10.5,
            Foreground = (Brush)FindResource("MutedForegroundBrush"),
            TextTrimming = TextTrimming.CharacterEllipsis,
            Margin = new Thickness(0, 2, 0, 2),
        };

        private void MainWindow_Activated(object? sender, EventArgs e)
        {
            if (DateTime.UtcNow - _portfolioInsightLastRefreshUtc < TimeSpan.FromSeconds(60)) return;
            _ = RefreshPortfolioInsightCardAsync();
        }

        // §20.8: backend status → card. Starting (checking / Python environment / installing packages / starting…) shows the
        // progress verbatim; online refreshes the figures at once; offline after a start shows "not running — click to start"
        // with the reason. No cached or old numbers.
        private void OnHedgeFundBackendStatusChanged()
        {
            try { Dispatcher.BeginInvoke(new Action(ApplyHedgeFundBackendStatus)); } catch { }
        }

        private void ApplyHedgeFundBackendStatus()
        {
            if (!_hedgeFundStartRequested) return;   // the Hedge Fund window manages its own start; the card refreshes on its timer
            if (HedgeFundBackendHost.IsStarting)
            {
                ShowHedgeFundStarting();
                return;
            }
            if (HedgeFundBackendHost.IsOnline)
            {
                _hedgeFundStartRequested = false;
                _hedgeFundLastFailure = null;
                _portfolioInsightLastRefreshUtc = DateTime.MinValue;
                _ = RefreshPortfolioInsightCardAsync();
                return;
            }
            // every other status ends this start: "offline — <reason>", "outdated backend running — …", "not started" (nothing ran)
            _hedgeFundStartRequested = false;
            _portfolioInsightLastState = PortfolioInsightState.BackendNotRunning;
            var status = HedgeFundBackendHost.Status;
            _hedgeFundLastFailure = status;
            var offline = status.StartsWith("offline", StringComparison.OrdinalIgnoreCase) || status.StartsWith("not started", StringComparison.OrdinalIgnoreCase);
            var headline = offline ? HedgeFundPortfolioInsightProvider.NotRunningText : "Hedge Fund backend: " + status;
            ShowPortfolioInsightText(headline, offline ? "Hedge Fund backend: " + status : null, ". Press to start the backend.");
        }

        private void ShowHedgeFundStarting()
        {
            const string starting = "Starting Hedge Fund backend (also runs the paper engine)…";
            var status = HedgeFundBackendHost.Status;
            ShowPortfolioInsightText(starting, "Hedge Fund backend: " + status, "");
        }

        // Card click (§20.8, user decision 2026-10-18 — no confirmation): backend not running → START it (the window does not
        // open; the card shows the progress, then the figures); a start already running → nothing; backend running → Hedge Fund
        // → Monitoring → Portfolio insights: the open Hedge Fund window switches there; otherwise the window is opened through the
        // same OpenModuleWindow path as the Hedge Fund tile and switches once it is loaded.
        private async void PortfolioInsightCard_Click(object sender, RoutedEventArgs e)
        {
            if (HedgeFundBackendHost.IsStarting) { ShowHedgeFundStarting(); return; }
            // not known to be running (down, or the first refresh has not finished yet): ask /health first
            if (!HedgeFundBackendHost.IsOnline
                && (_portfolioInsightLastState == null || _portfolioInsightLastState == PortfolioInsightState.BackendNotRunning)
                && !await HedgeFundBackendHost.IsRunningAsync())
            {
                if (HedgeFundBackendHost.IsStarting) { ShowHedgeFundStarting(); return; }
                _hedgeFundStartRequested = true;
                ShowHedgeFundStarting();
                await HedgeFundBackendHost.EnsureStartedAsync();   // returns at once when a start runs or the process is alive
                ApplyHedgeFundBackendStatus();                     // no-op start (e.g. alive but not answering) → its end state, never stuck
                return;
            }
            var open = Application.Current?.Windows.OfType<HedgeFundWindow>().FirstOrDefault(w => w.IsLoaded && !w.IsClosingOrClosed);
            if (open != null)
            {
                open.ShowPortfolioInsights();
                return;
            }
            OpenModuleWindow(HedgeFundTile, () =>
            {
                var window = new HedgeFundWindow();
                window.Show();
                window.Dispatcher.BeginInvoke(new Action(window.ShowPortfolioInsights), DispatcherPriority.Loaded);
                return window;
            });
            HedgeFundTile.IsEnabled = true;   // the tile stays clickable: a second click brings the window back (OpenHedgeFund_Click)
        }

        private static string FormatRelativeAgo(DateTimeOffset timestamp)
        {
            var elapsed = DateTimeOffset.Now - timestamp;
            if (elapsed < TimeSpan.Zero) elapsed = TimeSpan.Zero;

            if (elapsed.TotalMinutes < 1) return "just now";
            if (elapsed.TotalHours < 1) return $"{(int)elapsed.TotalMinutes}m ago";
            if (elapsed.TotalDays < 1) return $"{(int)elapsed.TotalHours}h ago";
            return $"{(int)elapsed.TotalDays}d ago";
        }

        private void MarketsCard_Click(object sender, RoutedEventArgs e)
        {
            if (_marketsHeadline is not null)
            {
                new ArticleWindow(_marketsHeadline, this).Show();
            }
        }

        private void MoneyFlowsCard_Click(object sender, RoutedEventArgs e)
        {
            if (_moneyFlowsHeadline is not null)
            {
                new ArticleWindow(_moneyFlowsHeadline, this).Show();
            }
        }

        // ===================== Window chrome =====================

        // Title bar (borderless window, conventions "Borderless windows"): double-click toggles maximize;
        // dragging a MAXIMIZED window restores it only once the mouse actually moves
        // with the button held (a plain click does nothing), keeps the cursor at its
        // relative spot on the title bar, then drags. Same pattern as
        // HedgeFund/HedgeFundWindow.xaml.cs (TitleBar_MouseMove).
        private bool _restoreOnDrag;
        private System.Windows.Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (e.ClickCount == 2)
            {
                _restoreOnDrag = false;
                WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                return;
            }
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
            var toDip = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice
                        ?? System.Windows.Media.Matrix.Identity;
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

        // Maximized: square corners. The WM_GETMINMAXINFO hook (ConstrainMaximizedBoundsToWorkArea) already fits
        // the maximized window to the work area, and a transparent window has no resize frame
        // hanging off the screen, so no extra padding is needed.
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            var maximized = WindowState == WindowState.Maximized;
            RootBorder.CornerRadius = maximized ? new CornerRadius(0) : new CornerRadius(8);
            TitleBarBorder.CornerRadius = maximized ? new CornerRadius(0) : new CornerRadius(8, 8, 0, 0);
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        // ===================== UFO flight animation =====================
        //
        // The reference HTML/CSS/JS design uses a CSS `offset-path` on a closed loop
        // plus `offset-rotate: auto` to make the UFO bank into its turns, landing at a
        // point on the loop where the tangent is horizontal (so the landing tilt is
        // gentle, not steep). The WPF equivalent of that whole mechanism is
        // MatrixAnimationUsingPath with DoesRotateWithTangent="true" - it animates a
        // Matrix (rotation + translation) along a PathGeometry in one step, which is
        // exactly the same effect. The landing target position/size is computed from
        // the actual rendered "UFOS.ai" heading, the same way the JS measured
        // getBoundingClientRect() on the heading before building the path.
        private void SetupUfoFlight()
        {
            if (_ufoFlightStarted) return;

            double w = HeadingText.ActualWidth;
            double h = HeadingText.ActualHeight;
            if (w <= 0 || h <= 0)
            {
                // Layout not ready yet - try again shortly instead of skipping the animation.
                Dispatcher.BeginInvoke(new Action(SetupUfoFlight), DispatcherPriority.Loaded);
                return;
            }

            _ufoFlightStarted = true;

            double charWidth = w / Math.Max(HeadingText.Text.Length, 1);
            double targetX = w - 3 + charWidth;
            double targetY = h * -0.06;
            double halfW = (w + 40) / 2;
            double dropH = h * 1.3;

            // Closed loop whose "top" sits exactly at the landing target, so the
            // path's tangent there is horizontal (gentle bank on landing) rather
            // than vertical (steep bank), matching the fix made in the HTML design.
            var figure = new PathFigure { StartPoint = new Point(targetX, targetY), IsClosed = true, IsFilled = false };
            figure.Segments.Add(new BezierSegment(
                new Point(targetX + halfW, targetY),
                new Point(targetX + halfW, targetY + dropH),
                new Point(targetX, targetY + dropH),
                isStroked: true));
            figure.Segments.Add(new BezierSegment(
                new Point(targetX - halfW, targetY + dropH),
                new Point(targetX - halfW, targetY),
                new Point(targetX, targetY),
                isStroked: true));

            var geometry = new PathGeometry();
            geometry.Figures.Add(figure);
            geometry.Freeze();

            // Position the landed (monochrome, superscript-sized) icon at the same
            // target point. Its viewBox is 100x60 (non-square), so the vertical
            // centering must account for that aspect ratio rather than treating the
            // icon as a square box.
            const double landedSize = 21;
            double landedHeight = landedSize * 0.6;
            Canvas.SetLeft(UfoLandedGroup, targetX - landedSize / 2);
            Canvas.SetTop(UfoLandedGroup, targetY - landedHeight / 2);

            var matrixTransform = new MatrixTransform();
            UfoFlightGroup.RenderTransform = matrixTransform;

            var anim = new MatrixAnimationUsingPath
            {
                PathGeometry = geometry,
                Duration = TimeSpan.FromSeconds(9),
                DoesRotateWithTangent = true,
                FillBehavior = FillBehavior.HoldEnd
            };
            anim.Completed += (_, __) => OnUfoLanded(matrixTransform);

            matrixTransform.BeginAnimation(MatrixTransform.MatrixProperty, anim);
        }

        private void OnUfoLanded(MatrixTransform matrixTransform)
        {
            // Read the ACTUAL final computed rotation off the matrix the animation
            // produced, rather than assuming a value, so the landed icon's tilt is
            // guaranteed to match the flying sprite's last frame exactly.
            Matrix m = matrixTransform.Matrix;
            double angleDegrees = Math.Atan2(m.M12, m.M11) * (180 / Math.PI);
            UfoLandedRotate.Angle = angleDegrees;

            var fadeOut = new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(400));
            UfoFlightGroup.BeginAnimation(UIElement.OpacityProperty, fadeOut);

            var fadeIn = new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(400));
            UfoLandedGroup.BeginAnimation(UIElement.OpacityProperty, fadeIn);
        }

        // ===================== Settings popup (mirrors LiveStreamAgent's =====================
        // ===================== 3-second hover-close Popup pattern)      =====================

        private void ShowAiNoticeCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            if (!IsLoaded || _syncingAiNoticeCheckBox) return;
            AiDisclaimerWindow.SetShowAtStartup(ShowAiNoticeCheckBox.IsChecked == true);
        }

        private void AiNoticeLink_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            SettingsPopup.IsOpen = false;
            CancelSettingsCloseTimer();
            AiDisclaimerWindow.ShowForReading(this);
        }

        // Settings popup -> "AI budgets…" (task B, contract UFOS.ai/docs/ai-budgets-spec.md §6): one
        // instance; a second click brings the open window to the front instead of opening a duplicate.
        private void AiBudgetsButton_Click(object sender, RoutedEventArgs e)
        {
            SettingsPopup.IsOpen = false;
            CancelSettingsCloseTimer();
            try
            {
                if (_aiBudgetsWindow is { } existing)
                {
                    if (existing.WindowState == WindowState.Minimized) existing.WindowState = WindowState.Normal;
                    existing.Activate();
                    return;
                }
                var window = new AiBudgetsWindow();
                window.Closed += (_, __) =>
                {
                    if (ReferenceEquals(_aiBudgetsWindow, window)) _aiBudgetsWindow = null;
                };
                _aiBudgetsWindow = window;
                window.Show();
            }
            catch (Exception ex)
            {
                _aiBudgetsWindow = null;
                (Application.Current as IUiErrorReporter)?.ShowUiException(ex);
            }
        }

        private void SettingsButton_Click(object sender, RoutedEventArgs e)
        {
            SettingsPopup.IsOpen = !SettingsPopup.IsOpen;
            CancelSettingsCloseTimer();
        }

        private void SettingsHoverArea_MouseEnter(object sender, MouseEventArgs e)
        {
            CancelSettingsCloseTimer();
        }

        private void SettingsHoverArea_MouseLeave(object sender, MouseEventArgs e)
        {
            StartSettingsCloseTimer();
        }

        private void StartSettingsCloseTimer()
        {
            CancelSettingsCloseTimer();
            _settingsCloseTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
            _settingsCloseTimer.Tick += (_, __) =>
            {
                SettingsPopup.IsOpen = false;
                CancelSettingsCloseTimer();
            };
            _settingsCloseTimer.Start();
        }

        private void CancelSettingsCloseTimer()
        {
            _settingsCloseTimer?.Stop();
            _settingsCloseTimer = null;
        }

        // ===================== Settings popup: Python packages =====================
        // Services/PythonEnvManager.cs does the work; this only renders its state.

        private const string PythonEnvLogModule = "Python env";

        private static async Task InitPythonEnvAsync()
        {
            try
            {
                await Task.Run(() => PythonEnvManager.RefreshOffline());
                // Don't run "pip list --outdated" while the app-start preparation installs
                // packages; afterwards re-read the (now updated) required-install state.
                await PythonEnvironment.AllReady;
                await Task.Run(() => PythonEnvManager.RefreshOffline());
                await Task.Run(() => PythonEnvManager.RunOnlineCheckIfDueAsync());
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(PythonEnvLogModule, "Python package check failed.", ex); } catch { }
            }
        }

        // ===================== Header: Python environment preparation banner =====================
        // Shared/PythonEnvironment.cs prepares every module venv once at app start; this
        // renders its state under the clock: spinner while busy, "ready" for ~4 s, failures stay.

        private static readonly string[] PythonEnvSpinnerFrames = { "|", "/", "-", "\\" };
        private DispatcherTimer? _pythonEnvSpinnerTimer;
        private int _pythonEnvSpinnerFrame;
        private bool _pythonEnvReadyShown;

        private void OnPythonEnvironmentStateChanged()
        {
            // Raised on whatever thread changed the state.
            try { Dispatcher.BeginInvoke(new Action(RenderPythonEnvBanner)); } catch { }
        }

        private void RenderPythonEnvBanner()
        {
            var text = PythonEnvironment.StatusText;
            if (PythonEnvironment.IsBusy)
            {
                PythonEnvBanner.BeginAnimation(OpacityProperty, null);
                PythonEnvBanner.Opacity = 1;
                PythonEnvBanner.Cursor = null;
                PythonEnvBanner.ToolTip = "Module backends start once their Python environment is ready.";
                PythonEnvBannerIcon.Foreground = PythonEnvBrush("AccentAmberBrush");
                PythonEnvBannerText.Foreground = PythonEnvBrush("MutedForegroundBrush");
                PythonEnvBannerText.Text = text;
                PythonEnvBanner.Visibility = Visibility.Visible;
                if (_pythonEnvSpinnerTimer == null)
                {
                    _pythonEnvSpinnerTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(150) };
                    _pythonEnvSpinnerTimer.Tick += (_, __) =>
                    {
                        _pythonEnvSpinnerFrame = (_pythonEnvSpinnerFrame + 1) % PythonEnvSpinnerFrames.Length;
                        PythonEnvBannerIcon.Text = PythonEnvSpinnerFrames[_pythonEnvSpinnerFrame];
                    };
                }
                PythonEnvBannerIcon.Text = PythonEnvSpinnerFrames[_pythonEnvSpinnerFrame];
                _pythonEnvSpinnerTimer.Start();
                return;
            }

            _pythonEnvSpinnerTimer?.Stop();
            if (string.IsNullOrEmpty(text))
            {
                PythonEnvBanner.Visibility = Visibility.Collapsed;
                return;
            }

            if (PythonEnvironment.FailedModules.Count > 0)
            {
                PythonEnvBanner.BeginAnimation(OpacityProperty, null);
                PythonEnvBanner.Opacity = 1;
                PythonEnvBanner.Cursor = Cursors.Hand;
                PythonEnvBanner.ToolTip = "Click to open the Backend Log. The module retries the setup when it starts its backend.";
                PythonEnvBannerIcon.Text = "!";
                PythonEnvBannerIcon.Foreground = PythonEnvBrush("CautionBrush");
                PythonEnvBannerText.Foreground = PythonEnvBrush("CautionBrush");
                PythonEnvBannerText.Text = text;
                PythonEnvBanner.Visibility = Visibility.Visible;
                return;
            }

            // Ready: show it once, then fade out.
            if (_pythonEnvReadyShown) return;
            _pythonEnvReadyShown = true;
            PythonEnvBanner.Cursor = null;
            PythonEnvBanner.ToolTip = null;
            PythonEnvBannerIcon.Text = "✓";
            PythonEnvBannerIcon.Foreground = PythonEnvBrush("PositiveGreenBrush");
            PythonEnvBannerText.Foreground = PythonEnvBrush("MutedForegroundBrush");
            PythonEnvBannerText.Text = text;
            PythonEnvBanner.Visibility = Visibility.Visible;
            _ = HidePythonEnvBannerLaterAsync();
        }

        private async Task HidePythonEnvBannerLaterAsync()
        {
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(4));
                if (PythonEnvironment.IsBusy || PythonEnvironment.FailedModules.Count > 0) return;
                var fade = new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(600));
                fade.Completed += (_, __) =>
                {
                    if (PythonEnvironment.IsBusy || PythonEnvironment.FailedModules.Count > 0) return;
                    PythonEnvBanner.Visibility = Visibility.Collapsed;
                    PythonEnvBanner.BeginAnimation(OpacityProperty, null);
                    PythonEnvBanner.Opacity = 1;
                };
                PythonEnvBanner.BeginAnimation(OpacityProperty, fade);
            }
            catch { }
        }

        private void PythonEnvBanner_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
        {
            if (PythonEnvironment.FailedModules.Count == 0) return;
            try { BackendLogsWindow.Show(this); }
            catch (Exception ex)
            {
                try { AppLog.Warn(PythonEnvLogModule, "Could not open the Backend Log.", ex); } catch { }
            }
        }

        private void OnPythonEnvChanged()
        {
            // Raised on whatever thread changed the state.
            try { Dispatcher.BeginInvoke(new Action(RenderPythonEnvStatus)); } catch { }
        }

        private void RenderPythonEnvStatus()
        {
            var statuses = PythonEnvManager.Statuses;
            PythonEnvModuleList.Children.Clear();
            foreach (var status in statuses)
                PythonEnvModuleList.Children.Add(BuildPythonEnvRow(status));

            var required = statuses.Count(st => st.RequiredReason != null);
            var updates = statuses.Sum(st => st.Outdated.Count);
            string summary;
            if (PythonEnvManager.IsUpdating) summary = "updating…";
            else if (PythonEnvManager.IsChecking) summary = "checking…";
            else if (statuses.Count == 0) summary = "checking…";
            else if (required == 0 && updates == 0) summary = "up to date";
            else
            {
                var parts = new List<string>();
                if (required > 0) parts.Add($"{required} required");
                if (updates > 0) parts.Add($"{updates} update{(updates == 1 ? "" : "s")}");
                summary = string.Join(" · ", parts);
            }
            PythonEnvSummaryText.Text = summary;
            var lastCheck = PythonEnvManager.LastOnlineCheckUtc;
            var heldBack = statuses.SelectMany(st => st.HeldBack.Select(h => st.Name + ": " + h)).ToList();
            PythonEnvSummaryText.ToolTip = (lastCheck == null
                ? "No online update check yet (runs in the background at most once a day)."
                : "Last online update check: " + lastCheck.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture))
                + (heldBack.Count > 0 ? "\nHeld back for ccxt (not counted as updates): " + string.Join(", ", heldBack) : "");

            SettingsUpdateDot.Visibility = PythonEnvManager.HasPending ? Visibility.Visible : Visibility.Collapsed;
            UpdatePythonPackagesButton.IsEnabled = !PythonEnvManager.IsUpdating;
            UpdatePythonPackagesButtonText.Text = PythonEnvManager.IsUpdating ? "Updating Python packages…" : "Update Python packages";
        }

        private UIElement BuildPythonEnvRow(PythonEnvModuleStatus status)
        {
            string state;
            string stateBrush;
            string? detail = null;
            if (status.Module == null)
            {
                state = "not found";
                stateBrush = "MutedForegroundBrush";
                detail = "Backend folder not found next to this app.";
            }
            else if (status.Locked)
            {
                state = "installing…";
                stateBrush = "CautionBrush";
                detail = "An install/update is running — the backend starts once it's done.";
            }
            else if (status.RequiredReason != null)
            {
                state = "required";
                stateBrush = "CautionBrush";
                detail = $"Required — installs automatically on next module start ({status.RequiredReason}).";
            }
            else if (status.Outdated.Count > 0)
            {
                var n = status.Outdated.Count;
                state = $"{n} update{(n == 1 ? "" : "s")}";
                stateBrush = "AccentAmberBrush";
                detail = $"{n} optional package update{(n == 1 ? "" : "s")}: " +
                         string.Join(", ", status.Outdated.Take(3)) + (n > 3 ? ", …" : "");
            }
            else if (status.CheckError != null)
            {
                state = "installed";
                stateBrush = "MutedForegroundBrush";
                detail = "Online update check failed: " + status.CheckError;
            }
            else
            {
                state = "up to date";
                stateBrush = "PositiveGreenBrush";
            }

            var panel = new StackPanel { Margin = new Thickness(0, 0, 0, 6) };
            var header = new Grid();
            header.Children.Add(new TextBlock
            {
                Text = status.Name,
                FontSize = 11.5,
                Foreground = PythonEnvBrush("SecondaryForegroundBrush"),
            });
            header.Children.Add(new TextBlock
            {
                Text = state,
                FontSize = 11,
                FontFamily = new FontFamily("Consolas"),
                Foreground = PythonEnvBrush(stateBrush),
                HorizontalAlignment = HorizontalAlignment.Right,
            });
            panel.Children.Add(header);

            if (detail != null || status.LastResult != null)
            {
                var text = new TextBlock
                {
                    FontSize = 10.5,
                    TextWrapping = TextWrapping.Wrap,
                    Foreground = PythonEnvBrush("MutedForegroundBrush"),
                    Margin = new Thickness(0, 2, 0, 0),
                };
                if (detail != null) text.Inlines.Add(new Run(detail));
                if (status.LastResult != null)
                {
                    if (detail != null) text.Inlines.Add(new LineBreak());
                    var skipped = status.LastResult.StartsWith("skipped", StringComparison.OrdinalIgnoreCase);
                    text.Inlines.Add(new Run("Last update: " + status.LastResult)
                    {
                        Foreground = PythonEnvBrush(!status.LastResultFailed ? "PositiveGreenBrush"
                                                    : skipped ? "CautionBrush" : "AccentRedBrush"),
                    });
                    // Stage 10 lock (LOCK_API, make_lockfile exit 5): the optional broker set gets its own line; the main
                    // update above still counts as done.
                    if (status.LastResultNote != null)
                    {
                        text.Inlines.Add(new LineBreak());
                        text.Inlines.Add(new Run(status.LastResultNote) { Foreground = PythonEnvBrush("CautionBrush") });
                    }
                }
                panel.Children.Add(text);
            }
            // Packages the lock holds back on purpose for ccxt ("# held-by-broker:") are not counted as updates — named here.
            if (status.HeldBack.Count > 0)
                panel.ToolTip = "Held back for ccxt (the Binance demo broker package pins them exactly): " + string.Join(", ", status.HeldBack);
            return panel;
        }

        private Brush PythonEnvBrush(string key) => TryFindResource(key) as Brush ?? Brushes.Gray;

        private void ShowPythonEnvResult(string text)
        {
            PythonEnvResultText.Text = text;
            PythonEnvResultText.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
        }

        private async void UpdatePythonPackagesButton_Click(object sender, RoutedEventArgs e)
        {
            if (PythonEnvManager.IsUpdating) return;
            UpdatePythonPackagesButton.IsEnabled = false;
            ShowPythonEnvResult("Starting…");
            try
            {
                await Task.Run(() => PythonEnvManager.UpdateAllAsync(
                    message => Dispatcher.BeginInvoke(new Action(() => ShowPythonEnvResult(message)))));
                ShowPythonEnvResult("Done — results per module above. Backends that were stopped are not restarted here: " +
                                    "each module starts its backend again the next time it needs it (e.g. when you open its window). " +
                                    "Full output: %LOCALAPPDATA%\\UFOS.ai\\python-env\\update.log");
            }
            catch (Exception ex)
            {
                ShowPythonEnvResult("Update failed: " + ex.Message);
                try { AppLog.Error(PythonEnvLogModule, "Update Python packages failed.", ex); } catch { }
            }
            finally
            {
                RenderPythonEnvStatus();
            }
        }

        private void ShowUfoCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            // IsChecked="True" in XAML fires this Checked event during
            // InitializeComponent, before every named element below it in the
            // document has been connected - skip until the window has actually
            // loaded, since the XAML-default visibility already matches anyway.
            if (!IsLoaded) return;

            bool show = ShowUfoCheckBox.IsChecked == true;
            UfoOverlayCanvas.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        }

        private void ShowVerseCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            if (!IsLoaded) return;

            bool show = ShowVerseCheckBox.IsChecked == true;

            // Removing (not just hiding) Verse from the UniformGrid's Children - and
            // dropping Columns to 4 - makes the remaining cards divide the row exactly
            // the way the module grid below divides its own row into 4 columns, so the
            // two rows' cards line up instead of leaving a gap or using mismatched widths.
            if (show)
            {
                if (!OverviewGrid.Children.Contains(VerseCard))
                {
                    OverviewGrid.Children.Insert(3, VerseCard);
                }
                VerseCard.Visibility = Visibility.Visible;
                OverviewGrid.Columns = 5;
            }
            else
            {
                OverviewGrid.Children.Remove(VerseCard);
                OverviewGrid.Columns = 4;
            }
        }

        // ===================== Add-module tile (dims at rest, lights up on hover) =====================

        private void AddModuleButton_MouseEnter(object sender, MouseEventArgs e)
        {
            AddModuleButton.Opacity = 1.0;
        }

        private void AddModuleButton_MouseLeave(object sender, MouseEventArgs e)
        {
            AddModuleButton.Opacity = 0.5;
        }

        // ===================== Module launch handlers =====================

        // Disables the tile for as long as its window is open - the window is a
        // one-off per module (no docs/tabs), so a second instance would just be a
        // confusing duplicate - and re-enables it as soon as that window closes.
        private void OpenModuleWindow(Button tile, Func<Window?> createAndShow)
        {
            tile.IsEnabled = false;
            try
            {
                var window = createAndShow();
                if (window is null)
                {
                    tile.IsEnabled = true;
                    return;
                }

                window.Closed += (_, __) => tile.IsEnabled = true;
            }
            catch (Exception ex)
            {
                tile.IsEnabled = true;
                (Application.Current as IUiErrorReporter)?.ShowUiException(ex);
            }
        }

        private void OpenLiveStreamAgent_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(LiveStreamAgentTile, () =>
            {
                var window = new LivestreamAgent();
                window.Show();
                return window;
            });
        }

        private void OpenHedgeFund_Click(object sender, RoutedEventArgs e)
        {
            // Live click-test 2026-10-22: a second click on the tile brings the open Hedge Fund window back (a minimized window
            // returns to its last state, then it is activated) — same path as the Portfolio Insight card. The tile stays enabled.
            // review 2026-10-22: the same filter as the insight card — a closing window is not reused, a new one opens below
            var open = Application.Current?.Windows.OfType<HedgeFundWindow>().FirstOrDefault(w => w.IsLoaded && !w.IsClosingOrClosed);
            if (open != null)
            {
                open.BringToFront();
                return;
            }
            OpenModuleWindow(HedgeFundTile, () =>
            {
                var window = new HedgeFundWindow();
                window.Show();
                return window;
            });
            HedgeFundTile.IsEnabled = true;
        }

        // Beispiel für neue Module: ein Button reicht, um das zentrale, globale Log-Fenster
        // zu öffnen. Es zeigt automatisch die Log-Einträge aller Module an, die über
        // UFOS.ai.Logging.AppLog protokollieren.
        private void OpenLogs_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(SystemLogsTile, () => BackendLogsWindow.Show(this));
        }

        // Transaction Log: costs of every AI request across all modules (shared ledger
        // %LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl, see Services/AiCostLedger.cs).
        private void OpenTransactionLog_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(TransactionLogTile, () =>
            {
                var window = new TransactionLogWindow();
                window.Show();
                return window;
            });
        }

        private void OpenGlobalMonitoring_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(GlobalMonitoringTile, () =>
            {
                var window = new GlobalMonitoringWindow();
                window.Show();
                return window;
            });
        }

        // Sentiment Optimization and Trading Strategy Discovery don't have a project/
        // window yet (no .csproj / ProjectReference wired up). Their tiles are shown
        // for real (Idle status) so the intended module layout is visible, but the
        // click is illustrative-only until those modules actually exist.
        private void OpenSentimentOptimization_Click(object sender, RoutedEventArgs e)
        {
            ShowLaunchCaption("Sentiment Optimization", "not available yet (illustration only)");
        }

        private void OpenTradingStrategyDiscovery_Click(object sender, RoutedEventArgs e)
        {
            ShowLaunchCaption("Strategy Discovery", "not available yet (illustration only)");
        }

        private void AddModule_Click(object sender, RoutedEventArgs e)
        {
            ShowLaunchCaption("Add module", "not wired up yet (illustration only)");
        }

        private void ShowLaunchCaption(string moduleName, string suffix)
        {
            LaunchCaptionText.Inlines.Clear();
            LaunchCaptionText.Inlines.Add(new Run("→ "));
            LaunchCaptionText.Inlines.Add(new Run(moduleName) { FontWeight = FontWeights.Bold });
            LaunchCaptionText.Inlines.Add(new Run(" — " + suffix));

            LaunchCaption.BeginAnimation(UIElement.OpacityProperty, null);
            LaunchCaption.Opacity = 0;

            var fadeIn = new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(200));
            LaunchCaption.BeginAnimation(UIElement.OpacityProperty, fadeIn);

            _launchCaptionTimer?.Stop();
            _launchCaptionTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(2200) };
            _launchCaptionTimer.Tick += (_, __) =>
            {
                var fadeOut = new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(300));
                LaunchCaption.BeginAnimation(UIElement.OpacityProperty, fadeOut);
                _launchCaptionTimer?.Stop();
            };
            _launchCaptionTimer.Start();
        }
    }
}

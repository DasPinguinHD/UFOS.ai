using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// Market Research section (MR1 shell + MR3 chart; plan docs/market-research-plan.md §2.2, contract
    /// backend/market_research/SPEC.md §1, §4.3.1, "MR3 bridge contract v1", "MR3b notes"). One instance per Hedge
    /// Fund window; HedgeFundWindow hosts it in the rail section or moves it into <see cref="ResearchPopoutWindow"/>
    /// (<see cref="PopOutToggleRequested"/>).
    ///
    /// Live data: the ONE app-wide <see cref="ResearchSocket"/> runs while this view is shown (visible, host not
    /// minimized); while it is connected the feed status arrives over it, while it is down
    /// GET /research/feeds/status is polled every 5 s (§4.3.1 "WS vs polling").
    ///
    /// MR4 symbol workspace (ResearchView.Workspace.cs: header, key stats, tabs + session restore, Ctrl+K palette;
    /// ResearchView.Watchlists.cs: watchlists): a symbol opened from the palette or a watchlist → a tab → bars for the
    /// selected timeframe/session (REST) into the <see cref="ChartHost"/>, live candle + trades over the socket, older
    /// history on the page's needMoreLeft; snapshot (header) and stats (key stats) per symbol.
    /// </summary>
    public partial class ResearchView : UserControl
    {
        private const string LogModule = "HedgeFund";
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(5); // SPEC §1.5 / §4.4 STATUS_POLL_SECONDS
        private static readonly TimeSpan MarketRefreshInterval = TimeSpan.FromSeconds(60);
        private const int MaxTabs = 12; // SPEC §4.4 MAX_TABS
        private static readonly Regex TickerInput = new(@"^[A-Za-z0-9 .\-/]{1,32}$", RegexOptions.CultureInvariant);
        private static readonly Regex PlatformSymbol = new(@"^[A-Z0-9][A-Z0-9./\-]{0,31}$", RegexOptions.CultureInvariant);
        private static readonly HashSet<string> IntradayTfs = new(StringComparer.Ordinal) { "1m", "5m", "15m", "1H", "4H" };

        private readonly DispatcherTimer _pollTimer;
        private readonly DispatcherTimer _marketTimer;
        private readonly DispatcherTimer _countdownTimer; // "next try in s s" while the socket waits
        private readonly ResearchSocket _socket = ResearchSocket.Instance;
        private readonly ChartHost _chart;
        private bool _loading;
        private bool _pollAgain;
        private bool _shutDown;
        private bool _isPoppedOut;
        // The window currently hosting this view (HedgeFundWindow or the pop-out); its StateChanged is hooked so a
        // minimized host stops the poll and the socket (IsVisible stays true while minimized). Re-evaluated on every re-parenting.
        private Window? _hostWindow;
        private FeedsStatus? _lastFeeds;
        private bool _wasShown;

        // ---------- symbol tabs / chart (MR3) ----------
        private readonly List<ResearchTab> _tabs = new();
        private ResearchTab? _active;
        private int _loadGeneration;
        private bool _paging;
        private bool _syncingUi;
        private DateTime? _chartLoadStartedUtc;
        private double? _prevClose;
        private double? _lastTrade;
        private bool? _symbolLive;        // symbol_status for the active symbol (null = not reported yet)
        private string? _symbolReason;
        private string? _candleError;     // candle_unavailable for the active topic
        private MarketPart? _market;
        private string? _marketError;

        /// <summary>One open symbol (MR4: saved with PUT /research/session, restored on start).</summary>
        private sealed class ResearchTab
        {
            public string Symbol = "";
            public string? Name;
            public string AssetClass = "equity"; // equity · crypto
            public string? Kind;
            public string? Exchange;
            public string Tf = "1D";
            public string ChartType = "candles";
            public string Session = "rth";       // equities: rth · eth
            /// <summary>Restored from /research/session: its type/session win over the symbol's chart settings (MR5).</summary>
            public bool FromSession;

            public bool IsCrypto => AssetClass == "crypto";
            /// <summary>The REST `session` parameter (the backend answers 400 when it is given for crypto).</summary>
            public string? SessionParam => IsCrypto ? null : Session;
            /// <summary>The session the REST /bars echoes and the candle topic uses (`utc` for crypto, MR2b notes).</summary>
            public string TopicSession => IsCrypto ? "utc" : Session;
        }

        /// <summary>Raised by the "⧉ Pop out" / "Return" button; HedgeFundWindow moves the view.</summary>
        public event EventHandler? PopOutToggleRequested;

        private const double SymbolChartRowMin = 430;   // = RowDefinition MinHeight in ResearchView.xaml (card 420 + margin)
        private const double SymbolNewsRowMin = 120;    // = RowDefinition MinHeight in ResearchView.xaml

        public ResearchView()
        {
            InitializeComponent();
            _pollTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = PollInterval };
            _pollTimer.Tick += async (_, _) => await PollAsync();
            _marketTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = MarketRefreshInterval };
            _marketTimer.Tick += async (_, _) =>
            {
                await RefreshMarketAsync();
                await RefreshSnapshotAsync(); // header values every 60 s while shown (stats every 5 min)
            };
            _countdownTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromSeconds(1) };
            _countdownTimer.Tick += (_, _) => RenderLiveState();

            _chart = new ChartHost { Visibility = Visibility.Collapsed };
            ChartSlot.Child = _chart;
            // live retest 2026-10-18: header + chart card (row min 430) + news (row min 120) — below that the symbol page scrolls
            SymbolHeaderCard.SizeChanged += (_, _) => SymbolPageGrid.MinHeight = Math.Ceiling(SymbolHeaderCard.ActualHeight) + SymbolChartRowMin + SymbolNewsRowMin;
            _chart.NeedMoreLeft += Chart_NeedMoreLeft;
            _chart.RetryRequested += (_, _) => { if (_active != null) _ = LoadChartAsync(_active, withSnapshot: true); };
            InitDrawingsAndIndicators(); // MR5: tool rail, objects panel, persistence, indicators (ResearchView.Drawings.cs)
            InitAlerts();                // MR7: bell editor, Alerts section, alert-line link, live alert log (ResearchView.Alerts.cs)

            _socket.TopicProvider = BuildTopics;
            _socket.StateChanged += Socket_StateChanged;
            _socket.FeedsReceived += Socket_FeedsReceived;
            _socket.CandleReceived += Socket_CandleReceived;
            _socket.SipReplaceReceived += Socket_SipReplaceReceived;
            _socket.TradeReceived += Socket_TradeReceived;
            _socket.SymbolStatusReceived += Socket_SymbolStatusReceived;
            _socket.ServerErrorReceived += Socket_ServerErrorReceived;
            _socket.Resynced += Socket_Resynced;
            _socket.NewsReceived += Socket_NewsReceived; // MR6

            IsVisibleChanged += OnIsVisibleChanged;
            Loaded += (_, _) => UpdatePolling();
            Unloaded += (_, _) => UpdatePolling(); // removed from a window (moved / closed): unhooks the old host
            _applyingSession = true; // the initial section is not a user change (no session save)
            NavSymbol.IsChecked = true; // raises Nav_Checked → shows the symbol workspace
            _applyingSession = false;
            RenderUnknownFeeds("CHECKING…", "Checking the feed status…");
            SyncToolbar();
            RenderTabs();
            RenderHeader(null);
            RenderWatchlists();
            ShowWatchlistMessage("Loading watchlists…", retry: false);
        }

        /// <summary>True while the view lives in the pop-out window (changes the pop-out button).</summary>
        public bool IsPoppedOut
        {
            get => _isPoppedOut;
            set
            {
                _isPoppedOut = value;
                if (value)
                {
                    PopOutButton.Content = "⧉ Return to Hedge Fund window";
                    PopOutButton.ToolTip = "Close this window and show Market Research in the Hedge Fund window again";
                    AutomationProperties.SetName(PopOutButton, "Return Market Research to the Hedge Fund window");
                }
                else
                {
                    PopOutButton.Content = "⧉ Pop out";
                    PopOutButton.ToolTip = "Open Market Research in its own window (e.g. on a second monitor)";
                    AutomationProperties.SetName(PopOutButton, "Pop out Market Research into its own window");
                }
            }
        }

        /// <summary>Stops polling and the live connection for good (the Hedge Fund window is closing).</summary>
        public void Shutdown()
        {
            FlushSessionSave(); // a pending tab change is sent before the view stops
            ShutdownIntegrations(); // MR8: portfolio refresh + the reused Newsroom views' timers
            ShutdownDrawings(); // MR5: flush the chart-settings save, report unsaved drawing changes
            _shutDown = true;
            _sessionSaveTimer?.Stop();
            _paletteDebounce?.Stop();
            _pollTimer.Stop();
            _marketTimer.Stop();
            _countdownTimer.Stop();
            HookHostWindow(null);
            _loadGeneration++;
            _socket.StateChanged -= Socket_StateChanged;
            _socket.FeedsReceived -= Socket_FeedsReceived;
            _socket.CandleReceived -= Socket_CandleReceived;
            _socket.SipReplaceReceived -= Socket_SipReplaceReceived;
            _socket.TradeReceived -= Socket_TradeReceived;
            _socket.SymbolStatusReceived -= Socket_SymbolStatusReceived;
            _socket.ServerErrorReceived -= Socket_ServerErrorReceived;
            _socket.Resynced -= Socket_Resynced;
            _socket.NewsReceived -= Socket_NewsReceived;
            _newsAgeTimer?.Stop();
            if (ReferenceEquals(_socket.TopicProvider?.Target, this)) _socket.TopicProvider = null;
            ShutdownAlerts();
            // MR7: not Shutdown() — the one socket stays open (visible: false) for alert toasts while alert rules are enabled
            // (ResearchAlertNotifier); without them it closes like before.
            _socket.SetWanted(false);
        }

        // ---------- visibility-driven polling + live connection ----------

        private void OnIsVisibleChanged(object sender, DependencyPropertyChangedEventArgs e) => UpdatePolling();

        private void HostWindow_StateChanged(object? sender, EventArgs e) => UpdatePolling();

        /// <summary>The view is on screen: visible AND its host window is not minimized (SPEC §1.5, §4.3.1).</summary>
        private bool IsShown => !_shutDown && IsVisible && _hostWindow != null && _hostWindow.WindowState != WindowState.Minimized;

        /// <summary>Polls only while shown AND the live connection is not up (§4.3.1 "WS vs polling").</summary>
        private bool ShouldPoll => IsShown && _socket.State != ResearchSocketState.Connected;

        private void HookHostWindow(Window? window)
        {
            if (ReferenceEquals(window, _hostWindow)) return;
            if (_hostWindow != null) _hostWindow.StateChanged -= HostWindow_StateChanged;
            _hostWindow = window;
            if (_hostWindow != null) _hostWindow.StateChanged += HostWindow_StateChanged;
        }

        // Called on visibility changes, Loaded/Unloaded (re-parenting between HedgeFundWindow and the pop-out), the
        // host window's StateChanged and socket state changes: re-evaluates the host, then the socket demand, the
        // status poll (with an immediate poll when it starts) and the market-status refresh.
        private void UpdatePolling()
        {
            HookHostWindow(_shutDown ? null : Window.GetWindow(this));
            var wasShown = _wasShown;
            _wasShown = IsShown;
            if (!_shutDown) _socket.SetWanted(IsShown);
            if (IsShown && !wasShown)
            {
                EnsureWorkspaceLoaded(); // MR4: session restore + watchlists on first show (retried while failed)
                if (_listsLoad == LoadState.Loaded) RequestAllQuotes();
                // Review L4: while hidden the socket may stay open for alerts with only `alerts` subscribed — no re-sync
                // follows, so the open chart and the news re-fetch what they missed (a reconnect re-syncs by itself).
                if (_socket.State == ResearchSocketState.Connected && _active != null && _chart.HasSeries)
                {
                    _ = LoadChartAsync(_active, withSnapshot: true);
                    ReloadNews();
                }
            }
            if (ShouldPoll)
            {
                if (!_pollTimer.IsEnabled)
                {
                    _pollTimer.Start();
                    _ = PollAsync(); // first poll immediately on show / restore / socket loss
                }
            }
            else
            {
                // Hidden (other rail entry, being moved between windows), host minimized, or the socket delivers the status.
                _pollTimer.Stop();
            }
            if (IsShown && _active != null)
            {
                if (!_marketTimer.IsEnabled) _marketTimer.Start();
            }
            else
            {
                _marketTimer.Stop();
            }
            var waiting = IsShown && (_socket.State == ResearchSocketState.Waiting || _socket.State == ResearchSocketState.Refused);
            if (waiting) { if (!_countdownTimer.IsEnabled) _countdownTimer.Start(); }
            else _countdownTimer.Stop();
        }

        private async Task PollAsync()
        {
            if (!ShouldPoll) return;
            if (_loading)
            {
                _pollAgain = true; // e.g. Retry pressed while a poll is in flight: run once more afterwards
                return;
            }
            _loading = true;
            try
            {
                do
                {
                    _pollAgain = false;
                    var result = await ResearchClient.GetFeedsStatusAsync();
                    if (_shutDown) return;
                    // A poll that finishes after the socket connected must not overwrite the live status.
                    if (_socket.State == ResearchSocketState.Connected) return;
                    Render(result);
                } while (_pollAgain && ShouldPoll);
            }
            finally
            {
                _loading = false;
            }
        }

        // ---------- rendering of the feed status (SPEC §1.3 / §1.5 / §4.3.1) ----------

        private void Render(PlatformResult<FeedsStatus> result)
        {
            if (!result.Ok)
            {
                // Backend not reachable / token rejected / Research failed to load / old backend … (SPEC §1.3).
                // The last known feed status is NOT kept on screen (it would look current).
                _lastFeeds = null;
                ShowBanner(result.Message, error: true, retry: true);
                RenderUnknownFeeds("UNKNOWN", "Feed status unknown: " + result.Message);
                RenderLiveState();
                return;
            }
            var status = result.Value;
            if (status == null)
            {
                const string empty = "Research unavailable: empty response from the backend.";
                _lastFeeds = null;
                ShowBanner(empty, error: true, retry: true);
                RenderUnknownFeeds("UNKNOWN", "Feed status unknown: " + empty);
                RenderLiveState();
                return;
            }
            RenderStatus(status);
        }

        private void RenderStatus(FeedsStatus status)
        {
            _lastFeeds = status;
            var state = status.Research?.State;
            var reason = status.Research?.Reason;
            switch (state)
            {
                case "ok":
                    HideBanner();
                    EnsureWorkspaceLoaded(); // a session/watchlist load that failed earlier is retried (≥ 30 s apart)
                    break;
                case "starting":
                    ShowBanner("Research is starting…", error: false, retry: false);
                    break;
                case "unavailable":
                    ShowBanner("Research unavailable: " + (string.IsNullOrWhiteSpace(reason) ? "no reason was reported." : reason),
                               error: true, retry: false);
                    break;
                case "stopped":
                    ShowBanner("Research stopped: the HedgeFund backend is shutting down.", error: true, retry: false);
                    break;
                default:
                    ShowBanner("Research reported an unknown state (" + (state ?? "none") + ")"
                               + (string.IsNullOrWhiteSpace(reason) ? "." : ": " + reason),
                               error: true, retry: false);
                    break;
            }
            RenderFeeds(status);
            RenderLiveState();
        }

        private void ShowBanner(string text, bool error, bool retry)
        {
            StatusBannerText.Text = text;
            if (error)
            {
                StatusBanner.Background = new SolidColorBrush(Color.FromArgb(0x14, 0xED, 0x6A, 0x5A));
                StatusBanner.BorderBrush = new SolidColorBrush(Color.FromArgb(0x66, 0xED, 0x6A, 0x5A));
            }
            else
            {
                StatusBanner.Background = new SolidColorBrush(Color.FromArgb(0x14, 0xF0, 0xB4, 0x29));
                StatusBanner.BorderBrush = new SolidColorBrush(Color.FromArgb(0x66, 0xF0, 0xB4, 0x29));
            }
            AutomationProperties.SetName(StatusBanner, text);
            RetryButton.Visibility = retry ? Visibility.Visible : Visibility.Collapsed;
            StatusBanner.Visibility = Visibility.Visible;
        }

        private void HideBanner()
        {
            StatusBanner.Visibility = Visibility.Collapsed;
            RetryButton.Visibility = Visibility.Collapsed;
        }

        /// <summary>No usable payload: no dots, a neutral summary word, the reason in the tooltip.</summary>
        private void RenderUnknownFeeds(string summary, string tooltip)
        {
            FeedDots.Children.Clear();
            FeedSummaryText.Text = summary;
            FeedSummaryText.Foreground = Token("MutedForegroundBrush");
            FeedStatusRow.ToolTip = tooltip;
            AutomationProperties.SetName(FeedStatusRow, tooltip);
            FeedReasonsText.Text = "";
            FeedReasonsText.Visibility = Visibility.Collapsed;
            DemandNoteText.Text = "";
            DemandNoteText.Visibility = Visibility.Collapsed;
            BudgetText.Text = "";
            BudgetText.Visibility = Visibility.Collapsed;
            _baseSummaryBrush = "MutedForegroundBrush";
            RememberStatusBarBase();
            ApplySocketToStatusBar();
        }

        private void RenderFeeds(FeedsStatus status)
        {
            var feeds = status.Feeds ?? new List<FeedInfo>();

            FeedDots.Children.Clear();
            foreach (var feed in feeds)
            {
                var description = feed.DescriptionText;
                var dot = new Ellipse
                {
                    Width = 7,
                    Height = 7,
                    Margin = new Thickness(0, 0, 3, 0),
                    VerticalAlignment = VerticalAlignment.Center,
                    Fill = StateBrush(feed.State),
                    ToolTip = description,
                };
                AutomationProperties.SetName(dot, description);
                FeedDots.Children.Add(dot);
            }

            // "LIVE · n/m" when any feed is live; otherwise UNAVAILABLE > WAITING > CONNECTING > OFF (SPEC §1.5).
            var live = status.LiveCount;
            var count = status.FeedCount;
            var counts = (live?.ToString(CultureInfo.InvariantCulture) ?? "?") + "/" + (count?.ToString(CultureInfo.InvariantCulture) ?? "?");
            string word;
            string brushKey;
            if (live is > 0)
            {
                word = "LIVE";
                brushKey = "PositiveGreenBrush";
            }
            else if (feeds.Any(f => f.State == "unavailable"))
            {
                word = "UNAVAILABLE";
                brushKey = "AccentRedBrush";
            }
            else if (feeds.Any(f => f.State == "waiting"))
            {
                word = "WAITING";
                brushKey = "CautionBrush";
            }
            else if (feeds.Any(f => f.State == "connecting"))
            {
                word = "CONNECTING";
                brushKey = "CautionBrush";
            }
            else
            {
                word = "OFF";
                brushKey = "MutedForegroundBrush";
            }
            FeedSummaryText.Text = word + " · " + counts;
            FeedSummaryText.Foreground = Token(brushKey);

            var note = status.Demand?.Note;
            var lines = feeds.Select(f => f.DescriptionText).ToList();
            if (lines.Count == 0) lines.Add("The backend reported no feeds.");
            if (!string.IsNullOrWhiteSpace(note)) lines.Add("\n" + note);
            var tooltip = string.Join("\n", lines);
            FeedStatusRow.ToolTip = tooltip;
            AutomationProperties.SetName(FeedStatusRow, "Feeds " + word + " " + counts + ". " + string.Join(" ", feeds.Select(f => f.DescriptionText)));

            // Feeds that need attention: their reasons verbatim, visible without hovering.
            var problems = feeds.Where(f => f.State == "unavailable" || f.State == "waiting").Select(f => f.DescriptionText).ToList();
            FeedReasonsText.Text = string.Join("\n", problems);
            FeedReasonsText.Visibility = problems.Count > 0 ? Visibility.Visible : Visibility.Collapsed;

            DemandNoteText.Text = note ?? "";
            DemandNoteText.Visibility = string.IsNullOrWhiteSpace(note) ? Visibility.Collapsed : Visibility.Visible;
            // §8: "Waiting for Alpaca rate limit (n s)" — Research shares the Alpaca budget with trading (Q3).
            if (status.Budget?.WaitingSeconds is double wait && wait > 0)
            {
                BudgetText.Text = "Waiting for the Alpaca rate limit (" + Math.Ceiling(wait).ToString(CultureInfo.InvariantCulture) +
                                  " s) — shared with trading, which goes first.";
                BudgetText.Visibility = Visibility.Visible;
            }
            else
            {
                BudgetText.Text = "";
                BudgetText.Visibility = Visibility.Collapsed;
            }
            _baseSummaryBrush = brushKey;
            RememberStatusBarBase();
            ApplySocketToStatusBar();
        }

        // The feed-status texts as rendered from the last payload (or the unknown state); the socket state is
        // layered on top of them by ApplySocketToStatusBar (re-applied every second for the countdown).
        private string _baseSummary = "CHECKING…";
        private string _baseSummaryBrush = "MutedForegroundBrush";
        private string _baseTooltip = "";
        private string _baseReasons = "";

        private void RememberStatusBarBase()
        {
            _baseSummary = FeedSummaryText.Text;
            _baseTooltip = FeedStatusRow.ToolTip as string ?? "";
            _baseReasons = FeedReasonsText.Text;
        }

        /// <summary>
        /// §4.3.1: while the live connection to the backend is down the status bar reads "WAITING · attempt n" and
        /// the tooltip "Live connection to the backend lost — waiting (attempt n, next try in s s)."; a refused
        /// connection shows its reason ("NOT LIVE"). The feed dots keep coming from the 5 s poll meanwhile.
        /// </summary>
        private void ApplySocketToStatusBar()
        {
            var state = _socket.State;
            if (state != ResearchSocketState.Waiting && state != ResearchSocketState.Refused)
            {
                FeedSummaryText.Text = _baseSummary;
                FeedSummaryText.Foreground = Token(_baseSummaryBrush);
                FeedStatusRow.ToolTip = _baseTooltip;
                FeedReasonsText.Text = _baseReasons;
                FeedReasonsText.Visibility = string.IsNullOrEmpty(_baseReasons) ? Visibility.Collapsed : Visibility.Visible;
                return;
            }
            var description = _socket.DescribeState();
            if (state == ResearchSocketState.Waiting)
            {
                FeedSummaryText.Text = "WAITING · attempt " + _socket.Attempt.ToString(CultureInfo.InvariantCulture);
                FeedSummaryText.Foreground = Token("CautionBrush");
            }
            else
            {
                FeedSummaryText.Text = "NOT LIVE";
                FeedSummaryText.Foreground = Token("AccentRedBrush");
            }
            FeedStatusRow.ToolTip = string.IsNullOrEmpty(_baseTooltip) ? description : description + "\n\n" + _baseTooltip;
            AutomationProperties.SetName(FeedStatusRow, description);
            FeedReasonsText.Text = string.IsNullOrEmpty(_baseReasons) ? description : description + "\n" + _baseReasons;
            FeedReasonsText.Visibility = Visibility.Visible;
        }

        private Brush StateBrush(string? state) => state switch
        {
            "live" => Token("PositiveGreenBrush"),
            "connecting" or "waiting" => Token("CautionBrush"),
            "unavailable" => Token("AccentRedBrush"),
            _ => Token("MutedForegroundBrush"), // "off" and anything not in the contract
        };

        private Brush Token(string key) => (Brush)Resources[key];

        // =====================================================================================
        // Tabs (MR4: opened from the palette / a watchlist; saved + restored via /research/session)
        // =====================================================================================

        private void Activate(ResearchTab tab)
        {
            _active = tab;
            _prevClose = null;
            _lastTrade = null;
            _symbolLive = null;
            _symbolReason = null;
            _candleError = null;
            _market = null;
            _marketError = null;
            ResetSymbolDetails();
            if (!_workspaceMessageIsSaveError) HideWorkspaceMessage();
            NavSymbol.IsChecked = true;
            RenderTabs();
            SyncToolbar();
            ChartEmptyPanel.Visibility = Visibility.Collapsed;
            _chart.Visibility = Visibility.Visible;
            RenderHeader(null);
            RenderWatchlistSelection();
            _socket.TopicsChanged();
            UpdatePolling();
            OnSymbolActivated(tab); // MR5: chart settings (indicators, magnet; type/session for new tabs)
            _ = LoadChartAsync(tab, withSnapshot: true);
            _ = LoadStatsAsync(tab);
            PostRecent(tab.Symbol);
            ScheduleSessionSave();
        }

        private void CloseTab(ResearchTab tab)
        {
            var index = _tabs.IndexOf(tab);
            if (index < 0) return;
            _tabs.RemoveAt(index);
            if (!ReferenceEquals(tab, _active))
            {
                RenderTabs();
                ScheduleSessionSave();
                return;
            }
            if (_tabs.Count > 0)
            {
                Activate(_tabs[Math.Min(index, _tabs.Count - 1)]);
                return;
            }
            // No symbol open any more.
            _active = null;
            OnNoSymbol(); // MR5: tool rail / objects panel off
            _loadGeneration++;
            _paging = false;
            ResetSymbolDetails();
            _chart.Clear();
            _chart.HideMessage();
            _chart.Visibility = Visibility.Collapsed;
            ChartEmptyPanel.Visibility = Visibility.Visible;
            RenderTabs();
            SyncToolbar();
            RenderHeader(null);
            RenderWatchlistSelection();
            _socket.TopicsChanged();
            UpdatePolling();
            ScheduleSessionSave();
        }

        private void RenderTabs()
        {
            TabsPanel.Children.Clear();
            foreach (var tab in _tabs)
            {
                var isActive = ReferenceEquals(tab, _active);
                var open = new Button
                {
                    Content = tab.Symbol,
                    FontFamily = new FontFamily("Consolas"),
                    Padding = new Thickness(10, 3, 8, 3),
                    ToolTip = (tab.Name ?? tab.Symbol) + (tab.Exchange != null ? " · " + tab.Exchange : "") + " · " + tab.Tf,
                };
                open.SetResourceReference(StyleProperty, "RoundedDarkButtonStyle");
                if (isActive)
                {
                    open.BorderBrush = Token("FocusBrush");
                    open.Foreground = new SolidColorBrush(Color.FromRgb(0xE6, 0xE6, 0xE6));
                }
                AutomationProperties.SetName(open, "Tab " + tab.Symbol + (isActive ? " (selected)" : "") + ", " + (_tabs.IndexOf(tab) + 1) + " of " + _tabs.Count);
                var target = tab;
                open.Click += (_, _) => { if (!ReferenceEquals(target, _active)) Activate(target); };

                var close = new Button { Content = "×", Padding = new Thickness(6, 3, 6, 3), Margin = new Thickness(2, 0, 0, 0), ToolTip = "Close " + tab.Symbol };
                close.SetResourceReference(StyleProperty, "RoundedDarkButtonStyle");
                AutomationProperties.SetName(close, "Close " + tab.Symbol);
                close.Click += (_, _) => CloseTab(target);

                var pair = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 8, 0) };
                pair.Children.Add(open);
                pair.Children.Add(close);
                TabsPanel.Children.Add(pair);
            }
            TabsEmptyText.Visibility = _tabs.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
            TabsEmptyText.Text = "No symbol open — press Ctrl+K or +";
        }

        /// <summary>Toolbar = the active tab's timeframe / type / session (no reload while syncing).</summary>
        private void SyncToolbar()
        {
            _syncingUi = true;
            try
            {
                var tab = _active;
                ChartToolbar.IsEnabled = tab != null;
                foreach (var rb in new[] { Tf1m, Tf5m, Tf15m, Tf1H, Tf4H, Tf1D, Tf1W })
                    rb.IsChecked = tab != null && (string)rb.Tag == tab.Tf;
                foreach (var rb in new[] { TypeCandles, TypeLine, TypeArea })
                    rb.IsChecked = tab != null && (string)rb.Tag == tab.ChartType;
                var equity = tab != null && !tab.IsCrypto;
                ExtendedHoursToggle.Visibility = equity ? Visibility.Visible : Visibility.Collapsed;
                ExtendedHoursToggle.IsChecked = equity && tab!.Session == "eth";
                ExtendedHoursToggle.Content = ExtendedHoursToggle.IsChecked == true ? "EXTENDED HOURS" : "RTH";
            }
            finally
            {
                _syncingUi = false;
            }
        }

        private void Timeframe_Checked(object sender, RoutedEventArgs e)
        {
            if (_syncingUi || _active == null || sender is not RadioButton rb || rb.Tag is not string tf || tf == _active.Tf) return;
            _active.Tf = tf;
            _candleError = null;
            _socket.TopicsChanged();
            _ = LoadChartAsync(_active, withSnapshot: false);
            RenderTabs();
            ScheduleSessionSave();
        }

        private void ChartType_Checked(object sender, RoutedEventArgs e)
        {
            if (_syncingUi || _active == null || sender is not RadioButton rb || rb.Tag is not string type) return;
            if (_active.ChartType == type) return;
            _active.ChartType = type;
            _chart.SetChartType(type);
            ScheduleSessionSave();
            ScheduleChartSettingsSave();
        }

        private void ExtendedHours_Changed(object sender, RoutedEventArgs e)
        {
            if (_syncingUi || _active == null || _active.IsCrypto) return;
            var session = ExtendedHoursToggle.IsChecked == true ? "eth" : "rth";
            ExtendedHoursToggle.Content = session == "eth" ? "EXTENDED HOURS" : "RTH";
            if (session == _active.Session) return;
            _active.Session = session;
            _candleError = null;
            _socket.TopicsChanged();
            _ = LoadChartAsync(_active, withSnapshot: false);
            ScheduleSessionSave();
            ScheduleChartSettingsSave();
        }

        // =====================================================================================
        // MR3: chart data (REST) — loading, paging, header
        // =====================================================================================

        /// <summary>
        /// Loads the initial window of bars (and, with <paramref name="withSnapshot"/>, the header snapshot) for
        /// <paramref name="tab"/>. Visible states: "Loading …" over the chart, the backend's reason on failure
        /// (feed unavailable, SIP plan, keys missing, not covered …) with Retry — never older or other data.
        /// </summary>
        private async Task LoadChartAsync(ResearchTab tab, bool withSnapshot)
        {
            var generation = ++_loadGeneration;
            _paging = false;
            _chartLoadStartedUtc = DateTime.UtcNow;
            HideChartNote();
            _chart.SetChartType(tab.ChartType);
            _chart.ShowMessage("Loading " + tab.Symbol + " · " + tab.Tf + "…",
                               tab.IsCrypto ? "Alpaca crypto · UTC days" : (tab.Session == "eth" ? "Extended hours (04:00–20:00 ET)" : "Regular trading hours (09:30–16:00 ET)"));
            try
            {
                var barsTask = ResearchClient.GetBarsAsync(tab.Symbol, tab.Tf, tab.SessionParam, null, null);
                var snapshotTask = withSnapshot ? ResearchClient.GetSnapshotAsync(tab.Symbol) : null;

                var bars = await barsTask;
                if (generation != _loadGeneration || _shutDown) return;
                if (!bars.Ok || bars.Value == null)
                {
                    _chart.Clear();
                    _chart.ShowMessage("Chart unavailable for " + tab.Symbol + " · " + tab.Tf,
                                       bars.Ok ? "Empty response from the backend." : bars.Message, retry: true);
                    UpdateFeedChips(null, tab);
                }
                else
                {
                    var r = bars.Value;
                    var session = r.Session is "rth" or "eth" or "utc" ? r.Session! : tab.TopicSession;
                    var exchangeTz = r.Tz == "UTC" ? "UTC" : "America/New_York";
                    var dropped = _chart.LoadBars(tab.Symbol, tab.Tf, session, tab.IsCrypto ? "crypto" : "equity", exchangeTz,
                                                  r.Bars, r.HasMoreLeft == true, r.SipThrough);
                    if (_chart.BarCount == 0)
                        _chart.ShowMessage("No candles for " + tab.Symbol + " · " + tab.Tf + " in the loaded range.",
                                           tab.IsCrypto || tab.Session == "eth" ? null : "Try another timeframe or extended hours.", retry: true);
                    else
                        _chart.HideMessage();
                    if (dropped > 0) ShowChartNote(dropped + " invalid candle(s) from the backend were not drawn (see the Backend Log).");
                    UpdateFeedChips(r, tab);
                    OnChartSeriesLoaded(tab, generation); // MR5: drawings (new symbol) + indicators for this window
                    OnChartBarsLoadedMarkers(tab);        // MR8: chart markers (cleared, then re-loaded when on)
                    ApplyLastPrice();
                }

                if (snapshotTask != null)
                {
                    var snapshot = await snapshotTask;
                    if (generation != _loadGeneration || _shutDown) return;
                    if (snapshot.Ok && snapshot.Value != null)
                    {
                        var s = snapshot.Value;
                        _prevClose = s.PrevClose?.Value;
                        if (_lastTrade == null || !_lastTradeLive) _lastTrade = s.Last?.Value;
                        _market = s.Market;
                        _marketError = null;
                        RenderHeader(s);
                    }
                    else
                    {
                        _prevClose = null;
                        _marketError = snapshot.Message;
                        _snapshot = null;
                        _snapshotError = snapshot.Message;
                        RenderHeader(null);
                    }
                    ApplyLastPrice();
                }
                else
                {
                    RenderHeader(null);
                }
            }
            catch (Exception ex)
            {
                if (generation != _loadGeneration) return;
                AppLog.Warn(LogModule, "Market Research: loading the chart failed.", ex);
                _chart.ShowMessage("Chart unavailable for " + tab.Symbol, "Unexpected error — see the Backend Log.", retry: true);
            }
        }

        private async void Chart_NeedMoreLeft(object? sender, long oldestMs)
        {
            var tab = _active;
            if (tab == null || _paging || _shutDown) return;
            var generation = _loadGeneration;
            _paging = true;
            ShowChartNote("Loading older candles…");
            try
            {
                var result = await ResearchClient.GetBarsAsync(tab.Symbol, tab.Tf, tab.SessionParam, ChartHost.ToIso(oldestMs), null);
                if (generation != _loadGeneration || _shutDown) return;
                if (!result.Ok || result.Value == null)
                {
                    _chart.EndPaging();
                    ShowChartNote("Older history not loaded: " + (result.Ok ? "empty response from the backend." : result.Message));
                    return;
                }
                var withinLimit = _chart.PrependBars(result.Value.Bars, result.Value.HasMoreLeft == true);
                OnChartPagedLeft(tab, generation, ChartHost.ToIso(oldestMs)); // MR5: indicator values for the older page
                if (!withinLimit)
                    ShowChartNote("History limit reached: at most " + ChartHost.MaxBars.ToString("N0", CultureInfo.InvariantCulture) +
                                  " candles per chart — use a larger timeframe for older history.");
                else if (result.Value.HasMoreLeft != true && (tab.Tf == "1m" || tab.Tf == "5m" || tab.Tf == "15m"))
                    ShowChartNote("Start of the loaded history: intraday history in Research covers the last 30 days.");
                else
                    HideChartNote();
            }
            catch (Exception ex)
            {
                if (generation != _loadGeneration) return;
                AppLog.Warn(LogModule, "Market Research: loading older candles failed.", ex);
                _chart.EndPaging();
                ShowChartNote("Older history not loaded — see the Backend Log.");
            }
            finally
            {
                if (generation == _loadGeneration) _paging = false;
            }
        }

        private void ShowChartNote(string text)
        {
            ChartNoteText.Text = text;
            ChartNoteText.Visibility = Visibility.Visible;
            ChartChips.Visibility = Visibility.Visible;
        }

        private void HideChartNote()
        {
            ChartNoteText.Text = "";
            ChartNoteText.Visibility = Visibility.Collapsed;
            ChartChips.Visibility = SipChip.Visibility == Visibility.Visible || TailChip.Visibility == Visibility.Visible
                ? Visibility.Visible : Visibility.Collapsed;
        }

        /// <summary>Feed labels of the loaded candles (SPEC §7.1 model c): SIP history / IEX live tail / daily / crypto.</summary>
        private void UpdateFeedChips(BarsResponse? response, ResearchTab tab)
        {
            if (response == null)
            {
                SipChip.Visibility = Visibility.Collapsed;
                TailChip.Visibility = Visibility.Collapsed;
                ChartChips.Visibility = ChartNoteText.Visibility;
                return;
            }
            string sipText, sipTip;
            if (tab.IsCrypto)
            {
                sipText = "ALPACA CRYPTO · REAL TIME";
                sipTip = "Candles from Alpaca's crypto feed (UTC-aligned buckets, crypto days are UTC days).";
            }
            else if (IntradayTfs.Contains(tab.Tf))
            {
                var through = LocalTime(response.SipThrough);
                sipText = "SIP · 15 MIN DELAYED" + (through != null ? " · THROUGH " + through : "");
                sipTip = "History from SIP (all US exchanges), available 15 minutes after the fact" +
                         (through != null ? " — SIP candles up to " + through + " (local time)." : ".");
            }
            else
            {
                sipText = "SIP · DAILY BARS";
                sipTip = "Completed sessions from SIP daily bars (split- and dividend-adjusted); today's candle is built from today's minutes.";
            }
            SipChipText.Text = sipText;
            SipChip.ToolTip = sipTip;
            AutomationProperties.SetName(SipChip, sipText + ". " + sipTip);
            SipChip.Visibility = Visibility.Visible;
            RefreshTailChip(response.SipThrough);
            ChartChips.Visibility = Visibility.Visible;
        }

        private string? _tailSipThrough;

        private void RefreshTailChip(string? sipThroughIso = null)
        {
            if (sipThroughIso != null) _tailSipThrough = sipThroughIso;
            if (_active == null || _active.IsCrypto || !_chart.HasTailBars)
            {
                TailChip.Visibility = Visibility.Collapsed;
                return;
            }
            var since = LocalTime(_tailSipThrough);
            TailChipText.Text = "IEX · LIVE TAIL" + (since != null ? " FROM " + since : "");
            TailChip.ToolTip = "The newest candles (from " + (since ?? "the SIP limit") + " local time) come from IEX — real time, but only " +
                               "one exchange's trades. They are drawn lighter and replaced by SIP candles once SIP is available (15 min).";
            AutomationProperties.SetName(TailChip, TailChipText.Text + ". " + TailChip.ToolTip);
            TailChip.Visibility = Visibility.Visible;
        }

        private async Task RefreshMarketAsync()
        {
            var tab = _active;
            if (tab == null || !IsShown) return;
            var result = await ResearchClient.GetMarketStatusAsync();
            if (_shutDown || !ReferenceEquals(tab, _active)) return;
            if (result.Ok && result.Value != null)
            {
                _market = tab.IsCrypto ? result.Value.Crypto : result.Value.Equities;
                _marketError = null;
            }
            else
            {
                _market = null;
                _marketError = result.Message;
            }
            RenderMarketChip();
            RenderPriceValues(); // the "today / at close" suffix follows the market state
        }

        /// <summary>
        /// Live state of the active symbol (chip + note): the socket state first (down → "NOT LIVE", last-price line
        /// hidden — never shown as current, §4.3.1), then the backend's symbol_status / candle_unavailable reason.
        /// </summary>
        private void RenderLiveState()
        {
            ApplySocketToStatusBar();
            var tab = _active;
            if (tab == null) { LiveChip.Visibility = Visibility.Collapsed; return; }
            string text, tip, brush;
            switch (_socket.State)
            {
                case ResearchSocketState.Connected:
                    if (_candleError != null)
                    {
                        text = "NOT LIVE";
                        tip = _candleError;
                        brush = "AccentRedBrush";
                    }
                    else if (_symbolLive == false)
                    {
                        text = "NOT LIVE";
                        tip = _symbolReason ?? "The backend reports this symbol as not live.";
                        brush = "AccentRedBrush";
                    }
                    else if (_symbolLive == true)
                    {
                        text = tab.IsCrypto ? "● LIVE · ALPACA CRYPTO" : "● LIVE · IEX";
                        tip = _symbolReason ?? (tab.IsCrypto ? "Live trades from Alpaca's crypto feed." : "Live trades from IEX (real time, one exchange).");
                        brush = _symbolReason != null ? "CautionBrush" : "PositiveGreenBrush";
                    }
                    else
                    {
                        text = "CONNECTING…";
                        tip = "Waiting for the live status of " + tab.Symbol + ".";
                        brush = "CautionBrush";
                    }
                    break;
                case ResearchSocketState.Waiting:
                    text = "NOT LIVE · WAITING (ATTEMPT " + _socket.Attempt.ToString(CultureInfo.InvariantCulture) + ")";
                    tip = _socket.DescribeState();
                    brush = "CautionBrush";
                    break;
                case ResearchSocketState.Refused:
                    text = "NOT LIVE";
                    tip = _socket.DescribeState();
                    brush = "AccentRedBrush";
                    break;
                case ResearchSocketState.Connecting:
                    text = "CONNECTING…";
                    tip = _socket.DescribeState();
                    brush = "CautionBrush";
                    break;
                default:
                    text = "NOT LIVE";
                    tip = _socket.DescribeState();
                    brush = "MutedForegroundBrush";
                    break;
            }
            LiveChipText.Text = text;
            LiveChipText.Foreground = Token(brush);
            LiveChip.ToolTip = tip;
            AutomationProperties.SetName(LiveChip, text + ". " + tip);
            LiveChip.Visibility = Visibility.Visible;

            // The reason is also visible without hovering when the chart is not live for a known reason.
            if (_socket.State == ResearchSocketState.Connected && (_candleError != null || _symbolReason != null))
                ShowChartNote(_candleError ?? _symbolReason!);
            RenderPriceValues(); // MR4 header numbers: greyed + "not live" while not live
        }

        private void ApplyLastPrice()
        {
            if (_active == null) return;
            if (_socket.State != ResearchSocketState.Connected || _lastTrade == null)
            {
                _chart.SetLastPrice(null, null); // not live → the line is hidden, never shown as current
                return;
            }
            int? sign = _prevClose is > 0 ? Math.Sign(_lastTrade.Value - _prevClose.Value) : null;
            _chart.SetLastPrice(_lastTrade, sign);
        }

        private static string? LocalTime(string? iso)
        {
            if (string.IsNullOrWhiteSpace(iso)) return null;
            if (!DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto)) return null;
            var local = dto.ToLocalTime();
            return local.Date == DateTime.Today
                ? local.ToString("HH:mm", CultureInfo.InvariantCulture)
                : local.ToString("ddd HH:mm", CultureInfo.InvariantCulture);
        }

        // =====================================================================================
        // MR3: live updates over the socket
        // =====================================================================================

        private bool IsActiveSeries(string? symbol, string? tf, string? session)
        {
            var tab = _active;
            return tab != null && string.Equals(symbol, tab.Symbol, StringComparison.OrdinalIgnoreCase) && tf == tab.Tf && session == tab.TopicSession
                   && _chart.HasSeries && _chart.Symbol == tab.Symbol && _chart.Timeframe == tab.Tf && _chart.Session == tab.TopicSession;
        }

        private void Socket_StateChanged(object? sender, EventArgs e)
        {
            if (_shutDown) return;
            if (_socket.State != ResearchSocketState.Connected)
            {
                _symbolLive = null;
                _symbolReason = null;
                _candleError = null;
                _lastTradeLive = false; // the last live price is no longer current (refreshed from the snapshot after the re-sync)
                WatchlistConnectionLost();
            }
            else
            {
                RefreshAllRows();
            }
            UpdatePolling();
            RenderLiveState();
            ApplyLastPrice();
        }

        private void Socket_FeedsReceived(object? sender, FeedsStatus feeds)
        {
            if (_shutDown) return;
            RenderStatus(feeds);
        }

        private void Socket_Resynced(object? sender, EventArgs e)
        {
            // §4.3.1 step 4: candles missed while disconnected are not replayed — re-fetch the open chart, unless it was
            // (re)loaded after this handshake's subscribe.
            if (_shutDown || !IsShown) return; // review L4: hidden (alerts-only connection) → re-fetched when shown again
            RequestAllQuotes(); // MR4: watchlist quotes are re-requested after every reconnect
            ReloadNews();       // MR6: news missed while disconnected is not replayed — re-fetch the first page
            if (_active == null) return;
            var subscribed = _socket.SubscribedAtUtc;
            if (_chartLoadStartedUtc == null || subscribed == null || _chartLoadStartedUtc < subscribed)
                _ = LoadChartAsync(_active, withSnapshot: true);
        }

        private void Socket_CandleReceived(object? sender, WsCandle candle)
        {
            if (_shutDown || candle.Bar == null || !IsActiveSeries(candle.Symbol, candle.Tf, candle.Session)) return;
            _chart.UpdateBar(candle.Bar);
            if (candle.Bar.Final == true) OnLiveBarClosed(candle.Bar); // MR5: indicator refresh once per bar close
            if (_candleError != null) { _candleError = null; HideChartNote(); RenderLiveState(); }
            if (candle.Bar.Feed == "iex_tail" && TailChip.Visibility != Visibility.Visible) RefreshTailChip();
        }

        private void Socket_SipReplaceReceived(object? sender, WsSipReplace message)
        {
            if (_shutDown || message.Bars == null || !IsActiveSeries(message.Symbol, message.Tf, message.Session)) return;
            _chart.ReplaceBars(message.Bars); // each part applies on its own (independent ranges, oldest first)
            RefreshTailChip();
        }

        private void Socket_TradeReceived(object? sender, WsTrade trade)
        {
            if (_shutDown || trade.Symbol == null) return;
            if (trade.Price is not double price || !double.IsFinite(price) || price <= 0) return;
            var symbol = trade.Symbol.ToUpperInvariant();
            WatchlistTrade(symbol, price, trade.Ts); // MR4: watchlist rows (watchlist:<id> topics)
            var tab = _active;
            if (tab == null || symbol != tab.Symbol) return;
            _lastTrade = price;
            _lastTradeLive = true;
            _lastTradeTs = trade.Ts;
            ApplyLastPrice();
            RenderPriceValues();
        }

        private void Socket_SymbolStatusReceived(object? sender, WsSymbolStatus status)
        {
            if (_shutDown || status.Symbol == null) return;
            WatchlistSymbolStatus(status.Symbol.ToUpperInvariant(), status.Live, status.Reason); // MR4: per-symbol reason in the rows
            var tab = _active;
            if (tab == null || !string.Equals(status.Symbol, tab.Symbol, StringComparison.OrdinalIgnoreCase)) return;
            var hadReason = _symbolReason != null;
            _symbolLive = status.Live;
            _symbolReason = string.IsNullOrWhiteSpace(status.Reason) ? null : status.Reason;
            if (hadReason && _symbolReason == null && _candleError == null) HideChartNote();
            RenderLiveState();
        }

        private void Socket_ServerErrorReceived(object? sender, WsError error)
        {
            if (_shutDown) return;
            var tab = _active;
            if (error.Code == "candle_unavailable" && tab != null &&
                error.Topic == "candle:" + tab.Symbol + ":" + tab.Tf + ":" + tab.TopicSession)
            {
                _candleError = string.IsNullOrWhiteSpace(error.Message) ? "Live candle unavailable." : error.Message;
                RenderLiveState();
                return;
            }
            // Other backend errors (subscription_limit, message_too_large): shown in the status tooltip (§4.3.1).
            if (!string.IsNullOrWhiteSpace(error.Message))
            {
                _baseReasons = string.IsNullOrEmpty(_baseReasons) ? error.Message! : _baseReasons + "\n" + error.Message;
                ApplySocketToStatusBar();
            }
        }

        // ---------- section navigation (Overview / Symbol / Alerts) ----------

        private void Nav_Checked(object sender, RoutedEventArgs e)
        {
            if (SymbolPage == null || OverviewPage == null || AlertsPage == null || PortfolioPage == null || MacroPage == null) return; // raised during InitializeComponent
            SymbolPage.Visibility = ReferenceEquals(sender, NavSymbol) ? Visibility.Visible : Visibility.Collapsed;
            OverviewPage.Visibility = ReferenceEquals(sender, NavOverview) ? Visibility.Visible : Visibility.Collapsed;
            AlertsPage.Visibility = ReferenceEquals(sender, NavAlerts) ? Visibility.Visible : Visibility.Collapsed;
            PortfolioPage.Visibility = ReferenceEquals(sender, NavPortfolio) ? Visibility.Visible : Visibility.Collapsed; // MR8
            MacroPage.Visibility = ReferenceEquals(sender, NavMacro) ? Visibility.Visible : Visibility.Collapsed;         // MR8
            OnIntegrationSectionShown(sender);
            OnSectionShown(ReferenceEquals(sender, NavAlerts)); // MR7: load / refresh the alerts while the section is shown
            var section = ReferenceEquals(sender, NavOverview) ? "overview" : ReferenceEquals(sender, NavAlerts) ? "alerts"
                        : ReferenceEquals(sender, NavPortfolio) ? "portfolio" : ReferenceEquals(sender, NavMacro) ? "macro" : "symbol";
            if (section == _section) return;
            _section = section;
            ScheduleSessionSave();
        }

        // ---------- buttons ----------

        private void PopOutButton_Click(object sender, RoutedEventArgs e)
        {
            PopOutToggleRequested?.Invoke(this, EventArgs.Empty);
        }

        private async void RetryButton_Click(object sender, RoutedEventArgs e)
        {
            await PollAsync();
        }
    }
}

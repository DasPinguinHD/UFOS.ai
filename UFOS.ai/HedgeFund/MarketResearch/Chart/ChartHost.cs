using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// The Market Research chart card body: a WebView2 showing <c>chart.html</c> (KLineChart, MR3a) plus the
    /// host side of the bridge (backend/market_research/SPEC.md "MR3 bridge contract v1", §9 security).
    /// One per <see cref="ResearchView"/>.
    ///
    /// Hosting / security:
    /// - one CoreWebView2Environment per process, user-data folder
    ///   %LOCALAPPDATA%\UFOS.ai\HedgeFund\MarketResearch\WebView2 (separate from Global Monitoring's
    ///   %LOCALAPPDATA%\UFOS.ai\GlobalMonitoring\WebView2);
    /// - virtual host <c>chart.ufos.local</c> → &lt;app base dir&gt;\MarketResearch\Chart (DenyCors); only
    ///   https://chart.ufos.local/chart.html may be navigated to, every other navigation, every frame
    ///   navigation, new window, download and permission request is refused, requests to any other host are
    ///   answered 403 (the page's CSP already forbids them); no host objects, no script dialogs, no context
    ///   menu, no zoom, no browser accelerators and no DevTools in Release (DevTools + accelerators in Debug);
    /// - the vendored engine is checked against vendor/MANIFEST (sha256) BEFORE the page is loaded;
    /// - page → host messages: source + size (≤ 64 KiB) + schema checked; host → page: exactly the bridge
    ///   messages via PostWebMessageAsJson (≤ 4 MiB); the backend token never reaches the page.
    ///
    /// Time: the API's ISO <c>t</c> is converted to epoch ms HERE and only here (SPEC §0 Time,
    /// <see cref="TryToEpochMs"/> / <see cref="ToIso"/>).
    ///
    /// Moving between windows (pop-out): neither WPF WebView2 control documents re-parenting to another
    /// top-level window (WebView2CompositionControl's spec does not cover it; the HwndHost-based WebView2
    /// destroys its HWND when it leaves the tree). So this control RE-CREATES the WebView on every Loaded and
    /// disposes it on Unloaded; the chart state (series, bars, type, last price) lives in this class and is
    /// re-sent after the new page's <c>ready</c>. Cost: about a second of "Starting chart…" after pop-out/return.
    ///
    /// Control type: <see cref="WebView2CompositionControl"/> (visual hosting, SDK ≥ 1.0.2957) — the Hedge
    /// Fund window and the pop-out are AllowsTransparency (layered) windows, where an HwndHost child is not
    /// documented to render, and the composition control lets WPF draw the loading/error overlay ON TOP of
    /// the chart (no airspace issue). If the chart stays blank on a PC, set <see cref="UseCompositionControl"/>
    /// to false (the HwndHost <see cref="WebView2"/>; both implement IWebView2) — the overlay then hides the
    /// web view while it is shown.
    ///
    /// No fallback: WebView2 runtime missing, chart files missing, engine modified, page crash → a clear
    /// message in the chart card (the rest of the Research view keeps working), never another renderer.
    /// </summary>
    internal sealed partial class ChartHost : UserControl
    {
        private const string LogModule = "HedgeFund";

        public const string HostName = "chart.ufos.local";
        public const string PageUrl = "https://chart.ufos.local/chart.html";
        /// <summary>Bridge caps (SPEC §9): host → page ≤ 4 MiB serialized, page → host ≤ 64 KiB.</summary>
        private const int MaxHostToPageBytes = 4 * 1024 * 1024;
        private const int MaxPageToHostChars = 64 * 1024;
        /// <summary>Max bars kept/sent per chart (page limit for loadBars, plan R13 "10–20k candles").</summary>
        public const int MaxBars = 20_000;
        private const int MaxReplaceBars = 5_000;
        private static readonly TimeSpan ReadyTimeout = TimeSpan.FromSeconds(15);

        /// <summary>See the class remarks: composition (visual) hosting by default.</summary>
        private static readonly bool UseCompositionControl = true;

#if DEBUG
        private const bool DebugBuild = true;
#else
        private const bool DebugBuild = false;
#endif

        public const string RuntimeMissingMessage =
            "The chart needs the Microsoft Edge WebView2 Runtime, which is not installed on this PC. Install the " +
            "\"Evergreen\" runtime from https://developer.microsoft.com/microsoft-edge/webview2/ and reopen the Hedge Fund window.";

        // Theme = the module's tokens (ResearchView.xaml, values of Styles/DesignSystem.xaml); bridge `init.theme`.
        private static readonly Dictionary<string, string> Theme = new()
        {
            ["background"] = "#16171A", // ModulePanelBackgroundBrush (the chart card)
            ["grid"] = "#202020",       // SubtleBorderBrush
            ["text"] = "#BFBFBF",       // PrimaryForegroundBrush
            ["axisText"] = "#9A9A9A",   // SecondaryForegroundBrush
            ["muted"] = "#6F6F6F",      // MutedForegroundBrush
            ["up"] = "#4ADE80",         // PositiveGreenBrush
            ["down"] = "#ED6A5A",       // AccentRedBrush
            ["accent"] = "#5AC8FA",     // AccentBlueBrush
            ["caution"] = "#F0B429",    // CautionBrush
            ["fontFamily"] = "Consolas",
        };

        private static readonly JsonSerializerOptions BridgeJson = new() { NumberHandling = JsonNumberHandling.Strict };
        private static readonly Regex ErrorCodePattern = new("^[a-z0-9_]{1,40}$", RegexOptions.CultureInvariant);
        private static readonly HashSet<string> BarFeeds = new(StringComparer.Ordinal) { "sip", "iex_tail", "sip_daily", "crypto" };
        private static readonly long MinEpochMs = new DateTimeOffset(1990, 1, 1, 0, 0, 0, TimeSpan.Zero).ToUnixTimeMilliseconds();
        private static readonly long MaxEpochMs = new DateTimeOffset(2100, 1, 1, 0, 0, 0, TimeSpan.Zero).ToUnixTimeMilliseconds();
        /// <summary>chart.js LIMITS.maxVolume.</summary>
        private const double MaxVolume = 1e15;

        private static Task<CoreWebView2Environment>? _environment;

        // ---------- UI ----------
        private readonly Grid _webHost = new();
        private readonly Border _overlay;
        private readonly TextBlock _overlayTitle;
        private readonly TextBlock _overlayDetail;
        private readonly Button _overlayButton;
        private readonly Border _strip;
        private readonly TextBlock _stripText;

        // ---------- WebView ----------
        private FrameworkElement? _webElement;
        private IWebView2? _web;
        private CoreWebView2Environment? _env;
        private int _generation;
        private bool _creating;
        private bool _pageReady;
        private readonly DispatcherTimer _readyTimer;
        private readonly HashSet<string> _loggedOnce = new(StringComparer.Ordinal);
        // Navigations cancelled in NavigationStarting: their NavigationCompleted (IsSuccess false, OperationCanceled)
        // is expected and must not blank the working chart.
        private readonly HashSet<ulong> _cancelledNavigations = new();

        // ---------- chart state (survives WebView re-creation) ----------
        private string? _symbol;
        private string? _tf;
        private string? _session;
        private string? _assetClass;
        private string? _exchangeTz;
        private long? _sipThrough;
        private readonly List<ChartBar> _bars = new();
        private bool _hasSeries;
        private bool _hasMoreLeft;
        private bool _moreLeftPending;
        private string _chartType = "candles";
        private double? _lastPrice;
        private int? _dayChangeSign;

        // ---------- overlay state ----------
        private (string Code, string Title, string? Detail)? _fatal; // host problem: wins over view messages
        private (string Title, string? Detail, bool Retry)? _viewMessage;

        /// <summary>The page asked for older bars; argument = the host's oldest bar start (epoch ms).</summary>
        public event EventHandler<long>? NeedMoreLeft;
        /// <summary>The user pressed "Retry" on a view message (<see cref="ShowMessage"/>).</summary>
        public event EventHandler? RetryRequested;

        public ChartHost()
        {
            Focusable = false;
            AutomationProperties.SetName(this, "Price chart");
            var root = new Grid();
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            // Page error strip (bridge `error`, §8): above the chart, so it works with either control type.
            _stripText = new TextBlock { FontSize = 11, Foreground = MakeBrush("#FFE6E6E6"), TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
            var stripReload = MakeButton("Reload chart", (_, _) => ReloadChart());
            var stripClose = MakeButton("Dismiss", (_, _) => HideStrip());
            var stripGrid = new Grid();
            stripGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            stripGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            stripGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            Grid.SetColumn(stripReload, 1);
            Grid.SetColumn(stripClose, 2);
            stripReload.Margin = new Thickness(8, 0, 0, 0);
            stripClose.Margin = new Thickness(6, 0, 0, 0);
            stripGrid.Children.Add(_stripText);
            stripGrid.Children.Add(stripReload);
            stripGrid.Children.Add(stripClose);
            _strip = new Border
            {
                Visibility = Visibility.Collapsed,
                Margin = new Thickness(0, 0, 0, 6),
                Padding = new Thickness(8, 5, 8, 5),
                CornerRadius = new CornerRadius(6),
                BorderThickness = new Thickness(1),
                Background = MakeBrush("#14ED6A5A"),
                BorderBrush = MakeBrush("#66ED6A5A"),
                Child = stripGrid,
            };
            Grid.SetRow(_strip, 0);
            root.Children.Add(_strip);

            Grid.SetRow(_webHost, 1);
            root.Children.Add(_webHost);

            // Full overlay: starting / loading / waiting / error states (the WPF view stays usable).
            _overlayTitle = new TextBlock { FontSize = 12.5, Foreground = MakeBrush("#FF9A9A9A"), TextWrapping = TextWrapping.Wrap, TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center };
            _overlayDetail = new TextBlock { FontSize = 11, Foreground = MakeBrush("#FF6F6F6F"), TextWrapping = TextWrapping.Wrap, TextAlignment = TextAlignment.Center, HorizontalAlignment = HorizontalAlignment.Center, Margin = new Thickness(0, 4, 0, 0), MaxWidth = 560 };
            _overlayButton = MakeButton("Retry", OverlayButton_Click);
            _overlayButton.Margin = new Thickness(0, 10, 0, 0);
            _overlayButton.HorizontalAlignment = HorizontalAlignment.Center;
            var overlayStack = new StackPanel { HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(16) };
            overlayStack.Children.Add(_overlayTitle);
            overlayStack.Children.Add(_overlayDetail);
            overlayStack.Children.Add(_overlayButton);
            _overlay = new Border { Background = MakeBrush("#FF16171A"), Child = overlayStack, Visibility = Visibility.Collapsed };
            Grid.SetRow(_overlay, 1);
            root.Children.Add(_overlay);

            Content = root;

            _readyTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = ReadyTimeout };
            _readyTimer.Tick += (_, _) =>
            {
                _readyTimer.Stop();
                if (!_pageReady && _web != null)
                    SetFatal("page_timeout", "The chart page did not start.", "No 'ready' message within 15 s — press Reload chart; if it persists see the Backend Log.");
            };

            Loaded += (_, _) => _ = EnsureWebViewAsync();
            Unloaded += (_, _) => TearDownWebView();
            RenderOverlay();
        }

        // =====================================================================================
        // Public API (used by ResearchView) — every call updates the state and, when the page is ready,
        // sends the matching bridge message.
        // =====================================================================================

        public string? Symbol => _symbol;
        public string? Timeframe => _tf;
        public string? Session => _session;
        public bool HasSeries => _hasSeries;
        public bool HasMoreLeft => _hasMoreLeft;
        public int BarCount => _bars.Count;
        /// <summary>True while any loaded candle is an IEX live-tail candle (SPEC §7.1) — for the "IEX · live tail" chip.</summary>
        public bool HasTailBars => _bars.Exists(b => b.Feed == "iex_tail");
        /// <summary>Start (epoch ms) of the loaded bar at <paramref name="index"/> (0 = oldest), or null.</summary>
        public long? BarStartMs(int index) => index >= 0 && index < _bars.Count ? _bars[index].T : null;

        /// <summary>Oldest bar start (epoch ms) or null.</summary>
        public long? OldestBarMs => _bars.Count > 0 ? _bars[0].T : null;

        /// <summary>Replaces the whole series (new symbol / timeframe / session, or a reload after reconnect).</summary>
        /// <returns>The number of API bars dropped because they failed validation (logged).</returns>
        public int LoadBars(string symbol, string tf, string session, string assetClass, string exchangeTz,
                            IReadOnlyList<ApiBar>? bars, bool hasMoreLeft, string? sipThroughIso)
        {
            _symbol = symbol;
            _tf = tf;
            _session = session;
            _assetClass = assetClass;
            _exchangeTz = exchangeTz;
            _sipThrough = TryToEpochMs(sipThroughIso, out var st) ? st : null;
            _bars.Clear();
            var dropped = 0;
            var converted = ConvertBars(bars, ref dropped);
            converted.Sort((a, b) => a.T.CompareTo(b.T));
            foreach (var bar in converted)
                if (_bars.Count == 0 || bar.T > _bars[^1].T) _bars.Add(bar);
            if (_bars.Count > MaxBars)
            {
                _bars.RemoveRange(0, _bars.Count - MaxBars);
                hasMoreLeft = false;
            }
            _hasMoreLeft = hasMoreLeft;
            _moreLeftPending = false;
            _hasSeries = true;
            SendLoadBars();
            if (_drawingsSymbol != null && _drawingsSymbol != symbol)
            {
                // Another symbol: its drawings/indicators are loaded by the view; the old ones must not stay on screen.
                ClearDrawings();
                ClearIndicators();
            }
            return dropped;
        }

        /// <summary>Older history (answer to <see cref="NeedMoreLeft"/>).</summary>
        /// <returns>False when the <see cref="MaxBars"/> limit was reached (paging ends, the caller tells the user).</returns>
        public bool PrependBars(IReadOnlyList<ApiBar>? bars, bool hasMoreLeft)
        {
            _moreLeftPending = false;
            if (!_hasSeries) return true;
            var dropped = 0;
            var first = _bars.Count > 0 ? _bars[0].T : long.MaxValue;
            var older = ConvertBars(bars, ref dropped).FindAll(b => b.T < first);
            older.Sort((a, b) => a.T.CompareTo(b.T));
            var unique = new List<ChartBar>(older.Count);
            foreach (var bar in older)
                if (unique.Count == 0 || bar.T > unique[^1].T) unique.Add(bar);
            var withinLimit = true;
            var room = MaxBars - _bars.Count;
            if (unique.Count > room)
            {
                unique.RemoveRange(0, unique.Count - Math.Max(0, room));
                hasMoreLeft = false;
                withinLimit = false;
            }
            _bars.InsertRange(0, unique);
            _hasMoreLeft = hasMoreLeft;
            // An empty prependBars with hasMoreLeft:true ends paging on the page (MR3a refinement) — send what it means.
            Post(new { type = "prependBars", v = 1, bars = unique, hasMoreLeft = _hasMoreLeft && unique.Count > 0 });
            if (unique.Count == 0) _hasMoreLeft = false;
            return withinLimit;
        }

        /// <summary>Paging failed: no more automatic requests for this load (the caller shows why).</summary>
        public void EndPaging()
        {
            _moreLeftPending = false;
            _hasMoreLeft = false;
            Post(new { type = "prependBars", v = 1, bars = Array.Empty<ChartBar>(), hasMoreLeft = false });
        }

        /// <summary>Live candle / late-trade correction (same <c>t</c> replaces, newer appends, older unknown ignored).</summary>
        public void UpdateBar(ApiBar apiBar)
        {
            if (!_hasSeries) return;
            var bar = ConvertBar(apiBar);
            if (bar == null) { LogOnce("badlive", "Market Research chart: dropped an invalid live candle from the backend."); return; }
            var index = _bars.BinarySearch(bar, ChartBar.ByTime);
            if (index >= 0) _bars[index] = bar;
            else if (_bars.Count == 0 || bar.T > _bars[^1].T) _bars.Add(bar);
            else return; // older and not present: ignored (contract)
            TrimToMaxBars();
            Post(new { type = "updateBar", v = 1, bar });
        }

        /// <summary>
        /// Keeps at most <see cref="MaxBars"/> (the page's loadBars limit): live appends / SIP inserts drop the OLDEST
        /// bars, and paging ends (hasMoreLeft false) — otherwise a re-send after pop-out/Reload would exceed the limit
        /// and the page would reject the whole loadBars.
        /// </summary>
        private void TrimToMaxBars()
        {
            if (_bars.Count <= MaxBars) return;
            _bars.RemoveRange(0, _bars.Count - MaxBars);
            if (!_hasMoreLeft) return;
            _hasMoreLeft = false;
            // Tell the page paging ended (an empty prependBars without a pending request only changes hasMoreLeft).
            if (!_moreLeftPending) Post(new { type = "prependBars", v = 1, bars = Array.Empty<ChartBar>(), hasMoreLeft = false });
        }

        /// <summary>SIP replacement (one WS <c>sip_replace</c> part).</summary>
        public void ReplaceBars(IReadOnlyList<ApiBar>? apiBars)
        {
            if (!_hasSeries) return;
            var dropped = 0;
            var list = ConvertBars(apiBars, ref dropped);
            list.Sort((a, b) => a.T.CompareTo(b.T));
            var unique = new List<ChartBar>(list.Count);
            foreach (var bar in list)
                if (unique.Count == 0 || bar.T > unique[^1].T) unique.Add(bar);
            if (unique.Count == 0) return;
            foreach (var bar in unique)
            {
                var index = _bars.BinarySearch(bar, ChartBar.ByTime);
                if (index >= 0) _bars[index] = bar;
                else _bars.Insert(~index, bar);
            }
            TrimToMaxBars();
            for (var i = 0; i < unique.Count; i += MaxReplaceBars)
                Post(new { type = "replaceBars", v = 1, bars = unique.GetRange(i, Math.Min(MaxReplaceBars, unique.Count - i)) });
        }

        /// <summary><c>candles</c> · <c>line</c> · <c>area</c>.</summary>
        public void SetChartType(string chartType)
        {
            if (chartType != "candles" && chartType != "line" && chartType != "area") return;
            _chartType = chartType;
            Post(new { type = "setType", v = 1, chartType });
        }

        /// <summary>Last-price line coloured by the DAY change; <paramref name="price"/> null hides it (not live).</summary>
        public void SetLastPrice(double? price, int? dayChangeSign)
        {
            if (price.HasValue && !(double.IsFinite(price.Value) && price.Value > 0 && price.Value <= 1e9)) price = null;
            if (dayChangeSign.HasValue && dayChangeSign.Value is < -1 or > 1) dayChangeSign = null;
            _lastPrice = price;
            _dayChangeSign = dayChangeSign;
            Post(new { type = "setLastPrice", v = 1, price, dayChangeSign });
        }

        /// <summary>Forgets the series (no symbol open).</summary>
        public void Clear()
        {
            _hasSeries = false;
            _symbol = _tf = _session = null;
            _bars.Clear();
            _hasMoreLeft = false;
            _moreLeftPending = false;
            _lastPrice = null;
            _dayChangeSign = null;
            ClearDrawings();
            ClearIndicators();
        }

        /// <summary>A view state over the chart (loading, waiting, a feed/data error). Retry raises <see cref="RetryRequested"/>.</summary>
        public void ShowMessage(string title, string? detail = null, bool retry = false)
        {
            _viewMessage = (title, detail, retry);
            RenderOverlay();
        }

        public void HideMessage()
        {
            _viewMessage = null;
            RenderOverlay();
        }

        // =====================================================================================
        // Time conversion — the ONE place (SPEC §0 Time)
        // =====================================================================================

        /// <summary>API ISO-8601 (with offset; naive = UTC) → epoch ms UTC.</summary>
        public static bool TryToEpochMs(string? iso, out long epochMs)
        {
            epochMs = 0;
            if (string.IsNullOrWhiteSpace(iso) || iso.Length > 40) return false;
            if (!DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto)) return false;
            epochMs = dto.ToUnixTimeMilliseconds();
            return epochMs >= MinEpochMs && epochMs < MaxEpochMs;
        }

        /// <summary>Epoch ms UTC → the API's ISO form (<c>2026-10-16T12:00:00+00:00</c>), e.g. for <c>before=</c>.</summary>
        public static string ToIso(long epochMs)
            => DateTimeOffset.FromUnixTimeMilliseconds(epochMs).ToUniversalTime()
                .ToString("yyyy-MM-dd'T'HH:mm:ss'+00:00'", CultureInfo.InvariantCulture);

        private static List<ChartBar> ConvertBars(IReadOnlyList<ApiBar>? bars, ref int dropped)
        {
            var list = new List<ChartBar>(bars?.Count ?? 0);
            if (bars == null) return list;
            foreach (var b in bars)
            {
                var bar = ConvertBar(b);
                if (bar == null) dropped++;
                else list.Add(bar);
            }
            if (dropped > 0) AppLog.Warn(LogModule, "Market Research chart: dropped " + dropped + " invalid bar(s) from the backend (not drawn).");
            return list;
        }

        /// <summary>
        /// API bar → bridge bar; null = invalid (dropped + logged by the caller, never repaired). Mirrors EVERY
        /// page-side bar check so one bad bar can never make the page reject a whole message — keep in sync with
        /// Chart/chart.js: <c>LIMITS</c> (minT/maxT/maxPrice/maxVolume, ~line 18), <c>BAR_FIELDS</c>, <c>cPrice</c>,
        /// <c>cVolume</c>, <c>cEpoch</c>, <c>cBar</c> (~lines 165–210): t int in [1990, 2100); o/h/l/c finite in
        /// (0, 1e9]; v null or finite in [0, 1e15]; feed ∈ FEEDS; final bool; h ≥ l and o/c within [l, h] (eps
        /// 1e-9·max(1, h)); iex_tail never final; s ∈ pre|rth|post or absent. Only t/o/h/l/c/v/feed/final(/s) are sent
        /// (unknown keys are rejected).
        /// </summary>
        internal static ChartBar? ConvertBar(ApiBar? b)
        {
            if (b == null || !TryToEpochMs(b.T, out var t)) return null;
            if (!(Price(b.O) && Price(b.H) && Price(b.L) && Price(b.C))) return null;
            double o = b.O!.Value, h = b.H!.Value, l = b.L!.Value, c = b.C!.Value;
            var eps = 1e-9 * Math.Max(1, h);
            if (h + eps < l || o > h + eps || o < l - eps || c > h + eps || c < l - eps) return null;
            if (b.V.HasValue && !(double.IsFinite(b.V.Value) && b.V.Value >= 0 && b.V.Value <= MaxVolume)) return null;
            if (b.Feed == null || !BarFeeds.Contains(b.Feed) || b.Final == null) return null;
            if (b.Feed == "iex_tail" && b.Final.Value) return null;
            // Optional session flag (intraday stock bars; chart.js BAR_FIELDS.s = SESSIONS_FLAG): only these values,
            // absent → no key at all (the page refuses `s: null`).
            if (b.S != null && b.S is not ("pre" or "rth" or "post")) return null;
            return new ChartBar { T = t, O = o, H = h, L = l, C = c, V = b.V, Feed = b.Feed, Final = b.Final.Value, S = b.S };

            static bool Price(double? x) => x.HasValue && double.IsFinite(x.Value) && x.Value > 0 && x.Value <= 1e9;
        }

        // =====================================================================================
        // WebView lifecycle
        // =====================================================================================

        /// <summary>&lt;app base dir&gt;\MarketResearch\Chart (copied by MultiAgentHedgeFund.csproj).</summary>
        public static string ChartFolder => Path.Combine(AppContext.BaseDirectory, "MarketResearch", "Chart");

        private async Task EnsureWebViewAsync()
        {
            if (_web != null || _creating || _fatal != null) return;
            _creating = true;
            var generation = ++_generation;
            IWebView2? control = null;
            try
            {
                // 1) runtime present? (no fallback renderer)
                string? version = null;
                try { version = CoreWebView2Environment.GetAvailableBrowserVersionString(); }
                catch (WebView2RuntimeNotFoundException) { version = null; }
                if (string.IsNullOrEmpty(version))
                {
                    SetFatal("runtime_missing", "Chart unavailable: WebView2 Runtime missing.", RuntimeMissingMessage, retry: true);
                    return;
                }

                // 2) page files + engine integrity (before anything is loaded)
                var folder = ChartFolder;
                if (!File.Exists(Path.Combine(folder, "chart.html")) || !File.Exists(Path.Combine(folder, "chart.js")))
                {
                    SetFatal("files_missing", "Chart unavailable: the chart page files are missing.",
                             "Expected " + Path.Combine(folder, "chart.html") + " — rebuild the app (MultiAgentHedgeFund.csproj copies MarketResearch\\Chart).");
                    return;
                }
                var engineProblem = VerifyEngine(folder);
                if (engineProblem != null)
                {
                    AppLog.Warn(LogModule, "Market Research chart: engine check failed — " + engineProblem);
                    SetFatal("engine_modified", "Chart error (engine_modified)",
                             "Chart engine files do not match vendor/MANIFEST (" + engineProblem + ") — reinstall/rebuild the app; " +
                             "see MarketResearch\\Chart\\vendor\\README.txt.");
                    return;
                }

                ShowStarting();
                var env = await GetEnvironmentAsync();
                if (generation != _generation) return;
                _env = env;

                control = UseCompositionControl
                    ? (IWebView2)new WebView2CompositionControl()
                    : new Microsoft.Web.WebView2.Wpf.WebView2();
                control.DefaultBackgroundColor = System.Drawing.Color.FromArgb(255, 0x16, 0x17, 0x1A);
                var element = (FrameworkElement)control;
                AutomationProperties.SetName(element, "Price chart");
                _web = control;
                _webElement = element;
                _webHost.Children.Add(element);

                await control.EnsureCoreWebView2Async(env);
                if (generation != _generation) return; // torn down meanwhile (TearDownWebView disposed it)
                var core = control.CoreWebView2;
                if (core == null) throw new InvalidOperationException("CoreWebView2 not created");

                var settings = core.Settings;
                settings.AreDevToolsEnabled = DebugBuild;
                settings.AreBrowserAcceleratorKeysEnabled = DebugBuild; // Release: no F5/Ctrl+R reload, Ctrl+P, Ctrl+F …
                settings.AreDefaultContextMenusEnabled = false;
                settings.AreDefaultScriptDialogsEnabled = false;
                settings.AreHostObjectsAllowed = false;
                settings.IsWebMessageEnabled = true;
                settings.IsScriptEnabled = true;
                settings.IsStatusBarEnabled = false;
                settings.IsZoomControlEnabled = false;
                settings.IsPinchZoomEnabled = false;
                settings.IsSwipeNavigationEnabled = false;
                settings.IsPasswordAutosaveEnabled = false;
                settings.IsGeneralAutofillEnabled = false;
                settings.IsBuiltInErrorPageEnabled = false;

                core.SetVirtualHostNameToFolderMapping(HostName, folder, CoreWebView2HostResourceAccessKind.DenyCors);
                core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
                core.WebResourceRequested += Core_WebResourceRequested;
                core.NavigationStarting += Core_NavigationStarting;
                core.FrameNavigationStarting += Core_FrameNavigationStarting;
                core.NavigationCompleted += Core_NavigationCompleted;
                core.NewWindowRequested += Core_NewWindowRequested;
                core.DownloadStarting += Core_DownloadStarting;
                core.PermissionRequested += Core_PermissionRequested;
                core.WebMessageReceived += Core_WebMessageReceived;
                core.ProcessFailed += Core_ProcessFailed;

                _pageReady = false;
                _readyTimer.Stop();
                _readyTimer.Start();
                core.Navigate(PageUrl);
            }
            catch (WebView2RuntimeNotFoundException)
            {
                if (generation == _generation)
                    SetFatal("runtime_missing", "Chart unavailable: WebView2 Runtime missing.", RuntimeMissingMessage, retry: true);
            }
            catch (Exception ex)
            {
                if (generation == _generation)
                {
                    AppLog.Warn(LogModule, "Market Research chart: the WebView2 could not be started.", ex);
                    SetFatal("webview_failed", "Chart unavailable: the chart view could not start.",
                             ex.GetType().Name + " — see the Backend Log. Press Reload chart to try again.");
                }
            }
            finally
            {
                if (generation == _generation) _creating = false;
            }
        }

        private void TearDownWebView()
        {
            _generation++;
            _creating = false;
            _pageReady = false;
            _readyTimer.Stop();
            _cancelledNavigations.Clear();
            var web = _web;
            var element = _webElement;
            _web = null;
            _webElement = null;
            if (element != null) _webHost.Children.Remove(element);
            if (web != null)
            {
                try { (web as IDisposable)?.Dispose(); } // both controls are IDisposable (IWebView2 is not in every SDK version)
                catch (Exception ex) { AppLog.Warn(LogModule, "Market Research chart: disposing the WebView2 failed.", ex); }
            }
        }

        /// <summary>Reload = a fresh WebView (also clears a host error); the state is re-sent after `ready`.</summary>
        private void ReloadChart()
        {
            HideStrip();
            _fatal = null;
            TearDownWebView();
            RenderOverlay();
            if (IsLoaded) _ = EnsureWebViewAsync();
        }

        private static Task<CoreWebView2Environment> GetEnvironmentAsync()
        {
            if (_environment == null || _environment.IsFaulted || _environment.IsCanceled)
            {
                var folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                          "UFOS.ai", "HedgeFund", "MarketResearch", "WebView2");
                Directory.CreateDirectory(folder);
                _environment = CoreWebView2Environment.CreateAsync(null, folder);
            }
            return _environment;
        }

        // =====================================================================================
        // Engine integrity (SPEC §9 + bridge contract "Engine check")
        // =====================================================================================

        /// <summary>Null when every file in vendor/MANIFEST matches its sha256 and a .js engine is listed; else the problem.</summary>
        internal static string? VerifyEngine(string chartFolder)
        {
            try
            {
                var vendor = Path.GetFullPath(Path.Combine(chartFolder, "vendor"));
                var manifest = Path.Combine(vendor, "MANIFEST");
                if (!File.Exists(manifest)) return "vendor/MANIFEST is missing";
                var engineScripts = 0;
                foreach (var raw in File.ReadAllLines(manifest))
                {
                    var line = raw.Trim();
                    if (line.Length == 0 || line.StartsWith('#')) continue;
                    var parts = line.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
                    if (parts.Length < 4) return "a MANIFEST line is malformed";
                    if (parts[3].Equals("sha256", StringComparison.OrdinalIgnoreCase)) continue; // header line
                    var name = parts[0];
                    var sha = parts[3];
                    if (sha.Length != 64 || !IsHex(sha)) return "MANIFEST has an invalid sha256 for " + ClipText(name, 60);
                    var file = Path.GetFullPath(Path.Combine(vendor, name));
                    if (!file.StartsWith(vendor + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                        return "MANIFEST names a file outside vendor/";
                    if (!File.Exists(file)) return "vendor/" + ClipText(name, 60) + " is missing";
                    string actual;
                    using (var fs = File.OpenRead(file)) actual = Convert.ToHexString(SHA256.HashData(fs));
                    if (!actual.Equals(sha, StringComparison.OrdinalIgnoreCase)) return "vendor/" + ClipText(name, 60) + " does not match its sha256";
                    if (name.EndsWith(".js", StringComparison.OrdinalIgnoreCase)) engineScripts++;
                }
                return engineScripts == 0 ? "MANIFEST lists no engine script" : null;
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is ArgumentException || ex is NotSupportedException)
            {
                return "vendor files could not be read (" + ex.GetType().Name + ")";
            }

            static bool IsHex(string s)
            {
                foreach (var ch in s) if (!Uri.IsHexDigit(ch)) return false;
                return true;
            }
        }

        // =====================================================================================
        // CoreWebView2 events (all on the UI thread)
        // =====================================================================================

        private static bool IsPageUri(string? uri)
        {
            if (!Uri.TryCreate(uri, UriKind.Absolute, out var u)) return false;
            return u.Scheme == Uri.UriSchemeHttps && string.Equals(u.Host, HostName, StringComparison.OrdinalIgnoreCase)
                   && u.IsDefaultPort && u.AbsolutePath == "/chart.html" && string.IsNullOrEmpty(u.Query);
        }

        private void Core_NavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
        {
            if (IsPageUri(e.Uri)) return;
            e.Cancel = true;
            if (_cancelledNavigations.Count < 256) _cancelledNavigations.Add(e.NavigationId);
            LogOnce("nav", "Market Research chart: blocked a navigation away from the chart page (" + ClipText(e.Uri, 120) + ").");
        }

        private void Core_FrameNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
        {
            e.Cancel = true; // the page has no frames
            LogOnce("frame", "Market Research chart: blocked a frame navigation.");
        }

        private void Core_NewWindowRequested(object? sender, CoreWebView2NewWindowRequestedEventArgs e)
        {
            e.Handled = true; // never a new (chrome-less) WebView window, never a browser launch from the chart
            LogOnce("window", "Market Research chart: blocked a new-window request.");
        }

        private void Core_DownloadStarting(object? sender, CoreWebView2DownloadStartingEventArgs e)
        {
            e.Cancel = true;
            e.Handled = true;
        }

        private void Core_PermissionRequested(object? sender, CoreWebView2PermissionRequestedEventArgs e)
        {
            e.State = CoreWebView2PermissionState.Deny;
            e.SavesInProfile = false;   // nothing is remembered
            e.Handled = true;           // decided here — WebView2 never shows its own prompt
        }

        private void Core_WebResourceRequested(object? sender, CoreWebView2WebResourceRequestedEventArgs e)
        {
            // Defence in depth next to the page's CSP (connect-src 'none' …): nothing but the chart host is served.
            if (Uri.TryCreate(e.Request.Uri, UriKind.Absolute, out var u) &&
                u.Scheme == Uri.UriSchemeHttps && string.Equals(u.Host, HostName, StringComparison.OrdinalIgnoreCase))
                return;
            if (e.Request.Uri.StartsWith("data:", StringComparison.OrdinalIgnoreCase)) return;
            if (_env != null) e.Response = _env.CreateWebResourceResponse(null, 403, "Forbidden", "");
            LogOnce("res", "Market Research chart: blocked a request to " + ClipText(u?.Host ?? e.Request.Uri, 80) + ".");
        }

        private void Core_NavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            var cancelledByUs = _cancelledNavigations.Remove(e.NavigationId);
            if (e.IsSuccess) return;
            if (cancelledByUs && e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled) return;
            _readyTimer.Stop();
            SetFatal("page_load_failed", "Chart unavailable: the chart page could not be loaded.",
                     "WebView2 reported " + e.WebErrorStatus + ". Press Reload chart; if it persists rebuild the app.");
        }

        private void Core_ProcessFailed(object? sender, CoreWebView2ProcessFailedEventArgs e)
        {
            AppLog.Warn(LogModule, "Market Research chart: WebView2 process failed (" + e.ProcessFailedKind + ").");
            var kind = e.ProcessFailedKind;
            // GPU / utility / frame / other helper processes are restarted by WebView2 itself: log only.
            if (kind != CoreWebView2ProcessFailedKind.RenderProcessExited &&
                kind != CoreWebView2ProcessFailedKind.RenderProcessUnresponsive &&
                kind != CoreWebView2ProcessFailedKind.BrowserProcessExited)
                return;
            if (kind == CoreWebView2ProcessFailedKind.BrowserProcessExited) _environment = null; // a new environment next time
            _pageReady = false;
            SetFatal("page_crashed", "The chart page stopped (" + e.ProcessFailedKind + ").",
                     "Press Reload chart. The rest of Market Research keeps working.");
        }

        private void Core_WebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            if (!IsPageUri(e.Source))
            {
                LogOnce("src", "Market Research chart: ignored a bridge message from an unexpected source.");
                return;
            }
            string json;
            try { json = e.WebMessageAsJson; }
            catch (Exception) { return; }
            if (string.IsNullOrEmpty(json) || json.Length > MaxPageToHostChars)
            {
                LogOnce("size", "Market Research chart: dropped a bridge message larger than 64 KiB.");
                return;
            }
            try
            {
                using var doc = JsonDocument.Parse(json, new JsonDocumentOptions { MaxDepth = 16 });
                var root = doc.RootElement;
                if (root.ValueKind == JsonValueKind.String)
                {
                    // A JSON string holding the object is accepted too (contract) — parse once more, same limits.
                    var inner = root.GetString() ?? "";
                    if (inner.Length > MaxPageToHostChars) return;
                    using var innerDoc = JsonDocument.Parse(inner, new JsonDocumentOptions { MaxDepth = 16 });
                    HandlePageMessage(innerDoc.RootElement);
                    return;
                }
                HandlePageMessage(root);
            }
            catch (JsonException)
            {
                LogOnce("json", "Market Research chart: dropped a bridge message that is not valid JSON.");
            }
        }

        private void HandlePageMessage(JsonElement m)
        {
            if (m.ValueKind != JsonValueKind.Object ||
                !m.TryGetProperty("type", out var typeEl) || typeEl.ValueKind != JsonValueKind.String ||
                !m.TryGetProperty("v", out var vEl) || vEl.ValueKind != JsonValueKind.Number || !vEl.TryGetInt32(out var v) || v != 1)
            {
                LogOnce("shape", "Market Research chart: dropped a bridge message without {type, v: 1}.");
                return;
            }
            var type = typeEl.GetString();
            switch (type)
            {
                case "ready":
                    if (m.TryGetProperty("engineVersion", out var ev) && ev.ValueKind == JsonValueKind.String && (ev.GetString()?.Length ?? 0) <= 32)
                        LogOnce("ready:" + ev.GetString(), "Market Research chart: page ready (KLineChart " + ev.GetString() + ").");
                    _readyTimer.Stop();
                    _pageReady = true;
                    OnPageReadyMr5(m);
                    SendAll();
                    RenderOverlay();
                    PageReady?.Invoke(this, EventArgs.Empty);
                    break;
                case "needMoreLeft":
                    if (!m.TryGetProperty("beforeTs", out var ts) || ts.ValueKind != JsonValueKind.Number || !ts.TryGetInt64(out _)) return;
                    if (!_pageReady || !_hasSeries || _moreLeftPending) return;
                    if (!_hasMoreLeft || _bars.Count == 0)
                    {
                        // Nothing more to load: answer at once so the page releases its pending left-edge request.
                        Post(new { type = "prependBars", v = 1, bars = Array.Empty<ChartBar>(), hasMoreLeft = false });
                        return;
                    }
                    _moreLeftPending = true;
                    NeedMoreLeft?.Invoke(this, _bars[0].T); // the host's oldest bar, not the page's value
                    break;
                case "crosshair":
                    break; // legend is in the page (MR3)
                case "error":
                {
                    var code = m.TryGetProperty("code", out var c) && c.ValueKind == JsonValueKind.String ? c.GetString() : null;
                    var message = m.TryGetProperty("message", out var msg) && msg.ValueKind == JsonValueKind.String ? msg.GetString() : null;
                    if (code == null || !ErrorCodePattern.IsMatch(code) || message == null || message.Length > 300)
                    {
                        LogOnce("errshape", "Market Research chart: dropped a malformed page error message.");
                        return;
                    }
                    AppLog.Warn(LogModule, "Market Research chart page error (" + code + "): " + message);
                    if (code == "drawing_invalid") DrawingNotice?.Invoke(this, message);           // rev. 2: a finished drawing was discarded
                    else if (code != "drawing_unknown") ShowStrip("Chart error (" + code + "): " + message); // drawing_unknown: log only
                    break;
                }
                default:
                    if (HandleDrawingMessage(type, m)) break; // MR5 bridge revision 2 (ChartHost.Drawings.cs)
                    LogOnce("type:" + ClipText(type, 40), "Market Research chart: ignored unknown page message type '" + ClipText(type, 40) + "'.");
                    break;
            }
        }

        // =====================================================================================
        // Host → page
        // =====================================================================================

        private void SendAll()
        {
            Post(new { type = "init", v = 1, theme = Theme, locale = "en-US", tz = LocalIanaZone() });
            if (_hasSeries)
            {
                SendLoadBars();
                Post(new { type = "setType", v = 1, chartType = _chartType });
                Post(new { type = "setLastPrice", v = 1, price = _lastPrice, dayChangeSign = _dayChangeSign });
            }
            else
            {
                Post(new { type = "setType", v = 1, chartType = _chartType });
            }
            SendMr5State(); // drawings · indicators · tool · magnet · hide-all (revision-2 pages only)
            SendMarkers();  // MR8 markers (revision-4 pages only, ChartHost.Markers.cs)
        }

        private void SendLoadBars()
        {
            if (!_hasSeries || _symbol == null || _tf == null || _session == null || _assetClass == null || _exchangeTz == null) return;
            // Never more than the page's limit (defensive — TrimToMaxBars already keeps the list within it).
            var bars = _bars.Count > MaxBars ? _bars.GetRange(_bars.Count - MaxBars, MaxBars) : _bars;
            Post(new
            {
                type = "loadBars",
                v = 1,
                symbol = _symbol,
                tf = _tf,
                session = _session,
                assetClass = _assetClass,
                exchangeTz = _exchangeTz,
                bars,
                hasMoreLeft = _hasMoreLeft && _bars.Count <= MaxBars,
                sipThrough = _sipThrough,
            });
        }

        private bool Post(object payload)
        {
            var core = _web?.CoreWebView2;
            if (core == null || !_pageReady) return false; // sent with the full state after the next `ready`
            string json;
            try { json = JsonSerializer.Serialize(payload, BridgeJson); }
            catch (Exception ex) when (ex is ArgumentException || ex is NotSupportedException || ex is InvalidOperationException)
            {
                AppLog.Warn(LogModule, "Market Research chart: a bridge message could not be serialized.", ex);
                return false;
            }
            if (Encoding.UTF8.GetByteCount(json) > MaxHostToPageBytes)
            {
                ShowStrip("Chart error (too_large): the chart data is larger than 4 MiB and was not sent.");
                return false;
            }
            try
            {
                core.PostWebMessageAsJson(json);
                return true;
            }
            catch (Exception ex)
            {
                LogOnce("post", "Market Research chart: sending to the chart page failed (" + ex.GetType().Name + ").");
                return false;
            }
        }

        private static string? LocalIanaZone()
        {
            try
            {
                var local = TimeZoneInfo.Local;
                if (local.HasIanaId) return local.Id;
                return TimeZoneInfo.TryConvertWindowsIdToIanaId(local.Id, out var iana) ? iana : null; // null = the page uses the PC zone
            }
            catch { return null; }
        }

        // =====================================================================================
        // Overlay / strip
        // =====================================================================================

        private void ShowStarting()
        {
            if (_fatal == null && _viewMessage == null)
            {
                _overlayTitle.Text = "Starting chart…";
                _overlayDetail.Text = "";
                _overlayDetail.Visibility = Visibility.Collapsed;
                _overlayButton.Visibility = Visibility.Collapsed;
                _overlay.Visibility = Visibility.Visible;
                AutomationProperties.SetName(_overlay, "Starting chart");
            }
        }

        private void SetFatal(string code, string title, string detail, bool retry = true)
        {
            _fatal = (code, title, detail);
            _pageReady = false;
            _readyTimer.Stop();
            RenderOverlay();
        }

        private void RenderOverlay()
        {
            string? title = null, detail = null, button = null;
            if (_fatal is { } f)
            {
                title = f.Title;
                detail = f.Detail;
                button = "Reload chart";
            }
            else if (_viewMessage is { } m)
            {
                title = m.Title;
                detail = m.Detail;
                button = m.Retry ? "Retry" : null;
            }
            else if (_web != null && !_pageReady)
            {
                title = "Starting chart…";
            }

            if (title == null)
            {
                _overlay.Visibility = Visibility.Collapsed;
                if (_webElement != null) _webElement.Visibility = Visibility.Visible;
                return;
            }
            _overlayTitle.Text = title;
            _overlayDetail.Text = detail ?? "";
            _overlayDetail.Visibility = string.IsNullOrEmpty(detail) ? Visibility.Collapsed : Visibility.Visible;
            _overlayButton.Content = button ?? "";
            _overlayButton.Visibility = button == null ? Visibility.Collapsed : Visibility.Visible;
            _overlay.Visibility = Visibility.Visible;
            AutomationProperties.SetName(_overlay, title + (string.IsNullOrEmpty(detail) ? "" : " " + detail));
            // HwndHost variant: the native view would paint over the overlay (airspace) — hide it meanwhile.
            if (!UseCompositionControl && _webElement != null) _webElement.Visibility = Visibility.Hidden;
        }

        private void OverlayButton_Click(object sender, RoutedEventArgs e)
        {
            if (_fatal != null) ReloadChart();
            else RetryRequested?.Invoke(this, EventArgs.Empty);
        }

        private void ShowStrip(string text)
        {
            _stripText.Text = text;
            AutomationProperties.SetName(_strip, text);
            _strip.Visibility = Visibility.Visible;
        }

        private void HideStrip() => _strip.Visibility = Visibility.Collapsed;

        // =====================================================================================
        // helpers
        // =====================================================================================

        private Button MakeButton(string text, RoutedEventHandler click)
        {
            var b = new Button { Content = text, Padding = new Thickness(10, 3, 10, 3), FontSize = 11.5, Cursor = System.Windows.Input.Cursors.Hand };
            // Same look as ResearchView's RoundedDarkButtonStyle (resolved once this control is in the view's tree).
            b.SetResourceReference(StyleProperty, "RoundedDarkButtonStyle");
            b.Click += click;
            return b;
        }

        private static SolidColorBrush MakeBrush(string hex)
        {
            var brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(hex));
            brush.Freeze();
            return brush;
        }

        private void LogOnce(string key, string text)
        {
            if (_loggedOnce.Count < 128 && _loggedOnce.Add(key)) AppLog.Warn(LogModule, text);
        }

        private static string ClipText(string? text, int max) => text == null ? "" : (text.Length <= max ? text : text.Substring(0, max) + "…");
    }

    /// <summary>Bridge bar (SPEC "MR3 bridge contract v1"): <c>t</c> = bar start, epoch ms UTC.</summary>
    internal sealed class ChartBar
    {
        public static readonly IComparer<ChartBar> ByTime = Comparer<ChartBar>.Create((a, b) => a.T.CompareTo(b.T));

        [JsonPropertyName("t")] public long T { get; init; }
        [JsonPropertyName("o")] public double O { get; init; }
        [JsonPropertyName("h")] public double H { get; init; }
        [JsonPropertyName("l")] public double L { get; init; }
        [JsonPropertyName("c")] public double C { get; init; }
        [JsonPropertyName("v")] public double? V { get; init; }
        [JsonPropertyName("feed")] public string Feed { get; init; } = "";
        [JsonPropertyName("final")] public bool Final { get; init; }
        /// <summary>Session flag pre · rth · post (intraday stock bars only) — omitted when null.</summary>
        [JsonPropertyName("s")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? S { get; init; }
    }
}

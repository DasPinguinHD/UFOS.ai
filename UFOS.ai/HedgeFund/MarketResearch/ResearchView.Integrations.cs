using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>Buy/Sell/Short/Cover hand-over to the Hedge Fund window's manual paper order panel (MR8).</summary>
    public sealed class ResearchOrderHandOverEventArgs : EventArgs
    {
        public ResearchOrderHandOverEventArgs(string symbol, string side)
        {
            Symbol = symbol;
            Side = side;
        }

        /// <summary>Platform symbol (AAPL, BRK.B, BTC-USD).</summary>
        public string Symbol { get; }

        /// <summary>buy · sell · short · cover</summary>
        public string Side { get; }
    }

    /// <summary>
    /// MR8 in-module integrations (SPEC "MR8 backend notes", plan §10 / §8): Portfolio section (HedgeFund paper account,
    /// read-only), Macro section (the Financial Newsroom's own Macro Indicators + Economic Calendar views, reused — not
    /// copied), Buy/Sell hand-over (opens the EXISTING manual paper order panel pre-filled with symbol + side; Research
    /// never places an order — the panel keeps its validation, confirmations and audit), chart markers (insider
    /// buys/sells, FOMC / release dates, backtest fills + attribution periods / drawdowns), "Open in Market Research"
    /// from a backtest report, "Use as universe" for a watchlist (opens a NEW strategy draft in the Library's editor —
    /// the user saves there; no strategy write from Research).
    /// </summary>
    public partial class ResearchView
    {
        private static readonly TimeSpan PortfolioRefreshInterval = TimeSpan.FromSeconds(60);
        private static readonly Dictionary<string, string> SideLabels = new(StringComparer.Ordinal)
        {
            ["buy"] = "Buy",
            ["sell"] = "Sell",
            ["short"] = "Short",
            ["cover"] = "Cover",
        };

        /// <summary>Raised by Buy / Sell / Short / Cover; HedgeFundWindow opens its manual order panel (nothing is submitted).</summary>
        public event EventHandler<ResearchOrderHandOverEventArgs>? OrderHandOverRequested;

        // ---------- portfolio ----------
        private PortfolioState? _portfolio;
        private string? _portfolioError;
        private bool _portfolioLoading;
        private DateTime? _portfolioLoadedUtc;
        private DispatcherTimer? _portfolioTimer;

        // ---------- macro (reused Newsroom views, created on first use) ----------
        private MacroIndicatorsView? _macroView;
        private EconomicCalendarView? _calendarView;

        // ---------- markers ----------
        private sealed class TabMarkers
        {
            public bool On;
            public string? RunId;
        }

        private readonly Dictionary<ResearchTab, TabMarkers> _tabMarkers = new();
        private int _markersGeneration;

        // =====================================================================================
        // Sections (called from Nav_Checked)
        // =====================================================================================

        private void OnIntegrationSectionShown(object sender)
        {
            if (ReferenceEquals(sender, NavPortfolio))
            {
                if (_portfolioTimer == null)
                {
                    _portfolioTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = PortfolioRefreshInterval };
                    _portfolioTimer.Tick += (_, _) =>
                    {
                        if (IsShown && PortfolioPage.Visibility == Visibility.Visible) _ = LoadPortfolioAsync();
                    };
                }
                _portfolioTimer.Start();
                _ = LoadPortfolioAsync();
            }
            else
            {
                _portfolioTimer?.Stop();
            }
            if (ReferenceEquals(sender, NavMacro)) ShowMacroTab(MacroCalendarTab.IsChecked == true);
        }

        /// <summary>The Hedge Fund window closes: stop the reused Newsroom views' timers and the portfolio refresh.</summary>
        private void ShutdownIntegrations()
        {
            _portfolioTimer?.Stop();
            _macroView?.Shutdown();
            _calendarView?.Shutdown();
        }

        // =====================================================================================
        // Portfolio (GET /research/portfolio — read-only)
        // =====================================================================================

        private void PortfolioRefreshButton_Click(object sender, RoutedEventArgs e) => _ = LoadPortfolioAsync();

        private async Task LoadPortfolioAsync()
        {
            if (_portfolioLoading || _shutDown) return;
            _portfolioLoading = true;
            PortfolioRefreshButton.IsEnabled = false;
            if (_portfolio == null && _portfolioError == null) SetPortfolioMessage("Loading the paper account…");
            PlatformResult<PortfolioState> result;
            try { result = await ResearchClient.GetPortfolioAsync(); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading the portfolio failed.", ex);
                result = new PlatformResult<PortfolioState> { Status = PlatformCallStatus.Error, Message = "unexpected error — see the Backend Log." };
            }
            finally
            {
                _portfolioLoading = false;
                PortfolioRefreshButton.IsEnabled = true;
            }
            if (_shutDown) return;
            _portfolioLoadedUtc = DateTime.UtcNow;
            if (result.Ok && result.Value != null)
            {
                _portfolio = result.Value;
                _portfolioError = null;
            }
            else
            {
                _portfolio = null; // no older account state shown as current
                _portfolioError = "Portfolio unavailable: " + (result.Ok ? "empty response from the backend." : result.Message);
            }
            RenderPortfolio();
        }

        private void SetPortfolioMessage(string? text)
        {
            PortfolioMessageText.Text = text ?? "";
            PortfolioMessageText.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
            if (!string.IsNullOrEmpty(text)) AutomationProperties.SetName(PortfolioMessageText, text);
        }

        private void RenderPortfolio()
        {
            PortfolioRows.Children.Clear();
            var p = _portfolio;
            if (p == null)
            {
                PortfolioSummaryText.Text = "";
                PortfolioNoteText.Text = "";
                SetPortfolioMessage(_portfolioError ?? "Loading the paper account…");
                return;
            }
            SetPortfolioMessage(null);
            var parts = new List<string>
            {
                "Cash " + Money(p.Cash),
                "Equity " + Money(p.Equity?.Value) + (p.Equity?.Ts is { } ts && LocalDateTime(ts) is { } at ? " (" + at + ")" : ""),
            };
            if (p.Equity?.DrawdownPct is double dd) parts.Add("Drawdown " + dd.ToString("0.0", CultureInfo.InvariantCulture) + " %");
            if (p.StartingCapital is double sc) parts.Add("Start " + Money(sc));
            if (p.Engine != null)
                parts.Add("Engine: " + (p.Engine.Halted == true ? "HALTED" : "running") + (p.Engine.Autopilot == true ? " · autopilot on" : " · autopilot off"));
            PortfolioSummaryText.Text = string.Join("   ·   ", parts);
            if (p.Equity?.Reason is { Length: > 0 } eqReason) PortfolioSummaryText.Text += "\n" + eqReason;
            AutomationProperties.SetName(PortfolioSummaryText, "Paper account: " + string.Join(", ", parts));

            var positions = p.Positions ?? new List<PortfolioPosition>();
            if (positions.Count == 0)
            {
                PortfolioRows.Children.Add(new TextBlock { Text = "No open paper positions.", Style = (Style)Resources["EmptyTitleStyle"], Margin = new Thickness(0, 6, 0, 0) });
            }
            else
            {
                PortfolioRows.Children.Add(PortfolioRow(null));
                foreach (var pos in positions) PortfolioRows.Children.Add(PortfolioRow(pos));
                var t = p.Totals;
                if (t != null)
                {
                    var totals = "Totals: " + (t.Positions?.ToString(CultureInfo.InvariantCulture) ?? "?") + " position(s) · long " + Money(t.LongMv) +
                                 " · short " + Money(t.ShortMv) + " · unrealized " + SignedMoney(t.UnrealizedPnl) +
                                 (t.Complete == false ? " — incomplete: some positions have no engine mark yet" : "");
                    PortfolioRows.Children.Add(new TextBlock
                    {
                        Text = totals,
                        FontFamily = new FontFamily("Consolas"),
                        FontSize = 11,
                        Foreground = Token("SecondaryForegroundBrush"),
                        Margin = new Thickness(0, 8, 0, 0),
                        TextWrapping = TextWrapping.Wrap,
                    });
                }
            }
            PortfolioNoteText.Text = string.Join("\n", new[] { p.MarksNote, p.Note }.Where(n => !string.IsNullOrWhiteSpace(n)));
        }

        private static readonly double[] PortfolioColumns = { 92, 52, 84, 84, 84, 96, 120, 74, 96 };

        /// <summary>One row (null = the header). Numbers Consolas; P&amp;L with sign + arrow; the symbol opens its tab.</summary>
        private UIElement PortfolioRow(PortfolioPosition? pos)
        {
            var grid = new Grid { Margin = new Thickness(0, 0, 0, 2) };
            foreach (var w in PortfolioColumns) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(w) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            if (pos == null)
            {
                var heads = new[] { "SYMBOL", "SIDE", "QTY", "AVG", "MARK", "VALUE", "UNREALIZED", "%", "REALIZED" };
                for (var i = 0; i < heads.Length; i++)
                {
                    var h = new TextBlock { Text = heads[i], Style = (Style)Resources["CaptionStyle"], HorizontalAlignment = i == 0 || i == 1 ? HorizontalAlignment.Left : HorizontalAlignment.Right };
                    Grid.SetColumn(h, i);
                    grid.Children.Add(h);
                }
                return grid;
            }
            var symbol = pos.Symbol?.ToUpperInvariant() ?? "?";
            var open = new Button
            {
                Style = (Style)Resources["ListRowButtonStyle"],
                Content = new TextBlock { Text = symbol, FontFamily = new FontFamily("Consolas"), FontSize = 11.5, Foreground = Token("AccentBlueBrush") },
                Padding = new Thickness(2, 1, 2, 1),
                HorizontalAlignment = HorizontalAlignment.Left,
                ToolTip = "Open " + symbol + " in the symbol workspace (Buy/Sell there opens the Hedge Fund order panel).",
                IsEnabled = PlatformSymbol.IsMatch(symbol),
            };
            AutomationProperties.SetName(open, "Open " + symbol);
            open.Click += (_, _) => OpenSymbol(symbol, null, null, null, null);
            grid.Children.Add(open);
            var pnlBrush = pos.UnrealizedPnl is double u ? DirectionBrush(u) : Token("MutedForegroundBrush");
            var cells = new[]
            {
                (pos.Side ?? "—").ToUpperInvariant(),
                pos.Qty is double q ? q.ToString(Math.Abs(q - Math.Round(q)) < 1e-9 ? "N0" : "0.########", CultureInfo.InvariantCulture) : "—",
                pos.AvgPrice is double a ? FormatPrice(a) : "—",
                pos.LastPrice is double l ? FormatPrice(l) : "—",
                Money(pos.MarketValue),
                pos.UnrealizedPnl is double u2 ? Arrow(u2) + " " + SignedMoney(u2) : "—",
                pos.UnrealizedPct is double up ? SignedPct(up) : "—",
                SignedMoney(pos.RealizedPnl),
            };
            for (var i = 0; i < cells.Length; i++)
            {
                var cell = new TextBlock
                {
                    Text = cells[i],
                    FontFamily = new FontFamily("Consolas"),
                    FontSize = 11,
                    Foreground = i == 5 || i == 6 ? pnlBrush : Token("PrimaryForegroundBrush"),
                    HorizontalAlignment = i == 0 ? HorizontalAlignment.Left : HorizontalAlignment.Right,
                    VerticalAlignment = VerticalAlignment.Center,
                };
                if (i == 3) cell.ToolTip = "The HedgeFund engine's last mark (written at its runs) — not a live quote.";
                Grid.SetColumn(cell, i + 1);
                grid.Children.Add(cell);
            }
            var row = new StackPanel();
            row.Children.Add(grid);
            if (!string.IsNullOrWhiteSpace(pos.Reason))
                row.Children.Add(new TextBlock { Text = pos.Reason, FontSize = 10, Foreground = Token("MutedForegroundBrush"), Margin = new Thickness(4, 0, 0, 2), TextWrapping = TextWrapping.Wrap });
            AutomationProperties.SetName(row, symbol + ", " + (pos.Side ?? "") + ", quantity " + cells[1] + ", mark " + cells[3] + ", unrealized " +
                                              (pos.UnrealizedPnl is double u3 ? DirectionWord(u3) + " " + Money(Math.Abs(u3)) : "not available"));
            return row;
        }

        private static string Money(double? v) => v is double d ? "$" + d.ToString("N2", CultureInfo.InvariantCulture) : "—";

        private static string SignedMoney(double? v)
            => v is double d ? (d > 0 ? "+" : d < 0 ? "−" : "±") + "$" + Math.Abs(d).ToString("N2", CultureInfo.InvariantCulture) : "—";

        // =====================================================================================
        // Macro (reused Financial Newsroom views)
        // =====================================================================================

        private void MacroTab_Checked(object sender, RoutedEventArgs e)
        {
            if (MacroHost == null) return; // during InitializeComponent
            ShowMacroTab(ReferenceEquals(sender, MacroCalendarTab));
        }

        private void ShowMacroTab(bool calendar)
        {
            if (MacroPage.Visibility != Visibility.Visible) return;
            try
            {
                if (calendar)
                {
                    _calendarView ??= new EconomicCalendarView();
                    MacroHost.Content = _calendarView;
                }
                else
                {
                    _macroView ??= new MacroIndicatorsView();
                    MacroHost.Content = _macroView;
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: the Newsroom macro view could not be shown.", ex);
                MacroHost.Content = new TextBlock
                {
                    Text = "Macro unavailable: the Financial Newsroom view could not be created — see the Backend Log.",
                    Style = (Style)Resources["EmptyTitleStyle"],
                };
            }
        }

        // =====================================================================================
        // Buy / Sell hand-over (the existing manual paper order panel; nothing is submitted here)
        // =====================================================================================

        private void RenderOrderButtons()
        {
            var tab = _active;
            if (tab == null)
            {
                OrderButtonsPanel.Visibility = Visibility.Collapsed;
                return;
            }
            OrderButtonsPanel.Visibility = Visibility.Visible;
            string? reason = null;
            if (OrderHandOverRequested == null) reason = "The Hedge Fund order panel is not reachable from here.";
            else if (!FundAssets.IsValidSymbol(tab.Symbol) || tab.Symbol.Length > 14)
                reason = "The HedgeFund paper engine does not trade " + tab.Symbol + " (supported: US stock/ETF tickers and crypto pairs like BTC-USD).";
            foreach (var b in new[] { OrderBuyButton, OrderSellButton, OrderShortButton, OrderCoverButton })
            {
                var side = (string)b.Tag;
                b.IsEnabled = reason == null;
                ToolTipService.SetShowOnDisabled(b, true);
                var tip = reason ?? SideLabels[side] + " " + tab.Symbol + ": opens the Hedge Fund manual paper order panel pre-filled — enter the size there; " +
                          "the order is confirmed before it is sent (paper only, never live).";
                b.ToolTip = tip;
                AutomationProperties.SetName(b, SideLabels[side] + " " + tab.Symbol + (reason != null ? " — unavailable: " + reason : " — opens the paper order panel"));
            }
            OrderNoteText.Text = reason ?? "Paper · opens the Hedge Fund order panel";
        }

        private void OrderButton_Click(object sender, RoutedEventArgs e)
        {
            var tab = _active;
            if (tab == null || sender is not Button { Tag: string side } || !SideLabels.ContainsKey(side)) return;
            if (!FundAssets.IsValidSymbol(tab.Symbol)) return;
            // Research passes only {symbol, side}; the panel validates, confirms, submits (POST /fund/orders) and audits.
            OrderHandOverRequested?.Invoke(this, new ResearchOrderHandOverEventArgs(tab.Symbol, side));
        }

        // =====================================================================================
        // Chart markers (GET …/markers + GET /research/backtests/{run}/markers) → bridge setMarkers (revision 4)
        // =====================================================================================

        private TabMarkers MarkersOf(ResearchTab tab)
        {
            if (!_tabMarkers.TryGetValue(tab, out var m)) _tabMarkers[tab] = m = new TabMarkers();
            return m;
        }

        /// <summary>Called right after loadBars (LoadChartAsync): clears the old symbol's markers, loads this tab's when on.</summary>
        private void OnChartBarsLoadedMarkers(ResearchTab tab)
        {
            foreach (var gone in _tabMarkers.Keys.Where(k => !_tabs.Contains(k)).ToList()) _tabMarkers.Remove(gone);
            _chart.SetMarkers(null);
            var state = MarkersOf(tab);
            _syncingUi = true;
            try { MarkersToggle.IsChecked = state.On; }
            finally { _syncingUi = false; }
            MarkersToggle.Content = state.RunId != null ? "MARKERS + BACKTEST" : "MARKERS";
            if (!state.On)
            {
                SetMarkersNote(null, null);
                return;
            }
            _ = LoadMarkersAsync(tab, ++_markersGeneration);
        }

        private void MarkersToggle_Changed(object sender, RoutedEventArgs e)
        {
            if (_syncingUi || _active == null) return;
            var state = MarkersOf(_active);
            state.On = MarkersToggle.IsChecked == true;
            if (!state.On)
            {
                _markersGeneration++;
                _chart.SetMarkers(null);
                SetMarkersNote(null, null);
                return;
            }
            _ = LoadMarkersAsync(_active, ++_markersGeneration);
        }

        private async Task LoadMarkersAsync(ResearchTab tab, int generation)
        {
            SetMarkersNote("Loading markers…", null);
            string? from = _chart.OldestBarMs is long oldest ? ChartHost.ToIso(oldest) : null;
            var to = ChartHost.ToIso(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
            var notes = new List<string>();
            var inputs = new List<ChartMarkerInput>();
            var labels = new List<string>();
            try
            {
                var symbolTask = ResearchClient.GetMarkersAsync(tab.Symbol, from, to, "insider,calendar");
                var runId = MarkersOf(tab).RunId;
                var runTask = runId != null ? ResearchClient.GetBacktestMarkersAsync(runId, tab.Symbol) : null;
                var symbolResult = await symbolTask;
                var runResult = runTask != null ? await runTask : null;
                if (_shutDown || generation != _markersGeneration || !ReferenceEquals(tab, _active)) return;
                CollectMarkers(symbolResult, "Market markers", "m", inputs, labels, notes);
                if (runResult != null) CollectMarkers(runResult, "Backtest " + runId, "b", inputs, labels, notes);
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading chart markers failed.", ex);
                if (generation != _markersGeneration) return;
                notes.Add("Markers unavailable — see the Backend Log.");
            }
            var dropped = _chart.SetMarkers(inputs);
            if (dropped > 0) notes.Add(dropped + " marker(s) were invalid and not drawn (see the Backend Log).");
            if (inputs.Count > 0 && !_chart.SupportsMarkers)
                notes.Insert(0, inputs.Count + " marker(s) loaded, but this chart page cannot draw them yet (it needs bridge revision 4 — MR8 chart page pending).");
            else if (inputs.Count > 0)
                notes.Insert(0, inputs.Count + " marker(s) on the chart.");
            else
                notes.Insert(0, "No markers in the loaded range.");
            SetMarkersNote(string.Join(" ", notes), labels);
        }

        private static void CollectMarkers(PlatformResult<MarkersResponse> result, string what, string prefix, List<ChartMarkerInput> inputs,
                                           List<string> labels, List<string> notes)
        {
            if (!result.Ok || result.Value == null)
            {
                notes.Add(what + " unavailable: " + (result.Ok ? "empty response from the backend." : result.Message));
                return;
            }
            var v = result.Value;
            var i = 0;
            foreach (var item in v.Items ?? new List<MarkerItem>())
            {
                if (item.Kind == null || item.T == null) continue;
                var label = item.Label ?? item.Kind;
                inputs.Add(new ChartMarkerInput { Id = prefix + (i++).ToString(CultureInfo.InvariantCulture), T = item.T, TEnd = item.TEnd, Kind = item.Kind, Side = item.Side, Label = label });
                if (labels.Count < 25) labels.Add((LocalDateTime(item.T) ?? item.T) + " — " + label);
            }
            if (v.Truncated == true) notes.Add(what + ": more markers exist than are shown.");
            foreach (var kv in v.Unavailable ?? new Dictionary<string, string>())
                if (!string.IsNullOrWhiteSpace(kv.Value)) notes.Add(kv.Key + " unavailable: " + kv.Value);
        }

        private void SetMarkersNote(string? text, List<string>? labels)
        {
            MarkersNoteText.Text = text ?? "";
            MarkersNoteText.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
            MarkersNoteText.ToolTip = labels is { Count: > 0 } ? string.Join("\n", labels) : null;
            if (!string.IsNullOrEmpty(text)) AutomationProperties.SetName(MarkersNoteText, text + (labels is { Count: > 0 } ? " " + string.Join("; ", labels) : ""));
            if (!string.IsNullOrEmpty(text)) ChartChips.Visibility = Visibility.Visible;
        }

        /// <summary>
        /// "Open in Market Research" from a backtest report (HedgeFundWindow.OpenInResearch): opens the symbol and shows the
        /// run's entries/exits + attribution periods/drawdowns (and the market markers) on its chart. Older run cards show
        /// the backend's "unavailable" reason in the markers note.
        /// </summary>
        internal void OpenFromBacktest(string symbol, string? runId)
        {
            var sym = symbol.Trim().ToUpperInvariant().Replace('/', '-');
            if (!PlatformSymbol.IsMatch(sym))
            {
                ShowWorkspaceMessage("\"" + ClipText(symbol, 32) + "\" from the backtest is not a valid symbol.");
                return;
            }
            var existing = _tabs.FirstOrDefault(t => t.Symbol == sym);
            if (existing != null)
            {
                var m = MarkersOf(existing);
                m.On = true;
                m.RunId = runId;
            }
            OpenSymbol(sym, null, null, null, null);
            var tab = _tabs.FirstOrDefault(t => t.Symbol == sym);
            if (tab == null) return; // tab limit — OpenSymbol showed the message
            var state = MarkersOf(tab);
            state.On = true;
            state.RunId = runId;
            if (existing != null && ReferenceEquals(tab, _active) && _chart.HasSeries && _chart.Symbol == tab.Symbol) OnChartBarsLoadedMarkers(tab);
        }

        // =====================================================================================
        // "Use as universe" (GET /research/watchlists/{id}/universe → a NEW strategy draft in the Library's editor)
        // =====================================================================================

        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        private Task UseAsUniverseAsync(string listId) => DialogGuard.RunAsync("research-universe:" + listId, () => UseAsUniverseCoreAsync(listId));

        private async Task UseAsUniverseCoreAsync(string listId)
        {
            PlatformResult<WatchlistUniverse> result;
            try { result = await ResearchClient.GetWatchlistUniverseAsync(listId); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: building the universe failed.", ex);
                ShowWatchlistMessage("Universe not built — see the Backend Log.", retry: false);
                return;
            }
            if (_shutDown) return;
            if (!result.Ok || result.Value == null)
            {
                ShowWatchlistMessage("Universe not built: " + (result.Ok ? "empty response from the backend." : result.Message), retry: false);
                return;
            }
            var u = result.Value;
            var symbols = u.Universe?.Symbols?.Where(s => !string.IsNullOrWhiteSpace(s)).ToList() ?? new List<string>();
            if (symbols.Count == 0)
            {
                ShowWatchlistMessage("Universe not built: " + (u.Reason ?? "the list has no usable symbols."), retry: false);
                return;
            }
            string text;
            try
            {
                var root = JsonNode.Parse(StrategyEditorWindow.NewStrategyTemplate) as JsonObject
                           ?? throw new InvalidOperationException("strategy template is not a JSON object");
                var listName = u.Name ?? "watchlist";
                root["name"] = "New strategy (" + (listName.Length <= 40 ? listName : listName.Substring(0, 40)) + ")";
                var array = new JsonArray();
                foreach (var s in symbols) array.Add(s);
                root["universe"] = new JsonObject { ["symbols"] = array };
                text = root.ToJsonString(new JsonSerializerOptions { WriteIndented = true });
            }
            catch (Exception ex) when (ex is JsonException || ex is InvalidOperationException)
            {
                AppLog.Warn(LogModule, "Market Research: the strategy template could not be prepared.", ex);
                ShowWatchlistMessage("Universe not handed over: the strategy template could not be prepared — see the Backend Log.", retry: false);
                return;
            }
            try
            {
                new StrategyEditorWindow(Window.GetWindow(this), null, null, null, text).Show();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: the strategy editor could not be opened.", ex);
                ShowWatchlistMessage("The strategy editor could not be opened — see the Backend Log.", retry: false);
                return;
            }
            var excluded = (u.Excluded ?? new List<UniverseExcluded>()).Select(x => x.Symbol + " (" + x.Reason + ")").ToList();
            ShowWatchlistMessage("Opened a NEW strategy draft in the Library's editor with universe.symbols = " + symbols.Count + " symbol(s) of \"" +
                                 (u.Name ?? "the list") + "\" — nothing is saved until you save it there." +
                                 (excluded.Count > 0 ? " Excluded: " + string.Join(", ", excluded) + "." : "") +
                                 (string.IsNullOrWhiteSpace(u.Note) ? "" : " " + u.Note), retry: false);
        }
    }
}

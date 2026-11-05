using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Financial Newsroom → Disclosures → "Insider" tab (INSIDER-RET UI, 2026-10-25; until then the section "Insider Trades").
    /// Clarity rules (CONGRESS_TASK.md) + INSIDER_RET_API.md §8:
    ///  • compact rows from GET /newsroom/disclosures/insider: who, ticker, buy/sell, amount, reporting delay, ONE return chip
    ///    (DisclosureUi.ChipText); 10b5-1 / cluster / amendment badges;
    ///  • details ONLY on expand (GET …/insider/{id}): both start points, all horizons, 10b5-1 + its source, sector / ETF,
    ///    official document link (default browser, http/https only);
    ///  • default filters = open-market P/S, last 30 days; always "N of M shown · change filters" + Reset;
    ///  • no statistics here (they are on the Track record tab), no toasts (alerts live in Monitoring).
    /// Kept from the old section: the AI summary "✦ Sum up most notable" (POST /newsroom/insider-summary, labelled per the AI
    /// content rule) and the clusters banner, both on the live 5-day list (GET /newsroom/insider, polled every 10 s like before).
    /// </summary>
    public partial class InsiderTradesView : UserControl, INewsroomView
    {
        private const string LiveUrl = NewsroomBackend.BaseUrl + "/newsroom/insider";
        private const string SummaryUrl = NewsroomBackend.BaseUrl + "/newsroom/insider-summary";
        private static readonly TimeSpan LiveInterval = TimeSpan.FromSeconds(10);
        private static readonly TimeSpan ListInterval = TimeSpan.FromSeconds(60);
        private const int PageSize = 100;
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        // filters (defaults = the Clarity rule)
        private int _days = 30;
        private string _code = "P,S";
        private string _role = "";
        private string _plan = "all";
        private string _cluster = "all";
        private string _ticker = "";
        private bool _measurableOnly;
        private bool _amendments;
        private readonly List<Action> _resetActions = new();

        private readonly List<InsiderRow> _rows = new();
        private int _totalMatching;
        private int _totalAll;
        private readonly HashSet<string> _expanded = new(StringComparer.Ordinal);
        private readonly Dictionary<string, InsiderDetail> _details = new(StringComparer.Ordinal);
        private int _listGeneration;
        private bool _listLoading;
        private string _clusterSignature = "";
        private readonly DispatcherTimer _liveTimer;
        private readonly DispatcherTimer _listTimer;
        private readonly DispatcherTimer _tickerDebounce;
        private bool _shutDown;

        public InsiderTradesView()
        {
            InitializeComponent();
            ClustersList.ItemsSource = Array.Empty<ClusterViewModel>();
            // One-click AI analysis -> trigger labelled as AI (EU AI Act Art. 50).
            SummaryButton.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetHelpText(SummaryButton, AiContent.TriggerTooltip);
            SummaryCaveat.Text = AiContent.Caveat;
            BuildFilterBar();

            _liveTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = LiveInterval };
            _liveTimer.Tick += async (_, _) => { if (IsVisible) await LoadLiveAsync(); };
            _listTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = ListInterval };
            _listTimer.Tick += async (_, _) => { if (IsVisible) await LoadListAsync(silent: true, append: false); };
            _tickerDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(450) };
            _tickerDebounce.Tick += async (_, _) =>
            {
                _tickerDebounce.Stop();
                await LoadListAsync(silent: false, append: false);
            };
            IsVisibleChanged += async (_, e) =>
            {
                if (e.NewValue is not true || _shutDown) return;
                await LoadLiveAsync();
                await LoadListAsync(silent: _rows.Count > 0, append: false);
            };
            _liveTimer.Start();
            _listTimer.Start();
        }

        private async void View_Loaded(object sender, RoutedEventArgs e)
        {
            await LoadLiveAsync();
            await LoadListAsync(silent: _rows.Count > 0, append: false);
        }

        private void View_Unloaded(object sender, RoutedEventArgs e)
        {
        }

        /// <summary>Called by the newsroom window when it closes.</summary>
        public void Shutdown()
        {
            _shutDown = true;
            _liveTimer.Stop();
            _listTimer.Stop();
            _tickerDebounce.Stop();
        }

        private async void RefreshButton_Click(object sender, RoutedEventArgs e)
        {
            await LoadLiveAsync();
            await LoadListAsync(silent: false, append: false);
        }

        // =====================================================================================
        // Filter bar
        // =====================================================================================

        private void BuildFilterBar()
        {
            var wrap = new WrapPanel();
            void Seg(string label, (string, string, string?)[] options, string initial, Action<string> set)
            {
                wrap.Children.Add(DisclosureUi.Segmented(label, options, initial, v =>
                {
                    set(v);
                    _ = LoadListAsync(silent: false, append: false);
                }, out var select));
                _resetActions.Add(() => select(initial));
            }
            Seg("Period", new (string, string, string?)[] { ("7 d", "7", null), ("30 d", "30", null), ("90 d", "90", null), ("1 y", "365", null), ("2 y", "730", "The full 2-year history needs the backfill (status line above).") },
                "30", v => _days = int.Parse(v, Inv));
            Seg("Side", new (string, string, string?)[] { ("Buys + sells", "P,S", null), ("Buys", "P", "Open-market purchases (code P)"), ("Sells", "S", "Open-market sales (code S)") },
                "P,S", v => _code = v);
            Seg("Role", new (string, string, string?)[] { ("All", "", null), ("CEO", "CEO", null), ("CFO", "CFO", null), ("Director", "Director", null), ("10 % owner", "10pct", null), ("Other", "Other", null) },
                "", v => _role = v);
            Seg("10b5-1", new (string, string, string?)[] { ("All", "all", null), ("Planned", "planned", "Pre-planned under a Rule 10b5-1 trading plan"), ("Not planned", "unplanned", null) },
                "all", v => _plan = v);
            Seg("Cluster", new (string, string, string?)[] { ("All", "all", null), ("Clusters", "cluster", "≥ 2 insiders buying or ≥ 3 selling the same company within 14 days"), ("Single", "single", null) },
                "all", v => _cluster = v);

            var tickerPanel = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 14, 6), VerticalAlignment = VerticalAlignment.Center };
            tickerPanel.Children.Add(new TextBlock { Text = "Ticker", FontSize = 10.5, Foreground = FundPalette.Faint, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 6, 0) });
            var ticker = new TextBox
            {
                Width = 80, FontSize = 11, Padding = new Thickness(4, 1, 4, 1), CharacterCasing = CharacterCasing.Upper, MaxLength = 10,
                Background = DisclosureUi.CardBg, BorderBrush = DisclosureUi.CardBorder, Foreground = FundPalette.TextBright, CaretBrush = FundPalette.TextBright,
            };
            AutomationProperties.SetName(ticker, "Filter by ticker");
            ticker.TextChanged += (_, _) =>
            {
                _ticker = ticker.Text.Trim();
                _tickerDebounce.Stop();
                _tickerDebounce.Start();
            };
            _resetActions.Add(() => ticker.Text = "");
            tickerPanel.Children.Add(ticker);
            wrap.Children.Add(tickerPanel);

            CheckBox Check(string text, string tip, Action<bool> set)
            {
                var cb = new CheckBox { Content = text, FontSize = 11, Foreground = FundPalette.Text, Margin = new Thickness(0, 0, 14, 6), VerticalAlignment = VerticalAlignment.Center, ToolTip = tip };
                cb.Checked += (_, _) => { set(true); _ = LoadListAsync(silent: false, append: false); };
                cb.Unchecked += (_, _) => { set(false); _ = LoadListAsync(silent: false, append: false); };
                _resetActions.Add(() => cb.IsChecked = false);
                return cb;
            }
            wrap.Children.Add(Check("Measurable only", "Only trades with a ticker that can be priced (others show the reason)", v => _measurableOnly = v));
            wrap.Children.Add(Check("Show amendments", "Form 4/A amendments are shown with a badge but never measured (no double counting)", v => _amendments = v));
            FilterHost.Child = wrap;
        }

        private async void ResetFiltersButton_Click(object sender, RoutedEventArgs e)
        {
            _days = 30; _code = "P,S"; _role = ""; _plan = "all"; _cluster = "all"; _ticker = ""; _measurableOnly = false; _amendments = false;
            foreach (var a in _resetActions) a();   // the controls follow silently (no reload per control)
            _tickerDebounce.Stop();
            await LoadListAsync(silent: false, append: false);
        }

        private string Query(int offset)
        {
            var q = new StringBuilder();
            q.Append("days=").Append(_days.ToString(Inv)).Append("&code=").Append(Uri.EscapeDataString(_code));
            if (_role.Length > 0) q.Append("&role=").Append(Uri.EscapeDataString(_role));
            q.Append("&plan=").Append(_plan).Append("&cluster=").Append(_cluster);
            if (_ticker.Length > 0) q.Append("&ticker=").Append(Uri.EscapeDataString(_ticker));
            q.Append("&measurable=").Append(_measurableOnly ? "yes" : "all").Append("&amendments=").Append(_amendments ? "show" : "hide");
            q.Append("&limit=").Append(PageSize.ToString(Inv)).Append("&offset=").Append(offset.ToString(Inv));
            return q.ToString();
        }

        // =====================================================================================
        // Disclosures list
        // =====================================================================================

        private async Task LoadListAsync(bool silent, bool append)
        {
            if (_shutDown || (_listLoading && silent)) return;
            var generation = ++_listGeneration;
            _listLoading = true;
            if (!silent && !append) ShowStatus("Loading insider trades…");
            try
            {
                var result = await DisclosuresClient.InsiderListAsync(Query(append ? _rows.Count : 0));
                if (generation != _listGeneration || _shutDown) return;
                if (!result.Ok)
                {
                    if (silent && _rows.Count > 0)
                        LastUpdatedText.Text = "Refresh failed at " + DateTime.Now.ToString("HH:mm:ss", Inv) + " (" + result.Problem + ") — showing the last list";
                    else
                        ShowStatus("Insider trades not available: " + result.Problem + ".\n\nThe HedgeFund backend starts with the Hedge Fund window; this view retries on its own.");
                    return;
                }
                var v = result.Value!;
                if (!append) _rows.Clear();
                _rows.AddRange(v.Rows ?? new List<InsiderRow>());
                _totalMatching = v.TotalMatching;
                _totalAll = v.TotalAll;
                var src = v.Source;
                LastUpdatedText.Text = "Updated " + DateTime.Now.ToString("HH:mm:ss", Inv) + " · returns: daily job "
                                       + (src?.LastDailyJob != null ? "last run " + DisclosureUi.LocalTime(src.LastDailyJob) : "not run yet")
                                       + (src?.DailyJobState is string djs && djs != "done" ? " (" + djs + ")" : "")
                                       + " · history: " + (src?.Backfill ?? "unknown");
                RenderRows(keepScroll: silent || append);
            }
            finally
            {
                if (generation == _listGeneration) _listLoading = false;
            }
        }

        private void RenderRows(bool keepScroll)
        {
            var offset = ListScrollViewer.VerticalOffset;
            RowsPanel.Children.Clear();
            var shown = _rows.Count;
            CountText.Text = shown == _totalMatching
                ? $"{_totalMatching.ToString("N0", Inv)} of {_totalAll.ToString("N0", Inv)} shown · change filters"
                : $"{shown.ToString("N0", Inv)} of {_totalMatching.ToString("N0", Inv)} matching shown ({_totalAll.ToString("N0", Inv)} stored) · change filters";
            if (_rows.Count == 0)
            {
                ShowStatus(_totalAll == 0
                    ? "No insider trades stored yet. New Form 4 filings arrive with the SEC EDGAR collector; the 2-year history loads with \"Start backfill\" above."
                    : "No trades match the filters — " + _totalAll.ToString("N0", Inv) + " stored. Change or reset the filters.");
                return;
            }
            foreach (var r in _rows) RowsPanel.Children.Add(BuildCard(r));
            if (_rows.Count < _totalMatching)
            {
                var more = new Button
                {
                    Content = "Load " + Math.Min(PageSize, _totalMatching - _rows.Count).ToString(Inv) + " more",
                    Template = (ControlTemplate)FindResource("RefreshButtonTemplate"),
                    Foreground = DisclosureUi.InsiderBrush, FontSize = 11, HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 4, 0, 8),
                };
                more.Click += async (_, _) => await LoadListAsync(silent: false, append: true);
                RowsPanel.Children.Add(more);
            }
            ShowList();
            if (keepScroll) ListScrollViewer.ScrollToVerticalOffset(offset);
            else ListScrollViewer.ScrollToTop();
        }

        private Border BuildCard(InsiderRow r)
        {
            var buy = r.Code == "P";
            var sideBrush = buy ? DisclosureUi.InsiderBrush : DisclosureUi.InsiderSellBrush;
            var grid = new Grid { Margin = new Thickness(10, 7, 10, 7) };
            foreach (var w in new[] { 48.0, 70.0 }) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(w) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            for (var i = 0; i < 4; i++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            void Put(UIElement e, int col)
            {
                Grid.SetColumn(e, col);
                grid.Children.Add(e);
            }
            var side = new Border
            {
                BorderBrush = sideBrush, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(3), Padding = new Thickness(4, 0, 4, 1),
                HorizontalAlignment = HorizontalAlignment.Left, VerticalAlignment = VerticalAlignment.Center,
                Child = new TextBlock { Text = buy ? "BUY" : "SELL", FontSize = 9.5, FontWeight = FontWeights.SemiBold, Foreground = sideBrush },
            };
            Put(side, 0);
            Put(DisclosureUi.Text(r.Ticker ?? "—", 12.5, FundPalette.TextBright, bold: true, mono: true, wrap: false), 1);
            var who = DisclosureUi.Text((r.OwnerName ?? "unknown insider") + (string.IsNullOrWhiteSpace(r.RoleClass) ? "" : " · " + r.RoleClass)
                                        + (string.IsNullOrWhiteSpace(r.IssuerName) ? "" : " · " + r.IssuerName), 11.5, FundPalette.Text, wrap: false);
            who.TextTrimming = TextTrimming.CharacterEllipsis;
            who.VerticalAlignment = VerticalAlignment.Center;
            who.ToolTip = who.Text;
            Put(who, 2);
            var amount = DisclosureUi.Text(DisclosureUi.Money(r.ValueUsd), 11.5, FundPalette.TextBright, mono: true, wrap: false);
            amount.Margin = new Thickness(10, 0, 0, 0);
            amount.VerticalAlignment = VerticalAlignment.Center;
            amount.ToolTip = (r.Shares is double sh ? sh.ToString("N0", Inv) + " shares · " : "") + "value as reported (shares × price)";
            Put(amount, 3);
            var delayText = r.ReportingDelayDays is int d ? "filed +" + d.ToString(Inv) + " d" : "filed " + (r.FilingDate ?? "—");
            var delay = DisclosureUi.Text(delayText, 10.5, FundPalette.Dim, mono: true, wrap: false);
            delay.Margin = new Thickness(10, 0, 0, 0);
            delay.VerticalAlignment = VerticalAlignment.Center;
            delay.ToolTip = "Trade " + (r.TradeDateFirst ?? "—") + " → accepted by EDGAR " + DisclosureUi.LocalTime(r.AcceptedAt)
                            + (r.ReportingDelayDays is int dd ? " (" + dd.ToString(Inv) + " calendar days later)" : "");
            Put(delay, 4);
            var badges = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            if (r.Plan10b51) badges.Children.Add(DisclosureUi.Badge("10b5-1", DisclosureUi.ChipBorder, "Pre-planned trade under a Rule 10b5-1 trading plan: scheduled months in advance, so it says little about the insider's current view."));
            if (r.Cluster) badges.Children.Add(DisclosureUi.Badge("cluster", sideBrush, "Part of a cluster: ≥ 2 different insiders buying or ≥ 3 selling (unplanned) this company within 14 days."));
            if (r.Form == "4/A") badges.Children.Add(DisclosureUi.Badge("amendment", DisclosureUi.ChipBorder, "Form 4/A — shown, but not measured (the original is), so a trade is never counted twice."));
            Put(badges, 5);
            var (chipText, chipTip) = DisclosureUi.ChipText(r);
            var chip = new Border
            {
                BorderBrush = DisclosureUi.ChipBorder, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(10), Padding = new Thickness(8, 1, 8, 2),
                Margin = new Thickness(10, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center, ToolTip = chipTip,
                Child = DisclosureUi.Text(chipText, 10.5, r.Chip?.State == "complete" ? FundPalette.TextBright : FundPalette.Dim, mono: true, wrap: false),
            };
            Put(chip, 6);

            var detailHost = new StackPanel { Margin = new Thickness(10, 0, 10, 10), Visibility = Visibility.Collapsed };
            var header = DisclosureUi.FlatToggle(grid, (buy ? "Purchase" : "Sale") + " " + (r.Ticker ?? "") + " by " + (r.OwnerName ?? "unknown insider")
                                                       + ", " + DisclosureUi.Money(r.ValueUsd) + ", " + delayText + ", return " + chipText + ". Press to show details.");
            header.Checked += async (_, _) =>
            {
                _expanded.Add(r.Id);
                detailHost.Visibility = Visibility.Visible;
                await ShowDetailAsync(r.Id, detailHost, refresh: true);
            };
            header.Unchecked += (_, _) =>
            {
                _expanded.Remove(r.Id);
                detailHost.Visibility = Visibility.Collapsed;
            };
            var stack = new StackPanel();
            stack.Children.Add(header);
            stack.Children.Add(detailHost);
            if (_expanded.Contains(r.Id))
            {
                header.IsChecked = true;   // re-render after a refresh: open again (the Checked handler reloads the details)
            }
            return new Border
            {
                Background = DisclosureUi.CardBg, BorderBrush = DisclosureUi.CardBorder, BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6), Margin = new Thickness(0, 0, 0, 6), Child = stack,
            };
        }

        private async Task ShowDetailAsync(string id, StackPanel host, bool refresh)
        {
            if (_details.TryGetValue(id, out var cached)) RenderDetail(cached, host);
            else
            {
                host.Children.Clear();
                host.Children.Add(DisclosureUi.Text("Loading details…", 11, FundPalette.Dim));
            }
            if (!refresh && cached != null) return;
            var result = await DisclosuresClient.InsiderDetailAsync(id);
            if (_shutDown) return;
            if (!result.Ok)
            {
                if (cached == null)
                {
                    host.Children.Clear();
                    host.Children.Add(DisclosureUi.Text("Details not available: " + result.Problem, 11, FundPalette.Risk));
                }
                return;
            }
            _details[id] = result.Value!;
            RenderDetail(result.Value!, host);
        }

        private static void RenderDetail(InsiderDetail d, StackPanel host)
        {
            host.Children.Clear();
            host.Children.Add(new Border { BorderBrush = DisclosureUi.CardBorder, BorderThickness = new Thickness(0, 1, 0, 0), Margin = new Thickness(0, 0, 0, 6) });
            string Line(params string?[] parts) => string.Join(" · ", parts.Where(p => !string.IsNullOrWhiteSpace(p)));
            host.Children.Add(DisclosureUi.Text(Line(d.IssuerName, d.IssuerCik != null ? "CIK " + d.IssuerCik : null,
                                                     d.RolesRaw != null ? "roles: " + d.RolesRaw : null,
                                                     d.CoReporters is int co && co > 0 ? "joint filing with " + co.ToString(Inv) + " more reporting owner(s)" : null), 11, FundPalette.Text));
            host.Children.Add(DisclosureUi.Text(Line(
                (d.Shares is double sh ? sh.ToString("N0", Inv) + " shares" : null) + (d.AvgPrice is double ap ? " @ $" + ap.ToString("N2", Inv) + " (as reported, not used for returns)" : ""),
                "value " + DisclosureUi.Money(d.ValueUsd),
                "traded " + (d.TradeDateFirst ?? "—") + (d.TradeDateLast != null && d.TradeDateLast != d.TradeDateFirst ? " – " + d.TradeDateLast : ""),
                "accepted " + DisclosureUi.LocalTime(d.AcceptedAt), "Form " + (d.Form ?? "?")), 11, FundPalette.Dim));
            host.Children.Add(DisclosureUi.Text("Rule 10b5-1 plan: " + (d.Plan10b51 ? "yes" + (d.PlanSource != null ? " (from the " + d.PlanSource + ")" : "") : "no"), 11, FundPalette.Dim));
            host.Children.Add(DisclosureUi.Text("Sector: " + (d.Sic is int sic ? "SIC " + sic.ToString(Inv) + (d.SicDescription != null ? " " + d.SicDescription : "") : "SIC unknown")
                                                + " → " + (d.Etf ?? "no sector ETF" + (d.EtfReason != null ? " (" + d.EtfReason + ")" : "")), 11, FundPalette.Dim));
            if (!d.Measurable) host.Children.Add(DisclosureUi.Text("Not measurable: " + (d.NotMeasurableReason ?? "no reason given"), 11, FundPalette.Risk));

            host.Children.Add(ReturnsBlock("From the filing (tradable)", d.Returns?.Filing, main: true));
            host.Children.Add(ReturnsBlock("From the trade date — not tradable (insider's own timing)", d.Returns?.TradeDate, main: false));
            foreach (var n in d.Notes ?? new List<string>())
                host.Children.Add(DisclosureUi.Text(n, 10, FundPalette.Faint));
            if (!string.IsNullOrWhiteSpace(d.OfficialUrl)) host.Children.Add(DisclosureUi.Link("Open filing on SEC.gov ↗", d.OfficialUrl!));
        }

        /// <summary>Returns table of one start point (shared with the Congress tab).</summary>
        internal static FrameworkElement ReturnsBlock(string title, StartReturns? s, bool main)
        {
            var size = main ? 11.0 : 10.0;
            var dim = main ? FundPalette.Text : FundPalette.Faint;
            var panel = new StackPanel { Margin = new Thickness(0, 8, 0, 0), Opacity = main ? 1 : 0.85 };
            panel.Children.Add(DisclosureUi.Text(title, size, main ? FundPalette.TextBright : FundPalette.Dim, bold: main));
            if (s == null)
            {
                panel.Children.Add(DisclosureUi.Text("Not computed yet (the daily job runs after the US close).", size, FundPalette.Faint));
                return panel;
            }
            panel.Children.Add(DisclosureUi.Text("Entry: " + DisclosureUi.EntryBasisText(s.EntryBasis) + (s.EntryTs != null ? " · " + DisclosureUi.LocalTime(s.EntryTs) : "")
                                                 + (s.EntryPrice is double ep ? " @ $" + ep.ToString("N2", Inv) : "")
                                                 + (s.Etf != null ? " · benchmark " + s.Etf + (s.EtfEntryPrice is double ee ? " @ $" + ee.ToString("N2", Inv) : "") : ""), size, FundPalette.Dim));
            var grid = new Grid { Margin = new Thickness(0, 4, 0, 0) };
            foreach (var w in new[] { 70.0, 150.0, 80.0, 80.0, 80.0, 80.0 }) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(w) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var row = 0;
            void Add(string[] cells, bool head)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                for (var c = 0; c < cells.Length; c++)
                {
                    var t = DisclosureUi.Text(cells[c], size, head ? FundPalette.Faint : dim, bold: head, mono: !head && c >= 2 && c <= 5, wrap: c == 6);
                    t.Margin = new Thickness(0, 1, 8, 1);
                    Grid.SetRow(t, row);
                    Grid.SetColumn(t, c);
                    grid.Children.Add(t);
                }
                row++;
            }
            Add(new[] { "Horizon", "State", "Stock", "ETF", "Excess", "Score", "" }, head: true);
            foreach (var h in s.Horizons ?? new List<HorizonReturn>())
            {
                var state = h.State switch
                {
                    "complete" => "complete" + (h.ExitDate != null ? " (" + h.ExitDate + ")" : ""),
                    "running" => "running (" + (h.SessionsDone ?? 0).ToString(Inv) + " of " + h.H.ToString(Inv) + " d)",
                    _ => "not measurable",
                };
                Add(new[] { h.H.ToString(Inv) + " d", state, DisclosureUi.Pct(h.RStock), DisclosureUi.Pct(h.REtf), DisclosureUi.Pct(h.Excess), DisclosureUi.Pct(h.Score), h.Reason ?? "" }, head: false);
            }
            panel.Children.Add(grid);
            return panel;
        }

        // =====================================================================================
        // Live 5-day list: clusters banner + collector state (the AI summary reads the same list in the backend)
        // =====================================================================================

        private async Task LoadLiveAsync()
        {
            if (_shutDown) return;
            try
            {
                using var client = NewsroomBackend.CreateClient(TimeSpan.FromSeconds(10));
                using var doc = JsonDocument.Parse(await client.GetStringAsync(LiveUrl));
                var root = doc.RootElement;
                UpdateClusters(root);
                CollectorWarningText.Text = CollectorProblem(root) ?? "";
                CollectorWarningText.Visibility = CollectorWarningText.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
            }
            catch (Exception ex) when (ex is HttpRequestException || ex is TaskCanceledException || ex is JsonException)
            {
                CollectorWarningText.Text = "Live Form 4 list not available (" + ex.GetType().Name + ") — clusters and the AI summary need the HedgeFund backend (status: " + NewsroomBackend.Status + ").";
                CollectorWarningText.Visibility = Visibility.Visible;
            }
        }

        private static string? CollectorProblem(JsonElement root)
        {
            if (!root.TryGetProperty("status", out var st) || st.ValueKind != JsonValueKind.Object) return null;
            var failed = st.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.False;
            var text = st.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() : null;
            return failed && !string.IsNullOrWhiteSpace(text)
                ? "SEC EDGAR collector: " + text + " — it retries on its own; if it never loads, check the SEC EDGAR user agent (name + e-mail) in ⚙ Keys & data."
                : null;
        }

        private void UpdateClusters(JsonElement root)
        {
            var clusters = root.TryGetProperty("clusters", out var arr) && arr.ValueKind == JsonValueKind.Array
                ? arr.EnumerateArray().Select(ClusterViewModel.FromJson).ToList()
                : new List<ClusterViewModel>();
            var signature = string.Join("|", clusters.Select(c => c.Text));
            if (signature == _clusterSignature) return; // no rebuild every 10s
            _clusterSignature = signature;
            ClustersList.ItemsSource = clusters;
            ClustersBanner.Visibility = clusters.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        }

        // "Sum up most notable": the backend builds a compact, pre-ranked table of
        // the filings it has in memory (see backend/newsroom/insider_summary.py)
        // and asks OpenRouter to summarize it.
        private async void SummaryButton_Click(object sender, RoutedEventArgs e)
        {
            SummaryButton.IsEnabled = false;
            SummaryCard.Visibility = Visibility.Visible;
            SummaryCaveat.Visibility = Visibility.Collapsed;
            SummaryCost.Visibility = Visibility.Collapsed;
            SummaryHeader.Text = AiContent.LoadingBadge;
            SummaryText.Text = "Asking the language model (OpenRouter)…";
            AutomationProperties.SetName(SummaryText, AiContent.LoadingAutomationName("summary"));

            try
            {
                using var client = NewsroomBackend.CreateClient(TimeSpan.FromSeconds(75));
                using var response = await client.PostAsync(SummaryUrl, new StringContent("{}", Encoding.UTF8, "application/json"));
                using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                var root = doc.RootElement;

                if (response.IsSuccessStatusCode && root.TryGetProperty("summary", out var summary))
                {
                    var count = root.TryGetProperty("filings", out var f) && f.ValueKind == JsonValueKind.Number ? f.GetInt32() : 0;
                    // Machine-readable marking from the backend (newsroom/ai.py provenance()).
                    AiContent.TryReadProvenance(root, out var model, out var generatedAt);
                    generatedAt ??= DateTimeOffset.Now;
                    var badge = AiContent.Badge(model, generatedAt).ToUpperInvariant();
                    SummaryHeader.Text = count > 0
                        ? $"{badge} · MOST NOTABLE OF {count} FILINGS"
                        : $"{badge} · MOST NOTABLE TRADES";
                    SummaryText.Text = summary.GetString() ?? "";
                    SummaryCaveat.Visibility = Visibility.Visible;
                    // Cost of this request as reported by OpenRouter (usage.cost; credits = USD).
                    double? costUsd = null;
                    long? promptTokens = null, completionTokens = null;
                    if (root.TryGetProperty("usage", out var us) && us.ValueKind == JsonValueKind.Object)
                    {
                        if (us.TryGetProperty("cost_usd", out var c) && c.ValueKind == JsonValueKind.Number) costUsd = c.GetDouble();
                        if (us.TryGetProperty("prompt_tokens", out var pt) && pt.ValueKind == JsonValueKind.Number) promptTokens = pt.GetInt64();
                        if (us.TryGetProperty("completion_tokens", out var ct) && ct.ValueKind == JsonValueKind.Number) completionTokens = ct.GetInt64();
                    }
                    SummaryCost.Text = CostLine(costUsd, promptTokens, completionTokens);
                    SummaryCost.Visibility = Visibility.Visible;
                    AutomationProperties.SetName(SummaryText, AiContent.AutomationName(model, generatedAt));
                }
                else
                {
                    SummaryHeader.Text = AiContent.UnavailableBadge;
                    SummaryCost.Visibility = Visibility.Collapsed;
                    AutomationProperties.SetName(SummaryText, string.Empty);
                    SummaryText.Text = root.TryGetProperty("error", out var err)
                        ? err.GetString() ?? "Unknown error."
                        : $"Backend answered HTTP {(int)response.StatusCode}.";
                }
            }
            catch (Exception ex)
            {
                SummaryHeader.Text = AiContent.UnavailableBadge;
                SummaryCost.Visibility = Visibility.Collapsed;
                AutomationProperties.SetName(SummaryText, string.Empty);
                SummaryText.Text = "Could not reach the HedgeFund backend (" + ex.Message + "). Status: " + NewsroomBackend.Status;
            }
            finally
            {
                SummaryButton.IsEnabled = true;
            }
        }

        /// <summary>"Cost: $0.0123 · 1,512 in / 874 out tokens (charged by OpenRouter)" — same format as AnalysisReasoningView.</summary>
        private static string CostLine(double? costUsd, long? promptTokens, long? completionTokens)
        {
            var cost = costUsd == null
                ? "Cost: not reported by OpenRouter"
                : "Cost: $" + costUsd.Value.ToString(costUsd.Value < 0.01 ? "0.0000" : "0.000", CultureInfo.InvariantCulture);
            var tokens = promptTokens != null && completionTokens != null
                ? $" · {promptTokens.Value.ToString("N0", CultureInfo.InvariantCulture)} in / " +
                  $"{completionTokens.Value.ToString("N0", CultureInfo.InvariantCulture)} out tokens"
                : "";
            return cost + tokens + (costUsd == null ? "" : " (charged by OpenRouter)");
        }

        private void ShowStatus(string message)
        {
            StatusText.Text = message;
            StatusPanel.Visibility = Visibility.Visible;
            ListScrollViewer.Visibility = Visibility.Collapsed;
        }

        private void ShowList()
        {
            StatusPanel.Visibility = Visibility.Collapsed;
            ListScrollViewer.Visibility = Visibility.Visible;
        }

        /// <summary>One line of the clusters banner, e.g.
        /// "◆ Cluster buy: 3 insiders bought Acme Corp (ACME) · $1.2M · Sep 10–Sep 18".</summary>
        private sealed class ClusterViewModel
        {
            public string Text { get; init; } = "";
            public string Tooltip { get; init; } = "";
            public bool IsBuy { get; init; }

            private static string Str(JsonElement e, string name) =>
                e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

            private static string Money(double v) =>
                v >= 1_000_000 ? $"${v / 1_000_000:0.0}M"
                : v >= 1_000 ? $"${v / 1_000:0}K"
                : $"${v:0}";

            private static string ShortDate(string iso) =>
                DateTime.TryParseExact(iso, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var d)
                    ? d.ToString("MMM d", CultureInfo.InvariantCulture)
                    : iso;

            public static ClusterViewModel FromJson(JsonElement e)
            {
                var isBuy = Str(e, "kind") == "buy";
                var count = e.TryGetProperty("count", out var c) && c.ValueKind == JsonValueKind.Number ? c.GetInt32() : 0;
                var total = e.TryGetProperty("total_usd", out var t) && t.ValueKind == JsonValueKind.Number ? t.GetDouble() : 0;
                var names = e.TryGetProperty("insiders", out var n) && n.ValueKind == JsonValueKind.Array
                    ? n.EnumerateArray().Where(x => x.ValueKind == JsonValueKind.String).Select(x => x.GetString() ?? "").ToList()
                    : new List<string>();
                var company = Str(e, "issuer_name");
                var ticker = Str(e, "issuer_ticker");
                var who = string.IsNullOrWhiteSpace(company) ? (string.IsNullOrWhiteSpace(ticker) ? "unknown issuer" : ticker)
                    : string.IsNullOrWhiteSpace(ticker) ? company : $"{company} ({ticker})";
                var first = ShortDate(Str(e, "first_date"));
                var last = ShortDate(Str(e, "last_date"));
                var dates = first == last ? first : $"{first}–{last}";
                var verb = isBuy ? "bought" : "sold";
                var parts = new List<string> { $"◆ Cluster {(isBuy ? "buy" : "sell")}: {count} insiders {verb} {who}" };
                if (total > 0) parts.Add(Money(total));
                if (!string.IsNullOrWhiteSpace(dates)) parts.Add(dates);
                return new ClusterViewModel
                {
                    Text = string.Join(" · ", parts),
                    Tooltip = (isBuy ? "Open-market purchases" : "Open-market sales (Rule 10b5-1 plans excluded)") +
                              " by different insiders within 14 days (live 5-day list): " + string.Join(", ", names),
                    IsBuy = isBuy,
                };
            }
        }
    }
}

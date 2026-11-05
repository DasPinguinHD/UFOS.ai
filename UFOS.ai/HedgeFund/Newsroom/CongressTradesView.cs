using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Financial Newsroom → Disclosures → "Congress" tab (CONGRESS_API.md §7, Clarity rules of CONGRESS_TASK.md; H's slice a):
    ///  • info line: the source terms (once) + its freshness; stale → "source not updated since &lt;date&gt;" (no fallback source);
    ///  • compact rows from GET /newsroom/disclosures/congress: who (name · chamber · party-state, owner if not self), ticker,
    ///    buy/sell, amount range, "filed +N d" with a LATE badge (&gt; 45 days), ONE return chip (null until slice b);
    ///  • details only on expand (GET …/congress/{id}): asset / owner / filing type, comment, office, both start points, the
    ///    official House / Senate document (default browser, http/https only), "only this member" filter;
    ///  • default filters = open-market P/S, last 30 days; "N of M shown · change filters" + Reset;
    ///  • watch list (slice b): GET /members?q= + PUT /watch — each tick saves at once (no confirmation); a new filing of a
    ///    watched member raises an alert in Monitoring → Alerts only (no toast, decided by the user); rows of watched members
    ///    carry a "watched" badge.
    /// Same card layout / filter bar / chip as the Insider tab (DisclosureUi); fixed congress colour (teal).
    /// </summary>
    internal sealed class CongressTradesView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan ListInterval = TimeSpan.FromSeconds(120);
        private const int PageSize = 100;
        private const string TradeDateCaption = "From the transaction date — not tradable (the member's own timing)";

        private int _days = 30;
        private string _code = "P,S";
        private string _chamber = "all";
        private string _party = "all";
        private string _late = "all";
        private string _ticker = "";
        private string _member = "";
        private string _memberName = "";
        private bool _measurableOnly;
        private readonly List<Action> _resetActions = new();

        private readonly List<CongressRow> _rows = new();
        private int _totalMatching;
        private int _totalAll;
        private readonly HashSet<string> _expanded = new(StringComparer.Ordinal);
        private readonly Dictionary<string, CongressDetail> _details = new(StringComparer.Ordinal);
        private int _generation;
        private bool _loading;
        private bool _shutDown;

        private readonly TextBlock _infoText = DisclosureUi.Text("", 10.5, FundPalette.Faint);
        private readonly TextBlock _staleText = DisclosureUi.Text("", 10.5, FundPalette.Risk);
        private readonly TextBlock _countText = DisclosureUi.Text("", 11, FundPalette.Text, mono: true);
        private readonly Border _memberChip = new() { Visibility = Visibility.Collapsed };
        private readonly StackPanel _rowsPanel = new();
        private readonly ScrollViewer _scroll;
        private readonly TextBlock _statusText = DisclosureUi.Text("Loading congress trades…", 12, FundPalette.Text);
        private readonly DispatcherTimer _listTimer;
        private readonly DispatcherTimer _tickerDebounce;

        // watch list (slice b)
        private readonly HashSet<string> _watched = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _memberNames = new(StringComparer.Ordinal);
        private readonly TextBlock _watchSummary = DisclosureUi.Text("Watch list: loading…", 10.5, FundPalette.Faint);
        private readonly TextBlock _watchProblem = DisclosureUi.Text("", 10.5, FundPalette.Risk);
        private readonly Border _watchEditor = new() { Visibility = Visibility.Collapsed };
        private readonly StackPanel _watchRows = new();
        private readonly TextBox _watchSearch = new();
        private DispatcherTimer? _watchDebounce;
        private int _watchGeneration;
        private bool _watchSaving;
        private bool _watchLoaded;

        public CongressTradesView()
        {
            var root = new Grid();
            for (var i = 0; i < 5; i++) root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            var info = new StackPanel();
            var head = new StackPanel { Orientation = Orientation.Horizontal };
            head.Children.Add(new System.Windows.Shapes.Ellipse { Width = 7, Height = 7, Fill = DisclosureUi.CongressBrush, Margin = new Thickness(0, 0, 6, 0), VerticalAlignment = VerticalAlignment.Center });
            head.Children.Add(DisclosureUi.Text("STOCK Act trades of House and Senate members — return after the filing vs the sector ETF. Information, not a recommendation.", 10.5, FundPalette.Faint));
            info.Children.Add(head);
            info.Children.Add(_infoText);
            _staleText.Visibility = Visibility.Collapsed;
            info.Children.Add(_staleText);
            Put(root, info, 0);

            Put(root, BuildWatchList(), 1);

            Put(root, BuildFilterBar(), 2);

            var countRow = new DockPanel { LastChildFill = true, Margin = new Thickness(0, 4, 0, 0) };
            var reset = FundUi.MakeButton("Reset filters");
            reset.Padding = new Thickness(10, 1, 10, 1);
            reset.MinHeight = 22;
            reset.FontSize = 10.5;
            reset.ToolTip = "Back to the defaults: purchases and sales, last 30 days, both chambers, all parties";
            reset.Click += async (_, _) => await ResetAsync();
            DockPanel.SetDock(reset, Dock.Right);
            countRow.Children.Add(reset);
            _memberChip.VerticalAlignment = VerticalAlignment.Center;
            DockPanel.SetDock(_memberChip, Dock.Right);
            countRow.Children.Add(_memberChip);
            _countText.VerticalAlignment = VerticalAlignment.Center;
            AutomationProperties.SetLiveSetting(_countText, AutomationLiveSetting.Polite);
            countRow.Children.Add(_countText);
            Put(root, countRow, 3);

            var listGrid = new Grid { Margin = new Thickness(0, 10, 0, 0) };
            _scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = _rowsPanel };
            AutomationProperties.SetName(_rowsPanel, "Congress trades, newest filing first");
            listGrid.Children.Add(_scroll);
            _statusText.HorizontalAlignment = HorizontalAlignment.Center;
            _statusText.VerticalAlignment = VerticalAlignment.Center;
            _statusText.MaxWidth = 520;
            _statusText.TextAlignment = TextAlignment.Center;
            listGrid.Children.Add(_statusText);
            Put(root, listGrid, 5);
            Content = root;

            _listTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = ListInterval };
            _listTimer.Tick += async (_, _) => { if (IsVisible) await LoadAsync(silent: true, append: false); };
            _tickerDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(450) };
            _tickerDebounce.Tick += async (_, _) =>
            {
                _tickerDebounce.Stop();
                await LoadAsync(silent: false, append: false);
            };
            IsVisibleChanged += async (_, e) =>
            {
                if (e.NewValue is not true || _shutDown) return;
                if (!_watchLoaded) await LoadMembersAsync();
                await LoadAsync(silent: _rows.Count > 0, append: false);
            };
            _listTimer.Start();
        }

        public void Shutdown()
        {
            _shutDown = true;
            _listTimer.Stop();
            _tickerDebounce.Stop();
            _watchDebounce?.Stop();
        }

        // =====================================================================================
        // Watch list (GET /members?q=, PUT /watch) — alerts only in Monitoring → Alerts
        // =====================================================================================

        private FrameworkElement BuildWatchList()
        {
            var panel = new StackPanel { Margin = new Thickness(0, 6, 0, 0) };
            var row = new DockPanel { LastChildFill = true };
            var toggle = FundUi.MakeButton("Watch list…");
            toggle.Padding = new Thickness(10, 2, 10, 2);
            toggle.MinHeight = 22;
            toggle.FontSize = 11;
            toggle.Margin = new Thickness(0, 0, 10, 0);
            toggle.ToolTip = "Pick members to watch. Each tick is saved at once; a new filing of a watched member raises an alert in Monitoring → Alerts (no toast).";
            toggle.Click += async (_, _) =>
            {
                var open = _watchEditor.Visibility != Visibility.Visible;
                _watchEditor.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
                if (open) await LoadMembersAsync();
            };
            DockPanel.SetDock(toggle, Dock.Left);
            row.Children.Add(toggle);
            _watchSummary.VerticalAlignment = VerticalAlignment.Center;
            AutomationProperties.SetLiveSetting(_watchSummary, AutomationLiveSetting.Polite);
            row.Children.Add(_watchSummary);
            panel.Children.Add(row);
            _watchProblem.Visibility = Visibility.Collapsed;
            panel.Children.Add(_watchProblem);

            var editor = new StackPanel();
            var search = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 6) };
            search.Children.Add(new TextBlock { Text = "Find member", FontSize = 10.5, Foreground = FundPalette.Faint, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 6, 0) });
            _watchSearch.Width = 200;
            _watchSearch.FontSize = 11;
            _watchSearch.MaxLength = 60;
            _watchSearch.Padding = new Thickness(4, 1, 4, 1);
            _watchSearch.Background = DisclosureUi.CardBg;
            _watchSearch.BorderBrush = DisclosureUi.CardBorder;
            _watchSearch.Foreground = FundPalette.TextBright;
            _watchSearch.CaretBrush = FundPalette.TextBright;
            AutomationProperties.SetName(_watchSearch, "Find a member by name");
            _watchSearch.TextChanged += (_, _) =>
            {
                _watchDebounce ??= NewDebounce();
                _watchDebounce.Stop();
                _watchDebounce.Start();
            };
            search.Children.Add(_watchSearch);
            search.Children.Add(DisclosureUi.Text("  members with stored trades · ticked = watched (saved at once)", 10.5, FundPalette.Faint, wrap: false));
            editor.Children.Add(search);
            AutomationProperties.SetName(_watchRows, "Congress members — ticked members are watched");
            editor.Children.Add(new ScrollViewer { MaxHeight = 220, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = _watchRows });
            _watchEditor.Background = DisclosureUi.CardBg;
            _watchEditor.BorderBrush = DisclosureUi.CardBorder;
            _watchEditor.BorderThickness = new Thickness(1);
            _watchEditor.CornerRadius = new CornerRadius(6);
            _watchEditor.Padding = new Thickness(10, 6, 10, 6);
            _watchEditor.Margin = new Thickness(0, 6, 0, 0);
            _watchEditor.Child = editor;
            panel.Children.Add(_watchEditor);
            return panel;
        }

        private DispatcherTimer NewDebounce()
        {
            var t = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
            t.Tick += async (_, _) =>
            {
                t.Stop();
                await LoadMembersAsync();
            };
            return t;
        }

        private async Task LoadMembersAsync()
        {
            if (_shutDown) return;
            var generation = ++_watchGeneration;
            var result = await DisclosuresClient.MembersAsync(_watchSearch.Text);
            if (generation != _watchGeneration || _shutDown) return;
            if (!result.Ok)
            {
                _watchSummary.Text = "Watch list not available: " + result.Problem;
                _watchSummary.Foreground = FundPalette.Risk;
                return;
            }
            _watchLoaded = true;
            var v = result.Value!;
            _watched.Clear();
            foreach (var id in v.Watched ?? new List<string>()) _watched.Add(id);
            foreach (var m in v.Members ?? new List<CongressMember>()) _memberNames[m.Id] = m.Name ?? m.Id;
            RenderWatchSummary();
            _watchRows.Children.Clear();
            var members = v.Members ?? new List<CongressMember>();
            if (members.Count == 0)
                _watchRows.Children.Add(DisclosureUi.Text(_watchSearch.Text.Trim().Length > 0 ? "No member matches." : "No congress trades stored yet — the member list fills with the daily pull.", 11, FundPalette.Dim));
            foreach (var m in members)
            {
                var chamber = m.Chamber == "house" ? "House" : m.Chamber == "senate" ? "Senate" : m.Chamber;
                var cb = new CheckBox
                {
                    Content = string.Join(" · ", new[] { m.Name ?? m.Id, chamber, string.Join("-", new[] { m.Party, m.State }.Where(x => !string.IsNullOrWhiteSpace(x))) }
                                                 .Where(x => !string.IsNullOrWhiteSpace(x)))
                              + " · " + m.TradeCount.ToString("N0", Inv) + " trade(s)",
                    IsChecked = _watched.Contains(m.Id),
                    FontSize = 11, Foreground = FundPalette.Text, Margin = new Thickness(0, 1, 0, 1),
                };
                var id = m.Id;
                cb.Checked += async (_, _) => await ToggleWatchAsync(id, true, cb);
                cb.Unchecked += async (_, _) => await ToggleWatchAsync(id, false, cb);
                _watchRows.Children.Add(cb);
            }
            if (_rows.Count > 0) Render(keepScroll: true);   // the "watched" badges follow
        }

        private void RenderWatchSummary()
        {
            _watchSummary.Foreground = FundPalette.Faint;
            if (_watched.Count == 0)
            {
                _watchSummary.Text = "Watch list: nobody watched · a new filing of a watched member raises an alert in Monitoring → Alerts (no toast)";
                return;
            }
            var names = _watched.Select(id => _memberNames.TryGetValue(id, out var n) ? n : id).OrderBy(n => n, StringComparer.OrdinalIgnoreCase).ToList();
            _watchSummary.Text = "Watching " + _watched.Count.ToString(Inv) + ": " + string.Join(", ", names.Take(6)) + (names.Count > 6 ? " …" : "")
                                 + " · new filings → Monitoring → Alerts (no toast)";
        }

        /// <summary>One tick = one PUT with the whole list (the backend replaces it); a failure undoes the tick and says why.</summary>
        private async Task ToggleWatchAsync(string id, bool on, CheckBox box)
        {
            if (_watchSaving) return;
            var next = new HashSet<string>(_watched, StringComparer.Ordinal);
            if (on) next.Add(id); else next.Remove(id);
            _watchSaving = true;
            _watchRows.IsEnabled = false;
            try
            {
                var result = await DisclosuresClient.WatchAsync(next.OrderBy(x => x, StringComparer.Ordinal).ToList());
                if (!result.Ok)
                {
                    _watchProblem.Text = "The watch list could not be saved: " + result.Problem;
                    _watchProblem.Visibility = Visibility.Visible;
                    _watchSaving = true;   // keep the handlers quiet while the tick is undone
                    box.IsChecked = !on;
                    return;
                }
                _watchProblem.Visibility = Visibility.Collapsed;
                _watched.Clear();
                foreach (var w in result.Value!.Watched ?? new List<string>()) _watched.Add(w);
                RenderWatchSummary();
                if (_rows.Count > 0) Render(keepScroll: true);
            }
            finally
            {
                _watchSaving = false;
                _watchRows.IsEnabled = true;
            }
        }

        private static void Put(Grid g, UIElement e, int row)
        {
            Grid.SetRow(e, row);
            g.Children.Add(e);
        }

        // =====================================================================================
        // Filters
        // =====================================================================================

        private FrameworkElement BuildFilterBar()
        {
            var wrap = new WrapPanel { Margin = new Thickness(0, 10, 0, 0) };
            void Seg(string label, (string, string, string?)[] options, string initial, Action<string> set)
            {
                wrap.Children.Add(DisclosureUi.Segmented(label, options, initial, v =>
                {
                    set(v);
                    _ = LoadAsync(silent: false, append: false);
                }, out var select));
                _resetActions.Add(() => select(initial));
            }
            Seg("Period", new (string, string, string?)[] { ("7 d", "7", null), ("30 d", "30", null), ("90 d", "90", null), ("1 y", "365", null), ("2 y", "730", null) },
                "30", v => _days = int.Parse(v, Inv));
            Seg("Side", new (string, string, string?)[] { ("Buys + sells", "P,S", null), ("Buys", "P", "Purchases"), ("Sells", "S", "Sales (partial or full)"),
                                                          ("All types", "P,S,X", "Also other transaction types (exchanges, …) — listed, never measured") },
                "P,S", v => _code = v);
            Seg("Chamber", new (string, string, string?)[] { ("Both", "all", null), ("House", "house", null), ("Senate", "senate", null) }, "all", v => _chamber = v);
            Seg("Party", new (string, string, string?)[] { ("All", "all", null), ("D", "D", "Democrats"), ("R", "R", "Republicans"), ("I", "I", "Independents") }, "all", v => _party = v);
            Seg("Filed", new (string, string, string?)[] { ("All", "all", null), ("Late", "late", "Filed more than 45 days after the trade (STOCK Act deadline)"), ("On time", "on_time", null) },
                "all", v => _late = v);

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

            var measurable = new CheckBox { Content = "Measurable only", FontSize = 11, Foreground = FundPalette.Text, Margin = new Thickness(0, 0, 14, 6),
                                            VerticalAlignment = VerticalAlignment.Center, ToolTip = "Only rows with a ticker and a stock / ETF asset type (others show the reason)" };
            measurable.Checked += (_, _) => { _measurableOnly = true; _ = LoadAsync(silent: false, append: false); };
            measurable.Unchecked += (_, _) => { _measurableOnly = false; _ = LoadAsync(silent: false, append: false); };
            _resetActions.Add(() => measurable.IsChecked = false);
            wrap.Children.Add(measurable);
            return wrap;
        }

        private async Task ResetAsync()
        {
            _days = 30; _code = "P,S"; _chamber = "all"; _party = "all"; _late = "all"; _ticker = ""; _measurableOnly = false;
            _member = ""; _memberName = "";
            foreach (var a in _resetActions) a();
            _tickerDebounce.Stop();
            await LoadAsync(silent: false, append: false);
        }

        private async Task SetMemberAsync(string id, string name)
        {
            _member = id;
            _memberName = name;
            await LoadAsync(silent: false, append: false);
        }

        private void RenderMemberChip()
        {
            if (_member.Length == 0)
            {
                _memberChip.Visibility = Visibility.Collapsed;
                return;
            }
            var b = FundUi.MakeButton("Member: " + (_memberName.Length > 0 ? _memberName : _member) + "  ✕");
            b.Padding = new Thickness(8, 1, 8, 1);
            b.MinHeight = 22;
            b.FontSize = 10.5;
            b.Margin = new Thickness(0, 0, 8, 0);
            b.ToolTip = "Remove the member filter";
            b.Click += async (_, _) => await SetMemberAsync("", "");
            _memberChip.Child = b;
            _memberChip.Visibility = Visibility.Visible;
        }

        private string Query(int offset)
        {
            var q = new StringBuilder();
            q.Append("days=").Append(_days.ToString(Inv)).Append("&code=").Append(Uri.EscapeDataString(_code));
            q.Append("&chamber=").Append(_chamber).Append("&party=").Append(_party).Append("&late=").Append(_late);
            if (_ticker.Length > 0) q.Append("&ticker=").Append(Uri.EscapeDataString(_ticker));
            if (_member.Length > 0) q.Append("&member=").Append(Uri.EscapeDataString(_member));
            q.Append("&measurable=").Append(_measurableOnly ? "yes" : "all");
            q.Append("&limit=").Append(PageSize.ToString(Inv)).Append("&offset=").Append(offset.ToString(Inv));
            return q.ToString();
        }

        // =====================================================================================
        // List
        // =====================================================================================

        private async Task LoadAsync(bool silent, bool append)
        {
            if (_shutDown || (_loading && silent)) return;
            var generation = ++_generation;
            _loading = true;
            if (!silent && !append) ShowStatus("Loading congress trades…");
            try
            {
                var result = await DisclosuresClient.CongressListAsync(Query(append ? _rows.Count : 0));
                if (generation != _generation || _shutDown) return;
                if (!result.Ok)
                {
                    if (silent && _rows.Count > 0) _infoText.Text = "Refresh failed at " + DateTime.Now.ToString("HH:mm:ss", Inv) + " (" + result.Problem + ") — showing the last list.";
                    else ShowStatus("Congress trades not available: " + result.Problem + ".\n\nThe HedgeFund backend starts with the Hedge Fund window; this view retries on its own.");
                    return;
                }
                var v = result.Value!;
                if (!append) _rows.Clear();
                _rows.AddRange(v.Rows ?? new List<CongressRow>());
                _totalMatching = v.TotalMatching;
                _totalAll = v.TotalAll;
                RenderSource(v.Source);
                RenderMemberChip();
                Render(keepScroll: silent || append);
            }
            finally
            {
                if (generation == _generation) _loading = false;
            }
        }

        private void RenderSource(CongressSourceState? s)
        {
            _infoText.Text = (s?.Terms ?? "Congress trades: public records under the Ethics in Government Act — personal research only.")
                             + " · source data through " + (s?.UpdatedThrough ?? "unknown") + " · last pull " + DisclosureUi.LocalTime(s?.LastSuccess);
            var stale = s?.Stale == true;
            _staleText.Text = stale ? "Source not updated since " + (s?.UpdatedThrough ?? "an unknown date") + " — the list may be missing recent filings (no other source is used)." : "";
            _staleText.Visibility = stale ? Visibility.Visible : Visibility.Collapsed;
        }

        private void Render(bool keepScroll)
        {
            var offset = _scroll.VerticalOffset;
            _rowsPanel.Children.Clear();
            var shown = _rows.Count;
            _countText.Text = shown == _totalMatching
                ? $"{_totalMatching.ToString("N0", Inv)} of {_totalAll.ToString("N0", Inv)} shown · change filters"
                : $"{shown.ToString("N0", Inv)} of {_totalMatching.ToString("N0", Inv)} matching shown ({_totalAll.ToString("N0", Inv)} stored) · change filters";
            if (_rows.Count == 0)
            {
                ShowStatus(_totalAll == 0
                    ? "No congress trades stored yet — the daily pull of the congress source fills this list (see the status line)."
                    : "No trades match the filters — " + _totalAll.ToString("N0", Inv) + " stored. Change or reset the filters.");
                return;
            }
            foreach (var r in _rows) _rowsPanel.Children.Add(BuildCard(r));
            if (_rows.Count < _totalMatching)
            {
                var more = FundUi.MakeButton("Load " + Math.Min(PageSize, _totalMatching - _rows.Count).ToString(Inv) + " more");
                more.HorizontalAlignment = HorizontalAlignment.Left;
                more.Margin = new Thickness(0, 4, 0, 8);
                more.Click += async (_, _) => await LoadAsync(silent: false, append: true);
                _rowsPanel.Children.Add(more);
            }
            _statusText.Visibility = Visibility.Collapsed;
            _scroll.Visibility = Visibility.Visible;
            if (keepScroll) _scroll.ScrollToVerticalOffset(offset);
            else _scroll.ScrollToTop();
        }

        private void ShowStatus(string text)
        {
            _statusText.Text = text;
            _statusText.Visibility = Visibility.Visible;
            _scroll.Visibility = Visibility.Collapsed;
        }

        private static string SideText(string? code) => code switch { "P" => "BUY", "S" => "SELL", _ => "OTHER" };

        private static string Who(CongressRow r)
        {
            var chamber = r.Chamber == "house" ? "House" : r.Chamber == "senate" ? "Senate" : r.Chamber;
            var partyState = string.Join("-", new[] { r.Party, r.State }.Where(x => !string.IsNullOrWhiteSpace(x)));
            var owner = r.OwnerType is null or "self" ? null : "(" + r.OwnerType + ")";
            return string.Join(" · ", new[] { r.MemberName ?? "unknown member", chamber, partyState }.Where(x => !string.IsNullOrWhiteSpace(x)))
                   + (owner != null ? " " + owner : "");
        }

        private Border BuildCard(CongressRow r)
        {
            var sideBrush = r.Code == "X" ? FundPalette.Dim : DisclosureUi.CongressBrush;
            var grid = new Grid { Margin = new Thickness(10, 7, 10, 7) };
            foreach (var w in new[] { 52.0, 70.0 }) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(w) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            for (var i = 0; i < 4; i++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            void Put(UIElement e, int col)
            {
                Grid.SetColumn(e, col);
                grid.Children.Add(e);
            }
            Put(new Border
            {
                BorderBrush = sideBrush, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(3), Padding = new Thickness(4, 0, 4, 1),
                HorizontalAlignment = HorizontalAlignment.Left, VerticalAlignment = VerticalAlignment.Center,
                Child = new TextBlock { Text = SideText(r.Code), FontSize = 9.5, FontWeight = FontWeights.SemiBold, Foreground = sideBrush },
                ToolTip = r.Code == "X" ? "Other transaction type (e.g. exchange) — listed, not measured" : null,
            }, 0);
            var tick = DisclosureUi.Text(r.Ticker ?? "—", 12.5, FundPalette.TextBright, bold: true, mono: true, wrap: false);
            tick.ToolTip = r.AssetName;
            Put(tick, 1);
            var who = DisclosureUi.Text(Who(r) + (string.IsNullOrWhiteSpace(r.AssetName) ? "" : " · " + r.AssetName), 11.5, FundPalette.Text, wrap: false);
            who.TextTrimming = TextTrimming.CharacterEllipsis;
            who.VerticalAlignment = VerticalAlignment.Center;
            who.ToolTip = who.Text;
            Put(who, 2);
            var amount = DisclosureUi.Text(r.AmountLabel ?? "—", 11, FundPalette.TextBright, mono: true, wrap: false);
            amount.Margin = new Thickness(10, 0, 0, 0);
            amount.VerticalAlignment = VerticalAlignment.Center;
            amount.ToolTip = "Amount range as disclosed (members report ranges, not exact values)";
            Put(amount, 3);
            var delayText = r.DaysToFile is int d ? "filed +" + d.ToString(Inv) + " d" : "filed " + (r.FilingDate ?? "—");
            var delay = DisclosureUi.Text(delayText, 10.5, FundPalette.Dim, mono: true, wrap: false);
            delay.Margin = new Thickness(10, 0, 0, 0);
            delay.VerticalAlignment = VerticalAlignment.Center;
            delay.ToolTip = "Transaction " + (r.TransactionDate ?? "—") + " → filed " + (r.FilingDate ?? "—")
                            + (r.DaysToFile is int dd ? " (" + dd.ToString(Inv) + " days)" : "");
            Put(delay, 4);
            var badges = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            if (r.IsLate == true) badges.Children.Add(DisclosureUi.Badge("LATE", FundPalette.Risk, "Filed more than 45 days after the transaction (STOCK Act deadline), per the source's flag."));
            if (r.MemberId != null && _watched.Contains(r.MemberId))
                badges.Children.Add(DisclosureUi.Badge("watched", DisclosureUi.CongressBrush, "This member is on your watch list (new filings raise an alert in Monitoring → Alerts)."));
            Put(badges, 5);
            var (chipText, chipTip) = DisclosureUi.ChipText(r);
            Put(new Border
            {
                BorderBrush = DisclosureUi.ChipBorder, BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(10), Padding = new Thickness(8, 1, 8, 2),
                Margin = new Thickness(10, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center, ToolTip = chipTip,
                Child = DisclosureUi.Text(chipText, 10.5, r.Chip?.State == "complete" ? FundPalette.TextBright : FundPalette.Dim, mono: true, wrap: false),
            }, 6);

            var detailHost = new StackPanel { Margin = new Thickness(10, 0, 10, 10), Visibility = Visibility.Collapsed };
            var header = DisclosureUi.FlatToggle(grid, SideText(r.Code) + " " + (r.Ticker ?? r.AssetName ?? "") + " by " + Who(r) + ", " + (r.AmountLabel ?? "amount unknown")
                                                       + ", " + delayText + (r.IsLate == true ? ", late" : "") + ", return " + chipText + ". Press to show details.");
            header.Checked += async (_, _) =>
            {
                _expanded.Add(r.Id);
                detailHost.Visibility = Visibility.Visible;
                await ShowDetailAsync(r.Id, detailHost);
            };
            header.Unchecked += (_, _) =>
            {
                _expanded.Remove(r.Id);
                detailHost.Visibility = Visibility.Collapsed;
            };
            var stack = new StackPanel();
            stack.Children.Add(header);
            stack.Children.Add(detailHost);
            if (_expanded.Contains(r.Id)) header.IsChecked = true;
            return new Border
            {
                Background = DisclosureUi.CardBg, BorderBrush = DisclosureUi.CardBorder, BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6), Margin = new Thickness(0, 0, 0, 6), Child = stack,
            };
        }

        private async Task ShowDetailAsync(string id, StackPanel host)
        {
            if (_details.TryGetValue(id, out var cached)) RenderDetail(cached, host);
            else
            {
                host.Children.Clear();
                host.Children.Add(DisclosureUi.Text("Loading details…", 11, FundPalette.Dim));
            }
            var result = await DisclosuresClient.CongressDetailAsync(id);
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

        private void RenderDetail(CongressDetail d, StackPanel host)
        {
            host.Children.Clear();
            host.Children.Add(new Border { BorderBrush = DisclosureUi.CardBorder, BorderThickness = new Thickness(0, 1, 0, 0), Margin = new Thickness(0, 0, 0, 6) });
            string Line(params string?[] parts) => string.Join(" · ", parts.Where(p => !string.IsNullOrWhiteSpace(p)));
            host.Children.Add(DisclosureUi.Text(Line(d.AssetName, d.AssetType != null ? "asset type " + d.AssetType : null,
                                                     "owner: " + (d.OwnerType ?? "not stated"), d.Office, d.FilingType != null ? "filing: " + d.FilingType : null), 11, FundPalette.Text));
            host.Children.Add(DisclosureUi.Text(Line("transaction " + (d.TransactionDate ?? "—"), "filed " + (d.FilingDate ?? "—"),
                                                     d.DaysToFile is int dd ? dd.ToString(Inv) + " days to file" + (d.IsLate == true ? " (late, > 45 days)" : d.IsLate == false ? " (on time)" : "") : null,
                                                     "amount " + (d.AmountLabel ?? "—")), 11, FundPalette.Dim));
            if (!string.IsNullOrWhiteSpace(d.Comment)) host.Children.Add(DisclosureUi.Text("Comment (as filed): " + d.Comment, 11, FundPalette.Dim));
            if (!d.Measurable) host.Children.Add(DisclosureUi.Text("Not measurable: " + (d.NotMeasurableReason ?? "no reason given"), 11, FundPalette.Risk));
            host.Children.Add(InsiderTradesView.ReturnsBlock("From the filing (tradable: open of the first session after the filing date)", d.Returns?.Filing, main: true));
            host.Children.Add(InsiderTradesView.ReturnsBlock(TradeDateCaption, d.Returns?.TradeDate, main: false));
            foreach (var n in d.Notes ?? new List<string>()) host.Children.Add(DisclosureUi.Text(n, 10, FundPalette.Faint));
            var actions = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 4, 0, 0) };
            if (!string.IsNullOrWhiteSpace(d.OfficialUrl)) actions.Children.Add(DisclosureUi.Link("Open the official filing ↗", d.OfficialUrl!));
            if (!string.IsNullOrWhiteSpace(d.MemberId) && d.MemberId != _member)
            {
                var only = FundUi.MakeButton("Only this member");
                only.Padding = new Thickness(8, 1, 8, 1);
                only.MinHeight = 22;
                only.FontSize = 10.5;
                only.Margin = new Thickness(14, 4, 0, 0);
                var id = d.MemberId!;
                var name = d.MemberName ?? id;
                only.Click += async (_, _) => await SetMemberAsync(id, name);
                actions.Children.Add(only);
            }
            host.Children.Add(actions);
        }
    }
}

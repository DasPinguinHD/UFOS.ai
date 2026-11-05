using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Financial Newsroom section "Disclosures" (replaces "Insider Trades"; CONGRESS_TASK.md Clarity rules,
    /// INSIDER_RET_API.md §8): tabs Insider · Congress · Track record under one status line (GET /newsroom/disclosures/status)
    /// with the user's "Start backfill" button (progress, pause / resume, DB sizes). Statistics only on the Track record tab;
    /// alerts only in Monitoring (no toasts here); nothing here is AI output except the labelled summary of the Insider tab.
    /// </summary>
    public sealed class DisclosuresView : UserControl, INewsroomView
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan StatusIdle = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan StatusRunning = TimeSpan.FromSeconds(10);

        private readonly InsiderTradesView _insider = new();
        private readonly TrackRecordPanel _track = new();
        private readonly CongressTradesView _congress = new();
        private readonly ContentControl _tabHost = new() { Focusable = false };
        private readonly TextBlock _statusText = DisclosureUi.Text("Loading the history status…", 10.5, FundPalette.Dim);
        private readonly Button _backfillButton;
        private readonly StackPanel _statusDetails = new() { Visibility = Visibility.Collapsed, Margin = new Thickness(0, 4, 0, 0) };
        private readonly ToggleButton _detailsToggle;
        private readonly DispatcherTimer _statusTimer;
        private DisclosuresStatus? _status;
        private bool _busy;
        private bool _shutDown;

        public DisclosuresView()
        {
            var root = new Grid { Margin = new Thickness(22, 18, 22, 18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            var head = new StackPanel();
            head.Children.Add(DisclosureUi.Text("Disclosures", 16, FundPalette.TextBright, bold: true));
            head.Children.Add(DisclosureUi.Text("Public trading disclosures — insiders (SEC Form 4) and, next, members of Congress (STOCK Act) — "
                                                + "with the return after the filing. Personal research only; information, not a recommendation.", 10.5, FundPalette.Faint));
            Grid.SetRow(head, 0);
            root.Children.Add(head);

            // status line: history (backfill) + daily job + DB sizes, the user's button, details
            _backfillButton = FundUi.MakeButton("Start backfill");
            _backfillButton.Padding = new Thickness(10, 2, 10, 2);
            _backfillButton.MinHeight = 22;
            _backfillButton.FontSize = 11;
            _backfillButton.Margin = new Thickness(10, 0, 0, 0);
            _backfillButton.IsEnabled = false;
            _backfillButton.Click += async (_, _) => await BackfillClickAsync();
            _detailsToggle = new ToggleButton { Content = "details", FontSize = 10.5, Margin = new Thickness(8, 0, 0, 0), Padding = new Thickness(6, 0, 6, 1),
                                                Background = Brushes.Transparent, Foreground = FundPalette.Dim, BorderBrush = DisclosureUi.ChipBorder, Cursor = System.Windows.Input.Cursors.Hand };
            AutomationProperties.SetName(_detailsToggle, "Show history load details");
            _detailsToggle.Checked += (_, _) => _statusDetails.Visibility = Visibility.Visible;
            _detailsToggle.Unchecked += (_, _) => _statusDetails.Visibility = Visibility.Collapsed;
            var statusRow = new DockPanel { LastChildFill = true };
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Top };
            buttons.Children.Add(_backfillButton);
            buttons.Children.Add(_detailsToggle);
            DockPanel.SetDock(buttons, Dock.Right);
            statusRow.Children.Add(buttons);
            _statusText.VerticalAlignment = VerticalAlignment.Center;
            AutomationProperties.SetLiveSetting(_statusText, AutomationLiveSetting.Polite);
            statusRow.Children.Add(_statusText);
            var statusBox = new StackPanel();
            statusBox.Children.Add(statusRow);
            statusBox.Children.Add(_statusDetails);
            var statusBorder = new Border { Background = DisclosureUi.CardBg, BorderBrush = DisclosureUi.CardBorder, BorderThickness = new Thickness(1),
                                            CornerRadius = new CornerRadius(6), Padding = new Thickness(10, 6, 10, 6), Margin = new Thickness(0, 10, 0, 0), Child = statusBox };
            Grid.SetRow(statusBorder, 1);
            root.Children.Add(statusBorder);

            // tabs
            var tabs = DisclosureUi.Segmented("", new (string, string, string?)[]
            {
                ("Insider", "insider", "SEC Form 4 open-market purchases and sales"),
                ("Congress", "congress", "STOCK Act trades of House and Senate members"),
                ("Track record", "track", "Group statistics of the returns after the filing (descriptive, not a validated signal)"),
            }, "insider", ShowTab, out _);
            tabs.Margin = new Thickness(0, 12, 0, 8);
            Grid.SetRow(tabs, 2);
            root.Children.Add(tabs);
            Grid.SetRow(_tabHost, 3);
            root.Children.Add(_tabHost);
            Content = root;
            ShowTab("insider");

            _statusTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = StatusIdle };
            _statusTimer.Tick += async (_, _) => { if (IsVisible) await LoadStatusAsync(); };
            IsVisibleChanged += async (_, e) => { if (e.NewValue is true && !_shutDown) await LoadStatusAsync(); };
            Loaded += async (_, _) => await LoadStatusAsync();
            _statusTimer.Start();
        }

        public void Shutdown()
        {
            _shutDown = true;
            _statusTimer.Stop();
            _insider.Shutdown();
            _congress.Shutdown();
        }

        private void ShowTab(string id)
        {
            _tabHost.Content = id switch
            {
                "congress" => _congress,
                "track" => _track,
                _ => _insider,
            };
            if (id == "track") _ = _track.LoadAsync();
        }

        // =====================================================================================
        // Status line + backfill button
        // =====================================================================================

        private async Task LoadStatusAsync()
        {
            if (_shutDown) return;
            var result = await DisclosuresClient.StatusAsync();
            if (_shutDown) return;
            if (!result.Ok)
            {
                _statusText.Text = "History status not available: " + result.Problem + ".";
                _statusText.Foreground = FundPalette.Risk;
                _backfillButton.IsEnabled = false;
                return;
            }
            RenderStatus(result.Value!);
        }

        private void RenderStatus(DisclosuresStatus s)
        {
            _status = s;
            var b = s.Backfill;
            var state = b?.State ?? "not_started";
            var stateText = state switch
            {
                "not_started" => "2-year history not loaded yet",
                "running" => "2-year history loading" + CurrentStep(b),
                "paused" => "2-year history paused" + CurrentStep(b),
                "complete" => "2-year history loaded",
                "failed" => "2-year history load failed",
                "needs_decision" => "2-year history load stopped — needs a decision",
                _ => "2-year history: " + state,
            };
            var c = s.Counts;
            var parts = new List<string> { stateText };
            if (c != null) parts.Add(c.Trades.ToString("N0", Inv) + " trades stored (" + c.Measurable.ToString("N0", Inv) + " measurable, " + c.Complete20d.ToString("N0", Inv) + " with a complete 20-day return)");
            parts.Add("returns: daily job " + (s.DailyJob?.LastRun != null ? DisclosureUi.LocalTime(s.DailyJob.LastRun) : "not run yet")
                      + (s.DailyJob?.NextRun != null ? ", next " + DisclosureUi.LocalTime(s.DailyJob.NextRun) : ""));
            if (s.Congress is CongressStatus cg)
                parts.Add("congress: " + (cg.Rows ?? 0).ToString("N0", Inv) + " rows, source through " + (cg.UpdatedThrough ?? "unknown")
                          + (cg.Stale ? " — source not updated since " + (cg.UpdatedThrough ?? "an unknown date") : "")
                          + (cg.State == "failed" ? " — last pull failed" : ""));
            parts.Add("DB sizes: newsroom " + DisclosureUi.Bytes(s.Storage?.NewsroomDbBytes) + " · hedgefund " + DisclosureUi.Bytes(s.Storage?.HedgefundDbBytes));
            _statusText.Text = string.Join(" · ", parts);
            var problem = state == "failed" || state == "needs_decision" || !string.IsNullOrWhiteSpace(s.DailyJob?.LastError)
                          || s.Congress?.Stale == true || s.Congress?.State == "failed";
            _statusText.Foreground = problem ? FundPalette.Risk : FundPalette.Dim;

            var (label, tip) = state switch
            {
                "running" => ("Pause", "Pauses after the current step; Resume continues where it stopped."),
                "paused" or "needs_decision" => ("Resume", "Continues the history load from its stored progress."),
                "complete" => ("Run again", "Re-runs the steps (already loaded data stays; finished parts are rechecked)."),
                _ => ("Start backfill", "Loads the 2-year history in the background: " + (b?.Estimate?.Hours ?? "duration unknown")
                                        + ". SEC requests at most 5 per second; prices only outside US market hours."),
            };
            _backfillButton.Content = label;
            _backfillButton.ToolTip = tip;
            _backfillButton.IsEnabled = !_busy;
            _statusTimer.Interval = state == "running" ? StatusRunning : StatusIdle;
            RenderStatusDetails(s);
        }

        private static string CurrentStep(BackfillStatus? b)
        {
            var step = b?.Steps?.FirstOrDefault(x => x.State == "running") ?? b?.Steps?.FirstOrDefault(x => x.State != "done");
            return step == null ? "" : " (step " + StepLabel(step.Key) + " " + ProgressText(step.Progress) + ")";
        }

        private static string StepLabel(string? key) => key switch
        {
            "datasets" => "SEC data sets",
            "pilot" => "acceptance-time pilot",
            "acceptance" => "acceptance times + SIC",
            "gap" => "gap days (EDGAR daily index)",
            "prices" => "prices (Alpaca, outside market hours)",
            _ => key ?? "?",
        };

        /// <summary>"12 of 40" from a step's progress {done: n | [..], total: n} (+ its waiting reason).</summary>
        private static string ProgressText(JsonElement? progress)
        {
            if (progress is not JsonElement p || p.ValueKind != JsonValueKind.Object) return "";
            long? done = null, total = null;
            if (p.TryGetProperty("done", out var d))
                done = d.ValueKind == JsonValueKind.Number ? d.GetInt64() : d.ValueKind == JsonValueKind.Array ? d.GetArrayLength() : null;
            if (p.TryGetProperty("total", out var t) && t.ValueKind == JsonValueKind.Number) total = t.GetInt64();
            var text = done is long dn ? dn.ToString("N0", Inv) + (total is long tt ? " of " + tt.ToString("N0", Inv) : " done") : "";
            if (p.TryGetProperty("errors", out var e) && e.ValueKind == JsonValueKind.Number && e.GetInt64() > 0) text += ", " + e.GetInt64().ToString("N0", Inv) + " errors";
            if (p.TryGetProperty("waiting", out var w) && w.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(w.GetString())) text += " — waiting: " + w.GetString();
            return text.Trim();
        }

        private void RenderStatusDetails(DisclosuresStatus s)
        {
            _statusDetails.Children.Clear();
            var b = s.Backfill;
            if (!string.IsNullOrWhiteSpace(b?.LastError)) _statusDetails.Children.Add(DisclosureUi.Text("History load: " + b!.LastError, 10.5, FundPalette.Risk));
            foreach (var step in b?.Steps ?? new List<BackfillStep>())
            {
                var line = StepLabel(step.Key) + ": " + (step.State ?? "pending") + (ProgressText(step.Progress) is { Length: > 0 } pt ? " · " + pt : "");
                _statusDetails.Children.Add(DisclosureUi.Text(line, 10.5, step.State == "failed" ? FundPalette.Risk : FundPalette.Dim, mono: true));
                foreach (var g in (step.Gaps ?? new List<BackfillGap>()).Take(10))
                    _statusDetails.Children.Add(DisclosureUi.Text("   gap " + g.Key + ": " + g.Reason, 10, FundPalette.Risk, mono: true));
                if ((step.Gaps?.Count ?? 0) > 10) _statusDetails.Children.Add(DisclosureUi.Text("   … " + (step.Gaps!.Count - 10).ToString(Inv) + " more gaps (retried on the next run)", 10, FundPalette.Risk));
            }
            if (b?.Estimate != null)
                _statusDetails.Children.Add(DisclosureUi.Text("Estimate: ~" + (b.Estimate.RequestsSec ?? 0).ToString("N0", Inv) + " SEC requests, ~" + (b.Estimate.RequestsAlpaca ?? 0).ToString("N0", Inv)
                                                              + " Alpaca requests · " + (b.Estimate.Hours ?? ""), 10.5, FundPalette.Faint));
            if (s.SecIntervalSeconds is double sec)
                _statusDetails.Children.Add(DisclosureUi.Text("SEC request interval now: " + sec.ToString("0.0#", Inv) + " s (0.2 s while the history load runs)", 10.5, FundPalette.Faint));
            if (!string.IsNullOrWhiteSpace(s.DailyJob?.LastError))
                _statusDetails.Children.Add(DisclosureUi.Text("Daily job: " + s.DailyJob!.LastError, 10.5, FundPalette.Risk));
            if (s.Congress is CongressStatus c2)
            {
                _statusDetails.Children.Add(DisclosureUi.Text("Congress source: last pull " + DisclosureUi.LocalTime(c2.LastPull) + " (" + (c2.State ?? "not run") + "), last success "
                                                              + DisclosureUi.LocalTime(c2.LastSuccess) + ", data through " + (c2.UpdatedThrough ?? "unknown")
                                                              + (c2.MeasurableAssetTypes is { Count: > 0 } mt ? " · measured asset types: " + string.Join(", ", mt) : ""),
                                                              10.5, c2.Stale || c2.State == "failed" ? FundPalette.Risk : FundPalette.Faint));
                if (!string.IsNullOrWhiteSpace(c2.LastError)) _statusDetails.Children.Add(DisclosureUi.Text("Congress: " + c2.LastError, 10.5, FundPalette.Risk));
                if (c2.Skipped is JsonElement sk && sk.ValueKind == JsonValueKind.Object && sk.EnumerateObject().Any())
                    _statusDetails.Children.Add(DisclosureUi.Text("Congress rows skipped: " + string.Join(", ", sk.EnumerateObject().Select(p => p.Name + " " + p.Value.ToString())), 10.5, FundPalette.Faint));
            }
            if (s.SicUnmapped is { Count: > 0 } um)
                _statusDetails.Children.Add(DisclosureUi.Text("No sector ETF for SIC: " + string.Join(", ", um.Take(12).Select(u => (u.Sic?.ToString(Inv) ?? "?") + " (" + u.N.ToString(Inv) + ")"))
                                                              + (um.Count > 12 ? " …" : "") + " — these trades show the stock return only.", 10.5, FundPalette.Faint));
        }

        private async Task BackfillClickAsync()
        {
            var state = _status?.Backfill?.State ?? "not_started";
            var action = state switch
            {
                "running" => "pause",
                "paused" or "needs_decision" => "resume",
                _ => "start",
            };
            if (action == "start")
            {
                var owner = Window.GetWindow(this);
                var text = "Load the 2-year insider history in the background?\n\n"
                           + "Estimate: " + (_status?.Backfill?.Estimate?.Hours ?? "unknown") + ".\n"
                           + "SEC EDGAR at most 5 requests per second (shared with the live collector); Alpaca prices only outside US market hours "
                           + "and always after trading calls. No AI calls. Storage about 200 MB (newsroom.db) + 250 MB (hedgefund.db).\n\n"
                           + "You can pause and resume at any time.";
                if (MessageBox.Show(owner, text, "Start backfill", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;
            }
            _busy = true;
            _backfillButton.IsEnabled = false;
            try
            {
                var result = await DisclosuresClient.BackfillAsync(action);
                if (!result.Ok)
                {
                    MessageBox.Show(Window.GetWindow(this), "The history load could not be " + (action == "start" ? "started" : action + "d") + ": " + result.Problem,
                                    "Disclosures", MessageBoxButton.OK, MessageBoxImage.Warning);
                }
            }
            finally
            {
                _busy = false;
            }
            await LoadStatusAsync();
        }
    }

    /// <summary>
    /// "Track record" tab: GET /newsroom/disclosures/track-record — per group n, mean / median score, hit rate, running and not
    /// measurable counts; small samples (n &lt; 30) greyed. The backend's caveat is shown verbatim above the table. Statistics
    /// live ONLY here (never on the cards).
    /// </summary>
    internal sealed class TrackRecordPanel : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private string _source = "insider";
        private int _horizon = 20;
        private string _start = "filing";
        private string _group = "direction";
        private int _days = 730;
        private int _generation;
        private readonly TextBlock _caveat = DisclosureUi.Text("", 11, FundPalette.Text);
        private readonly StackPanel _table = new();

        public TrackRecordPanel()
        {
            var root = new StackPanel();
            var bar = new WrapPanel();
            void Seg(string label, (string, string, string?)[] options, string initial, Action<string> set)
                => bar.Children.Add(DisclosureUi.Segmented(label, options, initial, v => { set(v); _ = LoadAsync(); }, out _));
            // source first: the group choices depend on it (congress: chamber / party / late / amount — CONGRESS_API §6)
            var groupHost = new ContentControl { Focusable = false, VerticalAlignment = VerticalAlignment.Center };
            var sourceNote = DisclosureUi.Text("", 10.5, FundPalette.Faint);
            void BuildGroups()
            {
                var options = _source == "congress"
                    ? new (string, string, string?)[] { ("Direction", "direction", null), ("Chamber", "chamber", null), ("Party", "party", null),
                                                        ("Late filing", "late", "Filed more than 45 days after the transaction"),
                                                        ("Amount", "amount", "Disclosed amount range by its lower bound: ≤ $15k, $15k–50k, $50k–250k, > $250k") }
                    : new (string, string, string?)[] { ("Direction", "direction", null), ("10b5-1 plan", "plan", null), ("Role", "role", null), ("Cluster", "cluster", null) };
                _group = "direction";
                groupHost.Content = DisclosureUi.Segmented("Group by", options, "direction", v => { _group = v; _ = LoadAsync(); }, out _);
                sourceNote.Text = _source == "congress"
                    ? "Congress: entry = the open of the first session after the filing date (only the date is published); \"from trade date\" = the member's own timing."
                    : "Insider: entry = the first tradable price after the EDGAR acceptance; amendments are not counted.";
            }
            bar.Children.Add(DisclosureUi.Segmented("Source", new (string, string, string?)[] { ("Insider", "insider", "SEC Form 4"), ("Congress", "congress", "STOCK Act (House + Senate)") },
                                                    "insider", v => { _source = v; BuildGroups(); _ = LoadAsync(); }, out _));
            Seg("Horizon", new (string, string, string?)[] { ("1 d", "1", null), ("5 d", "5", null), ("20 d", "20", null), ("60 d", "60", null) }, "20", v => _horizon = int.Parse(v, Inv));
            Seg("Start", new (string, string, string?)[] { ("From filing", "filing", "Tradable: first price after the EDGAR acceptance"), ("From trade date", "trade_date", DisclosureUi.TradeDateCaption) }, "filing", v => _start = v);
            BuildGroups();
            bar.Children.Add(groupHost);
            Seg("Period", new (string, string, string?)[] { ("90 d", "90", null), ("1 y", "365", null), ("2 y", "730", null) }, "730", v => _days = int.Parse(v, Inv));
            root.Children.Add(bar);
            root.Children.Add(new Border { BorderBrush = DisclosureUi.InsiderBrush, BorderThickness = new Thickness(3, 0, 0, 0), Padding = new Thickness(10, 6, 10, 6),
                                           Margin = new Thickness(0, 6, 0, 6), Background = DisclosureUi.CardBg, Child = _caveat });
            root.Children.Add(DisclosureUi.Text("Score = return minus the sector ETF over the same window; for sales the sign is reversed (did the price fall afterwards?). "
                                                + "Hit rate = share of measured trades with a score above 0. n = trades with a complete measurement.",
                                                10.5, FundPalette.Faint));
            root.Children.Add(sourceNote);
            _table.Margin = new Thickness(0, 8, 0, 0);
            root.Children.Add(_table);
            Content = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = root };
        }

        public async Task LoadAsync()
        {
            var generation = ++_generation;
            _table.Children.Clear();
            _table.Children.Add(DisclosureUi.Text("Loading…", 11, FundPalette.Dim));
            var result = await DisclosuresClient.TrackRecordAsync(_source, _horizon, _start, _group, _days);
            if (generation != _generation) return;
            _table.Children.Clear();
            if (!result.Ok)
            {
                _table.Children.Add(DisclosureUi.Text("Track record not available: " + result.Problem, 11, FundPalette.Risk));
                return;
            }
            var v = result.Value!;
            _caveat.Text = v.Caveat ?? "";
            var groups = v.Groups ?? new List<TrackGroup>();
            if (groups.Count == 0)
            {
                _table.Children.Add(DisclosureUi.Text("No measured trades in this period yet (the 2-year history needs the backfill; returns need the daily job).", 11, FundPalette.Dim));
                return;
            }
            var grid = new Grid();
            foreach (var w in new[] { 170.0, 70.0, 100.0, 100.0, 80.0, 80.0, 110.0 }) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(w) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var row = 0;
            void Add(string[] cells, bool head, bool small)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                for (var c = 0; c < cells.Length; c++)
                {
                    var t = DisclosureUi.Text(cells[c], 11.5, head ? FundPalette.Faint : small ? FundPalette.Faint : FundPalette.Text, bold: head, mono: !head && c > 0, wrap: false);
                    t.Margin = new Thickness(0, 3, 10, 3);
                    Grid.SetRow(t, row);
                    Grid.SetColumn(t, c);
                    grid.Children.Add(t);
                }
                row++;
            }
            Add(new[] { "Group", "n", "Mean score", "Median score", "Hit rate", "Running", "Not measurable", "" }, true, false);
            foreach (var g in groups)
                Add(new[]
                {
                    g.Group ?? "?", g.N.ToString("N0", Inv), DisclosureUi.Pct(g.MeanScore), DisclosureUi.Pct(g.MedianScore),
                    g.HitRate is double h ? (h * 100).ToString("0", Inv) + " %" : "—", g.NRunning.ToString("N0", Inv), g.NNotMeasurable.ToString("N0", Inv),
                    g.SmallSample ? "small sample (n < 30) — noise" : "",
                }, false, g.SmallSample);
            _table.Children.Add(grid);
            _table.Children.Add(DisclosureUi.Text(v.Horizon.ToString(Inv) + "-day horizon · " + (v.Start == "trade_date" ? (v.Source == "congress" ? "from the transaction date — not tradable (the member's own timing)" : DisclosureUi.TradeDateCaption) : "from the filing (tradable)")
                                                  + " · " + (v.Source == "congress" ? "congress" : "insider")
                                                  + " · last " + v.Days.ToString(Inv) + " days", 10.5, FundPalette.Faint));
        }
    }
}

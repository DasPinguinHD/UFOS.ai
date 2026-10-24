using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // Monitoring → Alerts (§19.3), Reports (§19.4) and the Tax reports notice (§19.15 — no tax export).
    internal sealed partial class MonitoringView
    {
        private List<MonAlert> _alerts = new();
        private Dictionary<string, MonAlertKind> _alertKinds = new(StringComparer.Ordinal);
        private string? _alertsError;
        private bool _alertsUnackedOnly;
        private long? _selectedAlertId;

        private List<MonReportSummary>? _reports;
        private string? _reportsError;
        private string _reportPeriod = "daily";
        private MonReport? _report;
        private string? _reportError;

        private MonTaxNotice? _taxNotice;
        private string? _taxNoticeError;

        // =====================================================================================
        // Alerts
        // =====================================================================================

        private async Task LoadAlertsAsync(bool force)
        {
            var res = await PlatformClient.GetMonitoringAlertsAsync(0, 200, _alertsUnackedOnly);
            if (_stopped) return;
            if (res.Ok && res.Value != null)
            {
                _alerts = res.Value.Alerts ?? new List<MonAlert>();
                _alertKinds = res.Value.Kinds ?? new Dictionary<string, MonAlertKind>(StringComparer.Ordinal);
                _alertsError = null;
                MonitoringNotifier.Report(res.Value.Unacked);
            }
            else _alertsError = res.Ok ? "Unexpected response from the backend (/v2/monitoring/alerts)." : PlatformClient.DescribeFailure(res);
            if (force) _signatures.Remove(SecAlerts);
            RenderIfChanged(SecAlerts, new object?[] { _alerts, _alertsError, _alertsUnackedOnly, _selectedAlertId, MonitoringNotifier.ToastsWanted(), MonitoringNotifier.ToastProblem, MonitoringNotifier.PollProblem }, RenderAlerts);
        }

        private void RenderAlerts()
        {
            var panel = _alertsPanel;
            var hadListFocus = panel.IsKeyboardFocusWithin;
            panel.Children.Clear();

            // settings
            var settings = new StackPanel();
            var toasts = new CheckBox
            {
                Content = "Windows toasts for monitoring alerts",
                IsChecked = FundSettings.MonitoringToasts,
                Foreground = FundPalette.Text,
                FontSize = 12,
                Margin = new Thickness(0, 0, 0, 2),
            };
            AutomationProperties.SetName(toasts, "Windows toasts for monitoring alerts");
            AutomationProperties.SetHelpText(toasts, "On: decay pause, reconciliation halt, kill switch, drawdown kill, daily loss limit, dead-man switch, broker disconnected and incubation finished show a Windows toast. Every alert stays in this list either way.");
            toasts.Click += (_, _) =>
            {
                FundSettings.MonitoringToasts = toasts.IsChecked == true;
                _signatures.Remove(SecAlerts);
                RenderIfChanged(SecAlerts, new object?[] { _alerts, _alertsError, _alertsUnackedOnly, _selectedAlertId, MonitoringNotifier.ToastsWanted(), MonitoringNotifier.ToastProblem, MonitoringNotifier.PollProblem }, RenderAlerts);
            };
            settings.Children.Add(toasts);
            var muted = FundSettings.MuteNotifications;
            settings.Children.Add(MonUi.Line(muted
                ? "The Hedge Fund \"Mute notifications\" switch (Overview → settings) is ON — no toasts are shown until you switch it off."
                : "Clicking a toast opens this list. Toasts are a notification channel only — every alert stays here. The window does not need to be open (toasts work while UFOS.ai runs).",
                10.5, muted ? FundPalette.Risk : FundPalette.Faint));
            if (MonitoringNotifier.ToastProblem is string toastProblem)
                settings.Children.Add(MonUi.Line(toastProblem, 10.5, FundPalette.Kill));
            if (MonitoringNotifier.PollProblem is string pollProblem)
                settings.Children.Add(MonUi.Line(pollProblem, 10.5, FundPalette.Kill));
            panel.Children.Add(FundUi.Panel(settings, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10)));

            // controls
            var controls = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            var unackedOnly = new CheckBox { Content = "Only not yet seen", IsChecked = _alertsUnackedOnly, Foreground = FundPalette.Text, FontSize = 12, Margin = new Thickness(0, 2, 14, 0) };
            AutomationProperties.SetName(unackedOnly, "Show only alerts not yet marked as seen");
            unackedOnly.Click += async (_, _) => { _alertsUnackedOnly = unackedOnly.IsChecked == true; await LoadAlertsAsync(force: true); };
            controls.Children.Add(unackedOnly);
            var ackAll = MonUi.Small("Mark all as seen", "Mark all alerts as seen");
            ackAll.IsEnabled = _alerts.Any(a => !a.Acked);
            ackAll.Click += async (_, _) => await AckAllAsync();
            controls.Children.Add(ackAll);
            panel.Children.Add(controls);

            if (_alertsError != null) panel.Children.Add(MonUi.ErrorBlock(_alertsError));
            if (_alerts.Count == 0)
            {
                panel.Children.Add(MonUi.Line(_alertsUnackedOnly ? "No alert waits to be seen." : "No alerts yet.", 11.5, FundPalette.Dim));
                RenderKinds(panel);
                return;
            }

            var list = TradingUi.List("Alerts, newest first. Enter opens the strategy or account of the selected alert.", 300);
            foreach (var a in _alerts)
            {
                var row = new StackPanel { Margin = new Thickness(2, 3, 2, 4) };
                var chips = new WrapPanel();
                chips.Children.Add(MonUi.Chip(MonUi.SeverityLabel(a.Severity), MonUi.SeverityBrush(a.Severity)));
                chips.Children.Add(MonUi.Chip(a.Acked ? "SEEN" : "NEW", a.Acked ? FundPalette.Faint : FundPalette.TextBright));
                var meta = FundUi.Text(TradingText.Ts(a.Ts) + " · " + KindLabel(a.Kind) + (a.Toast ? " · toast" : ""), 10.5, FundPalette.Faint, mono: true, wrap: false);
                meta.VerticalAlignment = VerticalAlignment.Center;
                chips.Children.Add(meta);
                row.Children.Add(chips);
                row.Children.Add(FundUi.Text(a.Title ?? "(no title)", 11.5, a.Acked ? FundPalette.Text : FundPalette.TextBright, bold: true));
                if (!string.IsNullOrWhiteSpace(a.Body)) row.Children.Add(FundUi.Text(a.Body, 10.5, FundPalette.Dim));
                var item = new ListBoxItem { Content = row, Tag = a, HorizontalContentAlignment = HorizontalAlignment.Stretch };
                AutomationProperties.SetName(item, MonUi.SeverityLabel(a.Severity) + ", " + (a.Acked ? "seen" : "new") + ", " + TradingText.Ts(a.Ts) + ": " + (a.Title ?? "") + ". " + (a.Body ?? ""));
                list.Items.Add(item);
                if (_selectedAlertId == a.Id) list.SelectedItem = item;
            }
            var detailHost = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
            AutomationProperties.SetLiveSetting(detailHost, AutomationLiveSetting.Polite);
            list.SelectionChanged += (_, _) =>
            {
                if (list.SelectedItem is ListBoxItem { Tag: MonAlert sel })
                {
                    _selectedAlertId = sel.Id;
                    RenderAlertDetail(detailHost, sel);
                }
            };
            list.KeyDown += async (_, e) =>
            {
                if (e.Key == Key.Enter && list.SelectedItem is ListBoxItem { Tag: MonAlert sel })
                {
                    e.Handled = true;
                    await OpenAlertTargetAsync(sel);
                }
            };
            list.MouseDoubleClick += async (_, _) =>
            {
                if (list.SelectedItem is ListBoxItem { Tag: MonAlert sel }) await OpenAlertTargetAsync(sel);
            };
            panel.Children.Add(list);
            panel.Children.Add(detailHost);
            if (list.SelectedItem is ListBoxItem { Tag: MonAlert selected })
            {
                RenderAlertDetail(detailHost, selected);
                list.ScrollIntoView(list.SelectedItem);
                if (hadListFocus) list.Dispatcher.BeginInvoke(new Action(() => (list.ItemContainerGenerator.ContainerFromItem(list.SelectedItem) as ListBoxItem)?.Focus()));
            }
            RenderKinds(panel);
        }

        private void RenderAlertDetail(StackPanel host, MonAlert a)
        {
            host.Children.Clear();
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text(a.Title ?? "(no title)", 12.5, FundPalette.TextBright, bold: true));
            if (!string.IsNullOrWhiteSpace(a.Body)) stack.Children.Add(MonUi.Line(a.Body, 11, FundPalette.Text));
            var data = a.Data is JsonElement d && d.ValueKind == JsonValueKind.Object && d.EnumerateObject().Any() ? FundFormat.Plain(d) : "—";
            stack.Children.Add(MonUi.Pairs(new[]
            {
                ("Alert", "#" + a.Id.ToString(Inv) + " · " + KindLabel(a.Kind) + " (" + (a.Kind ?? "?") + ") · " + MonUi.SeverityLabel(a.Severity)),
                ("Time", TradingText.Ts(a.Ts)),
                ("Account", a.AccountId ?? "—"),
                ("Strategy", a.StrategyId ?? "—"),
                ("Reference", a.Ref ?? "—"),
                ("Data", data),
                ("Seen", a.Acked ? "yes, " + TradingText.Ts(a.AckedAt) : "not yet"),
            }));
            var row = new WrapPanel { Margin = new Thickness(0, 6, 0, 0) };
            if (!string.IsNullOrEmpty(a.StrategyId))
            {
                var open = MonUi.Small("Open strategy", "Open the strategy of this alert in Monitoring, Strategies");
                open.Click += async (_, _) => await OpenAlertTargetAsync(a);
                row.Children.Add(open);
            }
            else if (!string.IsNullOrEmpty(a.AccountId))
            {
                var open = MonUi.Small("Open account", "Open the account of this alert in Monitoring, Accounts and brokers");
                open.Click += async (_, _) => await OpenAlertTargetAsync(a);
                row.Children.Add(open);
            }
            // S10-C (SPEC §22.8, SWITCHOVER_FIX_API §7): Council alerts — incl. the critical council_switchover_failed (retry / abort there)
            // and council_legacy_duplicates — open the Council view; label and severity come from the backend's kind list as for every alert
            if (a.Kind != null && a.Kind.StartsWith("council_", StringComparison.Ordinal))
            {
                var c = MonUi.Small(a.Kind == "council_switchover_failed" ? "Council view — retry / abort…" : "Council view…",
                                    "Open the Council of AIs at the top of the Overview");
                c.Click += (_, _) => RequestCouncil();
                row.Children.Add(c);
            }
            if (a.Kind is "reconciliation_halt" or "broker_disconnected" or "broker_reconnected")
            {
                var b = MonUi.Small("Keys & data → Brokers…", "Open Keys and data at the Brokers section");
                b.Click += (_, _) => BrokersRequested?.Invoke();
                row.Children.Add(b);
            }
            if (!a.Acked)
            {
                var ack = MonUi.Small("Mark as seen", "Mark this alert as seen");
                ack.Click += async (_, _) => await AckAsync(new[] { a.Id });
                row.Children.Add(ack);
            }
            stack.Children.Add(row);
            host.Children.Add(FundUi.Panel(stack, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10)));
        }

        private void RenderKinds(StackPanel panel)
        {
            if (_alertKinds.Count == 0) return;
            panel.Children.Add(MonUi.Head("Alert kinds"));
            panel.Children.Add(MonUi.Table(new[] { "Kind", "Label", "Severity", "Toast" },
                _alertKinds.Select(kv => (IReadOnlyList<string>)new[] { kv.Key, kv.Value.Label ?? "", MonUi.SeverityLabel(kv.Value.Severity), kv.Value.Toast == true ? "yes" : "no" }),
                "Alert kinds", new double[] { 170, 0, 90, 60 }));
        }

        private string KindLabel(string? kind)
            // the backend's alert-kind list is the single source of labels; a kind without a label shows its raw id, marked, so the gap is visible
            => kind != null && _alertKinds.TryGetValue(kind, out var k) && !string.IsNullOrWhiteSpace(k.Label) ? k.Label!
               : string.IsNullOrWhiteSpace(kind) ? "—" : kind + " (unlabelled)";

        private async Task OpenAlertTargetAsync(MonAlert a)
        {
            if (!a.Acked) await AckAsync(new[] { a.Id });
            if (!string.IsNullOrEmpty(a.StrategyId) && (_overview?.Strategies ?? new List<MonStrategy>()).Any(s => s.StrategyId == a.StrategyId))
            {
                _expanded.Add(a.StrategyId!);
                SelectTab(SecStrategies);
            }
            else if (!string.IsNullOrEmpty(a.AccountId)) SelectTab(SecAccounts);
            else if (!string.IsNullOrEmpty(a.StrategyId))
                SetAction("The strategy " + a.StrategyId + " is no longer incubating — it is not monitored any more (see Library / Portfolio & Trading).", FundPalette.Dim);
        }

        private async Task AckAsync(IReadOnlyCollection<long> ids)
        {
            if (ids.Count == 0) return;
            var res = await PlatformClient.AckMonitoringAlertsAsync(ids);
            if (_stopped) return;
            if (!res.Ok) SetAction("Could not mark the alert as seen: " + PlatformClient.DescribeFailure(res), FundPalette.Kill);
            else MonitoringNotifier.Report(res.Value?.Unacked);
            RenderBanners();
            if (_section == SecAlerts) await LoadAlertsAsync(force: true);
        }

        private async Task AckAllAsync()
        {
            var res = await PlatformClient.AckAllMonitoringAlertsAsync();
            if (_stopped) return;
            if (!res.Ok) SetAction("Could not mark the alerts as seen: " + PlatformClient.DescribeFailure(res), FundPalette.Kill);
            else
            {
                MonitoringNotifier.Report(res.Value?.Unacked);
                SetAction("Marked " + (res.Value?.Acked ?? 0).ToString(Inv) + " alert(s) as seen.", FundPalette.Dim);
            }
            RenderBanners();
            await LoadAlertsAsync(force: true);
        }

        // =====================================================================================
        // Reports
        // =====================================================================================

        private async Task LoadReportsAsync()
        {
            var res = await PlatformClient.ListMonitoringReportsAsync(_reportPeriod, 60);
            if (_stopped) return;
            if (res.Ok && res.Value != null) { _reports = res.Value.Reports ?? new List<MonReportSummary>(); _reportsError = null; }
            else { _reports ??= new List<MonReportSummary>(); _reportsError = res.Ok ? "Unexpected response from the backend (/v2/monitoring/reports)." : PlatformClient.DescribeFailure(res); }
            if (_report == null && _reports.Count > 0 && _reports[0].Id is string first) await LoadReportAsync(first, render: false);
            RenderReports();
        }

        private async Task LoadReportAsync(string id, bool render)
        {
            var res = await PlatformClient.GetMonitoringReportAsync(id);
            if (_stopped) return;
            if (res.Ok && res.Value?.Report != null) { _report = res.Value.Report; _reportError = null; }
            else _reportError = res.Ok ? "Unexpected response from the backend (report)." : PlatformClient.DescribeFailure(res);
            if (render) RenderReports();
        }

        private void RenderReports()
        {
            var panel = _reportsPanel;
            panel.Children.Clear();
            panel.Children.Add(MonUi.Line("Numbers first: every report is computed by the backend from the ledgers, orders and alerts (deterministic, no AI). Daily = one UTC date, "
                                          + "weekly = one ISO week (Mon–Sun UTC); the scheduler creates them after 00:30 UTC of the next day / Monday. An AI commentary is optional.",
                                          10.5, FundPalette.Faint));
            var controls = new WrapPanel { Margin = new Thickness(0, 4, 0, 6) };
            var group = "rp" + GetHashCode().ToString(Inv);
            var daily = TradingUi.Radio(group, "Daily", "Daily reports", _reportPeriod == "daily");
            var weekly = TradingUi.Radio(group, "Weekly", "Weekly reports", _reportPeriod == "weekly");
            daily.Checked += async (_, _) => { if (_reportPeriod == "daily") return; _reportPeriod = "daily"; _report = null; await LoadReportsAsync(); };
            weekly.Checked += async (_, _) => { if (_reportPeriod == "weekly") return; _reportPeriod = "weekly"; _report = null; await LoadReportsAsync(); };
            controls.Children.Add(daily);
            controls.Children.Add(weekly);
            var create = MonUi.Small("Create report now…", "Create a " + _reportPeriod + " report now");
            create.Click += async (_, _) => await CreateReportAsync();
            controls.Children.Add(create);
            panel.Children.Add(controls);
            if (_reportsError != null) panel.Children.Add(MonUi.ErrorBlock(_reportsError));
            var reports = _reports ?? new List<MonReportSummary>();
            if (reports.Count == 0)
            {
                panel.Children.Add(MonUi.Line("No " + _reportPeriod + " report yet. The first one appears after the period is complete — or create one now.", 11.5, FundPalette.Dim));
                return;
            }
            var list = TradingUi.List("Reports, newest first", 140);
            foreach (var r in reports)
            {
                var text = (r.PeriodId ?? "?") + " · " + (r.CreatedBy == "engine" ? "scheduler" : r.CreatedBy == "user" ? "created by you" : r.CreatedBy ?? "—")
                           + " · " + TradingText.Ts(r.CreatedAt);
                var item = new ListBoxItem { Content = FundUi.Text(text, 11, FundPalette.Text, mono: true, wrap: false), Tag = r };
                AutomationProperties.SetName(item, "Report " + text);
                list.Items.Add(item);
                if (_report?.Id == r.Id) list.SelectedItem = item;
            }
            list.SelectionChanged += async (_, _) =>
            {
                if (list.SelectedItem is ListBoxItem { Tag: MonReportSummary s } && s.Id != null && s.Id != _report?.Id) await LoadReportAsync(s.Id, render: true);
            };
            panel.Children.Add(list);
            if (_reportError != null) panel.Children.Add(MonUi.ErrorBlock(_reportError));
            if (_report != null) panel.Children.Add(ReportBlock(_report));
        }

        private async Task CreateReportAsync()
        {
            var values = FundDialogs.Form(Owner, "Create a " + _reportPeriod + " report",
                "The backend computes the " + _reportPeriod + " report of the period that contains the given day (UTC). Leave the day empty for the last complete period. "
                + "A new report is stored each time (the newest wins).", new[] { ("Day inside the period (YYYY-MM-DD, optional)", "") }, "Create report");
            if (values == null) return;
            var day = values.Length > 0 ? values[0].Trim() : "";
            if (day.Length > 0 && !DateTime.TryParseExact(day, "yyyy-MM-dd", Inv, System.Globalization.DateTimeStyles.None, out _))
            {
                SetAction("The day must be written as YYYY-MM-DD (e.g. 2026-10-16).", FundPalette.Kill);
                return;
            }
            SetAction("Creating the report…", FundPalette.Faint);
            var res = await PlatformClient.CreateMonitoringReportAsync(_reportPeriod, day.Length > 0 ? day : null);
            if (_stopped) return;
            if (!res.Ok || res.Value?.Report == null)
            {
                SetAction(res.Ok ? "Unexpected response from the backend (report)." : PlatformClient.DescribeFailure(res), FundPalette.Kill);
                return;
            }
            SetAction("Report " + (res.Value.Report.PeriodId ?? "") + " created.", FundPalette.Dim);
            _report = res.Value.Report;
            await LoadReportsAsync();
        }

        private UIElement ReportBlock(MonReport r)
        {
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text((r.Period == "weekly" ? "Weekly" : "Daily") + " report " + (r.PeriodId ?? "?"), 13, FundPalette.TextBright, bold: true));
            stack.Children.Add(MonUi.Line(TradingText.Ts(r.Start) + " → " + TradingText.Ts(r.End) + " (local time) · created " + TradingText.Ts(r.CreatedAt) + " by "
                                          + (r.CreatedBy == "engine" ? "the scheduler" : r.CreatedBy ?? "—") + " · " + (r.Id ?? ""), 10.5, FundPalette.Faint, mono: true));
            stack.Children.Add(MonUi.Chip("PAPER / DEMO · SIMULATED · NUMBERS COMPUTED, NO AI", FundPalette.Focus));
            var n = r.Numbers;
            stack.Children.Add(MonUi.Head("Accounts"));
            stack.Children.Add(MonUi.Table(new[] { "Account", "Equity start", "Equity end", "P&L", "Return", "Max DD", "Fees", "Orders t/f/r/c", "Halts" },
                (n?.Accounts ?? new List<MonReportAccount>()).Select(a => (IReadOnlyList<string>)new[]
                {
                    (a.AccountId ?? "?") + (a.Paper == true ? " (paper)" : ""), FundFormat.Money(a.EquityStart), FundFormat.Money(a.EquityEnd), FundFormat.Money(a.Pnl, signed: true),
                    FundFormat.Pct(a.ReturnPct, 2, signed: true), FundFormat.Pct(a.MaxDrawdownPct, 2), FundFormat.Money(a.Fees),
                    MonUi.Int(a.Orders?.Total) + "/" + MonUi.Int(a.Orders?.Filled) + "/" + MonUi.Int(a.Orders?.Rejected) + "/" + MonUi.Int(a.Orders?.Cancelled),
                    MonUi.Plain(a.Halts),
                }), "Report accounts", new double[] { 150, 0, 0, 0, 70, 70, 70, 100, 50 }));
            stack.Children.Add(MonUi.Head("Strategies (incubating)"));
            stack.Children.Add(MonUi.Table(new[] { "Strategy", "Sessions", "Return", "Expected", "Band", "z", "Decay" },
                (n?.Strategies ?? new List<MonReportStrategy>()).Select(s => (IReadOnlyList<string>)new[]
                {
                    (s.Label ?? "?") + " — " + (s.Name ?? s.StrategyId ?? ""), MonUi.Int(s.Sessions), MonUi.LogAsPct(s.LogReturn), MonUi.LogAsPct(s.ExpectedMean),
                    MonUi.LogAsPct(s.BandLo) + " … " + MonUi.LogAsPct(s.BandHi), MonUi.Num(s.Z), (s.DecayState ?? "—").ToUpperInvariant(),
                }), "Report strategies", new double[] { 0, 60, 70, 70, 140, 50, 90 }));
            var alerts = n?.Alerts;
            stack.Children.Add(MonUi.Head("Alerts in the period"));
            var byKind = alerts?.ByKind ?? new Dictionary<string, JsonElement>();
            stack.Children.Add(MonUi.Line(MonUi.Int(alerts?.Total) + " alert(s)" + (byKind.Count > 0 ? ": " + string.Join(", ", byKind.Select(kv => KindLabel(kv.Key) + " " + FundFormat.Plain(kv.Value))) : ""),
                                          11, FundPalette.Text, mono: true));
            var brokers = n?.Brokers ?? new List<MonReportBroker>();
            if (brokers.Count > 0)
            {
                stack.Children.Add(MonUi.Head("Latest reconciliation per broker"));
                stack.Children.Add(MonUi.Table(new[] { "Account", "Status", "At" },
                    brokers.Select(b => (IReadOnlyList<string>)new[] { b.AccountId ?? "?", (b.Status ?? "—").ToUpperInvariant(), TradingText.Ts(b.At) }), "Reconciliations", new double[] { 150, 120, 0 }));
            }
            var facts = r.Facts ?? new List<MonFact>();
            if (facts.Count > 0)
            {
                var factsTable = MonUi.Table(new[] { "Fact id", "Label", "Value" }, facts.Select(f => (IReadOnlyList<string>)new[] { f.Id ?? "", f.Label ?? "", f.Display ?? MonUi.Plain(f.Value) }),
                                             "Fact table", new double[] { 230, 0, 160 });
                var exp = new Expander
                {
                    Header = FundUi.Text("Fact table (" + facts.Count.ToString(Inv) + ") — exactly what an AI commentary would receive", 11, FundPalette.Text),
                    Content = factsTable,
                    Margin = new Thickness(0, 8, 0, 4),
                    Foreground = FundPalette.Text,
                };
                AutomationProperties.SetName(exp, "Fact table of the report");
                stack.Children.Add(exp);
            }

            // optional AI commentary (budget key "reports"; numbers only)
            var reportId = r.Id;
            var ai = new MonCommentaryPanel("report",
                "It receives ONLY the fact table above (numbers with generated labels; strategies as \"strategy 1..n\"; no names, symbols or account numbers).",
                (model, language) => reportId == null
                    ? Task.FromResult(new PlatformResult<MonCommentaryEnvelope> { Status = PlatformCallStatus.Error, Message = "The report has no id." })
                    : PlatformClient.CreateReportCommentaryAsync(reportId, model, language));
            ai.SetCommentaries(r.Commentaries);
            ai.Created += c => { r.Commentaries ??= new List<MonCommentary>(); r.Commentaries.Insert(0, c); };
            var aiRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 10, 0, 2) };
            aiRow.Children.Add(ai.TriggerButton);
            aiRow.Children.Add(new TextBlock { Text = "  optional · labelled AI output · costs money", FontSize = 10.5, Foreground = FundPalette.Faint, VerticalAlignment = VerticalAlignment.Center });
            stack.Children.Add(aiRow);
            stack.Children.Add(ai);
            return FundUi.Panel(stack, new Thickness(14, 12, 14, 12), new Thickness(0, 8, 0, 10));
        }

        // =====================================================================================
        // Tax reports notice (§19.15 — user decision 2026-10-18: UFOS.ai has NO tax export)
        // =====================================================================================

        private async Task LoadTaxNoticeAsync()
        {
            var res = await PlatformClient.GetTaxNoticeAsync();
            if (_stopped) return;
            if (res.Ok && res.Value != null && !string.IsNullOrWhiteSpace(res.Value.Notice)) { _taxNotice = res.Value; _taxNoticeError = null; }
            else
            {
                _taxNotice = null;
                _taxNoticeError = !res.Ok ? PlatformClient.DescribeFailure(res) : "the backend sent no notice text (/v2/monitoring/tax-notice)";
            }
            RenderTaxNotice();
        }

        /// <summary>The fixed notice exactly as the backend words it (plain text); no stand-in text when the call fails.</summary>
        private void RenderTaxNotice()
        {
            var panel = _taxPanel;
            panel.Children.Clear();
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text("Tax reports", 12.5, FundPalette.TextBright, bold: true));
            if (_taxNotice?.Notice is string notice && notice.Length > 0)
            {
                var t = MonUi.Line(notice, 11.5, FundPalette.Text);
                t.LineHeight = 18;
                AutomationProperties.SetName(t, "Tax reports: " + notice);
                stack.Children.Add(t);
                if (!string.IsNullOrWhiteSpace(_taxNotice.PaperNotice))
                {
                    var paper = MonUi.Line(_taxNotice.PaperNotice, 11, FundPalette.Dim);
                    AutomationProperties.SetName(paper, _taxNotice.PaperNotice);
                    stack.Children.Add(paper);
                }
            }
            else
            {
                var err = MonUi.ErrorBlock("Tax reports notice not available (" + (_taxNoticeError ?? "not loaded yet") + ").");
                stack.Children.Add(err);
            }
            var card = FundUi.Panel(stack, new Thickness(14, 12, 14, 12), new Thickness(0, 0, 0, 10));
            AutomationProperties.SetName(card, "Tax reports notice");
            panel.Children.Add(card);
        }

        /// <summary>A file name without path characters (the backend's suggestion, else the fallback).</summary>
        internal static string SafeFileName(string? name, string fallback)
        {
            var leaf = Path.GetFileName(name ?? "") ?? "";
            var invalid = Path.GetInvalidFileNameChars();
            var clean = new string(leaf.Where(c => !invalid.Contains(c) && c >= 0x20).ToArray()).Trim();
            return clean.Length == 0 ? fallback : clean;
        }
    }
}

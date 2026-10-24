using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// S10-C R3 — the Council of AIs as a platform strategy, at the top of the Overview (SPEC §22.11 "Council view", §22.8 switch-over /
    /// revert). Header: label, record, task, next due, AI budget; actions: Run now, the "Council orders" switch, AI budget…, Switch
    /// over… / Revert…; the latest decision per symbol (agents, verdict with AI labelling, target vs position, execution outcome or why
    /// no order) and the recent runs. Polls GET /v2/council/state every 15 s while visible. <see cref="StateChanged"/> tells the window
    /// whether the old engine is still the active one (switch-over not "done").
    /// </summary>
    internal sealed class CouncilView : UserControl
    {
        private const string LogModule = "HedgeFund";
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(15);

        private readonly StackPanel _root = new();
        private readonly DispatcherTimer _timer;
        private CouncilState? _state;
        private List<CouncilRun>? _runs;
        private string? _problem;
        private string? _runsProblem;
        private CouncilPendingAttempt? _pending;
        private string? _pendingProblem;
        private bool _loading;
        private bool _refreshAgain;
        private bool _busy;

        /// <summary>Raised after every load: the state (null when it could not be read).</summary>
        public event Action<CouncilState?>? StateChanged;

        public CouncilState? State => _state;

        public CouncilView()
        {
            Content = FundUi.Panel(_root, margin: new Thickness(0, 0, 0, 10));
            _timer = new DispatcherTimer { Interval = PollInterval };
            _timer.Tick += async (_, _) => await RefreshAsync();
            IsVisibleChanged += async (_, _) =>
            {
                if (IsVisible) { _timer.Start(); await RefreshAsync(); }
                else _timer.Stop();
            };
            // S10-C R3: the toast sources' problems are shown here too (no silent poll failure) — re-render when they change
            Action onProblem = () => Dispatcher.BeginInvoke(new Action(Render));
            Loaded += (_, _) => { MonitoringNotifier.ProblemChanged += onProblem; FundNotifications.ProblemChanged += onProblem; };
            Unloaded += (_, _) =>
            {
                _timer.Stop();
                MonitoringNotifier.ProblemChanged -= onProblem;
                FundNotifications.ProblemChanged -= onProblem;
            };
            Render();
        }

        public async Task RefreshAsync()
        {
            // Review: a refresh asked for while a poll is in flight (e.g. right after an action) is QUEUED — the poll that started before
            // the action may carry the old state, so one more load follows it (no waiting for the next 15 s tick).
            if (_loading) { _refreshAgain = true; return; }
            _loading = true;
            try
            {
                do
                {
                    _refreshAgain = false;
                    await LoadOnceAsync();
                } while (_refreshAgain);
            }
            finally
            {
                _loading = false;
                _refreshAgain = false;
            }
        }

        private async Task LoadOnceAsync()
        {
            try
            {
                var res = await PlatformClient.GetCouncilStateAsync();
                if (res.Ok && res.Value != null)
                {
                    _state = res.Value;
                    _problem = null;
                    var runs = await PlatformClient.GetCouncilRunsAsync(10);
                    if (runs.Ok)
                    {
                        _runs = runs.Value?.Runs ?? new List<CouncilRun>();
                        _runsProblem = null;
                    }
                    else
                    {
                        // Review: never keep a stale run list silently
                        _runsProblem = "Runs could not be loaded: " + PlatformClient.DescribeFailure(runs)
                                       + " (as of " + DateTime.Now.ToString("HH:mm", Inv) + ")" + (_runs != null && _runs.Count > 0 ? " — the list below is older." : "");
                    }
                    // Review M2: an unfinished attempt shows its stored error and time (GET /v2/council/switchover → pending)
                    if (_state.Switchover == "pending")
                    {
                        var plan = await PlatformClient.GetCouncilSwitchoverPlanAsync();
                        _pending = plan.Ok ? plan.Value?.Pending : _pending;
                        _pendingProblem = plan.Ok ? null : "The attempt's details could not be loaded: " + PlatformClient.DescribeFailure(plan);
                    }
                    else
                    {
                        _pending = null;
                        _pendingProblem = null;
                    }
                }
                else
                {
                    _problem = res.Offline ? "Backend not reachable — " + res.Message : PlatformClient.DescribeFailure(res);
                    if (res.Status == PlatformCallStatus.NotFound) _state = null;   // older backend without the Council API
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Council view: refresh failed.", ex);
                _problem = "The Council state could not be shown (" + ex.GetType().Name + ").";
            }
            Render();
            StateChanged?.Invoke(_problem == null ? _state : null);
        }

        // ------------------------------------------------------------------ rendering

        private void Render()
        {
            _root.Children.Clear();
            var s = _state;
            var head = new Grid();
            head.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var title = new StackPanel { Orientation = Orientation.Horizontal };
            title.Children.Add(FundUi.Text("Council of AIs — platform strategy", 13, FundPalette.Text, bold: true, wrap: false));
            var chip = FundUi.Text(s?.Label ?? "not validated — AI strategy", 10, FundPalette.Risk, mono: true, wrap: false);
            chip.Margin = new Thickness(10, 2, 0, 0);
            title.Children.Add(chip);
            head.Children.Add(title);
            var actions = new WrapPanel { HorizontalAlignment = HorizontalAlignment.Right };
            Grid.SetColumn(actions, 1);
            head.Children.Add(actions);
            _root.Children.Add(head);

            if (s == null)
            {
                _root.Children.Add(Line(_problem ?? "Loading the Council state…", _problem != null ? FundPalette.Risk : FundPalette.Dim));
                return;
            }
            var done = s.SwitchedOver;
            var pending = s.Switchover == "pending";

            // status lines
            var sw = s.Switchover switch
            {
                "done" => "active (switched over)",
                "pending" => "switch-over UNFINISHED — retry or abort it below; otherwise it is only recovered when the backend restarts",
                "reverted" => "not active — reverted to the old engine",
                "none" => "not active — the old engine (below) still trades",
                "unreadable" => "switch-over state unreadable",
                _ => s.Switchover ?? "unknown",
            };
            var task = s.TaskState switch
            {
                "idle" => "idle",
                "running" => "running a cycle",
                "sandbox" => "sandbox — no cycles",
                "not_active" => "not active",
                _ => s.TaskState ?? "—",
            };
            _root.Children.Add(Line("Council: " + sw + " · task " + task
                                    + (s.NextDueAt != null ? " · next cycle " + FundFormat.LocalTime(s.NextDueAt, "ddd HH:mm") : "")
                                    + (s.Record?.Text != null ? " · " + s.Record.Text : ""), done ? FundPalette.Text : FundPalette.Dim));
            _root.Children.Add(Line(BudgetText(s.Budget), s.Budget?.Status is "reached" or "unreadable" ? FundPalette.Risk : FundPalette.Dim));
            var orders = done
                ? (s.OrdersEnabled ? "Council orders: ON — it places paper orders on paper-council." : "Council orders: OFF — it decides but places no orders.")
                : "Council orders: OFF (the Council is not active).";
            _root.Children.Add(Line(orders + " Paper trading · simulated money.", s.OrdersEnabled ? FundPalette.Long : FundPalette.Dim));
            if (_problem != null) _root.Children.Add(Line(_problem + " Showing the last known state.", FundPalette.Risk));
            if (pending)
            {
                var err = _pending?.LastError ?? s.SwitchoverError;
                var at = _pending?.ErrorAt != null ? " (" + FundFormat.LocalTime(_pending.ErrorAt, "yyyy-MM-dd HH:mm") + ")" : "";
                _root.Children.Add(Line(err != null
                    ? "Last error of the unfinished switch-over" + at + ": " + err
                    : "The switch-over attempt is unfinished; no error is stored (it may still be running — wait a moment, then retry or abort).",
                    FundPalette.Kill));
                if (_pending != null)
                    _root.Children.Add(Line("Attempt " + (_pending.Tx ?? "?") + " started " + FundFormat.LocalTime(_pending.At, "yyyy-MM-dd HH:mm")
                                            + " · its database transaction " + (_pending.Applied ? "was already applied" : "was not applied yet")
                                            + " · before it: " + (_pending.Previous ?? "?"), FundPalette.Dim));
                if (_pendingProblem != null) _root.Children.Add(Line(_pendingProblem, FundPalette.Risk));
            }
            // toast source (S10-C R3): after the switch-over the Council's events reach the user as monitoring alerts
            foreach (var problem in new[] { MonitoringNotifier.PollProblem, MonitoringNotifier.ToastProblem, done ? null : FundNotifications.SourceProblem })
                if (!string.IsNullOrWhiteSpace(problem)) _root.Children.Add(Line("Notifications: " + problem, FundPalette.Risk));
            if (done)
                _root.Children.Add(Line("Notifications: the Council's events (recommendation changed, order, cycle failed, AI budget reached, orders "
                                        + "switched off) arrive as monitoring alerts — toasts and Monitoring → Alerts.", FundPalette.Faint));

            // actions
            if (done)
            {
                actions.Children.Add(MakeAction("Run now", "Starts one Council cycle now (POST /v2/council/run)", RunNowAsync,
                                            enabled: s.TaskState == "idle"));
                actions.Children.Add(MakeAction(s.OrdersEnabled ? "Turn Council orders off…" : "Turn Council orders on…",
                                            s.OrdersEnabled ? "Stops the Council from placing orders and cancels its working orders"
                                                            : "Lets the Council place paper orders on paper-council",
                                            () => SetOrdersAsync(!s.OrdersEnabled)));
                actions.Children.Add(MakeAction("AI budget…", "The Council's daily AI budget (a new Council params version)", EditBudgetAsync));
                actions.Children.Add(MakeAction("Revert…", "Go back to the old engine (possible until the old engine is removed)", RevertAsync, danger: true));
            }
            else if (pending)
            {
                actions.Children.Add(MakeAction("Retry switch-over…", "Run the unfinished attempt again", async () => { await CouncilPending.RetryAsync(Window.GetWindow(this)); }));
                actions.Children.Add(MakeAction("Abort switch-over…", "Abandon the attempt and go back to the old engine", async () => { await CouncilPending.AbortAsync(Window.GetWindow(this)); },
                                                danger: true));
            }
            else
            {
                actions.Children.Add(MakeAction("Switch over…", "Replace the old engine by the Council — shows the dry run first", SwitchOverAsync));
            }

            // symbols of the latest run
            var symbols = s.Symbols ?? new List<CouncilSymbol>();
            if (symbols.Count > 0)
            {
                var run = s.LastRun;
                var sub = FundUi.Text("Latest run" + (run != null ? " #" + run.Id.ToString(Inv) + " · " + FundFormat.LocalTime(run.StartedAt, "yyyy-MM-dd HH:mm")
                                                     + " · " + (run.Status ?? "?") + (run.Legacy == true ? " · legacy (old engine)" : "") : ""),
                                      11.5, FundPalette.Text, bold: true);
                sub.Margin = new Thickness(0, 10, 0, 4);
                _root.Children.Add(sub);
                foreach (var sym in symbols) _root.Children.Add(SymbolCard(sym, run));
            }
            else if (done)
            {
                _root.Children.Add(Line("No Council decision yet — the first cycle runs on the schedule (or press Run now).", FundPalette.Dim));
            }

            // recent runs
            if (_runsProblem != null) _root.Children.Add(Line(_runsProblem, FundPalette.Risk));
            var runs = _runs ?? new List<CouncilRun>();
            if (runs.Count > 0)
            {
                var hdr = FundUi.Text("Recent runs", 11.5, FundPalette.Text, bold: true);
                hdr.Margin = new Thickness(0, 10, 0, 2);
                _root.Children.Add(hdr);
                foreach (var r in runs)
                    _root.Children.Add(Line("#" + r.Id.ToString(Inv) + "  " + FundFormat.LocalTime(r.StartedAt, "MM-dd HH:mm") + "  " + (r.Trigger ?? "?")
                                            + "  " + (r.Status ?? "?") + (r.CostUsd is double c ? "  " + FundFormat.Money(c, decimals: 3) : "")
                                            + (r.Legacy == true ? "  · legacy (old engine)" : r.OrdersEnabled == false ? "  · orders off" : ""),
                                            r.Status == "error" ? FundPalette.Risk : FundPalette.Dim, mono: true));
            }
        }

        private static string BudgetText(CouncilBudget? b)
        {
            if (b == null || b.LimitUsd == null) return "AI budget: —";
            var spent = b.SpentUsd is double s ? FundFormat.Money(s, decimals: 3) : "unreadable";
            return b.Status switch
            {
                "off" => "AI budget: off (0) — the judge makes no AI calls; carried decisions are still sized",
                "reached" => "AI budget reached: " + spent + " of " + FundFormat.Money(b.LimitUsd) + " today — symbols that need a judgement get none until tomorrow (UTC) or a higher limit",
                "unreadable" => "AI budget: today's spend could not be read — AI calls are paused",
                _ => "AI budget: " + spent + " of " + FundFormat.Money(b.LimitUsd) + " today (UTC)",
            };
        }

        private UIElement SymbolCard(CouncilSymbol sym, CouncilRun? run)
        {
            var stack = new StackPanel();
            var exec = sym.Execution;
            var execText = exec == null ? "no execution record"
                : (exec.Outcome ?? "?") + (string.IsNullOrWhiteSpace(exec.OrderState) ? "" : " (order " + exec.OrderState + ")")
                  + (string.IsNullOrWhiteSpace(exec.Reason) ? "" : " — " + exec.Reason);
            var line = (sym.Symbol ?? "?") + "  " + (sym.Action ?? "—").ToUpperInvariant()
                       + (sym.Confidence is double c ? "  conf " + c.ToString("0.00", Inv) : "")
                       + "  target " + FundFormat.Fraction(sym.TargetWeight, 1, signed: true)
                       + " (now " + FundFormat.Fraction(sym.Position?.Weight, 1, signed: true) + ")  ·  " + execText;
            stack.Children.Add(FundUi.Text(line, 11.5, FundPalette.TextBright, mono: true));
            var agents = sym.Agents ?? new List<CouncilAgent>();
            if (agents.Count > 0)
            {
                var a = FundUi.Text(string.Join("  ·  ", agents.Select(x => (x.Agent ?? "?") + " " + (x.Score is double sc ? sc.ToString("+0.00;-0.00;0.00", Inv) : "—")
                                                                           + (x.Confidence is double cf ? " (" + cf.ToString("0.00", Inv) + ")" : "")
                                                                           + (x.AiGenerated ? " ✦AI" : ""))), 10.5, FundPalette.Dim, mono: true);
                a.Margin = new Thickness(0, 2, 0, 4);
                stack.Children.Add(a);
            }
            if (!string.IsNullOrWhiteSpace(sym.UnavailableReason))
                stack.Children.Add(FundUi.Text("No judgement: " + sym.UnavailableReason, 10.5, FundPalette.Risk));
            if (!string.IsNullOrWhiteSpace(sym.Rationale))
                stack.Children.Add(FundUi.TextBlockMaybeAi("", sym.Rationale, sym.AiGenerated ? sym.Provenance : null, null, run?.StartedAt,
                                                           carried: sym.JudgeSource == "carried",
                                                           nonAiLabel: sym.JudgeSource == "rules" ? "Rule-based · no AI" : "No AI provenance"));
            return new Border
            {
                BorderBrush = FundPalette.Hairline,
                BorderThickness = new Thickness(0, 1, 0, 0),
                Padding = new Thickness(0, 6, 0, 2),
                Child = stack,
            };
        }

        private static TextBlock Line(string text, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, 11, brush, mono: mono);
            t.Margin = new Thickness(0, 3, 0, 0);
            return t;
        }

        private Button MakeAction(string text, string tooltip, Func<Task> run, bool enabled = true, bool danger = false)
        {
            var b = danger ? FundUi.MakeButton(text, FundPalette.ButtonBg, FundPalette.ButtonBorder, FundPalette.Kill) : FundUi.MakeButton(text);
            b.FontSize = 11.5;
            b.Padding = new Thickness(10, 3, 10, 3);
            b.Margin = new Thickness(8, 0, 0, 4);
            b.ToolTip = tooltip;
            b.IsEnabled = enabled && !_busy;
            AutomationProperties.SetName(b, text.TrimEnd('…') + " — " + tooltip);
            b.Click += async (_, _) =>
            {
                if (_busy) return;
                _busy = true;
                Render();
                try { await run(); }
                catch (Exception ex)
                {
                    AppLog.Warn(LogModule, "Council view: action '" + text + "' failed unexpectedly.", ex);
                    FundDialogs.Info(Window.GetWindow(this), text.TrimEnd('…'), "The action failed (" + ex.GetType().Name + "). The view reloads now.");
                }
                finally
                {
                    _busy = false;
                    Render();   // buttons back at once, also when the refresh below is queued behind a running poll
                }
                await RefreshAsync();
            };
            return b;
        }

        // ------------------------------------------------------------------ actions

        private async Task RunNowAsync()
        {
            var res = await PlatformClient.RunCouncilNowAsync();
            if (!res.Ok)
                FundDialogs.Info(Window.GetWindow(this), "Run now", PlatformClient.DescribeFailure(res));
        }

        private async Task SetOrdersAsync(bool enable)
        {
            var owner = Window.GetWindow(this);
            var values = FundDialogs.Form(owner, enable ? "Turn Council orders on" : "Turn Council orders off",
                enable
                    ? "From the next cycle the Council places paper orders on paper-council to reach its target weights (subject to the account's "
                      + "risk limits and the auto-trading switch). Paper trading only — simulated money."
                    : "The Council keeps deciding but places no orders; its working orders are cancelled now. Positions stay as they are.",
                new[] { ("Note (optional, stored in the audit log)", "") },
                enable ? "Turn orders on" : "Turn orders off", danger: !enable);
            if (values == null) return;
            var res = await PlatformClient.SetCouncilOrdersAsync(enable, values.Length > 0 ? values[0] : null);
            if (!res.Ok)
            {
                var code = PlatformClient.ErrorCode(res);
                FundDialogs.Info(owner, "Council orders", code == "halted"
                    ? "paper-council is halted — resume it first (toolbar → Resume…)."
                    : PlatformClient.DescribeFailure(res));
            }
            else if (!enable && res.Value?.CancelledOrders is int n && n > 0)
            {
                FundDialogs.Info(owner, "Council orders off", n.ToString(Inv) + " working Council order(s) cancelled.");
            }
        }

        private async Task EditBudgetAsync()
        {
            var owner = Window.GetWindow(this);
            var current = await PlatformClient.GetCouncilParamsAsync();
            if (!current.Ok || current.Value?.Params is not System.Text.Json.JsonElement p || p.ValueKind != System.Text.Json.JsonValueKind.Object)
            {
                FundDialogs.Info(owner, "AI budget", "The Council params could not be read: " + (current.Ok ? "empty answer" : PlatformClient.DescribeFailure(current)));
                return;
            }
            double? now = p.TryGetProperty("ai_daily_budget_usd", out var b) && b.ValueKind == System.Text.Json.JsonValueKind.Number ? b.GetDouble() : null;
            string range = "";
            double? min = null, max = null;
            if (current.Value.Limits is System.Text.Json.JsonElement l && l.ValueKind == System.Text.Json.JsonValueKind.Object
                && l.TryGetProperty("ai_daily_budget_usd", out var lim) && lim.ValueKind == System.Text.Json.JsonValueKind.Object)
            {
                if (lim.TryGetProperty("min", out var mn) && mn.ValueKind == System.Text.Json.JsonValueKind.Number) min = mn.GetDouble();
                if (lim.TryGetProperty("max", out var mx) && mx.ValueKind == System.Text.Json.JsonValueKind.Number) max = mx.GetDouble();
                if (min != null || max != null) range = " Allowed: " + FundFormat.Money(min) + " – " + FundFormat.Money(max) + ".";
            }
            var values = FundDialogs.Form(owner, "Council AI budget",
                "The Council's daily AI budget in US dollars (UTC day; 0 = no AI calls — carried decisions are still sized). Stored as a new "
                + "Council params version." + range,
                new[] { ("Daily budget (USD)", now?.ToString("0.##", Inv) ?? ""), ("Note (optional, stored in the audit log)", "") },
                "Save budget");
            if (values == null) return;
            if (!double.TryParse(values[0].Trim().Replace(',', '.'), NumberStyles.Float, Inv, out var usd) || usd < 0 || double.IsNaN(usd)
                || (min != null && usd < min) || (max != null && usd > max))
            {
                FundDialogs.Info(owner, "Council AI budget", "Not saved: enter a number" + (range.Length > 0 ? " in the allowed range." : " ≥ 0."));
                return;
            }
            var res = await PlatformClient.SetCouncilBudgetAsync(Math.Round(usd, 2), values.Length > 1 ? values[1] : null);
            if (!res.Ok) FundDialogs.Info(owner, "Council AI budget", "Not saved: " + PlatformClient.DescribeFailure(res));
        }

        private async Task SwitchOverAsync()
        {
            var dialog = new CouncilSwitchoverDialog(Window.GetWindow(this));
            dialog.ShowDialog();
            await Task.CompletedTask;
        }

        private async Task RevertAsync() => await CouncilRevert.RunAsync(Window.GetWindow(this));
    }
}

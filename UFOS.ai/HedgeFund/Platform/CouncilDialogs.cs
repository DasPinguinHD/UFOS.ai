using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// S10-C R3 — "Switch over…" (SPEC §22.8 R2, §22.14): shows the dry run of GET /v2/council/switchover (blockers, the Council params
    /// copied from the old engine, the heartbeat setting, the AI spend seed, the legacy import) and, on the user's confirmation, runs
    /// POST /v2/council/switchover {reason?}. A normal confirm with an optional note (professional style, no checkbox). The result is
    /// shown in place; afterwards the Council is active and its orders are OFF until the user turns them on.
    /// </summary>
    internal sealed class CouncilSwitchoverDialog : FundPopupWindow
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private readonly StackPanel _body = new() { Margin = new Thickness(20, 16, 20, 16) };
        private readonly TextBox _note;
        private readonly TextBlock _status;
        private readonly Button _confirm;
        private readonly Button _cancel;
        private bool _running;

        /// <summary>True when the switch-over was confirmed by the backend (state "done") — or may have happened (timeout).</summary>
        public bool Changed { get; private set; }

        public CouncilSwitchoverDialog(Window? owner) : base(owner, "Switch over to the Council", 720, 640, resizable: true)
        {
            _note = FundUi.Input("", mono: false);
            _note.MaxLength = 200;
            AutomationProperties.SetName(_note, "Note for the audit log (optional)");
            _status = FundUi.Text("Loading the dry run…", 11.5, FundPalette.Dim);
            _confirm = FundUi.MakeButton("Switch over to the Council", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            _confirm.IsEnabled = false;
            _confirm.Click += async (_, _) => await ConfirmAsync();
            _cancel = FundUi.MakeButton("Cancel");
            _cancel.IsCancel = true;
            _cancel.Click += (_, _) => { if (!_running) Close(); };
            Closing += (_, e) => { if (_running) e.Cancel = true; };   // never closed while the POST is in flight
            _body.Children.Add(FundUi.Text("Switch over to the Council", 14, FundPalette.TextBright, bold: true));
            _body.Children.Add(_status);
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = _body });
            Loaded += async (_, _) => await LoadAsync();
        }

        private async Task LoadAsync()
        {
            var res = await PlatformClient.GetCouncilSwitchoverPlanAsync();
            if (!res.Ok || res.Value == null)
            {
                _status.Text = "The dry run could not be loaded: " + (res.Ok ? "empty answer" : PlatformClient.DescribeFailure(res));
                _status.Foreground = FundPalette.Kill;
                _body.Children.Add(Buttons(confirm: false));
                return;
            }
            var p = res.Value;
            _body.Children.Remove(_status);
            var intro = FundUi.Text(
                "The old engine is replaced by the Council of AIs as a platform strategy on the paper account paper-council. "
                + "What happens on confirmation: the old engine is stopped (a running cycle is awaited), then ONE database transaction copies "
                + "its settings into the Council params, moves the heartbeat setting, carries today's AI spend over and imports the old run "
                + "history (read-only, marked \"legacy\"). Afterwards the Council runs on its schedule with its orders OFF — you turn them on "
                + "in the Council view. Paper trading only — simulated money. Until the old engine is removed (R4) the switch-over can be "
                + "reverted.", 11.5, FundPalette.Dim);
            intro.Margin = new Thickness(0, 6, 0, 10);
            intro.LineHeight = 17;
            _body.Children.Add(intro);

            var blockers = p.Blockers ?? new List<CouncilBlocker>();
            if (blockers.Count > 0)
            {
                _body.Children.Add(Section("Blocked — resolve these first", FundPalette.Kill));
                foreach (var b in blockers)
                {
                    _body.Children.Add(Bullet((b.Message ?? b.Code ?? "unknown blocker") + (string.IsNullOrWhiteSpace(b.Code) ? "" : "  [" + b.Code + "]"), FundPalette.Kill));
                    if (!string.IsNullOrWhiteSpace(b.Table) || b.Rows != null)
                        _body.Children.Add(Bullet("Table " + (b.Table ?? "(not named)") + " · "
                                                  + (b.Rows is long n ? n.ToString(System.Globalization.CultureInfo.InvariantCulture) + " duplicate row(s)" : "row count not reported"),
                                                  FundPalette.Kill, indent: 22));
                    if (CouncilPending.BlockerHint(b.Code) is { Length: > 0 } hint) _body.Children.Add(Bullet(hint, FundPalette.Risk, indent: 22));
                }
            }

            _body.Children.Add(Section("State", null));
            _body.Children.Add(Bullet("Switch-over state now: " + (p.State ?? "unknown") + " · parity report accepted: " + (p.ParityAccepted ?? "no"), FundPalette.Text));

            _body.Children.Add(Section("Council params (copied from the old engine)", null));
            var from = p.Params?.FromVersion is JsonElement fv && fv.ValueKind != JsonValueKind.Null ? "v" + FundFormat.Plain(fv) : "—";
            _body.Children.Add(Bullet("From the old engine's settings " + from + ":", FundPalette.Text));
            if (p.Params?.Council is JsonElement cp && cp.ValueKind == JsonValueKind.Object)
                foreach (var prop in cp.EnumerateObject())
                    _body.Children.Add(Bullet(prop.Name + " = " + FundFormat.Plain(prop.Value), FundPalette.Dim, indent: 22));
            foreach (var n in p.Params?.Notes ?? new List<string>())
                _body.Children.Add(Bullet(n, FundPalette.Risk, indent: 22));

            _body.Children.Add(Section("Heartbeat and AI spend", null));
            var hb = p.HeartbeatMinutes is JsonElement h && h.ValueKind != JsonValueKind.Null ? FundFormat.Plain(h) + " min" : "not set — auto trading stays off until it is set";
            _body.Children.Add(Bullet("Dead-man heartbeat for auto trading: " + hb, FundPalette.Text));
            _body.Children.Add(Bullet("Today's AI spend carried over to the Council budget: "
                                      + (p.SpendSeedUsd is double s ? FundFormat.Money(s) : "unreadable (blocks the switch-over)"), FundPalette.Text));

            _body.Children.Add(Section("Legacy history import (read-only)", null));
            var lg = p.Legacy;
            _body.Children.Add(Bullet((lg?.Runs ?? 0).ToString(Inv) + " runs, " + (lg?.Signals ?? 0).ToString(Inv) + " agent signals, "
                                      + (lg?.Recommendations ?? 0).ToString(Inv) + " recommendations", FundPalette.Text));
            _body.Children.Add(Bullet("High-water mark " + Plain(lg?.HwmBefore) + " → " + Plain(lg?.HwmAfter)
                                      + " (a later switch-over after a revert imports only newer old runs)", FundPalette.Dim));

            var noteLabel = FundUi.Text("Note (optional, stored in the audit log)", 11, FundPalette.Dim);
            noteLabel.Margin = new Thickness(0, 14, 0, 4);
            _body.Children.Add(noteLabel);
            _body.Children.Add(_note);
            _status.Text = blockers.Count > 0 ? "The switch-over is blocked (see above)." : "";
            _status.Foreground = blockers.Count > 0 ? FundPalette.Kill : FundPalette.Dim;
            _status.Margin = new Thickness(0, 10, 0, 0);
            _body.Children.Add(_status);
            // SWITCHOVER_FIX_API §2: the backend says what may be offered (a pending attempt is retried / aborted in the Council view)
            _confirm.IsEnabled = blockers.Count == 0 && p.Ok && (p.Actions?.Run ?? true);
            if (p.State == "pending")
            {
                _status.Text = "A switch-over attempt is unfinished — close this dialog and use \"Retry switch-over…\" or \"Abort switch-over…\" in the Council view.";
                _status.Foreground = FundPalette.Risk;
                _confirm.IsEnabled = false;
            }
            _body.Children.Add(Buttons(confirm: true));
        }

        private async Task ConfirmAsync()
        {
            if (_running) return;
            _running = true;
            _confirm.IsEnabled = false;
            _cancel.IsEnabled = false;
            _confirm.Content = "Switching over…";
            _status.Text = "Stopping the old engine and running the switch-over transaction — this can take up to two minutes.";
            _status.Foreground = FundPalette.Dim;
            PlatformResult<CouncilSwitchoverResult> res;
            try { res = await PlatformClient.SwitchOverToCouncilAsync(_note.Text); }
            finally
            {
                _running = false;
                _cancel.IsEnabled = true;
                _cancel.Content = "Close";
            }
            if (res.Ok && res.Value?.State == "done")
            {
                Changed = true;
                var r = res.Value;
                _confirm.Content = "Switched over";
                _status.Text = CouncilPending.DoneText(r);
                _status.Foreground = r.CouncilTaskStarted == false ? FundPalette.Risk : FundPalette.Long;
                return;
            }
            if (PlatformClient.ErrorCode(res) == "switchover_failed")
            {
                // SWITCHOVER_FIX_API §4: it failed AFTER "pending" — the attempt stays unfinished until retried / aborted (or a backend restart)
                Changed = true;
                _confirm.Content = "Switch over to the Council";
                _status.Text = "The switch-over started but did not finish: " + (PlatformClient.SwitchoverLastError(res) ?? PlatformClient.DescribeFailure(res))
                               + "\nThe attempt is now PENDING — close this dialog and use \"Retry switch-over…\" or \"Abort switch-over…\" in the Council view.";
                _cancel.Content = "Close";
                _status.Foreground = FundPalette.Kill;
                return;
            }
            if (res.TimedOut)
            {
                Changed = true;   // the caller reloads the state
                _confirm.Content = "Switch over to the Council";
                _status.Text = "No answer in time — the switch-over may have completed (the backend finishes an interrupted attempt at its next "
                               + "start). Close this dialog; the Council view shows the current state.";
                _status.Foreground = FundPalette.Risk;
                return;
            }
            _confirm.Content = "Switch over to the Council";
            var failCode = PlatformClient.ErrorCode(res);
            _confirm.IsEnabled = failCode is not ("sandbox" or "switchover_blocked");
            _status.Text = "Not switched over: " + CouncilPending.FailureText(res);
            _status.Foreground = FundPalette.Kill;
        }

        private UIElement Buttons(bool confirm)
        {
            var row = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 14, 0, 0) };
            row.Children.Add(_cancel);
            if (confirm)
            {
                _confirm.Margin = new Thickness(8, 0, 0, 0);
                row.Children.Add(_confirm);
            }
            return row;
        }

        private static string Plain(JsonElement? e) => e is JsonElement v && v.ValueKind != JsonValueKind.Null ? FundFormat.Plain(v) : "—";

        internal static TextBlock Section(string title, Brush? brush)
        {
            var t = FundUi.Text(title, 12.5, brush ?? FundPalette.TextBright, bold: true);
            t.Margin = new Thickness(0, 12, 0, 4);
            return t;
        }

        internal static TextBlock Bullet(string text, Brush brush, double indent = 6)
        {
            var t = FundUi.Text("• " + text, 11.5, brush);
            t.Margin = new Thickness(indent, 1, 0, 1);
            return t;
        }
    }

    /// <summary>S10-C R3 — the revert of the switch-over (SPEC §22.8, POST /v2/council/switchover/revert), confirmed with an optional note.</summary>
    internal static class CouncilRevert
    {
        /// <summary>True when the backend reverted (or may have — timeout); false when cancelled or refused (the reason was shown).</summary>
        public static async Task<bool> RunAsync(Window? owner)
        {
            var values = FundDialogs.Form(owner, "Revert the switch-over",
                "Goes back to the old engine. On confirmation:\n"
                + "• the Council orders are switched OFF and the Council's working orders are cancelled,\n"
                + "• the Council stops running cycles,\n"
                + "• today's AI spend is carried over to the old engine's budget,\n"
                + "• the old engine is started again with its own, untouched settings (its autopilot stays off).\n\n"
                + "The Council's records stay in the database (insert-only) and are shown again after a later switch-over, which then imports "
                + "only the old runs newer than this time. paper-council's positions are not changed. Possible until the old engine is "
                + "removed (R4).\n\nPaper trading only — simulated money.",
                new[] { ("Note (optional, stored in the audit log)", "") },
                "Revert to the old engine", danger: true);
            if (values == null) return false;
            var res = await PlatformClient.RevertCouncilSwitchoverAsync(values.Length > 0 ? values[0] : null);
            if (res.Ok)
            {
                // Review minor 2 / SWITCHOVER_FIX_API §6: "runs again" only when the old engine actually started; an already reverted
                // state is never reported as "not reverted".
                var r = res.Value;
                var head = r?.Already == true ? "The switch-over was already reverted." : "Switch-over reverted.";
                FundDialogs.Info(owner, r?.OldEngineStarted == true ? "Switch-over reverted" : "Switch-over reverted — old engine NOT running",
                    head + "\n\n" + CouncilPending.OldEngineText(r?.OldEngineStarted, r?.OldEngineError)
                    + (r?.Already == true ? "" : "\nToday's AI spend carried over: " + FundFormat.Money(r?.OldSpendSeedUsd) + "."));
                return true;
            }
            if (res.TimedOut)
            {
                FundDialogs.Info(owner, "Revert", "No answer in time — the revert may have been applied. The view reloads; check the Council state before trying again.");
                return true;
            }
            if (PlatformClient.ErrorCode(res) == "not_switched_over")
            {
                // never "Not reverted" when it already is (e.g. reverted meanwhile from elsewhere): look at the state first
                var st = await PlatformClient.GetCouncilStateAsync();
                if (st.Ok && st.Value?.Switchover == "reverted")
                {
                    FundDialogs.Info(owner, "Revert", "The switch-over is already reverted — the old engine is the active one. The view reloads now.");
                    return true;
                }
                if (st.Ok && st.Value?.Switchover == "pending")
                {
                    FundDialogs.Info(owner, "Revert", "The switch-over attempt is unfinished (pending) — use \"Abort switch-over…\" in the Council view instead.");
                    return true;
                }
            }
            FundDialogs.Info(owner, "Revert", "Not reverted: " + CouncilPending.FailureText(res));
            return false;
        }
    }

    /// <summary>
    /// Review M2 (SWITCHOVER_FIX_API §3 / §5): an unfinished (pending) switch-over attempt — retry the SAME attempt or abort it back to the old
    /// engine. Both are normal confirms with an optional note. Without one of them the attempt is only recovered at a backend restart.
    /// </summary>
    internal static class CouncilPending
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        public static string DoneText(CouncilSwitchoverResult r)
            => "Switched over. The Council is active (params v" + (r.ParamsVersion?.ToString(Inv) ?? "?")
               + ", AI spend carried over " + FundFormat.Money(r.SpendSeedUsd) + ", attempt " + (r.Tx ?? "?") + "). "
               + "Council orders are OFF — turn them on in the Council view when you want it to trade on paper-council."
               + (r.CouncilTaskStarted == false
                   ? "\nThe Council's cycle task did NOT start" + (string.IsNullOrWhiteSpace(r.CouncilTaskError) ? "" : ": " + r.CouncilTaskError)
                     + " — restart the Hedge Fund backend; until then no scheduled cycle runs."
                   : "");

        /// <summary>
        /// A readable message for a failed switch-over / retry / abort / revert call (SWITCHOVER_FIX_API §1–§6): the backend's text plus
        /// what to do for the known codes; 503 switchover_failed shows its last_error.
        /// </summary>
        public static string FailureText<T>(PlatformResult<T> res)
        {
            var code = PlatformClient.ErrorCode(res);
            var detail = PlatformClient.DescribeFailure(res);
            return code switch
            {
                "switchover_running" => "Another switch-over operation (switch-over, retry, abort or revert) is running right now — from this or "
                                        + "another window. Wait until it has finished, then look at the Council view.",
                "concurrent_import" => "The old engine's history changed while the switch-over was being prepared — nothing was written. Try again.",
                "switchover_failed" => "It started but did not finish: " + (PlatformClient.SwitchoverLastError(res) ?? detail)
                                       + "\nThe attempt is now PENDING — use \"Retry switch-over…\" or \"Abort switch-over…\" in the Council view.",
                "not_pending" => "There is no unfinished switch-over attempt (any more) — the Council view shows the current state.",
                "not_switched_over" => "The Council is not switched over — nothing to revert.",
                "sandbox" => "Sandbox mode — switch-over operations are refused (turn the sandbox off first).",
                "switchover_blocked" => detail + BlockerHints(res),
                _ => detail,
            };
        }

        /// <summary>What to do for the known blocker codes (appended to the backend's own blocker texts).</summary>
        public static string BlockerHint(string? code) => code switch
        {
            "switchover_pending" => "A switch-over attempt is unfinished — retry or abort it in the Council view.",
            "legacy_duplicates" => "The old engine's history was imported twice into the Council tables (from before schema v26) — this must be "
                                   + "resolved in the database before switching over; see SPEC §22.14 and the critical alert in Monitoring → Alerts.",
            "council_unavailable" => "The Council service could not be built when the backend started — see backend.log, then restart the backend.",
            "not_migrated" => "Migrate the old paper account first (Portfolio & Trading → migration).",
            "autopilot_on" => "Switch the old engine's autopilot off first (Overview → Execution).",
            "spend_unreadable" => "Today's AI spend could not be read — the switch-over refuses until it can (check the cost log / database).",
            "no_backup" => "The pre-v25 database backup is missing — the switch-over needs it as the emergency rollback.",
            _ => "",
        };

        private static string BlockerHints<T>(PlatformResult<T> res)
        {
            if (string.IsNullOrWhiteSpace(res.RawBody)) return "";
            var hints = new List<string>();
            try
            {
                using var doc = System.Text.Json.JsonDocument.Parse(res.RawBody);
                if (doc.RootElement.ValueKind == System.Text.Json.JsonValueKind.Object && doc.RootElement.TryGetProperty("blockers", out var bl)
                    && bl.ValueKind == System.Text.Json.JsonValueKind.Array)
                    foreach (var b in bl.EnumerateArray())
                        if (b.ValueKind == System.Text.Json.JsonValueKind.Object && b.TryGetProperty("code", out var c) && c.ValueKind == System.Text.Json.JsonValueKind.String
                            && BlockerHint(c.GetString()) is { Length: > 0 } h)
                            hints.Add(h);
            }
            catch (System.Text.Json.JsonException) { }
            return hints.Count == 0 ? "" : "\n" + string.Join("\n", hints.Distinct());
        }

        public static string OldEngineText(bool? started, string? error)
            => started == true
                ? "The old engine runs again (its autopilot is off — switch it on in the Overview's Execution panel if wanted)."
                : "The old engine did NOT start" + (string.IsNullOrWhiteSpace(error) ? " (no reason reported)" : ": " + error!.Trim())
                  + " — restart the Hedge Fund backend; it starts the old engine.";

        public static async Task<bool> RetryAsync(Window? owner)
        {
            var values = FundDialogs.Form(owner, "Retry the switch-over",
                "Runs the unfinished switch-over attempt again (the same attempt): the old engine is stopped if it still runs, the one database "
                + "transaction is applied unless it already was, then the Council becomes active with its orders OFF.\n\nPaper trading only — simulated money.",
                new[] { ("Note (optional, stored in the audit log)", "") }, "Retry switch-over");
            if (values == null) return false;
            var res = await PlatformClient.RetryCouncilSwitchoverAsync(values.Length > 0 ? values[0] : null);
            if (res.Ok && res.Value?.State == "done")
                FundDialogs.Info(owner, "Switched over", DoneText(res.Value));
            else if (res.TimedOut)
                FundDialogs.Info(owner, "Retry switch-over", "No answer in time — the retry may have completed. The Council view reloads now.");
            else
                FundDialogs.Info(owner, "Retry switch-over", PlatformClient.ErrorCode(res) == "switchover_failed"
                    ? "Not finished: " + (PlatformClient.SwitchoverLastError(res) ?? PlatformClient.DescribeFailure(res)) + "\nThe attempt stays pending — retry again or abort it."
                    : "Not retried: " + CouncilPending.FailureText(res));
            return true;
        }

        public static async Task<bool> AbortAsync(Window? owner)
        {
            var values = FundDialogs.Form(owner, "Abort the switch-over",
                "Abandons the unfinished switch-over attempt and goes back to the old engine:\n"
                + "• the Council orders are switched OFF,\n"
                + "• if the attempt's database transaction was not applied yet, nothing else changes; if it was, this works like a revert "
                + "(today's AI spend carried back, the imported Council rows stay — a later switch-over imports only newer old runs),\n"
                + "• the old engine is started again with its autopilot OFF.\n\nPaper trading only — simulated money.",
                new[] { ("Note (optional, stored in the audit log)", "") }, "Abort switch-over", danger: true);
            if (values == null) return false;
            var res = await PlatformClient.AbortCouncilSwitchoverAsync(values.Length > 0 ? values[0] : null);
            if (res.Ok)
            {
                var r = res.Value;
                FundDialogs.Info(owner, r?.OldEngineStarted == true ? "Switch-over aborted" : "Switch-over aborted — old engine NOT running",
                    "State: " + (r?.State ?? "?") + (r?.Applied == true ? " (the transaction had been applied — treated like a revert)." : " (nothing had been applied).")
                    + "\n\n" + OldEngineText(r?.OldEngineStarted, r?.OldEngineError));
            }
            else
            {
                FundDialogs.Info(owner, "Abort switch-over", res.TimedOut
                    ? "No answer in time — the abort may have been applied. The Council view reloads now."
                    : "Not aborted: " + CouncilPending.FailureText(res));
            }
            return true;
        }
    }

    /// <summary>
    /// S10-C R3 — "Resume…" of the toolbar (SPEC §22.8 "Kill / resume across the switch-over"): lists the halted platform accounts
    /// (paper-main, paper-council, broker accounts) and — only while the old engine still runs (not switched over) and is halted — the old
    /// engine. One POST /v2/oms/resume per chosen account, POST /fund/resume for the old engine. Resuming paper-council clears its halt
    /// only: Council orders stay OFF until turned on in the Council view.
    /// </summary>
    internal sealed class ResumeDialog : FundPopupWindow
    {
        private readonly List<(CheckBox Box, string? AccountId)> _choices = new();
        private readonly TextBox _note;
        private readonly TextBlock _status;
        private readonly Button _resume;

        public ResumeDialog(Window? owner, IReadOnlyList<TradingAccountSummary> halted, bool oldEngineHalted, bool switchedOver)
            : base(owner, "Resume", 560, 420, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Resume halted accounts", 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text("Clears the halt of the chosen accounts so orders may be placed again. Auto trading stays OFF — switch it on "
                                    + "explicitly. The drawdown peak is rebased to the current equity. Paper trading only — simulated money.",
                                    11.5, FundPalette.Dim);
            intro.Margin = new Thickness(0, 6, 0, 10);
            stack.Children.Add(intro);

            foreach (var a in halted)
            {
                var id = a.Id ?? "?";
                var extra = id == PlatformClient.CouncilAccountId ? " — Council orders stay OFF until you turn them on in the Council view" : "";
                var reason = string.IsNullOrWhiteSpace(a.Halt?.Reason) ? "" : " (halted: " + a.Halt!.Reason!.Trim() + ")";
                AddChoice(stack, id + reason + extra, id);
            }
            if (oldEngineHalted && !switchedOver)
                AddChoice(stack, "Old engine (Overview) — its autopilot stays off", null);
            if (_choices.Count == 0)
                stack.Children.Add(FundUi.Text(switchedOver
                    ? "No platform account is halted."
                    : "No platform account and not the old engine is halted.", 11.5, FundPalette.Text));

            var noteLabel = FundUi.Text("Note (optional, stored in the audit log)", 11, FundPalette.Dim);
            noteLabel.Margin = new Thickness(0, 12, 0, 4);
            stack.Children.Add(noteLabel);
            _note = FundUi.Input("", mono: false);
            _note.MaxLength = 200;
            AutomationProperties.SetName(_note, "Note for the audit log (optional)");
            stack.Children.Add(_note);
            _status = FundUi.Text("", 11.5, FundPalette.Dim);
            _status.Margin = new Thickness(0, 10, 0, 0);
            stack.Children.Add(_status);

            var row = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 14, 0, 0) };
            var close = FundUi.MakeButton("Close");
            close.IsCancel = true;
            close.Click += (_, _) => Close();
            row.Children.Add(close);
            _resume = FundUi.MakeButton("Resume selected");
            _resume.Margin = new Thickness(8, 0, 0, 0);
            _resume.IsEnabled = _choices.Count > 0;
            _resume.Click += async (_, _) => await ResumeAsync();
            row.Children.Add(_resume);
            stack.Children.Add(row);
            SetBody(stack);
        }

        /// <summary>True once at least one resume call was sent (the caller refreshes).</summary>
        public bool Changed { get; private set; }

        private void AddChoice(Panel stack, string text, string? accountId)
        {
            var box = new CheckBox { Content = text, IsChecked = true, Foreground = FundPalette.Text, FontSize = 11.5, Margin = new Thickness(6, 3, 0, 3) };
            AutomationProperties.SetName(box, "Resume " + text);
            stack.Children.Add(box);
            _choices.Add((box, accountId));
        }

        private async Task ResumeAsync()
        {
            var chosen = _choices.Where(c => c.Box.IsChecked == true).ToList();
            if (chosen.Count == 0) { _status.Text = "Choose at least one."; _status.Foreground = FundPalette.Risk; return; }
            _resume.IsEnabled = false;
            _resume.Content = "Resuming…";
            var lines = new List<string>();
            var failed = false;
            foreach (var (box, id) in chosen)
            {
                Changed = true;
                if (id == null)
                {
                    var r = await FundClient.ResumeAsync(_note.Text);
                    lines.Add("Old engine: " + (r.Ok ? "resumed" : "NOT resumed — " + r.Describe()));
                    failed |= !r.Ok;
                }
                else
                {
                    var r = await PlatformClient.ResumeTradingAccountAsync(id, _note.Text.Trim());
                    lines.Add(id + ": " + (r.Ok ? "resumed" : r.TimedOut ? "no answer in time — may be resumed, check Portfolio & Trading"
                                                                          : "NOT resumed — " + PlatformClient.DescribeFailure(r)));
                    failed |= !r.Ok;
                }
                box.IsEnabled = false;
            }
            _status.Text = string.Join("\n", lines);
            _status.Foreground = failed ? FundPalette.Kill : FundPalette.Long;
            _resume.Content = "Resume selected";
        }
    }
}

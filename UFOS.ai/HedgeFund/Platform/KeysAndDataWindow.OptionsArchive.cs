using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// ⚙ Keys &amp; data → Data sources → "Options archive (SPY/QQQ)" card (OS1, SPEC §21, OS1_API.md + Round 2). Shows the
    /// archive's status (GET /v2/options-archive/status — read-only, works before the archive ever ran: "not set up"; "running"
    /// only from the service heartbeat, a manual run as "manual run finished at …"), the Windows task state (schtasks /Query,
    /// read-only — on open, ↻ and after an action, never on the timer), today's coverage and the last error. Actions: "Gap
    /// report…", "Register background task…" (one confirmation dialog; starts now and at every logon), "Unregister…" and "Run
    /// one snapshot now…" — Register / Run once disabled with the reason in sandbox mode or when the paths do not check out.
    /// Every action is audited. The periodic refresh is the window's auto refresh (10 s while settling / backend down, else 60 s);
    /// an action's refresh is queued behind a running one; pre-action service / manual values are never shown as current.
    /// </summary>
    public partial class KeysAndDataWindow
    {
        private bool _oaClosed;
        private bool _oaLoading;
        private bool _oaRefreshQueued;
        private bool _oaRefreshQueuedQuery;
        private OaStatus? _oaStatus;
        private string? _oaStatusError;
        private OaRegistration? _oaReg;
        private string? _oaRegError;
        private OaPathsChecked? _oaPaths;
        private string? _oaPathsError;
        private OaTaskState? _oaTask;
        private string? _oaTaskError;
        private bool _oaTaskChecking;
        private (string Text, Brush Brush)? _oaMessage;

        // autorefresh (user 2026-10-24): never show pre-action values as current. After Register / Unregister the service line and
        // chip wait for a heartbeat NEWER than the action (and, after a re-register, another pid); after Run once the manual line
        // waits for a newer finish. The window's auto refresh (KeysAndDataWindow.AutoRefresh.cs) polls every 10 s until settled.
        private sealed record OaAction(string Kind, DateTimeOffset At, int? OldPid);
        private OaAction? _oaSvcAction;   // register / unregister (kept until superseded — the freshness rule)
        private OaAction? _oaRunAction;   // run once

        /// <summary>Called from the constructor (after InitBrokers). The periodic refresh is the window's auto refresh.</summary>
        private void InitOptionsArchive()
        {
            Loaded += async (_, _) => await RefreshOptionsArchiveAsync(queryTask: true);
            Closed += (_, _) => _oaClosed = true;
            RenderOptionsArchive();
        }

        // ---------- freshness after an action ----------

        private static DateTimeOffset? OaTs(string? iso) => FundFormat.ParseTs(iso);

        /// <summary>The status's service values are current: no register / unregister since, or a heartbeat newer than it (and another pid after a re-register).</summary>
        private bool OaServiceFresh()
        {
            var a = _oaSvcAction;
            var svc = _oaStatus?.Service;
            if (a == null) return true;
            if (svc == null || OaTs(svc.HeartbeatAt) is not DateTimeOffset hb || hb <= a.At) return false;
            return a.Kind != "register" || a.OldPid == null || svc.Pid != a.OldPid;
        }

        /// <summary>True while a pythonw process with this pid still exists (a reused pid of another program counts as ended).</summary>
        private static bool OaProcessAlive(int? pid)
        {
            if (pid is not int id) return false;
            try
            {
                using var p = Process.GetProcessById(id);
                return !p.HasExited && p.ProcessName.Equals("pythonw", StringComparison.OrdinalIgnoreCase);
            }
            catch (ArgumentException) { return false; }      // no such process
            catch (InvalidOperationException) { return false; }
            catch (Exception) { return true; }                // cannot tell (access) — keep waiting
        }

        private bool OaManualFresh()
            => _oaRunAction == null || (OaTs(_oaStatus?.Manual?.FinishedAt) is DateTimeOffset f && f > _oaRunAction.At);

        /// <summary>Registers an action for the freshness rule and the window's settle polling (10 s, max 2 min, then a clear note).</summary>
        private void BeginOaAction(string kind)
        {
            var action = new OaAction(kind, DateTimeOffset.UtcNow, _oaStatus?.Service?.Pid);
            if (kind == "run_once") _oaRunAction = action; else _oaSvcAction = action;
            AddSettle("oa-" + (kind == "run_once" ? "run" : "svc"), () => OaActionReached(action), () =>
            {
                _oaMessage = (kind switch
                {
                    "register" => "Still not confirmed after 2 minutes: no heartbeat of a new archive process yet — check Task Scheduler (task \"" + OaTask.TaskName + "\") and options_archive.log.",
                    "unregister" => "Still not confirmed after 2 minutes: the task or process " + (action.OldPid?.ToString(CultureInfo.InvariantCulture) ?? "—") + " is still there — check Task Manager / Task Scheduler.",
                    _ => "Still not confirmed after 2 minutes: the status shows no newer manual run — see options_archive.log.",
                }, FundPalette.Risk);
                RenderOptionsArchive();
            });
        }

        private bool OaActionReached(OaAction a) => a.Kind switch
        {
            "register" => _oaTask?.Registered == true && _oaStatus?.Service?.Alive == true && OaServiceFresh(),
            "unregister" => _oaTask?.Registered == false && !OaProcessAlive(a.OldPid),
            _ => OaManualFresh(),
        };

        /// <summary>
        /// Status (+ the registration payload and the path check when asked or not loaded yet, + the schtasks query when asked).
        /// Review m10: a refresh requested while one runs is not dropped — it runs once more afterwards (a timer tick is dropped).
        /// Never throws.
        /// </summary>
        private async Task RefreshOptionsArchiveAsync(bool queryTask, bool fromTimer = false)
        {
            if (_oaClosed) return;
            if (_oaLoading)
            {
                if (!fromTimer)
                {
                    _oaRefreshQueued = true;
                    _oaRefreshQueuedQuery |= queryTask;
                }
                return;
            }
            _oaLoading = true;
            try
            {
                while (true)
                {
                    await RefreshOptionsArchiveOnceAsync(queryTask);
                    if (_oaClosed || !_oaRefreshQueued) break;
                    queryTask = _oaRefreshQueuedQuery;
                    _oaRefreshQueued = false;
                    _oaRefreshQueuedQuery = false;
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: options archive refresh failed.", ex);
            }
            finally { _oaLoading = false; }
        }

        private async Task RefreshOptionsArchiveOnceAsync(bool queryTask)
        {
            var st = await PlatformClient.GetOptionsArchiveStatusAsync();
            if (_oaClosed) return;
            if (st.Ok && st.Value != null) { _oaStatus = st.Value; _oaStatusError = null; }
            else _oaStatusError = st.Ok ? "unexpected response from the backend (/v2/options-archive/status)"
                                        : st.HttpStatus == 404 && PlatformClient.ErrorCode(st) == null
                                            ? "the running backend has no options-archive routes (older than OS1) — restart it from the Hedge Fund window"
                                            : PlatformClient.DescribeFailure(st);
            if (_oaReg == null || queryTask)
            {
                var reg = await PlatformClient.GetOptionsArchiveRegistrationAsync();
                if (_oaClosed) return;
                if (reg.Ok && reg.Value != null) { _oaReg = reg.Value; _oaRegError = null; }
                else _oaRegError = reg.Ok ? "unexpected response from the backend (/v2/options-archive/registration)" : PlatformClient.DescribeFailure(reg);
                if (_oaReg != null) (_oaPaths, _oaPathsError) = OaTask.Validate(_oaReg);
                else { _oaPaths = null; _oaPathsError = _oaRegError; }
            }
            RenderOptionsArchive();
            if (queryTask) await QueryOaTaskAsync();
        }

        private async Task QueryOaTaskAsync()
        {
            if (_oaTaskChecking) return;
            _oaTaskChecking = true;
            RenderOptionsArchive();
            try
            {
                var (state, err) = await OaTask.QueryAsync(_oaPaths);
                if (_oaClosed) return;
                _oaTask = state;
                _oaTaskError = err;
            }
            finally
            {
                _oaTaskChecking = false;
                if (!_oaClosed) RenderOptionsArchive();
            }
        }

        // ---------- rendering ----------

        private (string Text, Brush Brush) OaChip()
        {
            var s = _oaStatus;
            var svc = s?.Service;
            if (s == null) return ("STATUS NOT AVAILABLE", FundPalette.Risk);
            if (!OaServiceFresh())
            {
                // never show pre-action values as current
                if (_oaSvcAction!.Kind == "register") return ("STARTING…", FundPalette.Focus);
                if (OaProcessAlive(_oaSvcAction.OldPid) || _oaTask?.Registered == true) return ("STOPPING…", FundPalette.Risk);
                return ("NOT REGISTERED · STOPPED", FundPalette.Faint);
            }
            if (svc?.Alive == true && svc.State == "collecting") return ("COLLECTING", FundPalette.Focus);
            if (svc?.Alive == true && svc.State == "error") return ("RUNNING · ERROR", FundPalette.Kill);
            if (svc?.Alive == true) return ("RUNNING · IDLE", FundPalette.Long);
            if (_oaTask?.Registered == true) return ("REGISTERED · NOT RUNNING", FundPalette.Risk);
            if (s.State == "error") return ("ERROR", FundPalette.Kill);
            if (s.IsNotSetUp) return ("NOT SET UP", FundPalette.Faint);
            if (_oaTask?.Registered == false) return ("NOT REGISTERED", FundPalette.Faint);
            return ("NOT RUNNING", FundPalette.Dim);
        }

        /// <summary>Why Register / Run once are disabled (sandbox, pythonw missing, paths that do not check out), or null.</summary>
        private string? OaActionsBlocked()
        {
            if (_oaStatus?.ActionsEnabled == false) return _oaStatus.ActionsNote ?? (_oaStatus.Sandbox == true ? "disabled in sandbox mode" : "disabled by the backend (no reason given)");
            if (_oaReg?.Available == false) return _oaReg.UnavailableReason ?? (_oaReg.Sandbox == true ? "disabled in sandbox mode" : "not available (no reason given)");
            if (_oaReg == null) return _oaRegError != null ? "registration data not available: " + _oaRegError : "registration data not loaded yet";
            if (_oaPaths == null) return "refused: " + (_oaPathsError ?? "the paths could not be checked");
            return null;
        }

        private void RenderOptionsArchive()
        {
            var host = OptionsArchiveHost;
            host.Children.Clear();
            var stack = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text("Options archive (SPY/QQQ)  ", 13, FundPalette.TextBright, bold: true, wrap: false));
            var (chipText, chipBrush) = OaChip();
            head.Children.Add(MonUi.Chip(chipText, chipBrush));
            if (_oaStatus?.Sandbox == true || _oaReg?.Sandbox == true)
            {
                var sb = MonUi.Chip("SANDBOX", FundPalette.Risk);
                sb.Margin = new Thickness(6, 0, 0, 0);
                head.Children.Add(sb);
            }
            stack.Children.Add(head);
            stack.Children.Add(MonUi.Line("An independent background process (a Windows task of your user — once registered it starts now and at every logon; own "
                                          + "database) that records option-chain snapshots and open interest for SPY and QQQ plus the VIX family from FRED — point "
                                          + "in time, gaps recorded, never filled in. Research data only; it sends no orders.", 10.5, FundPalette.Faint));

            if (_oaStatusError != null) stack.Children.Add(MonUi.ErrorBlock("Archive status not available: " + _oaStatusError));
            var s = _oaStatus;
            if (s != null)
            {
                if (s.IsNotSetUp)
                    stack.Children.Add(MonUi.Line("Not set up — the archive has never run on this PC (no database, no status file yet). Register the background task "
                                                  + "below, or run one snapshot to try it.", 11, FundPalette.Dim));
                var rows = new List<(string, string)>();
                var svc = s.Service;
                rows.Add(("Windows task", _oaTaskChecking ? "checking…"
                    : _oaTask != null ? (_oaTask.Registered
                                            ? "registered" + (_oaTask.SameAction == false ? " (other paths: " + (_oaTask.Command ?? "?") + " " + (_oaTask.Arguments ?? "") + " — unregister and register again)" : "")
                                            : "not registered")
                    : "not available" + (_oaTaskError != null ? " (" + _oaTaskError + ")" : "")));
                var svcFresh = OaServiceFresh();
                if (!svcFresh)
                    rows.Add(("Service", _oaSvcAction!.Kind == "register"
                        ? "starting… — waiting for the first heartbeat" + (_oaSvcAction.OldPid is int op ? " of a new process (old pid " + op.ToString(CultureInfo.InvariantCulture) + ")" : "")
                        : OaProcessAlive(_oaSvcAction.OldPid)
                            ? "stopping… — waiting for process " + _oaSvcAction.OldPid!.Value.ToString(CultureInfo.InvariantCulture) + " to end"
                            : "stopped" + (_oaSvcAction.OldPid is int gp ? " — process " + gp.ToString(CultureInfo.InvariantCulture) + " ended" : "")));
                else rows.Add(("Service", svc == null || svc.StatusFile != true ? "no status file yet"
                    : (svc.Alive == true ? "running" : svc.Alive == false ? "not running" : "unknown") + (string.IsNullOrWhiteSpace(svc.State) ? "" : " · " + svc.State)
                      + (svc.HeartbeatAt != null ? " · heartbeat " + TradingText.Ts(svc.HeartbeatAt) : "") + (svc.Pid is int pid ? " · pid " + pid.ToString(CultureInfo.InvariantCulture) : "")));
                if (!OaManualFresh())
                    rows.Add(("Manual run", "running… — waiting for the result"));
                else if (s.Manual is OaManual man)
                    rows.Add(("Manual run", "finished at " + TradingText.Ts(man.FinishedAt) + " · " + OaText.SlotStatusLabel(man.Status)
                                            + (man.ExitCode is int ec ? " · " + OaText.RunOnceExit(ec) : "")
                                            + (man.RunIds is { Count: > 0 } ids ? " · run " + string.Join(", ", ids.Select(i => i.ToString(CultureInfo.InvariantCulture))) : "")));
                var lr = s.LastRun;
                rows.Add(("Last run", lr == null ? "none yet"
                    : (lr.Kind ?? "?") + (string.IsNullOrWhiteSpace(lr.Label) ? "" : " (" + lr.Label + ")") + " · slot " + FundFormat.LocalTime(lr.SlotNy, "yyyy-MM-dd HH:mm")
                      + " · " + OaText.SlotStatusLabel(lr.Status) + " · " + (lr.Requests?.ToString(CultureInfo.InvariantCulture) ?? "—") + " requests · finished "
                      + TradingText.Ts(lr.FinishedAt) + (lr.SkewMs is long sk && Math.Abs(sk) >= 1000 ? " · clock skew " + (sk / 1000.0).ToString("0.#", CultureInfo.InvariantCulture) + " s" : "")
                      + (string.IsNullOrWhiteSpace(lr.Error) ? "" : " · " + lr.Error)));
                var ns = s.NextSlot;
                rows.Add(("Next slot", ns == null ? "none scheduled" : (ns.Kind ?? "?") + " · " + FundFormat.LocalTime(ns.SlotUtc ?? ns.SlotNy, "yyyy-MM-dd HH:mm") + " (your time)"));
                var t = s.Today;
                rows.Add(("Today (NY)", t == null ? "—"
                    : (t.DateNy ?? "?") + (t.TradingDay == false ? " · no trading day"
                        : (t.EarlyClose == true ? " · early close" : "") + " · " + OaInt(t.Collected) + " of " + OaInt(t.ExpectedSlots) + " slots collected"
                          + " · partial " + OaInt(t.Partial) + " · late " + OaInt(t.Late) + " · failed " + OaInt(t.Failed) + " · missing " + OaInt(t.Missing)
                          + " · pending " + OaInt(t.Pending))));
                if (t?.FixedSlotGaps is JsonElement fg && fg.ValueKind != JsonValueKind.Null && !(fg.ValueKind == JsonValueKind.Array && fg.GetArrayLength() == 0)
                    && !(fg.ValueKind == JsonValueKind.Number && fg.TryGetInt32(out var fgn) && fgn == 0))
                    rows.Add(("Fixed-slot gaps", FundFormat.Plain(fg) + " (09:55 / 15:25 late, failed or missing — a gap for H1/H2)"));
                var c = s.Counts;
                if (c != null)
                    rows.Add(("Stored", OaLong(c.Runs) + " runs · " + OaLong(c.Snapshots) + " snapshots · " + OaLong(c.OiRows) + " OI rows · " + OaLong(c.FredRows)
                                        + " FRED rows · " + OaLong(c.Gaps) + " gaps"));
                if (s.Db?.SizeBytes is long size) rows.Add(("Database", (size / 1024.0 / 1024.0).ToString("0.0", CultureInfo.InvariantCulture) + " MB · schema v"
                                                                       + (s.Db.SchemaVersion?.ToString(CultureInfo.InvariantCulture) ?? "?")));
                if (!string.IsNullOrWhiteSpace(s.Db?.Error)) rows.Add(("Database error", s.Db!.Error!));
                stack.Children.Add(MonUi.Pairs(rows));
                if (!string.IsNullOrWhiteSpace(s.LastError))
                    stack.Children.Add(MonUi.Line("Last error: " + s.LastError, 10.5, FundPalette.Kill));
                if (s.Config is JsonElement cfg && cfg.ValueKind == JsonValueKind.Object && cfg.TryGetProperty("provisional", out var prov) && prov.ValueKind == JsonValueKind.True)
                    stack.Children.Add(MonUi.Line("Filters and cadence are PROVISIONAL (SPEC §21) until the OS0 data check confirms them.", 10.5, FundPalette.Risk));
            }
            var blocked = OaActionsBlocked();
            if (blocked != null) stack.Children.Add(MonUi.Line("Register / Run once: " + blocked, 10.5, FundPalette.Risk));
            if (_oaMessage is { } msg) stack.Children.Add(MonUi.Line(msg.Text, 11, msg.Brush));

            var actions = new WrapPanel { Margin = new Thickness(0, 8, 0, 0) };
            var gaps = MonUi.Small("Gap report…", "Show the gap report of the options archive (days × slots)");
            gaps.Click += async (sender, _) => await DialogGuard.RunAsync("oa-gaps", () => { new OptionsArchiveGapDialog(this).ShowDialog(); return Task.CompletedTask; }, sender);
            actions.Children.Add(OaGap(gaps));
            var known = _oaTask != null && !_oaTaskChecking;
            if (_oaTask?.Registered != true)
            {
                var reg = MonUi.Small("Register background task…", "Register the options archive as a Windows task of your user — it starts now and at every logon (asks for confirmation)");
                reg.IsEnabled = known && blocked == null;
                reg.ToolTip = blocked ?? (known ? "Starts now and at every logon (one confirmation)." : "Available once the task state is known (↻ Check task).");
                ToolTipService.SetShowOnDisabled(reg, true);
                reg.Click += async (sender, _) => await DialogGuard.RunAsync("oa-register", RegisterOaTaskAsync, sender);
                actions.Children.Add(OaGap(reg));
            }
            else
            {
                var unreg = MonUi.Small("Unregister…", "End the running archive and remove its Windows task (asks for confirmation)");
                unreg.IsEnabled = known;
                unreg.Click += async (sender, _) => await DialogGuard.RunAsync("oa-unregister", UnregisterOaTaskAsync, sender, "Removing…");
                actions.Children.Add(OaGap(unreg));
            }
            var runOnce = MonUi.Small("Run one snapshot now…", "Run the archive once now in a separate process (asks for confirmation)");
            runOnce.IsEnabled = blocked == null && !DialogGuard.IsRunning("oa-run-once");
            if (blocked != null) { runOnce.ToolTip = blocked; ToolTipService.SetShowOnDisabled(runOnce, true); }
            runOnce.Click += async (sender, _) => await DialogGuard.RunAsync("oa-run-once", RunOaOnceAsync, sender, "Running…");
            actions.Children.Add(OaGap(runOnce));
            var check = MonUi.Small("↻ Check task", "Refresh the archive status and the Windows task state");
            check.IsEnabled = !_oaTaskChecking;
            check.Click += async (sender, _) => await DialogGuard.RunAsync("oa-refresh", () => RefreshOptionsArchiveAsync(queryTask: true), sender, "Checking…");
            actions.Children.Add(OaGap(check));
            stack.Children.Add(actions);

            var panel = FundUi.Panel(stack, new Thickness(12, 10, 12, 10), new Thickness(0, 10, 0, 0));
            AutomationProperties.SetName(panel, "Options archive, " + chipText);
            host.Children.Add(panel);
        }

        private static Button OaGap(Button b)
        {
            b.Margin = new Thickness(0, 0, 8, 4);
            return b;
        }

        private static string OaInt(int? v) => v?.ToString(CultureInfo.InvariantCulture) ?? "—";
        private static string OaLong(long? v) => v?.ToString("N0", CultureInfo.InvariantCulture) ?? "—";

        // ---------- actions (each audited) ----------

        private async Task RegisterOaTaskAsync()
        {
            // the check runs again right before the dialog (paths or sandbox may have changed since the last refresh)
            await RefreshOptionsArchiveAsync(queryTask: true);
            var blocked = OaActionsBlocked();
            if (blocked != null || _oaReg == null || _oaPaths == null)
            {
                FundDialogs.Info(this, "Register the options archive", "Not possible: " + (blocked ?? "the registration data could not be checked") + ".");
                return;
            }
            var dialog = new OptionsArchiveRegisterDialog(this, _oaReg, _oaPaths);
            if (dialog.ShowDialog() != true) return;
            BeginOaAction("register");   // the pid shown before (if any) is the "old" one
            _oaMessage = ("✓ Registered — the archive starts now and at every logon (" + (dialog.Detail ?? "registered") + ").", FundPalette.Long);
            await RefreshOptionsArchiveAsync(queryTask: true);
        }

        private async Task UnregisterOaTaskAsync()
        {
            var stop = _oaReg?.Summary?.Stop;
            if (!FundDialogs.Confirm(this, "Unregister the options archive",
                    "Ends the running archive now (schtasks /End) and removes the Windows task \"" + OaTask.TaskName + "\" of your user (schtasks /Delete). "
                    + "The collected data (options_archive.db) stay. You can register it again later."
                    + (string.IsNullOrWhiteSpace(stop) ? "" : "\n\n" + stop),
                    "End and unregister \"" + OaTask.TaskName + "\"", danger: true))
                return;
            var (ok, detail) = await OaTask.UnregisterAsync();
            if (ok) BeginOaAction("unregister");
            await PlatformClient.PostOptionsArchiveAuditAsync(ok ? "unregistered" : "unregister_failed", detail);
            if (_oaClosed) return;
            _oaMessage = ok ? ("Unregistered (" + detail + ") — the data stay.", FundPalette.Dim) : ("Not unregistered: " + detail, FundPalette.Kill);
            await RefreshOptionsArchiveAsync(queryTask: true);
        }

        private async Task RunOaOnceAsync()
        {
            var blocked = OaActionsBlocked();
            if (blocked != null || _oaPaths == null)
            {
                FundDialogs.Info(this, "Run one snapshot now", "Not possible: " + (blocked ?? "the paths could not be checked") + ".");
                return;
            }
            if (_oaStatus?.Service?.Alive == true)
            {
                FundDialogs.Info(this, "Run one snapshot now", "The background archive is running — it holds the archive lock, so a manual run would only be refused. "
                                                               + "Its next slot: " + FundFormat.LocalTime(_oaStatus.NextSlot?.SlotUtc ?? _oaStatus.NextSlot?.SlotNy, "yyyy-MM-dd HH:mm") + ".");
                return;
            }
            var paths = _oaPaths;
            if (!FundDialogs.Confirm(this, "Run one snapshot now",
                    "Starts the options archive once in a separate process: one snapshot of the SPY / QQQ option chains (Alpaca, your paper keys, "
                    + "indicative feed) and the FRED series, written to options_archive.db with its gaps. It sends no orders. This can take a few minutes; "
                    + "the window stays usable.\n\nProgram: " + paths.Pythonw + " \"" + paths.Tool + "\" run-once\nWorking directory: " + paths.Workdir,
                    "Run one snapshot now (SPY/QQQ)"))
                return;
            _oaMessage = ("Running one snapshot…", FundPalette.Faint);
            BeginOaAction("run_once");
            RenderOptionsArchive();
            var res = await OaTask.RunOnceAsync(paths);
            var ok = res.Error == null && res.ExitCode == 0;
            var text = res.Error ?? OaText.RunOnceExit(res.ExitCode);
            await PlatformClient.PostOptionsArchiveAuditAsync(ok ? "run_once" : "run_once_failed", "exit " + (res.ExitCode?.ToString(CultureInfo.InvariantCulture) ?? "—"));
            if (_oaClosed) return;
            _oaMessage = (ok ? "✓ Manual run finished — " + text : "Manual run: " + text, ok ? FundPalette.Long : FundPalette.Kill);
            await RefreshOptionsArchiveAsync(queryTask: false);
        }
    }
}

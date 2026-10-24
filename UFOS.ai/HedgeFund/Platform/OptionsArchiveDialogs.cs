using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Xml.Linq;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Newsroom;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // OS1 options archive (SPY/QQQ) — task registration, process runner and dialogs of ⚙ Keys & data → Data sources (SPEC §21,
    // OS1_API.md 2026-10-24 + Round 2). Lead decision (review M1): NO PowerShell anywhere. The WPF builds the task XML from a
    // FIXED template and calls %SystemRoot%\System32\schtasks.exe (argument list, no shell, no window) — register / unregister
    // only after the user's confirmation; the automatic state check is schtasks /Query (read-only). Every path comes from the
    // app's own backend folder (the one it starts the backend from); the backend's registration payload must agree with it, or
    // the action is refused with the reason. Every action is posted to the audit route.
    // =====================================================================================

    /// <summary>Result of one local process run (exit code, stdout, stderr, or why it could not run).</summary>
    internal sealed record OaProcessResult(int? ExitCode, string StdOut, string StdErr, string? Error)
    {
        public bool Ok => Error == null && ExitCode == 0;

        /// <summary>A short, single-line description for the UI and the audit detail (≤ 240 characters, no secrets are ever involved).</summary>
        public string Describe()
        {
            if (Error != null) return Error;
            var err = FirstLine(StdErr);
            if (err.Length == 0) err = FirstLine(StdOut);
            return "exit code " + (ExitCode?.ToString(CultureInfo.InvariantCulture) ?? "?") + (err.Length > 0 ? " — " + err : "");
        }

        private static string FirstLine(string text)
        {
            var line = (text ?? "").Split('\n').Select(l => l.Trim()).FirstOrDefault(l => l.Length > 0) ?? "";
            return line.Length > 200 ? line.Substring(0, 199) + "…" : line;
        }
    }

    internal static class OaProcess
    {
        /// <summary>
        /// Runs <paramref name="file"/> (an absolute path) with <paramref name="args"/> (ArgumentList — no shell parsing) hidden, in
        /// <paramref name="workdir"/>; stdout/stderr captured (system encoding when <paramref name="encoding"/> is null — schtasks
        /// writes in the console code page); killed on time-out.
        /// </summary>
        public static async Task<OaProcessResult> RunAsync(string file, IReadOnlyList<string> args, string? workdir, TimeSpan timeout, Encoding? encoding = null)
        {
            var psi = new ProcessStartInfo
            {
                FileName = file,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            if (encoding != null)
            {
                psi.StandardOutputEncoding = encoding;
                psi.StandardErrorEncoding = encoding;
            }
            foreach (var a in args) psi.ArgumentList.Add(a);
            if (!string.IsNullOrWhiteSpace(workdir)) psi.WorkingDirectory = workdir;
            Process? p;
            try { p = Process.Start(psi); }
            catch (Exception ex) { return new OaProcessResult(null, "", "", "could not start " + Path.GetFileName(file) + " (" + ex.GetType().Name + ": " + ex.Message + ")"); }
            if (p == null) return new OaProcessResult(null, "", "", "could not start " + Path.GetFileName(file));
            using (p)
            {
                var stdout = p.StandardOutput.ReadToEndAsync();
                var stderr = p.StandardError.ReadToEndAsync();
                using var cts = new CancellationTokenSource(timeout);
                try { await p.WaitForExitAsync(cts.Token).ConfigureAwait(false); }
                catch (OperationCanceledException)
                {
                    try { p.Kill(entireProcessTree: true); } catch { /* already gone */ }
                    return new OaProcessResult(null, "", "", "no result within " + timeout.TotalSeconds.ToString("0", CultureInfo.InvariantCulture) + " s — the process was stopped");
                }
                return new OaProcessResult(p.ExitCode, await stdout.ConfigureAwait(false), await stderr.ConfigureAwait(false), null);
            }
        }
    }

    /// <summary>The validated, app-side paths of the archive (never taken from the backend unchecked).</summary>
    internal sealed record OaPathsChecked(string BackendDir, string Pythonw, string Tool, string Workdir);

    /// <summary>Task state from <c>schtasks /Query /XML ONE</c>: registered, and whether its action matches the expected one.</summary>
    internal sealed record OaTaskState(bool Registered, bool? SameAction, string? Command, string? Arguments);

    /// <summary>The Windows scheduled task of the archive — fixed name, fixed XML template (OS1_API.md Round 2), schtasks.exe only.</summary>
    internal static class OaTask
    {
        // Round 2b (lead): ROOT folder of the Task Scheduler library — a standard user may not create a \UFOS.ai\ folder.
        // ArgumentList quotes the name (it contains a space). /Create denied → the error is shown + audited, no other fallback.
        public const string TaskName = "UFOS.ai OptionsArchive";
        private static readonly TimeSpan SchtasksTimeout = TimeSpan.FromSeconds(30);

        private static string Schtasks => Path.Combine(Environment.SystemDirectory, "schtasks.exe");

        /// <summary>
        /// The app's own paths (backend folder = where the app starts its backend from) and the check that the backend's payload
        /// agrees with them exactly: pythonw = &lt;backend&gt;\.venv\Scripts\pythonw.exe (exists), tool = &lt;backend&gt;\tools\options_archive.py
        /// (exists), work dir = backend folder, task name = the constant; no quote / CR / LF anywhere. Null + reason = refuse.
        /// </summary>
        public static (OaPathsChecked? Paths, string? Error) Validate(OaRegistration? reg)
        {
            var appDir = NewsroomBackend.BackendDir;
            if (string.IsNullOrWhiteSpace(appDir)) return (null, "the app cannot find its backend folder (HedgeFund\\backend)");
            string backend;
            try { backend = Path.GetFullPath(appDir!).TrimEnd('\\'); }
            catch (Exception ex) { return (null, "the backend folder path is invalid (" + ex.GetType().Name + ")"); }
            var pythonw = Path.Combine(backend, ".venv", "Scripts", "pythonw.exe");
            var tool = Path.Combine(backend, "tools", "options_archive.py");
            foreach (var p in new[] { backend, pythonw, tool })
                if (p.IndexOfAny(new[] { '"', '\r', '\n' }) >= 0) return (null, "a path contains a quote or a line break — refused: " + p);
            if (!File.Exists(pythonw)) return (null, "pythonw.exe was not found at " + pythonw + " (the backend's virtual environment) — start the Hedge Fund backend once so it is set up");
            if (!File.Exists(tool)) return (null, "the archive tool was not found at " + tool);
            if (reg != null)
            {
                if (!string.Equals(reg.TaskName, TaskName, StringComparison.Ordinal))
                    return (null, "the backend names the task \"" + (reg.TaskName ?? "—") + "\", expected \"" + TaskName + "\" — refused");
                var mismatch = Differs(reg.BackendDir, backend, "backend folder") ?? Differs(reg.Workdir, backend, "working directory")
                               ?? Differs(reg.Pythonw, pythonw, "pythonw") ?? Differs(reg.Tool, tool, "tool");
                if (mismatch != null) return (null, mismatch);
            }
            return (new OaPathsChecked(backend, pythonw, tool, backend), null);
        }

        private static string? Differs(string? backendValue, string expected, string what)
        {
            if (string.IsNullOrWhiteSpace(backendValue)) return "the backend sent no " + what + " — refused";
            string full;
            try { full = Path.GetFullPath(backendValue!).TrimEnd('\\'); }
            catch (Exception) { return "the backend's " + what + " is not a valid path — refused"; }
            return string.Equals(full, expected.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)
                ? null
                : "the backend's " + what + " (" + backendValue + ") is not the app's (" + expected + ") — refused";
        }

        public static string CurrentUser => WindowsIdentity.GetCurrent().Name;

        /// <summary>The action line shown in the confirmation dialog — exactly what the task runs.</summary>
        public static string ActionLine(OaPathsChecked p) => p.Pythonw + " \"" + p.Tool + "\" service   (working directory " + p.Workdir + ")";

        /// <summary>The fixed task XML of OS1_API.md Round 2; values XML-escaped.</summary>
        public static string BuildXml(OaPathsChecked p, string user)
        {
            static string X(string v) => SecurityElement.Escape(v) ?? "";
            return "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\r\n"
                 + "<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\r\n"
                 + "  <RegistrationInfo>\r\n"
                 + "    <Description>UFOS.ai options archive (SPY/QQQ, data collection only - no trading). SP 21.4</Description>\r\n"
                 + "  </RegistrationInfo>\r\n"
                 + "  <Triggers>\r\n"
                 + "    <LogonTrigger>\r\n"
                 + "      <Enabled>true</Enabled>\r\n"
                 + "      <UserId>" + X(user) + "</UserId>\r\n"
                 + "    </LogonTrigger>\r\n"
                 + "  </Triggers>\r\n"
                 + "  <Principals>\r\n"
                 + "    <Principal id=\"Author\">\r\n"
                 + "      <UserId>" + X(user) + "</UserId>\r\n"
                 + "      <LogonType>InteractiveToken</LogonType>\r\n"
                 + "      <RunLevel>LeastPrivilege</RunLevel>\r\n"
                 + "    </Principal>\r\n"
                 + "  </Principals>\r\n"
                 + "  <Settings>\r\n"
                 + "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\n"
                 + "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n"
                 + "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n"
                 + "    <AllowHardTerminate>true</AllowHardTerminate>\r\n"
                 + "    <StartWhenAvailable>false</StartWhenAvailable>\r\n"
                 + "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\r\n"
                 + "    <IdleSettings>\r\n"
                 + "      <StopOnIdleEnd>false</StopOnIdleEnd>\r\n"
                 + "      <RestartOnIdle>false</RestartOnIdle>\r\n"
                 + "    </IdleSettings>\r\n"
                 + "    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n"
                 + "    <Enabled>true</Enabled>\r\n"
                 + "    <Hidden>false</Hidden>\r\n"
                 + "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\r\n"
                 + "    <WakeToRun>false</WakeToRun>\r\n"
                 + "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\r\n"
                 + "    <Priority>7</Priority>\r\n"
                 + "    <RestartOnFailure>\r\n"
                 + "      <Interval>PT5M</Interval>\r\n"
                 + "      <Count>3</Count>\r\n"
                 + "    </RestartOnFailure>\r\n"
                 + "  </Settings>\r\n"
                 + "  <Actions Context=\"Author\">\r\n"
                 + "    <Exec>\r\n"
                 + "      <Command>" + X(p.Pythonw) + "</Command>\r\n"
                 + "      <Arguments>" + X("\"" + p.Tool + "\" service") + "</Arguments>\r\n"
                 + "      <WorkingDirectory>" + X(p.Workdir) + "</WorkingDirectory>\r\n"
                 + "    </Exec>\r\n"
                 + "  </Actions>\r\n"
                 + "</Task>\r\n";
        }

        /// <summary>schtasks /Create /TN … /XML &lt;temp file&gt; /F, then /Run (starts now). The temp file (random name, user temp dir) is deleted afterwards.</summary>
        public static async Task<(bool Ok, string Detail)> RegisterAsync(OaPathsChecked p)
        {
            var temp = Path.Combine(Path.GetTempPath(), "ufos-oa-" + Guid.NewGuid().ToString("N") + ".xml");
            try
            {
                try { File.WriteAllText(temp, BuildXml(p, CurrentUser), new UnicodeEncoding(bigEndian: false, byteOrderMark: true)); }
                catch (Exception ex) { return (false, "could not write the task file (" + ex.GetType().Name + ")"); }
                var create = await OaProcess.RunAsync(Schtasks, new[] { "/Create", "/TN", TaskName, "/XML", temp, "/F" }, null, SchtasksTimeout);
                if (!create.Ok) return (false, "schtasks /Create: " + create.Describe());
            }
            finally
            {
                try { if (File.Exists(temp)) File.Delete(temp); } catch { /* best effort — the file holds no secret */ }
            }
            var run = await OaProcess.RunAsync(Schtasks, new[] { "/Run", "/TN", TaskName }, null, SchtasksTimeout);
            return run.Ok ? (true, "registered and started") : (true, "registered, but the immediate start failed: " + run.Describe() + " — it starts at the next logon");
        }

        /// <summary>schtasks /Query /TN … /XML ONE (read-only): exit 0 = registered (+ action compared with the expected one).</summary>
        public static async Task<(OaTaskState? State, string? Error)> QueryAsync(OaPathsChecked? expected)
        {
            var res = await OaProcess.RunAsync(Schtasks, new[] { "/Query", "/TN", TaskName, "/XML", "ONE" }, null, SchtasksTimeout);
            if (res.Error != null) return (null, res.Error);
            if (res.ExitCode != 0) return (new OaTaskState(false, null, null, null), null);
            try
            {
                var doc = XDocument.Parse(res.StdOut.Trim());
                XNamespace ns = "http://schemas.microsoft.com/windows/2004/02/mit/task";
                var exec = doc.Descendants(ns + "Exec").FirstOrDefault();
                var cmd = exec?.Element(ns + "Command")?.Value;
                var args = exec?.Element(ns + "Arguments")?.Value;
                bool? same = expected == null || cmd == null ? null
                    : string.Equals(cmd.Trim(), expected.Pythonw, StringComparison.OrdinalIgnoreCase)
                      && string.Equals((args ?? "").Trim(), "\"" + expected.Tool + "\" service", StringComparison.OrdinalIgnoreCase);
                return (new OaTaskState(true, same, cmd, args), null);
            }
            catch (Exception)
            {
                return (new OaTaskState(true, null, null, null), null);   // registered; its XML could not be read for the comparison
            }
        }

        /// <summary>schtasks /End (failure ignored — "not running"), then /Delete /F.</summary>
        public static async Task<(bool Ok, string Detail)> UnregisterAsync()
        {
            var end = await OaProcess.RunAsync(Schtasks, new[] { "/End", "/TN", TaskName }, null, SchtasksTimeout);
            var del = await OaProcess.RunAsync(Schtasks, new[] { "/Delete", "/TN", TaskName, "/F" }, null, SchtasksTimeout);
            return del.Ok ? (true, end.Ok ? "ended and removed" : "removed (it was not running)") : (false, "schtasks /Delete: " + del.Describe());
        }

        /// <summary>Run one snapshot: the validated pythonw, "&lt;tool&gt;" run-once, work dir = backend folder, hidden.</summary>
        public static Task<OaProcessResult> RunOnceAsync(OaPathsChecked p)
            => OaProcess.RunAsync(p.Pythonw, new[] { p.Tool, "run-once" }, p.Workdir, TimeSpan.FromMinutes(15), Encoding.UTF8);
    }

    internal static class OaText
    {
        public static string SlotStatusLabel(string? s) => s switch
        {
            "ok" => "OK",
            "partial" => "PARTIAL",
            "failed" => "FAILED",
            "late" => "LATE",
            "missing" => "MISSING",
            "pending" => "PENDING",
            null or "" => "—",
            _ => s!.ToUpperInvariant(),
        };

        public static Brush SlotBrush(string? s) => s switch
        {
            "ok" => FundPalette.Long,
            "partial" or "late" => FundPalette.Risk,
            "failed" or "missing" => FundPalette.Kill,
            "pending" => FundPalette.Faint,
            _ => FundPalette.Dim,
        };

        /// <summary>CLI exit codes (OS1_API.md + Round 2).</summary>
        public static string RunOnceExit(int? code) => code switch
        {
            0 => "OK — one snapshot collected",
            1 => "partial or failed run — see the gap report",
            2 => "keys missing or rejected (Alpaca / FRED) — check Keys above",
            3 => "stopped (network, clock, config or calendar) — see the log",
            4 => "rate limit reached (HTTP 429) — try later",
            5 => "another archive process holds the lock (the background task) — nothing started",
            6 => "refused in sandbox mode",
            7 => "internal error — logged in options_archive.log",
            null => "no exit code",
            _ => "exit code " + code.Value.ToString(CultureInfo.InvariantCulture),
        };
    }

    /// <summary>
    /// "Register background task…" — ONE confirmation step (decision 2026-10-23: no typed word): the backend's summary, the exact
    /// action line (program + arguments + working directory) and the trigger ("starts now and at every logon"). The button builds
    /// the fixed task XML, runs schtasks /Create + /Run as the current user, confirms with /Query and posts the audit row.
    /// </summary>
    internal sealed class OptionsArchiveRegisterDialog : FundPopupWindow
    {
        private readonly OaPathsChecked _paths;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private bool _closed;

        /// <summary>The detail of a successful registration (null when not confirmed).</summary>
        public string? Detail { get; private set; }

        public OptionsArchiveRegisterDialog(Window? owner, OaRegistration reg, OaPathsChecked paths)
            : base(owner, "Register the options archive task", 680, 660, resizable: true)
        {
            _paths = paths;
            Closed += (_, _) => _closed = true;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Register \"" + OaTask.TaskName + "\" — starts now and at every logon", 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text("A Windows scheduled task of YOUR user account runs the options archive as its own process (independent of UFOS.ai). "
                                    + "It starts right after you confirm and again at every logon. No admin rights, no stored password. Nothing is registered "
                                    + "before you press the button.", 12, FundPalette.Dim);
            intro.Margin = new Thickness(0, 8, 0, 8);
            intro.LineHeight = 18;
            stack.Children.Add(intro);
            var s = reg.Summary;
            stack.Children.Add(MonUi.Pairs(new[]
            {
                ("What runs", s?.What ?? "not reported by the backend"),
                ("When", s?.When ?? "not reported by the backend"),
                ("Account", s?.Account ?? "not reported by the backend"),
                ("Keys", s?.Keys ?? "not reported by the backend"),
                ("Data", s?.Data ?? "not reported by the backend"),
                ("How to stop", s?.Stop ?? "not reported by the backend"),
            }));
            stack.Children.Add(TradingUi.Head("Exactly what is registered"));
            stack.Children.Add(MonUi.Pairs(new[]
            {
                ("Task", OaTask.TaskName + "  (root folder of the Task Scheduler library)"),
                ("Program", paths.Pythonw),
                ("Arguments", "\"" + paths.Tool + "\" service"),
                ("Working directory", paths.Workdir),
                ("Trigger", "now (started right after registering) and at every logon of " + OaTask.CurrentUser),
                ("Runs as", OaTask.CurrentUser + " · interactive · least privilege · no stored password"),
            }));
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            var okText = "Register and start now (and at every logon)";
            _ok = TradingUi.Primary(okText);
            AutomationProperties.SetName(_ok, "Register the options archive task — it starts now and at every logon");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_closed && !TradingUi.IsBusy(_ok)) DialogResult = false; };
            _ok.Click += async (_, _) => await RegisterAsync();
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
        }

        private async Task RegisterAsync()
        {
            if (TradingUi.IsBusy(_ok)) return;
            TradingUi.SetBusy(_ok, true, "Registering…");
            TradingUi.ShowError(_error, null);
            var (ok, detail) = await OaTask.RegisterAsync(_paths);
            var (state, qErr) = ok ? await OaTask.QueryAsync(_paths) : (null, null);
            var confirmed = ok && state?.Registered == true;
            await PlatformClient.PostOptionsArchiveAuditAsync(confirmed ? "registered" : "register_failed",
                confirmed ? detail : ok ? "created, but /Query did not confirm it: " + (qErr ?? "not registered") : detail);
            if (_closed) return;
            TradingUi.SetBusy(_ok, false);
            if (confirmed)
            {
                Detail = detail;
                DialogResult = true;
                return;
            }
            TradingUi.ShowError(_error, ok ? "The task was created, but schtasks /Query could not confirm it: " + (qErr ?? "not registered") + "."
                                           : "Not registered: " + detail);
        }
    }

    /// <summary>
    /// "Gap report…" — days × slots of the archive (GET /v2/options-archive/gaps, ≤ 31 days): one chip per slot (status, tooltip
    /// with kind, run and gap count) and the day's gap rows (≤ 200, verbatim). Read-only; works without the archive DB.
    /// </summary>
    internal sealed class OptionsArchiveGapDialog : FundPopupWindow
    {
        private readonly TextBox _from;
        private readonly TextBox _to;
        private readonly Button _load;
        private readonly StackPanel _body;
        private bool _closed;

        public OptionsArchiveGapDialog(Window? owner)
            : base(owner, "Options archive — gap report", 900, 700, resizable: true)
        {
            Closed += (_, _) => _closed = true;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Options archive — gap report (SPY/QQQ)", 14, FundPalette.TextBright, bold: true));
            stack.Children.Add(MonUi.Line("Per New York session: every expected slot and what was collected. A gap is a recorded fact (stale quote, missing page or "
                                          + "field, late or missed slot) — nothing is filled in. Up to 31 days at a time.", 11, FundPalette.Dim));
            var row = new WrapPanel { Margin = new Thickness(0, 8, 0, 8) };
            var todayNy = TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, NewYork()).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            row.Children.Add(TradingUi.Label("From (NY date)"));
            _from = FundUi.Input(DateTime.ParseExact(todayNy, "yyyy-MM-dd", CultureInfo.InvariantCulture).AddDays(-6).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));
            _from.Width = 110;
            _from.Margin = new Thickness(6, 0, 14, 0);
            AutomationProperties.SetName(_from, "From date, New York, yyyy-MM-dd");
            row.Children.Add(_from);
            row.Children.Add(TradingUi.Label("To"));
            _to = FundUi.Input(todayNy);
            _to.Width = 110;
            _to.Margin = new Thickness(6, 0, 14, 0);
            AutomationProperties.SetName(_to, "To date, New York, yyyy-MM-dd");
            row.Children.Add(_to);
            _load = TradingUi.Primary("Show");
            AutomationProperties.SetName(_load, "Show the gap report for these dates");
            _load.Click += async (_, _) => await LoadAsync();
            row.Children.Add(_load);
            stack.Children.Add(row);
            _body = new StackPanel();
            stack.Children.Add(_body);
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += async (_, _) => await LoadAsync();
        }

        private static TimeZoneInfo NewYork()
        {
            try { return TimeZoneInfo.FindSystemTimeZoneById("America/New_York"); }
            catch (Exception) { return TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time"); }
        }

        private static bool IsDate(string s) => DateTime.TryParseExact(s, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out _);

        private async Task LoadAsync()
        {
            if (TradingUi.IsBusy(_load)) return;
            var from = _from.Text.Trim();
            var to = _to.Text.Trim();
            _body.Children.Clear();
            if (!IsDate(from) || !IsDate(to)) { _body.Children.Add(MonUi.ErrorBlock("Enter both dates as yyyy-MM-dd.")); return; }
            var span = (DateTime.ParseExact(to, "yyyy-MM-dd", CultureInfo.InvariantCulture) - DateTime.ParseExact(from, "yyyy-MM-dd", CultureInfo.InvariantCulture)).TotalDays;
            if (span < 0 || span > 30) { _body.Children.Add(MonUi.ErrorBlock("The range must be 1 to 31 days, From before To.")); return; }
            TradingUi.SetBusy(_load, true, "Loading…");
            var res = await PlatformClient.GetOptionsArchiveGapsAsync(from, to);
            if (_closed) return;
            TradingUi.SetBusy(_load, false);
            _load.IsEnabled = true;
            if (!res.Ok || res.Value == null)
            {
                _body.Children.Add(MonUi.ErrorBlock("Gap report not available: " + (res.Ok ? "unexpected response from the backend." : PlatformClient.DescribeFailure(res))));
                return;
            }
            var days = res.Value.Days ?? new List<OaGapDay>();
            if (days.Count == 0) { _body.Children.Add(MonUi.Line("No days in this range.", 11.5, FundPalette.Dim)); return; }
            foreach (var d in days.OrderByDescending(x => x.Date, StringComparer.Ordinal)) _body.Children.Add(DayBlock(d));
        }

        private static UIElement DayBlock(OaGapDay d)
        {
            var stack = new StackPanel();
            var slots = d.Slots ?? new List<OaSlot>();
            var head = (d.Date ?? "?") + (d.TradingDay == false ? " · no trading day" : d.EarlyClose == true ? " · early close" : "")
                       + (slots.Count > 0 ? " · " + slots.Count(s => s.Status == "ok").ToString(CultureInfo.InvariantCulture) + " of " + slots.Count.ToString(CultureInfo.InvariantCulture) + " slots OK" : "");
            stack.Children.Add(FundUi.Text(head, 12, FundPalette.TextBright, bold: true));
            if (slots.Count > 0)
            {
                var grid = new WrapPanel { Margin = new Thickness(0, 4, 0, 4) };
                foreach (var s in slots)
                {
                    var text = (s.SlotNy ?? "?") + " " + OaText.SlotStatusLabel(s.Status) + (s.Gaps is int g && g > 0 ? " · " + g.ToString(CultureInfo.InvariantCulture) : "");
                    var chip = MonUi.Chip(text, OaText.SlotBrush(s.Status));
                    chip.Margin = new Thickness(0, 0, 4, 4);
                    chip.ToolTip = (s.Kind ?? "slot") + " · " + OaText.SlotStatusLabel(s.Status) + (s.RunId is long r ? " · run " + r.ToString(CultureInfo.InvariantCulture) : " · no run")
                                   + (s.Gaps is int gg ? " · " + gg.ToString(CultureInfo.InvariantCulture) + " gap(s)" : "");
                    AutomationProperties.SetName(chip, "Slot " + (s.SlotNy ?? "?") + ", " + (s.Kind ?? "slot") + ", " + OaText.SlotStatusLabel(s.Status));
                    grid.Children.Add(chip);
                }
                stack.Children.Add(grid);
            }
            var rows = d.GapRows ?? new List<OaGapRow>();
            if (rows.Count > 0)
            {
                var exp = new Expander
                {
                    Header = FundUi.Text("Gaps (" + rows.Count.ToString(CultureInfo.InvariantCulture) + (d.GapRowsTruncated == true ? ", list truncated at 200" : "") + ")", 11, FundPalette.Text, wrap: false),
                    Foreground = FundPalette.Text,
                };
                AutomationProperties.SetName(exp, "Gaps of " + (d.Date ?? "?"));
                exp.Content = MonUi.Table(new[] { "Slot", "Kind", "Underlying", "Symbol", "What", "Reason" },
                    rows.Select(r => (IReadOnlyList<string>)new[] { r.SlotNy ?? "—", r.Kind ?? "—", r.Underlying ?? "—", r.Symbol ?? "—", r.What ?? "", r.Reason ?? "" }),
                    "Gaps of " + (d.Date ?? "?"), new double[] { 60, 100, 80, 160, 0, 0 });
                stack.Children.Add(exp);
            }
            return FundUi.Panel(stack, new Thickness(12, 10, 12, 10), new Thickness(0, 0, 0, 8));
        }
    }
}

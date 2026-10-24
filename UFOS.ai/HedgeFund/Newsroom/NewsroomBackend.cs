using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Starts, checks and stops the HedgeFund Python backend (HedgeFund/backend,
    /// run_hedgefund_backend.ps1), which serves the Financial Newsroom's data on
    /// 127.0.0.1:8867. Until 2026-09-24 the HedgeFund window never started its
    /// backend at all (the dashboard is illustrative); the newsroom sections moved
    /// here from Global Monitoring need it, so this uses the same proven pattern as
    /// Global Monitoring's MainWindow.xaml.cs:
    /// - the script is found by walking up from the exe folder (works hosted in the
    ///   main UFOS.ai app AND as standalone HedgeFundApp.exe);
    /// - an already-running backend is reused only if its code is current
    ///   (GET /version vs. the .py files on disk), otherwise replaced (POST /shutdown);
    /// - the started process tree is bound to this app via a Job Object, so a crash
    ///   or a Visual Studio debugger stop never leaves an orphaned python.exe;
    /// - all output goes to the app-wide "Backend Log" window (module "HedgeFund")
    ///   and to backend/data/logs/process.log.
    ///
    /// Lifetime (backend/fund/SPEC.md §14, 2026-09-26): the backend also runs the
    /// paper-trading engine (backend/fund), so closing the Hedge Fund window no longer
    /// stops it — it lives as long as the UFOS.ai app process (Job Object, lifetime
    /// "app", the default). Opt-in lifetime "detached" (%LOCALAPPDATA%\UFOS.ai\
    /// hedgefund-backend.json, Fund/FundSettings.cs, checkbox in the Hedge Fund window;
    /// read at every start, so a change applies from the next backend start): the
    /// process is NOT bound to the Job Object and its output is NOT redirected (a pipe
    /// that closes with the app would break the process); the run script writes it to
    /// backend/data/logs/process.log (-LogFile) in a hidden window. The next app start
    /// reuses that backend through the /version check below. Stop() is kept for callers
    /// that really want to end a backend this app started; nothing calls it on window close.
    /// ShutdownAsync() ends ANY running backend (also a detached one from an earlier app
    /// session) via the token-authenticated POST /shutdown ("Stop engine…" in the window).
    ///
    /// Known limitation of "detached" (documented 2026-09-26, not fixed): the child is started
    /// with Process.Start, i.e. WITHOUT CREATE_BREAKAWAY_FROM_JOB. Our own Job Object is not
    /// used in detached mode, but if UFOS.ai itself was started inside a job with
    /// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (some launchers, IDE debuggers such as Visual Studio,
    /// terminals/CI runners, sandboxing tools), the powershell/python children inherit that job
    /// and are killed together with UFOS.ai — the engine then does NOT survive the app. Fixing it
    /// would need CreateProcess with CREATE_BREAKAWAY_FROM_JOB (only allowed when that job sets
    /// JOB_OBJECT_LIMIT_BREAKAWAY_OK) or a launcher outside the job (e.g. a scheduled task).
    /// Started normally from Explorer/Start menu, UFOS.ai is not in such a job and detached works.
    /// </summary>
    internal static class NewsroomBackend
    {
        public const string BaseUrl = "http://127.0.0.1:8867";

        private static string? _backendDir;

        /// <summary>The backend folder (…\HedgeFund\backend), where the backend publishes its API token.</summary>
        public static string? BackendDir
        {
            get
            {
                if (_backendDir != null) return _backendDir;
                var script = FindBackendScript(out _);
                _backendDir = script == null ? null : Path.GetDirectoryName(script);
                return _backendDir;
            }
        }

        /// <summary>
        /// HttpClient for the newsroom API, carrying the backend's current session token
        /// (X-UFOS-Token, see Shared/BackendAuth.cs and backend/api_auth.py). Every endpoint
        /// except /health and /version answers 401 without it.
        /// </summary>
        public static HttpClient CreateClient(TimeSpan timeout) => BackendAuth.CreateClient(BackendDir, timeout);
        private const string LogModule = "HedgeFund";

        private static Process? _process;
        private static bool _weStarted;
        private static RotatingLogFile? _processLog; // size-capped: 5 MB, 3 old files (Shared/RotatingLogFile.cs, S10-A2 L6)
        private static Task? _startTask;
        // Incremented by Stop(): a start that was still waiting for the Python environment
        // must not launch the backend after the window was closed.
        private static int _stopGeneration;

        // True while run_hedgefund_backend.ps1 installs Python packages or waits for
        // another install/update to finish (UFOS_ENV markers) - that time doesn't count
        // towards the "did not answer within 10 minutes" limit.
        private static volatile bool _envBusy;
        private const string EnvMarkerPrefix = "UFOS_ENV:";

        /// <summary>Short human-readable state for the UI ("starting…", "online", ...).</summary>
        public static string Status { get; private set; } = "not started";

        /// <summary>Raised (on any thread) whenever <see cref="Status"/> changes.</summary>
        public static event Action? StatusChanged;

        private static void SetStatus(string status)
        {
            Status = status;
            try { StatusChanged?.Invoke(); } catch { }
        }

        /// <summary>Idempotent: starts the backend unless it's already running/starting.</summary>
        public static Task EnsureStartedAsync()
        {
            if (_startTask != null && !_startTask.IsCompleted) return _startTask;
            if (_process != null && !_process.HasExited) return Task.CompletedTask;
            _startTask = StartAsync();
            return _startTask;
        }

        private static async Task StartAsync()
        {
            try
            {
                SetStatus("checking…");
                var script = FindBackendScript(out var tried);

                if (await IsRunningAsync())
                {
                    // KeepRunningBackendAsync sets the status itself when it keeps the process.
                    if (await KeepRunningBackendAsync(script)) return;
                }

                if (script == null)
                {
                    AppLog.Error(LogModule, "run_hedgefund_backend.ps1 not found — the Financial Newsroom backend can't be started. " +
                                            "Tried: " + string.Join(" | ", tried));
                    SetStatus("offline — backend script not found (see Backend Log)");
                    return;
                }

                // Python environments are prepared once at app start (Shared/PythonEnvironment.cs);
                // wait until this backend's .venv is ready (standalone HedgeFundApp.exe: prepare
                // it here). The script then runs with -SkipInstall and never touches pip.
                var generation = _stopGeneration;
                var envOk = await PythonEnvironment.WaitForModuleAsync(Path.GetDirectoryName(script)!, LogModule,
                    PythonEnvironment.ProgressOf(text =>
                    {
                        if (generation == _stopGeneration) SetStatus("starting… Python environment: " + text);
                    }));
                if (generation != _stopGeneration) return; // Stop() ran meanwhile
                if (!envOk)
                {
                    AppLog.Error(LogModule, "Python environment setup failed — the HedgeFund backend was not started.");
                    SetStatus("offline — Python environment setup failed (see Backend Log)");
                    return;
                }

                // Lifetime (SPEC §14): "app" (default) = redirected output + Job Object;
                // "detached" = no redirect (the output pipe would break when the app exits),
                // no Job Object, output appended to data/logs/process.log by the run script.
                var detached = FundSettings.IsDetached;
                var backendDir = Path.GetDirectoryName(script)!;
                ProcessStartInfo psi;
                if (detached)
                {
                    var logPath = Path.Combine(backendDir, "data", "logs", "process.log");
                    try { Directory.CreateDirectory(Path.GetDirectoryName(logPath)!); } catch { }
                    // The detached run script appends to this file itself: cap it before each start (S10-A2 L6).
                    var rotateProblem = RotatingLogFile.RotateIfLarge(logPath);
                    if (rotateProblem != null) AppLog.Warn(LogModule, "Could not rotate process.log before the start: " + rotateProblem);
                    psi = new ProcessStartInfo
                    {
                        // Absolute Windows PowerShell path + why Bypass stays for this .ps1: Shared/WindowsPowerShell.cs (S10-A2 L4).
                        FileName = WindowsPowerShell.ExePath,
                        Arguments = WindowsPowerShell.ScriptArguments(script, $"-SkipInstall -LogFile \"{logPath}\"", "-WindowStyle Hidden"),
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        WorkingDirectory = backendDir,
                    };
                }
                else
                {
                    OpenProcessLog(script);
                    psi = new ProcessStartInfo
                    {
                        FileName = WindowsPowerShell.ExePath,
                        Arguments = WindowsPowerShell.ScriptArguments(script, "-SkipInstall"),
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        RedirectStandardOutput = true,
                        RedirectStandardError = true,
                        WorkingDirectory = backendDir,
                    };
                }
                var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
                if (!detached)
                {
                    process.OutputDataReceived += (s, e) =>
                    {
                        Forward(e.Data, isError: false);
                        HandleEnvMarker(process, e.Data);
                    };
                    process.ErrorDataReceived += (s, e) => Forward(e.Data, isError: true);
                }
                process.Exited += (s, e) =>
                {
                    // Ignore a process that Stop() already let go of (or a replaced one).
                    if (!ReferenceEquals(_process, process)) return;
                    _envBusy = false;
                    try { _processLog?.WriteLine($"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} process exited ---"); } catch { }
                    try { AppLog.Warn(LogModule, "Backend process exited."); } catch { }
                    SetStatus("offline — backend process exited (see Backend Log)");
                };

                // The script reports package installs via UFOS_ENV markers (HandleEnvMarker);
                // a normal start doesn't install anything any more.
                _envBusy = false;
                SetStatus("starting…");
                AppLog.Info(LogModule, "Starting backend via " + script +
                                       (detached ? " (detached: not bound to UFOS.ai, output in data/logs/process.log)" : ""));
                _process = process; // before Start(): Exited may fire right away
                try
                {
                    _weStarted = process.Start();
                }
                catch
                {
                    _process = null;
                    throw;
                }
                if (!detached)
                {
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    if (_weStarted) BackendJob.Attach(process);
                }

                // Report "online" once the HTTP API answers. Time spent installing Python
                // packages (or waiting for an update to finish) doesn't count towards the limit.
                // Stop() may run meanwhile (window closed) — then just end quietly.
                for (var i = 0; i < 600; )
                {
                    if (!ReferenceEquals(_process, process)) return;
                    if (process.HasExited)
                    {
                        SetStatus("offline — backend process exited (see Backend Log)");
                        return;
                    }
                    if (await IsRunningAsync())
                    {
                        if (ReferenceEquals(_process, process)) SetStatus(detached ? "online · detached (keeps running after UFOS.ai closes)" : "online");
                        return;
                    }
                    if (!_envBusy) i++;
                    await Task.Delay(1000);
                }
                if (ReferenceEquals(_process, process))
                {
                    AppLog.Warn(LogModule, "Backend did not answer on " + BaseUrl + "/health within 10 minutes.");
                    SetStatus("offline — backend did not answer within 10 minutes (see Backend Log)");
                }
            }
            catch (Exception ex)
            {
                AppLog.Error(LogModule, "Failed to start the HedgeFund backend.", ex);
                SetStatus("offline — failed to start (see Backend Log)");
            }
        }

        /// <summary>Stops the backend if this app started it (also covered by the Job Object).</summary>
        public static void Stop()
        {
            // Let go of the process first, so its Exited handler and a still-running
            // StartAsync loop see it's no longer ours and don't overwrite the status.
            var process = _process;
            var weStarted = _weStarted;
            _process = null;
            _weStarted = false;
            _startTask = null;
            _stopGeneration++;
            _envBusy = false;
            try
            {
                if (process != null && weStarted && !process.HasExited)
                {
                    AppLog.Info(LogModule, "Stopping backend process tree, PID " + process.Id);
                    process.Kill(true);
                }
                process?.Dispose();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Failed to stop the backend process.", ex);
            }
            finally
            {
                try
                {
                    _processLog?.WriteLine($"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} stopped by app ---");
                    _processLog?.Dispose();
                }
                catch { }
                _processLog = null;
                SetStatus("stopped");
            }
        }

        /// <summary>
        /// Ends the running backend — whoever started it, also a detached one — through
        /// POST /shutdown (loopback only, needs the session token → CreateClient). Waits up to
        /// 10 s for /health to stop answering. Never throws; returns a short message for the UI.
        /// </summary>
        public static async Task<(bool Ok, string Message)> ShutdownAsync()
        {
            try
            {
                if (!await IsRunningAsync())
                {
                    Stop();
                    SetStatus("stopped by user — reopen the Hedge Fund window to start it again");
                    return (true, "The backend was not running.");
                }
                try
                {
                    using var client = CreateClient(TimeSpan.FromSeconds(5));
                    using var resp = await client.PostAsync(BaseUrl + "/shutdown", new StringContent(""));
                    if (!resp.IsSuccessStatusCode)
                    {
                        var code = (int)resp.StatusCode;
                        AppLog.Warn(LogModule, "Backend refused POST /shutdown (HTTP " + code + ").");
                        return (false, code == 401 || code == 403
                            ? "The backend refused the shutdown (HTTP " + code + ") — its session token did not match. End its python.exe in Task Manager."
                            : "The backend refused the shutdown (HTTP " + code + ").");
                    }
                }
                catch (HttpRequestException) { /* the process may drop the connection while exiting */ }
                catch (TaskCanceledException) { /* same */ }

                for (var i = 0; i < 40; i++)
                {
                    await Task.Delay(250);
                    if (!await IsRunningAsync())
                    {
                        AppLog.Info(LogModule, "Backend shut down on user request (Stop engine).");
                        // Let go of (and clean up) a process this app started; the Exited handler
                        // then no longer reports it as a crash.
                        Stop();
                        SetStatus("stopped by user — reopen the Hedge Fund window to start it again");
                        return (true, "The Hedge Fund backend has been shut down. It starts again the next time the Hedge Fund window is opened.");
                    }
                }
                AppLog.Warn(LogModule, "Backend still answers 10 s after POST /shutdown.");
                return (false, "The backend is still running 10 s after the shutdown request. End its python.exe in Task Manager.");
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Backend shutdown failed.", ex);
                return (false, "The shutdown request failed (" + ex.GetType().Name + ").");
            }
        }

        public static async Task<bool> IsRunningAsync()
        {
            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromMilliseconds(600) };
                var resp = await client.GetAsync(BaseUrl + "/health");
                return resp.IsSuccessStatusCode;
            }
            catch
            {
                return false;
            }
        }

        private static void Forward(string? line, bool isError)
        {
            if (string.IsNullOrEmpty(line)) return;
            try { _processLog?.WriteLine($"[{DateTime.Now:HH:mm:ss}]{(isError ? "[err]" : "")} {line}"); } catch { }
            // Python's logging writes to stderr by default, so "err" isn't necessarily an error.
            try { if (isError) AppLog.Warn(LogModule, line); else AppLog.Info(LogModule, line); } catch { }
        }

        /// <summary>
        /// run_hedgefund_backend.ps1 prints "UFOS_ENV: &lt;state&gt;" lines while it prepares the
        /// Python environment (protocol in the script header): waiting / installing &lt;reason&gt; /
        /// upgrading / changed &lt;n&gt; / failed &lt;reason&gt; / ready. Turn them into status text.
        /// </summary>
        private static void HandleEnvMarker(Process process, string? line)
        {
            if (line == null || !line.StartsWith(EnvMarkerPrefix, StringComparison.Ordinal)) return;
            if (!ReferenceEquals(_process, process)) return;
            var marker = line.Substring(EnvMarkerPrefix.Length).Trim();
            if (marker.StartsWith("waiting", StringComparison.OrdinalIgnoreCase))
            {
                _envBusy = true;
                SetStatus("starting… waiting for the Python package update to finish");
            }
            else if (marker.StartsWith("installing", StringComparison.OrdinalIgnoreCase) ||
                     marker.StartsWith("upgrading", StringComparison.OrdinalIgnoreCase))
            {
                _envBusy = true;
                SetStatus("starting… installing Python packages (first start or requirements changed) — can take a few minutes");
            }
            else if (marker.StartsWith("failed", StringComparison.OrdinalIgnoreCase))
            {
                _envBusy = false;
                SetStatus("starting… Python package install failed — trying with the installed packages (see Backend Log)");
            }
            else if (marker.StartsWith("ready", StringComparison.OrdinalIgnoreCase))
            {
                _envBusy = false;
                SetStatus("starting…");
            }
        }

        private static void OpenProcessLog(string script)
        {
            try
            {
                var logDir = Path.Combine(Path.GetDirectoryName(script)!, "data", "logs");
                Directory.CreateDirectory(logDir);
                _processLog = new RotatingLogFile(Path.Combine(logDir, "process.log"));
                _processLog.WriteLine($"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} starting backend via run_hedgefund_backend.ps1 ---");
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Could not open process.log.", ex);
                _processLog = null;
            }
        }

        /// <summary>
        /// Walks up from the exe folder: accepts &lt;dir&gt;\HedgeFund\backend\run_hedgefund_backend.ps1
        /// (hosted in the main UFOS.ai app) or &lt;dir&gt;\backend\run_hedgefund_backend.ps1 when
        /// &lt;dir&gt; is the HedgeFund project folder (standalone HedgeFundApp.exe).
        /// </summary>
        private static string? FindBackendScript(out List<string> tried)
        {
            tried = new List<string>();
            var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
            for (var depth = 0; dir != null && depth < 8; depth++, dir = dir.Parent)
            {
                var viaRoot = Path.Combine(dir.FullName, "HedgeFund", "backend", "run_hedgefund_backend.ps1");
                tried.Add(viaRoot);
                if (File.Exists(viaRoot)) return viaRoot;

                if (File.Exists(Path.Combine(dir.FullName, "MultiAgentHedgeFund.csproj")))
                {
                    var standalone = Path.Combine(dir.FullName, "backend", "run_hedgefund_backend.ps1");
                    tried.Add(standalone);
                    if (File.Exists(standalone)) return standalone;
                }
            }
            return null;
        }

        /// <summary>True = keep the running backend; false = it was shut down, start a fresh one.</summary>
        private static async Task<bool> KeepRunningBackendAsync(string? script)
        {
            if (script == null) { SetStatus("online"); return true; }

            double? runningMtime = null;
            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
                using var doc = JsonDocument.Parse(await client.GetStringAsync(BaseUrl + "/version"));
                runningMtime = doc.RootElement.GetProperty("code_mtime").GetDouble();
            }
            catch { /* no /version: predates the Newsroom — certainly outdated */ }

            if (runningMtime != null && runningMtime.Value + 1 >= NewestPyMtime(Path.GetDirectoryName(script)!))
            {
                AppLog.Info(LogModule, "Backend already running on 127.0.0.1:8867 with current code — using it.");
                SetStatus("online");
                return true;
            }

            if (runningMtime == null)
            {
                AppLog.Warn(LogModule, "An outdated HedgeFund backend (without /version) is running on port 8867 and can't be " +
                                       "replaced automatically. End its python.exe in Task Manager and reopen the Hedge Fund window.");
                SetStatus("outdated backend running — end python.exe in Task Manager, then reopen");
                return true;
            }

            AppLog.Warn(LogModule, "Running backend is older than the code on disk — replacing it.");
            try
            {
                using var client = CreateClient(TimeSpan.FromSeconds(2));
                await client.PostAsync(BaseUrl + "/shutdown", new StringContent(""));
            }
            catch { /* the process may drop the connection while exiting */ }

            for (var i = 0; i < 20; i++)
            {
                await Task.Delay(250);
                if (!await IsRunningAsync()) return false;
            }
            AppLog.Error(LogModule, "Old backend did not shut down within 5s — keeping it.");
            SetStatus("online (outdated backend could not be replaced)");
            return true;
        }

        private static double NewestPyMtime(string backendDir)
        {
            double newest = 0;
            try
            {
                // Same exclusions as _newest_py_mtime() in backend/main.py.
                var skip = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { ".venv", "__pycache__", "data" };
                foreach (var file in Directory.EnumerateFiles(backendDir, "*.py", SearchOption.AllDirectories))
                {
                    var relDirs = Path.GetRelativePath(backendDir, file).Split(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                    if (relDirs.Take(relDirs.Length - 1).Any(skip.Contains)) continue;
                    var t = new DateTimeOffset(File.GetLastWriteTimeUtc(file)).ToUnixTimeMilliseconds() / 1000.0;
                    if (t > newest) newest = t;
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Could not scan backend files for their timestamps.", ex);
            }
            return newest;
        }

        /// <summary>
        /// Windows Job Object with KILL_ON_JOB_CLOSE: when this app process ends for any
        /// reason, Windows terminates the backend tree (powershell.exe + python.exe) too.
        /// </summary>
        private static class BackendJob
        {
            private static IntPtr _job = IntPtr.Zero;

            public static void Attach(Process process)
            {
                try
                {
                    if (_job == IntPtr.Zero)
                    {
                        _job = CreateJobObject(IntPtr.Zero, null);
                        if (_job == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());

                        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                        var length = Marshal.SizeOf<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>();
                        var ptr = Marshal.AllocHGlobal(length);
                        try
                        {
                            Marshal.StructureToPtr(info, ptr, false);
                            if (!SetInformationJobObject(_job, JobObjectExtendedLimitInformation, ptr, (uint)length))
                                throw new Win32Exception(Marshal.GetLastWin32Error());
                        }
                        finally { Marshal.FreeHGlobal(ptr); }
                    }
                    if (!AssignProcessToJobObject(_job, process.Handle))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    AppLog.Info(LogModule, "Backend process bound to the app's lifetime (Job Object).");
                }
                catch (Exception ex)
                {
                    AppLog.Warn(LogModule, "Could not bind backend to the app's lifetime (Job Object); a debugger stop may leave it running.", ex);
                }
            }

            private const int JobObjectExtendedLimitInformation = 9;
            private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

            [StructLayout(LayoutKind.Sequential)]
            private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
            {
                public long PerProcessUserTimeLimit;
                public long PerJobUserTimeLimit;
                public uint LimitFlags;
                public UIntPtr MinimumWorkingSetSize;
                public UIntPtr MaximumWorkingSetSize;
                public uint ActiveProcessLimit;
                public UIntPtr Affinity;
                public uint PriorityClass;
                public uint SchedulingClass;
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct IO_COUNTERS
            {
                public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
                public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            {
                public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
                public IO_COUNTERS IoInfo;
                public UIntPtr ProcessMemoryLimit;
                public UIntPtr JobMemoryLimit;
                public UIntPtr PeakProcessMemoryUsed;
                public UIntPtr PeakJobMemoryUsed;
            }

            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string? lpName);

            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool SetInformationJobObject(IntPtr hJob, int infoType, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);
        }
    }
}

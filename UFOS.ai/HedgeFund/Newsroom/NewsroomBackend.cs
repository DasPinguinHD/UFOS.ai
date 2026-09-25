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
using UFOS.ai.Logging;

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
    /// </summary>
    internal static class NewsroomBackend
    {
        public const string BaseUrl = "http://127.0.0.1:8867";
        private const string LogModule = "HedgeFund";

        private static Process? _process;
        private static bool _weStarted;
        private static StreamWriter? _processLog;
        private static Task? _startTask;

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

                OpenProcessLog(script);
                var psi = new ProcessStartInfo
                {
                    FileName = "powershell.exe",
                    Arguments = $"-ExecutionPolicy Bypass -File \"{script}\"",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    WorkingDirectory = Path.GetDirectoryName(script),
                };
                var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
                process.OutputDataReceived += (s, e) => Forward(e.Data, isError: false);
                process.ErrorDataReceived += (s, e) => Forward(e.Data, isError: true);
                process.Exited += (s, e) =>
                {
                    // Ignore a process that Stop() already let go of (or a replaced one).
                    if (!ReferenceEquals(_process, process)) return;
                    try { _processLog?.WriteLine($"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} process exited ---"); } catch { }
                    try { AppLog.Warn(LogModule, "Backend process exited."); } catch { }
                    SetStatus("offline — backend process exited (see Backend Log)");
                };

                SetStatus("starting… (first start installs Python packages — can take a few minutes)");
                AppLog.Info(LogModule, "Starting backend via " + script);
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
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                if (_weStarted) BackendJob.Attach(process);

                // Report "online" once the HTTP API answers (pip install can take a while).
                // Stop() may run meanwhile (window closed) — then just end quietly.
                for (var i = 0; i < 600; i++)
                {
                    if (!ReferenceEquals(_process, process)) return;
                    if (process.HasExited)
                    {
                        SetStatus("offline — backend process exited (see Backend Log)");
                        return;
                    }
                    if (await IsRunningAsync())
                    {
                        if (ReferenceEquals(_process, process)) SetStatus("online");
                        return;
                    }
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

        private static void OpenProcessLog(string script)
        {
            try
            {
                var logDir = Path.Combine(Path.GetDirectoryName(script)!, "data", "logs");
                Directory.CreateDirectory(logDir);
                _processLog = new StreamWriter(Path.Combine(logDir, "process.log"), append: true) { AutoFlush = true };
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
                using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
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

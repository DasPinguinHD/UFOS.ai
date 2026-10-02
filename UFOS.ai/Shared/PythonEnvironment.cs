using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UFOS.ai.Logging;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Prepares the Python environments (backend\.venv) of the module backends
    /// (HedgeFund, Global Monitoring, LiveStreamAgent). Added 2026-09-25.
    ///
    /// - The root UFOS.ai app calls <see cref="StartAppPreparation"/> once at start
    ///   (MainWindow.Window_Loaded): in the background, one module after the other,
    ///   it creates a missing .venv and runs
    ///   "pip install -r requirements.txt --upgrade" ONLY when it is required
    ///   (no/broken .venv, requirements.txt changed since the last successful
    ///   install, or the last install did not complete). MainWindow shows the
    ///   progress (<see cref="StatusText"/>, <see cref="IsBusy"/>, <see cref="StateChanged"/>).
    /// - Every module launcher awaits <see cref="WaitForModuleAsync"/> before it
    ///   starts its run_*.ps1 (with -SkipInstall). That waits while anybody else
    ///   prepares the same venv, then checks the stamp; if the environment is still
    ///   not ready (e.g. a standalone module exe without the root app, or the app-start
    ///   preparation failed), it prepares it itself. So a backend never starts while
    ///   pip changes its venv, and pip never runs twice at once on the same venv.
    ///
    /// Same on-disk protocol as the run_*.ps1 scripts and Services/PythonEnvManager.cs
    /// (all three must stay compatible):
    /// - stamp .venv\ufos-requirements.sha256 = SHA-256 of requirements.txt (upper-case
    ///   hex like PowerShell's Get-FileHash, "none" without requirements.txt), written
    ///   only after a successful install;
    /// - .venv\ufos-install-pending exists while an install runs (a crash leaves it,
    ///   which forces a reinstall);
    /// - backend\.venv.lock is the cross-process lock: created with CreateNew and held
    ///   open with DeleteOnClose (no FileShare.Delete), so nobody else can open it while
    ///   it's held and Windows deletes it even if the holder is killed. A readable
    ///   leftover is stale when its PID is gone or it is older than 30 minutes.
    ///   (A file lock instead of a named Mutex: the PowerShell scripts, including the
    ///   Settings "Update Python packages" run, already use it, and it isn't thread-affine.)
    ///
    /// This file is compiled into the root app AND linked into every module project
    /// (internal on purpose, one copy per assembly). Static state is therefore per
    /// assembly - the launchers in the module assemblies coordinate with the root app's
    /// copy only through the lock file and the stamp, never through this class's fields.
    /// Output goes to AppLog (module tag = the moduleName passed in) and to
    /// backend\data\logs\env-setup.log. Nothing here throws; failures return false.
    /// </summary>
    internal static class PythonEnvironment
    {
        private const string AppLogModule = "Python env";
        private const string LockFileName = ".venv.lock";
        private const string HashFileName = "ufos-requirements.sha256";
        private const string PendingFileName = "ufos-install-pending";
        private const string BrokenReason = "the .venv is broken - its python.exe does not run";

        private static readonly TimeSpan PipTimeout = TimeSpan.FromMinutes(15);
        private static readonly TimeSpan VenvTimeout = TimeSpan.FromMinutes(5);
        private static readonly TimeSpan ProbeTimeout = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan LockStaleAfter = TimeSpan.FromMinutes(30);
        private static readonly TimeSpan LockWaitMax = TimeSpan.FromMinutes(35);

        private static readonly (string Name, string[] RelativeDir)[] KnownModules =
        {
            ("Hedge Fund", new[] { "HedgeFund", "backend" }),
            ("Global Monitoring", new[] { "Global Monitoring", "backend" }),
            ("LiveStream Agent", new[] { "LiveStreamAgent", "backend" }),
        };

        private static readonly object Sync = new();
        private static readonly object LogFileSync = new();
        private static readonly List<string> FailedModuleNames = new();
        private static TaskCompletionSource<bool>? _allReady;
        private static string _statusText = "";
        private static bool _isBusy;

        /// <summary>Raised (on any thread) when <see cref="StatusText"/>/<see cref="IsBusy"/> changed.</summary>
        public static event Action? StateChanged;

        /// <summary>One-line state of the app-start preparation ("" = nothing to show).</summary>
        public static string StatusText { get { lock (Sync) return _statusText; } }

        /// <summary>True while the app-start preparation runs.</summary>
        public static bool IsBusy { get { lock (Sync) return _isBusy; } }

        /// <summary>Modules whose environment could not be prepared at app start.</summary>
        public static IReadOnlyList<string> FailedModules { get { lock (Sync) return FailedModuleNames.ToArray(); } }

        /// <summary>
        /// Completes when the app-start preparation has finished (successfully or not).
        /// Already completed when <see cref="StartAppPreparation"/> was never called
        /// in this assembly (module launchers use <see cref="WaitForModuleAsync"/>).
        /// </summary>
        public static Task AllReady
        {
            get
            {
                lock (Sync) return (Task?)_allReady?.Task ?? Task.CompletedTask;
            }
        }

        /// <summary>Wraps a callback as a synchronous <see cref="IProgress{T}"/> (no SynchronizationContext post).</summary>
        public static IProgress<string> ProgressOf(Action<string> report) => new SyncProgress(report);

        // ------------------------------------------------------------------ app start

        /// <summary>
        /// Root app, once at start: prepares every module backend found next to the app,
        /// sequentially, in the background. Idempotent; returns <see cref="AllReady"/>.
        /// </summary>
        public static Task StartAppPreparation()
        {
            TaskCompletionSource<bool> tcs;
            lock (Sync)
            {
                if (_allReady != null) return _allReady.Task;
                tcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                _allReady = tcs;
                _isBusy = true;
                _statusText = "Preparing Python environments…";
            }
            RaiseStateChanged();

            _ = Task.Run(async () =>
            {
                var ok = false;
                try
                {
                    ok = await PrepareAllAsync().ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    SafeLog(AppLogModule, "Preparing the Python environments failed.", ex, error: true);
                    SetState("Python environment setup failed — see Backend Log", busy: false);
                }
                finally
                {
                    tcs.TrySetResult(ok);
                }
            });
            return tcs.Task;
        }

        /// <summary>
        /// Walks up from the exe folder (like the launchers' FindBackendScript helpers) and
        /// returns every module backend that has a requirements.txt.
        /// </summary>
        public static IReadOnlyList<(string Name, string BackendDir)> DiscoverModuleBackends()
        {
            var result = new List<(string Name, string BackendDir)>();
            foreach (var (name, relativeDir) in KnownModules)
            {
                try
                {
                    var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
                    for (var depth = 0; dir != null && depth < 8; depth++, dir = dir.Parent)
                    {
                        var backendDir = Path.Combine(new[] { dir.FullName }.Concat(relativeDir).ToArray());
                        if (File.Exists(Path.Combine(backendDir, "requirements.txt")))
                        {
                            result.Add((name, backendDir));
                            break;
                        }
                    }
                }
                catch (Exception ex)
                {
                    SafeLog(AppLogModule, "Could not look for the " + name + " backend folder.", ex, error: false);
                }
            }
            return result;
        }

        private static async Task<bool> PrepareAllAsync()
        {
            var modules = DiscoverModuleBackends();
            if (modules.Count == 0)
            {
                SafeLog(AppLogModule, "No module backends (requirements.txt) found next to the app - nothing to prepare.", null, error: false);
                SetState("", busy: false);
                return true;
            }

            SafeLog(AppLogModule, $"Preparing the Python environments of {modules.Count} module backend(s)…", null, error: false);
            var allOk = true;
            for (var i = 0; i < modules.Count; i++)
            {
                var (name, backendDir) = modules[i];
                var prefix = $"Preparing Python environments… ({name} {i + 1}/{modules.Count})";
                SetState(prefix, busy: true);
                var ok = await EnsureReadyAsync(backendDir, name,
                    ProgressOf(text => SetState(prefix + " · " + text, busy: true)), CancellationToken.None).ConfigureAwait(false);
                if (!ok)
                {
                    allOk = false;
                    lock (Sync) FailedModuleNames.Add(name);
                }
            }

            if (allOk)
            {
                SafeLog(AppLogModule, "Python environments ready.", null, error: false);
                SetState("Python environments ready", busy: false);
            }
            else
            {
                var failed = string.Join(", ", FailedModules);
                SafeLog(AppLogModule, "Python environment setup failed for: " + failed + ". Their backends won't start until it succeeds " +
                                      "(retried when the module starts its backend).", null, error: true);
                SetState("Python environment setup failed (" + failed + ") — see Backend Log", busy: false);
            }
            return allOk;
        }

        // ------------------------------------------------------------------ per module

        /// <summary>
        /// For launchers: call before starting a backend. Waits while another process/assembly
        /// (usually the root app's start-up preparation) prepares this venv, then makes sure it
        /// is ready - preparing it here if the stamp still doesn't match. True = start the backend.
        /// <paramref name="progress"/> gets short lower-case phrases (only when there's something
        /// to wait for), e.g. "waiting for a running install/update to finish…".
        /// </summary>
        public static Task<bool> WaitForModuleAsync(string backendDir, string moduleName,
                                                    IProgress<string>? progress = null, CancellationToken ct = default)
            // Off the caller's (UI) thread: the stamp check hashes a file and probes python.exe.
            => Task.Run(() => EnsureReadyAsync(backendDir, moduleName, progress, ct));

        /// <summary>
        /// Under the venv lock: create the .venv if missing/broken and run
        /// "pip install -r requirements.txt --upgrade" if requirements.txt changed since the
        /// last successful install (stamp), then write the stamp. Never throws.
        /// </summary>
        public static async Task<bool> EnsureReadyAsync(string backendDir, string moduleName,
                                                        IProgress<string>? progress, CancellationToken ct = default)
        {
            FileStream? lockStream = null;
            try
            {
                backendDir = Path.GetFullPath(backendDir);
                if (!Directory.Exists(backendDir))
                {
                    SafeLog(moduleName, "Python environment: backend folder not found: " + backendDir, null, error: true);
                    return false;
                }

                lockStream = await AcquireLockAsync(backendDir, moduleName, progress, ct).ConfigureAwait(false);
                if (lockStream == null)
                {
                    SafeLog(moduleName, "Python environment: could not get " + Path.Combine(backendDir, LockFileName) +
                                        " (another install/update did not finish in time).", null, error: true);
                    return false;
                }
                return await PrepareLockedAsync(backendDir, moduleName, progress, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                SafeLog(moduleName, "Python environment: preparation cancelled.", null, error: false);
                return false;
            }
            catch (Exception ex)
            {
                SafeLog(moduleName, "Python environment: preparation failed.", ex, error: true);
                AppendEnvLog(backendDir, moduleName, "ERROR: " + ex);
                return false;
            }
            finally
            {
                // DeleteOnClose removes the lock file.
                try { lockStream?.Dispose(); } catch { }
            }
        }

        private static async Task<bool> PrepareLockedAsync(string backendDir, string moduleName,
                                                           IProgress<string>? progress, CancellationToken ct)
        {
            var venvPython = VenvPython(backendDir);
            var reason = GetInstallReason(backendDir);
            if (File.Exists(venvPython) && !await VenvRunsAsync(backendDir, moduleName, ct).ConfigureAwait(false))
                reason = BrokenReason;
            if (reason == null) return true;

            SafeLog(moduleName, "Python environment needs an install: " + reason + ".", null, error: false);
            AppendEnvLog(backendDir, moduleName, $"===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} install needed: {reason} =====");

            var recreate = reason == BrokenReason;
            if (recreate || !File.Exists(venvPython))
            {
                progress?.Report(recreate ? "recreating the broken virtual environment (.venv)…" : "creating the virtual environment (.venv)…");
                if (!await CreateVenvAsync(backendDir, moduleName, recreate, ct).ConfigureAwait(false)) return false;
            }

            var venvDir = Path.Combine(backendDir, ".venv");
            var hashFile = Path.Combine(venvDir, HashFileName);
            var pendingFile = Path.Combine(venvDir, PendingFileName);
            var requirements = Path.Combine(backendDir, "requirements.txt");
            // Hash BEFORE installing: an edit during the install then triggers another one next time.
            var hash = RequirementsHash(backendDir);

            if (!File.Exists(requirements))
            {
                File.WriteAllText(hashFile, hash + "\r\n", Encoding.ASCII);
                AppendEnvLog(backendDir, moduleName, "No requirements.txt - nothing to install.");
                return true;
            }

            progress?.Report("installing packages (" + reason + ") — can take a few minutes…");
            File.WriteAllText(pendingFile, "install started\r\n", Encoding.ASCII);
            var args = $"-m pip install -r \"{requirements}\" --upgrade --disable-pip-version-check --no-input --progress-bar off";
            AppendEnvLog(backendDir, moduleName, "Running: .venv\\Scripts\\python.exe " + args);
            int exit;
            try
            {
                exit = await RunProcessAsync(venvPython, args, backendDir, PipTimeout, moduleName, ct).ConfigureAwait(false);
            }
            catch (TimeoutException ex)
            {
                SafeLog(moduleName, "Python environment: pip install " + ex.Message + ".", null, error: true);
                AppendEnvLog(backendDir, moduleName, "pip install " + ex.Message);
                return false;
            }
            if (exit != 0)
            {
                SafeLog(moduleName, $"Python environment: pip install exited with code {exit} (offline? see env-setup.log).", null, error: true);
                AppendEnvLog(backendDir, moduleName, $"pip install exited with code {exit}.");
                return false;
            }

            File.WriteAllText(hashFile, hash + "\r\n", Encoding.ASCII);
            try { File.Delete(pendingFile); } catch { }
            SafeLog(moduleName, "Python environment ready (packages installed).", null, error: false);
            AppendEnvLog(backendDir, moduleName, "Install finished - stamp written.");
            return true;
        }

        /// <summary>Same rules as Get-InstallReason in the run_*.ps1 scripts; null = up to date.</summary>
        private static string? GetInstallReason(string backendDir)
        {
            var venvDir = Path.Combine(backendDir, ".venv");
            if (!File.Exists(VenvPython(backendDir))) return "no .venv yet";
            if (File.Exists(Path.Combine(venvDir, PendingFileName))) return "last install did not complete";
            var hashFile = Path.Combine(venvDir, HashFileName);
            if (!File.Exists(hashFile)) return "requirements not recorded yet";
            string recorded;
            try { recorded = File.ReadAllText(hashFile).Trim(); }
            catch { recorded = ""; }
            return string.Equals(recorded, RequirementsHash(backendDir), StringComparison.OrdinalIgnoreCase)
                ? null
                : "requirements.txt changed";
        }

        /// <summary>SHA-256 hex (upper case, like PowerShell's Get-FileHash) or "none".</summary>
        private static string RequirementsHash(string backendDir)
        {
            var requirements = Path.Combine(backendDir, "requirements.txt");
            if (!File.Exists(requirements)) return "none";
            using var fs = new FileStream(requirements, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return Convert.ToHexString(SHA256.HashData(fs));
        }

        private static string VenvPython(string backendDir) => Path.Combine(backendDir, ".venv", "Scripts", "python.exe");

        /// <summary>
        /// False only when the venv's python.exe demonstrably doesn't run (e.g. its base Python
        /// was uninstalled). A slow start (timeout) counts as running - never wipe a venv for that.
        /// </summary>
        private static async Task<bool> VenvRunsAsync(string backendDir, string moduleName, CancellationToken ct)
        {
            try
            {
                var exit = await RunProcessAsync(VenvPython(backendDir), "-c \"import sys\"", backendDir, ProbeTimeout, moduleName, ct)
                    .ConfigureAwait(false);
                return exit == 0;
            }
            catch (TimeoutException) { return true; }
            catch (OperationCanceledException) { throw; }
            catch (Exception ex)
            {
                SafeLog(moduleName, "Python environment: .venv\\Scripts\\python.exe could not be started.", ex, error: false);
                return false;
            }
        }

        private static async Task<bool> CreateVenvAsync(string backendDir, string moduleName, bool clear, CancellationToken ct)
        {
            var venvDir = Path.Combine(backendDir, ".venv");
            var venvArgs = "-m venv" + (clear ? " --clear" : "") + " \"" + venvDir + "\"";
            // "python" first (like the scripts), then the py launcher (python.org installs
            // without "Add to PATH"; also skips the Microsoft Store alias stub).
            var candidates = new[] { ("python", venvArgs), ("py", "-3 " + venvArgs) };
            foreach (var (exe, args) in candidates)
            {
                ct.ThrowIfCancellationRequested();
                AppendEnvLog(backendDir, moduleName, "Running: " + exe + " " + args);
                try
                {
                    var exit = await RunProcessAsync(exe, args, backendDir, VenvTimeout, moduleName, ct).ConfigureAwait(false);
                    if (exit == 0 && File.Exists(VenvPython(backendDir))) return true;
                    SafeLog(moduleName, $"Python environment: '{exe} {args}' exited with code {exit}.", null, error: false);
                }
                catch (OperationCanceledException) { throw; }
                catch (Exception ex)
                {
                    SafeLog(moduleName, $"Python environment: could not run '{exe}': {ex.Message}", null, error: false);
                    AppendEnvLog(backendDir, moduleName, $"could not run '{exe}': {ex.Message}");
                }
            }
            SafeLog(moduleName, "Python environment: could not create .venv - is Python 3 installed and on PATH (python or the py launcher)?",
                    null, error: true);
            return false;
        }

        // ------------------------------------------------------------------ lock

        private static async Task<FileStream?> AcquireLockAsync(string backendDir, string moduleName,
                                                                IProgress<string>? progress, CancellationToken ct)
        {
            var path = Path.Combine(backendDir, LockFileName);
            var deadline = DateTime.UtcNow + LockWaitMax;
            var announced = false;
            var errors = 0;
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                try
                {
                    var fs = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read, 4096, FileOptions.DeleteOnClose);
                    try
                    {
                        var text = $"{Environment.ProcessId}\r\n{DateTime.UtcNow:o}\r\napp:{moduleName}\r\n";
                        var bytes = Encoding.ASCII.GetBytes(text);
                        fs.Write(bytes, 0, bytes.Length);
                        fs.Flush();
                    }
                    catch
                    {
                        // Content is informational only; the open handle is the lock.
                    }
                    if (announced) SafeLog(moduleName, "Python environment: the other install/update finished.", null, error: false);
                    return fs;
                }
                catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
                {
                    if (!File.Exists(path))
                    {
                        // Not a held lock (e.g. the folder is read-only) - retry a few times.
                        if (++errors >= 5)
                        {
                            SafeLog(moduleName, "Python environment: could not create " + path + ".", ex, error: true);
                            return null;
                        }
                        await Task.Delay(500, ct).ConfigureAwait(false);
                        continue;
                    }
                }

                if (IsLockStale(path))
                {
                    SafeLog(moduleName, "Python environment: removing a stale .venv.lock (its process is gone or it is older than 30 minutes).",
                            null, error: false);
                    try { File.Delete(path); }
                    catch { await Task.Delay(2000, ct).ConfigureAwait(false); }
                    continue;
                }

                if (!announced)
                {
                    announced = true;
                    SafeLog(moduleName, "Python environment: another install/update of this .venv is running - waiting for it to finish.",
                            null, error: false);
                    progress?.Report("waiting for a running install/update to finish…");
                }
                if (DateTime.UtcNow > deadline) return null;
                await Task.Delay(1000, ct).ConfigureAwait(false);
            }
        }

        /// <summary>Same rules as Test-LockStale in the run_*.ps1 scripts.</summary>
        private static bool IsLockStale(string path)
        {
            var holder = 0;
            DateTime lastWriteUtc;
            try
            {
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var reader = new StreamReader(fs, Encoding.ASCII))
                {
                    var first = reader.ReadLine();
                    int.TryParse(first?.Trim(), out holder);
                }
                lastWriteUtc = File.GetLastWriteTimeUtc(path);
            }
            catch
            {
                return false; // gone (retry) or held open by a live process
            }

            var age = DateTime.UtcNow - lastWriteUtc;
            if (age > LockStaleAfter) return true;
            if (holder <= 0) return age > TimeSpan.FromMinutes(1);
            if (holder == Environment.ProcessId) return true; // readable, so not held by us
            try
            {
                using var p = Process.GetProcessById(holder);
                return p.HasExited;
            }
            catch (ArgumentException)
            {
                return true; // no such process
            }
            catch
            {
                return false; // can't tell (e.g. access denied) - treat as held
            }
        }

        // ------------------------------------------------------------------ helpers

        /// <summary>Runs a process without a window; every output line goes to AppLog + env-setup.log.</summary>
        private static async Task<int> RunProcessAsync(string fileName, string arguments, string backendDir, TimeSpan timeout,
                                                       string moduleName, CancellationToken ct)
        {
            var psi = new ProcessStartInfo
            {
                FileName = fileName,
                Arguments = arguments,
                WorkingDirectory = backendDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            psi.Environment["PYTHONIOENCODING"] = "utf-8";

            using var process = new Process { StartInfo = psi };
            process.OutputDataReceived += (s, e) =>
            {
                if (string.IsNullOrWhiteSpace(e.Data)) return;
                SafeLog(moduleName, "[env] " + e.Data, null, error: false);
                AppendEnvLog(backendDir, moduleName, e.Data);
            };
            process.ErrorDataReceived += (s, e) =>
            {
                if (string.IsNullOrWhiteSpace(e.Data)) return;
                try { AppLog.Warn(moduleName, "[env] " + e.Data); } catch { }
                AppendEnvLog(backendDir, moduleName, "[err] " + e.Data);
            };

            if (!process.Start()) throw new InvalidOperationException("Could not start " + fileName);
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();

            using var timeoutCts = new CancellationTokenSource(timeout);
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(timeoutCts.Token, ct);
            try
            {
                await process.WaitForExitAsync(linked.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                try { process.Kill(true); } catch { }
                ct.ThrowIfCancellationRequested();
                throw new TimeoutException($"timed out after {timeout.TotalMinutes:0.#} minutes");
            }
            return process.ExitCode;
        }

        private static void SetState(string text, bool busy)
        {
            lock (Sync)
            {
                _statusText = text;
                _isBusy = busy;
            }
            RaiseStateChanged();
        }

        private static void RaiseStateChanged()
        {
            try { StateChanged?.Invoke(); } catch { }
        }

        private static void SafeLog(string module, string message, Exception? ex, bool error)
        {
            try
            {
                if (error) AppLog.Error(module, message, ex);
                else if (ex != null) AppLog.Warn(module, message, ex);
                else AppLog.Info(module, message);
            }
            catch { }
        }

        private static void AppendEnvLog(string backendDir, string moduleName, string line)
        {
            try
            {
                lock (LogFileSync)
                {
                    var logDir = Path.Combine(backendDir, "data", "logs");
                    Directory.CreateDirectory(logDir);
                    File.AppendAllText(Path.Combine(logDir, "env-setup.log"),
                        $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] [{moduleName}] {line}{Environment.NewLine}",
                        new UTF8Encoding(false));
                }
            }
            catch { /* logging must never break the setup */ }
        }

        private sealed class SyncProgress : IProgress<string>
        {
            private readonly Action<string> _report;
            public SyncProgress(Action<string> report) { _report = report; }
            public void Report(string value)
            {
                try { _report(value); } catch { }
            }
        }
    }
}

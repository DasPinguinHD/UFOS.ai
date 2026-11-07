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
    /// (HedgeFund, Global Monitoring, LiveStreamAgent) and of the Risk Lab (UFOS.ai\RiskLab\.venv,
    /// stage 11 Risk Lab view, see "Risk Lab lock contract" below; prepared LAZILY, never at app start). Added 2026-09-25.
    ///
    /// - The root UFOS.ai app calls <see cref="StartAppPreparation"/> once at start
    ///   (MainWindow.Window_Loaded): in the background, one module after the other,
    ///   it creates a missing .venv and runs
    ///   "pip install --require-hashes -r requirements.lock" ONLY when it is required
    ///   (no/broken .venv, the lock changed since the last successful install, or the
    ///   last install did not complete). Stage 10 (pinned dependencies, user 2026-10-24,
    ///   contract LOCK_API): installs come ONLY from a current requirements.lock (every
    ///   package "==" pinned with its sha256, made by make_lockfile.py); a missing or stale
    ///   lock refuses the install with the exact command to run — never an unpinned install. MainWindow shows the
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
    /// - stamp .venv\ufos-requirements.sha256 = SHA-256 of requirements.lock when it exists,
    ///   before the first lock of requirements.txt (upper-case hex like PowerShell's
    ///   Get-FileHash, "none" without either), written only after a successful install;
    /// - a lock is current when its "# source: requirements.txt sha256=&lt;lower-case hex&gt;" line
    ///   matches requirements.txt (<see cref="LockProblem"/>, = Get-LockProblem in the scripts);
    /// - .venv\ufos-install-pending exists while an install runs (a crash leaves it,
    ///   which forces a reinstall);
    /// - backend\.venv.lock is the cross-process lock: created with CreateNew and held
    ///   open with DeleteOnClose (no FileShare.Delete), so nobody else can open it while
    ///   it's held and Windows deletes it even if the holder is killed. A readable
    ///   leftover is stale when its PID is gone or it is older than 30 minutes.
    ///   (A file lock instead of a named Mutex: the PowerShell scripts, including the
    ///   Settings "Update Python packages" run, already use it, and it isn't thread-affine.)
    ///
    /// Risk Lab lock contract (RISKLAB_VIEW_TASK.md §B.2, 2026-11-06; review fixes rlview-be2 2026-11-07) — a folder with
    /// requirements.in and NO requirements.txt (<see cref="IsUvCompiled"/>): RiskLab's requirements.lock is compiled by uv
    /// (<see cref="UvCompileCommand"/>, uv 0.13.7, RiskLab README "Dependencies") and has no "# source:" line. Rules
    /// (<see cref="UvLockProblem"/>, checked on EVERY prepare / wait, also when the venv is installed):
    /// - lock missing, or its header has no "uv pip compile requirements.in" line → stop with the exact uv command;
    /// - .venv\ufos-lock-source.sha256 records "requirements.in normalized-sha256=&lt;hex&gt;" and
    ///   "requirements.lock sha256=&lt;hex&gt;" (lower-case) — written after every successful install and, when missing,
    ///   whenever the check passes with an up-to-date venv. requirements.in is hashed NORMALISED
    ///   (<see cref="NormalizedRequirementsIn"/>: comments, blank lines, whitespace runs, line order, line endings and the
    ///   case of project names do not count), so a cosmetic edit that recompiles to the identical lock is not "stale";
    ///   a real change of requirements.in while the lock did NOT change → stale → stop with the exact uv command; both
    ///   changed = the lock was recompiled → install;
    /// - no record yet (first install, recreated venv) → the lock is accepted as compiled from the current
    ///   requirements.in (it cannot be checked further; the header names the source);
    /// - the venv's Python must be one the lock has wheels for (<see cref="UvPythonMin"/>–<see cref="UvPythonMax"/>:
    ///   numpy 2.2.6 / scipy 1.15.3 publish CPython wheels for 3.10–3.13 only; the lock has no python markers) — checked
    ///   before an install; another version → stop naming the version found and the command to recreate the venv. No
    ///   automatic switch to another interpreter;
    /// - the install itself is the same as for the backends: "pip install --require-hashes -r requirements.lock" only
    ///   when the lock changed (same stamp .venv\ufos-requirements.sha256), venv lock file RiskLab\.venv.lock, log
    ///   RiskLab\data\logs\env-setup.log. The RiskLab package itself is NOT installed: the caller runs
    ///   "python -m risklab.app" with PYTHONPATH = RiskLab\src.
    /// - LAZY (lead decision 2026-11-07): the Risk Lab is not part of the app-start preparation (no first-start download,
    ///   no FailedModules entry); the Risk Lab view calls WaitForModuleAsync("risklab", progress, ct) before its first run.
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
        private const string VenvLockFileName = ".venv.lock";
        private const string HashFileName = "ufos-requirements.sha256";
        private const string PendingFileName = "ufos-install-pending";
        private const string BrokenReason = "the .venv is broken - its python.exe does not run";

        private static readonly TimeSpan PipTimeout = TimeSpan.FromMinutes(15);
        private static readonly TimeSpan VenvTimeout = TimeSpan.FromMinutes(5);
        private static readonly TimeSpan ProbeTimeout = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan LockStaleAfter = TimeSpan.FromMinutes(30);
        private static readonly TimeSpan LockWaitMax = TimeSpan.FromMinutes(35);

        // Key (for WaitForModuleAsync(key)), display name, folder below the repo's UFOS.ai folder, marker file that must
        // exist, prepared at app start (false = lazily, on the first WaitForModuleAsync(key)).
        private static readonly (string Key, string Name, string[] RelativeDir, string Marker, bool AtStart)[] KnownModules =
        {
            ("hedgefund", "Hedge Fund", new[] { "HedgeFund", "backend" }, "requirements.txt", true),
            ("globalmonitoring", "Global Monitoring", new[] { "Global Monitoring", "backend" }, "requirements.txt", true),
            ("livestreamagent", "LiveStream Agent", new[] { "LiveStreamAgent", "backend" }, "requirements.txt", true),
            ("risklab", "Risk Lab", new[] { "RiskLab" }, UvSourceFileName, false),  // own venv, uv lock, lazy (Risk Lab view)
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
        /// returns every module backend prepared at app start that has its marker file (requirements.txt). The Risk Lab
        /// is not listed: it is prepared lazily (<see cref="WaitForModuleAsync(string, IProgress{string}?, CancellationToken)"/>).
        /// </summary>
        public static IReadOnlyList<(string Name, string BackendDir)> DiscoverModuleBackends()
        {
            var result = new List<(string Name, string BackendDir)>();
            foreach (var (_, name, relativeDir, marker, atStart) in KnownModules)
            {
                if (!atStart) continue;
                var found = LocateModuleDir(name, relativeDir, marker);
                if (found != null) result.Add((name, found));
            }
            return result;
        }

        /// <summary>
        /// The folder of a known module by key ("hedgefund", "globalmonitoring", "livestreamagent", "risklab";
        /// case-insensitive), found like <see cref="DiscoverModuleBackends"/>; null when the key is unknown or the folder
        /// (with its marker file) is not found.
        /// </summary>
        public static string? FindModuleDir(string moduleKey)
        {
            foreach (var (key, name, relativeDir, marker, _) in KnownModules)
                if (string.Equals(key, moduleKey, StringComparison.OrdinalIgnoreCase))
                    return LocateModuleDir(name, relativeDir, marker);
            return null;
        }

        private static string? LocateModuleDir(string name, string[] relativeDir, string marker)
        {
            try
            {
                var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
                for (var depth = 0; dir != null && depth < 8; depth++, dir = dir.Parent)
                {
                    var moduleDir = Path.Combine(new[] { dir.FullName }.Concat(relativeDir).ToArray());
                    if (File.Exists(Path.Combine(moduleDir, marker))) return moduleDir;
                }
            }
            catch (Exception ex)
            {
                SafeLog(AppLogModule, "Could not look for the " + name + " folder.", ex, error: false);
            }
            return null;
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
        /// For callers that know a module only by its key — the Risk Lab view calls
        /// <c>WaitForModuleAsync("risklab", progress, ct)</c> before its first CLI run (lazy preparation: creates / installs
        /// the venv when needed). Same as the (backendDir, moduleName) overload; false (logged, reported through
        /// <paramref name="progress"/>) when the key is unknown, the module folder is not found, or the environment
        /// could not be prepared — the module-specific reason was reported before.
        /// </summary>
        public static Task<bool> WaitForModuleAsync(string moduleKey)
            => WaitForModuleAsync(moduleKey, null, CancellationToken.None);

        /// <inheritdoc cref="WaitForModuleAsync(string)"/>
        public static async Task<bool> WaitForModuleAsync(string moduleKey, IProgress<string>? progress, CancellationToken ct)
        {
            foreach (var (key, name, relativeDir, marker, _) in KnownModules)
            {
                if (!string.Equals(key, moduleKey, StringComparison.OrdinalIgnoreCase)) continue;
                var dir = LocateModuleDir(name, relativeDir, marker);
                if (dir == null)
                {
                    var why = "the " + name + " folder (" + string.Join("\\", relativeDir) + " with " + marker + ") was not found next to the app";
                    SafeLog(name, "Python environment: " + why + ".", null, error: true);
                    progress?.Report(why);
                    return false;
                }
                var ok = await WaitForModuleAsync(dir, name, progress, ct).ConfigureAwait(false);
                if (!ok)
                    SafeLog(name, "Python environment: the " + name + " environment (" + Path.Combine(dir, ".venv") +
                                  ") is not ready - " + name + " runs are not possible until it is (reason above; details in " +
                                  Path.Combine(dir, "data", "logs", "env-setup.log") + ").", null, error: true);
                return ok;
            }
            SafeLog(AppLogModule, "Python environment: unknown module key '" + moduleKey + "'.", null, error: true);
            return false;
        }

        /// <summary>The venv's python.exe of a module folder (from <see cref="FindModuleDir"/>); may not exist yet.</summary>
        internal static string VenvPythonPath(string moduleDir) => VenvPython(moduleDir);

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
                    SafeLog(moduleName, "Python environment: could not get " + Path.Combine(backendDir, VenvLockFileName) +
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
            // Risk Lab (uv lock contract, class summary): a missing / stale lock stops even when the venv is installed.
            var uv = IsUvCompiled(backendDir);
            if (uv)
            {
                var uvProblem = UvLockProblem(backendDir);
                if (uvProblem != null)
                {
                    SafeLog(moduleName, "Python environment: " + uvProblem + ".", null, error: true);
                    AppendEnvLog(backendDir, moduleName, "Not installed: " + uvProblem);
                    progress?.Report(uvProblem);
                    return false;
                }
            }
            var reason = GetInstallReason(backendDir);
            if (File.Exists(venvPython) && !await VenvRunsAsync(backendDir, moduleName, ct).ConfigureAwait(false))
                reason = BrokenReason;
            if (reason == null)
            {
                if (uv) EnsureUvRecord(backendDir, moduleName);     // check passed, venv current: record it when missing
                return true;
            }

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
            var lockPath = Path.Combine(backendDir, LockFileName);
            // Hash BEFORE installing: an edit during the install then triggers another one next time.
            var hash = RequirementsHash(backendDir);
            var uvRecord = uv ? UvRecordText(backendDir) : null;

            if (!uv && !File.Exists(requirements))
            {
                File.WriteAllText(hashFile, hash + "\r\n", Encoding.ASCII);
                AppendEnvLog(backendDir, moduleName, "No requirements.txt - nothing to install.");
                return true;
            }

            // Pinned installs only (LOCK_API): no current lock → stop with the exact command, never an unpinned install.
            var lockProblem = uv ? null : LockProblem(backendDir);
            if (lockProblem != null)
            {
                SafeLog(moduleName, "Python environment: " + lockProblem + " (in " + backendDir + ").", null, error: true);
                AppendEnvLog(backendDir, moduleName, "Not installed: " + lockProblem);
                progress?.Report(lockProblem);
                return false;
            }

            if (uv)
            {
                var pyProblem = await UvPythonProblemAsync(backendDir, ct).ConfigureAwait(false);
                if (pyProblem != null)
                {
                    SafeLog(moduleName, "Python environment: " + pyProblem + ".", null, error: true);
                    AppendEnvLog(backendDir, moduleName, "Not installed: " + pyProblem);
                    progress?.Report(pyProblem);
                    return false;
                }
            }

            progress?.Report("installing packages (" + reason + ") — can take a few minutes…");
            File.WriteAllText(pendingFile, "install started\r\n", Encoding.ASCII);
            var args = $"-m pip install --require-hashes -r \"{lockPath}\" --disable-pip-version-check --no-input --progress-bar off";
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
            if (uvRecord != null) File.WriteAllText(Path.Combine(venvDir, UvRecordFileName), uvRecord, Encoding.ASCII);
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
                : "requirements changed";
        }

        internal const string LockFileName = "requirements.lock";

        /// <summary>
        /// The stamp value: SHA-256 hex (upper case, like PowerShell's Get-FileHash) of requirements.lock when it exists,
        /// else of requirements.txt (so an installed venv keeps starting until the first lock is made), else "none".
        /// Same as Get-RequirementsHash in the run_*.ps1 scripts. Also used by Services/PythonEnvManager.cs.
        /// </summary>
        internal static string RequirementsHash(string backendDir)
        {
            var lockPath = Path.Combine(backendDir, LockFileName);
            var file = File.Exists(lockPath) ? lockPath : Path.Combine(backendDir, "requirements.txt");
            if (!File.Exists(file)) return "none";
            using var fs = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return Convert.ToHexString(SHA256.HashData(fs));
        }

        /// <summary>The lock tool as the user runs it from the backend folder (HedgeFund: tools\make_lockfile.py).</summary>
        internal static string LockToolHint(string backendDir)
            => File.Exists(Path.Combine(backendDir, "tools", "make_lockfile.py")) ? "tools\\make_lockfile.py" : "make_lockfile.py";

        private static readonly System.Text.RegularExpressions.Regex LockSourceLine =
            new(@"^# source: \S+ sha256=([0-9a-f]{64})$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);

        /// <summary>
        /// Null when requirements.lock exists and was made from the current requirements.txt (its "# source: … sha256=" line);
        /// else the message with the exact command — same texts as Get-LockProblem in the run_*.ps1 scripts.
        /// </summary>
        internal static string? LockProblem(string backendDir)
        {
            var tool = ".venv\\Scripts\\python.exe " + LockToolHint(backendDir);
            var lockPath = Path.Combine(backendDir, LockFileName);
            if (!File.Exists(lockPath)) return LockFileName + " missing - run once (needs internet): " + tool;
            string want;
            using (var fs = new FileStream(Path.Combine(backendDir, "requirements.txt"), FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                want = Convert.ToHexString(SHA256.HashData(fs)).ToLowerInvariant();
            string? source = null;
            foreach (var line in File.ReadLines(lockPath))
            {
                var m = LockSourceLine.Match(line.TrimEnd('\r'));
                if (m.Success) { source = m.Groups[1].Value; break; }
            }
            if (source == null) return LockFileName + " has no source line - regenerate it: " + tool;
            if (!string.Equals(source, want, StringComparison.Ordinal))
                return LockFileName + " is out of date (requirements changed) - run: " + tool + " --update";
            return null;
        }

        // ------------------------------------------------------------------ Risk Lab: uv-compiled lock

        internal const string UvSourceFileName = "requirements.in";
        private const string UvRecordFileName = "ufos-lock-source.sha256";
        private const string UvLockHeader = "uv pip compile requirements.in";
        private const string UvInKey = "requirements.in normalized-sha256";
        private const string UvLockKey = "requirements.lock sha256";

        /// <summary>The exact command that (re)compiles RiskLab's lock — run in UFOS.ai\RiskLab (RiskLab README "Dependencies").</summary>
        internal const string UvCompileCommand =
            "uv pip compile requirements.in --universal --fork-strategy fewest --generate-hashes --python-version 3.10 -o requirements.lock";

        /// <summary>
        /// The CPython range the lock can be installed on: its packages (numpy 2.2.6, scipy 1.15.3) publish wheels for
        /// cp310–cp313 only (checked on PyPI 2026-11-07; the uv lock carries no python markers). Update together with the lock.
        /// </summary>
        internal static readonly Version UvPythonMin = new(3, 10), UvPythonMax = new(3, 13);

        private static readonly System.Text.RegularExpressions.Regex UvRecordLine =
            new(@"^(requirements\.in normalized-sha256|requirements\.lock sha256)=([0-9a-f]{64})$",
                System.Text.RegularExpressions.RegexOptions.CultureInvariant);

        private static readonly System.Text.RegularExpressions.Regex RequirementName =
            new(@"^[A-Za-z0-9][A-Za-z0-9._-]*", System.Text.RegularExpressions.RegexOptions.CultureInvariant);

        private static readonly System.Text.RegularExpressions.Regex WhitespaceRun =
            new(@"\s+", System.Text.RegularExpressions.RegexOptions.CultureInvariant);

        /// <summary>True for a folder under the uv lock contract: requirements.in and no requirements.txt (the Risk Lab).</summary>
        internal static bool IsUvCompiled(string dir)
            => !File.Exists(Path.Combine(dir, "requirements.txt")) && File.Exists(Path.Combine(dir, UvSourceFileName));

        /// <summary>
        /// Null when requirements.lock may be installed under the uv contract (class summary); else the message with the
        /// exact uv command.
        /// </summary>
        internal static string? UvLockProblem(string dir)
        {
            var run = "run in " + dir + " (needs internet; uv 0.13.7): " + UvCompileCommand;
            var lockPath = Path.Combine(dir, LockFileName);
            if (!File.Exists(lockPath)) return LockFileName + " missing - " + run;
            var header = false;
            foreach (var line in File.ReadLines(lockPath).Take(20))
                if (line.TrimStart().StartsWith("#", StringComparison.Ordinal) && line.Contains(UvLockHeader, StringComparison.Ordinal))
                { header = true; break; }
            if (!header) return LockFileName + " was not compiled from " + UvSourceFileName + " by uv (no \"" + UvLockHeader + "\" header line) - " + run;
            var (recIn, recLock) = ReadUvRecord(dir);
            if (recIn == null || recLock == null) return null;  // no / unreadable record: accepted, (re)written once the check passes
            var inChanged = !string.Equals(recIn, NormalizedInHash(dir), StringComparison.Ordinal);
            var lockChanged = !string.Equals(recLock, Sha256Lower(lockPath), StringComparison.Ordinal);
            if (inChanged && !lockChanged)
                return LockFileName + " is out of date (" + UvSourceFileName + " changed since the last install, the lock did not; recorded in .venv\\"
                       + UvRecordFileName + ") - " + run;
            return null;
        }

        /// <summary>
        /// requirements.in reduced to what the resolver sees: comments ("#" at the line start or after whitespace) and blank
        /// lines removed, whitespace runs collapsed to one space, lines trimmed, the leading project name lower-cased, lines
        /// sorted (ordinal), joined with LF. Its sha256 is the record's "requirements.in normalized-sha256".
        /// </summary>
        internal static string NormalizedRequirementsIn(string text)
        {
            var lines = new List<string>();
            foreach (var raw in text.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n'))
            {
                var line = raw;
                for (var i = 0; i < line.Length; i++)
                    if (line[i] == '#' && (i == 0 || char.IsWhiteSpace(line[i - 1]))) { line = line.Substring(0, i); break; }
                line = WhitespaceRun.Replace(line, " ").Trim();
                if (line.Length == 0) continue;
                var name = RequirementName.Match(line);
                if (name.Success) line = name.Value.ToLowerInvariant() + line.Substring(name.Length);
                lines.Add(line);
            }
            lines.Sort(StringComparer.Ordinal);
            return string.Join("\n", lines);
        }

        private static string NormalizedInHash(string dir)
        {
            string text;
            using (var fs = new FileStream(Path.Combine(dir, UvSourceFileName), FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var reader = new StreamReader(fs, Encoding.UTF8))
                text = reader.ReadToEnd();
            return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(NormalizedRequirementsIn(text)))).ToLowerInvariant();
        }

        private static (string? In, string? Lock) ReadUvRecord(string dir)
        {
            var record = Path.Combine(dir, ".venv", UvRecordFileName);
            if (!File.Exists(record)) return (null, null);
            string? recIn = null, recLock = null;
            try
            {
                foreach (var line in File.ReadLines(record))
                {
                    var m = UvRecordLine.Match(line.Trim());
                    if (!m.Success) continue;
                    if (m.Groups[1].Value == UvInKey) recIn = m.Groups[2].Value; else recLock = m.Groups[2].Value;
                }
            }
            catch (IOException) { return (null, null); }
            return (recIn, recLock);
        }

        /// <summary>The record written after a successful install (hashes taken BEFORE the install, like the stamp).</summary>
        private static string UvRecordText(string dir)
            => UvInKey + "=" + NormalizedInHash(dir) + "\r\n"
             + UvLockKey + "=" + Sha256Lower(Path.Combine(dir, LockFileName)) + "\r\n";

        /// <summary>Writes the record when it is missing / unreadable and the venv is current (MINOR 3 of the review).</summary>
        private static void EnsureUvRecord(string dir, string moduleName)
        {
            var (recIn, recLock) = ReadUvRecord(dir);
            if (recIn != null && recLock != null) return;
            try
            {
                File.WriteAllText(Path.Combine(dir, ".venv", UvRecordFileName), UvRecordText(dir), Encoding.ASCII);
                AppendEnvLog(dir, moduleName, "Lock source recorded in .venv\\" + UvRecordFileName + ".");
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                SafeLog(moduleName, "Python environment: could not write .venv\\" + UvRecordFileName + " (checked again next time).", ex, error: false);
            }
        }

        /// <summary>
        /// Null when the venv's python.exe is a CPython the lock has wheels for (<see cref="UvPythonMin"/>–<see cref="UvPythonMax"/>);
        /// else the message naming the version found (or why it could not be read) and how to recreate the venv.
        /// </summary>
        private static async Task<string?> UvPythonProblemAsync(string dir, CancellationToken ct)
        {
            var py = VenvPython(dir);
            var range = UvPythonMin.ToString(2) + "–" + UvPythonMax.ToString(2);
            var recreate = "recreate the venv with a supported Python, in " + dir + ": py -" + UvPythonMax.ToString(2) +
                           " -m venv --clear .venv (no other interpreter is picked automatically)";
            var r = await OneShotProcess.RunAsync(py, new[] { "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])" },
                                                  workingDirectory: dir, timeout: ProbeTimeout, ct: ct).ConfigureAwait(false);
            ct.ThrowIfCancellationRequested();
            var text = r.StdOut.Trim();
            if (r.ExitCode != 0 || !Version.TryParse(text, out var found))
                return "the Python version of " + py + " could not be read (" + r.Describe() + ") - the " + LockFileName +
                       " needs CPython " + range + "; " + recreate;
            var minor = new Version(found.Major, found.Minor);
            if (minor < UvPythonMin || minor > UvPythonMax)
                return "the Risk Lab venv uses Python " + text + ", but " + LockFileName + " (numpy / scipy wheels) supports only CPython " +
                       range + " - " + recreate;
            return null;
        }

        private static string Sha256Lower(string path)
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return Convert.ToHexString(SHA256.HashData(fs)).ToLowerInvariant();
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
            var path = Path.Combine(backendDir, VenvLockFileName);
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

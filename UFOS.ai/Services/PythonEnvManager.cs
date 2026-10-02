using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.Services
{
    /// <summary>One module's Python backend as found on disk.</summary>
    internal sealed record PythonEnvModule(
        string Name,
        string BackendDir,
        string ScriptPath,
        string HealthUrl,
        string? ShutdownUrl,
        string StopHint);

    /// <summary>What the Settings popup shows for one module.</summary>
    internal sealed class PythonEnvModuleStatus
    {
        public PythonEnvModuleStatus(string name) { Name = name; }

        public string Name { get; }
        /// <summary>null = the backend folder wasn't found next to this app.</summary>
        public PythonEnvModule? Module { get; set; }
        /// <summary>Non-null: the module's launcher script installs packages on its next start.</summary>
        public string? RequiredReason { get; set; }
        /// <summary>An install/update of this venv is running right now (.venv.lock held).</summary>
        public bool Locked { get; set; }
        /// <summary>Optional updates from the daily online check (requirements.txt packages only).</summary>
        public List<string> Outdated { get; set; } = new();
        public string? CheckError { get; set; }
        /// <summary>Result of the last manual "Update Python packages" run in this session.</summary>
        public string? LastResult { get; set; }
        public bool LastResultFailed { get; set; }
    }

    /// <summary>
    /// Root-app side of the Python environment handling for the three module backends
    /// (HedgeFund, Global Monitoring, LiveStreamAgent). Added 2026-09-25.
    ///
    /// The REQUIRED installs (no .venv, requirements.txt changed, last install incomplete)
    /// are done once at app start by Shared/PythonEnvironment.cs (the module launchers wait
    /// for it and start run_*.ps1 with -SkipInstall; a script run by hand still installs
    /// itself - same stamp and .venv.lock protocol); this class only
    /// - checks, offline and fast at app start, whether a module will install on its next
    ///   start (same rules as the scripts: .venv\ufos-requirements.sha256 = SHA-256 of
    ///   requirements.txt, .venv\ufos-install-pending = last install didn't complete);
    /// - at most once a day, runs "pip list --outdated" per existing venv (needs network,
    ///   failures are tolerated; state in %LOCALAPPDATA%\UFOS.ai\python-env\check.json);
    /// - runs the MANUAL optional upgrade (Settings -> "Update Python packages"): per module
    ///   "run_*.ps1 -Upgrade -EnvOnly", which takes the same backend\.venv.lock as a normal
    ///   start, so a backend never starts while its venv is being changed. A running backend
    ///   is asked to stop first (loopback POST /shutdown) because Windows keeps its package
    ///   files locked; it is NOT restarted here - the module starts it again when it next
    ///   needs it (e.g. NewsroomBackend.EnsureStartedAsync when the Hedge Fund window opens).
    /// Full output goes to AppLog ("Python env") and %LOCALAPPDATA%\UFOS.ai\python-env\update.log.
    /// </summary>
    internal static class PythonEnvManager
    {
        private const string LogModule = "Python env";
        private const string EnvMarkerPrefix = "UFOS_ENV:";
        private static readonly TimeSpan CheckInterval = TimeSpan.FromHours(24);
        private static readonly TimeSpan LockStaleAfter = TimeSpan.FromMinutes(30);

        private static readonly object Sync = new();
        private static IReadOnlyList<PythonEnvModuleStatus> _statuses = Array.Empty<PythonEnvModuleStatus>();
        private static readonly Dictionary<string, (string Text, bool Failed)> LastResults = new();
        private static int _busy; // 1 while an update or online check runs

        /// <summary>Raised (on any thread) when <see cref="Statuses"/> or <see cref="IsUpdating"/> changed.</summary>
        public static event Action? Changed;

        public static IReadOnlyList<PythonEnvModuleStatus> Statuses { get { lock (Sync) return _statuses; } }
        public static bool IsUpdating { get; private set; }
        public static bool IsChecking { get; private set; }
        public static DateTime? LastOnlineCheckUtc { get; private set; }

        public static string DataDir => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai", "python-env");
        public static string UpdateLogPath => Path.Combine(DataDir, "update.log");
        private static string CheckStatePath => Path.Combine(DataDir, "check.json");

        /// <summary>True when something is pending: a required install or optional updates.</summary>
        public static bool HasPending => Statuses.Any(s => s.RequiredReason != null || s.Outdated.Count > 0);

        // ------------------------------------------------------------------ modules

        private sealed record KnownModule(string Name, string[] RelativeDir, string Script, int HealthPort, bool HasShutdown, string StopHint);

        private static readonly KnownModule[] Known =
        {
            new("Hedge Fund", new[] { "HedgeFund", "backend" }, "run_hedgefund_backend.ps1", 8867, true, ""),
            new("Global Monitoring", new[] { "Global Monitoring", "backend" }, "run_backend.ps1", 8768, true, ""),
            // LiveStreamAgent only has GET /health (port 8766), no /shutdown endpoint.
            new("LiveStream Agent", new[] { "LiveStreamAgent", "backend" }, "run_backend.ps1", 8766, false,
                "close the LiveStream Agent window first"),
        };

        /// <summary>
        /// Walks up from the exe folder (like the module launchers' FindBackendScript) and
        /// returns each module's backend, or null for a module that wasn't found.
        /// </summary>
        private static PythonEnvModule? Locate(KnownModule known)
        {
            var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
            for (var depth = 0; dir != null && depth < 8; depth++, dir = dir.Parent)
            {
                var backendDir = Path.Combine(new[] { dir.FullName }.Concat(known.RelativeDir).ToArray());
                var script = Path.Combine(backendDir, known.Script);
                if (File.Exists(script))
                {
                    var baseUrl = "http://127.0.0.1:" + known.HealthPort;
                    return new PythonEnvModule(known.Name, backendDir, script, baseUrl + "/health",
                        known.HasShutdown ? baseUrl + "/shutdown" : null, known.StopHint);
                }
            }
            return null;
        }

        // ------------------------------------------------------------------ offline check

        /// <summary>Fast, offline: re-evaluates every module (no network, no pip).</summary>
        public static IReadOnlyList<PythonEnvModuleStatus> RefreshOffline()
        {
            var state = LoadCheckState();
            var list = new List<PythonEnvModuleStatus>();
            foreach (var known in Known)
            {
                var status = new PythonEnvModuleStatus(known.Name);
                try
                {
                    var module = Locate(known);
                    status.Module = module;
                    if (module != null)
                    {
                        status.Locked = IsLocked(module.BackendDir);
                        status.RequiredReason = GetInstallReason(module.BackendDir);
                        if (state.Modules.TryGetValue(known.Name, out var check))
                        {
                            // Outdated info only applies to the venv as it was checked.
                            if (status.RequiredReason == null) status.Outdated = check.Outdated.ToList();
                            status.CheckError = check.Error;
                        }
                    }
                }
                catch (Exception ex)
                {
                    status.CheckError = ex.Message;
                }
                lock (Sync)
                {
                    if (LastResults.TryGetValue(known.Name, out var last))
                    {
                        status.LastResult = last.Text;
                        status.LastResultFailed = last.Failed;
                    }
                }
                list.Add(status);
            }
            lock (Sync)
            {
                _statuses = list;
                LastOnlineCheckUtc = state.LastCheckUtc;
            }
            RaiseChanged();
            return list;
        }

        /// <summary>Same rules as Get-InstallReason in the run_*.ps1 scripts.</summary>
        private static string? GetInstallReason(string backendDir)
        {
            var venv = Path.Combine(backendDir, ".venv");
            if (!File.Exists(Path.Combine(venv, "Scripts", "python.exe"))) return "no .venv yet";
            if (File.Exists(Path.Combine(venv, "ufos-install-pending"))) return "last install did not complete";
            var hashFile = Path.Combine(venv, "ufos-requirements.sha256");
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
            var req = Path.Combine(backendDir, "requirements.txt");
            if (!File.Exists(req)) return "none";
            using var fs = new FileStream(req, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return Convert.ToHexString(SHA256.HashData(fs));
        }

        /// <summary>
        /// backend\.venv.lock is held open by run_*.ps1 (DeleteOnClose, no FileShare.Delete),
        /// so while it's held it can't be opened here. A readable leftover is stale when its
        /// PID is gone or it's older than 30 minutes.
        /// </summary>
        private static bool IsLocked(string backendDir)
        {
            var path = Path.Combine(backendDir, ".venv.lock");
            if (!File.Exists(path)) return false;
            string content;
            try
            {
                using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                using var reader = new StreamReader(fs);
                content = reader.ReadToEnd();
            }
            catch (FileNotFoundException) { return false; }
            catch (DirectoryNotFoundException) { return false; }
            catch (IOException) { return true; }                 // held open by a running install
            catch (UnauthorizedAccessException) { return true; } // being deleted right now
            catch { return false; }

            var age = DateTime.UtcNow - File.GetLastWriteTimeUtc(path);
            if (age > LockStaleAfter) return false;
            var first = content.Split('\n')[0].Trim();
            if (!int.TryParse(first, out var pid) || pid <= 0) return age < TimeSpan.FromMinutes(1);
            try
            {
                using var p = Process.GetProcessById(pid);
                return !p.HasExited;
            }
            catch { return false; }
        }

        // ------------------------------------------------------------------ online check

        private sealed class CheckState
        {
            public DateTime? LastCheckUtc { get; set; }
            public Dictionary<string, ModuleCheck> Modules { get; set; } = new();
        }

        private sealed class ModuleCheck
        {
            public List<string> Outdated { get; set; } = new();
            public string? Error { get; set; }
            public DateTime CheckedUtc { get; set; }
        }

        private static CheckState LoadCheckState()
        {
            try
            {
                if (File.Exists(CheckStatePath))
                    return JsonSerializer.Deserialize<CheckState>(File.ReadAllText(CheckStatePath)) ?? new CheckState();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Could not read " + CheckStatePath + " - checking again.", ex);
            }
            return new CheckState();
        }

        private static void SaveCheckState(CheckState state)
        {
            try
            {
                Directory.CreateDirectory(DataDir);
                File.WriteAllText(CheckStatePath, JsonSerializer.Serialize(state, new JsonSerializerOptions { WriteIndented = true }));
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Could not write " + CheckStatePath + ".", ex);
            }
        }

        /// <summary>
        /// Runs "pip list --outdated" for every existing venv, at most once a day unless
        /// <paramref name="force"/>. Needs network; failures are recorded, not thrown.
        /// </summary>
        public static async Task RunOnlineCheckIfDueAsync(bool force = false)
        {
            var state = LoadCheckState();
            if (!force && state.LastCheckUtc != null && DateTime.UtcNow - state.LastCheckUtc.Value < CheckInterval) return;
            if (Interlocked.CompareExchange(ref _busy, 1, 0) != 0) return;
            IsChecking = true;
            RaiseChanged();
            try
            {
                foreach (var known in Known)
                {
                    var module = Locate(known);
                    if (module == null) { state.Modules.Remove(known.Name); continue; }
                    if (GetInstallReason(module.BackendDir) != null)
                    {
                        // Will be (re)installed on the next start anyway; nothing optional to report.
                        state.Modules[known.Name] = new ModuleCheck { CheckedUtc = DateTime.UtcNow };
                        continue;
                    }
                    if (IsLocked(module.BackendDir)) continue; // keep the previous result
                    state.Modules[known.Name] = await CheckOutdatedAsync(module);
                }
                state.LastCheckUtc = DateTime.UtcNow;
                SaveCheckState(state);
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Python package update check failed.", ex);
            }
            finally
            {
                IsChecking = false;
                Interlocked.Exchange(ref _busy, 0);
                RefreshOffline();
            }
        }

        private static async Task<ModuleCheck> CheckOutdatedAsync(PythonEnvModule module)
        {
            var result = new ModuleCheck { CheckedUtc = DateTime.UtcNow };
            var python = Path.Combine(module.BackendDir, ".venv", "Scripts", "python.exe");
            var stdout = new StringBuilder();
            string? lastError = null;
            try
            {
                var exit = await RunProcessAsync(python,
                    "-m pip list --outdated --format=json --disable-pip-version-check --retries 1 --timeout 10",
                    module.BackendDir, TimeSpan.FromMinutes(2),
                    (line, isError) => { if (isError && !string.IsNullOrWhiteSpace(line)) lastError = line.Trim(); },
                    stdout);
                if (exit != 0)
                {
                    result.Error = "pip exited with code " + exit + (lastError != null ? ": " + lastError : " (offline?)");
                    AppLog.Warn(LogModule, module.Name + ": update check failed - " + result.Error);
                    return result;
                }

                var text = stdout.ToString();
                var start = text.IndexOf('[');
                if (start < 0) { result.Error = "unexpected pip output"; return result; }
                var requirements = ReadRequirementNames(module.BackendDir);
                using var doc = JsonDocument.Parse(text.Substring(start));
                foreach (var item in doc.RootElement.EnumerateArray())
                {
                    var name = item.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                    var version = item.TryGetProperty("version", out var v) ? v.GetString() ?? "" : "";
                    var latest = item.TryGetProperty("latest_version", out var l) ? l.GetString() ?? "" : "";
                    if (name.Length == 0) continue;
                    // "pip install -r requirements.txt --upgrade" only upgrades the listed,
                    // unpinned packages - report just those, or the dot would never go away.
                    if (requirements.Count > 0 &&
                        (!requirements.TryGetValue(NormalizeName(name), out var pinned) || pinned)) continue;
                    result.Outdated.Add($"{name} {version} -> {latest}");
                }
                AppLog.Info(LogModule, $"{module.Name}: {result.Outdated.Count} optional package update(s)" +
                                       (result.Outdated.Count > 0 ? ": " + string.Join(", ", result.Outdated) : "."));
            }
            catch (Exception ex)
            {
                result.Error = ex.Message;
                AppLog.Warn(LogModule, module.Name + ": update check failed.", ex);
            }
            return result;
        }

        /// <summary>Normalized requirement name -> pinned (== / ===).</summary>
        private static Dictionary<string, bool> ReadRequirementNames(string backendDir)
        {
            var names = new Dictionary<string, bool>(StringComparer.Ordinal);
            try
            {
                foreach (var rawLine in File.ReadAllLines(Path.Combine(backendDir, "requirements.txt")))
                {
                    var line = rawLine;
                    var hash = line.IndexOf('#');
                    if (hash >= 0) line = line.Substring(0, hash);
                    line = line.Trim();
                    if (line.Length == 0 || line.StartsWith("-", StringComparison.Ordinal)) continue;
                    var m = Regex.Match(line, @"^([A-Za-z0-9][A-Za-z0-9._-]*)");
                    if (!m.Success) continue;
                    var rest = line.Substring(m.Length);
                    var marker = rest.IndexOf(';');
                    if (marker >= 0) rest = rest.Substring(0, marker);
                    names[NormalizeName(m.Groups[1].Value)] = rest.Contains("==", StringComparison.Ordinal);
                }
            }
            catch { /* no/unreadable requirements.txt: report everything pip lists */ }
            return names;
        }

        private static string NormalizeName(string name) => Regex.Replace(name.ToLowerInvariant(), "[-_.]+", "-");

        // ------------------------------------------------------------------ manual upgrade

        /// <summary>
        /// Settings -> "Update Python packages": sequentially upgrades every module's venv.
        /// <paramref name="progress"/> gets short one-line updates (any thread).
        /// </summary>
        public static async Task UpdateAllAsync(Action<string> progress)
        {
            if (Interlocked.CompareExchange(ref _busy, 1, 0) != 0)
            {
                progress("An update or update check is already running.");
                return;
            }
            IsUpdating = true;
            RaiseChanged();
            var anyUpdated = false;
            try
            {
                LogLine($"===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} Update Python packages =====");
                foreach (var known in Known)
                {
                    var module = Locate(known);
                    if (module == null)
                    {
                        SetResult(known.Name, "skipped - backend folder not found", failed: true);
                        continue;
                    }
                    var (text, failed, changed) = await UpdateModuleAsync(module, progress);
                    if (changed) anyUpdated = true;
                    SetResult(known.Name, text, failed);
                    RefreshOffline();
                }
                LogLine($"===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} done =====");
            }
            catch (Exception ex)
            {
                AppLog.Error(LogModule, "Update Python packages failed.", ex);
                LogLine("ERROR: " + ex);
            }
            finally
            {
                IsUpdating = false;
                Interlocked.Exchange(ref _busy, 0);
                RefreshOffline();
            }

            // Refresh the optional-update counts right away (network was just available).
            if (anyUpdated || HasPending)
            {
                progress("Re-checking for optional updates…");
                await RunOnlineCheckIfDueAsync(force: true);
            }
        }

        private static async Task<(string Text, bool Failed, bool Changed)> UpdateModuleAsync(PythonEnvModule module, Action<string> progress)
        {
            var name = module.Name;
            var stoppedBackend = false;

            if (await IsHealthyAsync(module.HealthUrl))
            {
                if (module.ShutdownUrl == null)
                {
                    LogLine($"{name}: backend is running and has no /shutdown endpoint - skipped.");
                    return ("skipped - backend is running; " + module.StopHint, true, false);
                }
                progress($"{name}: stopping its running backend…");
                LogLine($"{name}: backend is running - POST {module.ShutdownUrl}");
                try
                {
                    // /shutdown needs the running backend's session token (Shared/BackendAuth.cs).
                    using var client = BackendAuth.CreateClient(module.BackendDir, TimeSpan.FromSeconds(3));
                    await client.PostAsync(module.ShutdownUrl, new StringContent(""));
                }
                catch { /* it may drop the connection while exiting */ }

                var down = false;
                for (var i = 0; i < 60 && !down; i++)
                {
                    await Task.Delay(250);
                    down = !await IsHealthyAsync(module.HealthUrl);
                }
                if (!down)
                {
                    LogLine($"{name}: backend did not stop within 15 s - skipped.");
                    return ("failed - its backend did not stop; close the module window and try again", true, false);
                }
                stoppedBackend = true;
                await Task.Delay(1500); // let python.exe exit and release its package files
            }

            progress($"{name}: updating Python packages…");
            int? changedCount = null;
            string? failReason = null;
            string? lastError = null;
            int exit;
            try
            {
                exit = await RunProcessAsync("powershell.exe",
                    $"-NoProfile -ExecutionPolicy Bypass -File \"{module.ScriptPath}\" -Upgrade -EnvOnly",
                    module.BackendDir, TimeSpan.FromMinutes(40),
                    (line, isError) =>
                    {
                        LogLine($"{name}{(isError ? " [err]" : "")}: {line}");
                        if (isError && !string.IsNullOrWhiteSpace(line)) lastError = line.Trim();
                        if (!line.StartsWith(EnvMarkerPrefix, StringComparison.Ordinal)) return;
                        var marker = line.Substring(EnvMarkerPrefix.Length).Trim();
                        if (marker.StartsWith("changed", StringComparison.OrdinalIgnoreCase) &&
                            int.TryParse(marker.Substring("changed".Length).Trim(), out var n))
                            changedCount = n;
                        else if (marker.StartsWith("failed", StringComparison.OrdinalIgnoreCase))
                            failReason = marker.Substring("failed".Length).Trim();
                        else if (marker.StartsWith("waiting", StringComparison.OrdinalIgnoreCase))
                            progress($"{name}: waiting for a running install to finish…");
                    },
                    null);
            }
            catch (Exception ex)
            {
                LogLine($"{name}: {ex.Message}");
                return ("failed - " + ex.Message, true, false);
            }

            var stoppedNote = stoppedBackend ? " (its backend was stopped)" : "";
            if (exit != 0 || failReason != null)
            {
                var reason = failReason ?? lastError ?? ("exit code " + exit);
                return ("failed - " + Shorten(reason) + stoppedNote, true, false);
            }
            if (changedCount is > 0)
                return ($"updated ({changedCount} package{(changedCount == 1 ? "" : "s")} changed){stoppedNote}", false, true);
            return ("up to date - nothing to upgrade" + stoppedNote, false, false);
        }

        private static string Shorten(string text) => text.Length <= 140 ? text : text.Substring(0, 137) + "...";

        private static void SetResult(string module, string text, bool failed)
        {
            lock (Sync) LastResults[module] = (text, failed);
            var line = $"{module}: {text}";
            if (failed) AppLog.Warn(LogModule, line); else AppLog.Info(LogModule, line);
            AppendUpdateLog(line);
        }

        // ------------------------------------------------------------------ helpers

        private static async Task<bool> IsHealthyAsync(string url)
        {
            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromMilliseconds(800) };
                using var resp = await client.GetAsync(url);
                return resp.IsSuccessStatusCode;
            }
            catch
            {
                return false;
            }
        }

        /// <summary>Runs a process without a window; lines go to <paramref name="onLine"/> (any thread).</summary>
        private static async Task<int> RunProcessAsync(string fileName, string arguments, string workingDirectory, TimeSpan timeout,
                                                       Action<string, bool>? onLine, StringBuilder? stdoutCapture)
        {
            var psi = new ProcessStartInfo
            {
                FileName = fileName,
                Arguments = arguments,
                WorkingDirectory = workingDirectory,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            psi.Environment["PYTHONIOENCODING"] = "utf-8";

            using var process = new Process { StartInfo = psi };
            var captureLock = new object();
            process.OutputDataReceived += (s, e) =>
            {
                if (e.Data == null) return;
                if (stdoutCapture != null) lock (captureLock) stdoutCapture.AppendLine(e.Data);
                try { onLine?.Invoke(e.Data, false); } catch { }
            };
            process.ErrorDataReceived += (s, e) =>
            {
                if (e.Data == null) return;
                try { onLine?.Invoke(e.Data, true); } catch { }
            };

            if (!process.Start()) throw new InvalidOperationException("Could not start " + fileName);
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();

            using var cts = new CancellationTokenSource(timeout);
            try
            {
                await process.WaitForExitAsync(cts.Token);
            }
            catch (OperationCanceledException)
            {
                try { process.Kill(true); } catch { }
                throw new TimeoutException($"timed out after {timeout.TotalMinutes:0} minutes");
            }
            return process.ExitCode;
        }

        private static void LogLine(string line)
        {
            try { AppLog.Info(LogModule, line); } catch { }
            AppendUpdateLog(line);
        }

        private static readonly object LogFileLock = new();

        private static void AppendUpdateLog(string line)
        {
            try
            {
                lock (LogFileLock)
                {
                    Directory.CreateDirectory(DataDir);
                    File.AppendAllText(UpdateLogPath, $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {line}{Environment.NewLine}", Encoding.UTF8);
                }
            }
            catch { /* logging must never break the update */ }
        }

        private static void RaiseChanged()
        {
            try { Changed?.Invoke(); } catch { }
        }
    }
}

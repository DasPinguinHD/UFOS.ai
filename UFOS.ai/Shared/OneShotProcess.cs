using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UFOS.ai.Logging;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Result of one <see cref="OneShotProcess.RunAsync"/> run. <see cref="ExitCode"/> is null when the process could not be
    /// started (<see cref="Error"/>), was stopped (<see cref="TimedOut"/> / <see cref="Cancelled"/>) or wrote more than the
    /// stdout cap (<see cref="StdOutTooLarge"/> — then <see cref="StdOut"/> is empty: an over-long output is refused, never
    /// parsed in part). <see cref="StdErr"/> keeps at most <see cref="OneShotProcess.MaxStdErrBytes"/> (<see cref="StdErrTruncated"/>).
    /// </summary>
    internal sealed record OneShotResult(int? ExitCode, string StdOut, string StdErr, bool TimedOut, bool Cancelled,
                                         bool StdOutTooLarge, bool StdErrTruncated, string? Error)
    {
        /// <summary>True when the process ran to its end by itself (exit code known), whatever the code.</summary>
        public bool Completed => ExitCode != null;

        /// <summary>A short single-line description for the UI / log (≤ 240 characters); the caller decides what to show.</summary>
        public string Describe()
        {
            if (Error != null) return Error;
            if (Cancelled) return "cancelled - the process was stopped";
            if (TimedOut) return "no result within the time limit - the process was stopped";
            if (StdOutTooLarge) return "the output exceeded " + (OneShotProcess.MaxStdOutBytes / (1024 * 1024)).ToString(CultureInfo.InvariantCulture)
                                       + " MB - the process was stopped";
            var err = FirstLine(StdErr);
            return "exit code " + (ExitCode?.ToString(CultureInfo.InvariantCulture) ?? "?") + (err.Length > 0 ? " - " + err : "");
        }

        private static string FirstLine(string text)
        {
            var line = (text ?? "").Split('\n').Select(l => l.Trim()).FirstOrDefault(l => l.Length > 0) ?? "";
            return line.Length > 200 ? line.Substring(0, 199) + "…" : line;
        }
    }

    /// <summary>
    /// Generic one-shot runner (stage 11 Risk Lab view, RISKLAB_VIEW_TASK.md §B.3; generalises the options archive's
    /// <c>OaProcess.RunAsync</c>): start ONE local process with an absolute executable path and an <see cref="ProcessStartInfo.ArgumentList"/>
    /// (no shell, no PATH lookup, no command-line parsing), hidden (no window), optional stdin text (UTF-8 without BOM, then
    /// closed), stdout captured up to <see cref="MaxStdOutBytes"/> (8 MB; more → the process tree is killed and the result is
    /// <see cref="OneShotResult.StdOutTooLarge"/>), stderr captured up to <see cref="MaxStdErrBytes"/>, both decoded as UTF-8
    /// (a Python child must write UTF-8: pass <see cref="PythonUtf8Environment"/>), a time-out (default
    /// <see cref="DefaultTimeout"/> = 120 s; the caller passes one per command) and cancellation — both kill the whole process
    /// tree. Never throws (except for argument errors by the caller); no fallback, no retry.
    /// Job object (review rlview-be2): right after the start the child is put into its own Windows job with
    /// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; the job is closed when the run ends (also on a normal exit), so grandchildren the
    /// child left behind are killed, and they die with this app if it crashes. If the job cannot be set up the child is
    /// killed and the run fails (no unguarded run). Limitation: the child runs for the few microseconds between
    /// <see cref="Process.Start()"/> and the assignment (.NET cannot start it suspended); a grandchild spawned in that window
    /// is outside the job — the time-out / cancel path still kills the whole tree (<see cref="Process.Kill(bool)"/>).
    /// Compiled into every assembly that links Shared/*.cs (internal, one copy each); depends on nothing but the BCL.
    /// </summary>
    internal static class OneShotProcess
    {
        public static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(120);
        public const int MaxStdOutBytes = 8 * 1024 * 1024;
        public const int MaxStdErrBytes = 1024 * 1024;
        private static readonly TimeSpan DrainAfterExit = TimeSpan.FromSeconds(5);

        /// <summary>Environment for a Python child so that its stdin / stdout / stderr are UTF-8 (Windows defaults to the ANSI code page).</summary>
        public static IReadOnlyDictionary<string, string?> PythonUtf8Environment { get; } = new Dictionary<string, string?>
        {
            ["PYTHONUTF8"] = "1",
            ["PYTHONIOENCODING"] = "utf-8",
        };

        /// <summary>
        /// Runs <paramref name="fileName"/> (fully qualified path) with <paramref name="arguments"/>. <paramref name="environment"/>
        /// adds / replaces variables of the inherited environment (a null value removes one). Returns
        /// {ExitCode, StdOut, StdErr, TimedOut, Cancelled, …}; see <see cref="OneShotResult"/>.
        /// </summary>
        public static async Task<OneShotResult> RunAsync(string fileName, IReadOnlyList<string> arguments, string? stdin = null,
                                                         string? workingDirectory = null, TimeSpan? timeout = null,
                                                         IReadOnlyDictionary<string, string?>? environment = null,
                                                         CancellationToken ct = default)
        {
            ArgumentNullException.ThrowIfNull(fileName);
            ArgumentNullException.ThrowIfNull(arguments);
            var limit = timeout ?? DefaultTimeout;
            if (limit <= TimeSpan.Zero) throw new ArgumentOutOfRangeException(nameof(timeout), "the time-out must be positive");
            var name = SafeName(fileName);
            if (!Path.IsPathFullyQualified(fileName))
                return Fail("refused to start " + name + ": the executable path must be absolute (no PATH lookup)");
            if (!File.Exists(fileName)) return Fail("could not start " + name + ": the file does not exist");
            if (ct.IsCancellationRequested) return new OneShotResult(null, "", "", false, true, false, false, null);

            var utf8 = new UTF8Encoding(false);
            var psi = new ProcessStartInfo
            {
                FileName = fileName,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardInputEncoding = utf8,
            };
            foreach (var a in arguments) psi.ArgumentList.Add(a ?? throw new ArgumentException("an argument is null", nameof(arguments)));
            if (!string.IsNullOrWhiteSpace(workingDirectory)) psi.WorkingDirectory = workingDirectory;
            if (environment != null)
                foreach (var kv in environment)
                {
                    if (kv.Value == null) psi.Environment.Remove(kv.Key);
                    else psi.Environment[kv.Key] = kv.Value;
                }

            Process? p;
            try { p = Process.Start(psi); }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, "could not start " + fileName + ": " + ex.Message, ex); } catch { }
                return Fail("could not start the process (" + ex.GetType().Name + ")");
            }
            if (p == null) return Fail("could not start the process");

            var job = KillOnCloseJob.Assign(p, out var jobError);
            if (job == null)
            {
                KillTree(p);
                p.Dispose();
                try { AppLog.Warn(LogModule, "could not put " + fileName + " into a job object: " + jobError); } catch { }
                return Fail("could not put the process into a Windows job object (" + jobError + ") - it was stopped");
            }

            using (job)
            using (p)
            {
                using var tooLarge = new CancellationTokenSource();
                // Off the caller's thread: pipe streams may block synchronously.
                var outStream = p.StandardOutput.BaseStream;
                var errStream = p.StandardError.BaseStream;
                var outTask = Task.Run(() => ReadCappedAsync(outStream, MaxStdOutBytes, tooLarge));
                var errTask = Task.Run(() => ReadCappedAsync(errStream, MaxStdErrBytes, null));
                var inTask = Task.Run(() => WriteStdInAsync(p, stdin));

                using var timeoutCts = new CancellationTokenSource(limit);
                using var linked = CancellationTokenSource.CreateLinkedTokenSource(timeoutCts.Token, ct, tooLarge.Token);
                var stopped = false;
                try { await p.WaitForExitAsync(linked.Token).ConfigureAwait(false); }
                catch (OperationCanceledException)
                {
                    stopped = true;
                    KillTree(p);
                }
                job.Dispose();     // KILL_ON_JOB_CLOSE: grandchildren left behind die and release the output pipes

                await Task.WhenAny(Task.WhenAll(outTask, errTask, inTask), Task.Delay(DrainAfterExit)).ConfigureAwait(false);
                var (outBytes, outOver) = outTask.IsCompletedSuccessfully ? outTask.Result : (Array.Empty<byte>(), false);
                var (errBytes, errOver) = errTask.IsCompletedSuccessfully ? errTask.Result : (Array.Empty<byte>(), false);
                var stderr = utf8.GetString(errBytes);
                var over = outOver || tooLarge.IsCancellationRequested;    // a too-large stop is always reported as such
                if (stopped || over)
                {
                    var cancelled = !over && ct.IsCancellationRequested;
                    var timedOut = !cancelled && !over && timeoutCts.IsCancellationRequested;
                    if (!stopped) KillTree(p);
                    return new OneShotResult(null, "", stderr, timedOut, cancelled, over, errOver, null);
                }
                if (!outTask.IsCompletedSuccessfully)
                    return new OneShotResult(null, "", stderr, false, false, false, errOver,
                                             name + " exited but its output pipe stayed open (a child process still holds it)");
                return new OneShotResult(p.ExitCode, utf8.GetString(outBytes), stderr, false, false, false, errOver, null);
            }
        }

        private const string LogModule = "Process runner";

        private static OneShotResult Fail(string error) => new(null, "", "", false, false, false, false, error);

        private static string SafeName(string fileName)
        {
            try { return Path.GetFileName(fileName); }
            catch (ArgumentException) { return "the program"; }
        }

        private static void KillTree(Process p)
        {
            try { if (!p.HasExited) p.Kill(entireProcessTree: true); }
            catch { /* already gone */ }
        }

        /// <summary>A Windows job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE holding one child; Dispose closes it (kills what is left).</summary>
        private sealed class KillOnCloseJob : IDisposable
        {
            private const int JobObjectExtendedLimitInformation = 9;
            private const uint KillOnJobClose = 0x2000;
            private IntPtr _handle;

            private KillOnCloseJob(IntPtr handle) { _handle = handle; }

            public static KillOnCloseJob? Assign(Process p, out string? error)
            {
                error = null;
                if (!OperatingSystem.IsWindows()) { error = "job objects need Windows"; return null; }
                var h = CreateJobObjectW(IntPtr.Zero, null);
                if (h == IntPtr.Zero) { error = "CreateJobObject failed, Win32 error " + Marshal.GetLastWin32Error().ToString(CultureInfo.InvariantCulture); return null; }
                var job = new KillOnCloseJob(h);
                var info = new JobExtendedLimits();
                info.Basic.LimitFlags = KillOnJobClose;
                if (!SetInformationJobObject(h, JobObjectExtendedLimitInformation, ref info, (uint)Marshal.SizeOf<JobExtendedLimits>()))
                {
                    error = "SetInformationJobObject failed, Win32 error " + Marshal.GetLastWin32Error().ToString(CultureInfo.InvariantCulture);
                    job.Dispose();
                    return null;
                }
                IntPtr processHandle;
                try { processHandle = p.Handle; }
                catch (Exception ex) when (ex is InvalidOperationException || ex is System.ComponentModel.Win32Exception)
                {
                    error = "no process handle (" + ex.GetType().Name + ")";
                    job.Dispose();
                    return null;
                }
                if (!AssignProcessToJobObject(h, processHandle))
                {
                    error = "AssignProcessToJobObject failed, Win32 error " + Marshal.GetLastWin32Error().ToString(CultureInfo.InvariantCulture);
                    job.Dispose();
                    return null;
                }
                return job;
            }

            public void Dispose()
            {
                var h = Interlocked.Exchange(ref _handle, IntPtr.Zero);
                if (h != IntPtr.Zero) CloseHandle(h);
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct JobBasicLimits
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
            private struct JobIoCounters
            {
                public ulong ReadOperationCount;
                public ulong WriteOperationCount;
                public ulong OtherOperationCount;
                public ulong ReadTransferCount;
                public ulong WriteTransferCount;
                public ulong OtherTransferCount;
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct JobExtendedLimits
            {
                public JobBasicLimits Basic;
                public JobIoCounters IoInfo;
                public UIntPtr ProcessMemoryLimit;
                public UIntPtr JobMemoryLimit;
                public UIntPtr PeakProcessMemoryUsed;
                public UIntPtr PeakJobMemoryUsed;
            }

            [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
            private static extern IntPtr CreateJobObjectW(IntPtr lpJobAttributes, string? lpName);

            [DllImport("kernel32.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            private static extern bool SetInformationJobObject(IntPtr hJob, int infoClass, ref JobExtendedLimits info, uint cbInfoLength);

            [DllImport("kernel32.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

            [DllImport("kernel32.dll", SetLastError = true)]
            [return: MarshalAs(UnmanagedType.Bool)]
            private static extern bool CloseHandle(IntPtr hObject);
        }

        private static async Task WriteStdInAsync(Process p, string? stdin)
        {
            try
            {
                if (!string.IsNullOrEmpty(stdin)) await p.StandardInput.WriteAsync(stdin).ConfigureAwait(false);
                await p.StandardInput.FlushAsync().ConfigureAwait(false);
            }
            catch (IOException) { /* the child exited / closed stdin early — its exit code and stderr say why */ }
            catch (ObjectDisposedException) { }
            finally
            {
                try { p.StandardInput.Close(); } catch { }
            }
        }

        /// <summary>Reads to EOF keeping at most <paramref name="cap"/> bytes; (bytes, over cap). Over the cap: signals <paramref name="onOver"/> (when given) and keeps draining.</summary>
        private static async Task<(byte[] Bytes, bool Over)> ReadCappedAsync(Stream s, int cap, CancellationTokenSource? onOver)
        {
            using var ms = new MemoryStream();
            var buffer = new byte[81920];
            var over = false;
            try
            {
                int n;
                while ((n = await s.ReadAsync(buffer.AsMemory(0, buffer.Length)).ConfigureAwait(false)) > 0)
                {
                    if (over) continue;
                    var room = cap - (int)ms.Length;
                    if (n > room)
                    {
                        ms.Write(buffer, 0, Math.Max(room, 0));
                        over = true;
                        try { onOver?.Cancel(); } catch (ObjectDisposedException) { }
                        continue;
                    }
                    ms.Write(buffer, 0, n);
                }
            }
            catch (IOException) { /* pipe broken by a kill */ }
            catch (ObjectDisposedException) { }
            return (ms.ToArray(), over);
        }
    }
}

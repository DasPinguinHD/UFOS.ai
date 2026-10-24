using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;

namespace UFOS.ai.Logging
{
    /// <summary>
    /// Zentrales, modul-unabhängiges Logging. Jedes Modul (LiveStreamAgent, HedgeFund, ...)
    /// ruft einfach <see cref="Info"/>, <see cref="Warn"/> oder <see cref="Error"/> mit seinem
    /// Modulnamen auf. Alle Einträge landen im selben Ringpuffer, in derselben Logdatei und
    /// lösen das <see cref="LogAdded"/>-Event aus, an das sich beliebige Log-Viewer (z.B. das
    /// bestehende Backend-Logs-Fenster) anhängen können.
    /// </summary>
    public static class AppLog
    {
        private const int MaxBufferedEntries = 5000;

        private static readonly object _sync = new();
        private static readonly LinkedList<LogEntry> _buffer = new();
        private static readonly string _logFilePath = ResolveLogFilePath();

        // File sink (2026-09-25, Copilot review "batch logging instead of forcing per-message
        // fsync"): lines are queued and appended in one batch about once per second instead of
        // opening the file and forcing it to disk (Flush(true)) for every single message — the
        // LiveStreamAgent logs every WebSocket message. A durable flush (fsync) only happens for
        // errors (so the lines before a crash are on disk), on process exit and on Flush().
        // Trade-off: after a hard kill up to ~1 s of Info/Warn lines can be missing.
        private const int FlushIntervalMs = 1000;
        private const int MaxPendingLines = 20000;
        private static readonly object _pendingSync = new();
        private static readonly object _fileSync = new();
        private static readonly List<string> _pending = new();
        private static readonly Timer _flushTimer = new(_ => FlushPending(durable: false), null, FlushIntervalMs, FlushIntervalMs);

        static AppLog()
        {
            try
            {
                AppDomain.CurrentDomain.ProcessExit += (_, _) => Flush();
                AppDomain.CurrentDomain.UnhandledException += (_, _) => Flush();
            }
            catch { }
        }

        /// <summary>Writes all queued lines to the log file and forces them to disk.</summary>
        public static void Flush() => FlushPending(durable: true);

        /// <summary>
        /// Wird für jeden neuen Log-Eintrag ausgelöst. Abonnenten (z.B. ein Log-Fenster)
        /// werden auf dem jeweils aufrufenden Thread benachrichtigt - UI-Code muss ggf.
        /// selbst auf den Dispatcher-Thread wechseln.
        /// </summary>
        public static event Action<LogEntry>? LogAdded;

        public static string LogFilePath => _logFilePath;

        public static void Info(string module, string message) => Write(LogLevel.Info, module, message, null);

        public static void Warn(string module, string message, Exception? ex = null) => Write(LogLevel.Warn, module, message, ex);

        public static void Error(string module, string message, Exception? ex = null) => Write(LogLevel.Error, module, message, ex);

        /// <summary>
        /// Gibt alle aktuell gepufferten Log-Einträge formatiert als Text zurück (älteste zuerst).
        /// </summary>
        public static string GetBufferedText()
        {
            lock (_sync)
            {
                var sb = new StringBuilder();
                foreach (var entry in _buffer)
                {
                    sb.AppendLine(entry.ToString());
                }
                return sb.ToString();
            }
        }

        public static IReadOnlyList<LogEntry> GetBufferedEntries()
        {
            lock (_sync)
            {
                return _buffer.ToList();
            }
        }

        private static void Write(LogLevel level, string module, string message, Exception? ex)
        {
            var entry = new LogEntry(DateTime.Now, level, module ?? "Unknown", message ?? string.Empty, ex?.ToString());

            lock (_sync)
            {
                _buffer.AddLast(entry);
                while (_buffer.Count > MaxBufferedEntries)
                {
                    _buffer.RemoveFirst();
                }
            }

            QueueForFile(entry);

            try { LogAdded?.Invoke(entry); } catch { /* never let a listener crash logging */ }
        }

        private static void QueueForFile(LogEntry entry)
        {
            lock (_pendingSync)
            {
                _pending.Add(entry + Environment.NewLine);
                if (_pending.Count > MaxPendingLines) _pending.RemoveRange(0, _pending.Count - MaxPendingLines);
            }
            if (entry.Level == LogLevel.Error) FlushPending(durable: true);
        }

        private static void FlushPending(bool durable)
        {
            // _fileSync keeps batches in order when the timer and an Error flush overlap.
            lock (_fileSync)
            {
                string batch;
                lock (_pendingSync)
                {
                    if (_pending.Count == 0) return;
                    batch = string.Concat(_pending);
                    _pending.Clear();
                }
                try
                {
                    var dir = Path.GetDirectoryName(_logFilePath);
                    if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                    {
                        Directory.CreateDirectory(dir);
                    }

                    var bytes = Encoding.UTF8.GetBytes(batch);
                    using var fs = new FileStream(_logFilePath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                    fs.Write(bytes, 0, bytes.Length);
                    if (durable) fs.Flush(true);
                }
                catch
                {
                    // swallow to avoid crashing the calling module because of logging failures
                }
            }
        }

        private static string ResolveLogFilePath()
        {
            try
            {
                var baseDir = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                return Path.Combine(baseDir, "UFOS.ai", "logs", "app.log");
            }
            catch
            {
                return Path.Combine(Path.GetTempPath(), "UFOS.ai_app.log");
            }
        }
    }
}

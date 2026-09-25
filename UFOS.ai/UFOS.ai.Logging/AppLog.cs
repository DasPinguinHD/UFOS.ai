using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

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

            TryAppendToFile(entry);

            try { LogAdded?.Invoke(entry); } catch { /* never let a listener crash logging */ }
        }

        private static void TryAppendToFile(LogEntry entry)
        {
            try
            {
                var dir = Path.GetDirectoryName(_logFilePath);
                if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                {
                    Directory.CreateDirectory(dir);
                }

                var bytes = Encoding.UTF8.GetBytes(entry + Environment.NewLine);
                using var fs = new FileStream(_logFilePath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                fs.Write(bytes, 0, bytes.Length);
                fs.Flush(true);
            }
            catch
            {
                // swallow to avoid crashing the calling module because of logging failures
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

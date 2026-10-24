using System;

namespace UFOS.ai.Logging
{
    public enum LogLevel
    {
        Info,
        Warn,
        Error
    }

    public sealed class LogEntry
    {
        public DateTime Timestamp { get; }
        public LogLevel Level { get; }
        public string Module { get; }
        public string Message { get; }
        public string? Exception { get; }

        public LogEntry(DateTime timestamp, LogLevel level, string module, string message, string? exception)
        {
            Timestamp = timestamp;
            Level = level;
            Module = module;
            Message = message;
            Exception = exception;
        }

        public override string ToString()
        {
            var line = $"[{Timestamp:yyyy-MM-dd HH:mm:ss.fff}] [{Level.ToString().ToUpperInvariant(),-5}] [{Module}] {Message}";
            if (!string.IsNullOrEmpty(Exception))
            {
                line += Environment.NewLine + Exception;
            }
            return line;
        }
    }
}

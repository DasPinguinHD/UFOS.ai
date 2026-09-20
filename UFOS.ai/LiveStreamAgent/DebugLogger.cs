using System;
using System.IO;
using System.Text;

namespace UFOS.ai
{
    internal static class DebugLogger
    {
        private static readonly string TempPath = Path.Combine(Path.GetTempPath(), "UFOS.ai_ws_debug.log");
        private static readonly string RepoPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory ?? ".", "UFOS.ai_ws_debug_repo.log");

        public static void Log(string text)
        {
            var line = DateTime.Now.ToString("o") + " " + text + Environment.NewLine;
            TryAppend(TempPath, line);
            TryAppend(RepoPath, line);
        }

        private static void TryAppend(string path, string data)
        {
            try
            {
                var bytes = Encoding.UTF8.GetBytes(data);
                using var fs = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                fs.Write(bytes, 0, bytes.Length);
                fs.Flush(true);
            }
            catch
            {
                // swallow to avoid crashing UI
            }
        }
    }
}


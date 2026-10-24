using System;
using System.IO;
using System.Text;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Append-only text log with a size cap (security audit S10-A2, L6 follow-up): the backend process logs the WPF modules
    /// write (Global Monitoring backend\data\logs\process.log, Hedge Fund backend\data\logs\process.log) are rotated at
    /// <see cref="DefaultMaxBytes"/> — process.log → process.log.1 → .2 → .3, the oldest beyond <see cref="DefaultKeep"/>
    /// is dropped — instead of growing without limit. Thread-safe (stdout and stderr callbacks write concurrently).
    ///
    /// A rotation that fails (e.g. another process still has the file open) is not hidden: the writer keeps appending to the
    /// current file and writes one "log rotation failed" line saying why; it tries again after another tenth of the cap.
    /// Linked into every module project (..\Shared\*.cs).
    /// </summary>
    internal sealed class RotatingLogFile : IDisposable
    {
        public const long DefaultMaxBytes = 5L * 1024 * 1024;
        public const int DefaultKeep = 3;

        private readonly object _gate = new();
        private readonly string _path;
        private readonly long _maxBytes;
        private readonly int _keep;
        private StreamWriter? _writer;
        private long _size;
        private bool _rotationFailureNoted;
        private long _rotateAt;

        /// <summary>Opens <paramref name="path"/> for appending (rotating it first when it is already over the cap).
        /// Throws like File.Open when the file can't be opened at all.</summary>
        public RotatingLogFile(string path, long maxBytes = DefaultMaxBytes, int keep = DefaultKeep)
        {
            if (maxBytes <= 0) throw new ArgumentOutOfRangeException(nameof(maxBytes));
            if (keep < 1) throw new ArgumentOutOfRangeException(nameof(keep));
            _path = path;
            _maxBytes = maxBytes;
            _keep = keep;
            _rotateAt = maxBytes;
            var problem = RotateIfLarge(path, maxBytes, keep);
            Open();
            if (problem != null) Note(problem);
        }

        public void WriteLine(string line)
        {
            lock (_gate)
            {
                if (_writer == null) return; // disposed
                if (_size >= _rotateAt)
                {
                    _writer.Dispose();
                    _writer = null;
                    string? problem = null;
                    try { Rotate(_path, _keep); }
                    catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException) { problem = ex.GetType().Name + ": " + ex.Message; }
                    Open();
                    if (problem != null)
                    {
                        Note(problem);
                        _rotateAt = _size + _maxBytes / 10; // not on every line while the file stays locked
                    }
                    else
                    {
                        _rotationFailureNoted = false;
                        _rotateAt = _maxBytes;
                    }
                }
                _writer!.WriteLine(line);
                _size += Encoding.UTF8.GetByteCount(line) + Environment.NewLine.Length;
            }
        }

        public void Dispose()
        {
            lock (_gate)
            {
                _writer?.Dispose();
                _writer = null;
            }
        }

        /// <summary>
        /// Rotates <paramref name="path"/> when it is at least <paramref name="maxBytes"/> long — for logs written by another
        /// process (the detached Hedge Fund backend's -LogFile) before that process is started. Returns null on success or
        /// when nothing had to be done, else the reason the rotation failed (the file is then left as it is).
        /// </summary>
        public static string? RotateIfLarge(string path, long maxBytes = DefaultMaxBytes, int keep = DefaultKeep)
        {
            try
            {
                var info = new FileInfo(path);
                if (!info.Exists || info.Length < maxBytes) return null;
                Rotate(path, keep);
                return null;
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                return ex.GetType().Name + ": " + ex.Message;
            }
        }

        private static void Rotate(string path, int keep)
        {
            var oldest = path + "." + keep;
            if (File.Exists(oldest)) File.Delete(oldest);
            for (var i = keep - 1; i >= 1; i--)
            {
                var source = path + "." + i;
                if (File.Exists(source)) File.Move(source, path + "." + (i + 1));
            }
            if (File.Exists(path)) File.Move(path, path + ".1");
        }

        private void Open()
        {
            _writer = new StreamWriter(_path, append: true, new UTF8Encoding(false)) { AutoFlush = true };
            _size = new FileInfo(_path).Length;
        }

        private void Note(string problem)
        {
            if (_rotationFailureNoted || _writer == null) return;
            _rotationFailureNoted = true;
            var line = $"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} log rotation failed ({problem}) — continuing in this file ---";
            _writer.WriteLine(line);
            _size += Encoding.UTF8.GetByteCount(line) + Environment.NewLine.Length;
        }
    }
}

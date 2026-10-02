using System;
using System.IO;
using System.Net.Http;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Client side of the backends' access control (security-by-default rule,
    /// CLAUDE.md; server side: each backend's api_auth.py).
    ///
    /// Every Python backend creates a fresh random session token at each start
    /// and writes it to &lt;backend&gt;/data/api-token (git-ignored). All HTTP
    /// endpoints except /health and /version, and every WebSocket connection,
    /// require it in the <see cref="HeaderName"/> header — so a website open in
    /// the user's browser (or anything else without access to the user's
    /// files) can't call the local APIs, trigger paid LLM calls or stop a
    /// backend. The token is read from the file on every use, so a restarted
    /// backend (new token) just works, and it is never logged.
    ///
    /// Linked into the main app and every module (internal, like AiContent).
    /// </summary>
    internal static class BackendAuth
    {
        public const string HeaderName = "X-UFOS-Token";
        public const string TokenFileName = "api-token";

        /// <summary>&lt;backendDir&gt;/data/api-token.</summary>
        public static string TokenPath(string backendDir) => Path.Combine(backendDir, "data", TokenFileName);

        /// <summary>The running backend's current token, or null (backend not started yet / no folder).</summary>
        public static string? ReadToken(string? backendDir)
        {
            if (string.IsNullOrEmpty(backendDir)) return null;
            try
            {
                var path = TokenPath(backendDir);
                if (!File.Exists(path)) return null;
                // FileShare.ReadWrite|Delete: the backend replaces the file atomically at start.
                using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                using var reader = new StreamReader(fs);
                var token = reader.ReadToEnd().Trim();
                return token.Length == 0 ? null : token;
            }
            catch
            {
                return null;
            }
        }

        /// <summary>Adds the token header (if a token is available) to an HttpClient.</summary>
        public static HttpClient Authorize(HttpClient client, string? backendDir)
        {
            var token = ReadToken(backendDir);
            client.DefaultRequestHeaders.Remove(HeaderName);
            if (token != null) client.DefaultRequestHeaders.TryAddWithoutValidation(HeaderName, token);
            return client;
        }

        /// <summary>New HttpClient with the given timeout and the backend's token header.</summary>
        public static HttpClient CreateClient(string? backendDir, TimeSpan timeout)
            => Authorize(new HttpClient { Timeout = timeout }, backendDir);

        /// <summary>
        /// Walks up from the app's base directory (max. 8 levels) to find
        /// &lt;relative path&gt; containing main.py — works for the main app and
        /// a module's standalone exe alike. Null when not found.
        /// </summary>
        public static string? FindBackendDir(params string[] relativeDir)
        {
            try
            {
                var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
                for (var depth = 0; dir != null && depth < 8; depth++, dir = dir.Parent)
                {
                    var parts = new string[relativeDir.Length + 1];
                    parts[0] = dir.FullName;
                    Array.Copy(relativeDir, 0, parts, 1, relativeDir.Length);
                    var candidate = Path.Combine(parts);
                    if (File.Exists(Path.Combine(candidate, "main.py"))) return candidate;
                }
            }
            catch { }
            return null;
        }
    }
}

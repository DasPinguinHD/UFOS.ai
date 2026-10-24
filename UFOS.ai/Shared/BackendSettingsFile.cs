using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace UFOS.ai.Shared
{
    /// <summary>A problem with a module's settings.json; the message is user-facing (never contains a secret — none are stored there).</summary>
    internal sealed class BackendSettingsException : Exception
    {
        public BackendSettingsException(string message) : base(message) { }
    }

    /// <summary>
    /// The WPF side's access to a backend's non-secret settings file &lt;backend&gt;\data\settings.json (S10-E, ENV_API §4/§4a):
    /// read the current JSON object (keeping every other key), set values, write a temp file in the same folder and replace
    /// the file atomically. The backends only READ it (and validate it strictly at start). Used by the one-time .env import
    /// (Hedge Fund ⚙ Keys &amp; data) and the LiveStream Agent's YouTube URL field. Never for keys (Credential Manager only).
    /// Linked into every module project (..\Shared\*.cs).
    /// </summary>
    internal static class BackendSettingsFile
    {
        private const int MaxFileBytes = 256 * 1024;

        public static string PathFor(string backendDir) => Path.Combine(backendDir, "data", "settings.json");

        /// <summary>The current object; an empty one when the file does not exist. Throws BackendSettingsException otherwise.</summary>
        public static JsonObject Read(string path)
        {
            if (!File.Exists(path)) return new JsonObject();
            string text;
            try
            {
                if (new FileInfo(path).Length > MaxFileBytes) throw new BackendSettingsException(Name(path) + " is larger than 256 KB.");
                text = File.ReadAllText(path, Encoding.UTF8);
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                throw new BackendSettingsException(Name(path) + " could not be read (" + ex.GetType().Name + ").");
            }
            JsonNode? node;
            try { node = JsonNode.Parse(text); }
            catch (JsonException) { throw new BackendSettingsException(Name(path) + " is not valid JSON — fix or remove it."); }
            return node as JsonObject ?? throw new BackendSettingsException(Name(path) + ": the top level must be a JSON object.");
        }

        /// <summary>Sets the given keys (other keys unchanged) and replaces the file atomically. Throws BackendSettingsException.</summary>
        public static void SetValues(string path, IReadOnlyDictionary<string, JsonNode?> values)
        {
            var obj = Read(path);
            foreach (var pair in values)
            {
                if (LooksSecret(pair.Key))
                    throw new BackendSettingsException(Name(path) + ": " + pair.Key + " looks like a key — keys are stored only in the Windows Credential Manager.");
                obj[pair.Key] = pair.Value?.DeepClone();
            }
            var dir = Path.GetDirectoryName(path)!;
            var temp = Path.Combine(dir, "settings.json." + Guid.NewGuid().ToString("N") + ".tmp");
            try
            {
                Directory.CreateDirectory(dir);
                File.WriteAllText(temp, obj.ToJsonString(new JsonSerializerOptions { WriteIndented = true }) + "\n", new UTF8Encoding(false));
                File.Move(temp, path, overwrite: true);
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                try { if (File.Exists(temp)) File.Delete(temp); } catch { }
                throw new BackendSettingsException(Name(path) + " could not be written (" + ex.GetType().Name + ").");
            }
        }

        /// <summary>Same rule as the backends (ENV_API §4): a setting whose name contains key / secret / token / password is refused.</summary>
        public static bool LooksSecret(string name)
        {
            var lower = name.ToLowerInvariant();
            return lower.Contains("key") || lower.Contains("secret") || lower.Contains("token") || lower.Contains("passw");
        }

        // ---- LiveStream Agent youtube_url (ENV_API §4a: same rule as the backend) ----

        public const string YouTubeUrlKey = "youtube_url";
        private static readonly string[] YouTubeHosts = { "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be" };

        /// <summary>Null when valid: https, host youtube.com / www.youtube.com / m.youtube.com / youtu.be, no user info, ≤ 500 characters.</summary>
        public static string? ValidateYouTubeUrl(string? url)
        {
            var value = (url ?? "").Trim();
            if (value.Length == 0) return "Enter the YouTube URL of the live stream.";
            if (value.Length > 500) return "The URL is longer than 500 characters.";
            if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps)
                return "The URL must start with https://.";
            if (!string.IsNullOrEmpty(uri.UserInfo)) return "The URL must not contain a user name or password.";
            if (Array.IndexOf(YouTubeHosts, uri.Host.ToLowerInvariant()) < 0)
                return "Only YouTube URLs (youtube.com, www.youtube.com, m.youtube.com, youtu.be) are accepted.";
            return null;
        }

        private static string Name(string path)
        {
            var parent = Path.GetFileName(Path.GetDirectoryName(Path.GetDirectoryName(path)) ?? "");
            return (string.IsNullOrEmpty(parent) ? "" : parent + "\\") + "data\\settings.json";
        }
    }
}

using System;
using System.IO;
using System.Text.Json;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Non-secret settings of the main window app (S10-E, user decision 2026-10-24: no .env). File
    /// %LOCALAPPDATA%\UFOS.ai\app-settings.json — optional; a missing file or key means the code default. Same strict rules as
    /// the backends' data/settings.json (ENV_API §4): an unknown key, a wrong type or an invalid value is an error with the
    /// message "app-settings.json: &lt;key&gt;: &lt;reason&gt;" — never silently ignored. Keys never go here (Credential Manager only):
    /// a key whose name contains key / secret / token / password is refused.
    /// Read on every use (like the keys), so an edit applies without a restart. Written only by the one-time .env import
    /// (Hedge Fund ⚙ Keys & data) — which is why it lives in Shared (linked into every module project).
    ///
    ///   { "openrouter_base_url": "https://openrouter.ai/api/v1", "openrouter_model": "deepseek/deepseek-chat-v3-0324" }
    /// </summary>
    internal static class MainAppSettings
    {
        public const string OpenRouterBaseUrlKey = "openrouter_base_url";
        public const string OpenRouterModelKey = "openrouter_model";
        public const string DefaultOpenRouterBaseUrl = "https://openrouter.ai/api/v1";
        public const string DefaultOpenRouterModel = "deepseek/deepseek-chat-v3-0324";
        private const int MaxFileBytes = 64 * 1024;

        public static string FilePath => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai", "app-settings.json");

        /// <summary>The known keys with their code defaults (the import writes only these, and only when different).</summary>
        public static readonly (string Key, string Default)[] Known =
        {
            (OpenRouterBaseUrlKey, DefaultOpenRouterBaseUrl),
            (OpenRouterModelKey, DefaultOpenRouterModel),
        };

        /// <summary>The current values (defaults for missing keys). Throws <see cref="MainAppSettingsException"/> on a bad file.</summary>
        public static (string OpenRouterBaseUrl, string OpenRouterModel) Load()
        {
            string baseUrl = DefaultOpenRouterBaseUrl, model = DefaultOpenRouterModel;
            var path = FilePath;
            if (!File.Exists(path)) return (baseUrl, model);

            string text;
            try
            {
                var info = new FileInfo(path);
                if (info.Length > MaxFileBytes) throw new MainAppSettingsException($"app-settings.json: the file is larger than {MaxFileBytes / 1024} KB.");
                text = File.ReadAllText(path);
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
            {
                throw new MainAppSettingsException("app-settings.json could not be read (" + ex.GetType().Name + ").");
            }

            JsonDocument doc;
            try { doc = JsonDocument.Parse(text); }
            catch (JsonException) { throw new MainAppSettingsException("app-settings.json is not valid JSON."); }
            using (doc)
            {
                if (doc.RootElement.ValueKind != JsonValueKind.Object) throw new MainAppSettingsException("app-settings.json: the top level must be a JSON object.");
                foreach (var prop in doc.RootElement.EnumerateObject())
                {
                    var name = prop.Name;
                    var lower = name.ToLowerInvariant();
                    if (lower.Contains("key") || lower.Contains("secret") || lower.Contains("token") || lower.Contains("password"))
                        throw new MainAppSettingsException($"app-settings.json: {name}: keys and secrets are never stored here — set them in ⚙ Keys & data and remove this entry.");
                    if (name != OpenRouterBaseUrlKey && name != OpenRouterModelKey)
                        throw new MainAppSettingsException($"app-settings.json: {name}: unknown setting.");
                    if (prop.Value.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(prop.Value.GetString()))
                        throw new MainAppSettingsException($"app-settings.json: {name}: must be a non-empty string.");
                    var value = prop.Value.GetString()!.Trim();
                    if (value.Length > 500) throw new MainAppSettingsException($"app-settings.json: {name}: longer than 500 characters.");
                    if (name == OpenRouterBaseUrlKey) baseUrl = value; else model = value;
                }
            }
            return (baseUrl, model);
        }
    }

    /// <summary>A problem with app-settings.json; the message is user-facing.</summary>
    internal sealed class MainAppSettingsException : Exception
    {
        public MainAppSettingsException(string message) : base(message) { }
    }
}

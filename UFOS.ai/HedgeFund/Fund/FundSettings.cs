using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace UFOS.ai.HedgeFund.Fund
{
    /// <summary>
    /// Small per-user settings of the Hedge Fund module, stored as JSON under
    /// %LOCALAPPDATA%\UFOS.ai (never in the repo, no secrets):
    /// - hedgefund-settings.json: {"mute_notifications": bool} — Windows toasts on/off.
    /// - hedgefund-backend.json:  {"lifetime": "app" | "detached"} — SPEC §14. "app"
    ///   (default) binds the backend to the UFOS.ai process (Job Object); "detached"
    ///   (experimental) keeps the engine running after UFOS.ai closes. Read by
    ///   Newsroom/NewsroomBackend.cs at the next backend start.
    /// Unknown keys in the files are preserved on write. Every read/write is
    /// best-effort: a broken or locked file falls back to the defaults.
    /// </summary>
    internal static class FundSettings
    {
        public const string LifetimeApp = "app";
        public const string LifetimeDetached = "detached";

        private static string Folder =>
            System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai");

        public static string UiSettingsPath => System.IO.Path.Combine(Folder, "hedgefund-settings.json");
        public static string BackendSettingsPath => System.IO.Path.Combine(Folder, "hedgefund-backend.json");

        // ---------- notifications ----------

        public static bool MuteNotifications
        {
            get => ReadBool(UiSettingsPath, "mute_notifications", false);
            set => Write(UiSettingsPath, "mute_notifications", JsonValue.Create(value));
        }

        // ---------- backend lifetime ----------

        /// <summary>"app" (default) or "detached".</summary>
        public static string BackendLifetime
        {
            get
            {
                var text = ReadString(BackendSettingsPath, "lifetime");
                return string.Equals(text, LifetimeDetached, StringComparison.OrdinalIgnoreCase) ? LifetimeDetached : LifetimeApp;
            }
            set => Write(BackendSettingsPath, "lifetime",
                JsonValue.Create(string.Equals(value, LifetimeDetached, StringComparison.OrdinalIgnoreCase) ? LifetimeDetached : LifetimeApp));
        }

        public static bool IsDetached => BackendLifetime == LifetimeDetached;

        // ---------- plumbing ----------

        private static JsonObject? ReadObject(string path)
        {
            try
            {
                if (!File.Exists(path)) return null;
                return JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
            }
            catch
            {
                return null;
            }
        }

        private static bool ReadBool(string path, string key, bool fallback)
        {
            try
            {
                var node = ReadObject(path)?[key];
                if (node is JsonValue v && v.TryGetValue<bool>(out var b)) return b;
            }
            catch { }
            return fallback;
        }

        private static string? ReadString(string path, string key)
        {
            try
            {
                var node = ReadObject(path)?[key];
                if (node is JsonValue v && v.TryGetValue<string>(out var s)) return s;
            }
            catch { }
            return null;
        }

        private static void Write(string path, string key, JsonNode? value)
        {
            try
            {
                Directory.CreateDirectory(Folder);
                var obj = ReadObject(path) ?? new JsonObject();
                obj[key] = value;
                var tmp = path + ".tmp";
                File.WriteAllText(tmp, obj.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
                File.Move(tmp, path, overwrite: true);
            }
            catch
            {
                // Best effort — a settings file that can't be written must never break the UI.
            }
        }
    }
}

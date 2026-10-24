using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace UFOS.ai.HedgeFund.Fund
{
    /// <summary>
    /// Small per-user settings of the Hedge Fund module, stored as JSON under
    /// %LOCALAPPDATA%\UFOS.ai (never in the repo, no secrets):
    /// - hedgefund-settings.json: {"mute_notifications": bool, "monitoring_toasts": bool} — Windows toasts on/off
    ///   (all / the stage-9 monitoring alerts).
    /// - hedgefund-backend.json:  {"lifetime": "app" | "detached", "start_with_app": bool} — SPEC §14 / strategy_platform SPEC §20.8. "app"
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

        /// <summary>
        /// Strategy Platform stage 9 (SPEC §19.13): Windows toasts for the monitoring alerts (decay pause, reconciliation halt,
        /// kill switch, daily loss limit, broker disconnect, incubation finished …) on/off — default on. "Mute notifications"
        /// above still mutes every Hedge Fund toast. Key "monitoring_toasts" in hedgefund-settings.json.
        /// </summary>
        public static bool MonitoringToasts
        {
            get => ReadBool(UiSettingsPath, "monitoring_toasts", true);
            set => Write(UiSettingsPath, "monitoring_toasts", JsonValue.Create(value));
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

        /// <summary>
        /// Stage 9b (SPEC §20.8): "Start the Hedge Fund backend together with UFOS.ai" — OFF by default. When on, the MainWindow
        /// starts the backend in the background at app start (non-blocking, through HedgeFundBackendHost / NewsroomBackend —
        /// exactly as opening the Hedge Fund window would). Key "start_with_app" in hedgefund-backend.json.
        /// </summary>
        public static bool StartWithApp
        {
            get => ReadStartWithApp();
            set => Write(BackendSettingsPath, "start_with_app", JsonValue.Create(value));
        }

        private static bool _startWithAppProblemLogged;

        /// <summary>
        /// "start_with_app" — OFF when the file is missing or the key is absent; a file that cannot be read / parsed or a value
        /// that is not true/false also gives OFF, but is logged once (AppLog, module "HedgeFund") with the reason — never silent.
        /// </summary>
        private static bool ReadStartWithApp()
        {
            string? problem = null;
            var result = false;
            try
            {
                if (File.Exists(BackendSettingsPath))
                {
                    var node = JsonNode.Parse(File.ReadAllText(BackendSettingsPath));
                    if (node is not JsonObject obj) problem = "the file is not a JSON object";
                    else if (obj["start_with_app"] is JsonNode v)
                    {
                        if (v is JsonValue jv && jv.TryGetValue<bool>(out var b)) result = b;
                        else problem = "\"start_with_app\" is not true/false";
                    }
                }
            }
            catch (Exception ex)
            {
                problem = ex.GetType().Name + ": " + ex.Message;
            }
            if (problem != null && !_startWithAppProblemLogged)
            {
                _startWithAppProblemLogged = true;
                try { UFOS.ai.Logging.AppLog.Warn("HedgeFund", "hedgefund-backend.json could not be read (" + problem + ") — \"Start the Hedge Fund backend together with UFOS.ai\" stays OFF."); } catch { }
            }
            return result;
        }

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

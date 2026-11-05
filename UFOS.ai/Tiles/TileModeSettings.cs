using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using UFOS.ai.ModuleTiles;

namespace UFOS.ai.Tiles
{
    /// <summary>
    /// The preview mode per module tile, key <c>"tile_modes"</c> of the main app's non-secret settings
    /// %LOCALAPPDATA%\UFOS.ai\app-settings.json (Shared/MainAppSettings.cs accepts the key and leaves its validation here):
    /// <code>{"tile_modes": {"hedgefund": "live", "global-monitoring": "last_view", "system-logs": "illustration"}}</code>
    /// Missing file / key / module → Live (the user's default). Strict like the other keys (TILES_API §10.8): an unknown module
    /// id or value is a readable problem — the tile shows it and uses Live until the entry is fixed; it is never dropped silently.
    /// Writes keep every other key of the file unchanged (atomic temp + move).
    /// </summary>
    internal static class TileModeSettings
    {
        public const string Key = "tile_modes";
        private const int MaxFileBytes = 64 * 1024;

        public static string ToValue(TileMode mode) => mode switch
        {
            TileMode.LastView => "last_view",
            TileMode.Illustration => "illustration",
            _ => "live",
        };

        /// <summary>The stored modes of the known modules plus one problem text per module whose entry is invalid
        /// (and one for the whole file under the key "*").</summary>
        public static (Dictionary<string, TileMode> Modes, Dictionary<string, string> Problems) Load(IReadOnlyCollection<string> knownIds)
        {
            var modes = new Dictionary<string, TileMode>(StringComparer.Ordinal);
            var problems = new Dictionary<string, string>(StringComparer.Ordinal);
            JsonObject? root;
            try
            {
                root = ReadRoot();
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is JsonException || ex is InvalidDataException)
            {
                problems["*"] = "app-settings.json: " + ex.Message;
                return (modes, problems);
            }
            if (root == null || !root.TryGetPropertyValue(Key, out var node) || node == null) return (modes, problems);
            if (node is not JsonObject obj)
            {
                problems["*"] = "app-settings.json: tile_modes: must be an object {module id: mode}";
                return (modes, problems);
            }
            foreach (var (id, value) in obj)
            {
                var text = value is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
                if (!System.Linq.Enumerable.Contains(knownIds, id))
                {
                    problems["*"] = "app-settings.json: tile_modes: " + id + ": unknown module";
                    continue;
                }
                switch (text)
                {
                    case "live": modes[id] = TileMode.Live; break;
                    case "last_view": modes[id] = TileMode.LastView; break;
                    case "illustration": modes[id] = TileMode.Illustration; break;
                    default:
                        problems[id] = "app-settings.json: tile_modes: " + id + ": must be live, last_view or illustration";
                        break;
                }
            }
            return (modes, problems);
        }

        /// <summary>Stores the mode of one module (the other keys and modules stay). Throws with a readable message on failure.</summary>
        public static void Save(string moduleId, TileMode mode)
        {
            TilePaths.CheckId(moduleId);
            var root = ReadRoot() ?? new JsonObject();
            if (!root.TryGetPropertyValue(Key, out var node) || node is not JsonObject obj)
            {
                if (node != null) throw new InvalidDataException("tile_modes is not an object — fix app-settings.json first");
                obj = new JsonObject();
                root[Key] = obj;
            }
            obj[moduleId] = ToValue(mode);
            var path = FilePath;
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, root.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            File.Move(tmp, path, overwrite: true);
        }

        private static string FilePath => UFOS.ai.Shared.MainAppSettings.FilePath;

        private static JsonObject? ReadRoot()
        {
            var path = FilePath;
            if (!File.Exists(path)) return null;
            if (new FileInfo(path).Length > MaxFileBytes) throw new InvalidDataException("the file is larger than 64 KB");
            var node = JsonNode.Parse(File.ReadAllText(path));
            return node as JsonObject ?? throw new InvalidDataException("the top level must be a JSON object");
        }
    }
}

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Media;

namespace UFOS.ai.ModuleTiles
{
    /// <summary>
    /// Design-system brushes for code-built tile content (Styles/DesignSystem.xaml keys, merged into the main app's resources).
    /// A missing key is an error with the key's name, never a stand-in colour.
    /// </summary>
    public static class TileBrushes
    {
        public static Brush Get(string key)
        {
            if (Application.Current?.TryFindResource(key) is Brush b) return b;
            throw new InvalidOperationException("design-system brush '" + key + "' not found");
        }

        public static string KeyFor(TileTone tone) => tone switch
        {
            TileTone.Positive => "PositiveGreenBrush",
            TileTone.Negative => "AccentRedBrush",
            TileTone.Caution => "CautionBrush",
            TileTone.Ai => "AiAccentBrush",
            _ => "PrimaryForegroundBrush",
        };
    }

    /// <summary>
    /// Local storage of the previews — ONLY under %LOCALAPPDATA%\UFOS.ai\tile-previews\ (never the repo):
    ///   live\&lt;moduleId&gt;.json       last fresh figures (no secrets — figures only)
    ///   last-view\&lt;moduleId&gt;.png   the BLURRED picture (TileBlur) + last-view\&lt;moduleId&gt;.json (metadata)
    /// All writes are atomic (temp file + move).
    /// </summary>
    public static class TilePaths
    {
        private static readonly Regex IdPattern = new("^[a-z][a-z0-9-]{1,40}$", RegexOptions.CultureInvariant);

        public static string Root => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai", "tile-previews");

        public static string LiveFile(string moduleId) => Path.Combine(Root, "live", CheckId(moduleId) + ".json");
        public static string LastViewPng(string moduleId) => Path.Combine(Root, "last-view", CheckId(moduleId) + ".png");
        public static string LastViewMeta(string moduleId) => Path.Combine(Root, "last-view", CheckId(moduleId) + ".json");

        /// <summary>Module ids become file names: lower-case letters, digits and '-' only.</summary>
        public static string CheckId(string moduleId)
        {
            if (moduleId == null || !IdPattern.IsMatch(moduleId)) throw new ArgumentException("invalid module id '" + moduleId + "'", nameof(moduleId));
            return moduleId;
        }

        internal static void WriteAtomic(string path, byte[] bytes)
        {
            var dir = Path.GetDirectoryName(path)!;
            Directory.CreateDirectory(dir);
            var tmp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            File.WriteAllBytes(tmp, bytes);
            try
            {
                File.Move(tmp, path, overwrite: true);
            }
            catch
            {
                try { File.Delete(tmp); } catch { /* the temp file only — the error below is what counts */ }
                throw;
            }
        }
    }

    /// <summary>
    /// The last FRESH figures of a module, shown while its backend is off ("backend off — as of HH:MM"; user-accepted behaviour
    /// of the tile, TILES_TASK 2026-10-24). Format (schema 1):
    /// <code>{"schema": 1, "module": "global-monitoring", "as_of": "2026-10-24T17:05:00Z",
    ///  "figures": [{"label": "...", "value": "...", "tone": "neutral", "detail": null, "ai": false}],
    ///  "spark": [1.0, 2.0], "dots": [{"label": "flight", "count": 18, "brush": "AccentBlueBrush"}]}</code>
    /// Sandbox figures are never stored (the host checks <see cref="TileLiveResult.Sandbox"/>).
    /// </summary>
    public static class TileLiveStore
    {
        private const int MaxFileBytes = 64 * 1024;

        public static void Save(string moduleId, TileLiveData data)
        {
            using var ms = new MemoryStream();
            using (var w = new Utf8JsonWriter(ms, new JsonWriterOptions { Indented = true }))
            {
                w.WriteStartObject();
                w.WriteNumber("schema", 1);
                w.WriteString("module", moduleId);
                w.WriteString("as_of", data.AsOf.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture));
                w.WriteStartArray("figures");
                foreach (var f in data.Figures)
                {
                    w.WriteStartObject();
                    w.WriteString("label", f.Label);
                    w.WriteString("value", f.Value);
                    w.WriteString("tone", f.Tone.ToString().ToLowerInvariant());
                    if (f.Detail == null) w.WriteNull("detail"); else w.WriteString("detail", f.Detail);
                    w.WriteBoolean("ai", f.AiGenerated);
                    w.WriteEndObject();
                }
                w.WriteEndArray();
                if (data.Spark != null)
                {
                    w.WriteStartArray("spark");
                    foreach (var p in data.Spark) w.WriteNumberValue(double.IsFinite(p) ? p : 0);
                    w.WriteEndArray();
                }
                if (data.Dots != null)
                {
                    w.WriteStartArray("dots");
                    foreach (var d in data.Dots)
                    {
                        w.WriteStartObject();
                        w.WriteString("label", d.Label);
                        w.WriteNumber("count", d.Count);
                        w.WriteString("brush", d.BrushKey);
                        w.WriteEndObject();
                    }
                    w.WriteEndArray();
                }
                w.WriteEndObject();
            }
            TilePaths.WriteAtomic(TilePaths.LiveFile(moduleId), ms.ToArray());
        }

        /// <summary>The stored figures, null when there are none. A damaged file is an exception with a readable message.</summary>
        public static TileLiveData? Load(string moduleId)
        {
            var path = TilePaths.LiveFile(moduleId);
            if (!File.Exists(path)) return null;
            var info = new FileInfo(path);
            if (info.Length > MaxFileBytes) throw new InvalidDataException("stored figures larger than 64 KB (" + path + ")");
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllBytes(path));
                var root = doc.RootElement;
                if (root.GetProperty("schema").GetInt32() != 1) throw new InvalidDataException("unknown schema");
                var asOf = DateTimeOffset.Parse(root.GetProperty("as_of").GetString()!, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal);
                var figures = new List<TileFigure>();
                foreach (var f in root.GetProperty("figures").EnumerateArray())
                {
                    var tone = Enum.TryParse<TileTone>(f.GetProperty("tone").GetString(), ignoreCase: true, out var t) ? t
                        : throw new InvalidDataException("unknown tone");
                    var detail = f.TryGetProperty("detail", out var d) && d.ValueKind == JsonValueKind.String ? d.GetString() : null;
                    var ai = f.TryGetProperty("ai", out var a) && a.ValueKind == JsonValueKind.True;
                    figures.Add(new TileFigure(f.GetProperty("label").GetString() ?? "", f.GetProperty("value").GetString() ?? "", tone, detail, ai));
                }
                List<double>? spark = null;
                if (root.TryGetProperty("spark", out var s) && s.ValueKind == JsonValueKind.Array)
                {
                    spark = new List<double>();
                    foreach (var p in s.EnumerateArray()) spark.Add(p.GetDouble());
                }
                List<TileDot>? dots = null;
                if (root.TryGetProperty("dots", out var ds) && ds.ValueKind == JsonValueKind.Array)
                {
                    dots = new List<TileDot>();
                    foreach (var d2 in ds.EnumerateArray())
                        dots.Add(new TileDot(d2.GetProperty("label").GetString() ?? "", d2.GetProperty("count").GetInt32(), d2.GetProperty("brush").GetString() ?? ""));
                }
                return new TileLiveData(asOf, figures, spark, dots);
            }
            catch (Exception ex) when (ex is JsonException || ex is KeyNotFoundException || ex is FormatException || ex is InvalidOperationException)
            {
                throw new InvalidDataException("stored figures unreadable (" + ex.GetType().Name + ") — " + path, ex);
            }
        }
    }

    /// <summary>Metadata of the stored blurred picture.</summary>
    public sealed record TileLastViewInfo(string PngPath, DateTimeOffset TakenAt, int Width, int Height);

    /// <summary>The stored (blurred) last-view pictures.</summary>
    public static class TileLastViewStore
    {
        /// <summary>The blur recipe recorded with every picture (TileBlur).</summary>
        public const string BlurRecipe = "area-1/10+box5x5x2";

        /// <summary>Writes an ALREADY BLURRED PNG (see <see cref="TileBlur.BlurToPng"/> — the only producer) + its metadata.</summary>
        internal static void Save(string moduleId, TileBlur.BlurredPng png, DateTimeOffset takenAt)
        {
            TilePaths.WriteAtomic(TilePaths.LastViewPng(moduleId), png.Bytes);
            var meta = "{\"schema\": 1, \"taken_at\": \"" + takenAt.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture)
                       + "\", \"width\": " + png.Width.ToString(CultureInfo.InvariantCulture) + ", \"height\": " + png.Height.ToString(CultureInfo.InvariantCulture)
                       + ", \"blur\": \"" + BlurRecipe + "\"}";
            TilePaths.WriteAtomic(TilePaths.LastViewMeta(moduleId), Encoding.UTF8.GetBytes(meta));
        }

        /// <summary>The stored picture, or null when there is none. Damaged metadata is an exception with a readable message.</summary>
        public static TileLastViewInfo? Load(string moduleId)
        {
            var png = TilePaths.LastViewPng(moduleId);
            var metaPath = TilePaths.LastViewMeta(moduleId);
            if (!File.Exists(png) || !File.Exists(metaPath)) return null;
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllBytes(metaPath));
                var root = doc.RootElement;
                if (root.GetProperty("blur").GetString() != BlurRecipe) throw new InvalidDataException("the picture was not stored with the current blur");
                var taken = DateTimeOffset.Parse(root.GetProperty("taken_at").GetString()!, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal);
                return new TileLastViewInfo(png, taken, root.GetProperty("width").GetInt32(), root.GetProperty("height").GetInt32());
            }
            catch (Exception ex) when (ex is JsonException || ex is KeyNotFoundException || ex is FormatException || ex is InvalidOperationException)
            {
                throw new InvalidDataException("stored picture metadata unreadable (" + ex.GetType().Name + ") — " + metaPath, ex);
            }
        }

        /// <summary>"Delete last view": removes the picture and its metadata (the user confirmed in the menu).</summary>
        public static void Delete(string moduleId)
        {
            foreach (var p in new[] { TilePaths.LastViewPng(moduleId), TilePaths.LastViewMeta(moduleId) })
                if (File.Exists(p)) File.Delete(p);
        }
    }
}

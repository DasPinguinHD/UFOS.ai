using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using UFOS.ai.ModuleTiles;
using UFOS.ai.Shared;

namespace UFOS.ai.GlobalMonitoring.Tiles
{
    /// <summary>
    /// Global Monitoring tile preview (TILES_API §3 + §10.5; endpoint contract scratchpad/mr/GM_SUMMARY_API.md by Agent H):
    /// Live = <c>GET /events/summary?hours=24</c> — a pure read of the in-memory event store (no collector wake-up, no network,
    /// no AI call), token-protected. Events of the last 24 h per category as coloured dots, the total, and the feed status
    /// (a failing feed = Caution with its message). Backend off (no /health) → the host shows the stored figures.
    /// Last view: the Global Monitoring window; the globe (HwndHost WebView2) is captured by TileCapture via CapturePreviewAsync.
    /// </summary>
    public sealed class GlobalMonitoringTilePreview : IModuleTilePreview
    {
        public const int Port = 8768;
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private readonly LoopbackReader _reader = new(Port, () => BackendAuth.ReadToken(BackendAuth.FindBackendDir("Global Monitoring", "backend")), "Global Monitoring backend");

        public string ModuleId => "global-monitoring";
        public string DisplayName => "Global Monitoring";
        public string? LiveUnavailableReason => null;
        public string? LastViewUnavailableReason => null;

        /// <summary>Design-system brush per category (GM_SUMMARY_API: geopolitics, disaster, flight, markets; unknown ones appended).</summary>
        public static string BrushFor(string category) => category switch
        {
            "geopolitics" => "AccentRedBrush",
            "disaster" => "AccentAmberBrush",
            "flight" => "AccentBlueBrush",
            "markets" => "PositiveGreenBrush",
            _ => "MutedForegroundBrush",
        };

        public async Task<TileLiveResult> ReadLiveAsync(CancellationToken ct)
        {
            var health = await _reader.HealthAsync(ct).ConfigureAwait(false);
            if (!health.Running) return TileLiveResult.BackendOff();
            var r = await _reader.GetJsonAsync("/events/summary?hours=24", ct).ConfigureAwait(false);
            if (!r.Ok) return TileLiveResult.Unavailable(r.Problem ?? "no answer");
            try
            {
                return TileLiveResult.Fresh(Parse(r.Body!));
            }
            catch (Exception ex) when (ex is JsonException || ex is KeyNotFoundException || ex is InvalidOperationException || ex is FormatException)
            {
                return TileLiveResult.Unavailable("unexpected answer from the Global Monitoring backend (" + ex.GetType().Name + ")");
            }
        }

        /// <summary>Parses the schema-1 summary (public for the offline test of the mapping).</summary>
        public static TileLiveData Parse(string json)
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.GetProperty("schema").GetInt32() != 1) throw new FormatException("schema " + root.GetProperty("schema").GetRawText());
            var asOf = DateTimeOffset.Parse(root.GetProperty("as_of").GetString()!, Inv, DateTimeStyles.AssumeUniversal);
            var total = root.GetProperty("total").GetInt32();
            var undated = root.TryGetProperty("undated", out var u) && u.ValueKind == JsonValueKind.Number ? u.GetInt32() : 0;
            var dots = root.GetProperty("categories").EnumerateArray()
                .Select(c => new TileDot(c.GetProperty("category").GetString() ?? "?", c.GetProperty("count").GetInt32(), BrushFor(c.GetProperty("category").GetString() ?? "")))
                .ToList();
            var sources = root.GetProperty("sources").EnumerateArray().ToList();
            var failing = sources.Where(s => s.GetProperty("ok").ValueKind == JsonValueKind.False).ToList();
            var starting = sources.Count > 0 && sources.All(s => s.GetProperty("ok").ValueKind == JsonValueKind.Null);

            var figures = new List<TileFigure>
            {
                new("Events (24 h)", total.ToString("N0", Inv), TileTone.Neutral, undated > 0 ? undated.ToString(Inv) + " without a time (not counted)" : null),
            };
            if (failing.Count > 0)
            {
                var first = failing[0];
                figures.Add(new TileFigure("Feeds", failing.Count.ToString(Inv) + " failing", TileTone.Caution,
                    (first.GetProperty("source").GetString() ?? "?") + ": " + (first.TryGetProperty("message", out var m) ? m.GetString() : null)));
            }
            else if (starting)
            {
                figures.Add(new TileFigure("Feeds", "starting…", TileTone.Neutral));
            }
            else
            {
                figures.Add(new TileFigure("Feeds", sources.Count.ToString(Inv) + " ok", TileTone.Positive));
            }
            return new TileLiveData(asOf, figures, dots: dots);
        }

        public Drawing CreateIllustration()
        {
            var g = TileArt.Canvas();
            g.Children.Add(TileArt.Ring("AccentBlueDimBrush", 80, 45, 34, 1.5));
            g.Children.Add(TileArt.Ring("AccentBlueDimBrush", 80, 45, 22, 1, 0.6));
            g.Children.Add(TileArt.Polyline("AccentBlueDimBrush", 1, 0.6, new Point(46, 45), new Point(114, 45)));
            g.Children.Add(TileArt.Polyline("AccentBlueDimBrush", 1, 0.6, new Point(80, 11), new Point(80, 79)));
            g.Children.Add(TileArt.Dot("AccentRedBrush", 66, 32, 3));
            g.Children.Add(TileArt.Dot("AccentAmberBrush", 98, 54, 3));
            g.Children.Add(TileArt.Dot("AccentBlueBrush", 88, 26, 2.5));
            g.Children.Add(TileArt.Dot("PositiveGreenBrush", 60, 60, 2.5));
            return g;
        }

        public bool OwnsWindow(Window window) => window is MainWindow;
        public bool IsSandbox(Window window) => false;   // Global Monitoring has no sandbox profile
    }
}

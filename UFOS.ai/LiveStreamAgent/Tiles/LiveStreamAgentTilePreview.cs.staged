using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using UFOS.ai.ModuleTiles;
using UFOS.ai.Shared;

namespace UFOS.ai.LiveStreamTiles
{
    /// <summary>
    /// LiveStream Agent tile preview (TILES_API §3 + §10.6). Live = <c>GET /verdicts?limit=1</c> on the backend's HTTP API
    /// (127.0.0.1:8766, token) — the stored verdict history, a pure read (no AI call). Figures: the last verdict + confidence
    /// (AI output → marked "✦ AI" and "AI-generated" in the AutomationName) and its time, labelled "last verdict"
    /// (provenance.generated_at). Backend off → the host shows the stored figures.
    /// Last view: the LiveStream Agent window.
    /// </summary>
    public sealed class LiveStreamAgentTilePreview : IModuleTilePreview
    {
        public const int Port = 8766;
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private readonly LoopbackReader _reader = new(Port, () => BackendAuth.ReadToken(BackendAuth.FindBackendDir("LiveStreamAgent", "backend")), "LiveStream Agent backend");

        public string ModuleId => "livestream-agent";
        public string DisplayName => "LiveStream Agent";
        public string? LiveUnavailableReason => null;
        public string? LastViewUnavailableReason => null;

        public async Task<TileLiveResult> ReadLiveAsync(CancellationToken ct)
        {
            var health = await _reader.HealthAsync(ct).ConfigureAwait(false);
            if (!health.Running) return TileLiveResult.BackendOff();
            var r = await _reader.GetJsonAsync("/verdicts?limit=1", ct).ConfigureAwait(false);
            if (!r.Ok) return TileLiveResult.Unavailable(r.Problem ?? "no answer");
            try
            {
                return Parse(r.Body!);
            }
            catch (Exception ex) when (ex is JsonException || ex is KeyNotFoundException || ex is InvalidOperationException || ex is FormatException)
            {
                return TileLiveResult.Unavailable("unexpected answer from the LiveStream Agent backend (" + ex.GetType().Name + ")");
            }
        }

        /// <summary>Maps the newest verdict (public for the offline test).</summary>
        public static TileLiveResult Parse(string json)
        {
            using var doc = JsonDocument.Parse(json);
            var arr = doc.RootElement;
            if (arr.ValueKind != JsonValueKind.Array) throw new FormatException("not a list");
            if (arr.GetArrayLength() == 0) return TileLiveResult.NoData("no verdict yet");
            var v = arr[0];
            var verdict = v.GetProperty("verdict").GetString() ?? "?";
            var ticker = v.TryGetProperty("ticker", out var t) && t.ValueKind == JsonValueKind.String ? t.GetString() : null;
            string conf = v.TryGetProperty("confidence", out var c) && c.ValueKind == JsonValueKind.Number ? c.GetDouble().ToString("0", Inv) + "%" : "confidence n/a";
            DateTimeOffset? at = null;
            if (v.TryGetProperty("provenance", out var p) && p.ValueKind == JsonValueKind.Object
                && p.TryGetProperty("generated_at", out var g) && g.ValueKind == JsonValueKind.String
                && DateTimeOffset.TryParse(g.GetString(), Inv, DateTimeStyles.AssumeUniversal, out var parsed))
                at = parsed;
            var model = p.ValueKind == JsonValueKind.Object && p.TryGetProperty("model", out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() : null;
            var figures = new List<TileFigure>
            {
                new("Last verdict", verdict + (ticker != null ? " " + ticker : "") + " · " + conf, TileTone.Ai, model, AiGenerated: true),
                new("Last verdict at", at is DateTimeOffset a ? a.ToLocalTime().ToString("HH:mm, dd.MM.", Inv) : "time not reported", TileTone.Neutral),
            };
            return TileLiveResult.Fresh(new TileLiveData(DateTimeOffset.Now, figures));
        }

        public Drawing CreateIllustration()
        {
            var g = TileArt.Canvas();
            for (var i = 0; i < 16; i++)
            {
                var h = 6 + (i * 7 % 26);
                g.Children.Add(TileArt.Box("AccentBlueDimBrush", 18 + i * 8, 45 - h / 2.0, 4, h, 2, 0.8));
            }
            g.Children.Add(TileArt.Box("AiSurfaceBrush", 108, 58, 36, 16, 4));
            g.Children.Add(TileArt.Dot("AiAccentBrush", 116, 66, 2.5));
            return g;
        }

        public bool OwnsWindow(Window window) => window is UFOS.ai.LivestreamAgent;
        public bool IsSandbox(Window window) => false;   // the agent has no sandbox profile
    }
}

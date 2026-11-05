using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using UFOS.ai.HedgeFund.MarketResearch;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.ModuleTiles;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Tiles
{
    /// <summary>
    /// Hedge Fund tile preview (TILES_API §3 + lead decision §10.4). Live = <c>GET /v2/portfolio/summary</c> (SP §17.15, stored
    /// values only — no broker call, no market data, does not touch the dead-man heartbeat), mapped by
    /// <see cref="HedgeFundTileSummary"/>: equity, day P&amp;L, trading state (+ sparkline of paper-main). GET /health (no token,
    /// 800 ms) decides "backend off" — the tile never starts the backend. Sandbox figures (health or payload "mode": "sandbox")
    /// carry the SANDBOX chip and are never stored.
    /// Last view: the Hedge Fund window, the Research pop-out and the Financial Newsroom (any of them, however opened); no
    /// picture while the sandbox strip is shown or the backend reports sandbox mode.
    /// </summary>
    public sealed class HedgeFundTilePreview : IModuleTilePreview
    {
        public const int Port = 8867;
        private static volatile bool _lastHealthSandbox;
        private readonly LoopbackReader _reader = new(Port, () => BackendAuth.ReadToken(BackendAuth.FindBackendDir("HedgeFund", "backend")), "Hedge Fund backend");

        public string ModuleId => "hedgefund";
        public string DisplayName => "Hedge Fund";
        public string? LiveUnavailableReason => null;
        public string? LastViewUnavailableReason => null;

        public async Task<TileLiveResult> ReadLiveAsync(CancellationToken ct)
        {
            var health = await _reader.HealthAsync(ct).ConfigureAwait(false);
            _lastHealthSandbox = health.Running && health.Sandbox;
            if (!health.Running) return TileLiveResult.BackendOff();
            var r = await _reader.GetJsonAsync("/v2/portfolio/summary", ct).ConfigureAwait(false);
            if (!r.Ok) return TileLiveResult.Unavailable(r.Problem ?? "no answer", health.Sandbox);
            try
            {
                var result = HedgeFundTileSummary.Parse(r.Body!);
                // sandbox when the payload's "mode" OR /health says so — such figures are shown with the chip, never stored
                return health.Sandbox && !result.Sandbox ? result with { Sandbox = true } : result;
            }
            catch (Exception ex) when (ex is JsonException || ex is KeyNotFoundException || ex is InvalidOperationException || ex is FormatException)
            {
                return TileLiveResult.Unavailable("unexpected answer from the Hedge Fund backend (" + ex.GetType().Name + ")", health.Sandbox);
            }
        }

        public Drawing CreateIllustration()
        {
            var g = TileArt.Canvas();
            for (var i = 0; i < 7; i++)
            {
                var h = 8 + (i * 13 % 22);
                g.Children.Add(TileArt.Box("ControlBackgroundBrush", 20 + i * 18, 78 - h, 10, h, 2));
            }
            g.Children.Add(TileArt.Polyline("AccentBlueBrush", 2, 1, new Point(18, 58), new Point(40, 50), new Point(60, 54), new Point(84, 36),
                                            new Point(104, 40), new Point(124, 24), new Point(142, 18)));
            g.Children.Add(TileArt.Polyline("MutedForegroundBrush", 1.2, 0.6, new Point(18, 62), new Point(60, 56), new Point(100, 50), new Point(142, 42)));
            g.Children.Add(TileArt.Dot("PositiveGreenBrush", 142, 18, 3));
            return g;
        }

        public bool OwnsWindow(Window window) => window is HedgeFundWindow || window is ResearchPopoutWindow || window is FinancialNewsroomWindow;

        public bool IsSandbox(Window window)
        {
            if (_lastHealthSandbox) return true;
            foreach (Window w in Application.Current.Windows)
                if (w is HedgeFundWindow hf && hf.SandboxStrip.Visibility == Visibility.Visible) return true;
            return false;
        }
    }
}

using System;
using System.Collections.Generic;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>A chart marker as the API delivers it (ISO times) — converted to the bridge shape in <see cref="ChartHost"/>.</summary>
    internal sealed class ChartMarkerInput
    {
        public string Id = "";
        public string? T;
        public string? TEnd;
        public string Kind = "";
        public string? Side;
        public string Label = "";
    }

    /// <summary>
    /// MR8 markers on the chart (SPEC "MR8 bridge addition (revision 4)"): insider buys/sells, FOMC / release dates,
    /// backtest fills and attribution periods/drawdowns. Host → page <c>setMarkers {markers}</c>, sent ONLY to pages that
    /// report <c>ready.revision ≥ 4</c> (older pages would answer bad_message); kept here and re-sent after a page
    /// re-creation. ISO → epoch ms happens here (SPEC §0 Time). Labels are plain text (the page uses textContent).
    /// </summary>
    internal sealed partial class ChartHost
    {
        public const int MarkersRevision = 4;
        public const int MaxMarkers = 2000;
        private const int MaxMarkerLabelChars = 200;

        private static readonly HashSet<string> MarkerKinds = new(StringComparer.Ordinal)
            { "insider", "fomc", "release", "fill", "period_best", "period_worst", "drawdown" };
        private static readonly HashSet<string> MarkerSides = new(StringComparer.Ordinal) { "buy", "sell", "short", "cover" };

        private List<object> _markers = new();

        /// <summary>The loaded page can draw markers (bridge revision ≥ 4).</summary>
        public bool SupportsMarkers => PageRevision >= MarkersRevision;

        /// <summary>Number of markers currently held (sent when the page supports them).</summary>
        public int MarkerCount => _markers.Count;

        /// <summary>
        /// Replaces all markers (null/empty clears them). Invalid entries (unknown kind, bad time, end before start) are
        /// dropped, never repaired; returns how many were dropped.
        /// </summary>
        public int SetMarkers(IReadOnlyList<ChartMarkerInput>? markers)
        {
            var list = new List<object>();
            var dropped = 0;
            foreach (var m in markers ?? Array.Empty<ChartMarkerInput>())
            {
                if (list.Count >= MaxMarkers || !MarkerKinds.Contains(m.Kind) || !TryToEpochMs(m.T, out var t))
                {
                    dropped++;
                    continue;
                }
                long? tEnd = null;
                if (m.TEnd != null)
                {
                    if (!TryToEpochMs(m.TEnd, out var end) || end < t)
                    {
                        dropped++;
                        continue;
                    }
                    tEnd = end;
                }
                var side = m.Side != null && MarkerSides.Contains(m.Side) ? m.Side : null;
                var label = m.Label.Length <= MaxMarkerLabelChars ? m.Label : m.Label.Substring(0, MaxMarkerLabelChars - 1) + "…";
                var id = m.Id.Length <= 40 ? m.Id : m.Id.Substring(0, 40);
                list.Add(new { id, t, tEnd, kind = m.Kind, side, label });
            }
            _markers = list;
            SendMarkers();
            return dropped;
        }

        private void SendMarkers()
        {
            if (!SupportsMarkers) return;
            Post(new { type = "setMarkers", v = 1, markers = _markers });
        }
    }
}

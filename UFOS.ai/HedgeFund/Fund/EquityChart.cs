using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Media;

namespace UFOS.ai.HedgeFund.Fund
{
    /// <summary>
    /// Minimal line chart: paper equity (focus blue) vs. a benchmark (grey, dashed —
    /// SPY scaled to the starting capital) plus a hairline at the starting capital.
    /// Drawn in OnRender, no chart library. X = time; long series are thinned to
    /// about one point per device pixel. Used by the dashboard and the backtest popup.
    /// </summary>
    internal sealed class EquityChart : FrameworkElement
    {
        private static readonly Brush EquityBrush = Frozen(Color.FromRgb(0x7C, 0x8C, 0xF8));
        private static readonly Brush BenchmarkBrush = Frozen(Color.FromRgb(0x8A, 0x8A, 0x8A));
        private static readonly Brush GridBrush = Frozen(Color.FromRgb(0x2A, 0x2A, 0x2A));
        private static readonly Brush LabelBrush = Frozen(Color.FromRgb(0x6A, 0x6A, 0x6A));
        private static readonly Typeface LabelFace = new(new FontFamily("Consolas"), FontStyles.Normal, FontWeights.Normal, FontStretches.Normal);

        private List<(DateTime X, double Y)> _equity = new();
        private List<(DateTime X, double Y)> _benchmark = new();
        private double? _baseline;
        private string _emptyText = "No equity history yet — the engine records a snapshot every run.";

        public EquityChart()
        {
            SnapsToDevicePixels = true;
            MinHeight = 60;
        }

        /// <summary>Replaces the data. Points without a value are skipped; baseline = starting capital (optional).</summary>
        public void SetData(IEnumerable<(DateTime X, double? Y)> equity, IEnumerable<(DateTime X, double? Y)> benchmark,
                            double? baseline, string? emptyText = null)
        {
            _equity = Clean(equity);
            _benchmark = Clean(benchmark);
            _baseline = baseline is double b && !double.IsNaN(b) && b > 0 ? b : null;
            if (emptyText != null) _emptyText = emptyText;
            InvalidateVisual();
        }

        private static List<(DateTime X, double Y)> Clean(IEnumerable<(DateTime X, double? Y)> points)
            => points.Where(p => p.Y is double y && !double.IsNaN(y) && !double.IsInfinity(y))
                     .Select(p => (p.X, p.Y!.Value))
                     .OrderBy(p => p.X)
                     .ToList();

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            var w = ActualWidth;
            var h = ActualHeight;
            if (w < 20 || h < 20) return;
            dc.DrawRectangle(Brushes.Transparent, null, new Rect(0, 0, w, h)); // hit-testable area

            double ppd;
            try { ppd = VisualTreeHelper.GetDpi(this).PixelsPerDip; } catch { ppd = 1.0; }

            if (_equity.Count < 2)
            {
                var empty = Text(_emptyText, 10.5, ppd);
                empty.MaxTextWidth = Math.Max(20, w - 12);
                dc.DrawText(empty, new Point(6, Math.Max(0, (h - empty.Height) / 2)));
                return;
            }

            var all = _equity.Concat(_benchmark).ToList();
            var minX = all.Min(p => p.X);
            var maxX = all.Max(p => p.X);
            var minY = all.Min(p => p.Y);
            var maxY = all.Max(p => p.Y);
            if (_baseline is double baseValue)
            {
                minY = Math.Min(minY, baseValue);
                maxY = Math.Max(maxY, baseValue);
            }
            if (maxY - minY < 1e-6) { maxY += 1; minY -= 1; }
            var padY = (maxY - minY) * 0.08;
            minY -= padY;
            maxY += padY;
            var spanX = Math.Max(1.0, (maxX - minX).TotalSeconds);

            const double left = 4, top = 14, bottom = 14;
            var right = 4.0;
            var plotW = Math.Max(10, w - left - right);
            var plotH = Math.Max(10, h - top - bottom);

            Point Map((DateTime X, double Y) p) => new(
                left + (p.X - minX).TotalSeconds / spanX * plotW,
                top + (1 - (p.Y - minY) / (maxY - minY)) * plotH);

            // Starting-capital hairline
            if (_baseline is double baseline)
            {
                var y = Map((minX, baseline)).Y;
                dc.DrawLine(new Pen(GridBrush, 1), new Point(left, y), new Point(left + plotW, y));
            }

            if (_benchmark.Count >= 2)
            {
                var pen = new Pen(BenchmarkBrush, 1.2) { DashStyle = new DashStyle(new double[] { 3, 3 }, 0) };
                pen.Freeze();
                DrawSeries(dc, Thin(_benchmark, plotW).Select(Map).ToList(), pen);
            }
            var equityPen = new Pen(EquityBrush, 1.8) { LineJoin = PenLineJoin.Round };
            equityPen.Freeze();
            DrawSeries(dc, Thin(_equity, plotW).Select(Map).ToList(), equityPen);

            // Labels: max / min value, first / last date
            var inv = CultureInfo.InvariantCulture;
            dc.DrawText(Text("$" + maxY.ToString("N0", inv), 9.5, ppd), new Point(left, 0));
            var minLabel = Text("$" + minY.ToString("N0", inv), 9.5, ppd);
            dc.DrawText(minLabel, new Point(left, h - minLabel.Height));
            var fmt = spanX > 3 * 86400 ? "MM-dd" : "MM-dd HH:mm";
            var endLabel = Text(maxX.ToLocalTime().ToString(fmt, inv), 9.5, ppd);
            dc.DrawText(endLabel, new Point(Math.Max(left, left + plotW - endLabel.Width), h - endLabel.Height));
            var startLabel = Text(minX.ToLocalTime().ToString(fmt, inv), 9.5, ppd);
            var startX = left + minLabel.Width + 12;
            if (startX + startLabel.Width < left + plotW - endLabel.Width - 8)
                dc.DrawText(startLabel, new Point(startX, h - startLabel.Height));
        }

        private static void DrawSeries(DrawingContext dc, List<Point> pts, Pen pen)
        {
            if (pts.Count < 2) return;
            var geometry = new StreamGeometry();
            using (var ctx = geometry.Open())
            {
                ctx.BeginFigure(pts[0], false, false);
                ctx.PolyLineTo(pts.Skip(1).ToList(), true, true);
            }
            geometry.Freeze();
            dc.DrawGeometry(null, pen, geometry);
        }

        /// <summary>Keeps at most ~2 points per horizontal pixel (first and last always kept).</summary>
        private static IEnumerable<(DateTime X, double Y)> Thin(List<(DateTime X, double Y)> points, double width)
        {
            var max = Math.Max(50, (int)(width * 2));
            if (points.Count <= max) return points;
            var step = (double)points.Count / max;
            var result = new List<(DateTime X, double Y)>(max + 1);
            for (var i = 0.0; i < points.Count; i += step) result.Add(points[(int)i]);
            if (result[^1] != points[^1]) result.Add(points[^1]);
            return result;
        }

        private FormattedText Text(string text, double size, double ppd)
            => new(text, CultureInfo.InvariantCulture, FlowDirection.LeftToRight, LabelFace, size, LabelBrush, ppd);

        private static Brush Frozen(Color c)
        {
            var b = new SolidColorBrush(c);
            b.Freeze();
            return b;
        }
    }
}

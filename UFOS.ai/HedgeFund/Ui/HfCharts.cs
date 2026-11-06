using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Media;

namespace UFOS.ai.HedgeFund.Ui
{
    // Stage 11 §2: small native charts (OnRender, no chart library) for the Overview and later stage-11 views. Generic: they draw what
    // they are given — no interpolation, no invented points; an empty input draws the caller's empty text (or nothing).
    // Every chart is placed by its caller next to a "⛶" control (Shared/FullScreenHost via FundUi.FullScreenButton) + F11.

    internal static class ChartText
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        public static double Ppd(Visual v)
        {
            try { return VisualTreeHelper.GetDpi(v).PixelsPerDip; } catch { return 1.0; }
        }

        public static FormattedText Make(string text, double size, Brush brush, double ppd)
            => new(text, Inv, FlowDirection.LeftToRight, HfTokens.MonoFace, size, brush, ppd);

        public static double? Finite(double? v) => v is double d && !double.IsNaN(d) && !double.IsInfinity(d) ? d : null;

        public static string Num(double v)
            => Math.Abs(v - Math.Round(v)) < 1e-9 ? v.ToString("#,##0", Inv) : v.ToString("#,##0.##", Inv);

        /// <summary>Draws <paramref name="t"/> with its left edge at <paramref name="x"/>, kept inside [0, width].</summary>
        public static void DrawClamped(DrawingContext dc, FormattedText t, double x, double y, double width)
        {
            var left = Math.Max(0, Math.Min(x, width - t.Width));
            dc.DrawText(t, new Point(left, y));
        }

        public static void DrawEmpty(DrawingContext dc, string text, double w, double h, double ppd)
        {
            if (string.IsNullOrEmpty(text)) return;
            var t = Make(text, 11, HfTokens.TextFaint, ppd);
            t.MaxTextWidth = Math.Max(20, w - 12);
            dc.DrawText(t, new Point(6, Math.Max(0, (h - t.Height) / 2)));
        }
    }

    /// <summary>A neutral grey sparkline (market tiles): the given values left to right, auto-scaled.</summary>
    internal sealed class Sparkline : FrameworkElement
    {
        private static readonly Pen LinePen = MakePen();
        private List<double> _values = new();

        public Sparkline()
        {
            SnapsToDevicePixels = true;
            Height = 18;
        }

        private static Pen MakePen()
        {
            var p = new Pen(HfTokens.ChartLine, 1.25) { LineJoin = PenLineJoin.Round };
            p.Freeze();
            return p;
        }

        public void SetData(IEnumerable<double>? values)
        {
            _values = values == null ? new List<double>() : values.Where(v => !double.IsNaN(v) && !double.IsInfinity(v)).ToList();
            InvalidateVisual();
        }

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            var w = ActualWidth;
            var h = ActualHeight;
            if (w < 4 || h < 4 || _values.Count < 2) return;
            var min = _values.Min();
            var max = _values.Max();
            var span = max - min;
            var g = new StreamGeometry();
            using (var ctx = g.Open())
            {
                for (var i = 0; i < _values.Count; i++)
                {
                    var x = w * i / (_values.Count - 1);
                    var y = span <= 0 ? h / 2 : 1 + (h - 2) * (1 - (_values[i] - min) / span);
                    if (i == 0) ctx.BeginFigure(new Point(x, y), false, false);
                    else ctx.LineTo(new Point(x, y), true, true);
                }
            }
            g.Freeze();
            dc.DrawGeometry(null, LinePen, g);
        }
    }

    /// <summary>
    /// Gamma exposure by strike: one bar per strike (blue &gt; 0, orange &lt; 0 — the gamma-regime colours), a dashed flip line, a solid
    /// spot line and strike labels. The caller passes ONE variant's values and says which in the caption.
    /// </summary>
    internal sealed class GexBarChart : FrameworkElement
    {
        private static readonly Pen ZeroPen = Frozen(new Pen(HfTokens.ChartGrid, 1));
        private static readonly Pen SpotPen = Frozen(new Pen(HfTokens.ChartMarker, 1.5));
        private static readonly Pen FlipPen = Frozen(new Pen(HfTokens.TextDim, 1.2) { DashStyle = new DashStyle(new double[] { 3, 3 }, 0) });

        private List<(double Strike, double Value)> _bars = new();
        private double? _flip;
        private double? _spot;
        private string _empty = "";

        public GexBarChart()
        {
            SnapsToDevicePixels = true;
            MinHeight = 90;
        }

        private static Pen Frozen(Pen p)
        {
            p.Freeze();
            return p;
        }

        public void SetData(IEnumerable<(double Strike, double Value)> bars, double? flip, double? spot, string? emptyText = null)
        {
            _bars = bars.Where(b => !double.IsNaN(b.Strike) && !double.IsInfinity(b.Strike) && !double.IsNaN(b.Value) && !double.IsInfinity(b.Value))
                        .OrderBy(b => b.Strike).ToList();
            _flip = ChartText.Finite(flip);
            _spot = ChartText.Finite(spot);
            _empty = emptyText ?? "";
            InvalidateVisual();
        }

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            var w = ActualWidth;
            var h = ActualHeight;
            if (w < 40 || h < 40) return;
            dc.DrawRectangle(Brushes.Transparent, null, new Rect(0, 0, w, h));
            var ppd = ChartText.Ppd(this);
            if (_bars.Count == 0)
            {
                ChartText.DrawEmpty(dc, _empty, w, h, ppd);
                return;
            }

            const double padL = 4, padR = 4, padT = 16, padB = 18;
            var x0 = _bars[0].Strike;
            var x1 = _bars[_bars.Count - 1].Strike;
            if (_spot is double s0) { x0 = Math.Min(x0, s0); x1 = Math.Max(x1, s0); }
            if (_flip is double f0) { x0 = Math.Min(x0, f0); x1 = Math.Max(x1, f0); }
            if (x1 - x0 < 1e-9) { x0 -= 1; x1 += 1; }
            var pw = w - padL - padR;
            var ph = h - padT - padB;
            double X(double k) => padL + (k - x0) / (x1 - x0) * pw;

            var maxPos = Math.Max(0, _bars.Max(b => b.Value));
            var maxNeg = Math.Max(0, -_bars.Min(b => b.Value));
            if (maxPos + maxNeg <= 0) maxPos = 1;
            var zeroY = padT + ph * (maxPos / (maxPos + maxNeg));
            var scale = ph / (maxPos + maxNeg);

            var minGap = double.MaxValue;
            for (var i = 1; i < _bars.Count; i++)
            {
                var gap = X(_bars[i].Strike) - X(_bars[i - 1].Strike);
                if (gap > 0) minGap = Math.Min(minGap, gap);
            }
            if (minGap == double.MaxValue) minGap = pw * 0.05;
            var bw = Math.Max(1, Math.Min(18, minGap * 0.72));

            dc.DrawLine(ZeroPen, new Point(padL, zeroY), new Point(w - padR, zeroY));
            foreach (var (strike, value) in _bars)
            {
                var x = X(strike);
                var hgt = Math.Abs(value) * scale;
                if (hgt < 0.5) continue;
                var r = value >= 0 ? new Rect(x - bw / 2, zeroY - hgt, bw, hgt) : new Rect(x - bw / 2, zeroY, bw, hgt);
                dc.DrawRectangle(value >= 0 ? HfTokens.GammaPositive : HfTokens.GammaNegative, null, r);
            }

            double? flipLabelStart = null, flipLabelEnd = null;
            if (_flip is double f)
            {
                var fx = X(f);
                dc.DrawLine(FlipPen, new Point(fx, padT - 2), new Point(fx, padT + ph));
                var t = ChartText.Make("flip " + ChartText.Num(f), 10, HfTokens.TextDim, ppd);
                var left = Math.Max(0, Math.Min(fx + 3, w - t.Width));
                dc.DrawText(t, new Point(left, 0));
                flipLabelStart = left;
                flipLabelEnd = left + t.Width;
            }
            if (_spot is double s)
            {
                var sx = X(s);
                dc.DrawLine(SpotPen, new Point(sx, padT - 2), new Point(sx, padT + ph));
                var t = ChartText.Make("spot " + ChartText.Num(s), 10, HfTokens.ChartMarker, ppd);
                var left = sx + 3;
                if (flipLabelStart is double fs && flipLabelEnd is double fe && left < fe + 6 && left + t.Width > fs - 6) left = fe + 6;
                ChartText.DrawClamped(dc, t, left, 0, w);
            }
            var lo = ChartText.Make("strike " + ChartText.Num(_bars[0].Strike), 10, HfTokens.ChartLabel, ppd);
            var hi = ChartText.Make("strike " + ChartText.Num(_bars[_bars.Count - 1].Strike), 10, HfTokens.ChartLabel, ppd);
            dc.DrawText(lo, new Point(padL, h - padB + 3));
            dc.DrawText(hi, new Point(Math.Max(padL + lo.Width + 8, w - padR - hi.Width), h - padB + 3));
        }
    }

    /// <summary>Expected-move range bar: the band [lower, upper] on a track, the spot as a marker, the three values below.</summary>
    internal sealed class MoveRangeBar : FrameworkElement
    {
        private double? _lower;
        private double? _spot;
        private double? _upper;
        private Func<double, string> _format = v => ChartText.Num(v);

        public MoveRangeBar()
        {
            SnapsToDevicePixels = true;
            Height = 44;
        }

        public void SetData(double? lower, double? spot, double? upper, Func<double, string>? format = null)
        {
            _lower = ChartText.Finite(lower);
            _spot = ChartText.Finite(spot);
            _upper = ChartText.Finite(upper);
            if (format != null) _format = format;
            InvalidateVisual();
        }

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            var w = ActualWidth;
            var h = ActualHeight;
            if (w < 40 || h < 30 || _lower is not double lo || _upper is not double hi || _spot is not double s || hi <= lo) return;
            var ppd = ChartText.Ppd(this);
            var pad = (hi - lo) * 0.35;
            var d0 = Math.Min(lo - pad, s);
            var d1 = Math.Max(hi + pad, s);
            const double padX = 6;
            double X(double val) => padX + (val - d0) / (d1 - d0) * (w - 2 * padX);
            dc.DrawRoundedRectangle(HfTokens.ChartTrack, null, new Rect(padX, 8, w - 2 * padX, 8), 4, 4);
            dc.DrawRoundedRectangle(HfTokens.ChartBand, null, new Rect(X(lo), 8, Math.Max(1, X(hi) - X(lo)), 8), 4, 4);
            dc.DrawEllipse(HfTokens.ChartMarker, null, new Point(X(s), 12), 6, 6);
            var tl = ChartText.Make(_format(lo), 10.5, HfTokens.ChartLabel, ppd);
            var ts = ChartText.Make("spot " + _format(s), 10.5, HfTokens.TextDim, ppd);
            var th = ChartText.Make(_format(hi), 10.5, HfTokens.ChartLabel, ppd);
            var y = 24;
            var leftL = Math.Max(0, X(lo) - tl.Width / 2);
            var leftH = Math.Min(w - th.Width, X(hi) - th.Width / 2);
            var leftS = Math.Max(leftL + tl.Width + 6, Math.Min(X(s) - ts.Width / 2, leftH - ts.Width - 6));
            dc.DrawText(tl, new Point(leftL, y));
            dc.DrawText(th, new Point(Math.Max(0, leftH), y));
            if (leftS + ts.Width <= leftH - 2) dc.DrawText(ts, new Point(leftS, y));
        }
    }

    /// <summary>One point of a <see cref="TermCurve"/> (categorical x): the axis label and the value.</summary>
    internal sealed record TermPoint(string Label, double Value, string ValueText);

    /// <summary>A term-structure curve: ONLY the given points (categorical, evenly spaced), joined by a line, values above, labels below.</summary>
    internal sealed class TermCurve : FrameworkElement
    {
        private static readonly Pen LinePen = MakePen();
        private List<TermPoint> _points = new();
        private string _empty = "";

        public TermCurve()
        {
            SnapsToDevicePixels = true;
            MinHeight = 70;
        }

        private static Pen MakePen()
        {
            var p = new Pen(HfTokens.TextMuted, 1.5) { LineJoin = PenLineJoin.Round };
            p.Freeze();
            return p;
        }

        public void SetData(IEnumerable<TermPoint> points, string? emptyText = null)
        {
            _points = points.Where(p => !double.IsNaN(p.Value) && !double.IsInfinity(p.Value)).ToList();
            _empty = emptyText ?? "";
            InvalidateVisual();
        }

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            var w = ActualWidth;
            var h = ActualHeight;
            if (w < 40 || h < 40) return;
            dc.DrawRectangle(Brushes.Transparent, null, new Rect(0, 0, w, h));
            var ppd = ChartText.Ppd(this);
            if (_points.Count == 0)
            {
                ChartText.DrawEmpty(dc, _empty, w, h, ppd);
                return;
            }
            const double padX = 28, padT = 18, padB = 18;
            var min = _points.Min(p => p.Value);
            var max = _points.Max(p => p.Value);
            var span = max - min;
            var ph = h - padT - padB;
            double X(int idx) => _points.Count == 1 ? w / 2 : padX + (w - 2 * padX) * idx / (_points.Count - 1);
            double Y(double val) => span <= 0 ? padT + ph / 2 : padT + 4 + (ph - 8) * (1 - (val - min) / span);
            for (var i = 1; i < _points.Count; i++)
                dc.DrawLine(LinePen, new Point(X(i - 1), Y(_points[i - 1].Value)), new Point(X(i), Y(_points[i].Value)));
            for (var i = 0; i < _points.Count; i++)
            {
                var p = _points[i];
                var c = new Point(X(i), Y(p.Value));
                dc.DrawEllipse(HfTokens.ChartMarker, null, c, 3.5, 3.5);
                var tv = ChartText.Make(p.ValueText, 10.5, HfTokens.Text, ppd);
                ChartText.DrawClamped(dc, tv, c.X - tv.Width / 2, Math.Max(0, c.Y - 6 - tv.Height), w);
                var tl = ChartText.Make(p.Label, 10, HfTokens.ChartLabel, ppd);
                ChartText.DrawClamped(dc, tl, c.X - tl.Width / 2, h - padB + 3, w);
            }
        }
    }
}

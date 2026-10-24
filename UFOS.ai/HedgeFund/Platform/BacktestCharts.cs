using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Input;
using System.Windows.Media;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>Local design tokens and helpers of the backtest charts (dark theme, low-contrast grid).</summary>
    internal static class ChartTokens
    {
        /// <summary>
        /// Live click-test 2026-10-22: the mouse wheel over a chart scrolls the page. Unless the chart has the keyboard focus (arrow-key
        /// reading), the wheel scrolls the nearest ancestor ScrollViewer that can scroll vertically by the normal step (Windows wheel
        /// lines × 16 px) and is marked handled, so no element in between (e.g. a horizontal scroller around a table) swallows it.
        /// </summary>
        public static void ForwardWheel(UIElement source, MouseWheelEventArgs e)
        {
            if (e.Handled || e.Delta == 0 || source.IsKeyboardFocusWithin) return;
            for (var p = VisualTreeHelper.GetParent(source); p != null; p = VisualTreeHelper.GetParent(p))
            {
                if (p is System.Windows.Controls.ScrollViewer sv && sv.ScrollableHeight > 0
                    && sv.VerticalScrollBarVisibility != System.Windows.Controls.ScrollBarVisibility.Disabled)
                {
                    ScrollByWheel(sv, e.Delta);
                    e.Handled = true;
                    return;
                }
            }
        }

        /// <summary>
        /// Retest 2026-10-23 ("30 notches ≈ 40 px"): one wheel notch = Windows wheel lines × 16 px (3 × 16 = 48 px by default).
        /// Root cause of the slow scroll: the earlier version set ScrollToVerticalOffset(VerticalOffset − step), but VerticalOffset
        /// only changes after the next layout pass — quick notches (and the chart overlay redraws they trigger) all aimed at the
        /// same stale offset, so a burst of notches moved the page about one step. LineUp / LineDown are queued by the ScrollViewer
        /// and applied one after another, so every notch counts.
        /// </summary>
        public static void ScrollByWheel(System.Windows.Controls.ScrollViewer sv, int delta)
            => UFOS.ai.HedgeFund.Fund.FundUi.WheelScroll(sv, delta);   // one notch at least per event (retest 2026-10-23)

        /// <summary>
        /// The report page's ScrollViewer takes every wheel notch in the tunnelling phase (before any card, table, bar chart or expander
        /// below it can swallow or shrink it) and scrolls by the normal step — except while a chart has the keyboard focus for arrow-key
        /// reading (then the default routing applies).
        /// </summary>
        public static void AttachPageWheel(System.Windows.Controls.ScrollViewer sv)
        {
            sv.PreviewMouseWheel += (sender, e) =>
            {
                if (e.Handled || e.Delta == 0) return;
                if (InFocusedChart(e.OriginalSource as DependencyObject, sv)) return;
                ScrollByWheel(sv, e.Delta);
                e.Handled = true;
            };
        }

        private static bool InFocusedChart(DependencyObject? start, DependencyObject stop)
        {
            for (var d = start; d != null && !ReferenceEquals(d, stop); d = d is Visual || d is System.Windows.Media.Media3D.Visual3D ? VisualTreeHelper.GetParent(d) : LogicalTreeHelper.GetParent(d))
            {
                if (d is BacktestLineChart chart && chart.IsKeyboardFocusWithin) return true;
            }
            return false;
        }

        public static readonly Brush GridLine = Frozen(Color.FromArgb(0x12, 0xFF, 0xFF, 0xFF));      // white ≈ 7 %
        public static readonly Brush AxisLabel = Frozen(Color.FromRgb(0x6A, 0x6A, 0x6A));
        public static readonly Brush Crosshair = Frozen(Color.FromArgb(0x59, 0xFF, 0xFF, 0xFF));     // white ≈ 35 %
        public static readonly Brush TooltipBg = Frozen(Color.FromArgb(0xF2, 0x16, 0x16, 0x16));
        public static readonly Brush TooltipBorder = Frozen(Color.FromRgb(0x33, 0x33, 0x33));
        public static readonly Brush TooltipText = Frozen(Color.FromRgb(0xBF, 0xBF, 0xBF));
        public static readonly Brush FocusRing = Frozen(Color.FromRgb(0x7C, 0x8C, 0xF8));
        public static readonly Brush DotRing = Frozen(Color.FromRgb(0x11, 0x11, 0x11));
        public static readonly Typeface Mono = new(new FontFamily("Consolas"), FontStyles.Normal, FontWeights.Normal, FontStretches.Normal);
        public static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        public const string Minus = "−";

        public static Brush Frozen(Color c)
        {
            var b = new SolidColorBrush(c);
            b.Freeze();
            return b;
        }

        /// <summary>Centre of the nearest device pixel (a 1-device-pixel line drawn there is crisp).</summary>
        public static double Snap(double v, double ppd) => (Math.Round(v * ppd - 0.5) + 0.5) / ppd;

        /// <summary>1 device pixel wide pen.</summary>
        public static Pen Hairline(Brush brush, double ppd)
        {
            var p = new Pen(brush, 1.0 / ppd);
            p.Freeze();
            return p;
        }

        /// <summary>Step from {1, 2, 2.5, 5} × 10^n so that <paramref name="range"/> splits into about <paramref name="target"/> intervals.</summary>
        public static double NiceStep(double range, int target)
        {
            if (!(range > 0) || double.IsInfinity(range)) return 1;
            var raw = range / target;
            var mag = Math.Pow(10, Math.Floor(Math.Log10(raw)));
            foreach (var m in new[] { 1.0, 2.0, 2.5, 5.0, 10.0 })
                if (m * mag >= raw * 0.999) return m * mag;
            return 10 * mag;
        }

        /// <summary>"$80k", "$102.5k", "$1.2M".</summary>
        public static string CompactMoney(double v)
        {
            var a = Math.Abs(v);
            string s;
            if (a >= 1e9) s = (a / 1e9).ToString("0.##", Inv) + "B";
            else if (a >= 1e6) s = (a / 1e6).ToString("0.##", Inv) + "M";
            else if (a >= 1e3) s = (a / 1e3).ToString("0.##", Inv) + "k";
            else s = a.ToString("0", Inv);
            return (v < 0 && Math.Round(a, 2) != 0 ? Minus : "") + "$" + s;
        }

        public static string Money(double v) => (v < 0 && Math.Round(v, 0) != 0 ? Minus : "") + "$" + Math.Abs(v).ToString("N0", Inv);

        public static string Percent(double v, int decimals)
            => (v < 0 && Math.Round(v, decimals) != 0 ? Minus : "") + Math.Abs(v).ToString("F" + decimals, Inv) + " %";

        public static FormattedText Text(string text, double size, Brush brush, double ppd)
            => new(text, Inv, FlowDirection.LeftToRight, Mono, size, brush, ppd);

        /// <summary>Index of the point whose date is nearest to <paramref name="date"/> (dates sorted ascending), or −1.</summary>
        public static int Nearest(DateTime[] dates, DateTime date)
        {
            if (dates.Length == 0) return -1;
            var i = Array.BinarySearch(dates, date);
            if (i >= 0) return i;
            i = ~i;
            if (i <= 0) return 0;
            if (i >= dates.Length) return dates.Length - 1;
            return (date - dates[i - 1]) <= (dates[i] - date) ? i - 1 : i;
        }
    }

    /// <summary>Keeps the crosshair date of several charts in sync (equity + drawdown of one report).</summary>
    internal sealed class ChartCursorGroup
    {
        private readonly List<BacktestLineChart> _charts = new();

        public void Register(BacktestLineChart chart)
        {
            if (!_charts.Contains(chart)) _charts.Add(chart);
        }

        public void Broadcast(BacktestLineChart source, DateTime? date)
        {
            foreach (var c in _charts)
                if (!ReferenceEquals(c, source)) c.ShowSyncedCursor(date);
        }
    }

    /// <summary>
    /// Line chart of a backtest run (no chart library; same look as Fund/EquityChart). Two uses:
    /// - equity (focus blue) vs benchmark (grey, dashed), hairline at the starting capital;
    /// - drawdown (red line, area to zero), percent below the running peak.
    /// Layout: fixed left margin for the Y labels, so charts of one report share plot width and X
    /// positions; low-contrast hairline grid at "nice" Y milestones and at January 1 of the
    /// labelled years; optional vertical marker (data seam 2016-01-04).
    /// Two visuals: the base (grid, series, labels — redrawn only on data / size / DPI change, screen
    /// points precomputed there) and an overlay (crosshair, dots, tooltip, focus ring — redrawn on
    /// hover, found by binary search on the dates). Keyboard: Left/Right/Home/End move the
    /// crosshair while focused; Escape hides it.
    /// </summary>
    internal sealed class BacktestLineChart : FrameworkElement
    {
        private static readonly Brush EquityBrush = ChartTokens.Frozen(Color.FromRgb(0x7C, 0x8C, 0xF8));
        private static readonly Brush BenchmarkBrush = ChartTokens.Frozen(Color.FromRgb(0x8A, 0x8A, 0x8A));
        private static readonly Brush DrawdownBrush = ChartTokens.Frozen(Color.FromRgb(0xED, 0x6A, 0x5A));
        private static readonly Brush DrawdownFill = ChartTokens.Frozen(Color.FromArgb(0x30, 0xED, 0x6A, 0x5A));
        private static readonly Brush MarkerBrush = ChartTokens.Frozen(Color.FromRgb(0xD9, 0xA5, 0x4B));
        private static readonly Brush BaselineBrush = ChartTokens.Frozen(Color.FromRgb(0x2A, 0x2A, 0x2A));

        public const double LeftMargin = 58;
        private const double RightMargin = 8, TopMargin = 8, BottomMargin = 18;

        private readonly bool _drawdown;
        private readonly DrawingVisual _base = new();
        private readonly DrawingVisual _overlay = new();
        private readonly VisualCollection _visuals;

        // data (sorted by date)
        private DateTime[] _mainDates = System.Array.Empty<DateTime>();
        private double[] _mainValues = System.Array.Empty<double>();
        private DateTime[] _secondDates = System.Array.Empty<DateTime>();
        private double[] _secondValues = System.Array.Empty<double>();
        private DateTime[] _extraDates = System.Array.Empty<DateTime>();
        private double[] _extraValues = System.Array.Empty<double>();
        private string _mainLabel = "", _secondLabel = "", _extraLabel = "";
        private double? _baseline;
        private DateTime? _marker;
        private string _markerLabel = "";
        private string _emptyText = "No data.";
        private DateTime? _xMinWanted, _xMaxWanted;

        // layout of the last base render
        private bool _hasPlot;
        private double _plotW, _plotH;
        private DateTime _minX;
        private double _spanSec;
        private double _minY, _maxY;
        private Point[] _mainScreen = System.Array.Empty<Point>();
        private Point[] _secondScreen = System.Array.Empty<Point>();

        // cursor
        private int _cursor = -1;
        private bool _tooltip;
        private ChartCursorGroup? _group;

        /// <param name="drawdown">true = drawdown chart (values ≤ 0, percent), false = equity vs benchmark (USD).</param>
        public BacktestLineChart(bool drawdown)
        {
            _drawdown = drawdown;
            _visuals = new VisualCollection(this) { _base, _overlay };
            SnapsToDevicePixels = true;
            MinHeight = 60;
            Focusable = true;
            FocusVisualStyle = null; // the overlay draws its own focus ring
            Cursor = Cursors.Cross;
        }

        public ChartCursorGroup? CursorGroup
        {
            get => _group;
            set
            {
                _group = value;
                value?.Register(this);
            }
        }

        /// <summary>Replaces the data. <paramref name="xMin"/>/<paramref name="xMax"/> fix the time axis (same in all
        /// charts of a report so they line up). <paramref name="title"/> starts the screen-reader name.</summary>
        public void SetData(IEnumerable<(DateTime X, double? Y)> main, string mainLabel,
                            IEnumerable<(DateTime X, double? Y)>? second, string? secondLabel,
                            double? baseline, DateTime? marker, string markerLabel, string emptyText,
                            DateTime? xMin, DateTime? xMax, string title)
        {
            (_mainDates, _mainValues) = Clean(main);
            (_secondDates, _secondValues) = second == null ? (System.Array.Empty<DateTime>(), System.Array.Empty<double>()) : Clean(second);
            _mainLabel = mainLabel;
            _secondLabel = secondLabel ?? "";
            _baseline = baseline is double b && !double.IsNaN(b) && !double.IsInfinity(b) ? b : null;
            _marker = marker;
            _markerLabel = markerLabel;
            _emptyText = emptyText;
            _xMinWanted = xMin;
            _xMaxWanted = xMax;
            _cursor = -1;
            AutomationProperties.SetName(this, title + ". " + Summary() + (_mainDates.Length >= 2 ? " Arrow keys move the crosshair." : ""));
            RedrawBase();
            RedrawOverlay();
        }

        /// <summary>A series shown only in the tooltip (e.g. the equity value in the drawdown chart).</summary>
        public void SetTooltipExtra(string label, IEnumerable<(DateTime X, double? Y)> points)
        {
            (_extraDates, _extraValues) = Clean(points);
            _extraLabel = label;
        }

        private static (DateTime[] Dates, double[] Values) Clean(IEnumerable<(DateTime X, double? Y)> points)
        {
            var list = points.Where(p => p.Y is double y && !double.IsNaN(y) && !double.IsInfinity(y))
                             .Select(p => (p.X, Y: p.Y!.Value))
                             .OrderBy(p => p.X)
                             .ToList();
            return (list.Select(p => p.X).ToArray(), list.Select(p => p.Y).ToArray());
        }

        private string Summary()
        {
            if (_mainDates.Length < 2) return _emptyText;
            var period = _mainDates[0].ToString("yyyy-MM-dd", ChartTokens.Inv) + " to " + _mainDates[^1].ToString("yyyy-MM-dd", ChartTokens.Inv);
            if (_drawdown)
            {
                var i = System.Array.IndexOf(_mainValues, _mainValues.Min());
                return period + ": maximum drawdown " + ChartTokens.Percent(_mainValues[i], 1) + " on " + _mainDates[i].ToString("yyyy-MM-dd", ChartTokens.Inv) + ".";
            }
            var text = period + ": " + _mainLabel + " from " + ChartTokens.Money(_mainValues[0]) + " to " + ChartTokens.Money(_mainValues[^1]);
            if (_secondValues.Length >= 2)
                text += ", " + _secondLabel + " from " + ChartTokens.Money(_secondValues[0]) + " to " + ChartTokens.Money(_secondValues[^1]);
            return text + ".";
        }

        // ---------- visual plumbing ----------

        protected override int VisualChildrenCount => _visuals.Count;
        protected override Visual GetVisualChild(int index) => _visuals[index];

        protected override HitTestResult HitTestCore(PointHitTestParameters hitTestParameters)
            => new PointHitTestResult(this, hitTestParameters.HitPoint);

        protected override void OnRenderSizeChanged(SizeChangedInfo sizeInfo)
        {
            base.OnRenderSizeChanged(sizeInfo);
            RedrawBase();
            RedrawOverlay();
        }

        protected override void OnDpiChanged(DpiScale oldDpi, DpiScale newDpi)
        {
            base.OnDpiChanged(oldDpi, newDpi);
            RedrawBase();
            RedrawOverlay();
        }

        private double Ppd => VisualTreeHelper.GetDpi(this).PixelsPerDip;

        private double XOf(DateTime d) => LeftMargin + (d - _minX).TotalSeconds / _spanSec * _plotW;
        private double YOf(double v) => TopMargin + (1 - (v - _minY) / (_maxY - _minY)) * _plotH;

        // ---------- base layer ----------

        private void RedrawBase()
        {
            var w = ActualWidth;
            var h = ActualHeight;
            using var dc = _base.RenderOpen();
            _hasPlot = false;
            _mainScreen = System.Array.Empty<Point>();
            _secondScreen = System.Array.Empty<Point>();
            if (w < 40 || h < 30) return;
            var ppd = Ppd;
            dc.DrawRectangle(Brushes.Transparent, null, new Rect(0, 0, w, h));

            if (_mainDates.Length < 2)
            {
                var empty = ChartTokens.Text(_emptyText, 10.5, ChartTokens.AxisLabel, ppd);
                empty.MaxTextWidth = Math.Max(20, w - 12);
                dc.DrawText(empty, new Point(6, Math.Max(0, (h - empty.Height) / 2)));
                return;
            }

            // ----- ranges -----
            var minX = _mainDates[0];
            var maxX = _mainDates[^1];
            if (_secondDates.Length > 0)
            {
                if (_secondDates[0] < minX) minX = _secondDates[0];
                if (_secondDates[^1] > maxX) maxX = _secondDates[^1];
            }
            if (_xMinWanted is DateTime a && a < maxX) minX = a;
            if (_xMaxWanted is DateTime z && z > minX) maxX = z;
            _minX = minX;
            _spanSec = Math.Max(1.0, (maxX - minX).TotalSeconds);
            _plotW = Math.Max(10, w - LeftMargin - RightMargin);
            _plotH = Math.Max(10, h - TopMargin - BottomMargin);

            var yTicks = new List<double>();
            if (_drawdown)
            {
                var minDd = _mainValues.Min();
                var step = minDd < -40 ? 10.0 : 5.0;
                while (_plotH / (Math.Ceiling(-minDd / step) + 1) < 13 && step < 100) step *= 2; // never crowd the labels
                _minY = Math.Min(-step, Math.Floor(minDd / step) * step);
                _maxY = 0;
                for (var v = 0.0; v >= _minY - 1e-9; v -= step) yTicks.Add(v);
            }
            else
            {
                var lo = Math.Min(_mainValues.Min(), _secondValues.Length > 0 ? _secondValues.Min() : double.MaxValue);
                var hi = Math.Max(_mainValues.Max(), _secondValues.Length > 0 ? _secondValues.Max() : double.MinValue);
                if (_baseline is double bl) { lo = Math.Min(lo, bl); hi = Math.Max(hi, bl); }
                if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
                var step = ChartTokens.NiceStep(hi - lo, 5);
                _minY = Math.Floor(lo / step) * step;
                _maxY = Math.Ceiling(hi / step) * step;
                if (_maxY - _minY < step) _maxY = _minY + step;
                for (var k = 0; ; k++)
                {
                    var v = _minY + k * step;
                    if (v > _maxY + step * 1e-6) break;
                    yTicks.Add(v);
                }
            }
            _hasPlot = true;

            // ----- precomputed screen points (used for drawing and hover) -----
            _mainScreen = new Point[_mainDates.Length];
            for (var i = 0; i < _mainDates.Length; i++) _mainScreen[i] = new Point(XOf(_mainDates[i]), YOf(_mainValues[i]));
            _secondScreen = new Point[_secondDates.Length];
            for (var i = 0; i < _secondDates.Length; i++) _secondScreen[i] = new Point(XOf(_secondDates[i]), YOf(_secondValues[i]));

            // ----- grid (behind everything) -----
            var grid = ChartTokens.Hairline(ChartTokens.GridLine, ppd);
            var right = LeftMargin + _plotW;
            var bottom = TopMargin + _plotH;
            foreach (var v in yTicks)
            {
                var y = ChartTokens.Snap(YOf(v), ppd);
                dc.DrawLine(grid, new Point(LeftMargin, y), new Point(right, y));
                var label = ChartTokens.Text(_drawdown ? (v == 0 ? "0 %" : ChartTokens.Percent(v, 0)) : ChartTokens.CompactMoney(v), 9.5, ChartTokens.AxisLabel, ppd);
                dc.DrawText(label, new Point(LeftMargin - 6 - label.Width, Math.Max(0, Math.Min(h - label.Height, YOf(v) - label.Height / 2))));
            }
            foreach (var (at, text) in XTicks(minX, maxX, _plotW))
            {
                var x = ChartTokens.Snap(XOf(at), ppd);
                dc.DrawLine(grid, new Point(x, TopMargin), new Point(x, bottom));
                var label = ChartTokens.Text(text, 9.5, ChartTokens.AxisLabel, ppd);
                var lx = Math.Max(LeftMargin, Math.Min(right - label.Width, x - label.Width / 2));
                dc.DrawText(label, new Point(lx, h - label.Height));
            }

            // ----- starting-capital hairline, seam marker -----
            if (!_drawdown && _baseline is double baseline)
            {
                var y = ChartTokens.Snap(YOf(baseline), ppd);
                dc.DrawLine(ChartTokens.Hairline(BaselineBrush, ppd), new Point(LeftMargin, y), new Point(right, y));
            }
            if (_marker is DateTime m && m > minX && m < maxX)
            {
                var x = ChartTokens.Snap(XOf(m), ppd);
                var pen = new Pen(MarkerBrush, 1.0 / ppd) { DashStyle = new DashStyle(new double[] { 2, 3 }, 0) };
                pen.Freeze();
                dc.DrawLine(pen, new Point(x, TopMargin), new Point(x, bottom));
                if (!string.IsNullOrEmpty(_markerLabel))
                {
                    var label = ChartTokens.Text(_markerLabel, 9, MarkerBrush, ppd);
                    var lx = x + 4 + label.Width > right ? x - 4 - label.Width : x + 4;
                    dc.DrawText(label, new Point(lx, TopMargin));
                }
            }

            // ----- series -----
            if (_drawdown)
            {
                var pts = Thin(_mainScreen, _plotW);
                var zero = YOf(0);
                var area = new StreamGeometry();
                using (var ctx = area.Open())
                {
                    ctx.BeginFigure(new Point(pts[0].X, zero), true, true);
                    ctx.PolyLineTo(pts, true, true);
                    ctx.LineTo(new Point(pts[^1].X, zero), true, true);
                }
                area.Freeze();
                dc.DrawGeometry(DrawdownFill, null, area);
                var ddPen = new Pen(DrawdownBrush, 1.2) { LineJoin = PenLineJoin.Round };
                ddPen.Freeze();
                DrawSeries(dc, pts, ddPen);
            }
            else
            {
                if (_secondScreen.Length >= 2)
                {
                    var pen = new Pen(BenchmarkBrush, 1.2) { DashStyle = new DashStyle(new double[] { 3, 3 }, 0) };
                    pen.Freeze();
                    DrawSeries(dc, Thin(_secondScreen, _plotW), pen);
                }
                var eqPen = new Pen(EquityBrush, 1.8) { LineJoin = PenLineJoin.Round };
                eqPen.Freeze();
                DrawSeries(dc, Thin(_mainScreen, _plotW), eqPen);
            }
        }

        /// <summary>Year ticks at January 1 (step 1 year up to 6 years shown, then 2, 5, 10, 20, 50 — wider when the
        /// labels would crowd); month ticks for spans under two years.</summary>
        private static List<(DateTime At, string Label)> XTicks(DateTime min, DateTime max, double plotW)
        {
            var ticks = new List<(DateTime, string)>();
            var years = (max - min).TotalDays / 365.25;
            if (years >= 2)
            {
                var steps = new[] { 1, 2, 5, 10, 20, 50 };
                var limits = new[] { 6.0, 14, 35, 70, 140, double.MaxValue };
                var chosen = 50;
                for (var i = 0; i < steps.Length; i++)
                {
                    if (years > limits[i]) continue;
                    if (plotW / Math.Max(1, years / steps[i]) < 40) continue;   // ≥ 40 px between labels
                    chosen = steps[i];
                    break;
                }
                for (var y = min.Year; y <= max.Year; y++)
                {
                    if (y % chosen != 0) continue;
                    var at = new DateTime(y, 1, 1);
                    if (at >= min && at <= max) ticks.Add((at, y.ToString(ChartTokens.Inv)));
                }
            }
            else
            {
                var months = Math.Max(1, (max - min).TotalDays / 30.44);
                var step = 1;
                foreach (var s in new[] { 1, 2, 3, 6, 12 })
                {
                    step = s;
                    if (months / s <= 7 && plotW / Math.Max(1, months / s) >= 52) break;
                }
                for (var d = new DateTime(min.Year, min.Month, 1); d <= max; d = d.AddMonths(1))
                {
                    if ((d.Month - 1) % step != 0 || d < min) continue;
                    ticks.Add((d, d.ToString("yyyy-MM", ChartTokens.Inv)));
                }
            }
            return ticks;
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
        private static List<Point> Thin(Point[] points, double width)
        {
            var max = Math.Max(50, (int)(width * 2));
            if (points.Length <= max) return points.ToList();
            var step = (double)points.Length / max;
            var result = new List<Point>(max + 1);
            for (var i = 0.0; i < points.Length; i += step) result.Add(points[(int)i]);
            if (result[^1] != points[^1]) result.Add(points[^1]);
            return result;
        }

        // ---------- overlay: crosshair, dots, tooltip, focus ring ----------

        private void RedrawOverlay()
        {
            using var dc = _overlay.RenderOpen();
            var w = ActualWidth;
            var h = ActualHeight;
            if (w < 40 || h < 30) return;
            var ppd = Ppd;
            if (IsKeyboardFocused)
            {
                var ring = new Pen(ChartTokens.FocusRing, 1);
                ring.Freeze();
                dc.DrawRoundedRectangle(null, ring, new Rect(0.5, 0.5, w - 1, h - 1), 4, 4);
            }
            if (!_hasPlot || _cursor < 0 || _cursor >= _mainScreen.Length) return;

            var p = _mainScreen[_cursor];
            var date = _mainDates[_cursor];
            var x = ChartTokens.Snap(p.X, ppd);
            dc.DrawLine(ChartTokens.Hairline(ChartTokens.Crosshair, ppd), new Point(x, TopMargin), new Point(x, TopMargin + _plotH));

            var ringPen = new Pen(ChartTokens.DotRing, 1.5);
            ringPen.Freeze();
            int si = -1;
            if (_secondScreen.Length > 0)
            {
                si = ChartTokens.Nearest(_secondDates, date);
                if (si >= 0) dc.DrawEllipse(BenchmarkBrush, ringPen, _secondScreen[si], 3, 3);
            }
            dc.DrawEllipse(_drawdown ? DrawdownBrush : EquityBrush, ringPen, p, 3.5, 3.5);

            if (!_tooltip) return;
            var lines = new List<(string Label, string Value, Brush Brush)>
            {
                (_mainLabel, _drawdown ? ChartTokens.Percent(_mainValues[_cursor], 2) : ChartTokens.Money(_mainValues[_cursor]), _drawdown ? DrawdownBrush : EquityBrush),
            };
            if (si >= 0) lines.Add((_secondLabel, ChartTokens.Money(_secondValues[si]), BenchmarkBrush));
            if (_extraDates.Length > 0)
            {
                var ei = ChartTokens.Nearest(_extraDates, date);
                if (ei >= 0) lines.Add((_extraLabel, ChartTokens.Money(_extraValues[ei]), ChartTokens.TooltipText));
            }
            var pad = Math.Max(lines.Max(l => l.Label.Length), 4) + 2;
            var texts = new List<FormattedText> { ChartTokens.Text(date.ToString("yyyy-MM-dd ddd", ChartTokens.Inv), 10, ChartTokens.AxisLabel, ppd) };
            foreach (var (label, value, brush) in lines)
            {
                var t = ChartTokens.Text(label.PadRight(pad) + value, 10.5, ChartTokens.TooltipText, ppd);
                if (label.Length > 0) t.SetForegroundBrush(brush, 0, label.Length);
                texts.Add(t);
            }
            var boxW = texts.Max(t => t.WidthIncludingTrailingWhitespace) + 16;
            var boxH = texts.Sum(t => t.Height) + 10 + 2 * (texts.Count - 1);
            var right = LeftMargin + _plotW;
            var bx = x + 12 + boxW <= right ? x + 12 : x - 12 - boxW;
            bx = Math.Max(0, Math.Min(w - boxW, bx));
            var by = Math.Max(0, Math.Min(h - boxH, TopMargin + 4));
            var border = new Pen(ChartTokens.TooltipBorder, 1);
            border.Freeze();
            dc.DrawRoundedRectangle(ChartTokens.TooltipBg, border, new Rect(bx, by, boxW, boxH), 6, 6);
            var ty = by + 5;
            foreach (var t in texts)
            {
                dc.DrawText(t, new Point(bx + 8, ty));
                ty += t.Height + 2;
            }
        }

        private void SetCursor(int index, bool tooltip, bool broadcast)
        {
            _cursor = index;
            _tooltip = tooltip && index >= 0;
            RedrawOverlay();
            if (broadcast) _group?.Broadcast(this, index >= 0 && index < _mainDates.Length ? _mainDates[index] : null);
        }

        /// <summary>Crosshair driven by another chart of the group (line + dots, no tooltip).</summary>
        public void ShowSyncedCursor(DateTime? date)
        {
            if (date is not DateTime d || _mainDates.Length == 0 || d < _mainDates[0].AddDays(-3) || d > _mainDates[^1].AddDays(3))
            {
                SetCursor(-1, false, broadcast: false);
                return;
            }
            SetCursor(ChartTokens.Nearest(_mainDates, d), false, broadcast: false);
        }

        protected override void OnPreviewMouseWheel(MouseWheelEventArgs e)
        {
            base.OnPreviewMouseWheel(e);
            ChartTokens.ForwardWheel(this, e);   // the page scrolls; a keyboard-focused chart keeps the default routing
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (!_hasPlot || _mainDates.Length == 0) return;
            var x = Math.Max(LeftMargin, Math.Min(LeftMargin + _plotW, e.GetPosition(this).X));
            var date = _minX.AddSeconds((x - LeftMargin) / _plotW * _spanSec);
            var i = ChartTokens.Nearest(_mainDates, date);
            if (i != _cursor || !_tooltip) SetCursor(i, true, broadcast: true);
        }

        protected override void OnMouseLeave(MouseEventArgs e)
        {
            base.OnMouseLeave(e);
            if (IsKeyboardFocused) return; // the keyboard crosshair stays
            SetCursor(-1, false, broadcast: true);
        }

        protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
        {
            base.OnMouseLeftButtonDown(e);
            Focus();
        }

        protected override void OnGotKeyboardFocus(KeyboardFocusChangedEventArgs e)
        {
            base.OnGotKeyboardFocus(e);
            if (_hasPlot && _mainDates.Length > 0) SetCursor(_cursor >= 0 ? _cursor : _mainDates.Length - 1, true, broadcast: true);
            else RedrawOverlay();
        }

        protected override void OnLostKeyboardFocus(KeyboardFocusChangedEventArgs e)
        {
            base.OnLostKeyboardFocus(e);
            SetCursor(-1, false, broadcast: true);
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            base.OnKeyDown(e);
            if (!_hasPlot || _mainDates.Length == 0) return;
            var last = _mainDates.Length - 1;
            var cur = _cursor < 0 ? last : _cursor;
            var big = Math.Max(1, _mainDates.Length / 20);
            int? next = e.Key switch
            {
                Key.Left => Math.Max(0, cur - ((Keyboard.Modifiers & ModifierKeys.Shift) != 0 ? big : 1)),
                Key.Right => Math.Min(last, cur + ((Keyboard.Modifiers & ModifierKeys.Shift) != 0 ? big : 1)),
                Key.Home => 0,
                Key.End => last,
                Key.Escape => -1,
                _ => null,
            };
            if (next is int n)
            {
                SetCursor(n, n >= 0, broadcast: true);
                e.Handled = true;
            }
        }
    }

    /// <summary>IC decay: one bar per lag (mean rank IC, positive up / negative down), lag label below, Y grid at
    /// "nice" IC levels with labels in a left margin. Lags without a value are drawn as an empty slot "n/a".</summary>
    internal sealed class IcDecayChart : FrameworkElement
    {
        private static readonly Brush PosBrush = ChartTokens.Frozen(Color.FromRgb(0x6F, 0xA9, 0x8A));
        private static readonly Brush NegBrush = ChartTokens.Frozen(Color.FromRgb(0xC1, 0x60, 0x4B));
        private static readonly Brush ZeroBrush = ChartTokens.Frozen(Color.FromRgb(0x2A, 0x2A, 0x2A));
        private const double LeftMargin = 44, RightMargin = 8, TopMargin = 12, BottomMargin = 16;

        private List<(string Label, double? Value)> _bars = new();

        /// <summary>Number format of the value printed on each bar (IC: "0.000"; stage 4 fold Sharpe: "0.00").</summary>
        public string ValueFormat { get; set; } = "0.000";

        public IcDecayChart()
        {
            SnapsToDevicePixels = true;
            MinHeight = 60;
        }

        protected override void OnPreviewMouseWheel(MouseWheelEventArgs e)
        {
            base.OnPreviewMouseWheel(e);
            ChartTokens.ForwardWheel(this, e);
        }

        public void SetData(IEnumerable<(string Label, double? Value)> bars)
        {
            _bars = bars.Select(b => (b.Label, b.Value is double v && !double.IsNaN(v) && !double.IsInfinity(v) ? b.Value : null)).ToList();
            InvalidateVisual();
        }

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            var w = ActualWidth;
            var h = ActualHeight;
            if (w < 40 || h < 30 || _bars.Count == 0) return;
            dc.DrawRectangle(Brushes.Transparent, null, new Rect(0, 0, w, h));
            var ppd = VisualTreeHelper.GetDpi(this).PixelsPerDip;
            var inv = ChartTokens.Inv;

            var values = _bars.Where(b => b.Value.HasValue).Select(b => b.Value!.Value).ToList();
            var maxAbs = values.Count == 0 ? 0.01 : Math.Max(0.005, values.Max(v => Math.Abs(v)));
            var step = ChartTokens.NiceStep(maxAbs, 2);
            var bound = Math.Ceiling(maxAbs / step - 1e-9) * step;
            var plotH = Math.Max(10, h - TopMargin - BottomMargin);
            var plotW = Math.Max(10, w - LeftMargin - RightMargin);
            double YOf(double v) => TopMargin + (1 - (v + bound) / (2 * bound)) * plotH;

            var grid = ChartTokens.Hairline(ChartTokens.GridLine, ppd);
            for (var k = -(int)Math.Round(bound / step); k <= (int)Math.Round(bound / step); k++)
            {
                var v = k * step;
                var y = ChartTokens.Snap(YOf(v), ppd);
                dc.DrawLine(k == 0 ? ChartTokens.Hairline(ZeroBrush, ppd) : grid, new Point(LeftMargin, y), new Point(LeftMargin + plotW, y));
                var text = (v < 0 ? ChartTokens.Minus : "") + Math.Abs(v).ToString("0.###", inv);
                var label = ChartTokens.Text(text, 9.5, ChartTokens.AxisLabel, ppd);
                dc.DrawText(label, new Point(LeftMargin - 6 - label.Width, Math.Max(0, Math.Min(h - label.Height, YOf(v) - label.Height / 2))));
            }

            var zeroY = YOf(0);
            var slot = plotW / _bars.Count;
            for (var i = 0; i < _bars.Count; i++)
            {
                var (label, value) = _bars[i];
                var cx = LeftMargin + slot * (i + 0.5);
                var bw = Math.Min(28, slot * 0.6);
                if (value is double v)
                {
                    var bh = Math.Abs(v) / bound * (plotH / 2);
                    var rect = v >= 0 ? new Rect(cx - bw / 2, zeroY - bh, bw, bh) : new Rect(cx - bw / 2, zeroY, bw, bh);
                    dc.DrawRectangle(v >= 0 ? PosBrush : NegBrush, null, rect);
                    var vt = ChartTokens.Text(v.ToString(ValueFormat, inv), 9, ChartTokens.AxisLabel, ppd);
                    var vy = v >= 0 ? Math.Max(0, zeroY - bh - vt.Height) : Math.Min(h - BottomMargin - vt.Height, zeroY + bh);
                    dc.DrawText(vt, new Point(cx - vt.Width / 2, vy));
                }
                else
                {
                    var na = ChartTokens.Text("n/a", 9, ChartTokens.AxisLabel, ppd);
                    dc.DrawText(na, new Point(cx - na.Width / 2, zeroY - na.Height - 2));
                }
                var lt = ChartTokens.Text(label, 9.5, ChartTokens.AxisLabel, ppd);
                dc.DrawText(lt, new Point(cx - lt.Width / 2, h - lt.Height));
            }
        }
    }
}

using System;
using System.Windows;
using System.Windows.Media;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// Low–high range bar with a marker for the current value (symbol header "DAY RANGE", key stats
    /// "52-WEEK RANGE"; plan §1.2 / §2.2). Drawn in OnRender: a thin track, the filled part from the low to the
    /// marker, and the marker. Values that are missing (null, non-finite, high &lt; low) draw the track only — no
    /// invented position. A value outside [low, high] (e.g. a live trade beyond the last range update) is drawn at
    /// the nearest end. Brushes come from the view's tokens (set by <see cref="ResearchView"/>).
    /// The bar itself carries no text: the low/high/value numbers are TextBlocks next to it (never colour alone).
    /// </summary>
    public sealed class RangeBar : FrameworkElement
    {
        private double? _low;
        private double? _high;
        private double? _value;

        public Brush? TrackBrush { get; set; }
        public Brush? FillBrush { get; set; }
        public Brush? MarkerBrush { get; set; }

        public RangeBar()
        {
            Height = 10;
            SnapsToDevicePixels = true;
            IsHitTestVisible = true; // tooltips
        }

        /// <summary>Sets the range and the marker value; redraws.</summary>
        public void SetRange(double? low, double? high, double? value)
        {
            _low = Finite(low);
            _high = Finite(high);
            _value = Finite(value);
            InvalidateVisual();
        }

        private static double? Finite(double? v) => v is double d && double.IsFinite(d) ? d : null;

        protected override void OnRender(DrawingContext dc)
        {
            var width = ActualWidth;
            var height = ActualHeight;
            if (width <= 6 || height <= 0) return;
            dc.DrawRectangle(Brushes.Transparent, null, new Rect(0, 0, width, height)); // whole bar hit-testable (tooltip)
            var mid = Math.Round(height / 2) + 0.5;
            if (TrackBrush is not Brush track) return; // brushes are set by the view (tokens) before the bar is shown
            dc.DrawRectangle(track, null, new Rect(0, mid - 1.5, width, 3));

            if (_low is not double low || _high is not double high || high < low || _value is not double value) return;
            var t = high > low ? (value - low) / (high - low) : 0.5;
            t = Math.Clamp(t, 0, 1);
            var x = 3 + t * (width - 6);
            if (FillBrush != null) dc.DrawRectangle(FillBrush, null, new Rect(0, mid - 1.5, Math.Max(0, x), 3));
            if (MarkerBrush is Brush marker) dc.DrawEllipse(marker, null, new Point(x, mid), 3.5, 3.5);
        }
    }
}

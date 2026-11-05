using System.Windows;
using System.Windows.Media;

namespace UFOS.ai.ModuleTiles
{
    /// <summary>
    /// Building blocks for the modules' Illustration motifs (TILES_TASK mode 3: a designed, static vector motif per module from
    /// the design system — no information). Every colour is a design-system brush key (<see cref="TileBrushes"/>); the motif is
    /// drawn on a fixed 160 × 90 canvas and scaled uniformly by the tile.
    /// </summary>
    public static class TileArt
    {
        public const double Width = 160, Height = 90;

        /// <summary>A new motif with the fixed canvas size (a transparent frame keeps the bounds stable).</summary>
        public static DrawingGroup Canvas()
        {
            var g = new DrawingGroup();
            g.Children.Add(new GeometryDrawing(Brushes.Transparent, null, new RectangleGeometry(new Rect(0, 0, Width, Height))));
            return g;
        }

        public static Drawing Polyline(string brushKey, double thickness, double opacity, params Point[] points)
        {
            var geo = new StreamGeometry();
            using (var c = geo.Open())
            {
                c.BeginFigure(points[0], false, false);
                for (var i = 1; i < points.Length; i++) c.LineTo(points[i], true, true);
            }
            geo.Freeze();
            var pen = new Pen(TileBrushes.Get(brushKey), thickness) { LineJoin = PenLineJoin.Round, StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round };
            return Faded(new GeometryDrawing(null, pen, geo), opacity);
        }

        public static Drawing Dot(string brushKey, double x, double y, double radius, double opacity = 1)
            => Faded(new GeometryDrawing(TileBrushes.Get(brushKey), null, new EllipseGeometry(new Point(x, y), radius, radius)), opacity);

        public static Drawing Ring(string brushKey, double x, double y, double radius, double thickness, double opacity = 1)
            => Faded(new GeometryDrawing(null, new Pen(TileBrushes.Get(brushKey), thickness), new EllipseGeometry(new Point(x, y), radius, radius)), opacity);

        public static Drawing Box(string brushKey, double x, double y, double w, double h, double radius, double opacity = 1)
            => Faded(new GeometryDrawing(TileBrushes.Get(brushKey), null, new RectangleGeometry(new Rect(x, y, w, h), radius, radius)), opacity);

        private static Drawing Faded(Drawing d, double opacity)
        {
            if (opacity >= 0.999) return d;
            var g = new DrawingGroup { Opacity = opacity };
            g.Children.Add(d);
            return g;
        }
    }
}

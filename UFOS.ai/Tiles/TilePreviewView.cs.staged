using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using UFOS.ai.ModuleTiles;

namespace UFOS.ai.Tiles
{
    /// <summary>
    /// The preview area of one module tile (TILES_API §6) plus its "⋯" options button. Built in code; design-system brushes only
    /// (<see cref="TileBrushes"/>). The host (<see cref="ModuleTileHost"/>) decides WHAT is shown; this view only renders it.
    /// </summary>
    internal sealed class TilePreviewView
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private readonly ContentControl _content = new() { Focusable = false, IsTabStop = false };

        public TilePreviewView(string displayName)
        {
            DisplayName = displayName;
            Root = new Border
            {
                CornerRadius = new CornerRadius(6),
                BorderThickness = new Thickness(1),
                BorderBrush = TileBrushes.Get("SubtleBorderBrush"),
                Background = TileBrushes.Get("ModuleTileBackgroundBrush"),
                ClipToBounds = true,
                Padding = new Thickness(8, 6, 8, 6),
                Child = _content,
                IsHitTestVisible = false,   // the tile's own click opens the module; the preview is display only
            };
            MenuButton = new Button
            {
                Content = "⋯",
                Width = 22,
                Height = 22,
                Padding = new Thickness(0),
                FontSize = 14,
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0),
                Foreground = TileBrushes.Get("SecondaryForegroundBrush"),
                Cursor = Cursors.Hand,
                HorizontalAlignment = HorizontalAlignment.Right,
                VerticalAlignment = VerticalAlignment.Bottom,
                Template = FlatTemplate(),
            };
            AutomationProperties.SetName(MenuButton, "Preview options for " + displayName);
            MenuButton.MouseEnter += (_, _) => MenuButton.Foreground = TileBrushes.Get("PrimaryForegroundBrush");
            MenuButton.MouseLeave += (_, _) => MenuButton.Foreground = TileBrushes.Get("SecondaryForegroundBrush");
        }

        public string DisplayName { get; }

        /// <summary>The preview area (placed above the tile's name).</summary>
        public Border Root { get; }

        /// <summary>The "⋯" button (placed bottom right of the tile). Its Click is handled — it never opens the module.</summary>
        public Button MenuButton { get; }

        /// <summary>Text for the tile's AutomationName ("Preview: …"), set with every render.</summary>
        public string AutomationText { get; private set; } = "Preview: loading";

        // ------------------------------------------------------------------ renders

        /// <summary>Live figures. <paramref name="status"/> = "as of HH:MM" / "backend off — as of HH:MM" / a problem line.</summary>
        public void ShowLive(TileLiveData data, string status, TileTone statusTone, bool sandbox)
        {
            var stack = new StackPanel();
            var parts = new List<string>();
            if (sandbox)
            {
                stack.Children.Add(Chip("SANDBOX", "CautionBrush"));
                parts.Add("sandbox data");
            }
            foreach (var f in data.Figures.Take(3))
            {
                var row = new DockPanel { Margin = new Thickness(0, 0, 0, 1), LastChildFill = true };
                var value = new TextBlock
                {
                    Text = f.Value,
                    FontFamily = new FontFamily("Consolas"),
                    FontSize = 13,
                    Foreground = TileBrushes.Get(TileBrushes.KeyFor(f.Tone)),
                    TextTrimming = TextTrimming.CharacterEllipsis,
                    HorizontalAlignment = HorizontalAlignment.Right,
                };
                DockPanel.SetDock(value, Dock.Right);
                row.Children.Add(value);
                if (f.AiGenerated)
                {
                    var ai = new TextBlock { Text = "✦ AI ", FontSize = 9.5, Foreground = TileBrushes.Get("AiAccentBrush"), VerticalAlignment = VerticalAlignment.Center };
                    DockPanel.SetDock(ai, Dock.Right);
                    row.Children.Add(ai);
                }
                row.Children.Add(new TextBlock
                {
                    Text = f.Label,
                    FontSize = 10,
                    Foreground = TileBrushes.Get("MutedForegroundBrush"),
                    VerticalAlignment = VerticalAlignment.Center,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                });
                stack.Children.Add(row);
                if (!string.IsNullOrWhiteSpace(f.Detail))
                    stack.Children.Add(Small(f.Detail!, "QuietForegroundBrush"));
                parts.Add((f.AiGenerated ? "AI-generated " : "") + f.Label + " " + f.Value + (string.IsNullOrWhiteSpace(f.Detail) ? "" : " (" + f.Detail + ")"));
            }
            if (data.Dots is { Count: > 0 } dots)
            {
                var wrap = new WrapPanel { Margin = new Thickness(0, 2, 0, 0) };
                foreach (var d in dots.Take(6))
                {
                    var item = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 8, 0) };
                    item.Children.Add(new Ellipse { Width = 6, Height = 6, Fill = TileBrushes.Get(d.BrushKey), Margin = new Thickness(0, 0, 4, 0), VerticalAlignment = VerticalAlignment.Center });
                    item.Children.Add(new TextBlock { Text = d.Label + " " + d.Count.ToString(Inv), FontSize = 10, FontFamily = new FontFamily("Consolas"), Foreground = TileBrushes.Get("SecondaryForegroundBrush") });
                    wrap.Children.Add(item);
                    parts.Add(d.Label + " " + d.Count.ToString(Inv));
                }
                stack.Children.Add(wrap);
            }
            if (data.Spark is { Count: >= 2 } spark) stack.Children.Add(Sparkline(spark));
            stack.Children.Add(Small(status, TileBrushes.KeyFor(statusTone) == "PrimaryForegroundBrush" ? "QuietForegroundBrush" : TileBrushes.KeyFor(statusTone)));
            _content.Content = stack;
            AutomationText = "Preview: Live — " + (parts.Count == 0 ? "no figures" : string.Join("; ", parts)) + ", " + status;
        }

        /// <summary>A message instead of figures / picture (e.g. "no data yet", "Last view: no picture yet — …").</summary>
        public void ShowMessage(string modeLabel, string text, TileTone tone = TileTone.Neutral)
        {
            var t = new TextBlock
            {
                Text = text,
                FontSize = 10.5,
                TextWrapping = TextWrapping.Wrap,
                Foreground = TileBrushes.Get(tone == TileTone.Neutral ? "QuietForegroundBrush" : TileBrushes.KeyFor(tone)),
                VerticalAlignment = VerticalAlignment.Center,
            };
            _content.Content = t;
            AutomationText = "Preview: " + modeLabel + " — " + text;
        }

        /// <summary>The stored blurred picture (read into memory — the file is never locked) with its caption.</summary>
        public void ShowLastView(TileLastViewInfo info, string? problem)
        {
            var bytes = File.ReadAllBytes(info.PngPath);
            var img = new BitmapImage();
            img.BeginInit();
            img.CacheOption = BitmapCacheOption.OnLoad;
            img.StreamSource = new MemoryStream(bytes);
            img.EndInit();
            img.Freeze();
            var image = new Image { Source = img, Stretch = Stretch.UniformToFill, VerticalAlignment = VerticalAlignment.Top };
            RenderOptions.SetBitmapScalingMode(image, BitmapScalingMode.HighQuality);
            var caption = "Last view · " + info.TakenAt.ToLocalTime().ToString("HH:mm, dd.MM.", Inv);
            var grid = new Grid();
            grid.Children.Add(image);
            var cap = Small(caption + (problem == null ? "" : " · " + problem), problem == null ? "SecondaryForegroundBrush" : "CautionBrush");
            cap.VerticalAlignment = VerticalAlignment.Bottom;
            cap.Background = TileBrushes.Get("ModuleTileBackgroundBrush");
            cap.Padding = new Thickness(3, 1, 3, 1);
            cap.HorizontalAlignment = HorizontalAlignment.Left;
            grid.Children.Add(cap);
            _content.Content = grid;
            AutomationText = "Preview: last view from " + info.TakenAt.ToLocalTime().ToString("HH:mm, dd.MM.", Inv) + " (blurred picture)" + (problem == null ? "" : ", " + problem);
        }

        public void ShowIllustration(Drawing drawing, string? note)
        {
            var image = new Image { Source = new DrawingImage(drawing), Stretch = Stretch.Uniform };
            if (note == null)
            {
                _content.Content = image;
                AutomationText = "Preview: illustration";
                return;
            }
            var dock = new DockPanel { LastChildFill = true };
            var n = Small(note, "QuietForegroundBrush");
            DockPanel.SetDock(n, Dock.Bottom);
            dock.Children.Add(n);
            dock.Children.Add(image);
            _content.Content = dock;
            AutomationText = "Preview: illustration (" + note + ")";
        }

        // ------------------------------------------------------------------ menu

        /// <summary>Opens the options menu: Live · Last view · Illustration (+ Delete last view when a picture exists).</summary>
        public void OpenMenu(TileMode current, string? liveReason, string? lastViewReason, bool hasPicture, Action<TileMode> choose, Action deletePicture)
        {
            var menu = new ContextMenu { PlacementTarget = MenuButton, Placement = PlacementMode.Bottom };
            menu.Items.Add(ModeItem("Live", TileMode.Live, current, liveReason, choose));
            menu.Items.Add(ModeItem("Last view", TileMode.LastView, current, lastViewReason, choose));
            menu.Items.Add(ModeItem("Illustration", TileMode.Illustration, current, null, choose));
            if (hasPicture)
            {
                menu.Items.Add(new Separator());
                var del = new MenuItem { Header = "Delete last view" };
                AutomationProperties.SetHelpText(del, "Deletes the stored blurred picture of " + DisplayName);
                del.Click += (_, _) => deletePicture();
                menu.Items.Add(del);
            }
            menu.IsOpen = true;
        }

        private static MenuItem ModeItem(string header, TileMode mode, TileMode current, string? disabledReason, Action<TileMode> choose)
        {
            var item = new MenuItem { Header = header, IsCheckable = false, IsChecked = mode == current && disabledReason == null };
            if (disabledReason != null)
            {
                item.IsEnabled = false;
                item.ToolTip = disabledReason;
                ToolTipService.SetShowOnDisabled(item, true);
                AutomationProperties.SetHelpText(item, disabledReason);
                item.Header = header + " — " + disabledReason;
            }
            else
            {
                item.Click += (_, _) => choose(mode);
            }
            return item;
        }

        // ------------------------------------------------------------------ helpers

        private static TextBlock Small(string text, string brushKey) => new()
        {
            Text = text,
            FontSize = 10,
            Foreground = TileBrushes.Get(brushKey),
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 2, 0, 0),
        };

        private static Border Chip(string text, string brushKey) => new()
        {
            BorderBrush = TileBrushes.Get(brushKey),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(3),
            Padding = new Thickness(4, 0, 4, 0),
            Margin = new Thickness(0, 0, 0, 3),
            HorizontalAlignment = HorizontalAlignment.Left,
            Child = new TextBlock { Text = text, FontSize = 9, FontWeight = FontWeights.SemiBold, Foreground = TileBrushes.Get(brushKey) },
        };

        /// <summary>1.5 px line, no axes, scaled to an 18 px strip.</summary>
        private static FrameworkElement Sparkline(IReadOnlyList<double> points)
        {
            var pts = points.Where(double.IsFinite).TakeLast(60).ToList();
            var box = new Viewbox { Stretch = Stretch.Fill, Height = 18, Margin = new Thickness(0, 3, 0, 0) };
            if (pts.Count < 2) return box;
            double min = pts.Min(), max = pts.Max();
            var range = max - min < 1e-12 ? 1 : max - min;
            var line = new Polyline { Stroke = TileBrushes.Get("AccentBlueBrush"), StrokeThickness = 1.5, StrokeLineJoin = PenLineJoin.Round };
            for (var i = 0; i < pts.Count; i++) line.Points.Add(new Point(i * 4.0, 40 - (pts[i] - min) / range * 40));
            var canvas = new Canvas { Width = (pts.Count - 1) * 4.0, Height = 40 };
            canvas.Children.Add(line);
            box.Child = canvas;
            return box;
        }

        private static ControlTemplate FlatTemplate()
        {
            var bd = new FrameworkElementFactory(typeof(Border));
            bd.SetValue(Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
            bd.SetValue(Border.CornerRadiusProperty, new CornerRadius(4));
            var cp = new FrameworkElementFactory(typeof(ContentPresenter));
            cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Center);
            cp.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            bd.AppendChild(cp);
            return new ControlTemplate(typeof(Button)) { VisualTree = bd };
        }
    }
}

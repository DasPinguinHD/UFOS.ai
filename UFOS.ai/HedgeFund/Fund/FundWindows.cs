using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Fund
{
    /// <summary>Colors of the Hedge Fund dashboard, for UI built in code (same values as HedgeFundWindow.xaml).</summary>
    internal static class FundPalette
    {
        public static readonly Brush Long = Make(0xFF6FA98A);
        public static readonly Brush LongDim = Make(0xFF4E7A64);
        public static readonly Brush Short = Make(0xFFC1604B);
        public static readonly Brush ShortDim = Make(0xFF8E4739);
        public static readonly Brush Neutral = Make(0xFF9A9A9A);
        public static readonly Brush Risk = Make(0xFFD9A54B);
        public static readonly Brush Focus = Make(0xFF7C8CF8);
        public static readonly Brush Panel = Make(0xFF1A1A1A);
        public static readonly Brush PanelBorder = Make(0xFF2A2A2A);
        public static readonly Brush Hairline = Make(0xFF242424);
        public static readonly Brush WindowBg = Make(0xFF111111);
        public static readonly Brush Text = Make(0xFFBFBFBF);
        public static readonly Brush TextBright = Make(0xFFEDEDED);
        public static readonly Brush Dim = Make(0xFF9A9A9A);
        public static readonly Brush Faint = Make(0xFF6A6A6A);
        public static readonly Brush Ai = Make(0xFFB98CE8);          // AiAccentBrush (DesignSystem.xaml)
        public static readonly Brush AiSurface = Make(0x14B98CE8);   // AiSurfaceBrush
        public static readonly Brush Kill = Make(0xFFED6A5A);
        public static readonly Brush KillSurface = Make(0x26ED6A5A);
        public static readonly Brush ButtonBg = Make(0xFF242424);
        public static readonly Brush ButtonBorder = Make(0xFF333333);

        public static Brush Make(uint argb)
        {
            var b = new SolidColorBrush(Color.FromArgb((byte)(argb >> 24), (byte)(argb >> 16), (byte)(argb >> 8), (byte)argb));
            b.Freeze();
            return b;
        }

        public static Brush ForAction(string? action) => (action ?? "").ToUpperInvariant() switch
        {
            "LONG" => Long,
            "SHORT" => Short,
            _ => Neutral,
        };

        public static Brush ForSigned(double? value) => value is double v ? (v > 0.004 ? Long : v < -0.004 ? Short : Dim) : Dim;
    }

    /// <summary>Small factories for UI built in code (popups, list rows).</summary>
    internal static class FundUi
    {
        public static readonly FontFamily Mono = new("Consolas");

        public static TextBlock Text(string? text, double size = 11.5, Brush? brush = null, bool bold = false, bool mono = false, bool wrap = true)
        {
            var tb = new TextBlock
            {
                Text = text ?? "",
                FontSize = size,
                Foreground = brush ?? FundPalette.Dim,
                TextWrapping = wrap ? TextWrapping.Wrap : TextWrapping.NoWrap,
            };
            if (bold) tb.FontWeight = FontWeights.SemiBold;
            if (mono) tb.FontFamily = Mono;
            if (!wrap) tb.TextTrimming = TextTrimming.CharacterEllipsis;
            return tb;
        }

        public static Border Panel(UIElement child, Thickness? padding = null, Thickness? margin = null)
            => new()
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = padding ?? new Thickness(14, 12, 14, 12),
                Margin = margin ?? new Thickness(0, 0, 0, 10),
                Child = child,
            };

        /// <summary>Rounded dark button (same look as RoundedDarkButtonStyle), built without XAML.</summary>
        public static Button MakeButton(string text, Brush? background = null, Brush? border = null, Brush? foreground = null)
        {
            var bd = new FrameworkElementFactory(typeof(Border), "bd");
            bd.SetValue(Border.CornerRadiusProperty, new CornerRadius(6));
            bd.SetValue(Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
            bd.SetValue(Border.BorderBrushProperty, new TemplateBindingExtension(Control.BorderBrushProperty));
            bd.SetValue(Border.BorderThicknessProperty, new TemplateBindingExtension(Control.BorderThicknessProperty));
            bd.SetValue(UIElement.SnapsToDevicePixelsProperty, true);
            var cp = new FrameworkElementFactory(typeof(ContentPresenter));
            cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Center);
            cp.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            cp.SetValue(FrameworkElement.MarginProperty, new TemplateBindingExtension(Control.PaddingProperty));
            bd.AppendChild(cp);
            var template = new ControlTemplate(typeof(Button)) { VisualTree = bd };
            var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(new Setter(UIElement.OpacityProperty, 0.88, "bd"));
            template.Triggers.Add(hover);
            var disabled = new Trigger { Property = UIElement.IsEnabledProperty, Value = false };
            disabled.Setters.Add(new Setter(UIElement.OpacityProperty, 0.45, "bd"));
            template.Triggers.Add(disabled);

            return new Button
            {
                Template = template,
                Content = text,
                Padding = new Thickness(12, 5, 12, 5),
                MinHeight = 24,
                Background = background ?? FundPalette.ButtonBg,
                BorderBrush = border ?? FundPalette.ButtonBorder,
                BorderThickness = new Thickness(1),
                Foreground = foreground ?? FundPalette.Text,
                FontSize = 12,
                Cursor = Cursors.Hand,
            };
        }

        public static TextBox Input(string initial, bool mono = true)
        {
            var box = new TextBox
            {
                Text = initial,
                FontSize = 12,
                Foreground = FundPalette.Text,
                Background = FundPalette.WindowBg,
                BorderBrush = FundPalette.Hairline,
                BorderThickness = new Thickness(1),
                Padding = new Thickness(8, 5, 8, 5),
                CaretBrush = FundPalette.Text,
            };
            if (mono) box.FontFamily = Mono;
            return box;
        }

        public static Border Hairline(double top = 0, double bottom = 0)
            => new() { BorderBrush = FundPalette.Hairline, BorderThickness = new Thickness(0, 1, 0, 0), Margin = new Thickness(0, top, 0, bottom) };

        /// <summary>A key/value line: label left, value right.</summary>
        public static Grid KeyValue(string label, string value, Brush? valueBrush = null, double size = 11.5)
        {
            var grid = new Grid { Margin = new Thickness(0, 2, 0, 2) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var l = Text(label, size, FundPalette.Dim, wrap: false);
            var v = Text(value, size, valueBrush ?? FundPalette.Text, mono: true, wrap: false);
            v.Margin = new Thickness(10, 0, 0, 0);
            Grid.SetColumn(v, 1);
            grid.Children.Add(l);
            grid.Children.Add(v);
            return grid;
        }

        /// <summary>
        /// AI-labelled block (CLAUDE.md "AI content labelling"): badge before the text,
        /// caveat after it, violet container, screen-reader name starting with "AI-generated".
        /// Returns a plain block when <paramref name="provenance"/> does not mark AI output.
        /// </summary>
        public static Border TextBlockMaybeAi(string heading, string? text, JsonElement? provenance, string? fallbackModel,
                                              string? fallbackTs, bool carried = false)
        {
            var stack = new StackPanel();
            var isAi = FundAi.TryRead(provenance, fallbackModel, fallbackTs, out var model, out var at);
            if (isAi)
            {
                var badge = Text(AiContent.Badge(model, at) + (carried ? " · CARRIED" : ""), 10, FundPalette.Ai, bold: true, mono: true);
                badge.Margin = new Thickness(0, 0, 0, 4);
                stack.Children.Add(badge);
            }
            else
            {
                var badge = Text("Rule-based · no AI", 10, FundPalette.Faint, mono: true);
                badge.Margin = new Thickness(0, 0, 0, 4);
                stack.Children.Add(badge);
            }
            if (!string.IsNullOrWhiteSpace(heading))
            {
                stack.Children.Add(Text(heading, 11.5, FundPalette.Text, bold: true));
            }
            var body = Text(string.IsNullOrWhiteSpace(text) ? "(no text)" : text, 11.5, FundPalette.Dim);
            body.LineHeight = 17;
            stack.Children.Add(body);
            if (isAi)
            {
                var caveat = Text(AiContent.Caveat, 10, FundPalette.Faint);
                caveat.FontStyle = FontStyles.Italic;
                caveat.Margin = new Thickness(0, 4, 0, 0);
                stack.Children.Add(caveat);
                AutomationProperties.SetName(body, AiContent.AutomationName(model, at) + " " + (text ?? ""));
            }
            return new Border
            {
                Background = isAi ? FundPalette.AiSurface : Brushes.Transparent,
                BorderBrush = isAi ? FundPalette.Ai : FundPalette.Hairline,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 7, 10, 8),
                Margin = new Thickness(0, 0, 0, 8),
                Child = stack,
            };
        }
    }

    /// <summary>Reads the backend's provenance objects (SPEC §0).</summary>
    internal static class FundAi
    {
        /// <summary>
        /// True when the provenance marks AI output ("ai_generated": true). Model/time come from
        /// the provenance, falling back to the given model / timestamp.
        /// </summary>
        public static bool TryRead(JsonElement? provenance, string? fallbackModel, string? fallbackTs,
                                   out string? model, out DateTimeOffset? generatedAt)
        {
            model = fallbackModel;
            generatedAt = FundFormat.ParseTs(fallbackTs);
            if (provenance is not JsonElement p || p.ValueKind != JsonValueKind.Object) return false;
            if (!AiContent.TryReadProvenance(p, out var m, out var at)) return false;
            if (!string.IsNullOrWhiteSpace(m)) model = m;
            if (at.HasValue) generatedAt = at;
            return true;
        }
    }

    /// <summary>
    /// Borderless popup with the module's chrome (black 18 px title bar, rounded corners)
    /// and the project's full-screen rules (CLAUDE.md "Borderless windows must handle full
    /// screen"): StateChanged pads the root border by the resize frame + 4 and drops the
    /// corner radii while maximized; dragging the title bar of a maximized popup restores
    /// it under the cursor (same logic as HedgeFundWindow.TitleBar_MouseMove).
    /// </summary>
    internal class FundPopupWindow : Window
    {
        private readonly Border _root;
        private readonly Border _titleBar;
        private readonly Border _bodyHost;
        private bool _restoreOnDrag;
        private Point _restoreDragStart;

        public FundPopupWindow(Window? owner, string title, double width, double height, bool resizable)
        {
            Title = title;
            Width = width;
            Height = height;
            MinWidth = Math.Min(width, 360);
            MinHeight = Math.Min(height, 180);
            WindowStyle = WindowStyle.None;
            AllowsTransparency = true;
            Background = Brushes.Transparent;
            ResizeMode = resizable ? ResizeMode.CanResizeWithGrip : ResizeMode.NoResize;
            ShowInTaskbar = false;
            if (owner != null && owner.IsLoaded)
            {
                Owner = owner;
                WindowStartupLocation = WindowStartupLocation.CenterOwner;
            }
            else
            {
                WindowStartupLocation = WindowStartupLocation.CenterScreen;
            }

            // Title bar
            var titleRow = new Grid();
            var left = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0) };
            left.Children.Add(new System.Windows.Shapes.Ellipse { Width = 5, Height = 5, Fill = FundPalette.Neutral, VerticalAlignment = VerticalAlignment.Center });
            var titleText = FundUi.Text(title, 10, FundPalette.Dim, wrap: false);
            titleText.Margin = new Thickness(6, 0, 0, 0);
            titleText.VerticalAlignment = VerticalAlignment.Center;
            left.Children.Add(titleText);
            titleRow.Children.Add(left);

            var close = FundUi.MakeButton("✕", FundPalette.Kill, Brushes.Transparent, Brushes.White);
            close.Width = 14;
            close.Height = 14;
            close.MinHeight = 14;
            close.Padding = new Thickness(0);
            close.FontSize = 8;
            close.FontWeight = FontWeights.Bold;
            close.HorizontalAlignment = HorizontalAlignment.Right;
            close.VerticalAlignment = VerticalAlignment.Center;
            close.Margin = new Thickness(0, 0, 10, 0);
            close.Template = RoundTemplate();
            AutomationProperties.SetName(close, "Close");
            close.Click += (_, _) => Close();
            titleRow.Children.Add(close);

            _titleBar = new Border { Background = Brushes.Black, CornerRadius = new CornerRadius(8, 8, 0, 0), Height = 18, Child = titleRow };
            _titleBar.MouseLeftButtonDown += TitleBar_MouseLeftButtonDown;
            _titleBar.MouseMove += TitleBar_MouseMove;
            _titleBar.MouseLeftButtonUp += TitleBar_MouseLeftButtonUp;

            _bodyHost = new Border();
            var layout = new Grid();
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            Grid.SetRow(_bodyHost, 1);
            layout.Children.Add(_titleBar);
            layout.Children.Add(_bodyHost);
            ApplyCompactScrollBars(layout);

            _root = new Border
            {
                Background = FundPalette.WindowBg,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                SnapsToDevicePixels = true,
                Child = layout,
            };
            Content = _root;

            StateChanged += Window_StateChanged;
            PreviewKeyDown += (_, e) =>
            {
                if (e.Key == Key.Escape) { e.Handled = true; Close(); }
            };
        }

        protected void SetBody(UIElement body) => _bodyHost.Child = body;

        private static ControlTemplate RoundTemplate()
        {
            var bd = new FrameworkElementFactory(typeof(Border), "bd");
            bd.SetValue(Border.CornerRadiusProperty, new CornerRadius(999));
            bd.SetValue(Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
            var cp = new FrameworkElementFactory(typeof(ContentPresenter));
            cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Center);
            cp.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            bd.AppendChild(cp);
            var template = new ControlTemplate(typeof(Button)) { VisualTree = bd };
            var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(new Setter(UIElement.OpacityProperty, 0.85, "bd"));
            template.Triggers.Add(hover);
            return template;
        }

        /// <summary>Same compact dark scroll bar as the dashboard (parsed once; default bars if that fails).</summary>
        private static Style? _scrollBarStyle;
        private static bool _scrollBarStyleTried;

        private static void ApplyCompactScrollBars(FrameworkElement scope)
        {
            if (!_scrollBarStyleTried)
            {
                _scrollBarStyleTried = true;
                try
                {
                    const string xaml =
                        "<Style xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation' TargetType='ScrollBar'>" +
                        "<Setter Property='Background' Value='Transparent'/><Setter Property='Width' Value='8'/>" +
                        "<Setter Property='Template'><Setter.Value><ControlTemplate TargetType='ScrollBar'>" +
                        "<Grid Background='Transparent'><Border Margin='2,0,2,0' Background='#FF242424' CornerRadius='4'/>" +
                        "<Track x:Name='PART_Track' IsDirectionReversed='True' xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml'>" +
                        "<Track.DecreaseRepeatButton><RepeatButton Command='ScrollBar.PageUpCommand' Opacity='0' IsHitTestVisible='False'/></Track.DecreaseRepeatButton>" +
                        "<Track.Thumb><Thumb><Thumb.Template><ControlTemplate TargetType='Thumb'><Border Background='#FF4A4A4A' CornerRadius='4' MinHeight='28'/></ControlTemplate></Thumb.Template></Thumb></Track.Thumb>" +
                        "<Track.IncreaseRepeatButton><RepeatButton Command='ScrollBar.PageDownCommand' Opacity='0' IsHitTestVisible='False'/></Track.IncreaseRepeatButton>" +
                        "</Track></Grid></ControlTemplate></Setter.Value></Setter></Style>";
                    _scrollBarStyle = (Style)XamlReader.Parse(xaml);
                }
                catch
                {
                    _scrollBarStyle = null;
                }
            }
            if (_scrollBarStyle != null) scope.Resources[typeof(ScrollBar)] = _scrollBarStyle;
        }

        private void Window_StateChanged(object? sender, EventArgs e)
        {
            if (WindowState == WindowState.Maximized)
            {
                var frame = SystemParameters.WindowResizeBorderThickness;
                const double paddedBorder = 4; // SM_CXPADDEDBORDER, not exposed by SystemParameters
                _root.Margin = new Thickness(frame.Left + paddedBorder, frame.Top + paddedBorder,
                                             frame.Right + paddedBorder, frame.Bottom + paddedBorder);
                _root.CornerRadius = new CornerRadius(0);
                _titleBar.CornerRadius = new CornerRadius(0);
            }
            else
            {
                _root.Margin = new Thickness(0);
                _root.CornerRadius = new CornerRadius(8);
                _titleBar.CornerRadius = new CornerRadius(8, 8, 0, 0);
            }
        }

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (e.ClickCount == 2 && ResizeMode != ResizeMode.NoResize)
            {
                WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                return;
            }
            if (WindowState == WindowState.Maximized)
            {
                _restoreOnDrag = true;
                _restoreDragStart = e.GetPosition(this);
                ((UIElement)sender).CaptureMouse();
                return;
            }
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseMove(object sender, MouseEventArgs e)
        {
            if (!_restoreOnDrag || e.LeftButton != MouseButtonState.Pressed) return;
            var pos = e.GetPosition(this);
            if (Math.Abs(pos.X - _restoreDragStart.X) < SystemParameters.MinimumHorizontalDragDistance &&
                Math.Abs(pos.Y - _restoreDragStart.Y) < SystemParameters.MinimumVerticalDragDistance) return;

            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();

            var ratioX = ActualWidth > 0 ? pos.X / ActualWidth : 0.5;
            var restoreWidth = RestoreBounds.Width;
            var toDip = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice ?? Matrix.Identity;
            var cursor = toDip.Transform(PointToScreen(pos));

            WindowState = WindowState.Normal;
            Left = cursor.X - restoreWidth * ratioX;
            Top = cursor.Y - 9; // middle of the 18 px title bar
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
        {
            if (!_restoreOnDrag) return;
            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();
        }
    }

    /// <summary>Confirmation / small form dialogs (modal). Nothing is sent before the user confirms.</summary>
    internal sealed class FundFormDialog : FundPopupWindow
    {
        private readonly List<TextBox> _inputs = new();
        public string[] Values => _inputs.Select(t => t.Text ?? "").ToArray();

        public FundFormDialog(Window? owner, string title, string message, IReadOnlyList<(string Label, string Initial)> fields,
                              string confirmText, bool danger, string? footnote)
            : base(owner, title, 460, 200 + 58 * fields.Count + (footnote == null ? 0 : 24), resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text(title, 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(message, 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 12);
            msg.LineHeight = 18;
            stack.Children.Add(msg);

            foreach (var (label, initial) in fields)
            {
                var l = FundUi.Text(label, 10.5, FundPalette.Faint);
                l.Margin = new Thickness(0, 0, 0, 4);
                stack.Children.Add(l);
                var box = FundUi.Input(initial);
                box.Margin = new Thickness(0, 0, 0, 10);
                AutomationProperties.SetName(box, label);
                _inputs.Add(box);
                stack.Children.Add(box);
            }

            if (!string.IsNullOrWhiteSpace(footnote))
            {
                var note = FundUi.Text(footnote, 10.5, FundPalette.Faint);
                note.FontStyle = FontStyles.Italic;
                note.Margin = new Thickness(0, 0, 0, 12);
                stack.Children.Add(note);
            }

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var cancel = FundUi.MakeButton("Cancel");
            cancel.IsCancel = true;
            cancel.Click += (_, _) => { DialogResult = false; };
            var ok = danger
                ? FundUi.MakeButton(confirmText, FundPalette.KillSurface, FundPalette.Kill, FundPalette.Kill)
                : FundUi.MakeButton(confirmText, FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            ok.IsDefault = !danger; // a dangerous action is never confirmed by a stray Enter
            ok.Margin = new Thickness(8, 0, 0, 0);
            ok.Click += (_, _) => { DialogResult = true; };
            buttons.Children.Add(cancel);
            buttons.Children.Add(ok);
            stack.Children.Add(buttons);
            SetBody(stack);

            Loaded += (_, _) =>
            {
                if (_inputs.Count > 0) { _inputs[0].Focus(); _inputs[0].SelectAll(); }
                else cancel.Focus();
            };
        }
    }

    internal static class FundDialogs
    {
        private const string LogModule = "HedgeFund";

        /// <summary>
        /// Modal yes/no confirmation. True = confirmed. If the styled dialog can't be shown
        /// (e.g. owner not loaded yet, a theme resource missing), the failure is logged and a
        /// plain MessageBox (Yes/No) asks instead — a safety action such as the kill switch
        /// must never silently do nothing.
        /// </summary>
        public static bool Confirm(Window? owner, string title, string message, string confirmText, bool danger = false, string? footnote = null)
        {
            try
            {
                var dialog = new FundFormDialog(owner, title, message, Array.Empty<(string, string)>(), confirmText, danger, footnote);
                return dialog.ShowDialog() == true;
            }
            catch (Exception ex)
            {
                LogDialogFailure(title, ex);
                return FallbackYesNo(owner, title, message, confirmText, danger, footnote);
            }
        }

        /// <summary>
        /// Modal form; null = cancelled, else the field values in order. Fallback when the styled
        /// dialog fails: a MessageBox Yes/No — "Yes" returns the fields' initial values, so e.g.
        /// the kill switch still works (with its default reason).
        /// </summary>
        public static string[]? Form(Window? owner, string title, string message, IReadOnlyList<(string Label, string Initial)> fields,
                                     string confirmText, bool danger = false, string? footnote = null)
        {
            try
            {
                var dialog = new FundFormDialog(owner, title, message, fields, confirmText, danger, footnote);
                return dialog.ShowDialog() == true ? dialog.Values : null;
            }
            catch (Exception ex)
            {
                LogDialogFailure(title, ex);
                var extra = fields.Count == 0
                    ? ""
                    : "\n\n(The input form could not be shown; the defaults are used: "
                      + string.Join(", ", fields.Select(f => f.Label + " = " + (string.IsNullOrWhiteSpace(f.Initial) ? "(empty)" : f.Initial))) + ")";
                return FallbackYesNo(owner, title, message + extra, confirmText, danger, footnote)
                    ? fields.Select(f => f.Initial ?? "").ToArray()
                    : null;
            }
        }

        private static void LogDialogFailure(string title, Exception ex)
        {
            try { AppLog.Warn(LogModule, "Hedge Fund dialog \"" + title + "\" could not be shown — falling back to a standard message box.", ex); } catch { }
        }

        private static bool FallbackYesNo(Window? owner, string title, string message, string confirmText, bool danger, string? footnote)
        {
            try
            {
                var text = message + "\n\nChoose \"Yes\" to " + confirmText.Trim().TrimStart('■', '▶', ' ').TrimEnd('.', '…') + ", \"No\" to cancel."
                           + (string.IsNullOrWhiteSpace(footnote) ? "" : "\n\n" + footnote);
                var icon = danger ? MessageBoxImage.Warning : MessageBoxImage.Question;
                var result = owner != null && owner.IsLoaded
                    ? MessageBox.Show(owner, text, title, MessageBoxButton.YesNo, icon, MessageBoxResult.No)
                    : MessageBox.Show(text, title, MessageBoxButton.YesNo, icon, MessageBoxResult.No);
                return result == MessageBoxResult.Yes;
            }
            catch (Exception ex)
            {
                try { AppLog.Error(LogModule, "Hedge Fund confirmation could not be shown at all (\"" + title + "\").", ex); } catch { }
                return false;
            }
        }

        /// <summary>Simple message popup (non-modal is not needed here).</summary>
        public static void Info(Window? owner, string title, string message)
        {
            try
            {
                var popup = new FundInfoWindow(owner, title, message);
                popup.ShowDialog();
            }
            catch (Exception ex)
            {
                // Results of safety actions (kill switch, stop engine) must not vanish silently.
                LogDialogFailure(title, ex);
                try
                {
                    if (owner != null && owner.IsLoaded) MessageBox.Show(owner, message, title, MessageBoxButton.OK, MessageBoxImage.Information);
                    else MessageBox.Show(message, title, MessageBoxButton.OK, MessageBoxImage.Information);
                }
                catch { }
            }
        }
    }

    internal sealed class FundInfoWindow : FundPopupWindow
    {
        public FundInfoWindow(Window? owner, string title, string message) : base(owner, title, 440, 220, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text(title, 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(message, 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 14);
            msg.LineHeight = 18;
            stack.Children.Add(msg);
            var ok = FundUi.MakeButton("OK");
            ok.IsDefault = true;
            ok.IsCancel = true;
            ok.HorizontalAlignment = HorizontalAlignment.Right;
            ok.Click += (_, _) => Close();
            stack.Children.Add(ok);
            SetBody(stack);
            Loaded += (_, _) => ok.Focus();
        }
    }

    /// <summary>Details of one engine run: agents, recommendations and orders (GET /fund/runs/{id}).</summary>
    internal sealed class FundRunDetailsWindow : FundPopupWindow
    {
        public FundRunDetailsWindow(Window? owner, FundRunDetails details)
            : base(owner, "Hedge Fund · Run #" + (details.Run?.Id.ToString(CultureInfo.InvariantCulture) ?? "?"), 760, 640, resizable: true)
        {
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 20) };
            var run = details.Run;
            stack.Children.Add(FundUi.Text("Run #" + (run?.Id.ToString(CultureInfo.InvariantCulture) ?? "?"), 15, FundPalette.TextBright, bold: true));
            var sub = FundUi.Text("Paper trading · simulated · not investment advice. Times are local.", 10.5, FundPalette.Faint);
            sub.Margin = new Thickness(0, 4, 0, 12);
            stack.Children.Add(sub);

            if (run != null)
            {
                var info = new StackPanel();
                info.Children.Add(FundUi.KeyValue("Started", FundFormat.LocalTime(run.StartedAt, "yyyy-MM-dd HH:mm:ss")));
                info.Children.Add(FundUi.KeyValue("Finished", FundFormat.LocalTime(run.FinishedAt, "yyyy-MM-dd HH:mm:ss")));
                info.Children.Add(FundUi.KeyValue("Status", run.Status ?? FundFormat.Dash, StatusBrush(run.Status)));
                info.Children.Add(FundUi.KeyValue("Trigger", TriggerLabel(run.Trigger),
                    IsRiskWatch(run.Trigger) ? FundPalette.Risk : null));
                info.Children.Add(FundUi.KeyValue("Market", run.MarketOpen == true ? "open" : run.MarketOpen == false ? "closed" : FundFormat.Dash));
                info.Children.Add(FundUi.KeyValue("Judge mode", run.JudgeMode ?? FundFormat.Dash));
                info.Children.Add(FundUi.KeyValue("Models", run.Models is JsonElement m ? FundFormat.Plain(m) : FundFormat.Dash));
                info.Children.Add(FundUi.KeyValue("AI cost", FundFormat.Usd(run.CostUsd)));
                info.Children.Add(FundUi.KeyValue("Params version", run.ParamsVersion.HasValue ? "v" + run.ParamsVersion.Value.ToString(CultureInfo.InvariantCulture) : FundFormat.Dash));
                if (!string.IsNullOrWhiteSpace(run.Error))
                    info.Children.Add(FundUi.KeyValue("Error", run.Error!, FundPalette.Short));
                stack.Children.Add(FundUi.Panel(info));
            }

            // Recommendations
            stack.Children.Add(Section("Recommendations"));
            var recs = details.Recommendations ?? new List<FundRecommendation>();
            if (recs.Count == 0) stack.Children.Add(FundUi.Text("No recommendations in this run.", 11.5, FundPalette.Faint));
            foreach (var r in recs.OrderBy(r => r.Symbol))
            {
                var heading = $"{r.Symbol} · {r.Action} · conf {FundFormat.Fraction(r.Confidence, 0)} · target {FundFormat.Fraction(r.TargetWeight, 1, signed: true)}" +
                              $" · {r.Source} · {r.Status}" + (string.IsNullOrWhiteSpace(r.SkipReason) ? "" : " (" + r.SkipReason + ")");
                var block = FundUi.TextBlockMaybeAi(heading, r.Rationale, r.Provenance, r.Model, r.Ts,
                                                    carried: string.Equals(r.Source, "carried", StringComparison.OrdinalIgnoreCase));
                var cost = r.Usage?.EffectiveCost;
                if (cost.HasValue && block.Child is StackPanel sp && FundAi.TryRead(r.Provenance, r.Model, r.Ts, out _, out _))
                {
                    var batchNote = r.Usage?.BatchCostUsd is double batch ? " (share of a " + FundFormat.Usd(batch) + " batch call)" : "";
                    sp.Children.Add(FundUi.Text($"Cost: {FundFormat.Usd(cost)} · {r.Usage?.PromptTokens ?? 0} in / {r.Usage?.CompletionTokens ?? 0} out tokens" + batchNote,
                                                10, FundPalette.Faint, mono: true));
                }
                stack.Children.Add(block);
            }

            // Agents
            stack.Children.Add(Section("Agent signals"));
            var agents = details.Agents ?? new List<FundAgentSignal>();
            if (agents.Count == 0) stack.Children.Add(FundUi.Text("No agent signals stored for this run.", 11.5, FundPalette.Faint));
            foreach (var group in agents.GroupBy(a => a.Symbol ?? "?").OrderBy(g => g.Key))
            {
                var box = new StackPanel();
                box.Children.Add(FundUi.Text(group.Key, 12, FundPalette.Text, bold: true, mono: true));
                foreach (var a in group.OrderBy(a => AgentOrder(a.Agent)))
                {
                    var line = $"{FundFormat.Label(a.Agent)}: score {(a.Score is double sc ? sc.ToString("+0.00;-0.00;0.00", CultureInfo.InvariantCulture) : "—")} · conf {FundFormat.Fraction(a.Confidence, 0)} · {a.Source}";
                    var isAiSource = string.Equals(a.Source, "llm", StringComparison.OrdinalIgnoreCase) ||
                                     string.Equals(a.Source, "cached", StringComparison.OrdinalIgnoreCase);
                    if (isAiSource)
                    {
                        box.Children.Add(FundUi.TextBlockMaybeAi(line, a.Summary, a.Provenance, null, null,
                                                                 carried: string.Equals(a.Source, "cached", StringComparison.OrdinalIgnoreCase)));
                    }
                    else
                    {
                        var t = FundUi.Text(line + " — " + (a.Summary ?? ""), 11, FundPalette.Dim);
                        t.Margin = new Thickness(0, 2, 0, 2);
                        box.Children.Add(t);
                    }
                }
                stack.Children.Add(FundUi.Panel(box));
            }

            // Orders
            stack.Children.Add(Section("Orders"));
            var orders = details.Orders ?? new List<FundOrder>();
            if (orders.Count == 0) stack.Children.Add(FundUi.Text("No orders in this run.", 11.5, FundPalette.Faint));
            foreach (var o in orders)
            {
                stack.Children.Add(FundUi.Text(FundOrderText.Describe(o), 11, FundPalette.Dim, mono: true));
            }

            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
        }

        /// <summary>Run trigger "risk-watch" (2026-09-26): light check outside US hours — no agents, no AI, no new orders.</summary>
        internal static bool IsRiskWatch(string? trigger)
            => string.Equals((trigger ?? "").Replace('_', '-'), "risk-watch", StringComparison.OrdinalIgnoreCase);

        internal static string TriggerLabel(string? trigger)
            => IsRiskWatch(trigger)
                ? "risk watch · no AI (no agents, no new orders)"
                : string.IsNullOrWhiteSpace(trigger) ? FundFormat.Dash : trigger!;

        internal static int AgentOrder(string? agent) => (agent ?? "").ToLowerInvariant() switch
        {
            "technical" => 0,
            "macro" => 1,
            "sentiment" => 2,
            "risk" => 3,
            _ => 4,
        };

        internal static Brush StatusBrush(string? status) => (status ?? "").ToLowerInvariant() switch
        {
            "ok" => FundPalette.Long,
            "partial" or "skipped" => FundPalette.Risk,
            "error" => FundPalette.Short,
            "running" => FundPalette.Focus,
            _ => FundPalette.Dim,
        };

        private static TextBlock Section(string title)
        {
            var t = FundUi.Text(title, 13, FundPalette.Text, bold: true);
            t.Margin = new Thickness(2, 10, 0, 8);
            return t;
        }
    }

    internal static class FundOrderText
    {
        /// <summary>"14:05 BUY 10 SPY · filled @ 615.20 · fees $1.54 · autopilot".</summary>
        public static string Describe(FundOrder o)
        {
            var when = FundFormat.LocalTime(o.Ts ?? o.FilledAt, "MM-dd HH:mm");
            var text = $"{when} {(o.Side ?? "?").ToUpperInvariant()} {FundFormat.Qty(o.Qty)} {o.Symbol} · {o.Status ?? "?"}";
            if (o.FillPrice.HasValue) text += " @ " + FundFormat.Num(o.FillPrice, 2);
            if (o.Fees.HasValue && o.Fees.Value > 0) text += " · fees " + FundFormat.Money(o.Fees);
            if (!string.IsNullOrWhiteSpace(o.Origin)) text += " · " + o.Origin;
            if (!string.IsNullOrWhiteSpace(o.RejectReason)) text += " · " + o.RejectReason;
            return text;
        }

        /// <summary>Short result line for a submitted order.</summary>
        public static string Result(FundOrder o)
        {
            var side = (o.Side ?? "?").ToUpperInvariant();
            return (o.Status ?? "").ToLowerInvariant() switch
            {
                "filled" => $"{side} {FundFormat.Qty(o.Qty)} {o.Symbol} filled @ {FundFormat.Num(o.FillPrice, 2)}" +
                            (o.Fees is double f && f > 0 ? " · fees " + FundFormat.Money(f) : ""),
                "pending" => $"{side} {FundFormat.Qty(o.Qty)} {o.Symbol} pending — executes at the next market open",
                "rejected" => $"{side} {o.Symbol} rejected: {o.RejectReason ?? "no reason given"}",
                "cancelled" => $"{side} {o.Symbol} cancelled" + (string.IsNullOrWhiteSpace(o.RejectReason) ? "" : ": " + o.RejectReason),
                _ => $"{side} {FundFormat.Qty(o.Qty)} {o.Symbol} · {o.Status}",
            };
        }
    }

    /// <summary>Backtest result (rule-only, SPEC §12).</summary>
    internal sealed class FundBacktestResultWindow : FundPopupWindow
    {
        public FundBacktestResultWindow(Window? owner, FundBacktestResult result)
            : base(owner, "Hedge Fund · Backtest", 760, 640, resizable: true)
        {
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 20) };
            stack.Children.Add(FundUi.Text("Backtest · " + (result.Start ?? "?") + " → " + (result.End ?? "?"), 15, FundPalette.TextBright, bold: true));
            var sub = FundUi.Text("Rule-based · no AI. Technical + macro agents with risk sizing on daily bars, fills at the next open, " +
                                  "fees and borrow cost included, no dividends. Hypothetical, simulated results — past performance " +
                                  "does not indicate future results. Not investment advice.", 10.5, FundPalette.Faint);
            sub.Margin = new Thickness(0, 4, 0, 12);
            stack.Children.Add(sub);
            if (result.Symbols != null && result.Symbols.Count > 0)
            {
                var s = FundUi.Text("Symbols: " + string.Join(", ", result.Symbols), 11, FundPalette.Dim, mono: true);
                s.Margin = new Thickness(0, 0, 0, 10);
                stack.Children.Add(s);
            }

            var p = result.Portfolio;
            var stats = new StackPanel();
            stats.Children.Add(FundUi.Text("Portfolio", 12, FundPalette.Text, bold: true));
            stats.Children.Add(FundUi.KeyValue("Total return", FundFormat.Pct(p?.TotalReturnPct, 1, signed: true), FundPalette.ForSigned(p?.TotalReturnPct)));
            stats.Children.Add(FundUi.KeyValue("CAGR", FundFormat.Pct(p?.CagrPct, 1, signed: true), FundPalette.ForSigned(p?.CagrPct)));
            stats.Children.Add(FundUi.KeyValue("Max drawdown", FundFormat.Pct(p?.MaxDrawdownPct, 1)));
            stats.Children.Add(FundUi.KeyValue("Sharpe", FundFormat.Num(p?.Sharpe, 2)));
            stats.Children.Add(FundUi.KeyValue("Trades", FundFormat.Qty(p?.Trades)));
            stats.Children.Add(FundUi.KeyValue("Hit rate", FundFormat.Pct(p?.HitRatePct, 1)));
            stats.Children.Add(FundUi.KeyValue("Exposure", FundFormat.Pct(p?.ExposurePct, 1)));
            stack.Children.Add(FundUi.Panel(stats));

            var chart = new EquityChart { Height = 180 };
            var eq = result.Equity ?? new List<FundBacktestPoint>();
            chart.SetData(eq.Select(x => (ParseDate(x.Date), x.Equity)),
                          eq.Select(x => (ParseDate(x.Date), x.Benchmark)),
                          eq.FirstOrDefault()?.Equity,
                          "No equity series in the result.");
            var chartBox = new StackPanel();
            chartBox.Children.Add(chart);
            var legend = FundUi.Text("Blue: strategy equity · grey dashed: benchmark (equal-weight buy & hold)", 10, FundPalette.Faint);
            legend.Margin = new Thickness(0, 6, 0, 0);
            chartBox.Children.Add(legend);
            stack.Children.Add(FundUi.Panel(chartBox));

            if (result.Benchmark is JsonElement bench && bench.ValueKind == JsonValueKind.Object)
            {
                var b = new StackPanel();
                b.Children.Add(FundUi.Text("Benchmark", 12, FundPalette.Text, bold: true));
                foreach (var prop in bench.EnumerateObject()) AddJsonLines(b, prop.Name, prop.Value, 0);
                stack.Children.Add(FundUi.Panel(b));
            }

            if (result.PerSymbol != null && result.PerSymbol.Count > 0)
            {
                var ps = new StackPanel();
                ps.Children.Add(FundUi.Text("Per symbol", 12, FundPalette.Text, bold: true));
                foreach (var item in result.PerSymbol)
                {
                    if (item.ValueKind != JsonValueKind.Object) { ps.Children.Add(FundUi.Text(FundFormat.Plain(item), 11, FundPalette.Dim, mono: true)); continue; }
                    var name = item.TryGetProperty("symbol", out var sym) ? FundFormat.Plain(sym) : "?";
                    var parts = item.EnumerateObject().Where(x => x.Name != "symbol")
                                    .Select(x => FundFormat.Label(x.Name) + " " + FundFormat.Plain(x.Value));
                    var t = FundUi.Text(name + " · " + string.Join(" · ", parts), 11, FundPalette.Dim, mono: true);
                    t.Margin = new Thickness(0, 2, 0, 2);
                    ps.Children.Add(t);
                }
                stack.Children.Add(FundUi.Panel(ps));
            }

            if (result.Notes != null && result.Notes.Count > 0)
            {
                var n = new StackPanel();
                n.Children.Add(FundUi.Text("Notes", 12, FundPalette.Text, bold: true));
                foreach (var note in result.Notes) n.Children.Add(FundUi.Text("• " + note, 11, FundPalette.Dim));
                stack.Children.Add(FundUi.Panel(n));
            }

            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
        }

        private static void AddJsonLines(StackPanel target, string name, JsonElement value, int depth)
        {
            if (value.ValueKind == JsonValueKind.Object && depth < 2)
            {
                var head = FundUi.Text(FundFormat.Label(name), 11, FundPalette.Dim, bold: true);
                head.Margin = new Thickness(depth * 12, 4, 0, 0);
                target.Children.Add(head);
                foreach (var p in value.EnumerateObject()) AddJsonLines(target, p.Name, p.Value, depth + 1);
                return;
            }
            var kv = FundUi.KeyValue(FundFormat.Label(name), FundFormat.Plain(value), size: 11);
            kv.Margin = new Thickness(depth * 12, 1, 0, 1);
            target.Children.Add(kv);
        }

        private static DateTime ParseDate(string? date)
        {
            var t = FundFormat.ParseTs(date);
            return t.HasValue ? t.Value.UtcDateTime : DateTime.MinValue;
        }
    }
}

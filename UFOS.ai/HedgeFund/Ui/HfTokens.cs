using System;
using System.Globalization;
using System.Text;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Ui
{
    /// <summary>
    /// Stage 11 (UFOS.ai/docs/tasks/STAGE11_OVERVIEW_TASK.md §2): the ONE place for the colours and sizes of design D3, for UI
    /// built in code (the HedgeFund project cannot use Styles/DesignSystem.xaml). Mapped to the window brushes (FundPalette =
    /// HedgeFundWindow.xaml) where those exist. Colour rules (contract §0): red/green (Long/Short) only for the user's own P&amp;L;
    /// market changes neutral grey with a sign; blue/orange only for the gamma regime; violet only for AI; amber only for
    /// warnings / attention.
    /// </summary>
    internal static class HfTokens
    {
        // ---- surfaces
        public static readonly Brush CardBg = FundPalette.Make(0xFF141414);
        public static readonly Brush CardBorder = FundPalette.Make(0xFF232323);
        public static readonly Brush InsetBg = FundPalette.Make(0xFF181818);
        public static readonly Brush InsetBorder = FundPalette.Make(0xFF1F1F1F);
        public static readonly Brush Divider = FundPalette.Make(0xFF222222);
        public static readonly Brush ButtonBg = FundPalette.ButtonBg;
        public static readonly Brush ButtonBorder = FundPalette.ButtonBorder;
        public static readonly Brush ButtonSelectedBg = FundPalette.Make(0xFF1F2937);

        // ---- text
        public static readonly Brush TextTitle = FundPalette.TextBright;            // #EDEDED
        public static readonly Brush TextValue = FundPalette.Make(0xFFECECEC);
        public static readonly Brush Text = FundPalette.Make(0xFFD6D6D6);
        public static readonly Brush TextMuted = FundPalette.Make(0xFFA8A8A8);
        public static readonly Brush TextDim = FundPalette.Dim;                    // #9A9A9A
        public static readonly Brush TextFaint = FundPalette.Make(0xFF6F6F6F);

        // ---- semantic colours (contract §0)
        /// <summary>Market changes: neutral grey with a sign (never red / green).</summary>
        public static readonly Brush MarketChange = FundPalette.Make(0xFFA8A8A8);
        /// <summary>The user's own P&amp;L only.</summary>
        public static readonly Brush PnlUp = FundPalette.Long;
        public static readonly Brush PnlDown = FundPalette.Short;
        /// <summary>Gamma regime only: positive (blue) / negative (orange).</summary>
        public static readonly Brush GammaPositive = FundPalette.Make(0xFF3D86C0);
        public static readonly Brush GammaNegative = FundPalette.Make(0xFFC97A2E);
        /// <summary>AI only (AiAccentBrush / AiSurfaceBrush).</summary>
        public static readonly Brush Ai = FundPalette.Ai;
        public static readonly Brush AiSurface = FundPalette.AiSurface;
        /// <summary>Warnings / attention only (RiskBrush) and the attention strip's tints.</summary>
        public static readonly Brush Attention = FundPalette.Risk;
        public static readonly Brush AttentionBg = FundPalette.Make(0xFF131109);
        public static readonly Brush AttentionBorder = FundPalette.Make(0xFF2E2612);
        public static readonly Brush AttentionText = FundPalette.Make(0xFFE3D3A8);
        public static readonly Brush AttentionMuted = FundPalette.Make(0xFFA8935E);
        public static readonly Brush AttentionCount = FundPalette.Make(0xFFF0C860);
        /// <summary>Errors / failures of a status: amber like warnings — red / green stay reserved for the user's own P&amp;L (review 2026-11-06).</summary>
        public static readonly Brush Error = FundPalette.Risk;
        /// <summary>Links / keyboard focus (FocusBrush).</summary>
        public static readonly Brush Link = FundPalette.Focus;

        // ---- charts
        public static readonly Brush ChartLine = FundPalette.Make(0xFF8A8A8A);
        public static readonly Brush ChartGrid = FundPalette.Make(0xFF2A2A2A);
        public static readonly Brush ChartLabel = FundPalette.Make(0xFF7D7D7D);
        public static readonly Brush ChartMarker = FundPalette.Make(0xFFD6D6D6);
        public static readonly Brush ChartTrack = FundPalette.Make(0xFF232323);
        public static readonly Brush ChartBand = FundPalette.Make(0xFF4A4A4A);

        // ---- sizes
        public const double CardRadius = 12;
        public const double CardPadding = 16;
        public const double Gap = 16;
        public const double TitleSize = 14;
        public const double BodySize = 13;
        public const double SmallSize = 11.5;
        public const double ValueSize = 15;
        public const double HeroSize = 30;

        public static readonly FontFamily Mono = FundUi.Mono;
        public static readonly Typeface MonoFace = new(new FontFamily("Consolas"), FontStyles.Normal, FontWeights.Normal, FontStretches.Normal);
    }

    /// <summary>Status dot kinds (colour rules of contract §0).</summary>
    internal enum DotKind { None, Neutral, Warning, Error, Ai, GammaPositive, GammaNegative }

    /// <summary>Small code-built helpers shared by the stage-11 components and views.</summary>
    internal static class HfUi
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static ControlTemplate? _linkTemplate;
        private static ControlTemplate? _flatTemplate;

        /// <summary>One-line text with ellipsis (and the full text as tooltip), or wrapping text.</summary>
        public static TextBlock Text(string? text, double size, Brush brush, bool bold = false, bool mono = false, bool wrap = false)
        {
            var tb = new TextBlock
            {
                Text = text ?? "",
                FontSize = size,
                Foreground = brush,
                TextWrapping = wrap ? TextWrapping.Wrap : TextWrapping.NoWrap,
                TextTrimming = TextTrimming.CharacterEllipsis,
                VerticalAlignment = VerticalAlignment.Center,
            };
            if (bold) tb.FontWeight = FontWeights.SemiBold;
            if (mono) tb.FontFamily = HfTokens.Mono;
            SetTip(tb, text);
            return tb;
        }

        /// <summary>Sets the text and keeps the tooltip in step (long texts may be trimmed).</summary>
        public static void SetText(TextBlock tb, string? text, Brush? brush = null)
        {
            tb.Text = text ?? "";
            if (brush != null) tb.Foreground = brush;
            SetTip(tb, text);
        }

        private static void SetTip(TextBlock tb, string? text) => tb.ToolTip = !string.IsNullOrEmpty(text) && text!.Length >= 14 ? text : null;

        /// <summary>"not available (reason)" — the one wording for missing data (CLAUDE.md, contract §0).</summary>
        public static string NotAvailable(string? reason)
            => "not available (" + (string.IsNullOrWhiteSpace(reason) ? "no reason given" : Plain(reason, 400)) + ")";

        /// <summary>Untrusted text as plain one-line text: control characters removed, whitespace collapsed, capped.</summary>
        public static string Plain(string? text, int max = 300)
        {
            if (string.IsNullOrEmpty(text)) return "";
            var sb = new StringBuilder(Math.Min(text.Length, max + 1));
            var space = false;
            foreach (var c in text)
            {
                if (char.IsControl(c) || char.IsWhiteSpace(c))
                {
                    if (!space && sb.Length > 0) { sb.Append(' '); space = true; }
                    continue;
                }
                space = false;
                sb.Append(c);
                if (sb.Length >= max) { sb.Append('…'); break; }
            }
            return sb.ToString().Trim();
        }

        /// <summary>"<1 min", "12 min", "3 h 05 min", "2 d 4 h".</summary>
        public static string Age(double? minutes)
        {
            if (minutes is not double m || double.IsNaN(m) || double.IsInfinity(m)) return "age unknown";
            if (m < 0) m = 0;
            if (m < 1) return "<1 min";
            if (m < 60) return Math.Floor(m).ToString("0", Inv) + " min";
            if (m < 48 * 60)
            {
                var h = (int)Math.Floor(m / 60);
                var mm = (int)Math.Floor(m - h * 60);
                return h.ToString(Inv) + " h " + mm.ToString("00", Inv) + " min";
            }
            var d = (int)Math.Floor(m / 1440);
            var hh = (int)Math.Floor((m - d * 1440) / 60);
            return d.ToString(Inv) + " d " + hh.ToString(Inv) + " h";
        }

        /// <summary>A number with an explicit sign ("+0.42", "−1.30"; typographic minus) and an optional unit suffix.</summary>
        public static string Signed(double? value, int decimals, string suffix = "")
        {
            if (value is not double v || double.IsNaN(v) || double.IsInfinity(v)) return "—";
            var r = Math.Round(v, decimals);
            var abs = Math.Abs(v).ToString("N" + decimals, Inv);
            var sign = r > 0 ? "+" : r < 0 ? FundFormat.Minus : "";
            return sign + abs + suffix;
        }

        public static Brush DotBrush(DotKind kind) => kind switch
        {
            DotKind.Warning => HfTokens.Attention,
            DotKind.Error => HfTokens.Error,
            DotKind.Ai => HfTokens.Ai,
            DotKind.GammaPositive => HfTokens.GammaPositive,
            DotKind.GammaNegative => HfTokens.GammaNegative,
            _ => HfTokens.TextFaint,
        };

        public static Ellipse Dot(DotKind kind, double size = 7)
            => new()
            {
                Width = size,
                Height = size,
                Fill = DotBrush(kind),
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0, 0, 7, 0),
                Visibility = kind == DotKind.None ? Visibility.Collapsed : Visibility.Visible,
            };

        public static void SetDot(Ellipse dot, DotKind kind)
        {
            dot.Fill = DotBrush(kind);
            dot.Visibility = kind == DotKind.None ? Visibility.Collapsed : Visibility.Visible;
        }

        /// <summary>
        /// A text link ("Risk Lab →"): a keyboard-reachable button without chrome (focus ring in FocusBrush), plain-text content,
        /// AutomationName set.
        /// </summary>
        public static Button Link(string text, Action onClick, string automationName, Brush? brush = null, double size = 12)
        {
            var b = new Button
            {
                Template = LinkTemplate(),
                Content = text,
                Foreground = brush ?? HfTokens.Link,
                FontSize = size,
                Cursor = Cursors.Hand,
                Padding = new Thickness(2, 0, 2, 0),
                VerticalAlignment = VerticalAlignment.Center,
                Background = Brushes.Transparent,
                BorderBrush = Brushes.Transparent,
            };
            AutomationProperties.SetName(b, automationName);
            b.Click += (_, _) => onClick();
            return b;
        }

        /// <summary>A chrome-less, keyboard-reachable button around arbitrary content (clickable rows / tiles).</summary>
        public static Button FlatButton(UIElement content, Action onClick, string automationName)
        {
            var b = new Button
            {
                Template = FlatTemplate(),
                Content = content,
                Cursor = Cursors.Hand,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                Background = Brushes.Transparent,
                BorderBrush = Brushes.Transparent,
                Padding = new Thickness(0),
            };
            AutomationProperties.SetName(b, automationName);
            b.Click += (_, _) => onClick();
            return b;
        }

        private static ControlTemplate LinkTemplate() => _linkTemplate ??= BuildTemplate(underlineOnHover: true);
        private static ControlTemplate FlatTemplate() => _flatTemplate ??= BuildTemplate(underlineOnHover: false);

        private static ControlTemplate BuildTemplate(bool underlineOnHover)
        {
            var bd = new FrameworkElementFactory(typeof(Border), "bd");
            bd.SetValue(Border.CornerRadiusProperty, new CornerRadius(4));
            bd.SetValue(Border.BackgroundProperty, Brushes.Transparent);
            bd.SetValue(Border.BorderBrushProperty, Brushes.Transparent);
            bd.SetValue(Border.BorderThicknessProperty, new Thickness(1));
            bd.SetValue(Border.PaddingProperty, new System.Windows.TemplateBindingExtension(Control.PaddingProperty));
            var cp = new FrameworkElementFactory(typeof(ContentPresenter), "cp");
            cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, new System.Windows.TemplateBindingExtension(Control.HorizontalContentAlignmentProperty));
            cp.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
            bd.AppendChild(cp);
            var t = new ControlTemplate(typeof(Button)) { VisualTree = bd };
            var focus = new Trigger { Property = UIElement.IsKeyboardFocusedProperty, Value = true };
            focus.Setters.Add(new Setter(Border.BorderBrushProperty, FundPalette.Focus, "bd"));
            t.Triggers.Add(focus);
            var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(new Setter(UIElement.OpacityProperty, underlineOnHover ? 0.8 : 1.0, "bd"));
            if (!underlineOnHover) hover.Setters.Add(new Setter(Border.BackgroundProperty, FundPalette.Make(0xFF1B1B1B), "bd"));
            t.Triggers.Add(hover);
            var disabled = new Trigger { Property = UIElement.IsEnabledProperty, Value = false };
            disabled.Setters.Add(new Setter(UIElement.OpacityProperty, 0.45, "bd"));
            t.Triggers.Add(disabled);
            return t;
        }
    }
}

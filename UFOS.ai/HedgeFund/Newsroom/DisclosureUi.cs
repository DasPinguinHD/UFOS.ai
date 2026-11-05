using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Shared building blocks of the Disclosures section (CONGRESS_TASK.md "Clarity rules", INSIDER_RET_API.md §8): ONE card
    /// layout (compact row, details on expand), ONE filter bar style (segmented choices), ONE return chip. Congress will reuse
    /// them. Fixed colours: insider = gold (the Newsroom's SEC colour), congress = teal; violet stays reserved for AI — nothing
    /// here is AI output. Code-built (FundUi / FundPalette look of the Hedge Fund module).
    /// </summary>
    internal static class DisclosureUi
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        public static readonly Brush InsiderBrush = FundPalette.Make(0xFFE8C468);
        public static readonly Brush InsiderSellBrush = FundPalette.Make(0xFFC29B95);
        public static readonly Brush CongressBrush = FundPalette.Make(0xFF6FB7C9);
        public static readonly Brush CardBg = FundPalette.Make(0xFF1A1A1A);
        public static readonly Brush CardBorder = FundPalette.Make(0xFF2A2A2A);
        public static readonly Brush ChipBorder = FundPalette.Make(0xFF4A4A4A);

        public const string TradeDateCaption = "from trade date — not tradable (insider's own timing)";

        // ------------------------------------------------------------------ formatting

        /// <summary>0.021 → "+2.1 %" (one decimal, always signed).</summary>
        public static string Pct(double? v)
        {
            if (v is not double x || !double.IsFinite(x)) return "—";
            var p = Math.Round(x * 100, 1);
            return (p > 0 ? "+" : p < 0 ? "−" : "±") + Math.Abs(p).ToString("0.0", Inv) + " %";
        }

        public static string Money(double? v)
        {
            if (v is not double x || !double.IsFinite(x)) return "—";
            var a = Math.Abs(x);
            return a >= 1_000_000_000 ? "$" + (a / 1_000_000_000).ToString("0.0", Inv) + "B"
                 : a >= 1_000_000 ? "$" + (a / 1_000_000).ToString("0.0", Inv) + "M"
                 : a >= 1_000 ? "$" + (a / 1_000).ToString("0", Inv) + "K"
                 : "$" + a.ToString("0", Inv);
        }

        public static string LocalTime(string? iso)
            => DateTimeOffset.TryParse(iso, Inv, DateTimeStyles.AssumeUniversal, out var t) ? t.ToLocalTime().ToString("yyyy-MM-dd HH:mm", Inv) : "—";

        public static string Bytes(long? b)
            => b is long v ? (v >= 1L << 30 ? (v / (double)(1L << 30)).ToString("0.0", Inv) + " GB" : (v / (double)(1L << 20)).ToString("0", Inv) + " MB") : "unknown";

        public static string EntryBasisText(string? basis) => basis switch
        {
            "minute" => "first IEX minute bar after the EDGAR acceptance",
            "next_open" => "open of the next regular session (accepted outside the session)",
            "next_open_no_trade" => "open of the next session (no IEX trade after the acceptance before the close)",
            "next_open_after_filing_date" => "open of the first session after the filing date",
            "close" => "close of the trade date",
            "prev_close_non_trading_day" => "close of the previous trading day (the trade date was not a trading day)",
            null or "" => "entry not known yet",
            _ => basis,
        };

        private const string InsiderHow = "From the SEC filing: first tradable price after the EDGAR acceptance to the close h sessions later, minus the "
                                          + "sector ETF over the same window (an open entry includes the entry session). Prices adjusted for splits and "
                                          + "dividends (Alpaca IEX). Expand the row for both start points and all horizons.";
        private const string CongressHow = "From the filing: the open of the first session after the filing date (only the date is published) to the close "
                                           + "h sessions later, minus the sector ETF over the same window. Prices adjusted for splits and dividends "
                                           + "(Alpaca IEX). Expand the row for both start points and all horizons.";

        /// <summary>The ONE return chip of an insider row: "+2.1 % vs XLK · 20 d", "running (12 of 20 d)", "not measurable — …".</summary>
        public static (string Text, string Tooltip) ChipText(InsiderRow row)
            => ChipText(row.Chip, row.Measurable, row.NotMeasurableReason, row.Form == "4/A", InsiderHow);

        /// <summary>The same chip for a congress row (entry: open of the first session after the filing date).</summary>
        public static (string Text, string Tooltip) ChipText(CongressRow row)
            => ChipText(row.Chip, row.Measurable, row.NotMeasurableReason, amendment: false, CongressHow);

        private static (string Text, string Tooltip) ChipText(ReturnChip? c, bool measurable, string? notMeasurableReason, bool amendment, string how)
        {
            if (c == null)
            {
                if (!measurable) return ("not measurable — " + (notMeasurableReason ?? "no reason given"), "This trade cannot be measured.");
                if (amendment) return ("amendment — not measured", "Amendments (Form 4/A) are shown but not measured, so a trade is never counted twice.");
                return ("return pending", "The return is computed by the daily job after the US close (16:45 ET + 15 min).");
            }
            switch (c.State)
            {
                case "complete":
                    if (c.Excess is double ex)
                        return (Pct(ex) + " vs " + (c.Etf ?? "sector ETF") + " · " + c.Horizon + " d", how + "\nStock " + Pct(c.RStock) + ".");
                    return (Pct(c.RStock) + " · " + c.Horizon + " d · no excess", how + "\nNo excess: " + (c.Reason ?? "no sector ETF"));
                case "running":
                    return ("running (" + (c.SessionsDone ?? 0) + " of " + c.Horizon + " d)", how);
                default:
                    return ("not measurable — " + (c.Reason ?? "no reason given"), how);
            }
        }

        // ------------------------------------------------------------------ controls

        /// <summary>A segmented single choice (dark toggle look). Returns the panel; <paramref name="changed"/> gets the value.</summary>
        public static WrapPanel Segmented(string label, IReadOnlyList<(string Text, string Value, string? Tip)> options, string initial,
                                          Action<string> changed, out Action<string> select)
        {
            var panel = new WrapPanel { Margin = new Thickness(0, 0, 14, 6), VerticalAlignment = VerticalAlignment.Center };
            panel.Children.Add(new TextBlock { Text = label, FontSize = 10.5, Foreground = FundPalette.Faint, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 6, 0) });
            var group = "seg-" + Guid.NewGuid().ToString("N");
            var buttons = new List<(RadioButton B, string V)>();
            var syncing = false;
            foreach (var (text, value, tip) in options)
            {
                var rb = new RadioButton { GroupName = group, Content = text, IsChecked = value == initial, Template = SegmentTemplate(), Cursor = Cursors.Hand, ToolTip = tip };
                AutomationProperties.SetName(rb, label + " " + text);
                var v = value;
                rb.Checked += (_, _) => { if (!syncing) changed(v); };
                buttons.Add((rb, v));
                panel.Children.Add(rb);
            }
            select = v =>
            {
                syncing = true;
                try { foreach (var (b, bv) in buttons) b.IsChecked = bv == v; }
                finally { syncing = false; }
            };
            return panel;
        }

        private static ControlTemplate SegmentTemplate()
        {
            var bd = new FrameworkElementFactory(typeof(Border), "bd");
            bd.SetValue(Border.BackgroundProperty, Brushes.Transparent);
            bd.SetValue(Border.BorderBrushProperty, FundPalette.Make(0xFF333333));
            bd.SetValue(Border.BorderThicknessProperty, new Thickness(1));
            bd.SetValue(Border.CornerRadiusProperty, new CornerRadius(4));
            bd.SetValue(Border.PaddingProperty, new Thickness(8, 2, 8, 2));
            bd.SetValue(FrameworkElement.MarginProperty, new Thickness(0, 0, 4, 0));
            var cp = new FrameworkElementFactory(typeof(ContentPresenter));
            cp.SetValue(TextElement.FontSizeProperty, 10.5);
            cp.SetValue(TextElement.ForegroundProperty, FundPalette.Text);
            cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Center);
            bd.AppendChild(cp);
            var t = new ControlTemplate(typeof(RadioButton)) { VisualTree = bd };
            var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(new Setter(Border.BackgroundProperty, FundPalette.Make(0xFF1C1C1C), "bd"));
            t.Triggers.Add(hover);
            var on = new Trigger { Property = ToggleButton.IsCheckedProperty, Value = true };
            on.Setters.Add(new Setter(Border.BackgroundProperty, FundPalette.Make(0xFF242424), "bd"));
            on.Setters.Add(new Setter(Border.BorderBrushProperty, InsiderBrush, "bd"));
            t.Triggers.Add(on);
            var focus = new Trigger { Property = UIElement.IsKeyboardFocusedProperty, Value = true };
            focus.Setters.Add(new Setter(Border.BorderBrushProperty, FundPalette.Focus, "bd"));
            t.Triggers.Add(focus);
            return t;
        }

        public static Border Badge(string text, Brush brush, string tooltip)
        {
            var b = new Border
            {
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(3),
                Padding = new Thickness(5, 0, 5, 1),
                Margin = new Thickness(6, 0, 0, 0),
                VerticalAlignment = VerticalAlignment.Center,
                ToolTip = tooltip,
                Child = new TextBlock { Text = text, FontSize = 9.5, Foreground = brush },
            };
            AutomationProperties.SetName(b, text + ": " + tooltip);
            return b;
        }

        public static TextBlock Text(string text, double size, Brush brush, bool bold = false, bool mono = false, bool wrap = true)
            => FundUi.Text(text, size, brush, bold, mono, wrap);

        /// <summary>A hyperlink that opens an http(s) document in the default browser (NewsroomLinks refuses anything else).</summary>
        public static TextBlock Link(string text, string url)
        {
            var link = new Hyperlink(new Run(text)) { Foreground = FundPalette.Dim, ToolTip = url };
            if (Uri.TryCreate(url, UriKind.Absolute, out var uri)) link.NavigateUri = uri;
            link.RequestNavigate += (_, e) =>
            {
                e.Handled = true;
                NewsroomLinks.Open(url);
            };
            AutomationProperties.SetName(link, text + " (opens in your default browser)");
            var tb = new TextBlock { FontSize = 10.5, Margin = new Thickness(0, 6, 0, 0) };
            tb.Inlines.Add(link);
            return tb;
        }

        /// <summary>A flat toggle (no chrome) used as the clickable header of a row card; Enter / Space toggle it.</summary>
        public static ToggleButton FlatToggle(UIElement content, string automationName)
        {
            var bd = new FrameworkElementFactory(typeof(Border), "bd");
            bd.SetValue(Border.BackgroundProperty, Brushes.Transparent);
            var cp = new FrameworkElementFactory(typeof(ContentPresenter));
            bd.AppendChild(cp);
            var t = new ControlTemplate(typeof(ToggleButton)) { VisualTree = bd };
            var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(new Setter(Border.BackgroundProperty, FundPalette.Make(0x0CFFFFFF), "bd"));
            t.Triggers.Add(hover);
            var tb = new ToggleButton { Content = content, Template = t, Cursor = Cursors.Hand, HorizontalContentAlignment = HorizontalAlignment.Stretch };
            AutomationProperties.SetName(tb, automationName);
            return tb;
        }
    }
}

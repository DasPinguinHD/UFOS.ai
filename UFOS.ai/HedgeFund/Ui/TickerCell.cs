using System.Collections.Generic;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;

namespace UFOS.ai.HedgeFund.Ui
{
    /// <summary>
    /// Stage 11 §2: one market tile — label, change (neutral grey with a sign — never red / green), value, sparkline and the
    /// "source · age" (or "close &lt;date&gt;") footer. An unavailable item shows "not available (reason)" instead of value and sparkline.
    /// </summary>
    internal sealed class TickerCell : Border
    {
        private readonly TextBlock _label;
        private readonly TextBlock _change;
        private readonly TextBlock _value;
        private readonly TextBlock _reason;
        private readonly Sparkline _spark = new() { Margin = new Thickness(0, 4, 0, 0) };
        private readonly TextBlock _seriesNote;
        private readonly TextBlock _footer;

        public TickerCell(bool divider = true)
        {
            Padding = new Thickness(14, 10, 14, 10);
            BorderBrush = HfTokens.Divider;
            BorderThickness = new Thickness(0, 0, divider ? 1 : 0, 0);
            MinWidth = 120;
            var stack = new StackPanel();
            var top = new DockPanel { LastChildFill = true };
            _change = HfUi.Text("", 12, HfTokens.MarketChange, mono: true);
            _change.Margin = new Thickness(6, 0, 0, 0);
            DockPanel.SetDock(_change, Dock.Right);
            top.Children.Add(_change);
            _label = HfUi.Text("", 12, HfTokens.TextMuted, bold: true);
            top.Children.Add(_label);
            stack.Children.Add(top);
            _value = HfUi.Text("", HfTokens.ValueSize, HfTokens.TextValue, mono: true);
            _value.Margin = new Thickness(0, 2, 0, 0);
            stack.Children.Add(_value);
            _reason = HfUi.Text("", 11, HfTokens.TextDim, wrap: true);
            _reason.MaxHeight = 44;
            _reason.Margin = new Thickness(0, 2, 0, 0);
            _reason.Visibility = Visibility.Collapsed;
            stack.Children.Add(_reason);
            stack.Children.Add(_spark);
            _seriesNote = HfUi.Text("", 10.5, HfTokens.TextFaint);
            _seriesNote.Margin = new Thickness(0, 3, 0, 0);
            _seriesNote.Visibility = Visibility.Collapsed;
            stack.Children.Add(_seriesNote);
            _footer = HfUi.Text("", 11, HfTokens.TextFaint);
            _footer.Margin = new Thickness(0, 3, 0, 0);
            stack.Children.Add(_footer);
            Child = stack;
        }

        /// <summary>An available item. <paramref name="seriesNote"/>: shown in place of the sparkline when the series is missing.</summary>
        public void SetData(string label, string change, string value, IEnumerable<double>? series, string footer, string? tooltip = null,
                            string? seriesNote = null)
        {
            HfUi.SetText(_label, label);
            HfUi.SetText(_change, change);
            HfUi.SetText(_value, value);
            _value.Visibility = Visibility.Visible;
            _reason.Visibility = Visibility.Collapsed;
            var points = series == null ? new List<double>() : new List<double>(series);
            _spark.Visibility = points.Count >= 2 ? Visibility.Visible : Visibility.Collapsed;
            _spark.SetData(points);
            HfUi.SetText(_seriesNote, points.Count >= 2 ? "" : seriesNote);
            _seriesNote.Visibility = points.Count < 2 && !string.IsNullOrEmpty(seriesNote) ? Visibility.Visible : Visibility.Collapsed;
            HfUi.SetText(_footer, footer);
            ToolTip = string.IsNullOrWhiteSpace(tooltip) ? null : tooltip;
            AutomationProperties.SetName(this, label + ": " + value + ", change " + change + ", " + footer);
        }

        /// <summary>An unavailable item: "not available (reason)"; the footer names the source when known.</summary>
        public void SetUnavailable(string label, string? reason, string footer = "")
        {
            var text = HfUi.NotAvailable(reason);
            HfUi.SetText(_label, label);
            HfUi.SetText(_change, "");
            _value.Visibility = Visibility.Collapsed;
            _reason.Text = text;
            _reason.Visibility = Visibility.Visible;
            _spark.Visibility = Visibility.Collapsed;
            _spark.SetData(null);
            _seriesNote.Visibility = Visibility.Collapsed;
            HfUi.SetText(_footer, footer);
            ToolTip = text;
            AutomationProperties.SetName(this, label + ": " + text);
        }
    }
}

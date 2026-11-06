using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shapes;

namespace UFOS.ai.HedgeFund.Ui
{
    /// <summary>Stage 11 §2: a card — panel background, 1 px border, radius 12, padding 16 (design D3).</summary>
    internal sealed class Card : Border
    {
        public Card(UIElement? child = null, Thickness? padding = null)
        {
            Background = HfTokens.CardBg;
            BorderBrush = HfTokens.CardBorder;
            BorderThickness = new Thickness(1);
            CornerRadius = new CornerRadius(HfTokens.CardRadius);
            Padding = padding ?? new Thickness(HfTokens.CardPadding);
            SnapsToDevicePixels = true;
            Child = child;
        }
    }

    /// <summary>Stage 11 §2: a section heading (a noun, never a question) with optional content on the right (links, ⛶, state).</summary>
    internal sealed class SectionHeader : DockPanel
    {
        private readonly TextBlock _title;
        private UIElement? _right;

        public SectionHeader(string title, UIElement? right = null, double size = HfTokens.TitleSize)
        {
            LastChildFill = true;
            Margin = new Thickness(0, 0, 0, 8);
            _title = HfUi.Text(title, size, HfTokens.TextTitle, bold: true);
            AutomationProperties.SetName(_title, title);
            Children.Add(_title);
            SetRight(right);
        }

        public string Title
        {
            get => _title.Text;
            set { HfUi.SetText(_title, value); AutomationProperties.SetName(_title, value); }
        }

        /// <summary>Replaces the right-hand content (null removes it).</summary>
        public void SetRight(UIElement? right)
        {
            if (_right != null) Children.Remove(_right);
            _right = right;
            if (right == null) return;
            DockPanel.SetDock(right, Dock.Right);
            if (right is FrameworkElement fe)
            {
                fe.VerticalAlignment = VerticalAlignment.Center;
                if (fe.Margin == default) fe.Margin = new Thickness(12, 0, 0, 0);
            }
            Children.Insert(0, right);   // the title stays the last (filling) child
        }
    }

    /// <summary>
    /// Stage 11 §2: one-line "label … dot value" row with an optional faint second line (sub) and a hairline below.
    /// Long values end in an ellipsis with the full text as tooltip.
    /// </summary>
    internal sealed class KeyValueRow : Border
    {
        private readonly TextBlock _label;
        private readonly TextBlock _value;
        private readonly TextBlock _sub;
        private readonly Ellipse _dot;

        public KeyValueRow(string label, string value = "", string? sub = null, DotKind dot = DotKind.None, bool divider = true)
        {
            BorderBrush = HfTokens.Divider;
            BorderThickness = new Thickness(0, 0, 0, divider ? 1 : 0);
            Padding = new Thickness(0, 7, 0, 7);
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            _label = HfUi.Text(label, HfTokens.BodySize, HfTokens.TextDim);
            _label.Margin = new Thickness(0, 0, 12, 0);
            grid.Children.Add(_label);

            var right = new DockPanel { LastChildFill = true, HorizontalAlignment = HorizontalAlignment.Right };
            _dot = HfUi.Dot(dot);
            DockPanel.SetDock(_dot, Dock.Left);
            right.Children.Add(_dot);
            _value = HfUi.Text(value, HfTokens.BodySize, HfTokens.Text);
            _value.TextAlignment = TextAlignment.Right;
            right.Children.Add(_value);
            Grid.SetColumn(right, 1);
            grid.Children.Add(right);

            _sub = HfUi.Text(sub, HfTokens.SmallSize, HfTokens.TextFaint);
            _sub.HorizontalAlignment = HorizontalAlignment.Right;
            _sub.Margin = new Thickness(0, 2, 0, 0);
            _sub.Visibility = string.IsNullOrEmpty(sub) ? Visibility.Collapsed : Visibility.Visible;
            Grid.SetColumn(_sub, 0);
            Grid.SetColumnSpan(_sub, 2);
            Grid.SetRow(_sub, 1);
            grid.Children.Add(_sub);
            Child = grid;
            UpdateName();
        }

        public void Set(string value, string? sub = null, DotKind dot = DotKind.None, Brush? valueBrush = null)
        {
            HfUi.SetText(_value, value, valueBrush ?? HfTokens.Text);
            HfUi.SetText(_sub, sub);
            _sub.Visibility = string.IsNullOrEmpty(sub) ? Visibility.Collapsed : Visibility.Visible;
            HfUi.SetDot(_dot, dot);
            UpdateName();
        }

        private void UpdateName()
            => AutomationProperties.SetName(this, _label.Text + ": " + _value.Text + (string.IsNullOrEmpty(_sub.Text) ? "" : " — " + _sub.Text));
    }

    /// <summary>Stage 11 §2: a figure chip of the risk row — label left, value right; an unavailable figure shows its reason below.</summary>
    internal sealed class StatChip : Border
    {
        private readonly TextBlock _label;
        private readonly TextBlock _value;
        private readonly TextBlock _reason;

        public StatChip(string label)
        {
            Background = HfTokens.InsetBg;
            BorderBrush = HfTokens.InsetBorder;
            BorderThickness = new Thickness(1);
            CornerRadius = new CornerRadius(8);
            Padding = new Thickness(10, 6, 10, 6);
            Margin = new Thickness(0, 0, 8, 8);
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            _label = HfUi.Text(label, 12, HfTokens.TextDim);
            _label.Margin = new Thickness(0, 0, 10, 0);
            grid.Children.Add(_label);
            _value = HfUi.Text("", 13, HfTokens.TextValue, mono: true);
            _value.HorizontalAlignment = HorizontalAlignment.Right;
            Grid.SetColumn(_value, 1);
            grid.Children.Add(_value);
            _reason = HfUi.Text("", 11, HfTokens.TextFaint, wrap: true);
            _reason.Margin = new Thickness(0, 3, 0, 0);
            _reason.MaxHeight = 46;
            _reason.Visibility = Visibility.Collapsed;
            Grid.SetRow(_reason, 1);
            Grid.SetColumnSpan(_reason, 2);
            grid.Children.Add(_reason);
            Child = grid;
        }

        /// <summary>An available figure (<paramref name="tooltip"/>: method / basis).</summary>
        public void SetValue(string value, string? tooltip = null, Brush? brush = null)
        {
            HfUi.SetText(_value, value, brush ?? HfTokens.TextValue);
            _reason.Visibility = Visibility.Collapsed;
            ToolTip = string.IsNullOrWhiteSpace(tooltip) ? null : tooltip;
            AutomationProperties.SetName(this, _label.Text + ": " + value);
        }

        /// <summary>An unavailable figure: "not available" + the reason (also as tooltip).</summary>
        public void SetUnavailable(string? reason)
        {
            HfUi.SetText(_value, "not available", HfTokens.TextDim);
            var text = HfUi.NotAvailable(reason);
            _reason.Text = string.IsNullOrWhiteSpace(reason) ? "no reason given" : HfUi.Plain(reason, 200);
            _reason.Visibility = Visibility.Visible;
            ToolTip = text;
            AutomationProperties.SetName(this, _label.Text + ": " + text);
        }
    }

    /// <summary>One entry of the <see cref="AttentionStrip"/>: plain text, the navigation it opens (null = text only).</summary>
    internal sealed record AttentionItem(string Text, Action? OnClick, string AutomationName);

    /// <summary>
    /// Stage 11 §2: the one-line attention strip — amber left edge, "N open" and the items as inline links. With nothing open it turns
    /// into a single grey line "Nothing needs attention". Sources that could not be read are listed (never a silent "all clear").
    /// </summary>
    internal sealed class AttentionStrip : Border
    {
        private readonly Border _edge;
        private readonly WrapPanel _panel = new() { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        private string? _signature;

        public AttentionStrip()
        {
            CornerRadius = new CornerRadius(8);
            BorderThickness = new Thickness(1);
            SnapsToDevicePixels = true;
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(3) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            _edge = new Border { Background = HfTokens.Attention, CornerRadius = new CornerRadius(7, 0, 0, 7) };
            grid.Children.Add(_edge);
            _panel.Margin = new Thickness(12, 7, 12, 7);
            Grid.SetColumn(_panel, 1);
            grid.Children.Add(_panel);
            Child = grid;
            AutomationProperties.SetName(this, "Attention");
            SetItems(Array.Empty<AttentionItem>(), Array.Empty<string>());
        }

        /// <param name="items">Open points (each navigates to its view).</param>
        /// <param name="unavailable">Sources that could not be read, each "name (reason)".</param>
        public void SetItems(IReadOnlyList<AttentionItem> items, IReadOnlyList<string> unavailable)
        {
            // rebuilt only when what is shown changed (keeps the keyboard focus on a link across polls)
            var signature = string.Join("\u001f", items.Select(x => x.Text + "\u001e" + (x.OnClick != null))) + "\u001d" + string.Join("\u001f", unavailable);
            if (signature == _signature) return;
            _signature = signature;
            _panel.Children.Clear();
            var open = items.Count > 0;
            Background = open ? HfTokens.AttentionBg : Brushes.Transparent;
            BorderBrush = open ? HfTokens.AttentionBorder : Brushes.Transparent;
            _edge.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
            _panel.Margin = open ? new Thickness(12, 7, 12, 7) : new Thickness(0, 2, 0, 2);
            if (!open)
            {
                var text = unavailable.Count == 0 ? "Nothing needs attention" : "Nothing needs attention in the sources that could be read";
                _panel.Children.Add(HfUi.Text(text, 13, HfTokens.TextDim));
                AddUnavailable(unavailable);
                AutomationProperties.SetName(this, "Attention: " + text);
                return;
            }
            var count = HfUi.Text(items.Count + " open", 13, HfTokens.AttentionCount, bold: true);
            count.Margin = new Thickness(0, 0, 12, 0);
            _panel.Children.Add(count);
            for (var i = 0; i < items.Count; i++)
            {
                var it = items[i];
                if (i > 0) _panel.Children.Add(DotSeparator());
                if (it.OnClick != null)
                {
                    var link = HfUi.Link(it.Text + " →", it.OnClick, it.AutomationName, HfTokens.AttentionText, 13);
                    _panel.Children.Add(link);
                }
                else
                {
                    _panel.Children.Add(HfUi.Text(it.Text, 13, HfTokens.AttentionText));
                }
            }
            AddUnavailable(unavailable);
            AutomationProperties.SetName(this, "Attention: " + items.Count + " open — " + string.Join("; ", items.Select(x => x.Text)));
        }

        private void AddUnavailable(IReadOnlyList<string> unavailable)
        {
            if (unavailable.Count == 0) return;
            _panel.Children.Add(DotSeparator());
            var t = HfUi.Text("not available: " + string.Join("; ", unavailable), 12, HfTokens.TextFaint, wrap: true);
            _panel.Children.Add(t);
        }

        private static TextBlock DotSeparator()
        {
            var s = HfUi.Text("·", 13, HfTokens.AttentionMuted);
            s.Margin = new Thickness(10, 0, 10, 0);
            return s;
        }
    }
}

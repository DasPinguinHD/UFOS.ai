using System;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Navigation;
using System.Windows.Shapes;
using System.Windows.Media.Animation;

namespace FedTrader
{
    /// <summary>
    /// Interaction logic for MainWindow.xaml
    /// </summary>
    public partial class MainWindow : Window
    {
        public MainWindow()
        {
            InitializeComponent();
            // initialize default UI for verdict widget
            UpdateConfidence(0);
            UpdateVerdict("NONE", "");
            UpdateReason("[REASON]");
        }

        // Confidence history: add a sample (0..100)
        public void AddConfidenceSample(int value)
        {
            var clamped = Math.Max(0, Math.Min(100, value));
            Dispatcher.Invoke(() =>
            {
                // create a small bar
                var bar = new System.Windows.Shapes.Rectangle
                {
                    Width = 8,
                    Height = 40 * clamped / 100.0,
                    Fill = new SolidColorBrush(Color.FromRgb(0x4C, 0xC9, 0x50)), // greenish
                    Margin = new Thickness(2, 0, 2, 0),
                    VerticalAlignment = VerticalAlignment.Bottom,
                    ToolTip = $"{clamped}/100"
                };
                // keep max items
                while (ConfidenceStack.Children.Count > 80) ConfidenceStack.Children.RemoveAt(0);
                ConfidenceStack.Children.Add(bar);
            });
        }



        // Connection status update
        public void UpdateConnectionStatus(string status)
        {
            Dispatcher.Invoke(() =>
            {
                var s = (status ?? string.Empty).ToUpperInvariant();
                Brush color = Brushes.Gray;
                string text = status ?? "";
                if (s.Contains("CONNECTED")) color = Brushes.LimeGreen;
                else if (s.Contains("CONNECTING") || s.Contains("RECONNECT")) color = Brushes.Gold;
                else if (s.Contains("ERROR")) color = Brushes.Red;
                else if (s.Contains("DISCONNECTED")) color = Brushes.Gray;

                ConnectionIndicator.Fill = color;
                ConnectionStatusText.Text = text;
                ConnectionLastText.Text = $"last: {DateTime.Now:HH:mm:ss}";
            });
        }

        private void TextBox_TextChanged(object sender, TextChangedEventArgs e)
        {

        }

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton == MouseButtonState.Pressed)
            {
                try { this.DragMove(); } catch { }
            }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            this.WindowState = WindowState.Minimized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            this.Close();
        }

        private void TranscriptGrid_MouseEnter(object sender, MouseEventArgs e)
        {
            try
            {
                var anim = new DoubleAnimation(0.0, TimeSpan.FromSeconds(0.5)) { FillBehavior = FillBehavior.HoldEnd };
                TranscriptFadeRect.BeginAnimation(UIElement.OpacityProperty, anim);
            }
            catch { }
        }

        private void TranscriptGrid_MouseLeave(object sender, MouseEventArgs e)
        {
            try
            {
                var anim = new DoubleAnimation(1.0, TimeSpan.FromSeconds(0.5)) { FillBehavior = FillBehavior.HoldEnd };
                TranscriptFadeRect.BeginAnimation(UIElement.OpacityProperty, anim);
            }
            catch { }
        }

        // Public API to update the verdict widget dynamically
        public void UpdateVerdict(string verdict, string ticker)
        {
            var text = string.IsNullOrWhiteSpace(ticker) ? verdict : $"{verdict} {ticker}";
            VerdictTextBlock.Text = $"Current Verdict: {text}";

            // determine color: green for long/buy, red for short/sell, gray otherwise
            var v = verdict?.ToUpperInvariant() ?? string.Empty;
            Brush color = Brushes.Gray;
            if (v.Contains("LONG") || v.Contains("BUY") || v.Contains("BULL")) color = Brushes.Green;
            else if (v.Contains("SHORT") || v.Contains("SELL") || v.Contains("BEAR")) color = Brushes.Red;

            RingForeground.Stroke = color;
        }

        public void UpdateReason(string reason)
        {
            ReasonTextBlock.Text = reason ?? string.Empty;
        }

        public void UpdateConfidence(int value)
        {
            var clamped = Math.Max(0, Math.Min(100, value));
            ConfidenceTextBlock.Text = $"{clamped}/100";

            // update stroke dash to show portion of the ring
            // a simple approach: set dash array to [value, 100-value]
            RingForeground.StrokeDashArray = new DoubleCollection() { clamped, 100 - clamped };
        }

        // Update ticker row (1..4) with name, value and tendency (e.g. "UP", "DOWN", "SIDEWAYS")
        public void UpdateTicker(int index, string name, string value, string tendency)
        {
            // normalize tendency
            var t = (tendency ?? string.Empty).ToUpperInvariant();

            switch (index)
            {
                case 1:
                    TickerName1.Text = name ?? string.Empty;
                    TickerValue1.Text = value ?? string.Empty;
                    ApplyTrend(TickerValue1, TickerUp1, TickerDown1, TickerSide1, t);
                    break;
                case 2:
                    TickerName2.Text = name ?? string.Empty;
                    TickerValue2.Text = value ?? string.Empty;
                    ApplyTrend(TickerValue2, TickerUp2, TickerDown2, TickerSide2, t);
                    break;
                case 3:
                    TickerName3.Text = name ?? string.Empty;
                    TickerValue3.Text = value ?? string.Empty;
                    ApplyTrend(TickerValue3, TickerUp3, TickerDown3, TickerSide3, t);
                    break;
                case 4:
                    TickerName4.Text = name ?? string.Empty;
                    TickerValue4.Text = value ?? string.Empty;
                    ApplyTrend(TickerValue4, TickerUp4, TickerDown4, TickerSide4, t);
                    break;
                default:
                    break;
            }
            // update timestamp whenever tickers are updated
            try
            {
                TimestampRun.Text = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
            }
            catch
            {
                // ignore if UI element not present during design-time
            }
        }

        private void ApplyTrend(TextBlock valueText, Polygon up, Polygon down, TextBlock side, string tendency)
        {
            // hide all first
            up.Visibility = Visibility.Collapsed;
            down.Visibility = Visibility.Collapsed;
            side.Visibility = Visibility.Collapsed;

            // default color (light for dark background)
            valueText.Foreground = Brushes.White;

            if (tendency.Contains("UP") || tendency.Contains("LONG") || tendency.Contains("BUY") || tendency.Contains("+") )
            {
                up.Visibility = Visibility.Visible;
                valueText.Foreground = Brushes.Green;
            }
            else if (tendency.Contains("DOWN") || tendency.Contains("SHORT") || tendency.Contains("SELL") || tendency.Contains("-") )
            {
                down.Visibility = Visibility.Visible;
                valueText.Foreground = Brushes.Red;
            }
            else
            {
                // sideways / neutral
                side.Visibility = Visibility.Visible;
                valueText.Foreground = Brushes.Gray;
            }
        }
    }
}
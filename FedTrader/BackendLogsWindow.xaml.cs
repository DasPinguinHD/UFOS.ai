using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Threading;

namespace FedTrader
{
    public partial class BackendLogsWindow : Window
    {
        private readonly Func<string> _getLogs;
        private readonly DispatcherTimer _timer;
        private int _lastLength = -1;

        public BackendLogsWindow(Func<string> getLogs)
        {
            InitializeComponent();
            _getLogs = getLogs ?? throw new ArgumentNullException(nameof(getLogs));
            // Try to apply the CompactScrollBarStyle from main window so the logs scrollbar matches the transcript
            try
            {
                Style? compact = null;
                try { compact = Application.Current?.MainWindow?.FindResource("CompactScrollBarStyle") as Style; } catch { }
                if (compact != null && LogsTextBox != null)
                {
                    // set ScrollBar style
                    LogsTextBox.Resources.Add(typeof(ScrollBar), compact);
                    // also try to copy the thumb style if available for exact match
                    try
                    {
                        var thumb = Application.Current?.MainWindow?.FindResource("CompactScrollThumbStyle") as Style;
                        if (thumb != null)
                        {
                            LogsTextBox.Resources.Add(typeof(Thumb), thumb);
                        }
                    }
                    catch { }
                }
            }
            catch { }
            _timer = new DispatcherTimer(TimeSpan.FromMilliseconds(500), DispatcherPriority.Background, (s, e) => LoadLogs(), Dispatcher.CurrentDispatcher);
            _timer.Start();
            LoadLogs();
            this.Closed += (_, __) => { try { _timer.Stop(); } catch { } };
        }

        private void LoadLogs()
        {
            try
            {
                var text = _getLogs() ?? string.Empty;
                // quick check to avoid unnecessary UI updates
                if (text.Length != _lastLength)
                {
                    if (LogsTextBox != null)
                    {
                        LogsTextBox.Text = text;
                        LogsTextBox.CaretIndex = LogsTextBox.Text.Length;
                        LogsTextBox.ScrollToEnd();
                    }
                    _lastLength = text.Length;
                }
            }
            catch { }
        }

        private void Refresh_Click(object sender, RoutedEventArgs e)
        {
            LoadLogs();
        }

        private void Close_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }
    }
}

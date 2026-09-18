using System;
using System.Windows;
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
            // auto-refresh every 500ms
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
                    LogsTextBox.Text = text;
                    LogsTextBox.CaretIndex = LogsTextBox.Text.Length;
                    LogsTextBox.ScrollToEnd();
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

using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Threading;

namespace FedTrader
{
    public partial class VerdictHistoryWindow : Window
    {
        private readonly Func<List<VerdictRecord>> _getHistory;
        private readonly DispatcherTimer _timer;

        public VerdictHistoryWindow(Func<List<VerdictRecord>> getHistory)
        {
            InitializeComponent();
            _getHistory = getHistory ?? throw new ArgumentNullException(nameof(getHistory));

            // Try to copy scroll styles from main window for a consistent look
            try
            {
                Style? compact = null;
                try { compact = Application.Current?.MainWindow?.FindResource("CompactScrollBarStyle") as Style; } catch { }
                if (compact != null && ItemsList != null)
                {
                    ItemsList.Resources.Add(typeof(ScrollBar), compact);
                    try
                    {
                        var thumb = Application.Current?.MainWindow?.FindResource("CompactScrollThumbStyle") as Style;
                        if (thumb != null) ItemsList.Resources.Add(typeof(Thumb), thumb);
                    }
                    catch { }
                }
            }
            catch { }

            _timer = new DispatcherTimer(TimeSpan.FromMilliseconds(500), DispatcherPriority.Background, (s, e) => LoadHistory(), Dispatcher.CurrentDispatcher);
            _timer.Start();
            LoadHistory();
            this.Closed += (_, __) => { try { _timer.Stop(); } catch { } };
            // Close on Escape key
            this.PreviewKeyDown += (s, e) =>
            {
                try
                {
                    if (e.Key == System.Windows.Input.Key.Escape) this.Close();
                }
                catch { }
            };
        }

        private void LoadHistory()
        {
            try
            {
                var list = _getHistory() ?? new List<VerdictRecord>();
                ItemsList.ItemsSource = list;
            }
            catch { }
        }

        private void Refresh_Click(object sender, RoutedEventArgs e)
        {
            LoadHistory();
        }

        private void Close_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }
    }
}

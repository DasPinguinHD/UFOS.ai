using System;
using System.ComponentModel;
using System.Globalization;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Newsroom;

namespace UFOS.ai.HedgeFund
{
    public partial class HedgeFundWindow : Window
    {
        // The dashboard (agent signals, verdict, portfolio, execution) is illustrative
        // only: no connection to a real broker, all values there are placeholder data.
        // The Financial Newsroom (toolbar submenu, Newsroom/) is the exception: it shows
        // live public data served by HedgeFund/backend, which this window starts/stops.
        private DispatcherTimer? _clockTimer;
        private FinancialNewsroomWindow? _newsroomWindow;

        public HedgeFundWindow()
        {
            InitializeComponent();
        }

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            _clockTimer = new DispatcherTimer
            {
                Interval = TimeSpan.FromSeconds(1)
            };
            _clockTimer.Tick += (_, _) => UpdateClock();
            _clockTimer.Start();
            UpdateClock();

            // Financial Newsroom: the backend is started
            // with the window so the collectors (SEC EDGAR needs 1–2 min for its first
            // fetch) have data ready by the time the newsroom is opened.
            NewsroomBackend.StatusChanged += OnNewsroomBackendStatusChanged;
            UpdateNewsroomStatus();
            _ = NewsroomBackend.EnsureStartedAsync();
        }

        private void Window_Closing(object? sender, CancelEventArgs e)
        {
            _clockTimer?.Stop();
            _clockTimer = null;

            NewsroomBackend.StatusChanged -= OnNewsroomBackendStatusChanged;
            _newsroomWindow?.Close();
            _newsroomWindow = null;
            NewsroomBackend.Stop();
        }

        private void UpdateClock()
        {
            ClockText.Text = DateTime.UtcNow.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " UTC";
        }

        // Dragging the title bar of a MAXIMIZED window: DragMove() does nothing there,
        // so the window used to be stuck in full screen. Like a native title bar, the
        // window is restored as soon as the mouse actually moves with the button held
        // (a plain click does nothing), placed so the cursor keeps its relative spot on
        // the title bar, and then dragged.
        private bool _restoreOnDrag;
        private Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
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
            // Cursor position in screen DIPs (PointToScreen returns device pixels).
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

        // A maximized borderless window (WindowStyle=None) is placed by Windows so that
        // its invisible resize frame hangs off the screen edges, cutting off the title
        // bar and the top corners. Pad the content by that frame while maximized and
        // drop the rounded corners there (same fix as Newsroom/FinancialNewsroomWindow).
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            if (WindowState == WindowState.Maximized)
            {
                var frame = SystemParameters.WindowResizeBorderThickness;
                const double paddedBorder = 4; // SM_CXPADDEDBORDER, not exposed by SystemParameters
                RootBorder.Margin = new Thickness(frame.Left + paddedBorder, frame.Top + paddedBorder,
                                                  frame.Right + paddedBorder, frame.Bottom + paddedBorder);
                RootBorder.CornerRadius = new CornerRadius(0);
                TitleBarBorder.CornerRadius = new CornerRadius(0);
            }
            else
            {
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(8);
                TitleBarBorder.CornerRadius = new CornerRadius(8, 8, 0, 0);
            }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        // ---------- Financial Newsroom submenu ----------

        // Opens the newsroom on its first section (NewsroomSections.All[0]); the user
        // navigates from there. Replaced the dropdown submenu on 2026-09-25.
        private void NewsroomMenuButton_Click(object sender, RoutedEventArgs e)
        {
            OpenNewsroom(null);
        }

        /// <summary>Opens (or brings to front) the single Financial Newsroom window on the given section.</summary>
        private void OpenNewsroom(string? sectionId)
        {
            if (_newsroomWindow == null)
            {
                var window = new FinancialNewsroomWindow { Owner = this };
                window.Closed += (_, _) =>
                {
                    if (ReferenceEquals(_newsroomWindow, window)) _newsroomWindow = null;
                };
                _newsroomWindow = window;
                window.Show();
            }
            else
            {
                if (_newsroomWindow.WindowState == WindowState.Minimized) _newsroomWindow.WindowState = WindowState.Normal;
                _newsroomWindow.Activate();
            }
            _newsroomWindow.ShowSection(sectionId);
            _ = NewsroomBackend.EnsureStartedAsync(); // no-op while running; restarts it if it exited
        }

        private void OnNewsroomBackendStatusChanged()
        {
            // Raised on whatever thread changed the status.
            Dispatcher.BeginInvoke(new Action(UpdateNewsroomStatus));
        }

        private void UpdateNewsroomStatus()
        {
            var status = NewsroomBackend.Status;
            var brush = new SolidColorBrush(FinancialNewsroomWindow.StatusColor(status));
            brush.Freeze();
            NewsroomStatusDot.Fill = brush;
            NewsroomStatusText.Text = "Newsroom backend: " + status;
        }

        // ---------- Execution: Manual / Autonomous ----------

        private void ModeManualTab_Checked(object sender, RoutedEventArgs e)
        {
            if (ManualPanel == null || AutoPanel == null) return;
            ManualPanel.Visibility = Visibility.Visible;
            AutoPanel.Visibility = Visibility.Collapsed;
        }

        private void ModeAutoTab_Checked(object sender, RoutedEventArgs e)
        {
            if (ManualPanel == null || AutoPanel == null) return;
            ManualPanel.Visibility = Visibility.Collapsed;
            AutoPanel.Visibility = Visibility.Visible;
        }

        // ---------- Manual panel (illustration only, no real orders) ----------

        private void ManualLong_Click(object sender, RoutedEventArgs e)
        {
            RecordIllustrativeManualAction("LONG");
        }

        private void ManualShort_Click(object sender, RoutedEventArgs e)
        {
            RecordIllustrativeManualAction("SHORT");
        }

        private void ManualFlatten_Click(object sender, RoutedEventArgs e)
        {
            var ticker = string.IsNullOrWhiteSpace(TickerInput.Text) ? "—" : TickerInput.Text.Trim().ToUpperInvariant();
            ManualStatusText.Text = $"Last manual action: {ticker} FLATTENED · just now";
        }

        private void RecordIllustrativeManualAction(string stance)
        {
            var ticker = string.IsNullOrWhiteSpace(TickerInput.Text) ? "—" : TickerInput.Text.Trim().ToUpperInvariant();
            var size = string.IsNullOrWhiteSpace(SizeInput.Text) ? "0" : SizeInput.Text.Trim();
            ManualStatusText.Text = $"Last manual action: {ticker} {stance} · {size}% of book · just now";
        }

        // ---------- Autonomous panel ----------

        private void AutoEngageToggle_CheckedChanged(object sender, RoutedEventArgs e)
        {
            AutoStatusText.Text = AutoEngageToggle.IsChecked == true
                ? "Active · waiting for the next verdict ≥ threshold"
                : "Disabled · waiting for activation";
        }

        private void ConfidenceSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
        {
            if (ConfidenceValueText == null) return;
            ConfidenceValueText.Text = $"{(int)Math.Round(e.NewValue)}%";
        }

        private void MaxSizeSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
        {
            if (MaxSizeValueText == null) return;
            MaxSizeValueText.Text = $"{(int)Math.Round(e.NewValue)}% of book";
        }

        private void MaxTradesSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
        {
            if (MaxTradesValueText == null) return;
            MaxTradesValueText.Text = ((int)Math.Round(e.NewValue)).ToString(CultureInfo.InvariantCulture);
        }

        private void DrawdownStopSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
        {
            if (DrawdownStopValueText == null) return;
            DrawdownStopValueText.Text = $"{(int)Math.Round(e.NewValue)}%";
        }
    }
}

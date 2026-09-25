using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// The Financial Newsroom window of the Hedge Fund module: a navigation list
    /// on the left (one entry per <see cref="NewsroomSections.All"/> section) and
    /// the selected section's view on the right. Section views are created on
    /// first use and kept while the window is open, so switching back and forth
    /// keeps their state (e.g. a generated AI summary); each view only polls the
    /// backend while it is shown (Loaded/Unloaded).
    /// </summary>
    public partial class FinancialNewsroomWindow : Window
    {
        private readonly Dictionary<string, UserControl> _views = new();

        public FinancialNewsroomWindow()
        {
            InitializeComponent();
            SectionList.ItemsSource = NewsroomSections.All;
            NewsroomBackend.StatusChanged += OnBackendStatusChanged;
            UpdateBackendStatus();
        }

        /// <summary>Selects a section by id ("insider", "macro", …); unknown ids fall back to the first section.</summary>
        public void ShowSection(string? sectionId)
        {
            var section = NewsroomSections.All.FirstOrDefault(s => s.Id == sectionId) ?? NewsroomSections.All.FirstOrDefault();
            if (section == null) return;
            if (ReferenceEquals(SectionList.SelectedItem, section))
                Display(section);
            else
                SectionList.SelectedItem = section; // -> SectionList_SelectionChanged -> Display
        }

        private void SectionList_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            if (SectionList.SelectedItem is NewsroomSection section) Display(section);
        }

        private void Display(NewsroomSection section)
        {
            if (!_views.TryGetValue(section.Id, out var view))
            {
                view = section.CreateView();
                _views[section.Id] = view;
            }
            if (!ReferenceEquals(SectionHost.Content, view)) SectionHost.Content = view;
            TitleSectionText.Text = "·  " + section.Title;
        }

        // ---------- Backend status (shared with the Hedge Fund window's toolbar) ----------

        private void OnBackendStatusChanged()
        {
            // Raised on whatever thread changed the status.
            Dispatcher.BeginInvoke(new Action(UpdateBackendStatus));
        }

        private void UpdateBackendStatus()
        {
            var status = NewsroomBackend.Status;
            var brush = new SolidColorBrush(StatusColor(status));
            brush.Freeze();
            BackendStatusDot.Fill = brush;
            TitleStatusDot.Fill = brush;
            BackendStatusText.Text = status;
        }

        /// <summary>Green = online, amber = starting/outdated, red = offline, grey = not started/stopped.</summary>
        internal static Color StatusColor(string status)
        {
            if (status.Contains("outdated", StringComparison.OrdinalIgnoreCase) ||
                status.StartsWith("starting", StringComparison.OrdinalIgnoreCase) ||
                status.StartsWith("checking", StringComparison.OrdinalIgnoreCase))
                return Color.FromRgb(0xED, 0xB4, 0x00);
            if (status.StartsWith("online", StringComparison.OrdinalIgnoreCase))
                return Color.FromRgb(0x3E, 0xCF, 0x8E);
            if (status.StartsWith("offline", StringComparison.OrdinalIgnoreCase))
                return Color.FromRgb(0xED, 0x6A, 0x5A);
            return Color.FromRgb(0x6A, 0x6A, 0x6A);
        }

        // ---------- Chrome ----------

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
        // its invisible resize frame hangs off the screen edges — that cut off the
        // title bar and the top corners in full screen. Pad the content by exactly
        // that frame while maximized, and drop the rounded corners there.
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

        protected override void OnClosed(EventArgs e)
        {
            NewsroomBackend.StatusChanged -= OnBackendStatusChanged;
            SectionHost.Content = null;
            foreach (var view in _views.Values)
                if (view is INewsroomView section) section.Shutdown(); // stops its auto-refresh timer
            _views.Clear();
            base.OnClosed(e);
        }
    }
}

using System;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// Borderless pop-out window for Market Research (docs/market-research-plan.md §2.1, MR1). It owns no
    /// research state: HedgeFundWindow moves its single <see cref="ResearchView"/> in with <see cref="Attach"/>
    /// and takes it back with <see cref="ReleaseView"/> from the <c>Closed</c> event, so there is never a second
    /// backend client or a second set of timers.
    /// </summary>
    public partial class ResearchPopoutWindow : Window
    {
        private const double DefaultWidth = 1440;
        private const double DefaultHeight = 900;

        public ResearchPopoutWindow()
        {
            InitializeComponent();
            // user request 2026-10-23: content window — starts maximized within the monitor's work area (taskbar visible)
            UFOS.ai.Shared.WindowMaximize.ConstrainToWorkArea(this);
            UFOS.ai.Shared.WindowMaximize.StartMaximized(this);
            Window_StateChanged(this, EventArgs.Empty);
            // retest 2026-10-23: closing this owned borderless window returned focus to the MainWindow — activate the owner instead
            Closing += (_, e) => { if (!e.Cancel) UFOS.ai.HedgeFund.Fund.FundUi.ActivateOwnerOnClose(this); };
            // Default 1440 × 900 (min 1100 × 680), but never larger than the work area of a small screen.
            var work = SystemParameters.WorkArea;
            Width = Math.Max(MinWidth, Math.Min(DefaultWidth, work.Width));
            Height = Math.Max(MinHeight, Math.Min(DefaultHeight, work.Height));
        }

        /// <summary>The hosted view, or null after <see cref="ReleaseView"/>.</summary>
        public ResearchView? View => ViewHost.Child as ResearchView;

        /// <summary>Puts the view into this window. The caller must have removed it from its previous parent.</summary>
        public void Attach(ResearchView view)
        {
            ViewHost.Child = view;
        }

        /// <summary>Removes the view from this window (it can then be re-parented) and returns it.</summary>
        public ResearchView? ReleaseView()
        {
            var view = ViewHost.Child as ResearchView;
            ViewHost.Child = null;
            return view;
        }

        // =====================================================================================
        // Window chrome (same pattern as HedgeFundWindow / Platform/KeysAndDataWindow,
        // conventions "Borderless windows")
        // =====================================================================================

        // Dragging the title bar of a MAXIMIZED window: DragMove() does nothing there. Like a native
        // title bar, the window is restored as soon as the mouse actually moves with the button held
        // (a plain click does nothing), placed so the cursor keeps its relative spot, then dragged.
        private bool _restoreOnDrag;
        private Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (e.ClickCount == 2)
            {
                ToggleMaximize();
                return;
            }
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

        // Maximized borderless window: pad by the resize frame (+ padded border) so the title bar
        // isn't cut off, and use square corners; restore both when normal.
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            if (WindowState == WindowState.Maximized)
            {
                // 2026-10-23: the WM_GETMINMAXINFO hook (Shared/WindowMaximize.cs) fits the maximized window to the work area —
                // no frame padding (the old padding left a gap and the window covered the taskbar)
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(0);
                TitleBarBorder.CornerRadius = new CornerRadius(0);
                MaximizeGlyph.Text = "❐";
                MaximizeButton.ToolTip = "Restore";
                System.Windows.Automation.AutomationProperties.SetName(MaximizeButton, "Restore");
            }
            else
            {
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(8);
                TitleBarBorder.CornerRadius = new CornerRadius(8, 8, 0, 0);
                MaximizeGlyph.Text = "▢";
                MaximizeButton.ToolTip = "Maximize";
                System.Windows.Automation.AutomationProperties.SetName(MaximizeButton, "Maximize");
            }
        }

        private void ToggleMaximize()
        {
            WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void MaximizeButton_Click(object sender, RoutedEventArgs e)
        {
            ToggleMaximize();
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }
    }
}

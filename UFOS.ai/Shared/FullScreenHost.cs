using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// The ONE full-screen building block of every chart, map and large table (user rule 2026-10-24: a "⛶" control, Esc exits, same
    /// behaviour everywhere; contract: CHART_FULLSCREEN_TASK.md). Linked into every module project (..\Shared\*.cs).
    ///
    /// How: IN PLACE — nothing is re-parented. Moving a WebView2 (the Research chart, the Global Monitoring globe) to another window or
    /// out of the visual tree re-creates it (ChartHost re-creates its WebView on Unloaded / Loaded; the HwndHost WebView2 destroys its
    /// HWND), which would reload the page and lose drawings / zoom. Instead the owner window becomes a borderless, topmost window over
    /// the WHOLE monitor it is on (taskbar area included) and every element that is not on the path from the window to the target is
    /// collapsed; on the path, margins / paddings go to 0, Grid cells span the whole grid, ScrollViewers stop scrolling (the target gets
    /// the viewport), children of other panels get the window size. Every change is recorded (local values incl. bindings) and undone
    /// on exit in reverse order; the window gets back its exact state (normal / maximized, position, size, topmost). The same chart
    /// instance keeps running — live updates continue, no reload.
    ///
    /// A thin bar at the top (outside the target, so no airspace issue with an HwndHost) shows the caller's title / header text
    /// (refreshed every second — e.g. symbol, last price, change) and "Exit full screen (Esc)". Esc and F11 exit; F11 also enters
    /// when the caller registers it (<see cref="RegisterF11"/>). One session per window.
    /// </summary>
    internal sealed class FullScreenHost
    {
        private const double BarHeight = 30;
        private static readonly ConditionalWeakTable<Window, FullScreenHost> Sessions = new();

        private readonly Window _window;
        private readonly FrameworkElement _target;
        private readonly string _title;
        private readonly Func<string?>? _headerText;
        private readonly List<Action> _undo = new();
        private readonly List<(FrameworkElement Element, bool Vertical)> _sizeToWindow = new();
        private BarAdorner? _bar;
        private UIElement? _adornedRoot;
        private DispatcherTimer? _headerTimer;
        private WindowState _savedState;
        private Rect _savedBounds;
        private bool _savedTopmost;
        private bool _exited;
        private bool _armed;   // keys are honoured only after the entering key press is over (F11 must not exit at once)
        private KeyEventHandler? _keyHandler;

        /// <summary>Raised on the UI thread after a session started (true) or ended (false) for the window.</summary>
        public static event Action<Window, bool>? Changed;

        private FullScreenHost(Window window, FrameworkElement target, string title, Func<string?>? headerText)
        {
            _window = window;
            _target = target;
            _title = title;
            _headerText = headerText;
        }

        /// <summary>The running session of the window that hosts <paramref name="element"/>, or null.</summary>
        public static FullScreenHost? ActiveFor(DependencyObject element)
        {
            var w = element as Window ?? Window.GetWindow(element);
            return w != null && Sessions.TryGetValue(w, out var s) ? s : null;
        }

        public static bool IsActive(FrameworkElement target) => ActiveFor(target)?._target == target;

        /// <summary>
        /// Full screen for <paramref name="target"/>. Returns the session, or null when its window is not loaded or the layout could not
        /// be changed — then everything already changed is undone and the reason is shown in a message box (never silent).
        /// </summary>
        public static FullScreenHost? Enter(FrameworkElement target, string title, Func<string?>? headerText = null)
        {
            var window = Window.GetWindow(target);
            if (window == null || !window.IsLoaded || !target.IsLoaded) return null;
            ActiveFor(window)?.Exit();
            var host = new FullScreenHost(window, target, title, headerText);
            try
            {
                host.Start();
                return host;
            }
            catch (Exception ex)
            {
                host.Exit();
                MessageBox.Show(window, "Full screen could not be started (" + ex.GetType().Name + ": " + ex.Message + "). The view is unchanged.",
                                title, MessageBoxButton.OK, MessageBoxImage.Warning);
                return null;
            }
        }

        /// <summary>Enters, or exits when <paramref name="target"/> is already full screen.</summary>
        public static void Toggle(FrameworkElement target, string title, Func<string?>? headerText = null)
        {
            if (IsActive(target)) ActiveFor(target)!.Exit();
            else Enter(target, title, headerText);
        }

        /// <summary>The standard "⛶" button (tooltip "Full screen (Esc to exit)", AutomationName) toggling full screen for the target.</summary>
        public static Button CreateButton(Func<FrameworkElement?> target, string title, Func<string?>? headerText = null, Style? style = null)
        {
            var b = new Button
            {
                Content = "⛶",
                ToolTip = "Full screen (Esc to exit)",
                Padding = new Thickness(8, 2, 8, 2),
                Margin = new Thickness(6, 0, 0, 0),
                Cursor = Cursors.Hand,
            };
            if (style != null) b.Style = style;
            AutomationProperties.SetName(b, "Full screen: " + title + " (Esc to exit)");
            b.Click += (_, _) => { if (target() is FrameworkElement t) Toggle(t, title, headerText); };
            return b;
        }

        /// <summary>Wires an existing "⛶" button (e.g. from XAML) with the standard tooltip, AutomationName and toggle.</summary>
        public static void Wire(ButtonBase button, Func<FrameworkElement?> target, string title, Func<string?>? headerText = null)
        {
            button.ToolTip = "Full screen (Esc to exit)";
            AutomationProperties.SetName(button, "Full screen: " + title + " (Esc to exit)");
            button.Click += (_, _) => { if (target() is FrameworkElement t) Toggle(t, title, headerText); };
        }

        /// <summary>
        /// F11 while the keyboard focus is inside <paramref name="scope"/> toggles full screen for the target. Listens to the bubbling
        /// KeyDown incl. handled events: a WebView2 raises function keys / Esc as WPF KeyDown on its control (accelerator keys), never as
        /// PreviewKeyDown — so the same handler works for WPF content and for a focused web page.
        /// </summary>
        public static void RegisterF11(UIElement scope, Func<FrameworkElement?> target, string title, Func<string?>? headerText = null)
        {
            scope.AddHandler(UIElement.KeyDownEvent, new KeyEventHandler((_, e) =>
            {
                if (e.Key != Key.F11 || !scope.IsKeyboardFocusWithin) return;
                if (target() is FrameworkElement t && !IsActive(t))   // exiting is the session's own Esc / F11 handler
                {
                    Enter(t, title, headerText);
                    e.Handled = true;
                }
            }), handledEventsToo: true);
        }

        // ------------------------------------------------------------------ enter / exit

        private void Start()
        {
            Sessions.AddOrUpdate(_window, this);
            _savedState = _window.WindowState;
            _savedBounds = _window.WindowState == WindowState.Normal
                ? new Rect(_window.Left, _window.Top, _window.ActualWidth, _window.ActualHeight)
                : _window.RestoreBounds;
            _savedTopmost = _window.Topmost;
            // normal state FIRST: windows re-style themselves on StateChanged (corner radius / frame padding) — the changes below win
            if (_window.WindowState != WindowState.Normal) _window.WindowState = WindowState.Normal;
            // no frame, no resize grip (all module windows are already borderless; a framed window becomes borderless here)
            if (_window.WindowStyle != WindowStyle.None) Set(_window, Window.WindowStyleProperty, WindowStyle.None);
            if (_window.ResizeMode != ResizeMode.NoResize) Set(_window, Window.ResizeModeProperty, ResizeMode.NoResize);

            // a fixed-size target (e.g. a chart with Height = 230) must fill the screen
            Set(_target, FrameworkElement.WidthProperty, double.NaN);
            Set(_target, FrameworkElement.HeightProperty, double.NaN);
            Set(_target, FrameworkElement.MaxWidthProperty, double.PositiveInfinity);
            Set(_target, FrameworkElement.MaxHeightProperty, double.PositiveInfinity);
            CollapseAroundTarget();
            // the bar strip on top of the target (outside it — an HwndHost target would hide an overlay)
            Remember(_target, FrameworkElement.MarginProperty);
            _target.Margin = new Thickness(0, BarHeight, 0, 0);

            // the whole monitor of the window (taskbar area included)
            var hwnd = new WindowInteropHelper(_window).Handle;
            if (hwnd != IntPtr.Zero && MonitorRect(hwnd) is RECT r && PresentationSource.FromVisual(_window)?.CompositionTarget is CompositionTarget ct)
            {
                // device pixels → DIPs, set through the WPF properties so the restore below is exact
                var fromDevice = ct.TransformFromDevice;
                var topLeft = fromDevice.Transform(new Point(r.Left, r.Top));
                var bottomRight = fromDevice.Transform(new Point(r.Right, r.Bottom));
                _window.Left = topLeft.X;
                _window.Top = topLeft.Y;
                _window.Width = bottomRight.X - topLeft.X;
                _window.Height = bottomRight.Y - topLeft.Y;
            }
            _window.Topmost = true;
            _window.Activated += Window_Activated;
            _window.Deactivated += Window_Deactivated;
            _window.AddHandler(UIElement.KeyDownEvent, _keyHandler = new KeyEventHandler(Window_KeyDown), handledEventsToo: true);
            // the F11 press that entered is still routing up to the window — honour keys only after it
            _window.Dispatcher.BeginInvoke(new Action(() => _armed = true), DispatcherPriority.Input);
            _window.Closing += Window_Closing;
            _target.Unloaded += Target_Unloaded;   // the view is moved / closed (e.g. a pop-out) → restore first

            _window.UpdateLayout();
            ApplyWindowSizes();
            _window.SizeChanged += Window_SizeChanged;

            if (_window.Content is UIElement root && AdornerLayer.GetAdornerLayer(root) is AdornerLayer layer)
            {
                _adornedRoot = root;
                _bar = new BarAdorner(root, _title, Exit, _window);
                layer.Add(_bar);
                UpdateHeader();
                if (_headerText != null)
                {
                    _headerTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
                    _headerTimer.Tick += (_, _) => UpdateHeader();
                    _headerTimer.Start();
                }
            }
            _target.Focus();
            Changed?.Invoke(_window, true);
        }

        /// <summary>Ends the session and restores everything (idempotent).</summary>
        public void Exit()
        {
            if (_exited) return;
            _exited = true;
            _headerTimer?.Stop();
            _window.Activated -= Window_Activated;
            _window.Deactivated -= Window_Deactivated;
            if (_keyHandler != null) _window.RemoveHandler(UIElement.KeyDownEvent, _keyHandler);
            _window.Closing -= Window_Closing;
            _window.SizeChanged -= Window_SizeChanged;
            _target.Unloaded -= Target_Unloaded;
            if (_bar != null && _adornedRoot != null) AdornerLayer.GetAdornerLayer(_adornedRoot)?.Remove(_bar);
            for (var i = _undo.Count - 1; i >= 0; i--)
            {
                try { _undo[i](); }
                catch (Exception ex) { System.Diagnostics.Debug.WriteLine("FullScreenHost: restore step failed: " + ex.Message); }
            }
            _undo.Clear();
            _window.Topmost = _savedTopmost;
            if (_savedBounds.Width > 0 && _savedBounds.Height > 0)
            {
                _window.Left = _savedBounds.Left;
                _window.Top = _savedBounds.Top;
                _window.Width = _savedBounds.Width;
                _window.Height = _savedBounds.Height;
            }
            if (_savedState != WindowState.Normal) _window.WindowState = _savedState;
            if (Sessions.TryGetValue(_window, out var s) && ReferenceEquals(s, this)) Sessions.Remove(_window);
            Changed?.Invoke(_window, false);
        }

        private void Window_KeyDown(object sender, KeyEventArgs e)
        {
            if (!_armed) return;
            // Esc already used by an element inside (closing a popup / dropdown, cancelling an edit) closes only that first
            if (e.Key == Key.Escape && e.Handled) return;
            if (e.Key == Key.Escape || e.Key == Key.F11)
            {
                e.Handled = true;
                Exit();
            }
        }

        private void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e) => Exit();
        private void Target_Unloaded(object sender, RoutedEventArgs e) => Exit();
        private void Window_Activated(object? sender, EventArgs e) => _window.Topmost = true;
        private void Window_Deactivated(object? sender, EventArgs e) => _window.Topmost = false;   // Alt+Tab shows other windows
        private void Window_SizeChanged(object sender, SizeChangedEventArgs e) => ApplyWindowSizes();

        private void UpdateHeader()
        {
            if (_bar == null) return;
            string? text = null;
            try { text = _headerText?.Invoke(); }
            catch (Exception ex) { text = "(header unavailable: " + ex.GetType().Name + ")"; }
            _bar.SetHeader(string.IsNullOrWhiteSpace(text) ? _title : _title + "  ·  " + text);
        }

        // ------------------------------------------------------------------ layout changes (all recorded)

        private void CollapseAroundTarget()
        {
            DependencyObject child = _target;
            var parent = ParentOf(child);
            while (parent != null && child is not Window)
            {
                if (child is FrameworkElement fe && !ReferenceEquals(fe, _target))
                {
                    Set(fe, FrameworkElement.MarginProperty, new Thickness(0));
                    Set(fe, FrameworkElement.MaxWidthProperty, double.PositiveInfinity);
                    Set(fe, FrameworkElement.MaxHeightProperty, double.PositiveInfinity);
                }
                if (child is FrameworkElement c)
                {
                    Set(c, FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Stretch);
                    Set(c, FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Stretch);
                }
                switch (parent)
                {
                    case Grid g:
                        CollapseSiblings(g, child);
                        Set(child, Grid.RowProperty, 0);
                        Set(child, Grid.RowSpanProperty, Math.Max(1, g.RowDefinitions.Count));
                        Set(child, Grid.ColumnProperty, 0);
                        Set(child, Grid.ColumnSpanProperty, Math.Max(1, g.ColumnDefinitions.Count));
                        break;
                    case Canvas:
                        break;   // absolute layout: nothing sensible to stretch
                    case StackPanel sp when child is FrameworkElement spc:
                        CollapseSiblings(sp, child);
                        _sizeToWindow.Add((spc, sp.Orientation == Orientation.Vertical));
                        Remember(spc, sp.Orientation == Orientation.Vertical ? FrameworkElement.HeightProperty : FrameworkElement.WidthProperty);
                        break;
                    case Panel p when child is FrameworkElement pc:
                        CollapseSiblings(p, child);
                        _sizeToWindow.Add((pc, true));
                        Remember(pc, FrameworkElement.HeightProperty);
                        break;
                    case ScrollViewer sv:
                        Set(sv, ScrollViewer.VerticalScrollBarVisibilityProperty, ScrollBarVisibility.Disabled);
                        Set(sv, ScrollViewer.HorizontalScrollBarVisibilityProperty, ScrollBarVisibility.Disabled);
                        Set(sv, Control.PaddingProperty, new Thickness(0));
                        sv.ScrollToHome();
                        break;
                    case Border b:
                        Set(b, Border.PaddingProperty, new Thickness(0));
                        Set(b, Border.CornerRadiusProperty, new CornerRadius(0));   // square screen corners (rounded window chrome)
                        break;
                    case Control ctl:
                        Set(ctl, Control.PaddingProperty, new Thickness(0));
                        break;
                }
                if (parent is Window) break;
                child = parent;
                parent = ParentOf(child);
            }
        }

        private void CollapseSiblings(Panel panel, DependencyObject keep)
        {
            foreach (UIElement c in panel.Children)
                if (!ReferenceEquals(c, keep)) Set(c, UIElement.VisibilityProperty, Visibility.Collapsed);
        }

        /// <summary>Children of stacking panels (StackPanel / DockPanel / WrapPanel …) get the window's content size.</summary>
        private void ApplyWindowSizes()
        {
            if (_sizeToWindow.Count == 0 || _window.Content is not FrameworkElement root) return;
            foreach (var (e, vertical) in _sizeToWindow)
            {
                // the target itself sits below the bar strip (its top margin)
                var bar = ReferenceEquals(e, _target) ? BarHeight : 0;
                if (vertical) e.Height = Math.Max(0, root.ActualHeight - bar);
                else e.Width = Math.Max(0, root.ActualWidth);
            }
        }

        private static DependencyObject? ParentOf(DependencyObject d)
            => LogicalTreeHelper.GetParent(d) ?? (d is Visual || d is System.Windows.Media.Media3D.Visual3D ? VisualTreeHelper.GetParent(d) : null);

        private void Set(DependencyObject d, DependencyProperty p, object value)
        {
            Remember(d, p);
            d.SetValue(p, value);
        }

        /// <summary>Records the LOCAL value (a binding is restored as the binding, an unset value is cleared again).</summary>
        private void Remember(DependencyObject d, DependencyProperty p)
        {
            var local = d.ReadLocalValue(p);
            if (local is BindingExpressionBase be)
            {
                var binding = be.ParentBindingBase;
                _undo.Add(() => BindingOperations.SetBinding(d, p, binding));
            }
            else if (local == DependencyProperty.UnsetValue)
            {
                _undo.Add(() => d.ClearValue(p));
            }
            else
            {
                _undo.Add(() => d.SetValue(p, local));
            }
        }

        // ------------------------------------------------------------------ the top bar

        private sealed class BarAdorner : Adorner
        {
            private readonly Grid _bar;
            private readonly TextBlock _header;

            public BarAdorner(UIElement adorned, string title, Action exit, Window window) : base(adorned)
            {
                var background = window.TryFindResource("ModulePanelBackgroundBrush") as Brush ?? window.TryFindResource("PanelBrush") as Brush ?? Brushes.Black;
                var foreground = window.TryFindResource("PrimaryForegroundBrush") as Brush ?? window.TryFindResource("TextBrightBrush") as Brush ?? Brushes.White;
                var muted = window.TryFindResource("MutedForegroundBrush") as Brush ?? Brushes.Gray;
                _header = new TextBlock
                {
                    Text = title,
                    Foreground = foreground,
                    FontSize = 12,
                    VerticalAlignment = VerticalAlignment.Center,
                    Margin = new Thickness(12, 0, 12, 0),
                    TextTrimming = TextTrimming.CharacterEllipsis,
                };
                AutomationProperties.SetName(_header, "Full screen: " + title);
                var hint = new TextBlock { Text = "Esc", Foreground = muted, FontSize = 10.5, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 0) };
                var exitButton = new Button
                {
                    Content = "Exit full screen",
                    Padding = new Thickness(10, 2, 10, 2),
                    Margin = new Thickness(0, 3, 8, 3),
                    Cursor = Cursors.Hand,
                    Foreground = foreground,
                    Background = Brushes.Transparent,
                    BorderBrush = muted,
                    BorderThickness = new Thickness(1),
                };
                AutomationProperties.SetName(exitButton, "Exit full screen (Esc)");
                exitButton.Click += (_, _) => exit();
                _bar = new Grid { Background = background, Height = BarHeight };
                _bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                _bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                _bar.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                _bar.Children.Add(_header);
                Grid.SetColumn(hint, 1);
                _bar.Children.Add(hint);
                Grid.SetColumn(exitButton, 2);
                _bar.Children.Add(exitButton);
                AddVisualChild(_bar);
                AddLogicalChild(_bar);
            }

            public void SetHeader(string text) => _header.Text = text;

            protected override int VisualChildrenCount => 1;
            protected override Visual GetVisualChild(int index) => _bar;

            protected override Size MeasureOverride(Size constraint)
            {
                _bar.Measure(new Size(AdornedElement.RenderSize.Width, BarHeight));
                return new Size(AdornedElement.RenderSize.Width, BarHeight);
            }

            protected override Size ArrangeOverride(Size finalSize)
            {
                _bar.Arrange(new Rect(0, 0, AdornedElement.RenderSize.Width, BarHeight));
                return finalSize;
            }
        }

        // ------------------------------------------------------------------ monitor (Win32)

        private static RECT? MonitorRect(IntPtr hwnd)
        {
            const int MONITOR_DEFAULTTONEAREST = 2;
            var monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
            if (monitor == IntPtr.Zero) return null;
            var info = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
            return GetMonitorInfo(monitor, ref info) ? info.rcMonitor : null;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct RECT
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MONITORINFO
        {
            public int cbSize;
            public RECT rcMonitor;
            public RECT rcWork;
            public int dwFlags;
        }

        [DllImport("user32.dll")]
        private static extern IntPtr MonitorFromWindow(IntPtr handle, int flags);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO lpmi);
    }
}

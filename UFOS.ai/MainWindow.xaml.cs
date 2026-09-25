using System;
using System.ComponentModel;
using System.Globalization;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using GlobalMonitoringWindow = UFOS.ai.GlobalMonitoring.MainWindow;
using UFOS.ai.HedgeFund;
using UFOS.ai.Logging;
using UFOS.ai.Services;
using UFOS.ai.Shared;

namespace UFOS.ai
{
    public partial class MainWindow : Window
    {
        private static readonly HttpClient OverviewHttpClient = CreateOverviewHttpClient();

        private readonly MarketQuoteService _marketQuoteService = new(OverviewHttpClient);
        private readonly NewsFeedService _newsFeedService = new(OverviewHttpClient);
        private readonly IPortfolioInsightProvider _portfolioInsightProvider = new PlaceholderPortfolioInsightProvider();

        // Kept from the last successful refresh so a card click can open the popup
        // with real data without re-fetching.
        private NewsHeadline? _marketsHeadline;
        private NewsHeadline? _moneyFlowsHeadline;

        private DispatcherTimer? _clockTimer;
        private DispatcherTimer? _settingsCloseTimer;
        private bool _syncingAiNoticeCheckBox;
        private DispatcherTimer? _launchCaptionTimer;
        private DispatcherTimer? _overviewRefreshTimer;
        private bool _ufoFlightStarted;

        public MainWindow()
        {
            InitializeComponent();
            ApplyScreenSizeSafeguards();
        }

        // MinWidth/MinHeight (and the default Width/Height) are declared in XAML as
        // fixed logical-pixel values, but the logical work area actually available
        // shrinks with both screen resolution AND DPI scaling - e.g. a common
        // 1920x1080 display at 175% scaling only has ~1097x617 logical pixels, less
        // than this window's declared MinHeight="640". When that happens WPF simply
        // refuses to size the window below its Min* constraints, so it ends up bigger
        // than the screen with edges (the title bar's Close button, or the bottom
        // module row) permanently off-screen and unreachable. Clamping Min*/initial
        // size to the actual work area at startup - and never raising them - fixes
        // that without changing behavior on any screen that already fits comfortably.
        private void ApplyScreenSizeSafeguards()
        {
            var workArea = SystemParameters.WorkArea;

            MinWidth = Math.Min(MinWidth, workArea.Width);
            MinHeight = Math.Min(MinHeight, workArea.Height);

            if (Width > workArea.Width) Width = workArea.Width;
            if (Height > workArea.Height) Height = workArea.Height;
        }

        // WindowStyle="None" + AllowsTransparency="True" windows overflow past the
        // monitor's actual work area when maximized (Windows sizes them to the raw
        // monitor rect instead, which is larger than the visible desktop by the hidden
        // resize-border thickness and covers the taskbar) - that overflow is exactly
        // what clips the title bar's Minimize/Close buttons off-screen when fullscreened,
        // and the exact amount varies by monitor/DPI, which is why it only shows up on
        // some screens. Overriding WM_GETMINMAXINFO constrains the maximized bounds to
        // the real work area instead.
        protected override void OnSourceInitialized(EventArgs e)
        {
            base.OnSourceInitialized(e);
            if (PresentationSource.FromVisual(this) is HwndSource hwndSource)
            {
                hwndSource.AddHook(WindowProc);
            }
        }

        private static IntPtr WindowProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            const int WM_GETMINMAXINFO = 0x0024;
            if (msg == WM_GETMINMAXINFO)
            {
                ConstrainMaximizedBoundsToWorkArea(hwnd, lParam);
                handled = true;
            }

            return IntPtr.Zero;
        }

        private static void ConstrainMaximizedBoundsToWorkArea(IntPtr hwnd, IntPtr lParam)
        {
            var mmi = Marshal.PtrToStructure<MINMAXINFO>(lParam);

            const int MONITOR_DEFAULTTONEAREST = 2;
            var monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
            if (monitor != IntPtr.Zero)
            {
                var monitorInfo = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
                GetMonitorInfo(monitor, ref monitorInfo);

                var workArea = monitorInfo.rcWork;
                var monitorArea = monitorInfo.rcMonitor;

                mmi.ptMaxPosition.X = Math.Abs(workArea.Left - monitorArea.Left);
                mmi.ptMaxPosition.Y = Math.Abs(workArea.Top - monitorArea.Top);
                mmi.ptMaxSize.X = Math.Abs(workArea.Right - workArea.Left);
                mmi.ptMaxSize.Y = Math.Abs(workArea.Bottom - workArea.Top);
            }

            Marshal.StructureToPtr(mmi, lParam, true);
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct POINT
        {
            public int X;
            public int Y;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MINMAXINFO
        {
            public POINT ptReserved;
            public POINT ptMaxSize;
            public POINT ptMaxPosition;
            public POINT ptMinTrackSize;
            public POINT ptMaxTrackSize;
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
        private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO lpmi);

        private static HttpClient CreateOverviewHttpClient()
        {
            var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            client.DefaultRequestHeaders.UserAgent.ParseAdd("Mozilla/5.0 (compatible; UFOSai/1.0)");
            return client;
        }

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            // Reflect the persisted startup-notice opt-out without writing it back.
            _syncingAiNoticeCheckBox = true;
            ShowAiNoticeCheckBox.IsChecked = AiDisclaimerWindow.IsShownAtStartup;
            _syncingAiNoticeCheckBox = false;

            _clockTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _clockTimer.Tick += (_, __) => UpdateClock();
            _clockTimer.Start();
            UpdateClock();

            // Wait one render pass so HeadingText has a real ActualWidth/ActualHeight
            // before we build the UFO's flight path from it (mirrors the reference
            // design's JS, which measures the rendered heading before animating).
            Dispatcher.BeginInvoke(new Action(SetupUfoFlight), DispatcherPriority.Loaded);

            _overviewRefreshTimer = new DispatcherTimer { Interval = TimeSpan.FromMinutes(5) };
            _overviewRefreshTimer.Tick += (_, __) => _ = RefreshOverviewCardsAsync();
            _overviewRefreshTimer.Start();
            _ = RefreshOverviewCardsAsync();
        }

        private void Window_Closing(object? sender, CancelEventArgs e)
        {
            _clockTimer?.Stop();
            _settingsCloseTimer?.Stop();
            _launchCaptionTimer?.Stop();
            _overviewRefreshTimer?.Stop();
        }

        private void UpdateClock()
        {
            ClockText.Text = DateTime.Now.ToString("HH:mm:ss");
        }

        // ===================== Overview bar (Markets / Finance / Money Flows / =====================
        // ===================== Verse of the Day / Portfolio Insight cards)      =====================

        private async Task RefreshOverviewCardsAsync()
        {
            RefreshVerseOfTheDayCard();
            await RefreshFinanceCardAsync();
            await RefreshNewsCardsAsync();
            await RefreshPortfolioInsightCardAsync();
        }

        private void RefreshVerseOfTheDayCard()
        {
            var verse = VerseOfTheDay.GetForToday();
            VerseReferenceText.Text = verse.Reference;
            VerseQuoteText.Text = $"\"{verse.Text}\"";
        }

        private async Task RefreshFinanceCardAsync()
        {
            try
            {
                var quote = await _marketQuoteService.GetQuoteAsync("^GSPC");
                if (quote is null) return;

                FinanceValueText.Text = quote.Price.ToString("N2", CultureInfo.InvariantCulture);

                if (quote.ChangePercent is double changePercent)
                {
                    string sign = changePercent >= 0 ? "+" : string.Empty;
                    FinanceChangeText.Text = $"{sign}{changePercent.ToString("N2", CultureInfo.InvariantCulture)}% today · S&P 500";
                    FinanceValueText.Foreground = (Brush)FindResource(changePercent >= 0 ? "PositiveGreenBrush" : "AccentRedBrush");
                }
                else
                {
                    FinanceChangeText.Text = $"S&P 500 · {FormatRelativeAgo(quote.FetchedAt)}";
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("Overview", "Failed to refresh Finance card", ex); } catch { }
            }
        }

        private async Task RefreshNewsCardsAsync()
        {
            try
            {
                var headlines = await _newsFeedService.GetTopStoriesAsync();
                if (headlines.Count == 0) return;

                var marketsHeadline = headlines[0];
                _marketsHeadline = marketsHeadline;
                MarketsHeadlineText.Text = marketsHeadline.Title;
                MarketsSourceText.Text = $"{marketsHeadline.Source} · {FormatRelativeAgo(marketsHeadline.PublishedAt)}";

                var moneyFlowsHeadline = NewsFeedService.PickMoneyFlowsHeadline(headlines);
                if (moneyFlowsHeadline is not null)
                {
                    _moneyFlowsHeadline = moneyFlowsHeadline;
                    MoneyFlowsHeadlineText.Text = moneyFlowsHeadline.Title;
                    MoneyFlowsSourceText.Text = $"{moneyFlowsHeadline.Source} · {FormatRelativeAgo(moneyFlowsHeadline.PublishedAt)}";
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("Overview", "Failed to refresh Markets/Money Flows cards", ex); } catch { }
            }
        }

        private async Task RefreshPortfolioInsightCardAsync()
        {
            try
            {
                // Stays illustrative (existing static text left untouched) until a real
                // portfolio/broker microservice implements IPortfolioInsightProvider.
                var insight = await _portfolioInsightProvider.TryGetLatestAsync();
                if (insight is null) return;

                PortfolioInsightHeadlineText.Text = insight.Headline;
                if (insight.AiModel is not null)
                {
                    // AI-written insight -> "✦ AI-GENERATED · model · time" (EU AI Act Art. 50,
                    // see Shared/AiContent.cs). A real provider MUST set AiModel whenever an
                    // LLM wrote the headline/detail.
                    PortfolioInsightDetailText.Text = $"{AiContent.Badge(insight.AiModel, insight.GeneratedAt)} · {insight.Detail}";
                    PortfolioInsightDetailText.Foreground = TryFindResource("AiAccentBrush") as Brush ?? PortfolioInsightDetailText.Foreground;
                    PortfolioInsightDetailText.ToolTip = AiContent.Caveat;
                    System.Windows.Automation.AutomationProperties.SetName(PortfolioInsightDetailText,
                        AiContent.AutomationName(insight.AiModel, insight.GeneratedAt));
                }
                else
                {
                    PortfolioInsightDetailText.Text = $"{insight.Detail} · {FormatRelativeAgo(insight.GeneratedAt)}";
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("Overview", "Failed to refresh Portfolio Insight card", ex); } catch { }
            }
        }

        private static string FormatRelativeAgo(DateTimeOffset timestamp)
        {
            var elapsed = DateTimeOffset.Now - timestamp;
            if (elapsed < TimeSpan.Zero) elapsed = TimeSpan.Zero;

            if (elapsed.TotalMinutes < 1) return "just now";
            if (elapsed.TotalHours < 1) return $"{(int)elapsed.TotalMinutes}m ago";
            if (elapsed.TotalDays < 1) return $"{(int)elapsed.TotalHours}h ago";
            return $"{(int)elapsed.TotalDays}d ago";
        }

        private void MarketsCard_Click(object sender, RoutedEventArgs e)
        {
            if (_marketsHeadline is not null)
            {
                new ArticleWindow(_marketsHeadline, this).Show();
            }
        }

        private void MoneyFlowsCard_Click(object sender, RoutedEventArgs e)
        {
            if (_moneyFlowsHeadline is not null)
            {
                new ArticleWindow(_moneyFlowsHeadline, this).Show();
            }
        }

        // ===================== Window chrome (unchanged behavior) =====================

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.ClickCount == 2)
            {
                WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                return;
            }

            if (e.LeftButton != MouseButtonState.Pressed)
            {
                return;
            }

            if (WindowState == WindowState.Maximized)
            {
                // A plain DragMove() on an already-maximized borderless window doesn't
                // restore it first (unlike a native title bar), so dragging did nothing
                // and the window was effectively stuck full-screen. Restore it under the
                // cursor first, then let the drag continue normally.
                var cursorScreenPos = PointToScreen(e.GetPosition(this));
                var mouseRatioX = e.GetPosition(this).X / Math.Max(1, ActualWidth);
                var restoreBounds = RestoreBounds;

                WindowState = WindowState.Normal;

                Left = cursorScreenPos.X - (restoreBounds.Width * mouseRatioX);
                Top = cursorScreenPos.Y - 10;
            }

            try { DragMove(); } catch { }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        // ===================== UFO flight animation =====================
        //
        // The reference HTML/CSS/JS design uses a CSS `offset-path` on a closed loop
        // plus `offset-rotate: auto` to make the UFO bank into its turns, landing at a
        // point on the loop where the tangent is horizontal (so the landing tilt is
        // gentle, not steep). The WPF equivalent of that whole mechanism is
        // MatrixAnimationUsingPath with DoesRotateWithTangent="true" - it animates a
        // Matrix (rotation + translation) along a PathGeometry in one step, which is
        // exactly the same effect. The landing target position/size is computed from
        // the actual rendered "UFOS.ai" heading, the same way the JS measured
        // getBoundingClientRect() on the heading before building the path.
        private void SetupUfoFlight()
        {
            if (_ufoFlightStarted) return;

            double w = HeadingText.ActualWidth;
            double h = HeadingText.ActualHeight;
            if (w <= 0 || h <= 0)
            {
                // Layout not ready yet - try again shortly instead of skipping the animation.
                Dispatcher.BeginInvoke(new Action(SetupUfoFlight), DispatcherPriority.Loaded);
                return;
            }

            _ufoFlightStarted = true;

            double charWidth = w / Math.Max(HeadingText.Text.Length, 1);
            double targetX = w - 3 + charWidth;
            double targetY = h * -0.06;
            double halfW = (w + 40) / 2;
            double dropH = h * 1.3;

            // Closed loop whose "top" sits exactly at the landing target, so the
            // path's tangent there is horizontal (gentle bank on landing) rather
            // than vertical (steep bank), matching the fix made in the HTML design.
            var figure = new PathFigure { StartPoint = new Point(targetX, targetY), IsClosed = true, IsFilled = false };
            figure.Segments.Add(new BezierSegment(
                new Point(targetX + halfW, targetY),
                new Point(targetX + halfW, targetY + dropH),
                new Point(targetX, targetY + dropH),
                isStroked: true));
            figure.Segments.Add(new BezierSegment(
                new Point(targetX - halfW, targetY + dropH),
                new Point(targetX - halfW, targetY),
                new Point(targetX, targetY),
                isStroked: true));

            var geometry = new PathGeometry();
            geometry.Figures.Add(figure);
            geometry.Freeze();

            // Position the landed (monochrome, superscript-sized) icon at the same
            // target point. Its viewBox is 100x60 (non-square), so the vertical
            // centering must account for that aspect ratio rather than treating the
            // icon as a square box.
            const double landedSize = 21;
            double landedHeight = landedSize * 0.6;
            Canvas.SetLeft(UfoLandedGroup, targetX - landedSize / 2);
            Canvas.SetTop(UfoLandedGroup, targetY - landedHeight / 2);

            var matrixTransform = new MatrixTransform();
            UfoFlightGroup.RenderTransform = matrixTransform;

            var anim = new MatrixAnimationUsingPath
            {
                PathGeometry = geometry,
                Duration = TimeSpan.FromSeconds(9),
                DoesRotateWithTangent = true,
                FillBehavior = FillBehavior.HoldEnd
            };
            anim.Completed += (_, __) => OnUfoLanded(matrixTransform);

            matrixTransform.BeginAnimation(MatrixTransform.MatrixProperty, anim);
        }

        private void OnUfoLanded(MatrixTransform matrixTransform)
        {
            // Read the ACTUAL final computed rotation off the matrix the animation
            // produced, rather than assuming a value, so the landed icon's tilt is
            // guaranteed to match the flying sprite's last frame exactly.
            Matrix m = matrixTransform.Matrix;
            double angleDegrees = Math.Atan2(m.M12, m.M11) * (180 / Math.PI);
            UfoLandedRotate.Angle = angleDegrees;

            var fadeOut = new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(400));
            UfoFlightGroup.BeginAnimation(UIElement.OpacityProperty, fadeOut);

            var fadeIn = new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(400));
            UfoLandedGroup.BeginAnimation(UIElement.OpacityProperty, fadeIn);
        }

        // ===================== Settings popup (mirrors LiveStreamAgent's =====================
        // ===================== 3-second hover-close Popup pattern)      =====================

        private void ShowAiNoticeCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            if (!IsLoaded || _syncingAiNoticeCheckBox) return;
            AiDisclaimerWindow.SetShowAtStartup(ShowAiNoticeCheckBox.IsChecked == true);
        }

        private void AiNoticeLink_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            SettingsPopup.IsOpen = false;
            CancelSettingsCloseTimer();
            AiDisclaimerWindow.ShowForReading(this);
        }

        private void SettingsButton_Click(object sender, RoutedEventArgs e)
        {
            SettingsPopup.IsOpen = !SettingsPopup.IsOpen;
            CancelSettingsCloseTimer();
        }

        private void SettingsHoverArea_MouseEnter(object sender, MouseEventArgs e)
        {
            CancelSettingsCloseTimer();
        }

        private void SettingsHoverArea_MouseLeave(object sender, MouseEventArgs e)
        {
            StartSettingsCloseTimer();
        }

        private void StartSettingsCloseTimer()
        {
            CancelSettingsCloseTimer();
            _settingsCloseTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
            _settingsCloseTimer.Tick += (_, __) =>
            {
                SettingsPopup.IsOpen = false;
                CancelSettingsCloseTimer();
            };
            _settingsCloseTimer.Start();
        }

        private void CancelSettingsCloseTimer()
        {
            _settingsCloseTimer?.Stop();
            _settingsCloseTimer = null;
        }

        private void ShowUfoCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            // IsChecked="True" in XAML fires this Checked event during
            // InitializeComponent, before every named element below it in the
            // document has been connected - skip until the window has actually
            // loaded, since the XAML-default visibility already matches anyway.
            if (!IsLoaded) return;

            bool show = ShowUfoCheckBox.IsChecked == true;
            UfoOverlayCanvas.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        }

        private void ShowVerseCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            if (!IsLoaded) return;

            bool show = ShowVerseCheckBox.IsChecked == true;

            // Removing (not just hiding) Verse from the UniformGrid's Children - and
            // dropping Columns to 4 - makes the remaining cards divide the row exactly
            // the way the module grid below divides its own row into 4 columns, so the
            // two rows' cards line up instead of leaving a gap or using mismatched widths.
            if (show)
            {
                if (!OverviewGrid.Children.Contains(VerseCard))
                {
                    OverviewGrid.Children.Insert(3, VerseCard);
                }
                VerseCard.Visibility = Visibility.Visible;
                OverviewGrid.Columns = 5;
            }
            else
            {
                OverviewGrid.Children.Remove(VerseCard);
                OverviewGrid.Columns = 4;
            }
        }

        // ===================== Add-module tile (dims at rest, lights up on hover) =====================

        private void AddModuleButton_MouseEnter(object sender, MouseEventArgs e)
        {
            AddModuleButton.Opacity = 1.0;
        }

        private void AddModuleButton_MouseLeave(object sender, MouseEventArgs e)
        {
            AddModuleButton.Opacity = 0.5;
        }

        // ===================== Module launch handlers =====================

        // Disables the tile for as long as its window is open - the window is a
        // one-off per module (no docs/tabs), so a second instance would just be a
        // confusing duplicate - and re-enables it as soon as that window closes.
        private void OpenModuleWindow(Button tile, Func<Window?> createAndShow)
        {
            tile.IsEnabled = false;
            try
            {
                var window = createAndShow();
                if (window is null)
                {
                    tile.IsEnabled = true;
                    return;
                }

                window.Closed += (_, __) => tile.IsEnabled = true;
            }
            catch (Exception ex)
            {
                tile.IsEnabled = true;
                (Application.Current as IUiErrorReporter)?.ShowUiException(ex);
            }
        }

        private void OpenLiveStreamAgent_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(LiveStreamAgentTile, () =>
            {
                var window = new LivestreamAgent();
                window.Show();
                return window;
            });
        }

        private void OpenHedgeFund_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(HedgeFundTile, () =>
            {
                var window = new HedgeFundWindow();
                window.Show();
                return window;
            });
        }

        // Beispiel für neue Module: ein Button reicht, um das zentrale, globale Log-Fenster
        // zu öffnen. Es zeigt automatisch die Log-Einträge aller Module an, die über
        // UFOS.ai.Logging.AppLog protokollieren.
        private void OpenLogs_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(SystemLogsTile, () => BackendLogsWindow.Show(this));
        }

        // Transaction Log: costs of every AI request across all modules (shared ledger
        // %LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl, see Services/AiCostLedger.cs).
        private void OpenTransactionLog_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(TransactionLogTile, () =>
            {
                var window = new TransactionLogWindow();
                window.Show();
                return window;
            });
        }

        private void OpenGlobalMonitoring_Click(object sender, RoutedEventArgs e)
        {
            OpenModuleWindow(GlobalMonitoringTile, () =>
            {
                var window = new GlobalMonitoringWindow();
                window.Show();
                return window;
            });
        }

        // Sentiment Optimization and Trading Strategy Discovery don't have a project/
        // window yet (no .csproj / ProjectReference wired up). Their tiles are shown
        // for real (Idle status) so the intended module layout is visible, but the
        // click is illustrative-only until those modules actually exist.
        private void OpenSentimentOptimization_Click(object sender, RoutedEventArgs e)
        {
            ShowLaunchCaption("Sentiment Optimization", "not available yet (illustration only)");
        }

        private void OpenTradingStrategyDiscovery_Click(object sender, RoutedEventArgs e)
        {
            ShowLaunchCaption("Strategy Discovery", "not available yet (illustration only)");
        }

        private void AddModule_Click(object sender, RoutedEventArgs e)
        {
            ShowLaunchCaption("Add module", "not wired up yet (illustration only)");
        }

        private void ShowLaunchCaption(string moduleName, string suffix)
        {
            LaunchCaptionText.Inlines.Clear();
            LaunchCaptionText.Inlines.Add(new Run("→ "));
            LaunchCaptionText.Inlines.Add(new Run(moduleName) { FontWeight = FontWeights.Bold });
            LaunchCaptionText.Inlines.Add(new Run(" — " + suffix));

            LaunchCaption.BeginAnimation(UIElement.OpacityProperty, null);
            LaunchCaption.Opacity = 0;

            var fadeIn = new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(200));
            LaunchCaption.BeginAnimation(UIElement.OpacityProperty, fadeIn);

            _launchCaptionTimer?.Stop();
            _launchCaptionTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(2200) };
            _launchCaptionTimer.Tick += (_, __) =>
            {
                var fadeOut = new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(300));
                LaunchCaption.BeginAnimation(UIElement.OpacityProperty, fadeOut);
                _launchCaptionTimer?.Stop();
            };
            _launchCaptionTimer.Start();
        }
    }
}

using System;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Net.WebSockets;
using System.Net.Http;
using System.Text.Json;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows;
using Microsoft.Win32;
using System.Windows.Controls;
using System.Windows.Markup;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Controls.Primitives;
using System.Net.NetworkInformation;
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
        // state to control arc sweep direction
        private bool _isLongState = false;
        private bool _isShortState = false;
        private readonly System.Windows.Threading.DispatcherTimer _liveDisclaimerTimer = new();
        private int _liveDisclaimerDots = 0;
        private bool _liveDisclaimerHidden = false;
        private readonly DoubleAnimation _confidenceRingBrushAnimation = new()
        {
            From = -1.2,
            To = 1.2,
            Duration = TimeSpan.FromMilliseconds(1300),
            RepeatBehavior = RepeatBehavior.Forever,
            AutoReverse = false
        };
        private bool _confidenceRingAnimating = false;
        // view model for each ticker row shown in the UI
        private class TickerViewModel
        {
            public string Name { get; set; } = string.Empty;
            // numeric value / price
            public string Value { get; set; } = string.Empty;
            // change as percentage text
            public string Change { get; set; } = string.Empty;
            // original tendency string (optional)
            public string Tendency { get; set; } = string.Empty;
            // short tooltip keywords describing the ticker
            public string TooltipText { get; set; } = string.Empty;
            public object? TooltipControl { get; set; }
            // color indicator for trend rectangle
            public SolidColorBrush TrendColor { get; set; } = Brushes.Transparent;
            // rotation for triangle: 0 = up, 180 = down, 90 = right (sideways)
            public double TrendRotation { get; set; } = 90.0;
        }

        private class ExpandedMarketSectionViewModel
        {
            public string Header { get; set; } = string.Empty;
            public System.Collections.Generic.List<TickerViewModel> Tickers { get; set; } = new();
        }

        // Add a verdict to the in-memory history (newest first)
        public void AddVerdictToHistory(string verdict, string ticker, int confidence, string reason)
        {
            try
            {
                Dispatcher.Invoke(() =>
                {
                    var rec = new VerdictRecord
                    {
                        Timestamp = DateTime.Now,
                        Verdict = verdict ?? string.Empty,
                        Ticker = ticker ?? string.Empty,
                        Confidence = Math.Max(0, Math.Min(100, confidence)),
                        Reason = reason ?? string.Empty
                    };
                    _verdictHistory.Insert(0, rec);
                    // cap history to reasonable size
                    while (_verdictHistory.Count > 500) _verdictHistory.RemoveAt(_verdictHistory.Count - 1);
                });
            }
            catch { }
        }

        // Index into _verdictHistory of the verdict currently displayed in the main widget.
        // 0 = current/newest verdict, 1 = one older, etc. -1 = no verdict yet displayed via history navigation.
        private int _verdictHistoryIndex = 0;

        private void PrevVerdictButton_Click(object? sender, RoutedEventArgs e)
        {
            if (_verdictHistoryIndex > 0)
            {
                _verdictHistoryIndex--;
                DisplayVerdictAtHistoryIndex(_verdictHistoryIndex);
            }
        }

        private void NextVerdictButton_Click(object? sender, RoutedEventArgs e)
        {
            if (_verdictHistoryIndex < _verdictHistory.Count - 1)
            {
                _verdictHistoryIndex++;
                DisplayVerdictAtHistoryIndex(_verdictHistoryIndex);
            }
        }

        // Displays the verdict at the given index in _verdictHistory (0 = newest) in the main widget
        // and updates the enabled state of the navigation buttons accordingly.
        private void DisplayVerdictAtHistoryIndex(int index)
        {
            try
            {
                if (index < 0 || index >= _verdictHistory.Count) return;
                var rec = _verdictHistory[index];
                bool isCurrent = index == 0;
                RenderVerdict(rec.Verdict, rec.Ticker, rec.Confidence, rec.Reason, isCurrent ? (DateTime?)null : rec.Timestamp);
            }
            catch { }
            finally
            {
                UpdateVerdictNavButtons();
            }
        }

        private void UpdateVerdictNavButtons()
        {
            try
            {
                PrevVerdictButton.Visibility = _verdictHistoryIndex > 0 ? Visibility.Visible : Visibility.Hidden;
                NextVerdictButton.Visibility = _verdictHistoryIndex < _verdictHistory.Count - 1 ? Visibility.Visible : Visibility.Hidden;
            }
            catch { }
        }

        private void ShowLogs_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                // Redirect to the custom log popup builder so the UI chrome matches MainWindow
                OpenBackendLogButton_Click(sender, e);
            }
            catch (Exception ex)
            {
                try { (Application.Current as App)?.ShowUiException(new Exception("Fehler beim Öffnen der Logs: " + ex.Message)); } catch { }
            }
        }

        private readonly System.Collections.ObjectModel.ObservableCollection<TickerViewModel> _tickers = new();
        private readonly System.Collections.ObjectModel.ObservableCollection<ExpandedMarketSectionViewModel> _expandedGridSections = new();

        private static readonly System.Collections.Generic.List<(string Key, string[] Symbols)> SmallGridGroups = new()
        {
            ("treasury_yields_core", new[] { "BIL", "^TNX", "^TYX" }),
            ("liquidity_anchors", new[] { "UUP", "GLD" }),
            ("equity_indices", new[] { "QQQ", "IWM" }),
            ("banking_stress", new[] { "KRE" })
        };

        private static readonly System.Collections.Generic.List<(string Key, string[] Symbols)> ExpandedGridExtraGroups = new()
        {
            ("bond_proxies", new[] { "TLT", "AGG" }),
            ("reits", new[] { "VNQ" }),
            ("utilities", new[] { "XLU" }),
            ("homebuilders", new[] { "XHB", "ITB" }),
            ("tech_giants_extended", new[] { "MAGS" }),
            ("unprofitable_growth", new[] { "ARKK" }),
            ("financials_broad", new[] { "XLF" }),
            ("credit_risk", new[] { "HYG" })
        };

        private WebSocketService? _wsService;
        private CancellationTokenSource? _wsCts;
        private Process? _backendProcess;
        // flag to detect first real transcript input from backend
        private bool _transcriptReceived = false;
        private MarketUpdateMessage? _latestMarketUpdate;
        private readonly System.Windows.Threading.DispatcherTimer _marketDataPopupCloseTimer = new();
        private bool _isMarketDataPopupOpen = false;
        private readonly System.Text.StringBuilder _backendLogBuffer = new();
        private const int BackendLogBufferLimit = 1_048_576; // ~1 MB chars
        // verdict history (newest first)
        private readonly System.Collections.ObjectModel.ObservableCollection<VerdictRecord> _verdictHistory = new();

        private Button? ExpandMarketButtonElement => FindName("ExpandMarketButton") as Button;
        private Popup? MarketDataPopupElement => FindName("MarketDataPopup") as Popup;
        private Border? MarketDataPopupBorderElement => FindName("MarketDataPopupBorder") as Border;
        private Grid? ExpandedMarketGridElement => FindName("ExpandedMarketGrid") as Grid;

        private static string ToExpandedCategoryTitle(string key)
        {
            if (string.IsNullOrWhiteSpace(key))
            {
                return string.Empty;
            }

            var parts = key.Split('_', StringSplitOptions.RemoveEmptyEntries);
            for (var i = 0; i < parts.Length; i++)
            {
                var part = parts[i];
                parts[i] = part.Length == 0 ? part : char.ToUpperInvariant(part[0]) + part[1..];
            }

            return string.Join(" ", parts);
        }

        private static string FormatMarketPrice(double price)
        {
            return Math.Abs(price) < 0.0000001 ? "No Data Available" : $"{price:0.000}";
        }

        private static string FormatMarketChange(double changePercent)
        {
            return Math.Abs(changePercent) < 0.0000001 ? "No Data Available" : $"{changePercent:+0.000;-0.000;0.000}%";
        }

        private static string GetTickerTooltipText(string symbol)
        {
            return symbol.ToUpperInvariant() switch
            {
                "BIL" => "SPDR Bloomberg 1-3 Month T-Bill ETF · Short-term government bonds\nLow risk / cash proxy · Usually steady around Fed moves",
                "^TNX" => "CBOE 10-Year Treasury Yield Index · U.S. Treasury yields\nMacro risk indicator · Very sensitive to rate decisions",
                "^TYX" => "CBOE 30-Year Treasury Yield Index · U.S. Treasury yields\nLong-duration / rate sensitivity · Strongly impacted by Fed outlook",
                "UUP" => "Invesco DB US Dollar Index Bullish Fund · U.S. dollar\nHedging asset / low risk · Often reacts to rate differentials",
                "GLD" => "SPDR Gold Shares · Gold / commodities\nSafe haven / hedging asset · Often benefits from easing expectations",
                "QQQ" => "Invesco QQQ Trust · Nasdaq-100 / technology\nGrowth-oriented / high risk · Rate-sensitive and valuation-driven",
                "IWM" => "iShares Russell 2000 ETF · U.S. small caps\nCyclical / high risk · Often reacts to financing conditions",
                "KRE" => "SPDR S&P Regional Banking ETF · Regional banks / financials\nCyclical / risk-sensitive · Very exposed to rate policy shifts",
                "TLT" => "iShares 20+ Year Treasury Bond ETF · Long-dated U.S. Treasuries\nSafe haven / rate hedge · Usually rises when yields fall",
                "AGG" => "iShares Core U.S. Aggregate Bond ETF · U.S. bond market\nLow risk / diversification · Typically benefits from rate cuts",
                "VNQ" => "Vanguard Real Estate ETF · Real estate / REITs\nIncome-oriented / rate-sensitive · Often prefers lower rates",
                "XLU" => "Utilities Select Sector SPDR Fund · Utilities\nDefensive / low risk · Usually resilient in uncertain policy cycles",
                "XHB" => "SPDR S&P Homebuilders ETF · Homebuilding / housing\nCyclical / high risk · Sensitive to mortgage-rate expectations",
                "ITB" => "iShares U.S. Home Construction ETF · Homebuilding / housing\nCyclical / high risk · Sensitive to mortgage-rate expectations",
                "MAGS" => "Roundhill Magnificent Seven ETF · U.S. mega-cap tech\nMomentum / high risk · Usually reacts strongly to discount-rate moves",
                "ARKK" => "ARK Innovation ETF · Innovation / high growth\nHighly volatile / high risk · Often benefits from easier policy",
                "XLF" => "Financial Select Sector SPDR Fund · Financials\nCyclical / market-sensitive · Can react to yield-curve changes",
                "HYG" => "iShares iBoxx USD High Yield Corporate Bond ETF · High-yield bonds\nYield-oriented / high risk · Tends to improve with easier financial conditions",
                _ => "Unknown market proxy · Macro sensitivity\nBroad market observation · Reaction to Fed moves depends on asset type"
            };
        }

        private static ToolTip CreateTickerToolTip(string tooltipText)
        {
            return new ToolTip
            {
                Background = new SolidColorBrush(Color.FromRgb(16, 16, 16)),
                BorderBrush = new SolidColorBrush(Color.FromRgb(32, 32, 32)),
                BorderThickness = new Thickness(1),
                Padding = new Thickness(8, 6, 8, 6),
                Content = new TextBlock
                {
                    Text = tooltipText ?? string.Empty,
                    Foreground = Brushes.White,
                    FontSize = 11,
                    TextWrapping = TextWrapping.Wrap
                }
            };
        }

        private TickerViewModel CreateTickerViewModel(string symbol, Quote q)
        {
            var valueText = FormatMarketPrice(q.Price);
            var changeText = FormatMarketChange(q.ChangePercent);
            var tendency = Math.Abs(q.Price) < 0.0000001 && Math.Abs(q.ChangePercent) < 0.0000001
                ? "NO DATA"
                : q.ChangePercent > 0.0001 ? "UP" : (q.ChangePercent < -0.0001 ? "DOWN" : "SIDEWAYS");
            SolidColorBrush trendColor = Brushes.Gray;
            double trendRotation = 90.0;

            try
            {
                if (Math.Abs(q.Price) < 0.0000001 && Math.Abs(q.ChangePercent) < 0.0000001)
                {
                    trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FF9E9E9E"));
                    trendRotation = 90.0;
                }
                else if (q.ChangePercent > 0.0001) trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FF4CAF50"));
                else if (q.ChangePercent < -0.0001) trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FFF44336"));
                else trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FF9E9E9E"));

                if (q.ChangePercent > 0.0001) trendRotation = 0.0;
                else if (q.ChangePercent < -0.0001) trendRotation = 180.0;
                else trendRotation = 90.0;
            }
            catch { trendColor = Brushes.Gray; }

            var tooltipText = GetTickerTooltipText(symbol);
            return new TickerViewModel { Name = symbol, Value = valueText, Change = changeText, Tendency = tendency, TooltipText = tooltipText, TooltipControl = CreateTickerToolTip(tooltipText), TrendColor = trendColor, TrendRotation = trendRotation };
        }

        private System.Collections.Generic.List<ExpandedMarketSectionViewModel> BuildMarketSections(
            MarketUpdateMessage mu,
            System.Collections.Generic.IEnumerable<(string Key, string[] Symbols)> groups)
        {
            var sections = new System.Collections.Generic.List<ExpandedMarketSectionViewModel>();

            foreach (var group in groups)
            {
                var section = new ExpandedMarketSectionViewModel { Header = ToExpandedCategoryTitle(group.Key) };

                foreach (var symbol in group.Symbols)
                {
                    mu.Quotes.TryGetValue(symbol, out var q);
                    q ??= new Quote { Price = 0, ChangePercent = 0 };
                    section.Tickers.Add(CreateTickerViewModel(symbol, q));
                }

                sections.Add(section);
            }

            return sections;
        }
        public MainWindow()
        {
            InitializeComponent();
            // bind tickers collection to ItemsControl
            try { TickersItems.ItemsSource = _tickers; } catch { }
            try
            {
                _marketDataPopupCloseTimer.Interval = TimeSpan.FromSeconds(3);
                _marketDataPopupCloseTimer.Tick += (_, __) => CloseMarketDataPopup();
                if (MarketDataPopupElement != null)
                {
                    MarketDataPopupElement.Closed += (_, __) => OnMarketDataPopupClosed();
                }

                _liveDisclaimerTimer.Interval = TimeSpan.FromMilliseconds(450);
                _liveDisclaimerTimer.Tick += (_, __) => AnimateLiveDisclaimerDots();
                if (TopLiveMarketDisclaimer2 != null)
                {
                    TopLiveMarketDisclaimer2.Text = "Nothing to see here yet. Give us a moment.";
                }
                _liveDisclaimerTimer.Start();

            }
            catch { }
            // initialize default UI for verdict widget
            UpdateConfidence(0);
            UpdateVerdict("NONE", "");
            UpdateReason(string.Empty);
            // create a simple temp debug file marker so we can detect if UI code runs
            try { DebugLogger.Log("[UI] MainWindow ctor"); } catch { }
            // wire verdict navigation buttons if present
            try { PrevVerdictButton.Click += PrevVerdictButton_Click; } catch { }
            try { NextVerdictButton.Click += NextVerdictButton_Click; } catch { }
            try { UpdateVerdictNavButtons(); } catch { }
            // expose a simple public wrapper for showing logs and saving logs for global error dialog
            try { /* noop - methods exist below */ } catch { }
        }

        // Public wrapper so App can open the backend log window
        public void ShowBackendLogsWindow()
        {
            try { OpenBackendLogButton_Click(this, null); } catch { }
        }

        // Public wrapper to trigger save of current backend log buffer via SaveFileDialog
        public void TriggerSaveBackendLog()
        {
            try
            {
                var dlg = new Microsoft.Win32.SaveFileDialog()
                {
                    Title = "Save Backend Log",
                    Filter = "Log files (*.log)|*.log|Text files (*.txt)|*.txt|All files (*.*)|*.*",
                    FileName = $"backend-log-{DateTime.Now:yyyy-MM-dd_HHmmss}.log",
                    DefaultExt = ".log",
                    AddExtension = true
                };
                var res = dlg.ShowDialog(this);
                if (res == true)
                {
                    var text = string.Empty;
                    try { lock (_backendLogBuffer) { text = _backendLogBuffer.ToString(); } } catch { }
                    System.IO.File.WriteAllText(dlg.FileName, text, System.Text.Encoding.UTF8);
                }
            }
            catch { }
        }

        // Render a compact list of tickers from MarketUpdateMessage.
        public void RenderMarketUpdate(MarketUpdateMessage mu)
        {
            try
            {
                HideLiveDisclaimer();
                _latestMarketUpdate = mu;
                _expandedGridSections.Clear();
                foreach (var section in BuildMarketSections(mu, SmallGridGroups.Concat(ExpandedGridExtraGroups)))
                {
                    _expandedGridSections.Add(section);
                }

                // update observable collection for the compact 4x2 market grid
                _tickers.Clear();
                foreach (var group in SmallGridGroups)
                {
                    foreach (var symbol in group.Symbols)
                    {
                        mu.Quotes.TryGetValue(symbol, out var q);
                        q ??= new Quote { Price = 0, ChangePercent = 0 };
                        _tickers.Add(CreateTickerViewModel(symbol, q));
                    }
                }

                // update timestamp
                try { TimestampRun.Text = mu.Timestamp.ToString("yyyy-MM-dd HH:mm:ss"); } catch { }
                try { if (_isMarketDataPopupOpen) RefreshExpandedMarketPopup(); } catch { }
            }
            catch { }
        }

        private void AnimateLiveDisclaimerDots()
        {
            try
            {
                if (_liveDisclaimerHidden)
                {
                    _liveDisclaimerTimer.Stop();
                    return;
                }

                _liveDisclaimerDots = (_liveDisclaimerDots + 1) % 3;
                var dots = new string('.', _liveDisclaimerDots + 1);
                if (TopLiveMarketDisclaimer2 != null)
                {
                    TopLiveMarketDisclaimer2.Text = $"Nothing to see here yet. Give us a moment{dots}";
                }
            }
            catch { }
        }

        private void HideLiveDisclaimer()
        {
            try
            {
                _liveDisclaimerHidden = true;
                _liveDisclaimerTimer.Stop();
                if (TopLiveMarketDisclaimer2 != null)
                {
                    TopLiveMarketDisclaimer2.Visibility = Visibility.Hidden;
                }
            }
            catch { }
        }

        private void ExpandMarketButton_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                if (_isMarketDataPopupOpen)
                {
                    CloseMarketDataPopup();
                }
                else
                {
                    OpenMarketDataPopup();
                }
            }
            catch { }
        }

        private void CollapseMarketButton_Click(object sender, RoutedEventArgs e)
        {
            CloseMarketDataPopup();
        }

        private void MarketDataPopupRoot_MouseEnter(object sender, System.Windows.Input.MouseEventArgs e)
        {
            try { _marketDataPopupCloseTimer.Stop(); } catch { }
        }

        private void MarketDataPopupRoot_MouseLeave(object sender, System.Windows.Input.MouseEventArgs e)
        {
            try
            {
                if (_isMarketDataPopupOpen)
                {
                    _marketDataPopupCloseTimer.Stop();
                    _marketDataPopupCloseTimer.Start();
                }
            }
            catch { }
        }

        private void OpenMarketDataPopup()
        {
            if (_isMarketDataPopupOpen)
            {
                return;
            }

            RefreshExpandedMarketPopup();
            var anchor = GetMarketDataAnchorRect();
            _isMarketDataPopupOpen = true;
            try { if (TickersItems != null) TickersItems.Visibility = Visibility.Hidden; } catch { }
            try { if (ExpandMarketButtonElement != null) ExpandMarketButtonElement.Content = "Collapse"; } catch { }
            try
            {
                if (MarketDataPopupBorderElement != null)
                {
                    MarketDataPopupBorderElement.Width = anchor.Width;
                    MarketDataPopupBorderElement.Height = anchor.Height;
                }
            }
            catch { }
            try
            {
                if (MarketDataPopupElement != null)
                {
                    MarketDataPopupElement.PlacementTarget = this;
                    MarketDataPopupElement.Placement = System.Windows.Controls.Primitives.PlacementMode.Relative;
                    MarketDataPopupElement.HorizontalOffset = anchor.X;
                    MarketDataPopupElement.VerticalOffset = anchor.Y;
                    MarketDataPopupElement.IsOpen = true;
                }
            }
            catch { }
            try
            {
                _marketDataPopupCloseTimer.Stop();
                _marketDataPopupCloseTimer.Start();
            }
            catch { }

            try
            {
                Dispatcher.BeginInvoke(new Action(AnimateMarketDataPopupOpen), System.Windows.Threading.DispatcherPriority.Loaded);
            }
            catch { }
        }

        private void CloseMarketDataPopup()
        {
            try { _marketDataPopupCloseTimer.Stop(); } catch { }
            try { if (MarketDataPopupElement != null) MarketDataPopupElement.IsOpen = false; } catch { }
            OnMarketDataPopupClosed();
        }

        private void OnMarketDataPopupClosed()
        {
            _isMarketDataPopupOpen = false;
            try { if (TickersItems != null) TickersItems.Visibility = Visibility.Visible; } catch { }
            try { if (ExpandMarketButtonElement != null) ExpandMarketButtonElement.Content = "Expand"; } catch { }
            try { _marketDataPopupCloseTimer.Stop(); } catch { }
        }

        private void RefreshExpandedMarketPopup()
        {
            try
            {
                if (_latestMarketUpdate == null)
                {
                    if (ExpandedMarketGridElement != null)
                    {
                        ExpandedMarketGridElement.Children.Clear();
                        ExpandedMarketGridElement.RowDefinitions.Clear();
                    }
                    return;
                }

                BuildExpandedMarketGrid(_latestMarketUpdate);
            }
            catch { }
        }

        private System.Windows.Rect GetMarketDataAnchorRect()
        {
            try
            {
                if (TickersItems != null && IsLoaded)
                {
                    var anchor = TickersItems.TransformToAncestor(this).Transform(new Point(0, 0));
                    return new System.Windows.Rect(anchor.X, anchor.Y, Math.Max(1, TickersItems.ActualWidth), Math.Max(1, TickersItems.ActualHeight));
                }
            }
            catch { }

            return new System.Windows.Rect(25, 226, 450, 250);
        }

        private System.Windows.Size GetExpandedMarketTargetSize()
        {
            try
            {
                var grid = ExpandedMarketGridElement;
                if (grid == null)
                {
                    return new System.Windows.Size(640, 320);
                }

                grid.Measure(new System.Windows.Size(double.PositiveInfinity, double.PositiveInfinity));
                var desired = grid.DesiredSize;
                var borderPadding = 20.0;
                var borderThickness = 2.0;
                return new System.Windows.Size(Math.Max(640, desired.Width + borderPadding + borderThickness), Math.Max(260, desired.Height + borderPadding + borderThickness));
            }
            catch
            {
                return new System.Windows.Size(640, 320);
            }
        }

        private void AnimateMarketDataPopupOpen()
        {
            try
            {
                var popup = MarketDataPopupElement;
                var border = MarketDataPopupBorderElement;
                if (popup == null || border == null)
                {
                    return;
                }

                var targetSize = GetExpandedMarketTargetSize();

                var widthAnimation = new DoubleAnimation
                {
                    From = Math.Max(1, border.ActualWidth),
                    To = targetSize.Width,
                    Duration = TimeSpan.FromMilliseconds(220),
                    EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut }
                };

                var heightAnimation = new DoubleAnimation
                {
                    From = Math.Max(1, border.ActualHeight),
                    To = targetSize.Height,
                    Duration = TimeSpan.FromMilliseconds(220),
                    EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut }
                };

                border.BeginAnimation(FrameworkElement.WidthProperty, widthAnimation);
                border.BeginAnimation(FrameworkElement.HeightProperty, heightAnimation);
            }
            catch { }
        }

        private void BuildExpandedMarketGrid(MarketUpdateMessage mu)
        {
            var expandedGrid = ExpandedMarketGridElement;
            if (expandedGrid == null)
            {
                return;
            }

            expandedGrid.Children.Clear();
            expandedGrid.RowDefinitions.Clear();

            var sectionIndex = 0;
            var row = 0;
            while (sectionIndex < _expandedGridSections.Count)
            {
                expandedGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

                var rowGrid = new Grid
                {
                    Margin = new Thickness(0, row == 0 ? 0 : 8, 0, 0)
                };
                rowGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                rowGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

                for (var col = 0; col < 2 && sectionIndex < _expandedGridSections.Count; col++, sectionIndex++)
                {
                    var section = _expandedGridSections[sectionIndex];
                    var sectionPanel = CreateExpandedSectionPanel(section);
                    Grid.SetColumn(sectionPanel, col);
                    rowGrid.Children.Add(sectionPanel);
                }

                Grid.SetRow(rowGrid, row++);
                expandedGrid.Children.Add(rowGrid);
            }
        }

        private Border CreateExpandedSectionPanel(ExpandedMarketSectionViewModel section)
        {
            var sectionBorder = new Border
            {
                Background = Brushes.Transparent,
                Margin = new Thickness(0, 0, 12, 0),
                Padding = new Thickness(0)
            };

            var sectionStack = new StackPanel
            {
                Orientation = Orientation.Vertical
            };

            sectionStack.Children.Add(new TextBlock
            {
                Text = section.Header,
                Foreground = Brushes.White,
                FontSize = 12,
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 0, 0, 4)
            });

            var items = new UniformGrid
            {
                Columns = 1,
                HorizontalAlignment = HorizontalAlignment.Stretch
            };

            foreach (var ticker in section.Tickers)
            {
                items.Children.Add(CreateExpandedTickerCard(ticker));
            }

            sectionStack.Children.Add(items);
            sectionBorder.Child = sectionStack;
            return sectionBorder;
        }

        private Border CreateExpandedTickerCard(TickerViewModel ticker)
        {
            var card = new Border
            {
                Background = new SolidColorBrush(Color.FromRgb(16, 16, 16)),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(6),
                Margin = new Thickness(0, 0, 8, 0),
                BorderBrush = new SolidColorBrush(Color.FromRgb(32, 32, 32)),
                BorderThickness = new Thickness(1),
                MinHeight = 28,
                MinWidth = 0,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                ToolTip = CreateTickerToolTip(ticker.TooltipText)
            };

            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(48) });

            grid.Children.Add(new TextBlock
            {
                Text = ticker.Name,
                VerticalAlignment = VerticalAlignment.Center,
                Foreground = Brushes.White,
                FontSize = 12,
                FontWeight = FontWeights.SemiBold
            });

            var values = new Grid { HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Center };
            values.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(56) });
            values.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(48) });
            Grid.SetColumn(values, 1);
            values.Children.Add(new TextBlock
            {
                Text = ticker.Value,
                Foreground = Brushes.White,
                FontSize = 12,
                VerticalAlignment = VerticalAlignment.Center,
                TextAlignment = TextAlignment.Right,
                Margin = new Thickness(0, 0, 6, 0)
            });
            Grid.SetColumn(values.Children[0], 0);
            values.Children.Add(new TextBlock
            {
                Text = ticker.Change,
                Foreground = new SolidColorBrush(Color.FromRgb(191, 191, 191)),
                FontSize = 11,
                VerticalAlignment = VerticalAlignment.Center,
                TextAlignment = TextAlignment.Left
            });
            Grid.SetColumn(values.Children[1], 1);
            grid.Children.Add(values);

            var triangle = new Polygon
            {
                Points = new PointCollection(new[] { new Point(6, 0), new Point(12, 12), new Point(0, 12) }),
                Fill = ticker.TrendColor,
                Width = 12,
                Height = 12,
                HorizontalAlignment = HorizontalAlignment.Right,
                VerticalAlignment = VerticalAlignment.Center,
                RenderTransformOrigin = new Point(0.5, 0.5),
                RenderTransform = new RotateTransform(ticker.TrendRotation)
            };
            Grid.SetColumn(triangle, 2);
            grid.Children.Add(triangle);

            card.Child = grid;
            return card;
        }


        private async void Window_Loaded(object sender, RoutedEventArgs e)
        {
            // mark load start
            try { DebugLogger.Log("[UI] Window_Loaded start"); } catch { }
            // start websocket client
            // indicate connection attempt immediately
            try { UpdateConnectionStatus("Connecting"); } catch { }
            try
            {
                // attempt to start backend helper script (if present) so health endpoint becomes available
                try { DebugLogger.Log("[UI] Attempting to start backend helper..."); } catch { }
                try { TryStartBackendHelper(); } catch (Exception ex) { try { DebugLogger.Log("[UI] TryStartBackendHelper threw: " + ex.Message); } catch { } }
                // wait for backend health endpoint before attempting websocket connect
                var ok = await WaitForBackendHealthAsync(TimeSpan.FromSeconds(12)).ConfigureAwait(false);
                try { DebugLogger.Log($"[UI] Health check result: {ok}"); } catch { }

                _wsCts = new CancellationTokenSource();
                _wsService = new WebSocketService("ws://127.0.0.1:8765");
                _wsService.OnTicker += msg => Dispatcher.Invoke(() => UpdateTicker(msg.Index == 0 ? 1 : msg.Index, msg.Name, msg.Value, msg.Tendency));
                _wsService.OnMarketUpdate += mu => Dispatcher.Invoke(() => RenderMarketUpdate(mu));
                _wsService.OnConfidence += v => Dispatcher.Invoke(() => { AddConfidenceSample(v); UpdateConfidence(v); });
                _wsService.OnVerdict += vm => Dispatcher.Invoke(() => {
                    try { AddVerdictToHistory(vm.Verdict, vm.Ticker, vm.Confidence, vm.Reason); } catch { }
                    _verdictHistoryIndex = 0;
                    UpdateVerdict(vm.Verdict, vm.Ticker, vm.Confidence);
                    UpdateReason(vm.Reason);
                    if (vm.Confidence != 0) AddConfidenceSample(vm.Confidence);
                    UpdateVerdictNavButtons();
                });
                _wsService.OnTranscript += t => {
                    // log raw transcript arrival for debugging
                    try { DebugLogger.Log("[UI] OnTranscript raw: " + (t ?? string.Empty).Replace("\n", "\\n")); } catch { }
                    // append new transcript line at the bottom (newest last) on UI thread
                    try {
                        Dispatcher.Invoke(() => {
                            try {
                                if (!_transcriptReceived)
                                {
                                    TranscriptTextBox.Clear();
                                    _transcriptReceived = true;
                                    try { DebugLogger.Log("[UI] Cleared initial transcript placeholder"); } catch { }
                                }
                                if (!string.IsNullOrEmpty(TranscriptTextBox.Text)) TranscriptTextBox.AppendText("\n");
                                TranscriptTextBox.AppendText(t ?? string.Empty);
                                TranscriptTextBox.ScrollToEnd();
                            }
                            catch (Exception ex)
                            {
                                try { DebugLogger.Log("[UI] Error appending transcript: " + ex.Message); } catch { }
                            }
                        });
                    }
                    catch (Exception ex)
                    {
                        try { DebugLogger.Log("[UI] Dispatcher.Invoke failed for transcript: " + ex.Message); } catch { }
                    }
                };
                _wsService.OnConnectionStatus += s => Dispatcher.Invoke(() => UpdateConnectionStatus(s));
                // also log connection status changes to backend buffer for diagnostics
                _wsService.OnConnectionStatus += s => { try { lock(_backendLogBuffer){ _backendLogBuffer.AppendLine("[WS STATUS] " + s); if (_backendLogBuffer.Length > BackendLogBufferLimit) { var ov = _backendLogBuffer.Length - BackendLogBufferLimit; if (ov>0) _backendLogBuffer.Remove(0, ov); } } } catch { } };
                _wsService.OnRawMessage += raw => { if (!string.IsNullOrEmpty(raw)) { lock(_backendLogBuffer) { _backendLogBuffer.AppendLine("[WS RAW] " + raw); if (_backendLogBuffer.Length > BackendLogBufferLimit) { var ov = _backendLogBuffer.Length - BackendLogBufferLimit; if (ov>0) _backendLogBuffer.Remove(0, ov); } } } };
                // show immediate alert for connection/receive errors so user sees them
                _wsService.OnRawMessage += raw => {
                    try
                    {
                        if (string.IsNullOrEmpty(raw)) return;
                        if (raw.Contains("WS CONNECT ERROR") || raw.Contains("WS RECEIVE ERROR") || raw.Contains("CONNECT ERROR"))
                        {
                            try { Dispatcher.Invoke(() => (Application.Current as App)?.ShowUiException(new Exception("WebSocket error:\n" + raw))); } catch { }
                        }
                    }
                    catch { }
                };
                await _wsService.StartAsync(_wsCts.Token).ConfigureAwait(false);
                try { DebugLogger.Log("[UI] After StartAsync Completed"); } catch { }
            }
            catch (Exception ex)
            {
                try { DebugLogger.Log("[UI] Window_Loaded exception: " + ex.ToString()); } catch { }
                // show exception to user and record to backend log buffer for diagnostics
                try
                {
                    lock (_backendLogBuffer)
                    {
                        _backendLogBuffer.AppendLine("[UI] Window_Loaded exception: " + ex.ToString());
                        if (_backendLogBuffer.Length > BackendLogBufferLimit)
                        {
                            var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                            if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                        }
                    }
                }
                catch { }
                try { UpdateConnectionStatus("Error"); } catch { }
                try { (Application.Current as App)?.ShowUiException(new Exception("Fehler beim Starten der Verbindung:\n" + ex.ToString())); } catch { }
            }
        }

        private async void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
        {
            try
            {
                CloseMarketDataPopup();
                _wsCts?.Cancel();
                if (_wsService != null)
                {
                    await _wsService.StopAsync().ConfigureAwait(false);
                    _wsService.Dispose();
                    _wsService = null;
                }
                TryStopBackendHelper();
            }
            catch { }
        }

        private void TryStartBackendHelper()
        {
            try
            {
                // mark invocation for diagnostics
                try { lock (_backendLogBuffer) { _backendLogBuffer.AppendLine($"[BACKEND] TryStartBackendHelper invoked at {DateTime.Now:O}"); } } catch { }
                var script = System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..", "..", "backend", "run_backend.ps1");
                script = System.IO.Path.GetFullPath(script);
                if (!File.Exists(script))
                {
                    // backend script not present — append a diagnostic note and try to load latest backend logs if any
                    lock (_backendLogBuffer)
                    {
                        _backendLogBuffer.AppendLine($"[BACKEND] run_backend.ps1 not found at {script}");
                    }
                    try
                    {
                        var logsDir = System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..", "..", "backend", "data", "logs");
                        logsDir = System.IO.Path.GetFullPath(logsDir);
                        if (Directory.Exists(logsDir))
                        {
                            var files = Directory.GetFiles(logsDir, "*.jsonl");
                            if (files.Length > 0)
                            {
                                var latest = files.OrderBy(f => File.GetLastWriteTimeUtc(f)).Last();
                                var txt = File.ReadAllText(latest);
                                lock (_backendLogBuffer)
                                {
                                    // keep only last BackendLogBufferLimit chars
                                    if (txt.Length > BackendLogBufferLimit) txt = txt.Substring(txt.Length - BackendLogBufferLimit);
                                    _backendLogBuffer.AppendLine($"[BACKEND LOG FILE] {latest}");
                                    _backendLogBuffer.AppendLine(txt);
                                }
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        try { lock (_backendLogBuffer) { _backendLogBuffer.AppendLine("[BACKEND] failed to read logs: " + ex.Message); } } catch { }
                    }
                    return;
                }

                var psi = new ProcessStartInfo
                {
                    FileName = "powershell.exe",
                    Arguments = $"-ExecutionPolicy Bypass -File \"{script}\"",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                };

                _backendProcess = new Process { StartInfo = psi, EnableRaisingEvents = true };
                _backendProcess.OutputDataReceived += (s, e) => {
                    if (string.IsNullOrEmpty(e.Data)) return;
                    lock (_backendLogBuffer)
                    {
                        _backendLogBuffer.AppendLine(e.Data);
                        if (_backendLogBuffer.Length > BackendLogBufferLimit)
                        {
                            // remove oldest chars to keep buffer within limit.
                            var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                            if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                        }
                    }
                };
                _backendProcess.ErrorDataReceived += (s, e) => {
                    if (string.IsNullOrEmpty(e.Data)) return;
                    lock (_backendLogBuffer)
                    {
                        _backendLogBuffer.AppendLine(e.Data);
                        if (_backendLogBuffer.Length > BackendLogBufferLimit)
                        {
                            var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                            if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                        }
                    }
                };
                _backendProcess.Exited += (s, e) => {
                    try
                    {
                        var p = s as Process;
                        var exit = "?";
                        try { if (p != null) exit = p.ExitCode.ToString(); } catch { }
                        lock (_backendLogBuffer)
                        {
                            _backendLogBuffer.AppendLine($"[BACKEND] helper exited (code={exit})");
                            if (_backendLogBuffer.Length > BackendLogBufferLimit)
                            {
                                var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                                if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                            }
                        }
                    }
                    catch { }
                };
                lock (_backendLogBuffer)
                {
                    _backendLogBuffer.AppendLine("[BACKEND] starting helper script...");
                    if (_backendLogBuffer.Length > BackendLogBufferLimit)
                    {
                        var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                        if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                    }
                }
                try
                {
                    var started = _backendProcess.Start();
                    _backendProcess.BeginOutputReadLine();
                    _backendProcess.BeginErrorReadLine();
                    lock (_backendLogBuffer)
                    {
                        _backendLogBuffer.AppendLine($"[BACKEND] helper process started: Success={started} PID={_backendProcess.Id}");
                        if (_backendLogBuffer.Length > BackendLogBufferLimit)
                        {
                            var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                            if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                        }
                    }
                }
                catch (Exception ex)
                {
                    try { lock (_backendLogBuffer) { _backendLogBuffer.AppendLine("[BACKEND] failed to start helper: " + ex.Message); } } catch { }
                }

                // fire-and-check: short background probe whether process exited quickly or listener appeared
                Task.Run(async () =>
                {
                    try
                    {
                        await Task.Delay(1500);
                        try
                        {
                            if (_backendProcess == null)
                            {
                                lock (_backendLogBuffer) { _backendLogBuffer.AppendLine("[BACKEND] process is null after start check"); }
                                return;
                            }
                            if (_backendProcess.HasExited)
                            {
                                lock (_backendLogBuffer)
                                {
                                    _backendLogBuffer.AppendLine($"[BACKEND] helper exited early (code={_backendProcess.ExitCode})");
                                    if (_backendLogBuffer.Length > BackendLogBufferLimit)
                                    {
                                        var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                                        if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                                    }
                                }
                            }
                            else
                            {
                                // check active TCP listeners for port 8765
                                try
                                {
                                    var props = IPGlobalProperties.GetIPGlobalProperties();
                                    var listeners = props.GetActiveTcpListeners();
                                    var found = listeners.Any(l => l.Port == 8765);
                                    lock (_backendLogBuffer)
                                    {
                                        _backendLogBuffer.AppendLine($"[BACKEND] port 8765 listening: {found}");
                                        if (_backendLogBuffer.Length > BackendLogBufferLimit)
                                        {
                                            var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                                            if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                                        }
                                    }
                                }
                                catch { }
                            }
                        }
                        catch (Exception ex2) { try { lock (_backendLogBuffer) { _backendLogBuffer.AppendLine("[BACKEND] post-start check failed: " + ex2.Message); } } catch { } }
                    }
                    catch { }
                });
            }
            catch { }
        }

        private void TryStopBackendHelper()
        {
            try
            {
                if (_backendProcess == null) return;
                if (!_backendProcess.HasExited)
                {
                    try { lock (_backendLogBuffer) { _backendLogBuffer.AppendLine($"[BACKEND] Attempting to kill backend process PID {_backendProcess.Id}"); } } catch { }
                    try { _backendProcess.Kill(true); } catch { }
                }
                _backendProcess.Dispose();
                _backendProcess = null;
            }
            catch { }
        }

        private void RestartButton_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                TryStartBackendHelper();
            }
            catch (Exception ex)
            {
                try { (Application.Current as App)?.ShowUiException(new Exception("Failed to start backend: " + ex.Message)); } catch { }
            }
        }

        private async Task<bool> WaitForBackendHealthAsync(TimeSpan timeout)
        {
            try
            {
                var client = new HttpClient();
                var sw = System.Diagnostics.Stopwatch.StartNew();
                while (sw.Elapsed < timeout)
                {
                    try
                    {
                        var resp = await client.GetAsync("http://127.0.0.1:8766/health");
                        if (resp.IsSuccessStatusCode) return true;
                    }
                    catch { }
                    await Task.Delay(500);
                }
            }
            catch { }
            return false;
        }

        private void OpenBackendLogButton_Click(object sender, RoutedEventArgs e)
        {
            Window? win = null;
            try
            {
                // Create a non-standard window that matches the MainWindow look: dark border and custom title bar
                win = new Window
                {
                    Title = "Backend Log",
                    Width = 700,
                    Height = 420,
                    Owner = this,
                    WindowStartupLocation = WindowStartupLocation.CenterOwner,
                    WindowStyle = WindowStyle.None,
                    AllowsTransparency = true,
                    Background = Brushes.Transparent
                };

                // Outer border to emulate MainWindow chrome
                var outer = new Border
                {
                    Background = new SolidColorBrush(Color.FromRgb(17, 17, 17)),
                    CornerRadius = new CornerRadius(8),
                    Padding = new Thickness(0),
                    SnapsToDevicePixels = true
                };

                var root = new Grid { Margin = new Thickness(0) };
                root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(18) }); // title bar
                root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

                // Title bar (black) with round buttons on the right
                var titleBar = new Border { Background = Brushes.Black, CornerRadius = new CornerRadius(8,8,0,0), Height = 18 };
                titleBar.MouseLeftButtonDown += (s, ev) => { try { if (ev.ButtonState == MouseButtonState.Pressed) win.DragMove(); } catch { } };

                var tbGrid = new Grid();
                tbGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                tbGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

                var titleText = new TextBlock { Text = "Backend Log", Foreground = Brushes.White, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8,0,0,0), FontSize = 12, FontWeight = FontWeights.SemiBold };
                Grid.SetColumn(titleText, 0);
                tbGrid.Children.Add(titleText);

                var btnPanel = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0,0,6,0) };

                var roundStyleObj = TryFindResource("RoundButtonStyle");
                Style roundStyle = roundStyleObj as Style;

                // If the RoundButtonStyle isn't available (resource lookup failed), create a fallback style from XAML
                if (roundStyle == null)
                {
                    // Fallback: construct equivalent Style in code to avoid parsing XAML at runtime
                    var template = new ControlTemplate(typeof(Button));
                    var borderFactory = new FrameworkElementFactory(typeof(Border));
                    borderFactory.SetValue(Border.CornerRadiusProperty, new CornerRadius(999));
                    borderFactory.SetBinding(Border.BackgroundProperty, new System.Windows.Data.Binding("Background") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
                    borderFactory.SetBinding(Border.WidthProperty, new System.Windows.Data.Binding("Width") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
                    borderFactory.SetBinding(Border.HeightProperty, new System.Windows.Data.Binding("Height") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
                    var contentPresenterFactory = new FrameworkElementFactory(typeof(ContentPresenter));
                    contentPresenterFactory.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
                    contentPresenterFactory.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
                    borderFactory.AppendChild(contentPresenterFactory);
                    template.VisualTree = borderFactory;

                    var style = new Style(typeof(Button));
                    style.Setters.Add(new Setter(Button.WidthProperty, 14.0));
                    style.Setters.Add(new Setter(Button.HeightProperty, 14.0));
                    style.Setters.Add(new Setter(Button.PaddingProperty, new Thickness(0)));
                    style.Setters.Add(new Setter(Button.BorderThicknessProperty, new Thickness(0)));
                    style.Setters.Add(new Setter(Button.BackgroundProperty, Brushes.Transparent));
                    style.Setters.Add(new Setter(Button.TemplateProperty, template));

                    roundStyle = style;
                }

                // save button (small blue) placed before minimize/close
                var saveBtn = new Button { Width = 14, Height = 14, Margin = new Thickness(6,-2,0,0), VerticalAlignment = VerticalAlignment.Center };
                if (roundStyle != null) saveBtn.Style = roundStyle;
                try { saveBtn.Background = new SolidColorBrush(Color.FromRgb(0x00, 0x7A, 0xFF)); } catch { saveBtn.Background = Brushes.DodgerBlue; }
                saveBtn.ToolTip = "Save log";
                saveBtn.Click += (s, ev) => {
                    try
                    {
                        var dlg = new SaveFileDialog()
                        {
                            Title = "Save Backend Log",
                            Filter = "Log files (*.log)|*.log|Text files (*.txt)|*.txt|All files (*.*)|*.*",
                            FileName = $"backend-log-{DateTime.Now:yyyy-MM-dd_HHmmss}.log",
                            DefaultExt = ".log",
                            AddExtension = true
                        };
                        var res = dlg.ShowDialog(this);
                        if (res == true)
                        {
                            var text = string.Empty;
                            try { lock (_backendLogBuffer) { text = _backendLogBuffer.ToString(); } } catch { }
                            System.IO.File.WriteAllText(dlg.FileName, text, System.Text.Encoding.UTF8);
                        }
                    }
                    catch { }
                };
                saveBtn.Content = new TextBlock { Text = "💾", Foreground = Brushes.White, FontSize = 8.1, FontWeight = FontWeights.Bold, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };

                var minimizeBtn = new Button { Width = 14, Height = 14, Margin = new Thickness(6,0,0,0) };
                if (roundStyle != null) minimizeBtn.Style = roundStyle;
                // match MainWindow exact amber background color
                try { minimizeBtn.Background = new SolidColorBrush(Color.FromRgb(0xED, 0xB4, 0x00)); } catch { minimizeBtn.Background = Brushes.Gold; }
                minimizeBtn.Click += (s, ev) => { try { win.WindowState = WindowState.Minimized; } catch { } };
                var minTxt = new TextBlock { Text = "—", Foreground = Brushes.White, FontSize = 9, FontWeight = FontWeights.Bold, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
                minimizeBtn.Content = minTxt;

                var closeBtn = new Button { Width = 14, Height = 14, Margin = new Thickness(6,0,0,0) };
                if (roundStyle != null) closeBtn.Style = roundStyle;
                // match MainWindow exact red background color
                try { closeBtn.Background = new SolidColorBrush(Color.FromRgb(0xED, 0x6A, 0x5A)); } catch { closeBtn.Background = Brushes.IndianRed; }
                closeBtn.Click += (s, ev) => { try { win.Close(); } catch { } };
                var closeTxt = new TextBlock { Text = "✕", Foreground = Brushes.White, FontSize = 9, FontWeight = FontWeights.Bold, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
                closeBtn.Content = closeTxt;

                btnPanel.Children.Add(saveBtn);
                btnPanel.Children.Add(minimizeBtn);
                btnPanel.Children.Add(closeBtn);
                Grid.SetColumn(btnPanel, 1);
                tbGrid.Children.Add(btnPanel);

                titleBar.Child = tbGrid;
                Grid.SetRow(titleBar, 0);
                root.Children.Add(titleBar);

                // Content area with padding
                var contentGrid = new Grid { Margin = new Thickness(8) };
                Grid.SetRow(contentGrid, 1);

                var tb = new TextBox
                {
                    Text = string.Empty,
                    IsReadOnly = true,
                    TextWrapping = TextWrapping.Wrap,
                    VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                    HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                    Background = new SolidColorBrush(Color.FromRgb(17, 17, 17)),
                    Foreground = new SolidColorBrush(Color.FromRgb(240, 240, 240)),
                    FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                    Margin = new Thickness(0)
                };

                // Apply the same compact scrollbar style used by the transcript textbox
                try
                {
                    var compact = TryFindResource("CompactScrollBarStyle") as Style;
                    if (compact != null) tb.Resources.Add(typeof(ScrollBar), compact);
                    var thumbStyle = TryFindResource("CompactScrollThumbStyle") as Style;
                    if (thumbStyle != null) tb.Resources.Add(typeof(Thumb), thumbStyle);
                    // Ensure internal scrollbars get the template after the textbox is loaded
                    tb.Loaded += (s, ev) => ApplyScrollStylesToVisualTree(tb);
                }
                catch { }

                // helper to apply styles to internal ScrollBar/Thumb elements
                void ApplyScrollStylesToVisualTree(DependencyObject root)
                {
                    try
                    {
                        var sbStyle = TryFindResource("CompactScrollBarStyle") as Style ?? Application.Current?.FindResource("CompactScrollBarStyle") as Style;
                        var thStyle = TryFindResource("CompactScrollThumbStyle") as Style ?? Application.Current?.FindResource("CompactScrollThumbStyle") as Style;
                        if (sbStyle == null && thStyle == null) return;
                        // traverse visual tree and set styles
                        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++)
                        {
                            var child = VisualTreeHelper.GetChild(root, i);
                            if (child is ScrollBar sb)
                            {
                                if (sbStyle != null) sb.Style = sbStyle;
                            }
                            if (child is Thumb th)
                            {
                                if (thStyle != null) th.Style = thStyle;
                            }
                            ApplyScrollStylesToVisualTree(child);
                        }
                    }
                    catch { }
                }

                contentGrid.Children.Add(tb);
                root.Children.Add(contentGrid);

                outer.Child = root;
                win.Content = outer;

                // populate current buffer
                lock (_backendLogBuffer)
                {
                    tb.Text = _backendLogBuffer.ToString();
                }

                // update periodically while open (refresh view)
                var timer = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
                timer.Tick += (s, ev) => { lock (_backendLogBuffer) { tb.Text = _backendLogBuffer.ToString(); tb.CaretIndex = tb.Text.Length; tb.ScrollToEnd(); } };
                win.Closed += (s, ev) => timer.Stop();
                // Close on Escape key when this dynamic window is focused
                win.PreviewKeyDown += (s, e) => { try { if (e.Key == System.Windows.Input.Key.Escape) win.Close(); } catch { } };
                timer.Start();

                //Block Ownership

                win.Owner = null;

                // Instead of secondaryWindow.ShowDialog(); (which blocks the owner/main window)
                win.Show();
            }
            catch (Exception ex)
            {
                lock (_backendLogBuffer)
                {
                    _backendLogBuffer.AppendLine($"[UI ERR] OpenBackendLogButton_Click failed: {ex}");
                    if (_backendLogBuffer.Length > BackendLogBufferLimit)
                    {
                        var overflow = _backendLogBuffer.Length - BackendLogBufferLimit;
                        if (overflow > 0) _backendLogBuffer.Remove(0, overflow);
                    }
                }
                try { (Application.Current as App)?.ShowUiException(new Exception($"Failed to open backend log window.\n\n{ex.Message}")); } catch { }
                try { win?.Close(); } catch { }
            }
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
                try
                {
                    // Show Restart button when status indicates error
                    if (s.Contains("ERROR"))
                    {
                        try { RestartButton.Visibility = Visibility.Visible; } catch { }
                    }
                    else
                    {
                        try { RestartButton.Visibility = Visibility.Collapsed; } catch { }
                    }
                }
                catch { }
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
        public void UpdateVerdict(string verdict, string ticker, int confidence = 0)
        {
            RenderVerdict(verdict, ticker, confidence, null, null);
        }

        // Renders a verdict into the widget. If historyTimestamp is set, the header uses the
        // "Older Verdict from [DATETIME]: [VERDICT]" format instead of the current-verdict format.
        private void RenderVerdict(string verdict, string ticker, int confidence, string? reason, DateTime? historyTimestamp)
        {
            var text = string.IsNullOrWhiteSpace(ticker) ? verdict : $"{verdict} {ticker}";
            // Do not include percentage in the verdict text; show confidence as ring fill and centered number.
            // Keep the heading format/position constant regardless of current vs. older verdict; the
            // timestamp for older verdicts is shown separately in VerdictDateTextBlock.
            VerdictTextBlock.Text = historyTimestamp.HasValue
                ? $"Older Verdict: {text}"
                : $"Current Verdict: {text}";

            try
            {
                if (historyTimestamp.HasValue)
                {
                    VerdictDateTextBlock.Text = $"from {historyTimestamp.Value:yyyy-MM-dd HH:mm:ss}";
                    VerdictDateTextBlock.Visibility = Visibility.Visible;
                }
                else
                {
                    VerdictDateTextBlock.Text = string.Empty;
                    VerdictDateTextBlock.Visibility = Visibility.Collapsed;
                }
            }
            catch { }

            if (reason != null)
            {
                try { UpdateReason(reason); } catch { }
            }

            // determine color: green for long/buy, red for short/sell, gray otherwise
            var v = verdict?.ToUpperInvariant() ?? string.Empty;
            Brush color = Brushes.Gray;
            bool isLong = false;
            bool isShort = false;
            if (v.Contains("LONG") || v.Contains("BUY") || v.Contains("BULL")) { color = Brushes.Green; isLong = true; }
            else if (v.Contains("SHORT") || v.Contains("SELL") || v.Contains("BEAR")) { color = Brushes.Red; isShort = true; }

            // set stroke color on the ring arc path
            try { RingArc.Stroke = color; } catch { }

            // remember state for UpdateConfidence sweep direction
            _isLongState = isLong;
            _isShortState = isShort;

            try
            {
                // Ensure no additional rotation is applied here; UpdateConfidence draws arc starting at 3 o'clock.
                RingArc.RenderTransform = Transform.Identity;
            }
            catch { }

            // update the stroke dash to represent confidence (0..100)
            try {
                UpdateConfidence(confidence);
            } catch { }
        }

        public void UpdateReason(string reason)
        {
            ReasonTextBlock.Text = reason ?? string.Empty;
        }

        public void UpdateConfidence(int value)
        {
            var clamped = Math.Max(0, Math.Min(100, value));
            // show centered big bold number with percent sign (no-op)
            try { ConfidenceCenterText.Text = clamped.ToString() + "%"; } catch { }

            try
            {
                if (RingArc?.Stroke is LinearGradientBrush brush && brush.RelativeTransform is TranslateTransform tt)
                {
                    if (clamped > 0 && !_confidenceRingAnimating)
                    {
                        tt.BeginAnimation(TranslateTransform.XProperty, _confidenceRingBrushAnimation);
                        _confidenceRingAnimating = true;
                    }
                    else if (clamped <= 0 && _confidenceRingAnimating)
                    {
                        tt.BeginAnimation(TranslateTransform.XProperty, null);
                        tt.X = -1.2;
                        _confidenceRingAnimating = false;
                    }
                }
            }
            catch { }

            try
            {
                // Draw an explicit ArcSegment on RingArc so we map percent -> sweep precisely.
                double w = RingArc.Width;
                double h = RingArc.Height;
                double stroke = RingArc.StrokeThickness;
                double cx = w / 2.0;
                double cy = h / 2.0;
                double radius = Math.Max(0.0, Math.Min(w, h) / 2.0 - stroke / 2.0);

                if (clamped <= 0)
                {
                    RingArc.Data = null;
                }
                else if (clamped >= 100)
                {
                    // full circle: use EllipseGeometry to draw complete ring
                    RingArc.Data = new EllipseGeometry(new Point(cx, cy), radius, radius);
                }
                else
                {
                    double percent = clamped / 100.0;
                    double sweepDeg = 360.0 * percent;

                    // Start at 12 o'clock and sweep by verdict direction.
                    // LONG: clockwise, SHORT: counterclockwise.
                    double startDeg = -90.0;
                    double endDeg = _isShortState ? startDeg - sweepDeg : startDeg + sweepDeg;

                    double startRad = startDeg * Math.PI / 180.0;
                    double endRad = endDeg * Math.PI / 180.0;

                    var startPoint = new Point(cx + radius * Math.Cos(startRad), cy + radius * Math.Sin(startRad));
                    var endPoint = new Point(cx + radius * Math.Cos(endRad), cy + radius * Math.Sin(endRad));

                    bool isLargeArc = Math.Abs(sweepDeg) > 180.0;
                    var pf = new PathFigure { StartPoint = startPoint, IsClosed = false, IsFilled = false };
                    var seg = new ArcSegment(endPoint, new Size(radius, radius), 0.0, isLargeArc, _isShortState ? SweepDirection.Counterclockwise : SweepDirection.Clockwise, true);
                    pf.Segments.Clear();
                    pf.Segments.Add(seg);
                    var pg = new PathGeometry();
                    pg.Figures.Add(pf);
                    RingArc.Data = pg;
                }
            }
            catch { }
        }

        // Update ticker row (1..4) with name, value and tendency (e.g. "UP", "DOWN", "SIDEWAYS")
        public void UpdateTicker(int index, string name, string value, string tendency)
        {
            // normalize tendency
            var t = (tendency ?? string.Empty).ToUpperInvariant();

            try
            {
                // find existing ticker item in the observable collection
                var existing = System.Linq.Enumerable.FirstOrDefault(_tickers, t => string.Equals(t.Name, name, StringComparison.OrdinalIgnoreCase));
                if (existing != null)
                {
                    existing.Value = value ?? string.Empty;
                    existing.Tendency = t;
                    // try to parse numeric change from value when possible (fallback to empty)
                    try { existing.Change = existing.Change ?? string.Empty; } catch { }
                    // no reliable change provided here; keep existing.Change
                    // refresh the collection item by replacing it (simple approach)
                    var idx = _tickers.IndexOf(existing);
                    if (idx >= 0)
                    {
                        _tickers[idx] = existing;
                    }
                }
                else
                {
                    // append new ticker; infer change and trend color if possible
                    string changeText = string.Empty;
                    SolidColorBrush trendColor = Brushes.Gray;
                    try
                    {
                        // attempt to parse change if value contains % or a sign
                        if (!string.IsNullOrEmpty(value) && value.Contains("%"))
                        {
                            changeText = value;
                        }
                        else
                        {
                            changeText = string.Empty;
                        }
                        if (t.Contains("UP")) trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FF4CAF50"));
                        else if (t.Contains("DOWN")) trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FFF44336"));
                        else trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FF9E9E9E"));
                    }
                    catch { trendColor = Brushes.Gray; }

                    var tooltipText = GetTickerTooltipText(name ?? string.Empty);
                    _tickers.Add(new TickerViewModel { Name = name ?? string.Empty, Value = value ?? string.Empty, Change = changeText, Tendency = t, TooltipText = tooltipText, TooltipControl = CreateTickerToolTip(tooltipText), TrendColor = trendColor });
                }

                // update timestamp whenever tickers are updated
                try { TimestampRun.Text = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"); } catch { }
            }
            catch { }
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
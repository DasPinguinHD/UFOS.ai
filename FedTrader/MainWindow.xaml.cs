using System;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Net.WebSockets;
using System.Net.Http;
using System.Text.Json;
using System.Diagnostics;
using System.IO;
using System.Windows;
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
            // color indicator for trend rectangle
            public SolidColorBrush TrendColor { get; set; } = Brushes.Transparent;
            // rotation for triangle: 0 = up, 180 = down, 90 = right (sideways)
            public double TrendRotation { get; set; } = 90.0;
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
                try { MessageBox.Show(this, "Fehler beim Öffnen der Logs: " + ex.Message, "Fehler", MessageBoxButton.OK, MessageBoxImage.Error); } catch { }
            }
        }

        private readonly System.Collections.ObjectModel.ObservableCollection<TickerViewModel> _tickers = new();
        // ordered groups as in screenshot (preserve grouping)
        private static readonly System.Collections.Generic.List<(string Key, string[] Symbols)> TickerGroups = new()
        {
            ("treasury_yields", new[] { "^TNX", "^TYX" }),
            ("bond_proxies", new[] { "TLT", "AGG" }),
            ("reits", new[] { "VNQ" }),
            ("utilities", new[] { "XLU" }),
            ("homebuilders", new[] { "XHB", "ITB" }),
            ("tech_giants", new[] { "QQQ", "MAGS" }),
            ("unprofitable_growth", new[] { "ARKK" }),
            ("financials", new[] { "XLF" })
        };

        private WebSocketService? _wsService;
        private CancellationTokenSource? _wsCts;
        private Process? _backendProcess;
        // flag to detect first real transcript input from backend
        private bool _transcriptReceived = false;
        private readonly System.Text.StringBuilder _backendLogBuffer = new();
        private const int BackendLogBufferLimit = 1_048_576; // ~1 MB chars
        public MainWindow()
        {
            InitializeComponent();
            // bind tickers collection to ItemsControl
            try { TickersItems.ItemsSource = _tickers; } catch { }
            // initialize default UI for verdict widget
            UpdateConfidence(0);
            UpdateVerdict("NONE", "");
            UpdateReason("[REASON]");
            // create a simple temp debug file marker so we can detect if UI code runs
            try { DebugLogger.Log("[UI] MainWindow ctor"); } catch { }
        }

        // Render a compact list of tickers from MarketUpdateMessage.
        public void RenderMarketUpdate(MarketUpdateMessage mu)
        {
            try
            {
                // update observable collection for ItemsControl
                _tickers.Clear();
                foreach (var group in TickerGroups)
                {
                    // add only actual ticker symbols (no category headers)
                    foreach (var symbol in group.Symbols)
                    {
                        mu.Quotes.TryGetValue(symbol, out var q);
                        q ??= new Quote { Price = 0, ChangePercent = 0 };
                        // show prices with three decimals to capture precision for treasury yields
                        var valueText = q.Price != 0 ? $"{q.Price:0.000}" : string.Empty;
                        var changeText = $"{q.ChangePercent:+0.000;-0.000;0.000}%";
                        var tendency = q.ChangePercent > 0.0001 ? "UP" : (q.ChangePercent < -0.0001 ? "DOWN" : "SIDEWAYS");
                        SolidColorBrush trendColor = Brushes.Gray;
                        double trendRotation = 90.0; // default sideways
                        try {
                            if (q.ChangePercent > 0.0001) trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FF4CAF50"));
                            else if (q.ChangePercent < -0.0001) trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FFF44336"));
                            else trendColor = (SolidColorBrush)(new BrushConverter().ConvertFromString("#FF9E9E9E"));
                            if (q.ChangePercent > 0.0001) trendRotation = 0.0;
                            else if (q.ChangePercent < -0.0001) trendRotation = 180.0;
                            else trendRotation = 90.0;
                        } catch { trendColor = Brushes.Gray; }

                        _tickers.Add(new TickerViewModel { Name = symbol, Value = valueText, Change = changeText, Tendency = tendency, TrendColor = trendColor, TrendRotation = trendRotation });
                    }
                }

                // update timestamp
                try { TimestampRun.Text = mu.Timestamp.ToString("yyyy-MM-dd HH:mm:ss"); } catch { }
            }
            catch { }
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
                _wsService.OnVerdict += vm => Dispatcher.Invoke(() => { UpdateVerdict(vm.Verdict, vm.Ticker, vm.Confidence); UpdateReason(vm.Reason); if (vm.Confidence != 0) AddConfidenceSample(vm.Confidence); });
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
                            try { Dispatcher.Invoke(() => MessageBox.Show(this, "WebSocket error:\n" + raw, "WebSocket Error", MessageBoxButton.OK, MessageBoxImage.Error)); } catch { }
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
                try { MessageBox.Show(this, "Fehler beim Starten der Verbindung:\n" + ex.ToString(), "Fehler", MessageBoxButton.OK, MessageBoxImage.Error); } catch { }
            }
        }

        private async void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
        {
            try
            {
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
                    try { _backendProcess.Kill(true); } catch { }
                }
                _backendProcess.Dispose();
                _backendProcess = null;
            }
            catch { }
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

                // update periodically while open
                var timer = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
                timer.Tick += (s, ev) => { lock (_backendLogBuffer) { tb.Text = _backendLogBuffer.ToString(); tb.CaretIndex = tb.Text.Length; tb.ScrollToEnd(); } };
                win.Closed += (s, ev) => timer.Stop();
                timer.Start();

                win.ShowDialog();
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
                try { MessageBox.Show(this, $"Failed to open backend log window.\n\n{ex.Message}", "Error", MessageBoxButton.OK, MessageBoxImage.Error); } catch { }
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
            var text = string.IsNullOrWhiteSpace(ticker) ? verdict : $"{verdict} {ticker}";
            // Do not include percentage in the verdict text; show confidence as ring fill and centered number
            VerdictTextBlock.Text = $"Current Verdict: {text}";

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

                    _tickers.Add(new TickerViewModel { Name = name ?? string.Empty, Value = value ?? string.Empty, Change = changeText, Tendency = t, TrendColor = trendColor });
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
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
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
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
        // view model for each ticker row shown in the UI
        private class TickerViewModel
        {
            public string Name { get; set; } = string.Empty;
            public string Value { get; set; } = string.Empty;
            public string Tendency { get; set; } = string.Empty;
        }

        private void ShowLogs_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                var dlg = new BackendLogsWindow(() => {
                    lock (_backendLogBuffer)
                    {
                        return _backendLogBuffer.ToString();
                    }
                });
                dlg.Owner = this;
                dlg.ShowDialog();
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

        // Render a compact list of tickers from MarketUpdateMessage
        public void RenderMarketUpdate(MarketUpdateMessage mu)
        {
            try
            {
                // update observable collection for ItemsControl
                _tickers.Clear();
                foreach (var group in TickerGroups)
                {
                    // group header
                    _tickers.Add(new TickerViewModel { Name = group.Key.Replace('_', ' ').ToUpperInvariant(), Value = string.Empty, Tendency = string.Empty });
                    foreach (var symbol in group.Symbols)
                    {
                        mu.Quotes.TryGetValue(symbol, out var q);
                        q ??= new Quote { Price = 0, ChangePercent = 0 };
                        var valueText = q.Price != 0 ? $"{q.Price:0.00}" : $"{q.ChangePercent:0.00}%";
                        var tendency = q.ChangePercent > 0.0001 ? "UP" : (q.ChangePercent < -0.0001 ? "DOWN" : "SIDEWAYS");
                        _tickers.Add(new TickerViewModel { Name = symbol, Value = valueText, Tendency = tendency });
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
                try { DebugLogger.Log("[UI] Before TryStartBackendHelper"); } catch { }
                // start backend helper script if available
                TryStartBackendHelper();
                try { DebugLogger.Log("[UI] After TryStartBackendHelper"); } catch { }
                // wait for backend health endpoint before attempting websocket connect
                var ok = await WaitForBackendHealthAsync(TimeSpan.FromSeconds(12)).ConfigureAwait(false);
                try { DebugLogger.Log($"[UI] Health check result: {ok}"); } catch { }

                _wsCts = new CancellationTokenSource();
                _wsService = new WebSocketService("ws://127.0.0.1:8765");
                _wsService.OnTicker += msg => Dispatcher.Invoke(() => UpdateTicker(msg.Index == 0 ? 1 : msg.Index, msg.Name, msg.Value, msg.Tendency));
                _wsService.OnMarketUpdate += mu => Dispatcher.Invoke(() => RenderMarketUpdate(mu));
                _wsService.OnConfidence += v => Dispatcher.Invoke(() => { AddConfidenceSample(v); UpdateConfidence(v); });
                _wsService.OnVerdict += vm => Dispatcher.Invoke(() => { UpdateVerdict(vm.Verdict, vm.Ticker); UpdateReason(vm.Reason); if (vm.Confidence != 0) AddConfidenceSample(vm.Confidence); });
                _wsService.OnTranscript += t => Dispatcher.Invoke(() => {
                    // prepend new transcript line to top
                    try {
                        TranscriptTextBox.Text = t + "\n" + TranscriptTextBox.Text;
                    } catch { }
                });
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
                _backendProcess.Start();
                _backendProcess.BeginOutputReadLine();
                _backendProcess.BeginErrorReadLine();
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
                win = new Window {
                    Title = "Backend Log",
                    Width = 700,
                    Height = 420,
                    Owner = this,
                    WindowStartupLocation = WindowStartupLocation.CenterOwner
                };
                var grid = new Grid { Background = new SolidColorBrush(Color.FromRgb(17,17,17)), Margin = new Thickness(8) };
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

                var header = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
                var closeBtn = new Button { Content = "Close", Width = 64, Margin = new Thickness(0,0,0,8) };
                var styleObj = TryFindResource("SmallFlatButtonStyle");
                if (styleObj is Style style) closeBtn.Style = style;
                closeBtn.Click += (s, ev) => win.Close();
                header.Children.Add(closeBtn);
                Grid.SetRow(header, 0);
                grid.Children.Add(header);

                var tb = new TextBox {
                    Text = string.Empty,
                    IsReadOnly = true,
                    TextWrapping = TextWrapping.Wrap,
                    VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                    HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                    Background = new SolidColorBrush(Color.FromRgb(24,24,24)),
                    Foreground = new SolidColorBrush(Color.FromRgb(240,240,240)),
                    FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                    Margin = new Thickness(0)
                };
                Grid.SetRow(tb, 1);
                grid.Children.Add(tb);

                // populate current buffer
                lock(_backendLogBuffer)
                {
                    tb.Text = _backendLogBuffer.ToString();
                }

                // update periodically while open
                var timer = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
                timer.Tick += (s, ev) => { lock(_backendLogBuffer){ tb.Text = _backendLogBuffer.ToString(); tb.CaretIndex = tb.Text.Length; tb.ScrollToEnd(); } };
                win.Closed += (s, ev) => timer.Stop();
                timer.Start();

                win.Content = grid;
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
        public void UpdateVerdict(string verdict, string ticker)
        {
            var text = string.IsNullOrWhiteSpace(ticker) ? verdict : $"{verdict} {ticker}";
            VerdictTextBlock.Text = $"Current Verdict: {text}";

            // determine color: green for long/buy, red for short/sell, gray otherwise
            var v = verdict?.ToUpperInvariant() ?? string.Empty;
            Brush color = Brushes.Gray;
            if (v.Contains("LONG") || v.Contains("BUY") || v.Contains("BULL")) color = Brushes.Green;
            else if (v.Contains("SHORT") || v.Contains("SELL") || v.Contains("BEAR")) color = Brushes.Red;

            RingForeground.Stroke = color;
        }

        public void UpdateReason(string reason)
        {
            ReasonTextBlock.Text = reason ?? string.Empty;
        }

        public void UpdateConfidence(int value)
        {
            var clamped = Math.Max(0, Math.Min(100, value));
            ConfidenceTextBlock.Text = $"{clamped}/100";

            // update stroke dash to show portion of the ring
            // a simple approach: set dash array to [value, 100-value]
            RingForeground.StrokeDashArray = new DoubleCollection() { clamped, 100 - clamped };
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
                    // refresh the collection item by replacing it (simple approach)
                    var idx = _tickers.IndexOf(existing);
                    if (idx >= 0)
                    {
                        _tickers[idx] = existing;
                    }
                }
                else
                {
                    // append new ticker
                    _tickers.Add(new TickerViewModel { Name = name ?? string.Empty, Value = value ?? string.Empty, Tendency = t });
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
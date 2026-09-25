using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Navigation;
using System.Windows.Threading;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Financial Newsroom → "Macro Indicators". Replaces the one-line FRED ticker
    /// that used to sit next to Global Monitoring's globe (moved 2026-09-24): one
    /// card per series with latest value, change vs. the previous observation, a
    /// sparkline of the last observations and a note on how to read it. Reads
    /// GET /newsroom/macro from the local HedgeFund backend; the backend polls
    /// FRED on its own (every 6 hours by default — the series are daily or
    /// monthly), this view only re-reads the backend
    /// every minute. Source data only — no AI-generated text in this section.
    /// </summary>
    /// <summary>Chart range of the cards: the recent observations (default), or the
    /// backend's monthly averages since 2000 (last 5 years / everything).</summary>
    internal enum ChartRange { Recent, FiveYears, Since2000 }

    public partial class MacroIndicatorsView : UserControl, INewsroomView
    {
        private ChartRange _range = ChartRange.Recent;
        private string? _lastJson;
        private const string ItemsUrl = NewsroomBackend.BaseUrl + "/newsroom/macro";
        private static readonly TimeSpan AutoRefreshInterval = TimeSpan.FromSeconds(60);

        private readonly ObservableCollection<MacroSeriesViewModel> _series = new();   // all cards (also feeds the "i" popup)
        private readonly ObservableCollection<MacroSeriesViewModel> _raw = new();      // FRED series
        private readonly ObservableCollection<MacroSeriesViewModel> _derived = new();  // computed indicators
        private readonly ObservableCollection<MacroSeriesViewModel> _europe = new();   // euro-area series
        private readonly DispatcherTimer _autoRefreshTimer;
        private string _lastSignature = "";
        private bool _loading;

        public MacroIndicatorsView()
        {
            InitializeComponent();
            SeriesList.ItemsSource = _raw;
            DerivedList.ItemsSource = _derived;
            EuropeList.ItemsSource = _europe;
            SignalRulesList.ItemsSource = _series;
            _autoRefreshTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = AutoRefreshInterval };
            _autoRefreshTimer.Tick += async (s, e) =>
            {
                if (IsVisible) await LoadAsync(silent: true);
            };
            IsVisibleChanged += async (s, e) =>
            {
                if (e.NewValue is true && !_shutDown) await LoadAsync(silent: true);
            };
            _autoRefreshTimer.Start();
        }

        // Auto-refresh (reworked 2026-09-25): the timer no longer depends on the
        // view's Loaded/Unloaded events (cached views that are swapped in and out
        // of the newsroom window's ContentControl don't reliably get them in every
        // order). It runs for the view's lifetime at Normal priority, skips ticks
        // while the view isn't visible, refreshes right away when it becomes
        // visible again, and is stopped by Shutdown() when the window closes.
        private bool _shutDown;

        private async void View_Loaded(object sender, RoutedEventArgs e)
        {
            await LoadAsync(silent: _series.Count > 0);
        }

        private void View_Unloaded(object sender, RoutedEventArgs e)
        {
            CloseSignalInfo();
        }

        /// <summary>Called by the newsroom window when it closes.</summary>
        public void Shutdown()
        {
            _shutDown = true;
            _autoRefreshTimer.Stop();
            CloseSignalInfo();
        }

        // ---------- "i" info popup ----------
        // Hover: opens on entering the "i", closes as soon as the pointer leaves it.
        // Click: pins it open; a pinned popup closes 3 s after the pointer is off the
        // "i" (re-entering cancels that), or immediately on a second click.

        private DispatcherTimer? _signalInfoCloseTimer;
        private bool _signalInfoPinned;

        private void SignalInfo_MouseEnter(object sender, System.Windows.Input.MouseEventArgs e)
        {
            CancelSignalInfoCloseTimer();
            if (!SignalInfoPopup.IsOpen) OpenSignalInfo();
        }

        private void SignalInfo_MouseLeave(object sender, System.Windows.Input.MouseEventArgs e)
        {
            if (_signalInfoPinned) StartSignalInfoCloseTimer();
            else CloseSignalInfo();
        }

        private void SignalInfoButton_Click(object sender, RoutedEventArgs e)
        {
            if (_signalInfoPinned)
            {
                CloseSignalInfo(); // second click
                return;
            }
            _signalInfoPinned = true;
            if (!SignalInfoPopup.IsOpen) OpenSignalInfo();
            // Keyboard activation: the pointer isn't on the "i", so the 3 s start now.
            if (!SignalInfoButton.IsMouseOver) StartSignalInfoCloseTimer();
        }

        private void OpenSignalInfo()
        {
            SignalRulesEmptyText.Visibility = _series.Any(s => !string.IsNullOrEmpty(s.WatchRule))
                ? Visibility.Collapsed : Visibility.Visible;
            SignalInfoPopup.IsOpen = true;
        }

        private void CloseSignalInfo()
        {
            CancelSignalInfoCloseTimer();
            _signalInfoPinned = false;
            SignalInfoPopup.IsOpen = false;
        }

        private void StartSignalInfoCloseTimer()
        {
            CancelSignalInfoCloseTimer();
            _signalInfoCloseTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
            _signalInfoCloseTimer.Tick += (_, _) => CloseSignalInfo();
            _signalInfoCloseTimer.Start();
        }

        private void CancelSignalInfoCloseTimer()
        {
            _signalInfoCloseTimer?.Stop();
            _signalInfoCloseTimer = null;
        }

        private async void RefreshButton_Click(object sender, RoutedEventArgs e)
        {
            await LoadAsync(silent: false);
        }

        // ---------- Chart range switcher ----------

        private void RangeButton_Checked(object sender, RoutedEventArgs e)
        {
            if (!IsInitialized) return;
            _range = sender == RangeFiveYears ? ChartRange.FiveYears
                   : sender == RangeSince2000 ? ChartRange.Since2000
                   : ChartRange.Recent;
            if (_lastJson == null) return;
            try
            {
                Render(_lastJson, silent: true); // re-render the cached response, no new request
            }
            catch (Exception ex)
            {
                LastUpdatedText.Text = "Could not switch the chart range: " + ex.Message;
            }
        }

        private void Series_RequestNavigate(object sender, RequestNavigateEventArgs e)
        {
            NewsroomLinks.Open(e.Uri?.AbsoluteUri);
            e.Handled = true;
        }

        private async Task LoadAsync(bool silent)
        {
            if (_loading) return;
            _loading = true;
            if (!silent) ShowStatus("Loading macro indicators…");

            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
                var json = await client.GetStringAsync(ItemsUrl);
                _lastJson = json;
                Render(json, silent);
            }
            catch (Exception ex)
            {
                SetWaiting(true); // backend not reachable yet (e.g. still starting) -> retry soon
                if (silent && _series.Count > 0)
                {
                    LastUpdatedText.Text = $"Auto-refresh failed at {DateTime.Now:HH:mm:ss} ({ex.Message}) — showing last values";
                }
                else
                {
                    ShowStatus("The HedgeFund backend isn't answering yet (status: " + NewsroomBackend.Status + "). " +
                               "It is started automatically with the Hedge Fund window — the first start installs " +
                               "Python packages and can take a few minutes. This view retries on its own.");
                }
            }
            finally
            {
                _loading = false;
            }
        }


        /// <summary>Builds all cards from a /newsroom/macro response. Also used by the range
        /// switcher, which re-renders the cached response without a new request.</summary>
        private void Render(string json, bool silent)
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var items = new List<MacroSeriesViewModel>();
            if (root.TryGetProperty("items", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                var regimeDrawn = false;
                foreach (var el in arr.EnumerateArray())
                {
                    if (el.TryGetProperty("macro", out var mm) && mm.ValueKind == JsonValueKind.Object &&
                        mm.TryGetProperty("regime", out var reg) && reg.ValueKind == JsonValueKind.Object)
                    {
                        DrawRegime(reg);
                        regimeDrawn = true;
                        continue;
                    }
                    var vm = MacroSeriesViewModel.FromJson(el, _range);
                    if (vm != null) items.Add(vm);
                }
                if (!regimeDrawn) RegimeCard.Visibility = Visibility.Collapsed;
            }

            // Until the FRED collector has finished its first poll (status.ok still null)
            // or while nothing has arrived, re-read every few seconds instead of every
            // minute — otherwise a freshly started backend meant up to a minute of
            // "loading" although the cards arrive within seconds.
            var collectorPending = root.TryGetProperty("status", out var st0) && st0.ValueKind == JsonValueKind.Object &&
                                   st0.TryGetProperty("ok", out var ok0) && ok0.ValueKind == JsonValueKind.Null;
            SetWaiting(items.Count == 0 || collectorPending);
            LastUpdatedText.Text = _waiting
                ? $"Waiting for the first FRED poll — re-reading every {WaitingRefreshInterval.TotalSeconds:0}s · updated {DateTime.Now:HH:mm:ss}"
                : $"Re-read every {AutoRefreshInterval.TotalSeconds:0}s (the backend polls FRED every 6 h) · updated {DateTime.Now:HH:mm:ss}";

            if (items.Count == 0)
            {
                _series.Clear();
                _raw.Clear();
                _derived.Clear();
                _europe.Clear();
                DerivedHeader.Visibility = Visibility.Collapsed;
                EuropeHeader.Visibility = Visibility.Collapsed;
                _lastSignature = "";
                ShowStatus(EmptyMessage(root));
                return;
            }

            var signature = string.Join("|", items.Select(i => i.Signature));
            if (signature == _lastSignature)
            {
                ShowList();
                return;
            }
            _lastSignature = signature;

            var offset = ListScrollViewer.VerticalOffset;
            _series.Clear();
            _raw.Clear();
            _derived.Clear();
            _europe.Clear();
            foreach (var i in items)
            {
                _series.Add(i);
                (i.IsDerived ? _derived : i.Group == "europe" ? _europe : _raw).Add(i);
            }
            DerivedHeader.Visibility = _derived.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
            EuropeHeader.Visibility = _europe.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
            ShowList();
            if (silent) ListScrollViewer.ScrollToVerticalOffset(offset);
        }

        private static readonly TimeSpan WaitingRefreshInterval = TimeSpan.FromSeconds(3);
        private bool _waiting;

        private void SetWaiting(bool waiting)
        {
            _waiting = waiting;
            var interval = waiting ? WaitingRefreshInterval : AutoRefreshInterval;
            if (_autoRefreshTimer.Interval != interval) _autoRefreshTimer.Interval = interval;
        }

        private static string EmptyMessage(JsonElement root)
        {
            if (root.TryGetProperty("status", out var st) && st.ValueKind == JsonValueKind.Object)
            {
                var failed = st.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.False;
                var text = st.TryGetProperty("message", out var m) ? m.GetString() : null;
                if (failed && !string.IsNullOrWhiteSpace(text))
                    return "FRED collector: " + text + "\n\nIf the key is missing, set FRED_API_KEY in HedgeFund/backend/.env " +
                           "and restart the Hedge Fund window.";
            }
            return "No macro data loaded yet — the FRED collector fetches all series within a few seconds of the backend " +
                   "starting. This view refreshes automatically.";
        }

        // ---------- Macro regime map ----------

        private static readonly Dictionary<string, Color> RegimeColors = new()
        {
            ["goldilocks"] = Color.FromRgb(0x3E, 0xCF, 0x8E),
            ["reflation"] = Color.FromRgb(0xF3, 0x9C, 0x4A),
            ["slowdown"] = Color.FromRgb(0xF3, 0x9C, 0x4A),
            ["stagflation"] = Color.FromRgb(0xED, 0x6A, 0x5A),
        };

        private static SolidColorBrush FrozenBrush(Color c, byte alpha = 0xFF)
        {
            var b = new SolidColorBrush(Color.FromArgb(alpha, c.R, c.G, c.B));
            b.Freeze();
            return b;
        }

        private static string RegimeStr(JsonElement e, string name) =>
            e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

        private static double RegimeNum(JsonElement e, string name) =>
            e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0;

        private static string MonthLabel(string isoDate) =>
            DateTime.TryParseExact(isoDate, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var d)
                ? d.ToString("MMM yyyy", CultureInfo.InvariantCulture) : isoDate;

        private static ToolTip DarkToolTip(string text) => new()
        {
            Content = new TextBlock { Text = text },
            Background = FrozenBrush(Color.FromRgb(0x1A, 0x1A, 0x1A)),
            BorderBrush = FrozenBrush(Color.FromRgb(0x33, 0x33, 0x33)),
            Foreground = FrozenBrush(Color.FromRgb(0xE6, 0xE6, 0xE6)),
            Padding = new Thickness(7, 4, 7, 4),
            FontFamily = new FontFamily("Consolas"),
            FontSize = 10.5,
        };

        private void DrawRegime(JsonElement r)
        {
            var quadrant = RegimeStr(r, "quadrant");
            var color = RegimeColors.TryGetValue(quadrant, out var c) ? c : Color.FromRgb(0x8A, 0x8A, 0x8A);

            RegimeLabel.Text = RegimeStr(r, "label");
            RegimeLabel.Foreground = FrozenBrush(color);
            var months = (int)RegimeNum(r, "months_in_regime");
            RegimeSince.Text = $"as of {MonthLabel(RegimeStr(r, "date"))} · {months} month{(months == 1 ? "" : "s")} in this regime" +
                               (r.TryGetProperty("borderline", out var bl) && bl.ValueKind == JsonValueKind.True ? " · borderline" : "");
            RegimeDescription.Text = RegimeStr(r, "description");
            RegimeGrowth.Text = RegimeStr(r, "growth_text");
            RegimeInflation.Text = RegimeStr(r, "inflation_text");
            RegimeMethod.Text = RegimeStr(r, "method");

            var trail = new List<(string Date, double X, double Y, string Quadrant)>();
            if (r.TryGetProperty("trail", out var t) && t.ValueKind == JsonValueKind.Array)
                foreach (var p in t.EnumerateArray())
                    trail.Add((RegimeStr(p, "date"), RegimeNum(p, "x"), RegimeNum(p, "y"), RegimeStr(p, "quadrant")));
            if (trail.Count == 0)
            {
                RegimeCard.Visibility = Visibility.Collapsed;
                return;
            }

            var canvas = RegimeCanvas;
            canvas.Children.Clear();
            double w = canvas.Width, h = canvas.Height, cx = w / 2, cy = h / 2;
            // Symmetric scale per axis so the origin stays centred; at least ±0.5 pp.
            var maxX = Math.Max(0.5, trail.Max(p => Math.Abs(p.X))) * 1.2;
            var maxY = Math.Max(0.5, trail.Max(p => Math.Abs(p.Y))) * 1.2;
            double PX(double x) => cx + x / maxX * (cx - 10);
            double PY(double y) => cy - y / maxY * (cy - 10);

            // Quadrant tints + names.
            void Quad(double left, double top, string key, string name, HorizontalAlignment ha, VerticalAlignment va)
            {
                var active = key == quadrant;
                var rect = new Border
                {
                    Width = cx, Height = cy,
                    Background = FrozenBrush(RegimeColors[key], active ? (byte)0x26 : (byte)0x0E),
                    Child = new TextBlock
                    {
                        Text = name, FontSize = 10.5, FontWeight = active ? FontWeights.Bold : FontWeights.SemiBold,
                        Foreground = FrozenBrush(RegimeColors[key], active ? (byte)0xFF : (byte)0x99),
                        Margin = new Thickness(8, 6, 8, 6), HorizontalAlignment = ha, VerticalAlignment = va,
                    },
                };
                Canvas.SetLeft(rect, left);
                Canvas.SetTop(rect, top);
                canvas.Children.Add(rect);
            }
            Quad(0, 0, "stagflation", "STAGFLATION", HorizontalAlignment.Left, VerticalAlignment.Top);
            Quad(cx, 0, "reflation", "REFLATION", HorizontalAlignment.Right, VerticalAlignment.Top);
            Quad(0, cy, "slowdown", "SLOWDOWN", HorizontalAlignment.Left, VerticalAlignment.Bottom);
            Quad(cx, cy, "goldilocks", "GOLDILOCKS", HorizontalAlignment.Right, VerticalAlignment.Bottom);

            // Axes + captions.
            var axis = FrozenBrush(Color.FromRgb(0x44, 0x44, 0x44));
            canvas.Children.Add(new System.Windows.Shapes.Line { X1 = 0, X2 = w, Y1 = cy, Y2 = cy, Stroke = axis, StrokeThickness = 1 });
            canvas.Children.Add(new System.Windows.Shapes.Line { X1 = cx, X2 = cx, Y1 = 0, Y2 = h, Stroke = axis, StrokeThickness = 1 });
            var caption = FrozenBrush(Color.FromRgb(0x6A, 0x6A, 0x6A));
            var gx = new TextBlock { Text = "growth slowing ←   → accelerating", FontSize = 9, Foreground = caption };
            Canvas.SetLeft(gx, cx - 78);
            Canvas.SetTop(gx, cy + 2);
            canvas.Children.Add(gx);
            var iy = new TextBlock { Text = "↑ inflation rising", FontSize = 9, Foreground = caption };
            Canvas.SetLeft(iy, cx + 4);
            Canvas.SetTop(iy, 4);
            canvas.Children.Add(iy);
            var iy2 = new TextBlock { Text = "↓ inflation falling", FontSize = 9, Foreground = caption };
            Canvas.SetLeft(iy2, cx + 4);
            Canvas.SetTop(iy2, h - 16);
            canvas.Children.Add(iy2);

            // Trail line.
            var line = new System.Windows.Shapes.Polyline
            {
                Stroke = FrozenBrush(Color.FromRgb(0x6A, 0x6A, 0x6A)),
                StrokeThickness = 1.2,
                StrokeLineJoin = PenLineJoin.Round,
                IsHitTestVisible = false,
            };
            foreach (var p in trail) line.Points.Add(new Point(PX(p.X), PY(p.Y)));
            canvas.Children.Add(line);

            // Dots: older = smaller and fainter; latest = large, in the regime colour.
            for (var i = 0; i < trail.Count; i++)
            {
                var p = trail[i];
                var latest = i == trail.Count - 1;
                var size = latest ? 13.0 : 6.0;
                var name = RegimeColors.ContainsKey(p.Quadrant) ? p.Quadrant : quadrant;
                var dot = new System.Windows.Shapes.Ellipse
                {
                    Width = size, Height = size,
                    Fill = latest ? FrozenBrush(color) : FrozenBrush(RegimeColors.TryGetValue(name, out var dc) ? dc : color,
                                                                     (byte)(0x55 + 0x99 * i / Math.Max(1, trail.Count - 1))),
                    Stroke = FrozenBrush(Color.FromRgb(0x11, 0x11, 0x11)),
                    StrokeThickness = latest ? 2 : 1,
                    Cursor = System.Windows.Input.Cursors.Hand,
                    ToolTip = DarkToolTip($"{MonthLabel(p.Date)} · {(name.Length > 0 ? char.ToUpper(name[0]) + name[1..] : "—")}\n" +
                                          $"growth {p.X:+0.00;-0.00;0.00} pp · inflation {p.Y:+0.00;-0.00;0.00} pp"),
                };
                ToolTipService.SetInitialShowDelay(dot, 0);
                Canvas.SetLeft(dot, PX(p.X) - size / 2);
                Canvas.SetTop(dot, PY(p.Y) - size / 2);
                canvas.Children.Add(dot);
            }

            RegimeCard.Visibility = Visibility.Visible;
        }

        private void ShowStatus(string message)
        {
            StatusText.Text = message;
            StatusPanel.Visibility = Visibility.Visible;
            ListScrollViewer.Visibility = Visibility.Collapsed;
        }

        private void ShowList()
        {
            StatusPanel.Visibility = Visibility.Collapsed;
            ListScrollViewer.Visibility = Visibility.Visible;
        }

        /// <summary>One hoverable observation on a sparkline (coordinates in sparkline pixels).</summary>
        private sealed record SparkDot(double HitLeft, double HitWidth, double HitCenter, double DotLeft, double DotTop, string Label, Brush Brush);

        private sealed class MacroSeriesViewModel
        {
            // Must match the Grid in MacroCardTemplate (MacroIndicatorsView.xaml).
            private const double SparkWidth = 270;
            private const double SparkHeight = 44;
            private const double SparkPad = 3;
            private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

            // Stability colours: green = stable, orange = watch, red = alert.
            private static readonly Brush OkBrush = Frozen(0x3E, 0xCF, 0x8E);
            private static readonly Brush WatchBrush = Frozen(0xF3, 0x9C, 0x4A);
            private static readonly Brush AlertBrush = Frozen(0xED, 0x6A, 0x5A);

            private static Brush Frozen(byte r, byte g, byte b)
            {
                var brush = new SolidColorBrush(Color.FromRgb(r, g, b));
                brush.Freeze();
                return brush;
            }

            public string Label { get; init; } = "";
            public string ValueDisplay { get; init; } = "";
            public string ChangeDisplay { get; init; } = "";
            public string DateDisplay { get; init; } = "";
            public PointCollection SparkPoints { get; init; } = new();
            public List<SparkDot> SparkDots { get; init; } = new();
            public Brush SignalBrush { get; init; } = OkBrush;
            public string SignalText { get; init; } = "";
            public string WatchRule { get; init; } = "";
            public string AlertRule { get; init; } = "";
            public bool ShowZeroLine { get; init; }
            public bool ShowRefLine { get; init; }
            public double RefLineY { get; init; }
            public bool IsDerived { get; init; }
            public string Group { get; init; } = "us";
            public string ContextText { get; init; } = "";
            public bool HasContext => !string.IsNullOrEmpty(ContextText);
            public string Formula { get; init; } = "";
            public bool HasFormula => !string.IsNullOrEmpty(Formula);
            public bool HasUrl => UrlUri != null;
            public double ZeroLineY { get; init; }
            public string RangeDisplay { get; init; } = "";
            public string Note { get; init; } = "";
            public Uri? UrlUri { get; init; }
            public string SourceDisplay { get; init; } = "";
            public string Signature { get; init; } = "";

            private static string Str(JsonElement e, string name) =>
                e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

            private static double? Num(JsonElement e, string name) =>
                e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;

            private const int MaxChartPoints = 120; // keeps the hover columns usable on long ranges

            /// <summary>Keeps at most maxPoints points, evenly spaced, always including the last one.</summary>
            private static List<(string Date, double Value)> Downsample(List<(string Date, double Value)> points, int maxPoints)
            {
                if (points.Count <= maxPoints) return points;
                var result = new List<(string Date, double Value)>();
                var step = (points.Count - 1) / (double)(maxPoints - 1);
                for (var i = 0; i < maxPoints; i++)
                    result.Add(points[(int)Math.Round(i * step)]);
                return result;
            }

            /// <summary>" · next release Oct 15" for monthly/weekly series (from the FRED release calendar).</summary>
            private static string NextReleaseText(JsonElement m)
            {
                if (!m.TryGetProperty("next_release", out var nr) || nr.ValueKind != JsonValueKind.Object) return "";
                var d = Str(nr, "date");
                return DateTime.TryParseExact(d, "yyyy-MM-dd", Inv, DateTimeStyles.None, out var dt)
                    ? $" · next release {dt.ToString("MMM d", Inv)}"
                    : "";
            }

            private static string PrettyDate(string isoDate, bool monthly) =>
                DateTime.TryParseExact(isoDate, "yyyy-MM-dd", Inv, DateTimeStyles.None, out var d)
                    ? d.ToString(monthly ? "MMM yyyy" : "MMM d, yyyy", Inv)
                    : isoDate;

            // Counts in thousands (jobless claims) read better without decimals: "197K".
            private static string Fmt(double value, string unit) =>
                unit == "K" ? value.ToString("0", Inv) + "K"
                            : value.ToString("0.00", Inv) + (unit == "%" ? "%" : " " + unit);

            public static MacroSeriesViewModel? FromJson(JsonElement e, ChartRange chartRange)
            {
                if (!e.TryGetProperty("macro", out var m) || m.ValueKind != JsonValueKind.Object) return null;
                var value = Num(m, "value");
                if (value == null) return null;

                var unit = Str(m, "unit");
                var seriesId = Str(m, "series_id");
                var date = Str(m, "date");
                var previousDate = Str(m, "previous_date");
                var change = Num(m, "change");
                var changeUnit = unit == "%" || unit == "pp" ? "pp" : unit;

                // Arrow shows direction only — deliberately neutral colour: whether a
                // rising yield or falling unemployment is "good" depends on the position.
                var changeText = "";
                if (change != null)
                {
                    var arrow = change > 0 ? "▲" : change < 0 ? "▼" : "▶";
                    changeText = unit == "K"
                        ? $"{arrow} {change.Value.ToString("+0;-0;0", Inv)}K"
                        : $"{arrow} {change.Value.ToString("+0.00;-0.00;0.00", Inv)} {changeUnit}";
                }

                var history = new List<(string Date, double Value)>();
                if (m.TryGetProperty("history", out var h) && h.ValueKind == JsonValueKind.Array)
                {
                    foreach (var p in h.EnumerateArray())
                    {
                        var v = Num(p, "value");
                        if (v != null) history.Add((Str(p, "date"), v.Value));
                    }
                }

                // Range switcher: 5Y / since 2000 use the backend's monthly averages
                // (long_history); the value, change and colour always stay on the recent data.
                var rangeLabel = "";
                if (chartRange != ChartRange.Recent)
                {
                    var longHistory = new List<(string Date, double Value)>();
                    if (m.TryGetProperty("long_history", out var lh) && lh.ValueKind == JsonValueKind.Array)
                        foreach (var p in lh.EnumerateArray())
                        {
                            var v = Num(p, "value");
                            if (v != null) longHistory.Add((Str(p, "date"), v.Value));
                        }
                    if (longHistory.Count >= 2)
                    {
                        if (chartRange == ChartRange.FiveYears && longHistory.Count > 60)
                            longHistory = longHistory.GetRange(longHistory.Count - 60, 60);
                        history = Downsample(longHistory, MaxChartPoints);
                        rangeLabel = (chartRange == ChartRange.FiveYears ? "5 years" : "since 2000") + ", monthly averages · ";
                    }
                    else
                    {
                        rangeLabel = "long history unavailable — recent data · ";
                    }
                }

                // Stability signal computed by the backend (fred_collector.stability_signal).
                var signalBrush = OkBrush;
                var signalText = "";
                var watchRule = "";
                var alertRule = "";
                if (m.TryGetProperty("signal", out var sig) && sig.ValueKind == JsonValueKind.Object)
                {
                    var level = Str(sig, "level");
                    var reason = Str(sig, "reason");
                    (signalBrush, var label) = level switch
                    {
                        "alert" => (AlertBrush, "● Alert"),
                        "watch" => (WatchBrush, "● Watch"),
                        _ => (OkBrush, "● Stable"),
                    };
                    signalText = string.IsNullOrWhiteSpace(reason) ? label : label + " — " + reason;
                    if (sig.TryGetProperty("rules", out var rules) && rules.ValueKind == JsonValueKind.Object)
                    {
                        watchRule = Str(rules, "watch");
                        alertRule = Str(rules, "alert");
                    }
                }

                var points = new PointCollection();
                var dots = new List<SparkDot>();
                var showZero = false;
                double zeroY = 0;
                var refLine = Num(m, "ref_line");
                var showRef = false;
                double refY = 0;
                var isDerived = Str(m, "group") == "derived";
                var range = "";
                if (history.Count >= 2)
                {
                    var min = history.Min(x => x.Value);
                    var max = history.Max(x => x.Value);
                    // Keep a reference line (e.g. Sahm 0.5) inside the chart.
                    if (refLine != null)
                    {
                        min = Math.Min(min, refLine.Value);
                        max = Math.Max(max, refLine.Value);
                    }
                    var span = max - min;
                    if (span < 1e-9) { min -= 0.5; max += 0.5; span = 1; }
                    double Y(double v) => SparkPad + (max - v) / span * (SparkHeight - 2 * SparkPad);
                    var step = (SparkWidth - 2 * SparkPad) / (history.Count - 1);
                    // Monthly series are dated on the 1st -> show "Aug 2026" instead of "2026-08-01".
                    var monthly = history.All(x => x.Date.EndsWith("-01", StringComparison.Ordinal));
                    var hitWidth = Math.Max(4, Math.Min(step, 16));
                    for (var i = 0; i < history.Count; i++)
                    {
                        var px = SparkPad + i * step;
                        var py = Y(history[i].Value);
                        points.Add(new Point(px, py));
                        dots.Add(new SparkDot(
                            HitLeft: px - hitWidth / 2,
                            HitWidth: hitWidth,
                            HitCenter: hitWidth / 2,
                            DotLeft: hitWidth / 2 - 3.5,
                            DotTop: py - 3.5,
                            Label: $"{PrettyDate(history[i].Date, monthly)} · {Fmt(history[i].Value, unit)}",
                            Brush: signalBrush));
                    }
                    if (min < 0 && max > 0)
                    {
                        showZero = true;
                        zeroY = Y(0);
                    }
                    if (refLine != null)
                    {
                        showRef = true;
                        refY = Y(refLine.Value);
                    }
                    range = rangeLabel + $"{history.Count} points · {history[0].Date} → {history[^1].Date} · " +
                            $"low {Fmt(history.Min(x => x.Value), unit)} / high {Fmt(history.Max(x => x.Value), unit)}";
                }
                points.Freeze();

                var url = Str(e, "url");
                return new MacroSeriesViewModel
                {
                    Label = string.IsNullOrWhiteSpace(Str(e, "title")) ? seriesId : Str(e, "title"),
                    ValueDisplay = Fmt(value.Value, unit),
                    ChangeDisplay = changeText,
                    DateDisplay = (string.IsNullOrEmpty(previousDate)
                        ? $"as of {date}"
                        : $"as of {date} · previous {previousDate}") + NextReleaseText(m),
                    ContextText = m.TryGetProperty("context", out var ctx) && ctx.ValueKind == JsonValueKind.Object
                        ? Str(ctx, "text") : "",
                    Group = string.IsNullOrEmpty(Str(m, "group")) ? "us" : Str(m, "group"),
                    SparkPoints = points,
                    SparkDots = dots,
                    SignalBrush = signalBrush,
                    SignalText = signalText,
                    WatchRule = watchRule,
                    AlertRule = alertRule,
                    ShowZeroLine = showZero,
                    ZeroLineY = zeroY,
                    ShowRefLine = showRef,
                    RefLineY = refY,
                    IsDerived = isDerived,
                    Formula = isDerived ? "= " + Str(m, "formula") : "",
                    RangeDisplay = range,
                    Note = Str(m, "note"),
                    UrlUri = Uri.TryCreate(url, UriKind.Absolute, out var uri) ? uri : null,
                    SourceDisplay = $"FRED series {seriesId} ↗",
                    Signature = $"{seriesId}:{date}:{value.Value.ToString("R", Inv)}:{history.Count}:{signalText}:{chartRange}:" +
                                (m.TryGetProperty("context", out var ctx2) ? ctx2.ToString() : "") +
                                (m.TryGetProperty("next_release", out var nr2) ? nr2.ToString() : ""),
                };
            }
        }
    }
}

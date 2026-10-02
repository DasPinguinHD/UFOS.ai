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

            // Extended regime map popup: right edge aligned with the Expand button, below it
            // (above it if there is no room below); hover-close timer like the ticker popup.
            RegimePopup.CustomPopupPlacementCallback = (popupSize, targetSize, offset) => new[]
            {
                new System.Windows.Controls.Primitives.CustomPopupPlacement(
                    new Point(targetSize.Width - popupSize.Width, targetSize.Height + 6),
                    System.Windows.Controls.Primitives.PopupPrimaryAxis.Horizontal),
                new System.Windows.Controls.Primitives.CustomPopupPlacement(
                    new Point(targetSize.Width - popupSize.Width, -popupSize.Height - 6),
                    System.Windows.Controls.Primitives.PopupPrimaryAxis.Horizontal),
            };
            _regimePopupCloseTimer.Tick += (_, _) =>
            {
                _regimePopupCloseTimer.Stop();
                if (RegimePopupBorder.IsMouseOver || RegimeExpandButton.IsMouseOver) return;
                CloseRegimePopup();
            };
            // Switching to another newsroom section hides the view -> don't leave the popup floating.
            IsVisibleChanged += (s, e) =>
            {
                if (e.NewValue is false) CloseRegimePopup();
            };
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
            HookRegimeWindow();
            await LoadAsync(silent: _series.Count > 0);
        }

        private void View_Unloaded(object sender, RoutedEventArgs e)
        {
            CloseSignalInfo();
            CloseRegimePopup();
        }

        /// <summary>Called by the newsroom window when it closes.</summary>
        public void Shutdown()
        {
            _shutDown = true;
            _autoRefreshTimer.Stop();
            CloseSignalInfo();
            CloseRegimePopup();
            UnhookRegimeWindow();
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
                using var client = NewsroomBackend.CreateClient(TimeSpan.FromSeconds(10));
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
                        UpdateRegimeExtended(reg);
                        regimeDrawn = true;
                        continue;
                    }
                    var vm = MacroSeriesViewModel.FromJson(el, _range);
                    if (vm != null) items.Add(vm);
                }
                if (!regimeDrawn)
                {
                    RegimeCard.Visibility = Visibility.Collapsed;
                    UpdateRegimeExtended(null);
                }
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

        // ---------- Extended regime map (Expand popup / inline on wide windows) ----------
        // Backend contract: macro.regime.extended = { trail_long, recessions, components, note,
        // euro, phase_behaviour } (newsroom/macro_derived.py). Parsed into plain records right
        // away because the JsonDocument is disposed after Render. Missing "extended" -> the
        // Expand button stays hidden and only the compact card is shown. Source data only.

        // Inline layout (instead of the popup) when the view is at least this wide and the
        // window at least this tall. The expanded content is three columns, ~1340 × 650 px
        // (2026-09-26: fits a maximized 1920×1080 window — and 2560×1440 at 150 % — without
        // scrolling); + card padding/border/margin, the view's margins and a scrollbar.
        private const double RegimeInlineMinWidth = 1480;   // view ActualWidth
        private const double RegimeInlineMinHeight = 900;   // hosting window ActualHeight
        private static readonly Color EuroBlue = Color.FromRgb(0x49, 0xA7, 0xFF);
        private static readonly Color RecessionRed = Color.FromRgb(0xED, 0x6A, 0x5A);
        private static readonly Color RegimeGood = Color.FromRgb(0x3E, 0xCF, 0x8E);
        private static readonly string[] RegimePhaseKeys = { "goldilocks", "reflation", "stagflation", "slowdown" };

        private sealed record RegimePoint(string Date, double X, double Y, string Quadrant);
        private sealed record RegimeRecession(string Start, string End);
        private sealed record RegimeComponent(string Name, string Axis, double? Latest, double? SixMonthsAgo, double? Change, string Unit, string Date);
        private sealed record RegimeEuro(double X, double Y, string Quadrant, string Label, string Date, List<RegimePoint> Trail, string Inputs, string SourceUrl);
        private sealed record RegimePhaseInfo(string Summary, List<(string Asset, string Tendency)> Assets);
        private sealed record RegimeEpisode(string Label, string Date, double X, double Y, string Quadrant);
        private sealed record RegimeTendencyRow(string Quadrant, string Label, List<(string Direction, string Text)> Cells);
        private sealed record RegimeTendencies(string Title, List<string> Columns, List<RegimeTendencyRow> Rows, string Disclaimer);
        private sealed record RegimeExtended(
            string Quadrant, string Label, string Date,
            List<RegimePoint> TrailLong, List<RegimeRecession> Recessions, List<RegimeComponent> Components, string Note,
            RegimeEuro? Euro, Dictionary<string, RegimePhaseInfo> Phases, string Disclaimer,
            List<RegimeEpisode> Episodes, List<(string Date, string Quadrant)> History,
            Dictionary<string, List<(string Date, double Value)>> ComponentSeries, string InflationName,
            RegimeTendencies? Tendencies);

        private RegimeExtended? _regimeExt;
        private string _regimeExtRaw = "";
        private bool _regimePopupOpen;
        private bool _regimeInline;
        private bool _regimeInlineDismissed; // user pressed "Collapse" while the window is wide
        private int _regimeTrailMonths = 36;  // 12, 36 (default) or 0 (= everything since 2000)
        private string _regimePhase = "";
        private bool _regimeSuppressToggle;
        private readonly DispatcherTimer _regimePopupCloseTimer = new() { Interval = TimeSpan.FromSeconds(3) };
        private Window? _regimeHookedWindow;

        private bool IsRegimeExpandedShown => _regimePopupOpen || _regimeInline;

        private static double? RegimeNumOrNull(JsonElement e, string name) =>
            e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;

        private static string JsonToText(JsonElement v) => v.ValueKind switch
        {
            JsonValueKind.String => v.GetString() ?? "",
            JsonValueKind.Number => v.GetDouble().ToString("0.###", CultureInfo.InvariantCulture),
            JsonValueKind.True => "yes",
            JsonValueKind.False => "no",
            JsonValueKind.Object => string.Join(" · ", v.EnumerateObject().Select(p => p.Name + ": " + JsonToText(p.Value))),
            JsonValueKind.Array => string.Join(", ", v.EnumerateArray().Select(a => JsonToText(a))),
            _ => "",
        };

        private static List<RegimePoint> ParseRegimePoints(JsonElement parent, string name)
        {
            var list = new List<RegimePoint>();
            if (parent.ValueKind != JsonValueKind.Object || !parent.TryGetProperty(name, out var arr) || arr.ValueKind != JsonValueKind.Array)
                return list;
            foreach (var p in arr.EnumerateArray())
            {
                if (p.ValueKind != JsonValueKind.Object) continue;
                var x = RegimeNumOrNull(p, "x");
                var y = RegimeNumOrNull(p, "y");
                if (x == null || y == null) continue;
                list.Add(new RegimePoint(RegimeStr(p, "date"), x.Value, y.Value, RegimeStr(p, "quadrant")));
            }
            return list;
        }

        private static RegimeExtended? ParseRegimeExtended(JsonElement r)
        {
            if (r.ValueKind != JsonValueKind.Object || !r.TryGetProperty("extended", out var ex) || ex.ValueKind != JsonValueKind.Object)
                return null;

            var trail = ParseRegimePoints(ex, "trail_long");
            if (trail.Count == 0) trail = ParseRegimePoints(r, "trail"); // fall back to the compact 12-month trail
            if (trail.Count == 0) return null;

            var recessions = new List<RegimeRecession>();
            if (ex.TryGetProperty("recessions", out var rec) && rec.ValueKind == JsonValueKind.Array)
                foreach (var p in rec.EnumerateArray())
                {
                    if (p.ValueKind != JsonValueKind.Object) continue;
                    var start = RegimeStr(p, "start");
                    var end = RegimeStr(p, "end");
                    if (start.Length < 7) continue;
                    recessions.Add(new RegimeRecession(start[..7], end.Length >= 7 ? end[..7] : "")); // empty end = ongoing
                }

            var components = new List<RegimeComponent>();
            if (ex.TryGetProperty("components", out var cs) && cs.ValueKind == JsonValueKind.Array)
                foreach (var c in cs.EnumerateArray())
                {
                    if (c.ValueKind != JsonValueKind.Object) continue;
                    components.Add(new RegimeComponent(RegimeStr(c, "name"), RegimeStr(c, "axis"), RegimeNumOrNull(c, "latest"),
                        RegimeNumOrNull(c, "six_months_ago"), RegimeNumOrNull(c, "change"), RegimeStr(c, "unit"), RegimeStr(c, "date")));
                }

            RegimeEuro? euro = null;
            if (ex.TryGetProperty("euro", out var eu) && eu.ValueKind == JsonValueKind.Object)
            {
                var euX = RegimeNumOrNull(eu, "x");
                var euY = RegimeNumOrNull(eu, "y");
                if (euX != null && euY != null)
                {
                    var inputs = "";
                    if (eu.TryGetProperty("inputs", out var inp))
                        inputs = inp.ValueKind == JsonValueKind.Object
                            ? string.Join("\n", inp.EnumerateObject().Select(p => p.Name + ": " + JsonToText(p.Value)))
                            : JsonToText(inp);
                    euro = new RegimeEuro(euX.Value, euY.Value, RegimeStr(eu, "quadrant"), RegimeStr(eu, "label"), RegimeStr(eu, "date"),
                        ParseRegimePoints(eu, "trail"), inputs, RegimeStr(eu, "source_url"));
                }
            }

            var phases = new Dictionary<string, RegimePhaseInfo>();
            var disclaimer = RegimeStr(ex, "disclaimer");
            if (ex.TryGetProperty("phase_behaviour", out var pb) && pb.ValueKind == JsonValueKind.Object)
                foreach (var prop in pb.EnumerateObject())
                {
                    if (prop.Name == "disclaimer")
                    {
                        if (prop.Value.ValueKind == JsonValueKind.String) disclaimer = prop.Value.GetString() ?? "";
                        continue;
                    }
                    if (prop.Value.ValueKind != JsonValueKind.Object) continue;
                    var assets = new List<(string Asset, string Tendency)>();
                    if (prop.Value.TryGetProperty("assets", out var aa) && aa.ValueKind == JsonValueKind.Array)
                        foreach (var a in aa.EnumerateArray())
                            if (a.ValueKind == JsonValueKind.Object)
                                assets.Add((RegimeStr(a, "asset"), RegimeStr(a, "tendency")));
                    phases[prop.Name] = new RegimePhaseInfo(RegimeStr(prop.Value, "summary"), assets);
                }

            // Second pass (2026-09-25): episodes, monthly history, sparkline series, asset table.
            var episodes = new List<RegimeEpisode>();
            if (ex.TryGetProperty("episodes", out var eps) && eps.ValueKind == JsonValueKind.Array)
                foreach (var p in eps.EnumerateArray())
                {
                    if (p.ValueKind != JsonValueKind.Object) continue;
                    var epX = RegimeNumOrNull(p, "x");
                    var epY = RegimeNumOrNull(p, "y");
                    if (epX == null || epY == null) continue;
                    episodes.Add(new RegimeEpisode(RegimeStr(p, "label"), RegimeStr(p, "date"), epX.Value, epY.Value, RegimeStr(p, "quadrant")));
                }

            var history = new List<(string Date, string Quadrant)>();
            if (ex.TryGetProperty("history", out var hist) && hist.ValueKind == JsonValueKind.Array)
                foreach (var p in hist.EnumerateArray())
                    if (p.ValueKind == JsonValueKind.Object)
                        history.Add((RegimeStr(p, "date"), RegimeStr(p, "quadrant")));
            if (history.Count == 0) history = trail.Select(p => (p.Date, p.Quadrant)).ToList();

            var series = new Dictionary<string, List<(string Date, double Value)>>();
            var inflationName = "";
            if (ex.TryGetProperty("component_series", out var cser) && cser.ValueKind == JsonValueKind.Object)
                foreach (var prop in cser.EnumerateObject())
                {
                    if (prop.Name == "inflation_name")
                    {
                        if (prop.Value.ValueKind == JsonValueKind.String) inflationName = prop.Value.GetString() ?? "";
                        continue;
                    }
                    if (prop.Value.ValueKind != JsonValueKind.Array) continue;
                    var pts = new List<(string Date, double Value)>();
                    foreach (var p in prop.Value.EnumerateArray())
                    {
                        if (p.ValueKind != JsonValueKind.Object) continue;
                        var v = RegimeNumOrNull(p, "value");
                        if (v != null) pts.Add((RegimeStr(p, "date"), v.Value));
                    }
                    series[prop.Name] = pts;
                }

            RegimeTendencies? tendencies = null;
            if (ex.TryGetProperty("asset_tendencies", out var at) && at.ValueKind == JsonValueKind.Object)
            {
                var columns = new List<string>();
                if (at.TryGetProperty("columns", out var cols) && cols.ValueKind == JsonValueKind.Array)
                    foreach (var c in cols.EnumerateArray())
                        columns.Add(c.ValueKind == JsonValueKind.String ? c.GetString() ?? "" : "");
                var rows = new List<RegimeTendencyRow>();
                if (at.TryGetProperty("rows", out var rs) && rs.ValueKind == JsonValueKind.Array)
                    foreach (var row in rs.EnumerateArray())
                    {
                        if (row.ValueKind != JsonValueKind.Object) continue;
                        var cells = new List<(string Direction, string Text)>();
                        if (row.TryGetProperty("cells", out var cl) && cl.ValueKind == JsonValueKind.Array)
                            foreach (var c in cl.EnumerateArray())
                                if (c.ValueKind == JsonValueKind.Object)
                                    cells.Add((RegimeStr(c, "direction"), RegimeStr(c, "text")));
                        rows.Add(new RegimeTendencyRow(RegimeStr(row, "quadrant"), RegimeStr(row, "label"), cells));
                    }
                if (columns.Count > 0 && rows.Count > 0)
                    tendencies = new RegimeTendencies(RegimeStr(at, "title"), columns, rows, RegimeStr(at, "disclaimer"));
            }

            return new RegimeExtended(RegimeStr(r, "quadrant"), RegimeStr(r, "label"), RegimeStr(r, "date"),
                trail, recessions, components, RegimeStr(ex, "note"), euro, phases, disclaimer,
                episodes, history, series, inflationName, tendencies);
        }

        /// <summary>Called from Render with the regime element (null = no regime in the response).</summary>
        private void UpdateRegimeExtended(JsonElement? r)
        {
            RegimeExtended? ext = null;
            var raw = "";
            try
            {
                if (r is JsonElement re && re.ValueKind == JsonValueKind.Object && RegimeCard.Visibility == Visibility.Visible)
                {
                    ext = ParseRegimeExtended(re);
                    if (ext != null) raw = re.GetRawText();
                }
            }
            catch (Exception)
            {
                ext = null; // malformed extended block -> compact card only
                raw = "";
            }
            var changed = raw != _regimeExtRaw;
            _regimeExt = ext;
            _regimeExtRaw = raw;
            var rendered = UpdateRegimeLayout();
            // Re-render the expanded view on new data only while it is visible.
            if (changed && !rendered && IsRegimeExpandedShown) RenderRegimeExpanded();
        }

        private static string PhaseName(string key) =>
            string.IsNullOrEmpty(key) ? "—" : char.ToUpperInvariant(key[0]) + key[1..];

        /// <summary>"2026-08-01" or "2026-08" -> "Aug 2026".</summary>
        private static string RegimeMonth(string date)
        {
            if (DateTime.TryParseExact(date, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var d) ||
                DateTime.TryParseExact(date, "yyyy-MM", CultureInfo.InvariantCulture, DateTimeStyles.None, out d))
                return d.ToString("MMM yyyy", CultureInfo.InvariantCulture);
            return date;
        }

        // ----- layout: popup vs. inline -----

        private void View_SizeChanged(object sender, SizeChangedEventArgs e)
        {
            // The view grows/shrinks with the window in both directions.
            if (e.WidthChanged || e.HeightChanged) UpdateRegimeLayout();
        }

        private bool IsRegimeRoomy()
        {
            var height = Window.GetWindow(this)?.ActualHeight ?? ActualHeight;
            return ActualWidth >= RegimeInlineMinWidth && height >= RegimeInlineMinHeight;
        }

        /// <summary>Shows the extended map inline (wide view) or keeps it in the popup host.
        /// Returns true when it just rendered the expanded content.</summary>
        private bool UpdateRegimeLayout()
        {
            var has = _regimeExt != null;
            RegimeExpandButton.Visibility = has ? Visibility.Visible : Visibility.Collapsed;
            var wide = IsRegimeRoomy();
            if (!wide) _regimeInlineDismissed = false; // next time the view is wide it opens inline again
            var shouldInline = has && wide && !_regimeInlineDismissed;
            var rendered = false;

            if (shouldInline && !_regimeInline)
            {
                CloseRegimePopup();
                RegimePopupRoot.Children.Remove(RegimeExpandedRoot);
                RegimeInlineHost.Child = RegimeExpandedRoot;
                RegimeInlineHost.Visibility = Visibility.Visible;
                RegimeCompactGrid.Visibility = Visibility.Collapsed;
                _regimeInline = true;
                RenderRegimeExpanded();
                rendered = true;
            }
            else if (!shouldInline && _regimeInline)
            {
                RegimeInlineHost.Child = null;
                RegimeInlineHost.Visibility = Visibility.Collapsed;
                RegimePopupRoot.Children.Add(RegimeExpandedRoot);
                RegimeCompactGrid.Visibility = Visibility.Visible;
                _regimeInline = false;
            }

            if (!has) CloseRegimePopup();
            // Inline: the card's own Expand/Collapse column is hidden (saves ~80 px of width) and
            // the Collapse button inside the expanded view takes over. Popup: both exist.
            RegimeExpandButton.Visibility = has && !_regimeInline ? Visibility.Visible : Visibility.Collapsed;
            RegimeInnerCollapseButton.Visibility = Visibility.Visible;
            ApplyRegimeColumns(_regimeInline);
            UpdateRegimeButtonText();
            return rendered;
        }

        /// <summary>
        /// Inline (wide view): map | inputs | asset behaviour side by side. Popup (narrow view):
        /// the asset column goes below the first two so the popup stays ~950 px wide.
        /// </summary>
        // Macro card grid: cards are 336 px wide + 10 px gap (MacroCardTemplate). The regime card
        // lines up with it: compact = three cards, expanded inline = four cards (= the 1340 px
        // three-column content + 32 px padding + 2 px border).
        private const double MacroCardPitch = 336 + 10;
        private const double RegimeCompactWidth = 3 * MacroCardPitch - 10;  // 1028
        private const double RegimeExpandedWidth = 4 * MacroCardPitch - 10; // 1374

        private void ApplyRegimeColumns(bool threeColumns)
        {
            RegimeCard.Width = threeColumns ? RegimeExpandedWidth : RegimeCompactWidth;
            Grid.SetRow(RegimeAssetColumn, threeColumns ? 0 : 1);
            Grid.SetColumn(RegimeAssetColumn, threeColumns ? 2 : 0);
            Grid.SetColumnSpan(RegimeAssetColumn, threeColumns ? 1 : 2);
            RegimeAssetColumn.Margin = threeColumns ? new Thickness(20, 0, 0, 0) : new Thickness(0, 18, 0, 0);
        }

        private void UpdateRegimeButtonText()
        {
            var expanded = IsRegimeExpandedShown;
            RegimeExpandButton.Content = expanded ? "Collapse" : "Expand";
            System.Windows.Automation.AutomationProperties.SetName(RegimeExpandButton, expanded
                ? "Collapse the extended macro regime map"
                : "Expand the macro regime map: 36-month trail, historical episodes, input sparklines, regime history since 2000 and typical asset behaviour");
        }

        // ----- open / close -----

        private void RegimeExpandButton_Click(object sender, RoutedEventArgs e)
        {
            if (_regimeExt == null) return;
            if (_regimeInline)
            {
                _regimeInlineDismissed = true; // back to the compact card
                UpdateRegimeLayout();
                return;
            }
            if (IsRegimeRoomy())
            {
                _regimeInlineDismissed = false; // wide view: expand inline again
                UpdateRegimeLayout();
                return;
            }
            if (_regimePopupOpen) CloseRegimePopup(); // second click
            else OpenRegimePopup();
        }

        private void RegimeInnerCollapse_Click(object sender, RoutedEventArgs e)
        {
            if (_regimeInline)
            {
                _regimeInlineDismissed = true; // back to the compact card
                UpdateRegimeLayout();
                RegimeExpandButton.Focus();
                return;
            }
            CloseRegimePopup();
            RegimeExpandButton.Focus();
        }

        private void OpenRegimePopup()
        {
            if (_regimePopupOpen || _regimeInline || _regimeExt == null) return;
            _regimePhase = _regimeExt.Quadrant; // each opening starts on the current phase
            RenderRegimeExpanded();
            RegimePopupScroll.MaxHeight = Math.Max(320, SystemParameters.WorkArea.Height * 0.85);
            _regimePopupOpen = true;
            RegimePopup.IsOpen = true;
            UpdateRegimeButtonText();
            // The 3 s hover-close timer only starts once the pointer leaves the popup/button.
            _regimePopupCloseTimer.Stop();
        }

        private void CloseRegimePopup()
        {
            _regimePopupCloseTimer.Stop();
            if (RegimePopup.IsOpen) RegimePopup.IsOpen = false;
            _regimePopupOpen = false;
            UpdateRegimeButtonText();
        }

        private void RegimePopup_Closed(object? sender, EventArgs e)
        {
            _regimePopupCloseTimer.Stop();
            _regimePopupOpen = false;
            UpdateRegimeButtonText();
        }

        private void RestartRegimePopupCloseTimer()
        {
            _regimePopupCloseTimer.Stop();
            _regimePopupCloseTimer.Start();
        }

        // Hover behaviour like the ticker popup (LiveStreamAgent MarketDataPopup): the pointer
        // leaving the popup (or the Expand button while the popup is open) starts a 3 s close
        // timer, coming back cancels it. Opened by keyboard it stays until Collapse/Esc.
        private void RegimePopupBorder_MouseEnter(object sender, System.Windows.Input.MouseEventArgs e)
        {
            _regimePopupCloseTimer.Stop();
        }

        private void RegimePopupBorder_MouseLeave(object sender, System.Windows.Input.MouseEventArgs e)
        {
            if (_regimePopupOpen) RestartRegimePopupCloseTimer();
        }

        private void RegimeExpandButton_MouseEnter(object sender, System.Windows.Input.MouseEventArgs e)
        {
            _regimePopupCloseTimer.Stop();
        }

        private void RegimeExpandButton_MouseLeave(object sender, System.Windows.Input.MouseEventArgs e)
        {
            if (_regimePopupOpen && !RegimePopupBorder.IsMouseOver) RestartRegimePopupCloseTimer();
        }

        private void View_PreviewKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
        {
            if (e.Key != System.Windows.Input.Key.Escape || !_regimePopupOpen) return;
            CloseRegimePopup();
            RegimeExpandButton.Focus();
            e.Handled = true;
        }

        // The popup doesn't follow the window when it's moved -> close it instead.
        private void HookRegimeWindow()
        {
            var w = Window.GetWindow(this);
            if (w == _regimeHookedWindow) return;
            UnhookRegimeWindow();
            _regimeHookedWindow = w;
            if (w != null) w.LocationChanged += RegimeWindow_LocationChanged;
        }

        private void UnhookRegimeWindow()
        {
            if (_regimeHookedWindow != null) _regimeHookedWindow.LocationChanged -= RegimeWindow_LocationChanged;
            _regimeHookedWindow = null;
        }

        private void RegimeWindow_LocationChanged(object? sender, EventArgs e) => CloseRegimePopup();

        // ----- toggles -----

        private void RegimeTrailRange_Checked(object sender, RoutedEventArgs e)
        {
            if (!IsInitialized || _regimeSuppressToggle) return;
            _regimeTrailMonths = sender == RegimeTrail3Y ? 36 : sender == RegimeTrailAll ? 0 : 12;
            var ext = _regimeExt;
            if (ext == null || !IsRegimeExpandedShown) return;
            try
            {
                DrawRegimeBig(ext);
            }
            catch (Exception ex)
            {
                RegimeBigSummary.Text = "Could not draw the extended regime map: " + ex.Message;
            }
        }

        private void RegimePhase_Checked(object sender, RoutedEventArgs e)
        {
            if (!IsInitialized || _regimeSuppressToggle) return;
            if (sender is not RadioButton rb || rb.Tag is not string key) return;
            SelectRegimePhase(key);
        }

        /// <summary>Shows the typical-behaviour details for a phase (table row click or toggle).</summary>
        private void SelectRegimePhase(string key)
        {
            if (!RegimePhaseKeys.Contains(key)) return;
            _regimePhase = key;
            var ext = _regimeExt;
            if (ext == null) return;
            SyncRegimeToggles(ext);
            FillRegimePhase(ext);
            FillRegimeTendencies(ext); // selection highlight
        }

        private void SyncRegimeToggles(RegimeExtended ext)
        {
            _regimeSuppressToggle = true;
            try
            {
                RegimeTrail12M.IsChecked = _regimeTrailMonths == 12;
                RegimeTrail3Y.IsChecked = _regimeTrailMonths == 36;
                RegimeTrailAll.IsChecked = _regimeTrailMonths == 0;
                foreach (var rb in new[] { RegimePhaseGoldilocks, RegimePhaseReflation, RegimePhaseStagflation, RegimePhaseSlowdown })
                {
                    var key = rb.Tag as string ?? "";
                    rb.IsChecked = key == _regimePhase;
                    var current = key == ext.Quadrant;
                    rb.Content = current ? "● " + PhaseName(key) : PhaseName(key); // dot = current phase
                    rb.ToolTip = current ? "Current phase" : null;
                    System.Windows.Automation.AutomationProperties.SetName(rb,
                        "Typical behaviour in " + PhaseName(key) + (current ? " (current phase)" : ""));
                }
            }
            finally
            {
                _regimeSuppressToggle = false;
            }
        }

        // ----- rendering -----

        private void RenderRegimeExpanded()
        {
            var ext = _regimeExt;
            if (ext == null) return;
            try
            {
                if (!RegimePhaseKeys.Contains(_regimePhase))
                    _regimePhase = RegimePhaseKeys.Contains(ext.Quadrant) ? ext.Quadrant : RegimePhaseKeys[0];
                SyncRegimeToggles(ext);
                SetTip(RegimeHistoryInfo, "One cell per month, coloured by the quadrant the same method gave for that month. Hover a month for details.");
                SetTip(RegimeSparkInfo, "Monthly YoY rates and the two smoothed momentum series (dashed line = 0). Hover for each month.");
                DrawRegimeBig(ext);
                FillRegimeComponents(ext);
                FillRegimeSparks(ext);
                FillRegimePhase(ext);
                DrawRegimeHistory(ext);
                FillRegimeTendencies(ext);
            }
            catch (Exception ex)
            {
                RegimeBigSummary.Text = "Could not draw the extended regime map: " + ex.Message;
            }
        }

        private static double Percentile(List<double> values, double q)
        {
            if (values.Count == 0) return 0;
            var sorted = values.OrderBy(v => v).ToList();
            var idx = (int)Math.Ceiling(q * sorted.Count) - 1;
            return sorted[Math.Clamp(idx, 0, sorted.Count - 1)];
        }

        private static string Signed(double v) => v.ToString("+0.00;-0.00;0.00", CultureInfo.InvariantCulture);

        private static void SetTip(FrameworkElement el, string text)
        {
            el.ToolTip = DarkToolTip(text);
            ToolTipService.SetInitialShowDelay(el, 0);
            ToolTipService.SetBetweenShowDelay(el, 0);
        }

        private void DrawRegimeBig(RegimeExtended ext)
        {
            var canvas = RegimeBigCanvas;
            canvas.Children.Clear();
            RegimeBigLegend.Children.Clear();

            var all = ext.TrailLong;
            var pts = _regimeTrailMonths > 0 && all.Count > _regimeTrailMonths
                ? all.GetRange(all.Count - _regimeTrailMonths, _regimeTrailMonths)
                : all;
            if (pts.Count == 0) return;
            var euro = ext.Euro;
            var euroTrail = euro == null ? new List<RegimePoint>()
                : euro.Trail.Count > 12 ? euro.Trail.GetRange(euro.Trail.Count - 12, 12) : euro.Trail;
            var quadrant = ext.Quadrant;
            var color = RegimeColors.TryGetValue(quadrant, out var qc) ? qc : Color.FromRgb(0x8A, 0x8A, 0x8A);

            RegimeBigTitle.Text = $"{(string.IsNullOrEmpty(ext.Label) ? PhaseName(quadrant) : ext.Label)} · as of {RegimeMonth(ext.Date)}";
            RegimeBigTitle.Foreground = FrozenBrush(color);

            double w = canvas.Width, h = canvas.Height, cx = w / 2, cy = h / 2;
            const double pad = 12;

            // Symmetric scale per axis from the 95th percentile of |x| / |y|, so one crisis month
            // (2008, 2020) doesn't squash 25 years into the centre; points beyond it are pinned
            // to the edge and drawn hollow. The latest US point is always inside.
            var xs = pts.Select(p => Math.Abs(p.X)).Concat(euroTrail.Select(p => Math.Abs(p.X))).ToList();
            var ys = pts.Select(p => Math.Abs(p.Y)).Concat(euroTrail.Select(p => Math.Abs(p.Y))).ToList();
            if (euro != null)
            {
                xs.Add(Math.Abs(euro.X));
                ys.Add(Math.Abs(euro.Y));
            }
            var maxX = Math.Max(0.5, Percentile(xs, 0.95)) * 1.2;
            var maxY = Math.Max(0.5, Percentile(ys, 0.95)) * 1.2;
            maxX = Math.Max(maxX, Math.Abs(pts[^1].X) * 1.1);
            maxY = Math.Max(maxY, Math.Abs(pts[^1].Y) * 1.1);

            (double X, double Y, bool Out) Map(double x, double y)
            {
                var px = cx + x / maxX * (cx - pad);
                var py = cy - y / maxY * (cy - pad);
                var clX = Math.Clamp(px, pad / 2, w - pad / 2);
                var clY = Math.Clamp(py, pad / 2, h - pad / 2);
                return (clX, clY, Math.Abs(clX - px) > 0.01 || Math.Abs(clY - py) > 0.01);
            }

            bool InRecession(string date)
            {
                if (date.Length < 7) return false;
                var m = date[..7];
                return ext.Recessions.Any(rc => string.CompareOrdinal(m, rc.Start) >= 0 &&
                                                (rc.End.Length == 0 || string.CompareOrdinal(m, rc.End) <= 0));
            }

            // Quadrant tints + names (same layout/colours as the compact map).
            void Quad(double left, double top, string key, string name, HorizontalAlignment ha, VerticalAlignment va)
            {
                var active = key == quadrant;
                var rect = new Border
                {
                    Width = cx, Height = cy,
                    Background = FrozenBrush(RegimeColors[key], active ? (byte)0x26 : (byte)0x0E),
                    Child = new TextBlock
                    {
                        Text = name, FontSize = 11.5, FontWeight = active ? FontWeights.Bold : FontWeights.SemiBold,
                        Foreground = FrozenBrush(RegimeColors[key], active ? (byte)0xFF : (byte)0x99),
                        Margin = new Thickness(10, 8, 10, 8), HorizontalAlignment = ha, VerticalAlignment = va,
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

            // Axes, captions and scale.
            var axis = FrozenBrush(Color.FromRgb(0x44, 0x44, 0x44));
            canvas.Children.Add(new System.Windows.Shapes.Line { X1 = 0, X2 = w, Y1 = cy, Y2 = cy, Stroke = axis, StrokeThickness = 1 });
            canvas.Children.Add(new System.Windows.Shapes.Line { X1 = cx, X2 = cx, Y1 = 0, Y2 = h, Stroke = axis, StrokeThickness = 1 });
            var caption = FrozenBrush(Color.FromRgb(0x6A, 0x6A, 0x6A));
            void Caption(string text, double left, double top)
            {
                var tb = new TextBlock { Text = text, FontSize = 9.5, Foreground = caption, IsHitTestVisible = false };
                Canvas.SetLeft(tb, left);
                Canvas.SetTop(tb, top);
                canvas.Children.Add(tb);
            }
            Caption("growth slowing ←   → accelerating", cx - 84, cy + 3);
            Caption("↑ inflation rising", cx + 5, 5);
            Caption("↓ inflation falling", cx + 5, h - 17);
            Caption($"{Signed(maxX)} pp", w - 58, cy - 15);
            Caption($"{Signed(-maxX)} pp", 4, cy - 15);
            Caption($"{Signed(maxY)} pp", cx - 58, 5);

            // US trail line.
            var many = pts.Count > 60;
            var line = new System.Windows.Shapes.Polyline
            {
                Stroke = FrozenBrush(Color.FromRgb(0x6A, 0x6A, 0x6A), many ? (byte)0x88 : (byte)0xFF),
                StrokeThickness = many ? 0.8 : 1.2,
                StrokeLineJoin = PenLineJoin.Round,
                IsHitTestVisible = false,
            };
            foreach (var p in pts)
            {
                var m = Map(p.X, p.Y);
                line.Points.Add(new Point(m.X, m.Y));
            }
            canvas.Children.Add(line);

            var dark = FrozenBrush(Color.FromRgb(0x11, 0x11, 0x11));
            var anyClipped = false;
            var recessionCount = 0;
            FrameworkElement? latestDot = null;

            // US dots: older = fainter; recession months get a red ring; latest = big, drawn last.
            for (var i = 0; i < pts.Count; i++)
            {
                var p = pts[i];
                var latest = i == pts.Count - 1;
                var m = Map(p.X, p.Y);
                anyClipped |= m.Out;
                var inRec = InRecession(p.Date);
                if (inRec) recessionCount++;
                var name = RegimeColors.ContainsKey(p.Quadrant) ? p.Quadrant : quadrant;
                var dc = RegimeColors.TryGetValue(name, out var c2) ? c2 : color;
                var age = pts.Count > 1 ? i / (double)(pts.Count - 1) : 1.0;
                var alpha = (byte)(0x40 + 0xBF * age);
                var size = latest ? 16.0 : many ? 5.0 : 7.0;

                if (inRec)
                {
                    var ringSize = size + 6;
                    var ring = new System.Windows.Shapes.Ellipse
                    {
                        Width = ringSize, Height = ringSize,
                        Stroke = FrozenBrush(RecessionRed, (byte)Math.Max(0xA0, (int)alpha)),
                        StrokeThickness = 1.4,
                        IsHitTestVisible = false,
                    };
                    Canvas.SetLeft(ring, m.X - ringSize / 2);
                    Canvas.SetTop(ring, m.Y - ringSize / 2);
                    canvas.Children.Add(ring);
                }

                var dot = new System.Windows.Shapes.Ellipse
                {
                    Width = size, Height = size,
                    Fill = m.Out && !latest ? Brushes.Transparent : latest ? FrozenBrush(color) : FrozenBrush(dc, alpha),
                    Stroke = m.Out && !latest ? FrozenBrush(dc, alpha) : dark,
                    StrokeThickness = latest ? 2 : m.Out ? 1.4 : 0.8,
                    Cursor = System.Windows.Input.Cursors.Hand,
                };
                SetTip(dot, $"{RegimeMonth(p.Date)} · {PhaseName(name)}{(inRec ? " · NBER recession" : "")}{(latest ? " · latest" : "")}\n" +
                            $"growth {Signed(p.X)} pp · inflation {Signed(p.Y)} pp" +
                            (m.Out ? "\n(beyond the chart scale — pinned to the edge)" : ""));
                Canvas.SetLeft(dot, m.X - size / 2);
                Canvas.SetTop(dot, m.Y - size / 2);
                if (latest) latestDot = dot;
                else canvas.Children.Add(dot);

                // Quarter labels on some points ("Q1 '25"): every quarter start on short trails,
                // January/July on the 3-year trail; none on the long view (too crowded).
                if (!latest && !many && QuarterLabel(p.Date, pts.Count <= 18) is string ql)
                {
                    var qt = new TextBlock
                    {
                        Text = ql, FontSize = 8.5, IsHitTestVisible = false,
                        Foreground = FrozenBrush(Color.FromRgb(0xB0, 0xB0, 0xB0), (byte)Math.Max(0x70, (int)alpha)),
                    };
                    Canvas.SetLeft(qt, m.X + size / 2 + 2);
                    Canvas.SetTop(qt, m.Y - size / 2 - 11);
                    canvas.Children.Add(qt);
                }
            }

            // Euro area: 12-month trail (thin blue line) + diamond for the latest month.
            if (euro != null)
            {
                var blueLine = new System.Windows.Shapes.Polyline
                {
                    Stroke = FrozenBrush(EuroBlue, 0xAA),
                    StrokeThickness = 1,
                    StrokeLineJoin = PenLineJoin.Round,
                    IsHitTestVisible = false,
                };
                foreach (var p in euroTrail)
                {
                    var m = Map(p.X, p.Y);
                    blueLine.Points.Add(new Point(m.X, m.Y));
                }
                var em = Map(euro.X, euro.Y);
                anyClipped |= em.Out;
                blueLine.Points.Add(new Point(em.X, em.Y));
                canvas.Children.Add(blueLine);

                for (var i = 0; i < euroTrail.Count; i++)
                {
                    var p = euroTrail[i];
                    var m = Map(p.X, p.Y);
                    anyClipped |= m.Out;
                    var age = euroTrail.Count > 1 ? i / (double)(euroTrail.Count - 1) : 1.0;
                    var d = new System.Windows.Shapes.Ellipse
                    {
                        Width = 5, Height = 5,
                        Fill = m.Out ? Brushes.Transparent : FrozenBrush(EuroBlue, (byte)(0x50 + 0xAF * age)),
                        Stroke = m.Out ? FrozenBrush(EuroBlue) : null,
                        StrokeThickness = 1,
                        Cursor = System.Windows.Input.Cursors.Hand,
                    };
                    SetTip(d, $"Euro area · {RegimeMonth(p.Date)} · {PhaseName(p.Quadrant)}\n" +
                              $"growth {Signed(p.X)} pp · inflation {Signed(p.Y)} pp");
                    Canvas.SetLeft(d, m.X - 2.5);
                    Canvas.SetTop(d, m.Y - 2.5);
                    canvas.Children.Add(d);
                }

                const double r = 8;
                var diamond = new System.Windows.Shapes.Polygon
                {
                    Points = new PointCollection { new Point(r, 0), new Point(2 * r, r), new Point(r, 2 * r), new Point(0, r) },
                    Fill = FrozenBrush(EuroBlue),
                    Stroke = dark,
                    StrokeThickness = 1.5,
                    Cursor = System.Windows.Input.Cursors.Hand,
                };
                SetTip(diamond, $"{(string.IsNullOrEmpty(euro.Label) ? "Euro area" : "Euro area · " + euro.Label)} · {RegimeMonth(euro.Date)} · {PhaseName(euro.Quadrant)}\n" +
                                $"growth {Signed(euro.X)} pp · inflation {Signed(euro.Y)} pp" +
                                (string.IsNullOrWhiteSpace(euro.Inputs) ? "" : "\n" + euro.Inputs) +
                                "\nEurostat industrial production × HICP");
                Canvas.SetLeft(diamond, em.X - r);
                Canvas.SetTop(diamond, em.Y - r);
                canvas.Children.Add(diamond);
                var ea = new TextBlock { Text = "EA", FontSize = 9.5, FontWeight = FontWeights.SemiBold, Foreground = FrozenBrush(EuroBlue), IsHitTestVisible = false };
                Canvas.SetLeft(ea, em.X + r + 2);
                Canvas.SetTop(ea, em.Y - 7);
                canvas.Children.Add(ea);
            }

            // Historical episodes (backend: extended.episodes) — the same regime math applied to
            // fixed reference months since 2000. Scale decision: the map keeps the trail's own
            // scale (95th percentile of the shown range), so the current picture isn't squashed
            // by 2009/2020; an episode beyond that scale is pinned to the edge, its label gets an
            // arrow pointing where it really lies, and the tooltip carries its real values.
            var epBrush = FrozenBrush(Color.FromRgb(0xD8, 0xD8, 0xD8));
            var epLabelBg = FrozenBrush(Color.FromRgb(0x11, 0x11, 0x11), 0xC0);
            var placed = new List<Rect>();
            foreach (var ep in ext.Episodes)
            {
                var em = Map(ep.X, ep.Y);
                var rawX = cx + ep.X / maxX * (cx - pad);
                var rawY = cy - ep.Y / maxY * (cy - pad);
                var arrow = (rawX > w - pad / 2 ? "→" : rawX < pad / 2 ? "←" : "") +
                            (rawY < pad / 2 ? "↑" : rawY > h - pad / 2 ? "↓" : "");
                const double er = 6;
                var diamondEp = new System.Windows.Shapes.Polygon
                {
                    Points = new PointCollection { new Point(er, 0), new Point(2 * er, er), new Point(er, 2 * er), new Point(0, er) },
                    Fill = dark,
                    Stroke = epBrush,
                    StrokeThickness = 1.3,
                    Cursor = System.Windows.Input.Cursors.Hand,
                };
                SetTip(diamondEp, $"{ep.Label} · {PhaseName(ep.Quadrant)}\n" +
                                  $"growth {Signed(ep.X)} pp · inflation {Signed(ep.Y)} pp\n" +
                                  "Historical reference month, same method as today's dot" +
                                  (em.Out ? $"\n(beyond the chart scale — pinned to the edge {arrow})" : ""));
                Canvas.SetLeft(diamondEp, em.X - er);
                Canvas.SetTop(diamondEp, em.Y - er);
                canvas.Children.Add(diamondEp);

                var epText = new TextBlock
                {
                    Text = arrow.Length > 0 ? $"{ep.Label} {arrow}" : ep.Label,
                    FontSize = 9.5, Foreground = epBrush, Background = epLabelBg,
                    Padding = new Thickness(2, 0, 2, 0), IsHitTestVisible = false,
                };
                epText.Measure(new Size(double.PositiveInfinity, double.PositiveInfinity));
                var tw = epText.DesiredSize.Width;
                var th = Math.Max(12, epText.DesiredSize.Height);
                var left = em.X + er + 3;
                if (left + tw > w - 2) left = Math.Max(2, em.X - er - 3 - tw);
                var top = Math.Clamp(em.Y - th / 2, 1, h - th - 1);
                // Nudge down (then up) until it doesn't overlap an earlier label.
                for (var tries = 0; tries < 8 && placed.Any(rc => rc.IntersectsWith(new Rect(left, top, tw, th))); tries++)
                    top = top + th + 1 <= h - th - 1 ? top + th + 1 : Math.Max(1, top - 2 * (th + 1));
                placed.Add(new Rect(left, top, tw, th));
                Canvas.SetLeft(epText, left);
                Canvas.SetTop(epText, top);
                canvas.Children.Add(epText);
            }

            if (latestDot != null)
            {
                canvas.Children.Add(latestDot);
                var lm = Map(pts[^1].X, pts[^1].Y);
                var us = new TextBlock { Text = "US", FontSize = 9.5, FontWeight = FontWeights.SemiBold, Foreground = FrozenBrush(color), IsHitTestVisible = false };
                Canvas.SetLeft(us, lm.X + 11);
                Canvas.SetTop(us, lm.Y - 7);
                canvas.Children.Add(us);
            }

            // Legend (shapes + text, not colour only).
            var legendText = FrozenBrush(Color.FromRgb(0x9A, 0x9A, 0x9A));
            void Legend(FrameworkElement swatch, string text)
            {
                swatch.VerticalAlignment = VerticalAlignment.Center;
                var sp = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 16, 4) };
                sp.Children.Add(swatch);
                sp.Children.Add(new TextBlock { Text = text, FontSize = 10, Foreground = legendText, Margin = new Thickness(6, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center });
                RegimeBigLegend.Children.Add(sp);
            }
            var grey = Color.FromRgb(0x9A, 0x9A, 0x9A);
            Legend(new System.Windows.Shapes.Ellipse { Width = 6, Height = 6, Fill = FrozenBrush(grey, 0x88) }, "US monthly readings (older = fainter)");
            Legend(new System.Windows.Shapes.Ellipse { Width = 12, Height = 12, Fill = FrozenBrush(color), Stroke = dark, StrokeThickness = 2 }, "Latest US month");
            if (ext.Recessions.Count > 0)
                Legend(new System.Windows.Shapes.Ellipse { Width = 12, Height = 12, Stroke = FrozenBrush(RecessionRed), StrokeThickness = 1.4 }, "NBER recession months");
            if (ext.Episodes.Count > 0)
                Legend(new System.Windows.Shapes.Polygon
                {
                    Points = new PointCollection { new Point(5, 0), new Point(10, 5), new Point(5, 10), new Point(0, 5) },
                    Stroke = epBrush, StrokeThickness = 1.2,
                }, "Historical episodes (arrow = beyond the scale, in that direction)");
            if (euro != null)
                Legend(new System.Windows.Shapes.Polygon
                {
                    Points = new PointCollection { new Point(6, 0), new Point(12, 6), new Point(6, 12), new Point(0, 6) },
                    Fill = FrozenBrush(EuroBlue),
                }, "Euro area (Eurostat IP × HICP)");
            if (anyClipped)
                Legend(new System.Windows.Shapes.Ellipse { Width = 7, Height = 7, Stroke = FrozenBrush(grey), StrokeThickness = 1.2 }, "Beyond the scale, pinned to the edge");
            if (euro != null && Uri.TryCreate(euro.SourceUrl, UriKind.Absolute, out var srcUri))
            {
                var link = new System.Windows.Documents.Hyperlink(new System.Windows.Documents.Run("Eurostat source ↗"))
                {
                    NavigateUri = srcUri,
                    Foreground = FrozenBrush(EuroBlue),
                };
                link.RequestNavigate += Series_RequestNavigate;
                var tb = new TextBlock { FontSize = 10, Margin = new Thickness(0, 0, 16, 4), VerticalAlignment = VerticalAlignment.Center };
                tb.Inlines.Add(link);
                RegimeBigLegend.Children.Add(tb);
            }

            // Summary (visible caption + screen-reader name of the canvas).
            var last = pts[^1];
            var summary = $"{PhaseName(quadrant)} as of {RegimeMonth(ext.Date)}: US growth {Signed(last.X)} pp, inflation {Signed(last.Y)} pp. " +
                          $"Showing {pts.Count} month{(pts.Count == 1 ? "" : "s")} ({RegimeMonth(pts[0].Date)} – {RegimeMonth(last.Date)})" +
                          (ext.Recessions.Count > 0 ? $", {recessionCount} of them in NBER recessions" : "") + "." +
                          (euro != null ? $" Euro area: {PhaseName(euro.Quadrant)} (growth {Signed(euro.X)} pp, inflation {Signed(euro.Y)} pp, {RegimeMonth(euro.Date)})." : "") +
                          (ext.Episodes.Count > 0 ? $" Diamonds: {ext.Episodes.Count} historical episodes for comparison." : "") +
                          " Hover the dots for each month.";
            RegimeBigSummary.Text = summary;
            System.Windows.Automation.AutomationProperties.SetName(canvas, "Extended macro regime map. " + summary);
        }

        private void FillRegimeComponents(RegimeExtended ext)
        {
            var g = RegimeComponentsGrid;
            g.Children.Clear();
            g.RowDefinitions.Clear();
            g.ColumnDefinitions.Clear();
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            var inv = CultureInfo.InvariantCulture;
            var headBrush = FrozenBrush(Color.FromRgb(0x6A, 0x6A, 0x6A));
            var textBrush = FrozenBrush(Color.FromRgb(0xE6, 0xE6, 0xE6));
            var numBrush = FrozenBrush(Color.FromRgb(0xBF, 0xBF, 0xBF));
            var neutral = FrozenBrush(Color.FromRgb(0x8A, 0x8A, 0x8A));
            var row = 0;

            void Add(UIElement el, int col, int colSpan = 1)
            {
                Grid.SetRow(el, row);
                Grid.SetColumn(el, col);
                if (colSpan > 1) Grid.SetColumnSpan(el, colSpan);
                g.Children.Add(el);
            }
            void NewRow() => g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            TextBlock Num(string text, Brush fg) => new()
            {
                Text = text, FontFamily = new FontFamily("Consolas"), FontSize = 10.5, Foreground = fg,
                Margin = new Thickness(10, 3, 0, 3), HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Top,
            };
            static string Fmt(double? v, string unit) =>
                v == null ? "—" : v.Value.ToString("0.00", CultureInfo.InvariantCulture) +
                                  (unit == "%" ? "%" : string.IsNullOrEmpty(unit) ? "" : " " + unit);

            if (ext.Components.Count == 0)
            {
                NewRow();
                Add(new TextBlock { Text = "No component data from the backend.", FontSize = 10.5, Foreground = neutral }, 0, 4);
            }
            else
            {
                NewRow();
                Add(new TextBlock { Text = "INPUT", FontSize = 9.5, FontWeight = FontWeights.SemiBold, Foreground = headBrush }, 0);
                Add(new TextBlock { Text = "LATEST", FontSize = 9.5, FontWeight = FontWeights.SemiBold, Foreground = headBrush, Margin = new Thickness(10, 0, 0, 0), HorizontalAlignment = HorizontalAlignment.Right }, 1);
                Add(new TextBlock { Text = "6M AGO", FontSize = 9.5, FontWeight = FontWeights.SemiBold, Foreground = headBrush, Margin = new Thickness(10, 0, 0, 0), HorizontalAlignment = HorizontalAlignment.Right }, 2);
                Add(new TextBlock { Text = "CHANGE", FontSize = 9.5, FontWeight = FontWeights.SemiBold, Foreground = headBrush, Margin = new Thickness(10, 0, 0, 0), HorizontalAlignment = HorizontalAlignment.Right }, 3);
                row++;

                foreach (var axisKey in new[] { "growth", "inflation", "" })
                {
                    var group = ext.Components
                        .Where(c => axisKey.Length == 0 ? c.Axis != "growth" && c.Axis != "inflation" : c.Axis == axisKey)
                        .ToList();
                    if (group.Count == 0) continue;
                    NewRow();
                    Add(new TextBlock
                    {
                        Text = axisKey == "growth" ? "Growth axis (left ↔ right)" : axisKey == "inflation" ? "Inflation axis (down ↕ up)" : "Other inputs",
                        FontSize = 10, FontStyle = FontStyles.Italic, Foreground = headBrush, Margin = new Thickness(0, 6, 0, 2),
                    }, 0, 4);
                    row++;

                    foreach (var c in group)
                    {
                        NewRow();
                        var nameBlock = new TextBlock
                        {
                            Text = string.IsNullOrEmpty(c.Name) ? "—" : c.Name, FontSize = 10.5, Foreground = textBrush,
                            TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 3, 0, 3),
                        };
                        if (!string.IsNullOrEmpty(c.Date)) SetTip(nameBlock, $"{c.Name} · latest reading {RegimeMonth(c.Date)}");
                        Add(nameBlock, 0);
                        Add(Num(Fmt(c.Latest, c.Unit), numBrush), 1);
                        Add(Num(Fmt(c.SixMonthsAgo, c.Unit), numBrush), 2);

                        // Arrow colour = what the change does to the regime reading:
                        // green = stronger growth / cooler inflation, red = the opposite.
                        var change = c.Change ?? (c.Latest != null && c.SixMonthsAgo != null ? c.Latest - c.SixMonthsAgo : null);
                        Brush arrowBrush = neutral;
                        var arrow = "▶";
                        if (change != null && Math.Abs(change.Value) > 1e-9)
                        {
                            var up = change.Value > 0;
                            arrow = up ? "▲" : "▼";
                            if (c.Axis == "growth") arrowBrush = FrozenBrush(up ? RegimeGood : RecessionRed);
                            else if (c.Axis == "inflation") arrowBrush = FrozenBrush(up ? RecessionRed : RegimeGood);
                        }
                        var changeUnit = c.Unit == "%" ? "pp" : c.Unit;
                        var changeText = change == null ? "—"
                            : $"{arrow} {change.Value.ToString("+0.00;-0.00;0.00", inv)}{(string.IsNullOrEmpty(changeUnit) ? "" : " " + changeUnit)}";
                        Add(Num(changeText, arrowBrush), 3);
                        row++;
                    }
                }
            }

            var note = ext.Note;
            const string key = "Arrow colour: green = pushes towards stronger growth or cooler inflation, red = the opposite.";
            RegimeComponentsNote.Text = string.IsNullOrWhiteSpace(note) ? key : note + "\n" + key;
            // Shown as the ⓘ tooltip next to "What moves the dot" (saves ~100 px of height).
            SetTip(RegimeComponentsInfo, RegimeComponentsNote.Text);
        }

        private static string? QuarterLabel(string date, bool everyQuarter)
        {
            if (date.Length < 7 ||
                !int.TryParse(date.Substring(5, 2), NumberStyles.Integer, CultureInfo.InvariantCulture, out var month) ||
                !int.TryParse(date.Substring(2, 2), NumberStyles.Integer, CultureInfo.InvariantCulture, out var yy))
                return null;
            if (month % 3 != 1) return null;
            if (!everyQuarter && month != 1 && month != 7) return null;
            return $"Q{(month + 2) / 3} '{yy:00}";
        }

        private static string FmtSpark(double v, string unit) =>
            unit == "%" ? v.ToString("0.0", CultureInfo.InvariantCulture) + "%" : Signed(v) + " pp";

        // Sparkline rows: key in extended.component_series, label, unit, colour rule
        // (0 = neutral level, 1 = growth momentum: up = green, 2 = inflation momentum: up = red).
        private static readonly (string Key, string Name, string Unit, int Axis)[] RegimeSparkSpecs =
        {
            ("payrolls_yoy", "Nonfarm payrolls", "%", 0),
            ("indpro_yoy", "Industrial production", "%", 0),
            ("inflation_yoy", "", "%", 0), // name from inflation_name
            ("growth_momentum", "Growth momentum (x axis)", "pp", 1),
            ("inflation_momentum", "Inflation momentum (y axis)", "pp", 2),
        };

        private void FillRegimeSparks(RegimeExtended ext)
        {
            var panel = RegimeSparkPanel;
            panel.Children.Clear();
            var nameBrush = FrozenBrush(Color.FromRgb(0xE6, 0xE6, 0xE6));
            var valueBrush = FrozenBrush(Color.FromRgb(0xBF, 0xBF, 0xBF));
            var lineBrush = FrozenBrush(Color.FromRgb(0x9A, 0x9A, 0x9A));
            var zeroBrush = FrozenBrush(Color.FromRgb(0x55, 0x55, 0x55));
            var any = false;
            foreach (var (key, baseName, unit, axis) in RegimeSparkSpecs)
            {
                if (!ext.ComponentSeries.TryGetValue(key, out var pts) || pts.Count < 2) continue;
                any = true;
                var name = key == "inflation_yoy"
                    ? (string.IsNullOrEmpty(ext.InflationName)
                        ? "Inflation"
                        : char.ToUpperInvariant(ext.InflationName[0]) + ext.InflationName[1..] + " inflation")
                    : baseName;
                name += unit == "%" ? ", YoY" : ", 6-month change";
                var last = pts[^1];
                Brush lastBrush = axis == 0 ? valueBrush
                    : FrozenBrush((axis == 1) == (last.Value >= 0) ? RegimeGood : RecessionRed);

                var head = new Grid { Margin = new Thickness(0, 8, 0, 2) };
                head.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                head.Children.Add(new TextBlock { Text = name, FontSize = 10.5, Foreground = nameBrush, TextTrimming = TextTrimming.CharacterEllipsis });
                var val = new TextBlock
                {
                    Text = FmtSpark(last.Value, unit) + " · " + RegimeMonth(last.Date),
                    FontFamily = new FontFamily("Consolas"), FontSize = 10.5, Foreground = lastBrush, Margin = new Thickness(8, 0, 0, 0),
                };
                Grid.SetColumn(val, 1);
                head.Children.Add(val);
                panel.Children.Add(head);

                const double cw = 340, ch = 32, sp = 3; // = width of the inputs column
                var spark = new Canvas { Width = cw, Height = ch, HorizontalAlignment = HorizontalAlignment.Left, ClipToBounds = true, Background = Brushes.Transparent };
                var dataMin = pts.Min(q => q.Value);
                var dataMax = pts.Max(q => q.Value);
                double min = dataMin, max = dataMax;
                if (axis != 0)
                {
                    min = Math.Min(min, 0); // momentum: always show the zero line
                    max = Math.Max(max, 0);
                }
                if (max - min < 1e-9)
                {
                    max += 0.5;
                    min -= 0.5;
                }
                var count = pts.Count;
                double SX(int i) => sp + i * (cw - 2 * sp) / (count - 1);
                double SY(double v) => sp + (max - v) / (max - min) * (ch - 2 * sp);

                if (min < 0 && max > 0)
                    spark.Children.Add(new System.Windows.Shapes.Line
                    {
                        X1 = 0, X2 = cw, Y1 = SY(0), Y2 = SY(0), Stroke = zeroBrush, StrokeThickness = 1,
                        StrokeDashArray = new DoubleCollection { 3, 3 }, IsHitTestVisible = false,
                    });
                var poly = new System.Windows.Shapes.Polyline
                {
                    Stroke = lineBrush, StrokeThickness = 1.3, StrokeLineJoin = PenLineJoin.Round, IsHitTestVisible = false,
                };
                for (var i = 0; i < count; i++) poly.Points.Add(new Point(SX(i), SY(pts[i].Value)));
                spark.Children.Add(poly);
                var endDot = new System.Windows.Shapes.Ellipse { Width = 6, Height = 6, Fill = lastBrush, IsHitTestVisible = false };
                Canvas.SetLeft(endDot, SX(count - 1) - 3);
                Canvas.SetTop(endDot, SY(last.Value) - 3);
                spark.Children.Add(endDot);

                // Hover: one invisible column per month.
                var colW = (cw - 2 * sp) / (count - 1);
                for (var i = 0; i < count; i++)
                {
                    var hit = new System.Windows.Shapes.Rectangle { Width = colW, Height = ch, Fill = Brushes.Transparent };
                    SetTip(hit, $"{name}\n{RegimeMonth(pts[i].Date)}: {FmtSpark(pts[i].Value, unit)}");
                    Canvas.SetLeft(hit, Math.Max(0, SX(i) - colW / 2));
                    spark.Children.Add(hit);
                }
                System.Windows.Automation.AutomationProperties.SetName(spark,
                    $"{name}: {count} months to {RegimeMonth(last.Date)}, latest {FmtSpark(last.Value, unit)}, " +
                    $"range {FmtSpark(dataMin, unit)} to {FmtSpark(dataMax, unit)}");
                panel.Children.Add(spark);
            }
            RegimeSparkSection.Visibility = any ? Visibility.Visible : Visibility.Collapsed;
        }

        /// <summary>Horizontal timeline since 2000: one cell per month coloured by its quadrant
        /// (Slowdown drawn fainter than Reflation — both orange), NBER recessions as a thin red
        /// bar above, year ticks below, hover per month.</summary>
        private void DrawRegimeHistory(RegimeExtended ext)
        {
            var canvas = RegimeHistoryCanvas;
            canvas.Children.Clear();
            var hist = ext.History.Where(hm => hm.Date.Length >= 7).ToList();
            if (hist.Count == 0)
            {
                RegimeHistorySection.Visibility = Visibility.Collapsed;
                return;
            }
            RegimeHistorySection.Visibility = Visibility.Visible;

            var w = canvas.Width;
            const double recTop = 0, recH = 3, barTop = 5, barH = 20;
            var cellW = w / hist.Count;
            var grey = Color.FromRgb(0x5A, 0x5A, 0x5A);
            var tickBrush = FrozenBrush(Color.FromRgb(0x55, 0x55, 0x55));
            var yearBrush = FrozenBrush(Color.FromRgb(0x8A, 0x8A, 0x8A));
            var recBrush = FrozenBrush(RecessionRed);
            var counts = new Dictionary<string, int>();
            byte CellAlpha(string q) => q == "slowdown" ? (byte)0x70 : (byte)0xD8;

            for (var i = 0; i < hist.Count; i++)
            {
                var (date, q) = hist[i];
                var latest = i == hist.Count - 1;
                counts[q] = counts.TryGetValue(q, out var n) ? n + 1 : 1;
                var month = date[..7];
                var inRec = ext.Recessions.Any(rc => string.CompareOrdinal(month, rc.Start) >= 0 &&
                                                     (rc.End.Length == 0 || string.CompareOrdinal(month, rc.End) <= 0));
                var color = RegimeColors.TryGetValue(q, out var qc) ? qc : grey;
                var cell = new System.Windows.Shapes.Rectangle
                {
                    Width = cellW + 0.6, Height = barH, Fill = FrozenBrush(color, CellAlpha(q)),
                };
                SetTip(cell, $"{RegimeMonth(date)} · {PhaseName(q)}{(inRec ? " · NBER recession" : "")}{(latest ? " · latest" : "")}");
                Canvas.SetLeft(cell, i * cellW);
                Canvas.SetTop(cell, barTop);
                canvas.Children.Add(cell);

                if (inRec)
                {
                    var rec = new System.Windows.Shapes.Rectangle { Width = cellW + 0.6, Height = recH, Fill = recBrush, IsHitTestVisible = false };
                    Canvas.SetLeft(rec, i * cellW);
                    Canvas.SetTop(rec, recTop);
                    canvas.Children.Add(rec);
                }

                if (date.Length >= 7 && date.Substring(5, 2) == "01")
                {
                    var x = i * cellW;
                    canvas.Children.Add(new System.Windows.Shapes.Line
                    {
                        X1 = x, X2 = x, Y1 = barTop + barH, Y2 = barTop + barH + 4, Stroke = tickBrush, StrokeThickness = 1, IsHitTestVisible = false,
                    });
                    if (int.TryParse(date[..4], NumberStyles.Integer, CultureInfo.InvariantCulture, out var year) && year % 2 == 0)
                    {
                        var yt = new TextBlock { Text = date[..4], FontSize = 9, Foreground = yearBrush, IsHitTestVisible = false };
                        Canvas.SetLeft(yt, Math.Clamp(x - 10, 0, w - 24));
                        Canvas.SetTop(yt, barTop + barH + 4);
                        canvas.Children.Add(yt);
                    }
                }
            }

            // Latest month: outlined so it can be found at a glance.
            var mark = new System.Windows.Shapes.Rectangle
            {
                Width = Math.Max(3, cellW + 2), Height = barH + 2,
                Stroke = FrozenBrush(Color.FromRgb(0xF2, 0xF2, 0xF2)), StrokeThickness = 1, IsHitTestVisible = false,
            };
            Canvas.SetLeft(mark, Math.Min(w - Math.Max(3, cellW + 2), (hist.Count - 1) * cellW - 1));
            Canvas.SetTop(mark, barTop - 1);
            canvas.Children.Add(mark);

            // Legend row (shape + text, not colour only).
            var legend = new StackPanel { Orientation = Orientation.Horizontal, IsHitTestVisible = false };
            void Item(Brush fill, double height, string text)
            {
                legend.Children.Add(new System.Windows.Shapes.Rectangle { Width = 10, Height = height, Fill = fill, VerticalAlignment = VerticalAlignment.Center });
                legend.Children.Add(new TextBlock { Text = text, FontSize = 9.5, Foreground = yearBrush, Margin = new Thickness(5, 0, 14, 0), VerticalAlignment = VerticalAlignment.Center });
            }
            foreach (var key in RegimePhaseKeys)
                Item(FrozenBrush(RegimeColors[key], CellAlpha(key)), 10, PhaseName(key));
            if (ext.Recessions.Count > 0) Item(recBrush, 3, "NBER recession (bar above)");
            Canvas.SetLeft(legend, 0);
            Canvas.SetTop(legend, 46);
            canvas.Children.Add(legend);

            var total = hist.Count;
            var shares = string.Join(", ", RegimePhaseKeys.Select(k =>
                $"{PhaseName(k)} {(counts.TryGetValue(k, out var c) ? c : 0) * 100.0 / total:0}%"));
            System.Windows.Automation.AutomationProperties.SetName(canvas,
                $"Macro regime history {RegimeMonth(hist[0].Date)} – {RegimeMonth(hist[^1].Date)}, {total} months: {shares}.");
        }

        // Compact table (fits the 420 px asset column): one row per regime, one arrow cell per
        // asset class; the full wording + disclaimer are in each cell's tooltip ("*" = the
        // backend added a note, e.g. "led by cyclicals"). Clicking a row shows that regime's
        // typical-behaviour details below (replaces the separate phase toggles).
        private void FillRegimeTendencies(RegimeExtended ext)
        {
            var t = ext.Tendencies;
            var g = RegimeTendencyGrid;
            g.Children.Clear();
            g.RowDefinitions.Clear();
            g.ColumnDefinitions.Clear();
            if (t == null || t.Rows.Count == 0)
            {
                RegimeTendencySection.Visibility = Visibility.Collapsed;
                RegimePhaseToggles.Visibility = Visibility.Visible; // fallback selector
                return;
            }
            RegimeTendencySection.Visibility = Visibility.Visible;
            RegimePhaseToggles.Visibility = Visibility.Collapsed;
            if (!string.IsNullOrWhiteSpace(t.Title)) RegimeTendencyTitle.Text = t.Title;

            var headBrush = FrozenBrush(Color.FromRgb(0x6A, 0x6A, 0x6A));
            var upBrush = FrozenBrush(Color.FromRgb(0x8F, 0xD1, 0xB0));
            var downBrush = FrozenBrush(Color.FromRgb(0xE0, 0x95, 0x8A));
            var mixedBrush = FrozenBrush(Color.FromRgb(0x9A, 0x9A, 0x9A));
            var sepBrush = FrozenBrush(Color.FromRgb(0x26, 0x26, 0x26));
            var disclaimer = string.IsNullOrWhiteSpace(t.Disclaimer)
                ? "Historical tendencies, simplified; not a forecast, not investment advice."
                : t.Disclaimer;

            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(96) });
            for (var c = 0; c < t.Columns.Count; c++)
                g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var c = 0; c < t.Columns.Count; c++)
            {
                var name = t.Columns[c];
                var shortName = name.Length > 11 ? name[..7] + "." : name;
                var hd = new TextBlock
                {
                    Text = shortName.ToUpperInvariant(), FontSize = 9, FontWeight = FontWeights.SemiBold, Foreground = headBrush,
                    Margin = new Thickness(4, 0, 2, 4), TextWrapping = TextWrapping.Wrap,
                };
                if (shortName != name) SetTip(hd, name);
                Grid.SetColumn(hd, c + 1);
                g.Children.Add(hd);
            }

            for (var r = 0; r < t.Rows.Count; r++)
            {
                var row = t.Rows[r];
                var gridRow = r + 1;
                g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var current = row.Quadrant == ext.Quadrant;
                var selected = row.Quadrant == _regimePhase;
                var qColor = RegimeColors.TryGetValue(row.Quadrant, out var rc) ? rc : Color.FromRgb(0x8A, 0x8A, 0x8A);
                var rowLabel = string.IsNullOrEmpty(row.Label) ? PhaseName(row.Quadrant) : row.Label;

                // Row background = click target: tinted when selected, a quadrant-coloured bar on the left.
                var bg = new Border
                {
                    Background = selected ? FrozenBrush(qColor, 0x26) : Brushes.Transparent,
                    BorderBrush = selected ? FrozenBrush(qColor) : sepBrush,
                    BorderThickness = selected ? new Thickness(2, 1, 0, 0) : new Thickness(0, 1, 0, 0),
                    Cursor = System.Windows.Input.Cursors.Hand,
                };
                var quadrant = row.Quadrant;
                bg.MouseLeftButtonUp += (_, _) => SelectRegimePhase(quadrant);
                bg.MouseEnter += (_, _) => { if (quadrant != _regimePhase) bg.Background = FrozenBrush(qColor, 0x12); };
                bg.MouseLeave += (_, _) => { if (quadrant != _regimePhase) bg.Background = Brushes.Transparent; };
                SetTip(bg, $"Show typical behaviour in {rowLabel}" + (current ? " (current phase)" : ""));
                Grid.SetRow(bg, gridRow);
                Grid.SetColumnSpan(bg, t.Columns.Count + 1);
                g.Children.Add(bg);

                var label = new TextBlock
                {
                    Text = (current ? "● " : "") + rowLabel,
                    FontSize = 10.5, FontWeight = FontWeights.SemiBold, Foreground = FrozenBrush(qColor),
                    Margin = new Thickness(6, 4, 2, 4), TextWrapping = TextWrapping.Wrap, IsHitTestVisible = false,
                };
                Grid.SetRow(label, gridRow);
                g.Children.Add(label);

                for (var c = 0; c < Math.Min(t.Columns.Count, row.Cells.Count); c++)
                {
                    var (direction, text) = row.Cells[c];
                    var hasNote = !string.IsNullOrEmpty(text) && text.Contains('(');
                    var shortText = direction switch { "up" => "↑ rise", "down" => "↓ fall", "mixed" => "mixed", _ => "—" };
                    var cell = new TextBlock
                    {
                        Text = shortText + (hasNote ? "*" : ""), FontSize = 10.5,
                        Foreground = direction == "up" ? upBrush : direction == "down" ? downBrush : mixedBrush,
                        Margin = new Thickness(4, 4, 2, 4), Background = Brushes.Transparent,
                    };
                    // Cells keep their own tooltip (full wording); a click still selects the row.
                    cell.MouseLeftButtonUp += (_, _) => SelectRegimePhase(quadrant);
                    cell.Cursor = System.Windows.Input.Cursors.Hand;
                    SetTip(cell, $"{rowLabel} · {t.Columns[c]}: {(string.IsNullOrEmpty(text) ? "—" : text)}\n{disclaimer}");
                    System.Windows.Automation.AutomationProperties.SetName(cell, $"{rowLabel}, {t.Columns[c]}: {text}");
                    Grid.SetRow(cell, gridRow);
                    Grid.SetColumn(cell, c + 1);
                    g.Children.Add(cell);
                }
            }

            RegimeTendencyDisclaimer.Text = disclaimer;
        }

        private void FillRegimePhase(RegimeExtended ext)
        {
            if (ext.Phases.Count == 0)
            {
                RegimePhaseSection.Visibility = Visibility.Collapsed;
                return;
            }
            RegimePhaseSection.Visibility = Visibility.Visible;

            var key = _regimePhase;
            var current = key == ext.Quadrant;
            RegimePhaseHeader.Text = $"Typical behaviour in {PhaseName(key)}{(current ? " (current phase)" : "")}";
            RegimePhaseHeader.Foreground = RegimeColors.TryGetValue(key, out var pc)
                ? FrozenBrush(pc)
                : FrozenBrush(Color.FromRgb(0xF2, 0xF2, 0xF2));

            var g = RegimePhaseAssetsGrid;
            g.Children.Clear();
            g.RowDefinitions.Clear();
            g.ColumnDefinitions.Clear();
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(120) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            if (ext.Phases.TryGetValue(key, out var info))
            {
                RegimePhaseSummary.Text = info.Summary;
                RegimePhaseSummary.Visibility = string.IsNullOrWhiteSpace(info.Summary) ? Visibility.Collapsed : Visibility.Visible;
                var assetBrush = FrozenBrush(Color.FromRgb(0xE6, 0xE6, 0xE6));
                var tendencyBrush = FrozenBrush(Color.FromRgb(0xBF, 0xBF, 0xBF));
                var line = FrozenBrush(Color.FromRgb(0x26, 0x26, 0x26));
                for (var i = 0; i < info.Assets.Count; i++)
                {
                    var (asset, tendency) = info.Assets[i];
                    g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                    var sep = new Border { BorderBrush = line, BorderThickness = new Thickness(0, i == 0 ? 0 : 1, 0, 0) };
                    Grid.SetRow(sep, i);
                    Grid.SetColumnSpan(sep, 2);
                    g.Children.Add(sep);
                    var a = new TextBlock { Text = asset, FontSize = 10.5, FontWeight = FontWeights.SemiBold, Foreground = assetBrush, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 4, 8, 4) };
                    Grid.SetRow(a, i);
                    g.Children.Add(a);
                    var t = new TextBlock { Text = tendency, FontSize = 10.5, Foreground = tendencyBrush, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 4, 0, 4) };
                    Grid.SetRow(t, i);
                    Grid.SetColumn(t, 1);
                    g.Children.Add(t);
                }
            }
            else
            {
                RegimePhaseSummary.Text = "No typical-behaviour notes for this phase in the backend data.";
                RegimePhaseSummary.Visibility = Visibility.Visible;
            }

            RegimePhaseDisclaimer.Text = ext.Disclaimer;
            RegimePhaseDisclaimer.Visibility = string.IsNullOrWhiteSpace(ext.Disclaimer) ? Visibility.Collapsed : Visibility.Visible;
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
            private const double SparkWidth = 306; // card 336 − padding 28 − border 2
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

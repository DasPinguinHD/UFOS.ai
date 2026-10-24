using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// MR5 — indicators + chart settings (plan §4.3, SPEC "MR5 backend notes"). Indicators are computed ONLY by the
    /// backend (GET /research/symbols/{s}/indicators, the Strategy Platform DSL definitions, Q8) and sent to the page
    /// as data series (setIndicators / updateIndicators). Per-symbol chart settings (type, indicators, magnet,
    /// session) come from GET /research/chart-settings when a symbol opens and are PUT 800 ms after every change.
    /// A symbol whose settings could not be loaded is never PUT (that would overwrite what is stored); the
    /// indicators menu says so and offers Retry.
    /// Type/session precedence: a tab restored from /research/session keeps its own type/session; a tab opened
    /// now takes the symbol's saved type/session (when settings were saved). Indicators and magnet always come
    /// from the chart settings.
    /// </summary>
    public partial class ResearchView
    {
        private static readonly string[] IndicatorSources = { "close", "open", "high", "low", "hl2", "hlc3", "ohlc4" };
        private const int MaxCompares = 2; // MAX_COMPARE

        private sealed class SymbolChartSettings
        {
            public string Type = "candles";
            public List<string> Indicators = new();
            public string Magnet = "off";
            public string? Session;
            public bool Saved;
        }

        private readonly Dictionary<string, SymbolChartSettings> _chartSettings = new(StringComparer.Ordinal);
        private readonly HashSet<string> _chartSettingsLoading = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _chartSettingsErrors = new(StringComparer.Ordinal);
        private readonly HashSet<ResearchTab> _settingsApplied = new();
        private string? _indicatorMessage;
        private List<IndicatorInfoDto>? _indicatorInfos;
        private int _indicatorGeneration;
        private long? _lastIndicatorRefreshT;
        private DispatcherTimer? _settingsSaveTimer;
        private string? _settingsSaveSymbol;
        private DispatcherTimer? _indicatorRefreshTimer;
        private Popup? _indicatorsPopup;
        private StackPanel? _indicatorsBody;
        private string _smaSource = "close", _emaSource = "close", _bbSource = "close";
        private string? _indicatorsPopupMessage;
        // Compare overlay base (review H1): the first bar of the window of the last FULL indicator load, sent as
        // `cmp_base` on every compare request of that load (live refresh too), so merged values never jump. Paging
        // left with a compare active re-loads the full window instead (the base moves to the new left edge — plan §4.3
        // "% from the left edge").
        private string? _cmpBaseIso;

        private void InitIndicators()
        {
            _settingsSaveTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromMilliseconds(800) };
            _settingsSaveTimer.Tick += (_, _) => FlushChartSettingsSave();
            _indicatorRefreshTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromMilliseconds(1500) };
            _indicatorRefreshTimer.Tick += (_, _) => { _indicatorRefreshTimer.Stop(); _ = RefreshRecentIndicatorsAsync(); };
        }

        // =====================================================================================
        // Chart settings
        // =====================================================================================

        private void OnSymbolActivated(ResearchTab tab)
        {
            _indicatorInfos = null;
            _lastIndicatorRefreshT = null;
            _indicatorMessage = null;
            _indicatorsPopupMessage = null;
            UpdateBellButton(); // MR7
            if (_chartSettings.TryGetValue(tab.Symbol, out var settings)) ApplyChartSettings(tab, settings, fresh: false);
            else _ = LoadChartSettingsAsync(tab);
        }

        private async Task LoadChartSettingsAsync(ResearchTab tab)
        {
            var symbol = tab.Symbol;
            if (!_chartSettingsLoading.Add(symbol)) return;
            PlatformResult<ChartSettingsResponse> r;
            try { r = await ResearchClient.GetChartSettingsAsync(symbol); }
            finally { _chartSettingsLoading.Remove(symbol); }
            if (_shutDown) return;
            var s = r.Value?.Settings;
            if (!r.Ok || s == null)
            {
                var message = "Chart settings not loaded: " + (r.Ok ? "empty response from the backend." : r.Message) + " Indicators are off until they load.";
                _chartSettingsErrors[symbol] = message;
                if (_active?.Symbol == symbol) { _indicatorMessage = message; RenderDrawingStatus(); RenderIndicatorsPopup(); }
                return;
            }
            var settings = new SymbolChartSettings
            {
                Type = s.Type is "candles" or "line" or "area" ? s.Type : "candles",
                Indicators = s.Indicators?.Where(x => !string.IsNullOrWhiteSpace(x)).Distinct(StringComparer.Ordinal).ToList() ?? new List<string>(),
                Magnet = s.Magnet is "off" or "weak" or "strong" ? s.Magnet : "off",
                Session = s.Session is "rth" or "eth" ? s.Session : null,
                Saved = r.Value!.Saved == true,
            };
            _chartSettings[symbol] = settings;
            _chartSettingsErrors.Remove(symbol);
            if (_active != null && _active.Symbol == symbol) ApplyChartSettings(_active, settings, fresh: true);
        }

        private void ApplyChartSettings(ResearchTab tab, SymbolChartSettings settings, bool fresh)
        {
            if (!ReferenceEquals(tab, _active)) return;
            _chart.SetMagnet(settings.Magnet);
            if (fresh && settings.Saved && !tab.FromSession && _settingsApplied.Add(tab))
            {
                var reload = false;
                if (settings.Type != tab.ChartType)
                {
                    tab.ChartType = settings.Type;
                    _chart.SetChartType(settings.Type);
                }
                if (!tab.IsCrypto && settings.Session is "rth" or "eth" && settings.Session != tab.Session)
                {
                    tab.Session = settings.Session!;
                    reload = true;
                }
                SyncToolbar();
                ScheduleSessionSave();
                if (reload)
                {
                    _socket.TopicsChanged();
                    _ = LoadChartAsync(tab, withSnapshot: false); // its OnChartSeriesLoaded loads the indicators
                    RenderRail();
                    return;
                }
            }
            if (_chart.HasSeries && _chart.Symbol == tab.Symbol && _chart.Timeframe == tab.Tf) _ = LoadIndicatorsAsync(tab, _loadGeneration);
            RenderRail();
            RenderIndicatorsPopup();
        }

        /// <summary>PUT the active symbol's settings 800 ms after the last change (only once they were loaded).</summary>
        private void ScheduleChartSettingsSave()
        {
            var tab = _active;
            if (tab == null || _shutDown || !_chartSettings.TryGetValue(tab.Symbol, out var settings)) return;
            settings.Type = tab.ChartType;
            settings.Session = tab.IsCrypto ? null : tab.Session;
            settings.Magnet = _chart.Magnet;
            settings.Saved = true;
            if (_settingsSaveSymbol != null && _settingsSaveSymbol != tab.Symbol) FlushChartSettingsSave();
            _settingsSaveSymbol = tab.Symbol;
            _settingsSaveTimer?.Stop();
            _settingsSaveTimer?.Start();
        }

        private void FlushChartSettingsSave()
        {
            _settingsSaveTimer?.Stop();
            var symbol = _settingsSaveSymbol;
            _settingsSaveSymbol = null;
            if (symbol == null || !_chartSettings.TryGetValue(symbol, out var settings)) return;
            _ = SaveChartSettingsAsync(symbol, settings.Type, settings.Indicators.ToList(), settings.Magnet, settings.Session);
        }

        private async Task SaveChartSettingsAsync(string symbol, string type, List<string> indicators, string magnet, string? session)
        {
            PlatformResult<ChartSettingsResponse> r;
            try { r = await ResearchClient.PutChartSettingsAsync(symbol, type, indicators, magnet, session); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: saving chart settings failed.", ex);
                return;
            }
            if (r.Ok && r.Value?.Settings?.Indicators is { } saved && _chartSettings.TryGetValue(symbol, out var current) &&
                current.Indicators.SequenceEqual(indicators))
            {
                current.Indicators = saved.ToList(); // the backend's normalised spellings
                return;
            }
            if (!r.Ok && !_shutDown)
            {
                var message = r.Status == PlatformCallStatus.BadRequest ? r.Message : "Chart settings not saved: " + r.Message;
                if (_active?.Symbol == symbol) { _indicatorMessage = message; RenderDrawingStatus(); }
                else AppLog.Warn(LogModule, "Market Research: " + message);
            }
        }

        // =====================================================================================
        // Indicator data (backend only — the page never computes, Q8)
        // =====================================================================================

        private static bool IsSubPane(string spec) => spec.StartsWith("rsi", StringComparison.Ordinal) || spec.StartsWith("macd", StringComparison.Ordinal);

        /// <summary>The specs to request for <paramref name="tab"/>: VWAP only on intraday timeframes (the backend refuses it on 1D/1W).</summary>
        private List<string> EffectiveSpecs(SymbolChartSettings settings, ResearchTab tab)
            => settings.Indicators.Where(x => x != "vwap" || IntradayTfs.Contains(tab.Tf)).ToList();

        private int IndicatorLimit(ResearchTab tab) => Math.Clamp(_chart.BarCount, 1, IntradayTfs.Contains(tab.Tf) ? 5000 : 2000);

        private static bool HasCompare(IEnumerable<string> specs) => specs.Any(x => x.StartsWith("cmp:", StringComparison.Ordinal));

        /// <summary>ISO start of the first bar of a request for the newest <paramref name="limit"/> bars (the compare base).</summary>
        private string? CompareBaseFor(int limit)
            => _chart.BarStartMs(Math.Max(0, _chart.BarCount - limit)) is long ms ? ChartHost.ToIso(ms) : null;

        private async Task LoadIndicatorsAsync(ResearchTab tab, int generation)
        {
            if (!_chartSettings.TryGetValue(tab.Symbol, out var settings)) return; // they arrive later → ApplyChartSettings
            var specs = EffectiveSpecs(settings, tab);
            var indicatorGeneration = ++_indicatorGeneration;
            var vwapHidden = settings.Indicators.Contains("vwap") && !specs.Contains("vwap");
            if (specs.Count == 0)
            {
                _chart.ClearIndicators();
                _indicatorInfos = null;
                _indicatorMessage = vwapHidden ? "VWAP is intraday only — not shown on " + tab.Tf + "." : null;
                RenderDrawingStatus();
                RenderIndicatorsPopup();
                return;
            }
            if (!_chart.HasSeries || _chart.Symbol != tab.Symbol || _chart.BarCount == 0) return;
            var limit = IndicatorLimit(tab);
            _cmpBaseIso = HasCompare(specs) ? CompareBaseFor(limit) : null;
            PlatformResult<IndicatorsResponse> r;
            try { r = await ResearchClient.GetIndicatorsAsync(tab.Symbol, tab.Tf, tab.SessionParam, null, limit, string.Join(",", specs), _cmpBaseIso); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading indicators failed.", ex);
                r = new PlatformResult<IndicatorsResponse> { Status = PlatformCallStatus.Error, Message = "Indicators not loaded: unexpected error — see the Backend Log." };
            }
            if (_shutDown || indicatorGeneration != _indicatorGeneration || generation != _loadGeneration || !ReferenceEquals(tab, _active)) return;
            if (!r.Ok || r.Value == null)
            {
                _chart.ClearIndicators();
                _indicatorInfos = null;
                _indicatorMessage = r.Ok ? "Indicators not loaded: empty response from the backend." : WithPrefix(r);
            }
            else
            {
                var problem = _chart.SetIndicators(r.Value.Indicators, r.Value.T, r.Value.Series);
                _indicatorInfos = problem == null ? r.Value.Indicators : null;
                _indicatorMessage = problem != null ? "Indicators not shown: " + problem + "."
                    : vwapHidden ? "VWAP is intraday only — not shown on " + tab.Tf + "." : null;
            }
            RenderDrawingStatus();
            RenderIndicatorsPopup();
        }

        private static string WithPrefix<T>(PlatformResult<T> r)
            => r.Message.StartsWith("Indicators not loaded", StringComparison.Ordinal) ? r.Message : "Indicators not loaded: " + r.Message;

        /// <summary>After paging left: the indicator values of the older page (same <c>before</c> as /bars) are merged in.</summary>
        private async void OnChartPagedLeft(ResearchTab tab, int generation, string beforeIso)
        {
            try
            {
                if (_indicatorInfos == null || !_chartSettings.TryGetValue(tab.Symbol, out var settings)) return;
                var specs = EffectiveSpecs(settings, tab);
                if (specs.Count == 0) return;
                if (HasCompare(specs))
                {
                    await LoadIndicatorsAsync(tab, generation); // rebase the compare to the new left edge (whole window, one base)
                    return;
                }
                var indicatorGeneration = _indicatorGeneration;
                var r = await ResearchClient.GetIndicatorsAsync(tab.Symbol, tab.Tf, tab.SessionParam, beforeIso, null, string.Join(",", specs));
                if (_shutDown || indicatorGeneration != _indicatorGeneration || generation != _loadGeneration || !ReferenceEquals(tab, _active)) return;
                if (!r.Ok || r.Value == null)
                {
                    _indicatorMessage = "Indicators for the older candles not loaded: " + (r.Ok ? "empty response from the backend." : r.Message);
                }
                else
                {
                    var problem = _chart.UpdateIndicators(r.Value.T, r.Value.Series);
                    if (problem != null) _indicatorMessage = "Indicators for the older candles not shown: " + problem + ".";
                }
                RenderDrawingStatus();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading indicators for older candles failed.", ex);
            }
        }

        /// <summary>A live candle closed (final): refresh the recent indicator values once per bar close (debounced 1.5 s).</summary>
        private void OnLiveBarClosed(ApiBar bar)
        {
            if (_indicatorInfos == null || _indicatorInfos.Count == 0) return;
            if (!ChartHost.TryToEpochMs(bar.T, out var t) || t == _lastIndicatorRefreshT) return;
            _lastIndicatorRefreshT = t;
            _indicatorRefreshTimer?.Stop();
            _indicatorRefreshTimer?.Start();
        }

        private async Task RefreshRecentIndicatorsAsync()
        {
            var tab = _active;
            if (tab == null || _shutDown || _indicatorInfos == null || !_chartSettings.TryGetValue(tab.Symbol, out var settings)) return;
            var specs = EffectiveSpecs(settings, tab);
            if (specs.Count == 0) return;
            var generation = _loadGeneration;
            var indicatorGeneration = _indicatorGeneration;
            var r = await ResearchClient.GetIndicatorsAsync(tab.Symbol, tab.Tf, tab.SessionParam, null, Math.Min(300, IndicatorLimit(tab)), string.Join(",", specs),
                                                            HasCompare(specs) ? _cmpBaseIso : null); // same base as the full load
            if (_shutDown || indicatorGeneration != _indicatorGeneration || generation != _loadGeneration || !ReferenceEquals(tab, _active)) return;
            if (r.Ok && r.Value != null)
            {
                var problem = _chart.UpdateIndicators(r.Value.T, r.Value.Series);
                if (problem != null) { _indicatorMessage = "Indicators not updated: " + problem + "."; RenderDrawingStatus(); }
            }
            else
            {
                _indicatorMessage = "Indicators not updated: " + (r.Ok ? "empty response from the backend." : r.Message);
                RenderDrawingStatus();
            }
        }

        // =====================================================================================
        // Adding / removing indicators
        // =====================================================================================

        private void AddIndicatorSpec(string spec)
        {
            var tab = _active;
            if (tab == null) return;
            if (!_chartSettings.TryGetValue(tab.Symbol, out var settings))
            {
                SetIndicatorsPopupMessage(_chartSettingsErrors.TryGetValue(tab.Symbol, out var e) ? e : "The chart settings are still loading — try again in a moment.");
                return;
            }
            if (settings.Indicators.Contains(spec)) { SetIndicatorsPopupMessage("Already on the chart."); return; }
            if (settings.Indicators.Count >= ChartHost.MaxIndicatorSpecs) { SetIndicatorsPopupMessage("At most 8 indicators per chart — remove one first."); return; }
            if (IsSubPane(spec) && settings.Indicators.Count(IsSubPane) >= ChartHost.MaxSubPanes)
            { SetIndicatorsPopupMessage("At most 3 indicator panes (RSI, MACD) — remove one first."); return; }
            if (spec.StartsWith("cmp:", StringComparison.Ordinal) && settings.Indicators.Count(x => x.StartsWith("cmp:", StringComparison.Ordinal)) >= MaxCompares)
            { SetIndicatorsPopupMessage("At most 2 compare symbols — remove one first."); return; }
            if (spec == "vwap" && !IntradayTfs.Contains(tab.Tf))
                SetIndicatorsPopupMessage("VWAP added — it is intraday only and shows on 1m … 4H.");
            else
                SetIndicatorsPopupMessage(null);
            settings.Indicators.Add(spec);
            ScheduleChartSettingsSave();
            _ = LoadIndicatorsAsync(tab, _loadGeneration);
            RenderIndicatorsPopup();
        }

        private void RemoveIndicatorSpec(string spec)
        {
            var tab = _active;
            if (tab == null || !_chartSettings.TryGetValue(tab.Symbol, out var settings)) return;
            if (!settings.Indicators.Remove(spec)) return;
            SetIndicatorsPopupMessage(null);
            ScheduleChartSettingsSave();
            _ = LoadIndicatorsAsync(tab, _loadGeneration);
            RenderIndicatorsPopup();
        }

        /// <summary>Palette "compare" mode answer (ResearchView.Workspace.cs ExecutePaletteSelection).</summary>
        private void AddCompareIndicator(string symbol, string? assetClass)
        {
            var tab = _active;
            if (tab == null) return;
            var sym = symbol.ToUpperInvariant();
            var otherClass = assetClass is "equity" or "crypto" ? assetClass : AssetClassOf(sym);
            if (sym == tab.Symbol) { _indicatorMessage = "Compare needs another symbol than the chart's own."; RenderDrawingStatus(); return; }
            if (otherClass != tab.AssetClass)
            {
                _indicatorMessage = "Compare works within one asset class (stocks/ETFs with stocks/ETFs, crypto with crypto).";
                RenderDrawingStatus();
                return;
            }
            AddIndicatorSpec("cmp:" + sym);
            if (_indicatorsPopupMessage != null) { _indicatorMessage = _indicatorsPopupMessage; RenderDrawingStatus(); }
        }

        // =====================================================================================
        // Indicators menu (popup)
        // =====================================================================================

        private void IndicatorsButton_Click(object sender, RoutedEventArgs e)
        {
            if (_active == null) return;
            if (_indicatorsPopup == null)
            {
                _indicatorsBody = new StackPanel { Width = 300 };
                _indicatorsPopup = MakePopup(new ScrollViewer { Content = _indicatorsBody, MaxHeight = 520, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false }, "Indicators");
                _indicatorsPopup.PlacementTarget = IndicatorsButton;
                _indicatorsPopup.Placement = PlacementMode.Bottom;
            }
            if (_active != null && !_chartSettings.ContainsKey(_active.Symbol) && !_chartSettingsLoading.Contains(_active.Symbol))
                _ = LoadChartSettingsAsync(_active); // retry after a failed load
            RenderIndicatorsPopup();
            _indicatorsPopup.IsOpen = true;
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() =>
                _indicatorsBody?.Children.OfType<FrameworkElement>().FirstOrDefault(x => x is Button || x is Grid)?.MoveFocus(new TraversalRequest(FocusNavigationDirection.First))));
        }

        private void CloseIndicatorsPopup()
        {
            if (_indicatorsPopup != null) _indicatorsPopup.IsOpen = false;
        }

        private void SetIndicatorsPopupMessage(string? text)
        {
            _indicatorsPopupMessage = text;
            RenderIndicatorsPopup();
        }

        private void RenderIndicatorsPopup()
        {
            if (_indicatorsBody == null || _indicatorsPopup == null) return;
            _indicatorsBody.Children.Clear();
            var tab = _active;
            if (tab == null) { _indicatorsPopup.IsOpen = false; return; }
            _chartSettings.TryGetValue(tab.Symbol, out var settings);
            var specs = settings?.Indicators ?? new List<string>();
            var panes = specs.Count(IsSubPane);
            _indicatorsBody.Children.Add(new TextBlock
            {
                Text = "INDICATORS · " + tab.Symbol + " · " + specs.Count + "/" + ChartHost.MaxIndicatorSpecs + " · PANES " + panes + "/" + ChartHost.MaxSubPanes,
                Style = ViewStyle("CaptionStyle"),
                Margin = new Thickness(0, 0, 0, 6),
            });
            if (settings == null)
            {
                var loading = _chartSettingsLoading.Contains(tab.Symbol);
                _indicatorsBody.Children.Add(new TextBlock
                {
                    Text = loading ? "Loading the chart settings…" : (_chartSettingsErrors.TryGetValue(tab.Symbol, out var err) ? err : "Chart settings not loaded."),
                    FontSize = 11, Foreground = Token("SecondaryForegroundBrush"), TextWrapping = TextWrapping.Wrap,
                });
                if (!loading)
                {
                    var retry = MakeTextButton("Retry", "Load the chart settings again", (_, _) => { _ = LoadChartSettingsAsync(tab); RenderIndicatorsPopup(); });
                    retry.HorizontalAlignment = HorizontalAlignment.Left;
                    retry.Margin = new Thickness(0, 6, 0, 0);
                    _indicatorsBody.Children.Add(retry);
                }
                return;
            }

            if (specs.Count == 0)
                _indicatorsBody.Children.Add(new TextBlock { Text = "No indicators on this chart. Volume is always shown.", FontSize = 11, Foreground = Token("MutedForegroundBrush"), TextWrapping = TextWrapping.Wrap });
            foreach (var spec in specs.ToList())
            {
                var info = _indicatorInfos?.FirstOrDefault(i => i.Key == spec);
                var label = info?.Label ?? spec;
                var hidden = spec == "vwap" && !IntradayTfs.Contains(tab.Tf);
                var row = new Grid { Margin = new Thickness(0, 0, 0, 2) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var text = new TextBlock
                {
                    Text = label + (IsSubPane(spec) ? "  · pane" : "") + (hidden ? "  · intraday only" : ""),
                    FontFamily = new FontFamily("Consolas"), FontSize = 11, VerticalAlignment = VerticalAlignment.Center,
                    Foreground = Token(hidden ? "MutedForegroundBrush" : "PrimaryForegroundBrush"),
                    TextTrimming = TextTrimming.CharacterEllipsis,
                    ToolTip = (info?.Note != null ? label + "\n" + info.Note : label) + "\nComputed by the backend (Strategy Platform definitions).",
                };
                var remove = RowButton("×", "Remove " + label, "Remove " + label, () => RemoveIndicatorSpec(spec));
                Grid.SetColumn(remove, 1);
                row.Children.Add(text);
                row.Children.Add(remove);
                _indicatorsBody.Children.Add(row);
            }
            _indicatorsBody.Children.Add(new Border { Height = 1, Margin = new Thickness(0, 6, 0, 6), Background = Token("HairlineBrush") });
            _indicatorsBody.Children.Add(new TextBlock { Text = "ADD", Style = ViewStyle("CaptionStyle"), Margin = new Thickness(0, 0, 0, 4) });

            // SMA / EMA: period + source
            AddPeriodSourceRow("SMA", "Simple moving average", "20", 1, 500, () => _smaSource, v => _smaSource = v, (n, src) => "sma:" + n + ":" + src);
            AddPeriodSourceRow("EMA", "Exponential moving average", "20", 2, 500, () => _emaSource, v => _emaSource = v, (n, src) => "ema:" + n + ":" + src);

            // Bollinger: n, k, source
            {
                var n = NumberBox("20", "Bollinger period, 2 to 500");
                var k = NumberBox("2", "Bollinger band width k, 0.1 to 10");
                var src = SourceButton(() => _bbSource, v => _bbSource = v, "Bollinger source");
                AddIndicatorRow("BOLL", "Bollinger Bands (sample standard deviation, n − 1)", new UIElement[] { Wrap(n), SmallLabel("k"), Wrap(k), src }, () =>
                {
                    if (!TryInt(n.Text, 2, 500, out var period)) { SetIndicatorsPopupMessage("Bollinger period: a whole number from 2 to 500."); return; }
                    if (!double.TryParse(k.Text.Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out var kv) || !double.IsFinite(kv) || kv < 0.1 || kv > 10)
                    { SetIndicatorsPopupMessage("Bollinger k: a number from 0.1 to 10."); return; }
                    AddIndicatorSpec("bb:" + period + ":" + kv.ToString("G", CultureInfo.InvariantCulture) + ":" + _bbSource);
                });
            }
            // RSI
            {
                var n = NumberBox("14", "RSI period, 2 to 100");
                AddIndicatorRow("RSI", "Relative strength index (Wilder) — own pane", new UIElement[] { Wrap(n) }, () =>
                {
                    if (!TryInt(n.Text, 2, 100, out var period)) { SetIndicatorsPopupMessage("RSI period: a whole number from 2 to 100."); return; }
                    AddIndicatorSpec("rsi:" + period);
                });
            }
            // MACD
            {
                var f = NumberBox("12", "MACD fast period");
                var s = NumberBox("26", "MACD slow period");
                var g = NumberBox("9", "MACD signal period");
                AddIndicatorRow("MACD", "MACD (EMA fast − EMA slow, signal, histogram) — own pane", new UIElement[] { Wrap(f), Wrap(s), Wrap(g) }, () =>
                {
                    if (!TryInt(f.Text, 2, 200, out var fast) || !TryInt(s.Text, 2, 200, out var slow) || !TryInt(g.Text, 2, 200, out var signal))
                    { SetIndicatorsPopupMessage("MACD periods: whole numbers from 2 to 200."); return; }
                    if (fast >= slow) { SetIndicatorsPopupMessage("MACD: the fast period must be smaller than the slow one."); return; }
                    AddIndicatorSpec("macd:" + fast + ":" + slow + ":" + signal);
                });
            }
            // VWAP (intraday only)
            {
                var intraday = IntradayTfs.Contains(tab.Tf);
                AddIndicatorRow("VWAP", intraday ? "Session VWAP of the chart's bars" : "Session VWAP — intraday only (1m … 4H)", Array.Empty<UIElement>(), () => AddIndicatorSpec("vwap"));
            }
            // Compare (palette)
            {
                var choose = MakeTextButton("Choose symbol…", "Choose a symbol to compare with (opens the search)", (_, _) =>
                {
                    CloseIndicatorsPopup();
                    OpenPalette(PaletteMode.Compare, null);
                });
                choose.ToolTip = "Second symbol in % from the left edge (same asset class, at most 2)";
                var row = new DockPanel { Margin = new Thickness(0, 2, 0, 2) };
                var label = SmallLabel("CMP");
                label.Width = 44;
                row.Children.Add(label);
                row.Children.Add(choose);
                choose.HorizontalAlignment = HorizontalAlignment.Left;
                _indicatorsBody.Children.Add(row);
            }

            if (_indicatorsPopupMessage != null)
            {
                var msg = new TextBlock { Text = _indicatorsPopupMessage, FontSize = 10.5, Foreground = Token("CautionBrush"), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 0) };
                AutomationProperties.SetLiveSetting(msg, AutomationLiveSetting.Assertive);
                AutomationProperties.SetName(msg, _indicatorsPopupMessage);
                _indicatorsBody.Children.Add(msg);
            }
            _indicatorsBody.Children.Add(new TextBlock
            {
                Text = "Values come from the backend (the same definitions as the Strategy Platform's DSL). Max 8 indicators, 3 panes.",
                FontSize = 9.5, Foreground = Token("QuietForegroundBrush"), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0),
            });
        }

        private void AddPeriodSourceRow(string caption, string description, string defaultPeriod, int min, int max,
                                        Func<string> getSource, Action<string> setSource, Func<int, string, string> spec)
        {
            var n = NumberBox(defaultPeriod, caption + " period, " + min + " to " + max);
            var src = SourceButton(getSource, setSource, caption + " source");
            AddIndicatorRow(caption, description, new UIElement[] { Wrap(n), src }, () =>
            {
                if (!TryInt(n.Text, min, max, out var period)) { SetIndicatorsPopupMessage(caption + " period: a whole number from " + min + " to " + max + "."); return; }
                AddIndicatorSpec(spec(period, getSource()));
            });
        }

        private void AddIndicatorRow(string caption, string description, UIElement[] inputs, Action add)
        {
            var row = new DockPanel { Margin = new Thickness(0, 2, 0, 2), ToolTip = description };
            var label = SmallLabel(caption);
            label.Width = 44;
            row.Children.Add(label);
            var addButton = MakeTextButton("Add", "Add " + description, (_, _) => add());
            addButton.Padding = new Thickness(8, 1, 8, 1);
            DockPanel.SetDock(addButton, Dock.Right);
            row.Children.Add(addButton);
            var inputsPanel = new StackPanel { Orientation = Orientation.Horizontal };
            foreach (var input in inputs)
            {
                inputsPanel.Children.Add(input);
                // Enter in a number box adds the indicator (keyboard path).
                if (input is Border { Child: TextBox box }) box.KeyDown += (_, e) => { if (e.Key == Key.Enter) { e.Handled = true; add(); } };
            }
            row.Children.Add(inputsPanel);
            _indicatorsBody!.Children.Add(row);
        }

        private TextBlock SmallLabel(string text) => new()
        {
            Text = text, FontFamily = new FontFamily("Consolas"), FontSize = 10.5, Foreground = Token("SecondaryForegroundBrush"),
            VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 4, 0),
        };

        private TextBox NumberBox(string value, string name)
        {
            var box = new TextBox { Text = value, Width = 34, MaxLength = 5, Style = ViewStyle("InputBoxStyle"), FontFamily = new FontFamily("Consolas"), FontSize = 11 };
            AutomationProperties.SetName(box, name);
            return box;
        }

        private Border Wrap(TextBox box) => new() { Style = ViewStyle("InputBorderStyle"), Child = box, Margin = new Thickness(0, 0, 4, 0), Padding = new Thickness(4, 1, 4, 1) };

        private Button SourceButton(Func<string> get, Action<string> set, string name)
        {
            var b = new Button { Content = get() + " ▾", FontFamily = new FontFamily("Consolas"), FontSize = 10.5, Style = ViewStyle("IconButtonStyle"), Margin = new Thickness(0, 0, 4, 0) };
            void Label()
            {
                b.Content = get() + " ▾";
                b.ToolTip = "Source: " + get() + " — click for the next (close, open, high, low, hl2, hlc3, ohlc4)";
                AutomationProperties.SetName(b, name + " " + get() + ", click to change");
            }
            Label();
            b.Click += (_, _) =>
            {
                var i = Array.IndexOf(IndicatorSources, get());
                set(IndicatorSources[(i + 1) % IndicatorSources.Length]);
                Label();
            };
            return b;
        }

        private static bool TryInt(string text, int min, int max, out int value)
            => int.TryParse(text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out value) && value >= min && value <= max;
    }
}

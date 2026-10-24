using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// MR4 symbol workspace (SPEC §10 MR4 + "MR4 backend notes", plan §1.2 / §2.2 / §5.4): the full symbol header,
    /// the key-stats column (open, VWAP, avg volume, exchange, performance tiles, 52-week range), tabs with session
    /// restore (GET/PUT /research/session), recent symbols (POST /research/recent) and the Ctrl+K command palette
    /// (GET /research/palette). Watchlists live in ResearchView.Watchlists.cs.
    ///
    /// Number rules: Consolas; every number's feed (+ as-of time, or the reason when it is missing) in its tooltip;
    /// direction always as sign + ▲/▼ (never colour alone); a missing value is "—" with the backend's reason — no
    /// default, no older value shown as current (the live price is greyed and marked "not live" while the live
    /// connection is down).
    /// </summary>
    public partial class ResearchView
    {
        private enum LoadState { NotLoaded, Loading, Loaded, Failed }

        private static readonly TimeSpan StatsRefreshInterval = TimeSpan.FromMinutes(5);
        private static readonly TimeSpan WorkspaceRetrySpacing = TimeSpan.FromSeconds(30);
        private static readonly Regex CryptoSymbol = new(@"^[A-Z0-9]{2,10}-USD$", RegexOptions.CultureInvariant); // core/instruments CRYPTO_RE
        private static readonly HashSet<string> ValidTfs = new(StringComparer.Ordinal) { "1m", "5m", "15m", "1H", "4H", "1D", "1W" };
        private static readonly HashSet<string> ValidChartTypes = new(StringComparer.Ordinal) { "candles", "line", "area" };
        private static readonly string[] PerformanceKeys = { "1W", "1M", "3M", "6M", "YTD", "1Y" };
        private static readonly TimeZoneInfo? NewYorkZone = FindNewYorkZone();
        private const double SidebarWidth = 240;

        // ---------- header / key stats ----------
        private SymbolSnapshot? _snapshot;
        private string? _snapshotError;
        private bool _snapshotRefreshing;
        private bool _lastTradeLive;          // _lastTrade came from the live socket (not from the snapshot)
        private string? _lastTradeTs;         // its trade time (UTC ISO)
        private SymbolStats? _stats;
        private string? _statsError;
        private DateTime? _statsLoadedUtc;
        private ResearchTab? _statsInFlightFor;

        // ---------- session restore ----------
        private LoadState _sessionLoad = LoadState.NotLoaded;
        private DateTime _sessionAttemptUtc = DateTime.MinValue;
        private bool _sessionDirty;           // the user changed tabs/sidebar since the app started
        private bool _applyingSession;        // restoring: no save, no recent post
        private bool _sessionSaving;
        private bool _sessionSaveAgain;
        private bool _workspaceMessageIsSaveError;
        private DispatcherTimer? _sessionSaveTimer;
        private bool _sidebarCollapsed;
        private string _section = "symbol";

        // ---------- command palette ----------
        private enum PaletteMode { Open, AddToList, Compare } // Compare = MR5 indicator "compare symbol"
        private PaletteMode _paletteMode;
        private string? _paletteListId;
        private readonly List<PaletteItem> _paletteItems = new();
        private int _paletteIndex = -1;
        private int _paletteGeneration;
        private string? _paletteItemsQuery;   // the (trimmed) query the shown items belong to; "" = recent
        private bool _paletteEnterPending;
        private DispatcherTimer? _paletteDebounce;
        private IInputElement? _paletteReturnFocus;

        // =====================================================================================
        // Keyboard: Ctrl+K anywhere in the view (the chart page forwards no keys; click outside the chart first)
        // =====================================================================================

        private void UserControl_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            if (HandleChartKeys(e)) return; // MR5: Alt+T/H/F/R, Esc (drawing tools), Ctrl+T/W/Tab (ResearchView.Drawings.cs)
            var key = e.Key == Key.System ? e.SystemKey : e.Key;
            if (key == Key.K && (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control
                             && (Keyboard.Modifiers & (ModifierKeys.Alt | ModifierKeys.Shift)) == 0)
            {
                e.Handled = true;
                if (PaletteOverlay.Visibility == Visibility.Visible)
                {
                    PaletteBox.Focus();
                    PaletteBox.SelectAll();
                }
                else
                {
                    OpenPalette(PaletteMode.Open, null);
                }
            }
        }

        private void SearchButton_Click(object sender, RoutedEventArgs e) => OpenPalette(PaletteMode.Open, null);

        private void NewTabButton_Click(object sender, RoutedEventArgs e) => OpenPalette(PaletteMode.Open, null);

        // =====================================================================================
        // Opening symbols (palette, watchlist rows) — tabs
        // =====================================================================================

        /// <summary>Opens <paramref name="symbol"/> in a tab (or switches to its tab) — from the palette or a watchlist row.</summary>
        private void OpenSymbol(string symbol, string? name, string? assetClass, string? kind, string? exchange)
        {
            var sym = symbol.Trim().ToUpperInvariant().Replace('/', '-');
            if (!PlatformSymbol.IsMatch(sym))
            {
                ShowWorkspaceMessage("\"" + ClipText(symbol, 32) + "\" is not a valid symbol.");
                return;
            }
            NavSymbol.IsChecked = true;
            var existing = _tabs.FirstOrDefault(t => t.Symbol == sym);
            if (existing != null)
            {
                if (!ReferenceEquals(existing, _active)) Activate(existing);
                else PostRecent(sym);
                return;
            }
            if (_tabs.Count >= MaxTabs)
            {
                ShowWorkspaceMessage("At most " + MaxTabs + " symbols can be open — close a tab first.");
                return;
            }
            var tab = new ResearchTab
            {
                Symbol = sym,
                Name = name,
                AssetClass = assetClass is "equity" or "crypto" ? assetClass : AssetClassOf(sym),
                Kind = kind,
                Exchange = exchange,
            };
            _tabs.Add(tab);
            Activate(tab);
        }

        /// <summary>core/instruments.asset_class: crypto = <c>BASE-USD</c>, everything else equity (symbol syntax, not a guess).</summary>
        private static string AssetClassOf(string symbol) => CryptoSymbol.IsMatch(symbol) ? "crypto" : "equity";

        /// <summary>Clears the header/stats state of the previous symbol (called by Activate / CloseTab).</summary>
        private void ResetSymbolDetails()
        {
            _snapshot = null;
            _snapshotError = null;
            _lastTradeLive = false;
            _lastTradeTs = null;
            _stats = null;
            _statsError = null;
            _statsLoadedUtc = null;
        }

        /// <summary>POST /research/recent (palette "recent" list) — only for symbols the user opened, not for a restore.</summary>
        private void PostRecent(string symbol)
        {
            if (_applyingSession || _shutDown) return;
            _ = PostRecentAsync(symbol);
        }

        private static async Task PostRecentAsync(string symbol)
        {
            try
            {
                var result = await ResearchClient.AddRecentAsync(symbol);
                if (!result.Ok) AppLog.Warn(LogModule, "Market Research: recent symbol not stored (" + symbol + "): " + result.Message);
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: storing a recent symbol failed.", ex);
            }
        }

        private void ShowWorkspaceMessage(string text, bool saveError = false)
        {
            _workspaceMessageIsSaveError = saveError;
            WorkspaceMessageText.Text = text;
            WorkspaceMessageText.Visibility = Visibility.Visible;
            AutomationProperties.SetName(WorkspaceMessageText, text);
        }

        private void HideWorkspaceMessage()
        {
            _workspaceMessageIsSaveError = false;
            WorkspaceMessageText.Text = "";
            WorkspaceMessageText.Visibility = Visibility.Collapsed;
        }

        // =====================================================================================
        // Session restore / save (GET / PUT /research/session)
        // =====================================================================================

        /// <summary>First show: restore the tabs and load the watchlists (both retried while they failed, ≥ 30 s apart).</summary>
        private void EnsureWorkspaceLoaded()
        {
            if (_shutDown || !IsShown) return;
            var now = DateTime.UtcNow;
            if (_sessionLoad == LoadState.NotLoaded ||
                (_sessionLoad == LoadState.Failed && !_sessionDirty && _tabs.Count == 0 && now - _sessionAttemptUtc >= WorkspaceRetrySpacing))
                _ = RestoreSessionAsync();
            if (_listsLoad == LoadState.NotLoaded ||
                (_listsLoad == LoadState.Failed && now - _listsAttemptUtc >= WorkspaceRetrySpacing))
                _ = LoadWatchlistsAsync();
        }

        private async Task RestoreSessionAsync()
        {
            if (_sessionLoad == LoadState.Loading) return;
            _sessionLoad = LoadState.Loading;
            _sessionAttemptUtc = DateTime.UtcNow;
            PlatformResultHolder<SessionState> holder;
            try { holder = new PlatformResultHolder<SessionState>(await ResearchClient.GetSessionAsync()); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: restoring the tabs failed.", ex);
                _sessionLoad = LoadState.Failed;
                ShowWorkspaceMessage("Previous tabs not restored — see the Backend Log.");
                return;
            }
            if (_shutDown) return;
            var result = holder.Result;
            if (!result.Ok || result.Value == null)
            {
                _sessionLoad = LoadState.Failed;
                ShowWorkspaceMessage("Previous tabs not restored: " + (result.Ok ? "empty response from the backend." : result.Message) +
                                     " (retried automatically once the backend answers).");
                return;
            }
            _sessionLoad = LoadState.Loaded;
            if (!_workspaceMessageIsSaveError && WorkspaceMessageText.Text.StartsWith("Previous tabs not restored", StringComparison.Ordinal))
                HideWorkspaceMessage();
            if (!string.IsNullOrWhiteSpace(result.Value.Note)) ShowWorkspaceMessage("Previous tabs not restored: " + result.Value.Note);
            // The user already opened something meanwhile: keep that (it is saved), do not overwrite it with the old state.
            if (_sessionDirty || _tabs.Count > 0) return;
            ApplySession(result.Value);
        }

        private void ApplySession(SessionState state)
        {
            _applyingSession = true;
            try
            {
                var skipped = 0;
                foreach (var saved in state.Tabs ?? new List<SessionTab>())
                {
                    if (_tabs.Count >= MaxTabs) { skipped++; continue; }
                    var sym = saved.Symbol?.Trim().ToUpperInvariant();
                    if (sym == null || !PlatformSymbol.IsMatch(sym) || saved.Tf == null || !ValidTfs.Contains(saved.Tf))
                    {
                        skipped++;
                        continue;
                    }
                    var tab = new ResearchTab
                    {
                        Symbol = sym,
                        AssetClass = AssetClassOf(sym),
                        Tf = saved.Tf,
                        ChartType = saved.Type != null && ValidChartTypes.Contains(saved.Type) ? saved.Type : "candles",
                        FromSession = true,
                    };
                    if (!tab.IsCrypto) tab.Session = saved.Session == "eth" ? "eth" : "rth";
                    _tabs.Add(tab);
                }
                var sidebar = state.Sidebar;
                SetSidebarCollapsed(sidebar?.Collapsed == true);
                var section = sidebar?.Section;
                if (_tabs.Count > 0)
                {
                    var index = state.Active is int a && a >= 0 && a < _tabs.Count ? a : 0;
                    Activate(_tabs[index]); // selects SYMBOL
                }
                else
                {
                    RenderTabs();
                }
                // The saved section wins (Activate selected SYMBOL); MR8 sections (portfolio, macro) do not exist yet.
                switch (section)
                {
                    case "overview": NavOverview.IsChecked = true; break;
                    case "alerts": NavAlerts.IsChecked = true; break;
                    case "portfolio": NavPortfolio.IsChecked = true; break; // MR8
                    case "macro": NavMacro.IsChecked = true; break;         // MR8
                    case "symbol": NavSymbol.IsChecked = true; break;
                }
                if (skipped > 0)
                {
                    AppLog.Warn(LogModule, "Market Research: " + skipped + " saved tab(s) were not restored (invalid or over the limit).");
                    ShowWorkspaceMessage(skipped + " saved tab(s) could not be restored (invalid entry or more than " + MaxTabs + ").");
                }
            }
            finally
            {
                _applyingSession = false;
            }
        }

        /// <summary>Tabs / active tab / timeframe / chart type / session / sidebar changed: PUT the session after 1 s.</summary>
        private void ScheduleSessionSave()
        {
            if (_applyingSession || _shutDown) return;
            _sessionDirty = true;
            if (_sessionSaveTimer == null)
            {
                _sessionSaveTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromSeconds(1) };
                _sessionSaveTimer.Tick += (_, _) =>
                {
                    if (_sessionLoad == LoadState.Loading) return; // wait for the restore answer (keeps ticking)
                    _sessionSaveTimer!.Stop();
                    _ = SaveSessionAsync();
                };
            }
            _sessionSaveTimer.Stop();
            _sessionSaveTimer.Start();
        }

        /// <summary>The Hedge Fund window closes: send a pending save at once (fire and forget).</summary>
        private void FlushSessionSave()
        {
            if (_sessionSaveTimer is { IsEnabled: true })
            {
                _sessionSaveTimer.Stop();
                _ = SaveSessionAsync(force: true);
            }
        }

        private object BuildSessionBody()
        {
            var tabs = _tabs.Select(t => new Dictionary<string, object>
            {
                ["symbol"] = t.Symbol,
                ["tf"] = t.Tf,
                ["type"] = t.ChartType,
                ["session"] = t.TopicSession,
            }).ToList();
            int? active = null;
            if (tabs.Count > 0)
            {
                var index = _active == null ? -1 : _tabs.IndexOf(_active);
                active = index >= 0 ? index : 0;
            }
            return new Dictionary<string, object?>
            {
                ["tabs"] = tabs,
                ["active"] = active,
                ["sidebar"] = new Dictionary<string, object> { ["collapsed"] = _sidebarCollapsed, ["section"] = _section },
            };
        }

        private async Task SaveSessionAsync(bool force = false)
        {
            if (_shutDown && !force) return;
            if (_sessionSaving)
            {
                _sessionSaveAgain = true;
                return;
            }
            _sessionSaving = true;
            try
            {
                do
                {
                    _sessionSaveAgain = false;
                    var result = await ResearchClient.PutSessionAsync(BuildSessionBody());
                    if (_shutDown) return;
                    if (!result.Ok) ShowWorkspaceMessage("Open tabs not saved: " + result.Message, saveError: true);
                    else if (_workspaceMessageIsSaveError) HideWorkspaceMessage();
                } while (_sessionSaveAgain && !_shutDown);
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: saving the open tabs failed.", ex);
                if (!_shutDown) ShowWorkspaceMessage("Open tabs not saved — see the Backend Log.", saveError: true);
            }
            finally
            {
                _sessionSaving = false;
            }
        }

        // ---------- sidebar collapse ----------

        private void SidebarToggleButton_Click(object sender, RoutedEventArgs e)
        {
            SetSidebarCollapsed(!_sidebarCollapsed);
            ScheduleSessionSave();
        }

        private void SetSidebarCollapsed(bool collapsed)
        {
            _sidebarCollapsed = collapsed;
            SidebarColumn.Width = new GridLength(collapsed ? 0 : SidebarWidth);
            SidebarBorder.Visibility = collapsed ? Visibility.Collapsed : Visibility.Visible;
            MainArea.Margin = collapsed ? new Thickness(0) : new Thickness(12, 0, 0, 0);
            var text = collapsed ? "Show the sidebar (search, watchlists, live status)" : "Hide the sidebar";
            SidebarToggleButton.ToolTip = text;
            AutomationProperties.SetName(SidebarToggleButton, text);
        }

        // =====================================================================================
        // Header (snapshot §7.4) — identity, market chip, last price, change, day range, volume, prev close
        // =====================================================================================

        /// <summary>
        /// Renders the symbol header. A non-null <paramref name="snapshot"/> replaces the stored one (REST answer);
        /// null keeps it (timeframe switch) — a failed snapshot is cleared by the caller with its reason.
        /// </summary>
        private void RenderHeader(SymbolSnapshot? snapshot)
        {
            if (snapshot != null)
            {
                _snapshot = snapshot;
                _snapshotError = null;
            }
            var tab = _active;
            if (tab == null)
            {
                HeaderCaptionText.Text = "SYMBOL";
                HeaderTitleText.Text = "No symbol open";
                HeaderHintText.Text = "Press Ctrl+K (or ⌕ Search) to open a symbol.";
                HeaderHintText.Visibility = Visibility.Visible;
                LogoBox.Visibility = Visibility.Collapsed;
                KindChip.Visibility = Visibility.Collapsed;
                MarketChip.Visibility = Visibility.Collapsed;
                LiveChip.Visibility = Visibility.Collapsed;
                PriceRow.Visibility = Visibility.Collapsed;
                HeaderStatsPanel.Visibility = Visibility.Collapsed;
                RenderStats();
                RenderStar();
                NewsTrackActive(); // MR6: news card + header logo follow the active tab
                RenderOrderButtons(); // MR8: Buy/Sell hand-over
                return;
            }
            var s = _snapshot;
            if (s != null)
            {
                // Identity details for tabs restored from the session (symbol only) / opened from a watchlist.
                if (!string.IsNullOrWhiteSpace(s.Name)) tab.Name = s.Name;
                if (!string.IsNullOrWhiteSpace(s.Exchange)) tab.Exchange = s.Exchange;
                if (!string.IsNullOrWhiteSpace(s.Kind)) tab.Kind = s.Kind;
            }
            var exchange = tab.Exchange;
            HeaderCaptionText.Text = tab.Symbol + (string.IsNullOrWhiteSpace(exchange) ? "" : " · " + exchange!.ToUpperInvariant());
            // Asset names are untrusted external text: plain TextBlock only (SPEC §9).
            HeaderTitleText.Text = tab.Name ?? tab.Symbol;

            LogoText.Text = Initials(tab.Symbol);
            AutomationProperties.SetName(LogoBox, "Logo placeholder: " + LogoText.Text);
            LogoBox.Visibility = Visibility.Visible;

            var kind = KindLabel(tab);
            KindChipText.Text = kind;
            KindChip.ToolTip = "Asset class: " + (tab.IsCrypto ? "crypto (Alpaca coins)" : "US equity") + " — " + kind.ToLowerInvariant() +
                               " (Alpaca asset list).";
            AutomationProperties.SetName(KindChip, "Asset class " + kind);
            KindChip.Visibility = Visibility.Visible;

            RenderMarketChip();
            if (s == null)
            {
                PriceRow.Visibility = Visibility.Collapsed;
                HeaderStatsPanel.Visibility = Visibility.Collapsed;
                HeaderHintText.Text = _snapshotError != null ? "Price header unavailable: " + _snapshotError : "Loading price…";
                HeaderHintText.Visibility = Visibility.Visible;
            }
            else
            {
                PriceRow.Visibility = Visibility.Visible;
                HeaderStatsPanel.Visibility = Visibility.Visible;
            }
            RenderLiveState(); // live chip + RenderPriceValues
            RenderStats();
            RenderStar();
            NewsTrackActive(); // MR6: news card + header logo follow the active tab
            RenderOrderButtons(); // MR8: Buy/Sell hand-over
        }

        /// <summary>The live state of the active symbol as the price header sees it.</summary>
        private bool ActiveIsLive => _socket.State == ResearchSocketState.Connected && _symbolLive == true;

        /// <summary>Why the price is not live (for tooltips), or null.</summary>
        private string? NotLiveReason()
        {
            if (_socket.State != ResearchSocketState.Connected) return _socket.DescribeState();
            if (_symbolLive == false) return _symbolReason ?? "The backend reports this symbol as not live.";
            if (_symbolLive == null) return "Waiting for the live status of this symbol.";
            return null;
        }

        /// <summary>Last price, change vs previous close, day range, volume, previous close (called by RenderLiveState).</summary>
        private void RenderPriceValues()
        {
            var tab = _active;
            var s = _snapshot;
            if (tab == null || s == null) return;
            var live = ActiveIsLive;
            var notLive = NotLiveReason();
            var last = _lastTrade ?? s.Last?.Value;
            var bright = new SolidColorBrush(Color.FromRgb(0xE6, 0xE6, 0xE6));
            var muted = Token("MutedForegroundBrush");

            // ----- last price -----
            LastPriceText.Text = last is double l ? FormatPrice(l) : "—";
            LastPriceText.Foreground = live && last != null ? bright : muted;
            string lastTip;
            if (_lastTradeLive && last != null)
            {
                lastTip = "Last trade · " + (tab.IsCrypto ? "Alpaca crypto · real time" : "IEX · real time") + " (live stream)" +
                          (LocalDateTime(_lastTradeTs) is { } at ? "\nAt " + at + " (local time)" : "");
            }
            else
            {
                lastTip = NumTip("Last trade", s.Last);
            }
            if (notLive != null) lastTip += "\n\nNot live: " + notLive + (last != null ? " The value is the last one received — not current." : "");
            LastPriceText.ToolTip = lastTip;
            AutomationProperties.SetName(LastPriceText, "Last price " + LastPriceText.Text + (live ? "" : ", not live"));

            // ----- change vs previous close (§7.4: last − prev close; % of prev close) -----
            var prev = s.PrevClose?.Value;
            if (last is double lv && prev is double pv && pv > 0)
            {
                var change = lv - pv;
                var pct = change / pv * 100.0;
                ChangeText.Text = Arrow(change) + " " + Signed(change);
                ChangePctText.Text = SignedPct(pct);
                var brush = live ? DirectionBrush(change) : muted;
                ChangeText.Foreground = brush;
                ChangePctText.Foreground = brush;
                var tip = "Change vs the previous close (computed: last trade − previous close; % of the previous close).\n" +
                          NumTip("Previous close", s.PrevClose) + "\nDirection: " + DirectionWord(change) + ".";
                if (notLive != null) tip += "\n\nNot live: " + notLive;
                ChangeText.ToolTip = tip;
                ChangePctText.ToolTip = tip;
                AutomationProperties.SetName(ChangeText, "Change " + DirectionWord(change) + " " + FormatPrice(Math.Abs(change)) +
                                                         ", " + Math.Abs(pct).ToString("0.00", CultureInfo.InvariantCulture) + " percent");
            }
            else
            {
                ChangeText.Text = "—";
                ChangePctText.Text = "";
                ChangeText.Foreground = muted;
                var reason = FirstReason(s.Change, s.PrevClose, s.Last) ?? "Change unavailable (no last trade or previous close).";
                ChangeText.ToolTip = "Change vs the previous close: not available — " + reason;
                ChangePctText.ToolTip = null;
                AutomationProperties.SetName(ChangeText, "Change not available: " + reason);
            }
            ChangeSuffixText.Text = SessionSuffix(tab) + (live ? "" : " · not live");

            // ----- day range (marker = last price) -----
            var low = s.DayLow?.Value;
            var high = s.DayHigh?.Value;
            DayRangeBar.TrackBrush = Token("AccentBorderBrush");
            DayRangeBar.FillBrush = Token("QuietForegroundBrush");
            DayRangeBar.MarkerBrush = live ? Token("AccentBlueBrush") : muted;
            DayRangeBar.SetRange(low, high, last);
            DayLowText.Text = low is double dl ? FormatPrice(dl) : "—";
            DayHighText.Text = high is double dh ? FormatPrice(dh) : "—";
            DayLowText.ToolTip = NumTip("Day low", s.DayLow);
            DayHighText.ToolTip = NumTip("Day high", s.DayHigh);
            var rangeTip = (tab.IsCrypto ? "Day range (UTC day)" : "Day range (regular session)") + ": " + DayLowText.Text + " – " + DayHighText.Text +
                           "; marker = last price " + LastPriceText.Text + (live ? "" : " (not live)") + ".\n" + NumTip("Day low", s.DayLow);
            DayRangeBar.ToolTip = rangeTip;
            AutomationProperties.SetName(DayRangeBar, rangeTip);

            // ----- volume (feed label always visible, Q2) -----
            VolumeText.Text = s.Volume?.Value is double v ? FormatVolume(v) : "—";
            VolumeText.ToolTip = NumTip("Volume", s.Volume);
            VolumeFeedText.Text = (s.Volume?.FeedLabel ?? s.Volume?.Feed ?? "").ToUpperInvariant();
            AutomationProperties.SetName(VolumeText, "Volume " + VolumeText.Text + " " + (s.Volume?.FeedLabel ?? ""));

            // ----- previous close -----
            PrevCloseText.Text = prev is double p2 ? FormatPrice(p2) : "—";
            PrevCloseText.ToolTip = NumTip("Previous close", s.PrevClose);
            PrevCloseDateText.Text = LocalDate(s.PrevClose?.AsOf) is { } d ? "CLOSE " + d.ToUpperInvariant() : "";
            AutomationProperties.SetName(PrevCloseText, "Previous close " + PrevCloseText.Text);

            // ----- reasons of missing header values, visible without hovering -----
            var reasons = new[] { s.Last, s.PrevClose, s.DayLow, s.DayHigh, s.Volume }
                .Where(n => n != null && n.Value == null && !string.IsNullOrWhiteSpace(n.Reason))
                .Select(n => n!.Reason!.Trim()).Distinct().ToList();
            HeaderHintText.Text = string.Join(" ", reasons);
            HeaderHintText.Visibility = reasons.Count > 0 ? Visibility.Visible : Visibility.Collapsed;

            // The 52-week marker follows the live price too.
            RenderRange52();
        }

        /// <summary>"today" / "pre-market" / "after hours" / "at close" — what the day values refer to (§7.4 "at close").</summary>
        private string SessionSuffix(ResearchTab tab)
        {
            if (tab.IsCrypto) return "today (UTC day)";
            return _market?.State switch
            {
                "open" => "today",
                "pre_market" => "pre-market",
                "after_hours" => "after hours · day values at close",
                "closed" or "holiday" => "at close",
                _ => "",
            };
        }

        private async Task RefreshSnapshotAsync()
        {
            var tab = _active;
            if (tab == null || !IsShown || _snapshotRefreshing || _shutDown) return;
            _snapshotRefreshing = true;
            try
            {
                var result = await ResearchClient.GetSnapshotAsync(tab.Symbol);
                if (_shutDown || !ReferenceEquals(tab, _active)) return;
                if (result.Ok && result.Value != null)
                {
                    var s = result.Value;
                    _prevClose = s.PrevClose?.Value;
                    if (_lastTrade == null || !_lastTradeLive) _lastTrade = s.Last?.Value;
                    if (s.Market != null) { _market = s.Market; _marketError = null; }
                    RenderHeader(s);
                }
                else
                {
                    // No older value stays on screen as if current: the header shows the reason instead.
                    _snapshot = null;
                    _snapshotError = result.Ok ? "empty response from the backend." : result.Message;
                    RenderHeader(null);
                }
                ApplyLastPrice();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: refreshing the price header failed.", ex);
            }
            finally
            {
                _snapshotRefreshing = false;
            }
            if (_statsLoadedUtc == null || DateTime.UtcNow - _statsLoadedUtc.Value >= StatsRefreshInterval) _ = LoadStatsAsync(tab);
        }

        // =====================================================================================
        // Market chip: local time, exchange time in the tooltip
        // =====================================================================================

        private void RenderMarketChip()
        {
            var tab = _active;
            if (tab == null) { MarketChip.Visibility = Visibility.Collapsed; return; }
            string text, tip, brush;
            var m = _market;
            var zone = LocalZoneName();
            if (m == null)
            {
                if (_marketError == null) { MarketChip.Visibility = Visibility.Collapsed; return; }
                text = "MARKET STATUS UNKNOWN";
                tip = _marketError;
                brush = "MutedForegroundBrush";
            }
            else if (tab.IsCrypto)
            {
                text = "● OPEN · 24/7";
                tip = "Crypto trades around the clock; \"today\" = the current UTC day" +
                      (LocalTime(m.DayStart) is { } start ? " (started " + start + " your time, 00:00 UTC)." : ".");
                brush = "PositiveGreenBrush";
            }
            else
            {
                switch (m.State)
                {
                    case "open":
                        text = "● OPEN" + (LocalTime(m.ClosesAt) is { } closes ? " · CLOSES " + closes : "");
                        tip = "Regular session" + (m.EarlyClose == true ? " (early close)" : "") + " — closes " + BothTimes(m.ClosesAt) + ".";
                        brush = "PositiveGreenBrush";
                        break;
                    case "pre_market":
                        text = "PRE-MARKET" + (LocalTime(m.OpensAt) is { } opens ? " · OPENS " + opens : "");
                        tip = "Pre-market (from 04:00 ET) — the regular session opens " + BothTimes(m.OpensAt) + ".";
                        brush = "CautionBrush";
                        break;
                    case "after_hours":
                        text = "AFTER HOURS · AT CLOSE";
                        tip = "The regular session has closed; day values are the session's values at close. Next open " + BothTimes(m.NextOpenAt) + ".";
                        brush = "CautionBrush";
                        break;
                    case "holiday":
                        text = "HOLIDAY · AT CLOSE";
                        tip = "No session today (exchange holiday); values are the last session's values at close. Next open " + BothTimes(m.NextOpenAt) + ".";
                        brush = "MutedForegroundBrush";
                        break;
                    case "closed":
                        text = "CLOSED · AT CLOSE" + (LocalTime(m.NextOpenAt) is { } next ? " · OPENS " + next : "");
                        tip = "Market closed; values are the last session's values at close. Next open " + BothTimes(m.NextOpenAt) + ".";
                        brush = "MutedForegroundBrush";
                        break;
                    default:
                        text = "MARKET STATUS UNKNOWN";
                        tip = "The backend reported an unknown market state.";
                        brush = "MutedForegroundBrush";
                        break;
                }
                tip += "\nTimes on the chip are your local time (" + zone + "); exchange time = New York (ET). Calendar: NYSE (backend).";
            }
            MarketChipText.Text = text;
            MarketChipText.Foreground = Token(brush);
            MarketChip.ToolTip = tip;
            AutomationProperties.SetName(MarketChip, text + ". " + tip);
            MarketChip.Visibility = Visibility.Visible;
        }

        /// <summary>"22:00 your time (16:00 ET)" — or "unknown" when the backend sent no time.</summary>
        private static string BothTimes(string? iso)
        {
            var local = LocalTime(iso);
            if (local == null) return "(time not reported)";
            var et = ExchangeTime(iso);
            return local + " your time" + (et != null ? " (" + et + " ET)" : "");
        }

        private static string? ExchangeTime(string? iso)
        {
            if (NewYorkZone == null || string.IsNullOrWhiteSpace(iso)) return null;
            if (!DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto)) return null;
            var ny = TimeZoneInfo.ConvertTime(dto, NewYorkZone);
            var nowNy = TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, NewYorkZone);
            return ny.Date == nowNy.Date
                ? ny.ToString("HH:mm", CultureInfo.InvariantCulture)
                : ny.ToString("ddd HH:mm", CultureInfo.InvariantCulture);
        }

        private static TimeZoneInfo? FindNewYorkZone()
        {
            foreach (var id in new[] { "America/New_York", "Eastern Standard Time" })
            {
                try { return TimeZoneInfo.FindSystemTimeZoneById(id); }
                catch (TimeZoneNotFoundException) { }
                catch (InvalidTimeZoneException) { }
            }
            return null; // the ET part of the tooltip is then omitted (local times stay correct)
        }

        private static string LocalZoneName()
        {
            var zone = TimeZoneInfo.Local;
            return zone.IsDaylightSavingTime(DateTime.Now) ? zone.DaylightName : zone.StandardName;
        }

        // =====================================================================================
        // Key stats column (GET /research/symbols/{s}/stats)
        // =====================================================================================

        private async Task LoadStatsAsync(ResearchTab tab)
        {
            if (ReferenceEquals(_statsInFlightFor, tab)) return;
            _statsInFlightFor = tab;
            try
            {
                var result = await ResearchClient.GetStatsAsync(tab.Symbol);
                if (_shutDown || !ReferenceEquals(tab, _active)) return;
                _statsLoadedUtc = DateTime.UtcNow;
                if (result.Ok && result.Value != null)
                {
                    _stats = result.Value;
                    _statsError = null;
                }
                else
                {
                    _stats = null; // no older stats shown as current
                    _statsError = result.Ok ? "Empty response from the backend." : result.Message;
                }
                RenderStats();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading the key stats failed.", ex);
                if (ReferenceEquals(tab, _active))
                {
                    _stats = null;
                    _statsError = "Key stats could not be loaded — see the Backend Log.";
                    RenderStats();
                }
            }
            finally
            {
                if (ReferenceEquals(_statsInFlightFor, tab)) _statsInFlightFor = null;
            }
        }

        private void StatsRetryButton_Click(object sender, RoutedEventArgs e)
        {
            if (_active == null) return;
            _statsError = null;
            RenderStats();
            _ = LoadStatsAsync(_active);
        }

        private void RenderStats()
        {
            var tab = _active;
            StatsMessageText.Visibility = Visibility.Collapsed;
            StatsRetryButton.Visibility = Visibility.Collapsed;
            if (tab == null)
            {
                StatsContent.Visibility = Visibility.Collapsed;
                StatsEmptyPanel.Visibility = Visibility.Visible;
                StatsEmptyTitle.Text = "No symbol open";
                StatsEmptyHint.Text = "Open a symbol to see its key stats.";
                return;
            }
            var s = _stats;
            if (s == null)
            {
                StatsContent.Visibility = Visibility.Collapsed;
                StatsEmptyPanel.Visibility = Visibility.Visible;
                if (_statsError != null)
                {
                    StatsEmptyTitle.Text = "Key stats unavailable";
                    StatsEmptyHint.Text = _statsError;
                    StatsRetryButton.Visibility = Visibility.Visible;
                }
                else
                {
                    StatsEmptyTitle.Text = "Loading key stats…";
                    StatsEmptyHint.Text = tab.Symbol;
                }
                return;
            }
            StatsEmptyPanel.Visibility = Visibility.Collapsed;
            StatsContent.Visibility = Visibility.Visible;

            SetStat(StatOpenText, s.Open, "Open (first regular-session minute)", FormatPrice);
            SetStat(StatVwapText, s.Vwap, "VWAP (regular session, Σ(vwap·v)/Σv of 1m bars)", FormatPrice);
            SetStat(StatAvgVolText, s.AvgVolume30d, "Average daily volume of the last 30 completed sessions", FormatVolume);
            StatExchangeText.Text = string.IsNullOrWhiteSpace(s.Exchange) ? "—" : s.Exchange!;
            StatExchangeText.ToolTip = "Listing exchange (Alpaca asset list).";
            AutomationProperties.SetName(StatExchangeText, "Exchange " + StatExchangeText.Text);

            PerfGrid.Children.Clear();
            foreach (var key in PerformanceKeys)
            {
                PerformanceEntry? entry = null;
                s.Performance?.TryGetValue(key, out entry);
                PerfGrid.Children.Add(BuildPerformanceTile(key, entry, s.Notes));
            }
            var notes = s.Notes?.Where(n => !string.IsNullOrWhiteSpace(n)).ToList() ?? new List<string>();
            PerfNoteText.Text = string.Join(" ", notes);
            PerfNoteText.Visibility = notes.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
            RenderRange52();
        }

        private void SetStat(TextBlock target, NumValue? n, string what, Func<double, string> format)
        {
            target.Text = n?.Value is double v ? format(v) : "—";
            target.Foreground = n?.Value is double ? new SolidColorBrush(Color.FromRgb(0xE6, 0xE6, 0xE6)) : Token("MutedForegroundBrush");
            target.ToolTip = NumTip(what, n);
            AutomationProperties.SetName(target, what + ": " + target.Text + (n?.Value == null && n?.Reason != null ? " — " + n.Reason : ""));
        }

        private Border BuildPerformanceTile(string key, PerformanceEntry? entry, List<string>? notes)
        {
            var pct = entry?.Pct?.Value;
            string value;
            Brush fg;
            Brush bg;
            Brush border;
            if (pct is double p && double.IsFinite(p))
            {
                value = Arrow(p) + " " + SignedPct(p);
                fg = DirectionBrush(p);
                if (p > 0) { bg = Tint("PositiveGreenBrush", 0x14); border = Tint("PositiveGreenBrush", 0x55); }
                else if (p < 0) { bg = Tint("AccentRedBrush", 0x14); border = Tint("AccentRedBrush", 0x55); }
                else { bg = Token("ModulePanelBackgroundBrush"); border = Token("AccentBorderBrush"); }
            }
            else
            {
                value = "—";
                fg = Token("MutedForegroundBrush");
                bg = Token("ModulePanelBackgroundBrush");
                border = Token("AccentBorderBrush");
            }
            var label = new TextBlock
            {
                Text = key,
                FontFamily = new FontFamily("Consolas"),
                FontSize = 9.5,
                Foreground = Token("MutedForegroundBrush"),
                HorizontalAlignment = HorizontalAlignment.Center,
            };
            var number = new TextBlock
            {
                Text = value,
                FontFamily = new FontFamily("Consolas"),
                FontSize = 10.5,
                Foreground = fg,
                HorizontalAlignment = HorizontalAlignment.Center,
                Margin = new Thickness(0, 2, 0, 0),
            };
            var stack = new StackPanel();
            stack.Children.Add(label);
            stack.Children.Add(number);
            var tile = new Border
            {
                Background = bg,
                BorderBrush = border,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(5),
                Padding = new Thickness(2, 4, 2, 4),
                Margin = new Thickness(0, 0, 4, 4),
                Child = stack,
            };
            var period = key switch
            {
                "1W" => "1 week",
                "1M" => "1 month",
                "3M" => "3 months",
                "6M" => "6 months",
                "YTD" => "year to date",
                "1Y" => "1 year",
                _ => key,
            };
            string tip;
            if (pct is double p2 && double.IsFinite(p2))
            {
                tip = "Performance " + period + ": " + SignedPct(p2) + " (" + DirectionWord(p2) + ") — last price vs the close of " +
                      (entry?.BaseDate ?? "the base session") + ".";
            }
            else
            {
                tip = "Performance " + period + ": not available — " + (entry?.Pct?.Reason ?? (entry == null ? "not reported by the backend." : "no reason was reported."));
            }
            tip += "\nFeed: " + (entry?.Pct?.FeedLabel ?? entry?.Pct?.Feed ?? "not reported") +
                   (LocalDateTime(entry?.Pct?.AsOf) is { } asOf ? "\nAs of " + asOf + " (local time)" : "") +
                   "\nSplit-adjusted closes; dividends not included.";
            var extra = notes?.Where(n => !string.IsNullOrWhiteSpace(n)).ToList();
            if (extra is { Count: > 0 }) tip += "\n" + string.Join(" ", extra);
            tile.ToolTip = tip;
            AutomationProperties.SetName(tile, "Performance " + period + ": " +
                                               (pct is double p3 && double.IsFinite(p3) ? DirectionWord(p3) + " " + Math.Abs(p3).ToString("0.00", CultureInfo.InvariantCulture) + " percent" : "not available"));
            return tile;
        }

        private void RenderRange52()
        {
            var s = _stats;
            if (s == null || _active == null) return;
            var r = s.Range52w;
            var low = r?.Low?.Value;
            var high = r?.High?.Value;
            var last = _lastTrade ?? _snapshot?.Last?.Value;
            var live = ActiveIsLive;
            Range52Bar.TrackBrush = Token("AccentBorderBrush");
            Range52Bar.FillBrush = Token("QuietForegroundBrush");
            Range52Bar.MarkerBrush = live ? Token("AccentBlueBrush") : Token("MutedForegroundBrush");
            Range52Bar.SetRange(low, high, last);
            Range52LowText.Text = low is double l ? FormatPrice(l) : "—";
            Range52HighText.Text = high is double h ? FormatPrice(h) : "—";
            Range52LowText.ToolTip = NumTip("52-week low", r?.Low);
            Range52HighText.ToolTip = NumTip("52-week high", r?.High);
            Range52DatesText.Text = r?.From != null && r.To != null ? r.From + " – " + r.To : "";
            var position = r?.PositionPct;
            var tip = "52-week range (completed daily bars" + (r?.From != null ? ", " + r.From + " – " + r.To : "") + "): " +
                      Range52LowText.Text + " – " + Range52HighText.Text + "; marker = last price" + (live ? "" : " (not live)") + ".";
            if (position != null)
                tip += "\n" + (position.Value is double pp
                    ? "Position in the range: " + pp.ToString("0.0", CultureInfo.InvariantCulture) + " % (" + (position.FeedLabel ?? "computed") + ")"
                    : "Position in the range: not available — " + (position.Reason ?? "no reason was reported."));
            tip += "\n" + NumTip("52-week low", r?.Low);
            Range52Bar.ToolTip = tip;
            AutomationProperties.SetName(Range52Bar, tip);
        }

        // =====================================================================================
        // Ctrl+K command palette (GET /research/palette)
        // =====================================================================================

        private void OpenPalette(PaletteMode mode, string? listId)
        {
            if (_shutDown) return;
            StarPopup.IsOpen = false;
            _paletteMode = mode;
            _paletteListId = listId;
            var list = listId == null ? null : _lists.FirstOrDefault(l => l.Id == listId);
            if (mode == PaletteMode.AddToList && list == null) mode = _paletteMode = PaletteMode.Open;
            PaletteTitleText.Text = mode == PaletteMode.AddToList ? "ADD TO WATCHLIST · " + (list!.Name ?? "").ToUpperInvariant()
                                  : mode == PaletteMode.Compare ? "COMPARE WITH · " + (_active?.Symbol ?? "") : "OPEN SYMBOL";
            AutomationProperties.SetName(PaletteBox, (mode == PaletteMode.AddToList ? "Add a symbol to " + list!.Name
                                                    : mode == PaletteMode.Compare ? "Compare symbol for the chart" : "Search symbols") +
                                                    ": type a ticker or company name, arrow keys choose, Enter " +
                                                    (mode == PaletteMode.AddToList ? "adds" : mode == PaletteMode.Compare ? "adds the comparison" : "opens") +
                                                    ", Escape closes");
            if (PaletteOverlay.Visibility != Visibility.Visible)
                _paletteReturnFocus = Keyboard.FocusedElement;
            PaletteOverlay.Visibility = Visibility.Visible;
            _paletteItems.Clear();
            _paletteIndex = -1;
            _paletteItemsQuery = null;
            _paletteEnterPending = false;
            if (PaletteBox.Text.Length > 0) PaletteBox.Text = ""; // TextChanged → recent symbols
            else QueryPalette("");
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() =>
            {
                if (PaletteOverlay.Visibility != Visibility.Visible) return;
                PaletteBox.Focus();
                Keyboard.Focus(PaletteBox);
            }));
        }

        private void ClosePalette()
        {
            if (PaletteOverlay.Visibility != Visibility.Visible) return;
            _paletteDebounce?.Stop();
            _paletteGeneration++;
            _paletteEnterPending = false;
            PaletteOverlay.Visibility = Visibility.Collapsed;
            var back = _paletteReturnFocus as UIElement;
            _paletteReturnFocus = null;
            if (back != null && back.IsVisible && back.Focusable) back.Focus();
            else if (SearchButton.IsVisible) SearchButton.Focus();
        }

        private void PaletteCloseButton_Click(object sender, RoutedEventArgs e) => ClosePalette();

        private void PaletteBackdrop_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            ClosePalette();
            e.Handled = true;
        }

        private string PaletteQuery => PaletteBox.Text.Trim();

        private void PaletteBox_TextChanged(object sender, TextChangedEventArgs e)
        {
            PaletteHint.Visibility = string.IsNullOrEmpty(PaletteBox.Text) ? Visibility.Visible : Visibility.Collapsed;
            if (PaletteOverlay.Visibility != Visibility.Visible) return;
            var q = PaletteQuery;
            if (q.Length > 0 && !TickerInput.IsMatch(q))
            {
                _paletteDebounce?.Stop();
                _paletteGeneration++;
                _paletteItems.Clear();
                _paletteIndex = -1;
                _paletteItemsQuery = null;
                SetPaletteMessage("Use letters, digits, space and . - / only (at most 32 characters).");
                RenderPaletteRows();
                return;
            }
            // Debounced (180 ms): one request per pause in typing; an empty query shows the recent symbols at once.
            if (q.Length == 0)
            {
                _paletteDebounce?.Stop();
                QueryPalette("");
                return;
            }
            if (_paletteDebounce == null)
            {
                _paletteDebounce = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromMilliseconds(180) };
                _paletteDebounce.Tick += (_, _) =>
                {
                    _paletteDebounce!.Stop();
                    QueryPalette(PaletteQuery);
                };
            }
            _paletteDebounce.Stop();
            _paletteDebounce.Start();
        }

        private void QueryPalette(string query) => _ = QueryPaletteAsync(query);

        private async Task QueryPaletteAsync(string query)
        {
            var generation = ++_paletteGeneration;
            if (query.Length > 0 && (_paletteItemsQuery == null || _paletteItems.Count == 0)) SetPaletteMessage("Searching…");
            PlatformResultHolder<PaletteResult> holder;
            try { holder = new PlatformResultHolder<PaletteResult>(await ResearchClient.GetPaletteAsync(query, 20)); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: the symbol search failed.", ex);
                if (generation != _paletteGeneration) return;
                _paletteItems.Clear();
                _paletteIndex = -1;
                _paletteItemsQuery = query;
                _paletteEnterPending = false;
                SetPaletteMessage("Search failed — see the Backend Log.");
                RenderPaletteRows();
                return;
            }
            if (_shutDown || generation != _paletteGeneration || PaletteOverlay.Visibility != Visibility.Visible) return;
            var result = holder.Result;
            _paletteItems.Clear();
            _paletteItemsQuery = query;
            if (!result.Ok || result.Value == null)
            {
                _paletteIndex = -1;
                _paletteEnterPending = false;
                SetPaletteMessage(result.Ok ? "Empty response from the backend." : result.Message);
                RenderPaletteRows();
                return;
            }
            foreach (var item in result.Value.Items ?? new List<PaletteItem>())
            {
                var sym = item.Symbol?.ToUpperInvariant();
                if (sym == null || !PlatformSymbol.IsMatch(sym) || (item.AssetClass != "equity" && item.AssetClass != "crypto")) continue;
                item.Symbol = sym;
                _paletteItems.Add(item);
            }
            _paletteIndex = _paletteItems.Count > 0 ? 0 : -1;
            if (_paletteItems.Count == 0)
            {
                if (query.Length == 0)
                    SetPaletteMessage("Type a ticker or company name (e.g. NVDA, Apple, BTC-USD). Symbols you open appear here as recent.");
                else if (Regex.IsMatch(query, @"^[A-Za-z0-9]{2,10}[-/]USD$", RegexOptions.CultureInvariant))
                    SetPaletteMessage("Not available from Alpaca (the configured crypto source): no match for \"" + query + "\".");
                else
                    SetPaletteMessage("No match for \"" + query + "\" in Alpaca's asset list (US stocks/ETFs and Alpaca coins).");
            }
            else
            {
                SetPaletteMessage(null);
            }
            RenderPaletteRows();
            if (_paletteEnterPending)
            {
                _paletteEnterPending = false;
                if (query == PaletteQuery) ExecutePaletteSelection();
            }
        }

        private void SetPaletteMessage(string? text)
        {
            PaletteMessageText.Text = text ?? "";
            PaletteMessageText.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
            if (!string.IsNullOrEmpty(text)) AnnouncePalette(text!);
        }

        private void RenderPaletteRows()
        {
            PaletteResults.Children.Clear();
            var recentMode = _paletteItemsQuery == "";
            if (recentMode && _paletteItems.Count > 0)
                PaletteResults.Children.Add(new TextBlock { Text = "RECENT", Style = (Style)Resources["CaptionStyle"], Margin = new Thickness(6, 0, 0, 4) });
            var listNames = _lists.Where(l => l.Id != null).ToDictionary(l => l.Id!, l => l.Name ?? "", StringComparer.Ordinal);
            for (var i = 0; i < _paletteItems.Count; i++)
            {
                var item = _paletteItems[i];
                var index = i;
                var selected = i == _paletteIndex;
                var grid = new Grid();
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(92) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var ticker = new TextBlock
                {
                    Text = item.Symbol,
                    FontFamily = new FontFamily("Consolas"),
                    FontSize = 12,
                    FontWeight = FontWeights.SemiBold,
                    Foreground = selected ? Token("AccentBlueBrush") : new SolidColorBrush(Color.FromRgb(0xE6, 0xE6, 0xE6)),
                    VerticalAlignment = VerticalAlignment.Center,
                };
                var name = new TextBlock
                {
                    Text = item.Name ?? "",
                    FontSize = 11.5,
                    Foreground = Token("SecondaryForegroundBrush"),
                    TextTrimming = TextTrimming.CharacterEllipsis,
                    VerticalAlignment = VerticalAlignment.Center,
                    Margin = new Thickness(0, 0, 8, 0),
                };
                Grid.SetColumn(name, 1);
                var tags = new List<string>();
                if (_tabs.Any(t => t.Symbol == item.Symbol)) tags.Add("OPEN");
                var inLists = (item.Watchlists ?? new List<string>()).Where(listNames.ContainsKey).Select(id => listNames[id]).ToList();
                if (inLists.Count > 0) tags.Add("★ " + inLists[0].ToUpperInvariant() + (inLists.Count > 1 ? " +" + (inLists.Count - 1) : ""));
                if (item.Recent == true && !recentMode) tags.Add("RECENT");
                tags.Add(KindLabel(item.Kind, item.AssetClass) + (string.IsNullOrWhiteSpace(item.Exchange) ? "" : " · " + item.Exchange!.ToUpperInvariant()));
                var meta = new TextBlock
                {
                    Text = string.Join("  ", tags),
                    FontFamily = new FontFamily("Consolas"),
                    FontSize = 9.5,
                    Foreground = Token("MutedForegroundBrush"),
                    VerticalAlignment = VerticalAlignment.Center,
                };
                Grid.SetColumn(meta, 2);
                grid.Children.Add(WithLogo(item.Symbol!, ticker, 16)); // MR6 logo badge (initials when none)
                grid.Children.Add(name);
                grid.Children.Add(meta);
                var row = new Button
                {
                    Style = (Style)Resources["ListRowButtonStyle"],
                    Content = grid,
                    Focusable = false, // the query box keeps the focus (keyboard-only use with ↑/↓/Enter)
                    Padding = new Thickness(6, 4, 6, 4),
                    Margin = new Thickness(0, 0, 0, 1),
                };
                if (selected)
                {
                    row.Background = Tint("AccentBlueBrush", 0x1F);
                    row.BorderBrush = Tint("AccentBlueBrush", 0x99);
                }
                AutomationProperties.SetName(row, item.Symbol + " — " + (item.Name ?? "") + (inLists.Count > 0 ? ", in " + string.Join(", ", inLists) : ""));
                row.Click += (_, _) =>
                {
                    _paletteIndex = index;
                    ExecutePaletteSelection();
                };
                PaletteResults.Children.Add(row);
                if (selected) row.Loaded += (_, _) => row.BringIntoView();
            }
            if (_paletteIndex >= 0 && _paletteIndex < _paletteItems.Count)
            {
                var item = _paletteItems[_paletteIndex];
                AnnouncePalette(item.Symbol + " — " + (item.Name ?? "") + " (" + (_paletteIndex + 1) + " of " + _paletteItems.Count + ")");
            }
            else if (PaletteMessageText.Visibility != Visibility.Visible)
            {
                PaletteStatusText.Text = "";
            }
        }

        private void AnnouncePalette(string text)
        {
            PaletteStatusText.Text = text;
            try
            {
                var peer = UIElementAutomationPeer.FromElement(PaletteStatusText) ?? UIElementAutomationPeer.CreatePeerForElement(PaletteStatusText);
                peer?.RaiseAutomationEvent(AutomationEvents.LiveRegionChanged);
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: screen-reader announcement failed.", ex);
            }
        }

        private void PaletteOverlay_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            switch (e.Key)
            {
                case Key.Escape:
                    ClosePalette();
                    e.Handled = true;
                    break;
                case Key.Down:
                case Key.Up:
                case Key.PageDown:
                case Key.PageUp:
                {
                    e.Handled = true;
                    if (_paletteItems.Count == 0) return;
                    var step = e.Key switch { Key.Down => 1, Key.Up => -1, Key.PageDown => 5, _ => -5 };
                    _paletteIndex = Math.Clamp((_paletteIndex < 0 ? 0 : _paletteIndex) + step, 0, _paletteItems.Count - 1);
                    RenderPaletteRows();
                    break;
                }
                case Key.Enter:
                {
                    e.Handled = true;
                    var q = PaletteQuery;
                    if (_paletteItemsQuery == q && _paletteDebounce?.IsEnabled != true)
                    {
                        ExecutePaletteSelection();
                    }
                    else if (q.Length == 0 || TickerInput.IsMatch(q))
                    {
                        // Typed faster than the search answered: run it now and open the first result when it arrives.
                        _paletteDebounce?.Stop();
                        _paletteEnterPending = true;
                        QueryPalette(q);
                    }
                    break;
                }
            }
        }

        private void ExecutePaletteSelection()
        {
            if (_paletteIndex < 0 || _paletteIndex >= _paletteItems.Count) return;
            var item = _paletteItems[_paletteIndex];
            var symbol = item.Symbol;
            if (symbol == null) return;
            var mode = _paletteMode;
            var listId = _paletteListId;
            ClosePalette();
            if (mode == PaletteMode.AddToList && listId != null)
            {
                AddSymbolToList(listId, symbol);
                return;
            }
            if (mode == PaletteMode.Compare)
            {
                AddCompareIndicator(symbol, item.AssetClass); // MR5 (ResearchView.Indicators.cs)
                return;
            }
            OpenSymbol(symbol, item.Name, item.AssetClass, item.Kind, item.Exchange);
        }

        // =====================================================================================
        // Formatting helpers (numbers in Consolas; signs U+2212; direction never by colour alone)
        // =====================================================================================

        private static string FormatPrice(double v)
        {
            var a = Math.Abs(v);
            var format = a >= 1 ? "N2" : a >= 0.01 ? "N4" : "N8";
            return v.ToString(format, CultureInfo.InvariantCulture);
        }

        private static string Signed(double v) => (v > 0 ? "+" : v < 0 ? "−" : "±") + FormatPrice(Math.Abs(v));

        private static string SignedPct(double p) => (p > 0 ? "+" : p < 0 ? "−" : "±") + Math.Abs(p).ToString("0.00", CultureInfo.InvariantCulture) + "%";

        private static string Arrow(double v) => v > 0 ? "▲" : v < 0 ? "▼" : "■";

        private static string DirectionWord(double v) => v > 0 ? "up" : v < 0 ? "down" : "unchanged";

        private Brush DirectionBrush(double v) => v > 0 ? Token("PositiveGreenBrush") : v < 0 ? Token("AccentRedBrush") : Token("MutedForegroundBrush");

        private static string FormatVolume(double v)
        {
            var a = Math.Abs(v);
            if (a >= 1e9) return (v / 1e9).ToString("0.00", CultureInfo.InvariantCulture) + "B";
            if (a >= 1e6) return (v / 1e6).ToString("0.00", CultureInfo.InvariantCulture) + "M";
            if (a >= 1e4) return (v / 1e3).ToString("0.0", CultureInfo.InvariantCulture) + "K";
            return v == Math.Floor(v) ? v.ToString("N0", CultureInfo.InvariantCulture) : v.ToString("0.####", CultureInfo.InvariantCulture);
        }

        /// <summary>Tooltip of a <c>Num</c>: what · feed · as-of (local), or the reason when the value is missing.</summary>
        private static string NumTip(string what, NumValue? n)
        {
            if (n == null) return what + ": not reported by the backend.";
            var feed = !string.IsNullOrWhiteSpace(n.FeedLabel) ? n.FeedLabel! : (n.Feed ?? "feed not reported");
            var text = n.Value is double
                ? what + " · " + feed
                : what + ": not available — " + (string.IsNullOrWhiteSpace(n.Reason) ? "no reason was reported." : n.Reason) + "\nFeed: " + feed;
            if (LocalDateTime(n.AsOf) is { } asOf) text += "\nAs of " + asOf + " (local time)";
            return text;
        }

        private static string? FirstReason(params NumValue?[] values)
            => values.Where(n => n != null && n.Value == null && !string.IsNullOrWhiteSpace(n.Reason)).Select(n => n!.Reason).FirstOrDefault();

        private static string? LocalDateTime(string? iso)
        {
            if (string.IsNullOrWhiteSpace(iso)) return null;
            if (!DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto)) return null;
            return dto.ToLocalTime().ToString("ddd dd MMM HH:mm", CultureInfo.InvariantCulture);
        }

        private static string? LocalDate(string? iso)
        {
            if (string.IsNullOrWhiteSpace(iso)) return null;
            if (!DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto)) return null;
            // The close of a US session (20:00/21:00 UTC) is the same calendar day in New York; show the exchange date.
            var day = NewYorkZone != null ? TimeZoneInfo.ConvertTime(dto, NewYorkZone) : dto.ToLocalTime();
            return day.ToString("ddd dd MMM", CultureInfo.InvariantCulture);
        }

        private static string Initials(string symbol)
        {
            var core = symbol.Split('-', '.')[0];
            return core.Length <= 2 ? core : core.Substring(0, 2);
        }

        private static string KindLabel(ResearchTab tab) => KindLabel(tab.Kind, tab.AssetClass);

        private static string KindLabel(string? kind, string? assetClass) => kind switch
        {
            "stock" => "STOCK",
            "etf" => "ETF",
            "crypto" => "CRYPTO",
            _ => assetClass == "crypto" ? "CRYPTO" : "EQUITY",
        };

        /// <summary>A token colour with another alpha (tile/selection backgrounds) — no colour outside the tokens (plan §2.3).</summary>
        private Brush Tint(string tokenKey, byte alpha)
        {
            var c = ((SolidColorBrush)Resources[tokenKey]).Color;
            var brush = new SolidColorBrush(Color.FromArgb(alpha, c.R, c.G, c.B));
            brush.Freeze();
            return brush;
        }

        private static string ClipText(string? text, int max) => text == null ? "" : (text.Length <= max ? text : text.Substring(0, max) + "…");

        /// <summary>Wraps an awaited result so a catch block can sit around the await only (no logic in try).</summary>
        private readonly struct PlatformResultHolder<T>
        {
            public PlatformResultHolder(UFOS.ai.HedgeFund.Platform.PlatformResult<T> result) => Result = result;
            public UFOS.ai.HedgeFund.Platform.PlatformResult<T> Result { get; }
        }
    }
}

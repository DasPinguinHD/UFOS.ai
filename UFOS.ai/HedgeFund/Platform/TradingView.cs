using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Hedge Fund → "Trading" (Strategy Platform stage 7 — portfolio, risk, OMS, paper only; SPEC §17, exact JSON §17.11,
    /// deviations §17.12, WPF notes §17.13). Pinned header: the backend's paper notice, the coexistence state with the old
    /// engine (one owner per account, §17.1), the scope of the toolbar kill switch (it covers both engines — it is NOT
    /// duplicated here), the dead-man switch, auto trading (confirmation when switching on) and the runtime. Sections
    /// (tabs, Ctrl+1…7): Portfolio (per account: equity, cash, exposure, daily-loss and drawdown gauges with state chips,
    /// halt + resume, positions, runtime cycles), Strategies (sub-ledgers: allocation weight from the inverse-vol allocator
    /// with correlation penalty, P&amp;L, costs, exposure, state incubating day x of N / active / paused, ledger), Netting
    /// (per instrument: target vs current, contributing strategies), Orders (OMS blotter with filters; detail with state
    /// timeline, pre-trade checks, fills, reject reasons verbatim, cancel), Incubation (runs, start dialog with the
    /// immutable length, abort), Migration (status, dry-run report, one confirmation step) and Risk limits (adjustable with
    /// confirmation + optional audit note — fix1023). Everything is plain text; backend errors are shown verbatim; nothing is replaced by a
    /// stand-in value. Polls every 5 s only while visible (every GET touches the shared heartbeat, §17.1); a lost
    /// connection keeps the last known state with a "connection lost (attempt n)" banner and keeps polling.
    /// </summary>
    internal sealed partial class TradingView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(5);
        private static readonly Brush RiskSurface = FundPalette.Make(0x1FD9A54B);
        private static readonly Brush FocusSurface = FundPalette.Make(0x1A7C8CF8);
        private static readonly Brush LongSurface = FundPalette.Make(0x1A6FA98A);
        private static readonly Brush KillText = FundPalette.Make(0xFFF4A197);

        private const string SecPortfolio = "portfolio";
        private const string SecStrategies = "strategies";
        private const string SecNetting = "netting";
        private const string SecOrders = "orders";
        private const string SecIncubation = "incubation";
        private const string SecMigration = "migration";
        private const string SecSettings = "settings";

        // ----- header -----
        private readonly TextBlock _notice;
        private readonly StackPanel _coexistHost;
        private readonly Button _autoTrade;
        private readonly TextBlock _autoTradeText;
        private readonly TextBlock _runtimeText;
        /// <summary>Review 2026-10-22: every failed reload is visible ("refresh failed: …") and logged — never a silently stale view.</summary>
        private readonly TextBlock _refreshStatus;
        private readonly Button _runNow;
        private readonly StackPanel _bannerHost;
        private readonly List<(RadioButton Button, string Key)> _tabs = new();
        private readonly ScrollViewer _scroll;
        private readonly DispatcherTimer _poll;

        // ----- section panels (persistent; re-filled when their data changed) -----
        private readonly StackPanel _portfolioPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _strategiesPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _nettingPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _ordersPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _incubationPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _migrationPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _settingsPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly Dictionary<string, string> _signatures = new(StringComparer.Ordinal);

        // ----- data -----
        private TradingState? _state;
        private readonly Dictionary<string, TradingAccountDetail> _details = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _detailErrors = new(StringComparer.Ordinal);
        private TradingRuntime? _runtime;
        private string _section = SecPortfolio;
        private string? _accountId;               // account of the Strategies / Netting / Risk-limits sections
        private string? _connectionProblem;
        private int _connectionAttempts;
        private int _stateSeq;
        private bool _polling;
        private bool _stopped;
        private bool _busy;
        private bool _activated;
        // fix1023 review minor 6: actions whose confirm dialog closes BEFORE the request (resume, rebalance, abort run) — the
        // calling button stays disabled while the request is in flight, also when a poll re-renders it meanwhile.
        private readonly HashSet<string> _inFlight = new(StringComparer.Ordinal);

        /// <summary>Wires an action button: disabled while <paramref name="key"/> is in flight (also after a re-render).</summary>
        private Button InFlightButton(Button button, string key, Func<Task> action)
        {
            if (_inFlight.Contains(key)) TradingUi.SetBusy(button, true, "Working…");
            button.Click += async (_, _) =>
            {
                if (!_inFlight.Add(key)) return;
                TradingUi.SetBusy(button, true, "Working…");
                try { await action(); }
                finally
                {
                    _inFlight.Remove(key);
                    TradingUi.SetBusy(button, false);
                    button.IsEnabled = true;
                }
            };
            return button;
        }

        /// <summary>Stage 8 (SPEC §18.12): "Brokers…" — the host opens Settings (Keys &amp; data) at its Brokers section.</summary>
        public event Action? BrokersRequested;

        public TradingView()
        {
            AutomationProperties.SetName(this, "Portfolio and trading (paper)");
            Focusable = false;
            var root = new Grid();
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // notice + coexistence
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // controls (auto trading, runtime)
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // banners
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // tabs
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            // ----- notice + coexistence (pinned) -----
            var head = new StackPanel();
            var titleRow = new Grid();
            titleRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            titleRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            titleRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var title = FundUi.Text("Portfolio & Trading", 14, FundPalette.TextBright, bold: true, wrap: false);
            titleRow.Children.Add(title);
            _notice = FundUi.Text("Paper trading only · simulated money", 10.5, FundPalette.Faint, wrap: false);
            _notice.VerticalAlignment = VerticalAlignment.Center;
            _notice.Margin = new Thickness(12, 0, 0, 0);
            Grid.SetColumn(_notice, 1);
            titleRow.Children.Add(_notice);
            var refresh = SmallButton("↻ Refresh", "Reload portfolio and trading (F5)");
            refresh.Margin = new Thickness(0);
            refresh.ToolTip = "Reload everything (F5)";
            refresh.Click += async (_, _) => await RefreshAsync(force: true);
            var brokers = SmallButton("Brokers…", "Open Settings, Brokers: connect Alpaca paper or Binance Spot demo, reconciliation, routing, live guard (Ctrl+B)");
            brokers.Margin = new Thickness(0, 0, 8, 0);
            brokers.ToolTip = "Settings → Brokers (Ctrl+B): connection, reconciliation and Resolve…, routing of strategies, per-broker kill, live guard (read only)";
            brokers.Click += (_, _) => BrokersRequested?.Invoke();
            var titleButtons = new StackPanel { Orientation = Orientation.Horizontal };
            titleButtons.Children.Add(brokers);
            titleButtons.Children.Add(refresh);
            Grid.SetColumn(titleButtons, 2);
            titleRow.Children.Add(titleButtons);
            head.Children.Add(titleRow);
            _refreshStatus = FundUi.Text("", 10.5, FundPalette.Faint);
            _refreshStatus.Margin = new Thickness(0, 4, 0, 0);
            _refreshStatus.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(_refreshStatus, AutomationLiveSetting.Polite);
            head.Children.Add(_refreshStatus);
            _coexistHost = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
            AutomationProperties.SetName(_coexistHost, "Coexistence with the old engine and kill switch scope");
            head.Children.Add(_coexistHost);
            Place(root, FundUi.Panel(head, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 10, 8)), 0);

            // ----- controls: auto trading + runtime -----
            var controls = new Grid();
            controls.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            controls.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            controls.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            controls.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            _autoTrade = FundUi.MakeButton("Auto trading: —");
            _autoTrade.Padding = new Thickness(12, 3, 12, 3);
            _autoTrade.MinHeight = 24;
            _autoTrade.FontSize = 11.5;
            _autoTrade.IsEnabled = false;
            _autoTrade.Click += async (_, _) => await ToggleAutoTradeAsync();
            AutomationProperties.SetName(_autoTrade, "Auto trading switch");
            controls.Children.Add(_autoTrade);
            _autoTradeText = FundUi.Text("", 10.5, FundPalette.Dim);
            _autoTradeText.VerticalAlignment = VerticalAlignment.Center;
            _autoTradeText.Margin = new Thickness(10, 0, 12, 0);
            AutomationProperties.SetLiveSetting(_autoTradeText, AutomationLiveSetting.Polite);
            Grid.SetColumn(_autoTradeText, 1);
            controls.Children.Add(_autoTradeText);
            _runNow = SmallButton("▶ Run cycle now", "Run one platform runtime cycle now");
            _runNow.Margin = new Thickness(0);
            _runNow.ToolTip = "One runtime cycle now: decisions, executions, housekeeping, post-trade checks (POST /v2/oms/runtime/run)";
            _runNow.Click += async (_, _) => await RunCycleAsync();
            Grid.SetColumn(_runNow, 2);
            controls.Children.Add(_runNow);
            _runtimeText = FundUi.Text("Runtime: —", 10.5, FundPalette.Dim);
            _runtimeText.VerticalAlignment = VerticalAlignment.Center;
            _runtimeText.Margin = new Thickness(10, 0, 0, 0);
            Grid.SetColumn(_runtimeText, 3);
            controls.Children.Add(_runtimeText);
            Place(root, FundUi.Panel(controls, new Thickness(14, 8, 14, 8), new Thickness(0, 0, 10, 8)), 1);

            _bannerHost = new StackPanel { Margin = new Thickness(0, 0, 10, 0) };
            AutomationProperties.SetLiveSetting(_bannerHost, AutomationLiveSetting.Assertive);
            Place(root, _bannerHost, 2);

            // ----- tabs -----
            var tabs = new WrapPanel { Margin = new Thickness(0, 0, 10, 8) };
            KeyboardNavigation.SetDirectionalNavigation(tabs, KeyboardNavigationMode.Cycle);
            KeyboardNavigation.SetTabNavigation(tabs, KeyboardNavigationMode.Once);
            AutomationProperties.SetName(tabs, "Portfolio and trading sections");
            AddTab(tabs, SecPortfolio, "Portfolio", "Portfolio: accounts, equity, exposure, loss and drawdown limits, positions (Ctrl+1)", "Ctrl+1");
            AddTab(tabs, SecStrategies, "Strategies", "Strategies: sub-ledgers, allocation weights, P and L, costs, exposure, state (Ctrl+2)", "Ctrl+2");
            AddTab(tabs, SecNetting, "Netting", "Netting: per instrument target versus current position and contributing strategies (Ctrl+3)", "Ctrl+3");
            AddTab(tabs, SecOrders, "Orders", "Orders: OMS order blotter with filters, checks, fills and rejects (Ctrl+4)", "Ctrl+4");
            AddTab(tabs, SecIncubation, "Incubation", "Incubation: runs with a fixed length, start and abort (Ctrl+5)", "Ctrl+5");
            AddTab(tabs, SecMigration, "Migration", "Migration of the old paper account: status and dry run (Ctrl+6)", "Ctrl+6");
            AddTab(tabs, SecSettings, "Risk limits", "Risk limits: pre-trade checks, daily loss, drawdown kill, allocator (Ctrl+7)", "Ctrl+7");
            Place(root, tabs, 3);

            _scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = _portfolioPanel };
            Place(root, _scroll, 4);
            Content = root;

            _portfolioPanel.Children.Add(FundUi.Text("Loading…", 11.5, FundPalette.Faint));
            AutomationProperties.SetName(_portfolioPanel, "Portfolio");
            AutomationProperties.SetName(_strategiesPanel, "Strategies and sub-ledgers");
            AutomationProperties.SetName(_nettingPanel, "Netting");
            AutomationProperties.SetName(_ordersPanel, "Order blotter");
            AutomationProperties.SetName(_incubationPanel, "Incubation runs");
            AutomationProperties.SetName(_migrationPanel, "Migration of the old paper account");
            AutomationProperties.SetName(_settingsPanel, "Risk limits");

            _poll = new DispatcherTimer { Interval = PollInterval };
            _poll.Tick += async (_, _) => await PollAsync();
            IsVisibleChanged += (_, _) => UpdatePolling();
            PreviewKeyDown += async (_, e) => await OnKeyAsync(e);
        }

        // =====================================================================================
        // Lifecycle
        // =====================================================================================

        /// <summary>Called whenever the rail's "Trading" entry is selected.</summary>
        public async Task ActivateAsync()
        {
            if (_stopped) return;
            _activated = true;
            await RefreshAsync(force: true);
            UpdatePolling();
        }

        /// <summary>Live click-test 2026-10-22: reload now (a broker action in Settings changed accounts / routing). Only once activated.</summary>
        public void RequestRefresh()
        {
            if (_stopped || !_activated) return;
            _ = RefreshAsync(force: true);
        }

        /// <summary>A failed reload of /v2/portfolio/state: logged and shown in the header (the view keeps the last state, labelled).</summary>
        private void ShowRefreshFailure(string reason)
        {
            try { UFOS.ai.Logging.AppLog.Warn("HedgeFund", "Portfolio & Trading: refresh failed: " + reason); } catch { }
            _refreshStatus.Text = "refresh failed: " + reason + " — showing the last loaded state";
            _refreshStatus.Foreground = FundPalette.Risk;
            _refreshStatus.Visibility = Visibility.Visible;
            AutomationProperties.SetName(_refreshStatus, _refreshStatus.Text);
        }

        /// <summary>
        /// Reloads until <paramref name="done"/> holds (at most 5 more reads, 1 s apart) — after a create action the view must show the
        /// result without a manual Refresh (live click-test 2026-10-22: "No platform paper account yet" stayed after Open account).
        /// Failed reads stay visible (ShowRefreshFailure); when the result is still missing afterwards, that is shown and logged too.
        /// </summary>
        private async Task RefreshUntilAsync(Func<bool> done, string missingText)
        {
            await RefreshAsync(force: true);
            for (var i = 0; i < 5 && !_stopped && !done(); i++)
            {
                await Task.Delay(1000);
                if (_stopped) return;
                await RefreshAsync(force: true);
            }
            if (!_stopped && !done()) ShowRefreshFailure(missingText);
        }

        /// <summary>Window closed: stop polling for good.</summary>
        public void Shutdown()
        {
            _stopped = true;
            _poll.Stop();
        }

        private void UpdatePolling()
        {
            if (!_stopped && _activated && IsVisible) { if (!_poll.IsEnabled) _poll.Start(); }
            else _poll.Stop();
        }

        private async Task PollAsync()
        {
            if (_polling || _stopped || _busy || !IsVisible) return;
            _polling = true;
            try { await RefreshAsync(force: false); }
            finally { _polling = false; }
        }

        private async Task OnKeyAsync(KeyEventArgs e)
        {
            var ctrl = (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control;
            if (e.Key == Key.F5)
            {
                e.Handled = true;
                await RefreshAsync(force: true);
                return;
            }
            if (!ctrl) return;
            var index = e.Key switch
            {
                Key.D1 or Key.NumPad1 => 0,
                Key.D2 or Key.NumPad2 => 1,
                Key.D3 or Key.NumPad3 => 2,
                Key.D4 or Key.NumPad4 => 3,
                Key.D5 or Key.NumPad5 => 4,
                Key.D6 or Key.NumPad6 => 5,
                Key.D7 or Key.NumPad7 => 6,
                _ => -1,
            };
            if (index >= 0 && index < _tabs.Count)
            {
                e.Handled = true;
                _tabs[index].Button.IsChecked = true;
                _tabs[index].Button.Focus();
                return;
            }
            if (e.Key == Key.N)
            {
                e.Handled = true;
                await NewOrderAsync(_section == SecOrders ? _orderAccount ?? _accountId : _accountId, null, null, null);
            }
            else if (e.Key == Key.I)
            {
                e.Handled = true;
                await DialogGuard.RunAsync("incubation-start", () => StartIncubationAsync(null));   // dlgguard: Ctrl+I shares the button's guard
            }
            else if (e.Key == Key.B)
            {
                e.Handled = true;
                BrokersRequested?.Invoke();   // stage 8: Settings → Brokers
            }
        }

        private Window? Owner => Window.GetWindow(this);

        // =====================================================================================
        // Loading
        // =====================================================================================

        private async Task RefreshAsync(bool force)
        {
            if (_stopped) return;
            var seq = ++_stateSeq;
            var res = await PlatformClient.GetTradingStateAsync();
            if (seq != _stateSeq || _stopped) return;
            if (!res.Ok || res.Value == null)
            {
                _connectionAttempts++;
                _connectionProblem = res.Ok ? "Unexpected response from the backend (/v2/portfolio/state) — backend and app versions may not match." : PlatformClient.DescribeFailure(res);
                ShowRefreshFailure(_connectionProblem);
                RenderBanners();
                if (_state == null) ShowSectionMessage(_connectionProblem);
                return;
            }
            var reconnected = _connectionProblem != null;
            _connectionProblem = null;
            _connectionAttempts = 0;
            _state = res.Value;
            _refreshStatus.Visibility = Visibility.Collapsed;
            var accounts = Accounts();
            if (_accountId == null || !accounts.Any(a => a.Id == _accountId)) _accountId = accounts.FirstOrDefault()?.Id;
            RenderHeader();
            RenderBanners();
            if (force || reconnected) _signatures.Clear();
            await LoadSectionAsync(_section, force);
        }

        private List<TradingAccountSummary> Accounts() => _state?.Accounts?.Where(a => !string.IsNullOrEmpty(a.Id)).ToList() ?? new List<TradingAccountSummary>();

        private TradingAccountSummary? Account(string? id) => id == null ? null : Accounts().FirstOrDefault(a => a.Id == id);

        private async Task LoadDetailAsync(string accountId)
        {
            var res = await PlatformClient.GetTradingAccountAsync(accountId);
            if (_stopped) return;
            if (res.Ok && res.Value != null)
            {
                _details[accountId] = res.Value;
                _detailErrors.Remove(accountId);
            }
            else
            {
                _detailErrors[accountId] = res.Ok ? "Unexpected response from the backend (/v2/portfolio/accounts/" + accountId + ")." : PlatformClient.DescribeFailure(res);
            }
        }

        private async Task LoadSectionAsync(string section, bool force)
        {
            switch (section)
            {
                case SecPortfolio:
                    foreach (var a in Accounts()) await LoadDetailAsync(a.Id!);
                    var rt = await PlatformClient.GetRuntimeAsync();
                    if (rt.Ok && rt.Value != null) _runtime = rt.Value;
                    RenderIfChanged(SecPortfolio, new object?[] { _state?.Accounts, _details, _detailErrors, _runtime?.Cycles }, RenderPortfolio);
                    break;
                case SecStrategies:
                    await LoadStrategiesAsync();
                    break;
                case SecNetting:
                    await LoadNettingAsync();
                    break;
                case SecOrders:
                    await LoadOrdersAsync();
                    break;
                case SecIncubation:
                    await LoadIncubationAsync();
                    break;
                case SecMigration:
                    await LoadMigrationAsync();
                    break;
                case SecSettings:
                    // the editor is not re-filled by the poll (it would discard typed values) — only on activation / refresh / account switch
                    if (force || _settings == null || _settings.AccountId != _accountId) await LoadSettingsAsync();
                    break;
            }
        }

        /// <summary>Re-renders a section only when its data changed (keeps focus, expanders and scroll position otherwise).</summary>
        private void RenderIfChanged(string key, object payload, Action render)
        {
            string sig;
            try { sig = JsonSerializer.Serialize(payload); }
            catch (Exception) { sig = Guid.NewGuid().ToString("N"); }
            if (_signatures.TryGetValue(key, out var old) && old == sig) return;
            _signatures[key] = sig;
            render();
        }

        private void ShowSectionMessage(string text)
        {
            var panel = PanelOf(_section);
            panel.Children.Clear();
            panel.Children.Add(ErrorBlock(text));
        }

        private StackPanel PanelOf(string section) => section switch
        {
            SecStrategies => _strategiesPanel,
            SecNetting => _nettingPanel,
            SecOrders => _ordersPanel,
            SecIncubation => _incubationPanel,
            SecMigration => _migrationPanel,
            SecSettings => _settingsPanel,
            _ => _portfolioPanel,
        };

        private async Task SelectSectionAsync(string section)
        {
            if (_section == section && _scroll.Content == PanelOf(section)) return;
            _section = section;
            _scroll.Content = PanelOf(section);
            _scroll.ScrollToTop();
            if (_activated) await LoadSectionAsync(section, force: true);
        }

        // =====================================================================================
        // Header: notice, coexistence, kill-switch scope, auto trading, runtime, banners
        // =====================================================================================

        private void RenderHeader()
        {
            var s = _state;
            if (s == null) return;
            _notice.Text = string.IsNullOrWhiteSpace(s.Notice) ? "Paper trading only · simulated money" : s.Notice!;
            AutomationProperties.SetName(_notice, _notice.Text);

            _coexistHost.Children.Clear();
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(14) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            // old engine's account
            var legacy = s.LegacyAccount;
            var left = new StackPanel();
            var lHead = new WrapPanel();
            lHead.Children.Add(FundUi.Text("Old engine account (Overview)  ", 11, FundPalette.Text, bold: true, wrap: false));
            var (lChip, lBrush, lText) = (legacy?.Status) switch
            {
                "migrated" => ("MIGRATED · OWNER: PLATFORM", FundPalette.Focus,
                               "Migrated on " + FundFormat.LocalTime(legacy!.MigratedAt, "yyyy-MM-dd HH:mm") + (legacy.MigrationIdText.Length > 0 ? " (migration " + legacy.MigrationIdText + ")" : "")
                               + ". It is traded in the platform account paper-council now (source \"Council of AIs\"). The old engine only analyses (advisory) — its autopilot, manual orders and Execute are refused."),
                "migrating" => ("MIGRATING · FROZEN", FundPalette.Risk,
                                "The migration is running — the old account is frozen (no trading) until it is copied and marked migrated (or un-frozen if the copy fails)."),
                "active" => ("ACTIVE · OWNER: OLD ENGINE", FundPalette.Long,
                             "The old engine trades its own paper account exactly as before (Overview). The platform never trades it."),
                _ => ("UNKNOWN", FundPalette.Faint, "The backend did not report the old account's owner."),
            };
            lHead.Children.Add(Chip(lChip, lBrush));
            left.Children.Add(lHead);
            left.Children.Add(Line(lText, 10.5, FundPalette.Dim));
            grid.Children.Add(left);

            // platform accounts
            var right = new StackPanel();
            Grid.SetColumn(right, 2);
            var rHead = new WrapPanel();
            rHead.Children.Add(FundUi.Text("Platform accounts  ", 11, FundPalette.Text, bold: true, wrap: false));
            var accounts = Accounts();
            if (accounts.Count == 0) rHead.Children.Add(Chip("NONE OPEN", FundPalette.Faint));
            foreach (var a in accounts)
            {
                rHead.Children.Add(Chip((a.Id ?? "?").ToUpperInvariant() + (a.Halted ? " · HALTED" : ""), a.Halted ? FundPalette.Kill : FundPalette.Long));
                rHead.Children.Add(new Border { Width = 4 });
            }
            right.Children.Add(rHead);
            right.Children.Add(Line("One owner per account: the old engine and the platform runtime never trade the same account. paper-main is opened by you; "
                                    + "paper-council exists only after the migration of the old account; alpaca-paper / binance-demo appear once you connect "
                                    + "them in Settings → Brokers (paper / demo accounts at the broker).", 10.5, FundPalette.Dim));
            grid.Children.Add(right);
            _coexistHost.Children.Add(grid);

            // kill switch scope (the button itself stays pinned in the toolbar — not duplicated here)
            var hb = s.AutoTrade?.HeartbeatMinutes;
            var kill = new Border
            {
                Background = FundPalette.KillSurface,
                BorderBrush = FundPalette.Kill,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 5, 10, 6),
                Margin = new Thickness(0, 8, 0, 0),
            };
            var killText = "■ The kill switch in the toolbar (top right) covers BOTH engines: it halts the old engine and closes its positions, and in every "
                           + "platform account (" + (accounts.Count == 0 ? "none open yet" : string.Join(", ", accounts.Select(a => a.Id))) + ") it cancels all working "
                           + "orders, closes all positions and switches auto trading off — on a connected broker account it cancels every open order AT THE "
                           + "BROKER and closes the positions the broker reports. Each half reports its own result. "
                           + "Dead-man switch: one heartbeat for both — auto trading and the old autopilot switch off after "
                           + (hb is double m ? MinutesLabel(m) : "the configured time") + " without contact from UFOS.ai.";
            kill.Child = FundUi.Text(killText, 10.5, KillText);
            AutomationProperties.SetName(kill, killText);
            _coexistHost.Children.Add(kill);

            // auto trading
            var at = s.AutoTrade;
            var anyHalted = accounts.Any(a => a.Halted);
            _autoTrade.IsEnabled = !_busy && at != null && (at.Enabled || (!anyHalted && accounts.Count > 0));
            if (at == null)
            {
                _autoTrade.Content = "Auto trading: —";
                _autoTradeText.Text = "Not reported by the backend.";
            }
            else if (at.Enabled)
            {
                _autoTrade.Content = "Auto trading: ON — switch off";
                _autoTrade.Background = FocusSurface;
                _autoTrade.BorderBrush = FundPalette.Focus;
                _autoTrade.Foreground = FundPalette.TextBright;
                _autoTradeText.Text = "The runtime sends simulated paper orders for incubating strategies within the risk limits."
                                      + (at.OffAt != null ? " Switches itself off at " + FundFormat.LocalTime(at.OffAt, "HH:mm:ss") + " without contact (heartbeat)." : "")
                                      + (at.LastClientSeen != null ? " Last contact " + FundFormat.LocalTime(at.LastClientSeen, "HH:mm:ss") + "." : "");
                _autoTradeText.Foreground = FundPalette.Dim;
            }
            else
            {
                _autoTrade.Content = "Auto trading: OFF — switch on…";
                _autoTrade.Background = FundPalette.ButtonBg;
                _autoTrade.BorderBrush = FundPalette.ButtonBorder;
                _autoTrade.Foreground = FundPalette.Text;
                var by = at.ChangedBy == "engine" ? " — switched off by the engine (heartbeat / kill)" : at.ChangedBy == "system" ? " — off after a backend start" : "";
                _autoTradeText.Text = "Off" + by + (at.ChangedAt != null ? " since " + FundFormat.LocalTime(at.ChangedAt, "MM-dd HH:mm") : "")
                                      + ". The runtime still decides and records targets but sends no orders (incubation sessions count as missed)."
                                      + (anyHalted ? " Resume the halted account first." : accounts.Count == 0 ? " Open a platform account first." : "");
                _autoTradeText.Foreground = at.ChangedBy == "engine" ? FundPalette.Risk : FundPalette.Dim;
            }
            AutomationProperties.SetName(_autoTrade, at?.Enabled == true ? "Auto trading is on. Switch it off." : "Auto trading is off. Switch it on (asks for confirmation).");
            AutomationProperties.SetHelpText(_autoTrade, _autoTradeText.Text);

            // runtime
            var r = s.Runtime;
            if (r == null) _runtimeText.Text = "Runtime: not reported.";
            else
            {
                var txt = "Runtime: " + (r.Running == true ? (r.CycleInProgress == true ? "cycle running" : "running") : "NOT RUNNING")
                          + " · last cycle " + FundFormat.LocalTime(r.LastCycleAt, "HH:mm:ss") + " · next " + FundFormat.LocalTime(r.NextCycleAt, "HH:mm:ss");
                if (!string.IsNullOrWhiteSpace(r.LastError)) txt += " · last error: " + r.LastError;
                _runtimeText.Text = txt;
                _runtimeText.Foreground = r.Running != true || !string.IsNullOrWhiteSpace(r.LastError) ? FundPalette.Risk : FundPalette.Dim;
            }
            AutomationProperties.SetName(_runtimeText, _runtimeText.Text);
            _runNow.IsEnabled = !_busy && r?.Running == true;
        }

        private void RenderBanners()
        {
            _bannerHost.Children.Clear();
            if (_connectionProblem != null)
            {
                var text = "Connection lost (attempt " + _connectionAttempts.ToString(Inv) + ") — showing the last known state, retrying every 5 s. " + _connectionProblem;
                _bannerHost.Children.Add(Callout(text, FundPalette.Risk, RiskSurface));
            }
            foreach (var a in Accounts())
            {
                if (a.Valuation is { Ok: false } v)
                    _bannerHost.Children.Add(Callout(a.Id + ": " + (v.Message ?? "valuation paused") + " — values are not shown until a current quote exists (no stored-price valuation).",
                                                     FundPalette.Risk, RiskSurface));
            }
        }

        private async Task ToggleAutoTradeAsync()
        {
            var at = _state?.AutoTrade;
            if (at == null) return;
            var wanted = !at.Enabled;
            if (wanted)
            {
                var limits = string.Join("\n", Accounts().Select(a => "• " + a.Id + ": daily loss " + FundFormat.Pct(a.DayLoss?.Equity?.LimitPct, 1)
                                                                      + " · drawdown kill " + FundFormat.Pct(a.Drawdown?.KillPct, 1)));
                if (!FundDialogs.Confirm(Owner, "Switch auto trading on?",
                        "The platform runtime will send simulated paper orders on its own for every incubating strategy (allocation, netting, "
                        + "pre-trade checks), within these limits:\n" + limits + "\n\n"
                        + "It switches itself off with the kill switch, a drawdown kill, every backend start and after "
                        + (at.HeartbeatMinutes is double m ? MinutesLabel(m) : "the heartbeat time") + " without contact from UFOS.ai (dead-man switch). "
                        + "The kill switch in the toolbar stays available at all times.",
                        "Switch on", footnote: "Automated trading on your own paper account under the limits you set. Paper only — no real money."))
                    return;
            }
            _busy = true;
            _autoTrade.IsEnabled = false;
            try
            {
                var res = await PlatformClient.SetAutoTradeAsync(wanted);
                if (!res.Ok) FundDialogs.Info(Owner, "Auto trading", "Auto trading was not changed: " + PlatformClient.DescribeFailure(res));
            }
            finally { _busy = false; }
            await RefreshAsync(force: false);
        }

        private async Task RunCycleAsync()
        {
            _runNow.IsEnabled = false;
            var res = await PlatformClient.RunRuntimeCycleAsync();
            if (!res.Ok) FundDialogs.Info(Owner, "Run cycle now", "The cycle was not queued: " + PlatformClient.DescribeFailure(res));
            await Task.Delay(1500);
            await RefreshAsync(force: false);
        }

        // =====================================================================================
        // Portfolio section
        // =====================================================================================

        private void RenderPortfolio()
        {
            var p = _portfolioPanel;
            p.Children.Clear();
            var accounts = Accounts();
            if (accounts.Count == 0)
            {
                var box = new StackPanel();
                box.Children.Add(FundUi.Text("No platform paper account yet", 12.5, FundPalette.TextBright, bold: true));
                box.Children.Add(Line("Open paper-main to incubate validated strategies on paper (internal paper broker: same fees, slippage, margin, borrow "
                                      + "and crypto rules as the old engine and the backtest). The old account in the Overview is not affected; "
                                      + "paper-council appears only after its migration (Migration tab).", 11, FundPalette.Dim));
                if (_state?.Can?.OpenAccount == true)
                {
                    var open = SmallButton("Open platform paper account…", "Open the platform paper account paper-main");
                    open.Click += async (_, _) => await OpenAccountAsync();
                    box.Children.Add(open);
                }
                p.Children.Add(FundUi.Panel(box));
            }
            foreach (var a in accounts) p.Children.Add(AccountCard(a));
            if (accounts.Count > 0 && _state?.Can?.OpenAccount == true)
            {
                var open = SmallButton("Open platform paper account paper-main…", "Open the platform paper account paper-main");
                open.Click += async (_, _) => await OpenAccountAsync();
                p.Children.Add(open);
            }
            p.Children.Add(CyclesPanel());
        }

        private FrameworkElement AccountCard(TradingAccountSummary a)
        {
            var id = a.Id!;
            // §18.14: binance-demo's book is in USDT — shown as USDT, never converted or relabelled as USD
            var usdtBook = id == PlatformClient.BinanceDemoId;
            string Money(double? v, bool signed = false) => usdtBook ? BrokerText.Usdt(v, signed) : FundFormat.Money(v, signed: signed);
            var stack = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text((string.IsNullOrWhiteSpace(a.Label) ? id : a.Label!) + "  ", 13, FundPalette.TextBright, bold: true, wrap: false));
            head.Children.Add(FundUi.Text(id + "  ", 11, FundPalette.Faint, mono: true, wrap: false));
            head.Children.Add(Chip("PAPER", FundPalette.Neutral));
            head.Children.Add(new Border { Width = 4 });
            head.Children.Add(Chip(a.Origin == "migrated" ? "MIGRATED" : (a.Origin ?? "—").ToUpperInvariant(), FundPalette.Faint));
            head.Children.Add(new Border { Width = 4 });
            head.Children.Add(a.Halted ? Chip("HALTED · " + HaltKindLabel(a.Halt?.Kind).ToUpperInvariant(), FundPalette.Kill) : Chip("TRADING ALLOWED", FundPalette.Long));
            if (a.Valuation is { Ok: false })
            {
                head.Children.Add(new Border { Width = 4 });
                head.Children.Add(Chip("VALUATION PAUSED", FundPalette.Risk));
            }
            if (a.IsBrokerAccount)
            {
                // stage 8 (SPEC §18.10): link + reconciliation of a connected broker account
                var (lText, lBrush) = BrokerText.LinkChip(a.BrokerLink);
                head.Children.Add(new Border { Width = 4 });
                head.Children.Add(Chip(lText, lBrush));
                var (rText, rBrush) = BrokerText.ReconcileChip(a.Reconciliation, a.Halt);
                head.Children.Add(new Border { Width = 4 });
                head.Children.Add(Chip(rText, rBrush));
            }
            stack.Children.Add(head);
            stack.Children.Add(Line("Opened " + TradingText.Ts(a.OpenedAt) + " · broker " + (a.Broker ?? "—") + " · " + (a.PositionsCount?.ToString(Inv) ?? "—")
                                    + " positions · " + (a.WorkingOrders?.ToString(Inv) ?? "—") + " working orders", 10.5, FundPalette.Faint));
            if (a.IsBrokerAccount)
            {
                if (!string.IsNullOrWhiteSpace(a.BrokerLink?.Message))
                    stack.Children.Add(Line(a.BrokerLink!.Message!, 10.5, a.BrokerLink.State == "ok" ? FundPalette.Dim : FundPalette.Risk));
                if (!string.IsNullOrWhiteSpace(a.Reconciliation?.Reason))
                    stack.Children.Add(Line("Reconciliation: " + a.Reconciliation!.Reason!, 10.5, a.Reconciliation.Status == "unreachable" ? FundPalette.Risk : FundPalette.Kill));
                stack.Children.Add(Line("This account mirrors your " + (a.Id == PlatformClient.KrakenDemoId ? "Kraken Futures DEMO" : a.Id == PlatformClient.BinanceDemoId ? "Binance Spot DEMO (amounts in USDT — not converted)" : "Alpaca PAPER")
                                        + " account — the broker is the truth; its numbers here are the platform's expected book, reconciled with the broker. "
                                        + "Connection, Resolve… and the per-broker kill: Settings → Brokers.", 10.5, FundPalette.Faint));
            }

            if (a.Halted)
            {
                var halt = new StackPanel();
                halt.Children.Add(FundUi.Text("■ Halted (" + HaltKindLabel(a.Halt?.Kind) + ") since " + TradingText.Ts(a.Halt?.At) + " — " + (a.Halt?.Reason ?? "no reason given"), 11.5, KillText));
                halt.Children.Add(FundUi.Text(a.Halt?.Kind == "reconcile"
                                              ? "No new orders at all on this account (also no reducing ones — the expected book disagrees with the broker); only the kill switch still works. "
                                                + "Positions and orders are NOT touched by this halt."
                                              : "No new orders; working orders were cancelled and positions closed (protective orders queue while the market is closed). "
                                                + "Auto trading stays off after a resume.", 10.5, FundPalette.Dim));
                if (a.Halt?.Kind == "reconcile")
                {
                    // a reconcile halt is cleared only by Resolve… in Settings → Brokers (POST /v2/oms/resume answers 409 reconcile_mismatch)
                    halt.Children.Add(FundUi.Text("Reconciliation mismatch — nothing is corrected automatically. Resolve it in Settings → Brokers.", 10.5, FundPalette.Dim));
                    var resolve = SmallButton("Resolve in Settings → Brokers…", "Open Settings, Brokers to resolve the reconciliation of " + id);
                    resolve.Click += (_, _) => BrokersRequested?.Invoke();
                    halt.Children.Add(resolve);
                }
                else
                {
                    var resume = InFlightButton(SmallButton("Resume " + id + "…", "Resume the halted account " + id), "resume:" + id, () => ResumeAsync(id));
                    halt.Children.Add(resume);
                }
                stack.Children.Add(new Border
                {
                    Background = FundPalette.KillSurface,
                    BorderBrush = FundPalette.Kill,
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(6),
                    Padding = new Thickness(10, 6, 10, 6),
                    Margin = new Thickness(0, 6, 0, 4),
                    Child = halt,
                });
            }

            var v = a.Account;
            var cols = new Grid { Margin = new Thickness(0, 8, 0, 0) };
            for (var i = 0; i < 3; i++)
            {
                cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                if (i < 2) cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });
            }
            var c1 = new StackPanel();
            c1.Children.Add(FundUi.KeyValue("Total equity", Money(v?.Equity), FundPalette.TextBright));
            c1.Children.Add(FundUi.KeyValue("Cash", Money(v?.Cash)));
            c1.Children.Add(FundUi.KeyValue("Buying power", Money(v?.BuyingPower)));
            c1.Children.Add(FundUi.KeyValue("Starting capital", Money(v?.StartingCapital)));
            var c2 = new StackPanel();
            c2.Children.Add(FundUi.KeyValue("Long market value", Money(v?.LongMv)));
            c2.Children.Add(FundUi.KeyValue("Short market value", Money(v?.ShortMv)));
            c2.Children.Add(FundUi.KeyValue("Gross exposure", FundFormat.Pct(v?.GrossPct, 1) + " of equity"));
            c2.Children.Add(FundUi.KeyValue("Net exposure", FundFormat.Pct(v?.NetPct, 1, signed: true) + " of equity"));
            var c3 = new StackPanel();
            c3.Children.Add(FundUi.KeyValue("Day P&L", Money(v?.DayPnl, signed: true), FundPalette.ForSigned(v?.DayPnl)));
            c3.Children.Add(FundUi.KeyValue("Total P&L", Money(v?.TotalPnl, signed: true) + " (" + FundFormat.Pct(v?.TotalPnlPct, 2, signed: true) + ")", FundPalette.ForSigned(v?.TotalPnl)));
            c3.Children.Add(FundUi.KeyValue("Peak equity", Money(v?.PeakEquity)));
            c3.Children.Add(FundUi.KeyValue("Margin excess", Money(v?.MarginExcess)));
            if (v?.FundingTotal is double ft && Math.Abs(ft) > 0.004) c3.Children.Add(FundUi.KeyValue("Crypto funding", FundFormat.Funding(ft)));
            cols.Children.Add(c1);
            Grid.SetColumn(c2, 2);
            cols.Children.Add(c2);
            Grid.SetColumn(c3, 4);
            cols.Children.Add(c3);
            stack.Children.Add(cols);
            if (v == null)
                stack.Children.Add(Line("Not valued: " + (a.Valuation?.Message ?? "no current quote") + " — the numbers stay empty until the runtime has a current quote for every held position.", 10.5, FundPalette.Risk));

            // ----- limits: daily loss (equity NY day, crypto UTC day) + drawdown -----
            stack.Children.Add(SubHead("Hard limits (post-trade)"));
            // retest 2026-10-23: binance-demo trades crypto spot only (§18.14) — its equities row is not shown. The backend sends both
            // day-loss blocks for every account (no per-account asset scope in /v2/portfolio/state).
            if (a.Id != PlatformClient.BinanceDemoId)
                stack.Children.Add(DayLossGauge("Daily loss · equities (New York day)", a.DayLoss?.Equity));
            stack.Children.Add(DayLossGauge("Daily loss · crypto (UTC day)", a.DayLoss?.Crypto));
            stack.Children.Add(DrawdownGauge(a));

            // ----- positions -----
            _details.TryGetValue(id, out var detail);
            stack.Children.Add(SubHead("Positions"));
            if (_detailErrors.TryGetValue(id, out var derr)) stack.Children.Add(ErrorBlock(derr));
            var positions = detail?.Positions ?? new List<TradingPosition>();
            if (positions.Count == 0) stack.Children.Add(Line(detail == null ? "Loading…" : "No positions.", 11, FundPalette.Faint));
            else
            {
                var widths = new double[] { 90, 50, 80, 80, 80, 95, 60, 90, 0 };
                stack.Children.Add(Row(widths, new[] { "Symbol", "Side", "Qty", "Avg", "Last", "Market value", "Weight", "Unrealised", "By source" }, header: true));
                var names = SourceNames(detail);
                foreach (var pos in positions.OrderBy(x => x.Symbol, StringComparer.Ordinal))
                {
                    var by = string.Join(", ", (pos.BySource ?? new List<TradingSourceQty>()).Where(b => b.Qty is double q && Math.Abs(q) > 1e-12)
                                                                                           .Select(b => SourceName(names, b.SourceId) + " " + FundFormat.Qty(b.Qty)));
                    stack.Children.Add(Row(widths, new[]
                    {
                        (pos.Symbol ?? "?") + (pos.Instrument == "perp" ? " (perp)" : ""), pos.Side ?? "—", FundFormat.Qty(pos.Qty), FundFormat.Num(pos.AvgPrice, 2),
                        FundFormat.Num(pos.LastPrice, 2), Money(pos.MarketValue), FundFormat.Fraction(pos.Weight, 1), Money(pos.UnrealizedPnl, signed: true),
                        by.Length == 0 ? "—" : by,
                    }, brushes: new Brush?[] { FundPalette.TextBright, pos.Side == "short" ? FundPalette.Short : FundPalette.Long, null, null, null, null, null, FundPalette.ForSigned(pos.UnrealizedPnl), FundPalette.Dim }));
                }
            }
            var working = detail?.WorkingOrders ?? new List<OmsOrder>();
            if (working.Count > 0)
            {
                stack.Children.Add(SubHead("Working orders"));
                foreach (var o in working) stack.Children.Add(Line(TradingText.TsShort(o.CreatedAt) + "  " + TradingText.OrderLine(o) + "  · " + o.ClientOrderId, 10.5, FundPalette.Dim, mono: true));
            }

            var buttons = new WrapPanel { Margin = new Thickness(0, 6, 0, 0) };
            var order = SmallButton("New paper order…", "New manual paper order on " + id + " (Ctrl+N)");
            order.IsEnabled = !a.Halted;
            order.ToolTip = a.Halted ? "The account is halted — resume it first." : "A manual simulated market order through the OMS (Ctrl+N)";
            order.Click += async (_, _) => await NewOrderAsync(id, null, null, null);
            buttons.Children.Add(order);
            var orders = SmallButton("Orders of " + id, "Show the order blotter of " + id);
            orders.Click += async (_, _) => { _orderAccount = id; SelectTab(SecOrders); await Task.CompletedTask; };
            buttons.Children.Add(orders);
            var strategies = SmallButton("Strategies of " + id, "Show the strategy sub-ledgers of " + id);
            strategies.Click += async (_, _) => { _accountId = id; SelectTab(SecStrategies); await Task.CompletedTask; };
            buttons.Children.Add(strategies);
            stack.Children.Add(buttons);

            var card = FundUi.Panel(stack);
            AutomationProperties.SetName(card, "Account " + id + ", equity " + Money(v?.Equity) + (a.Halted ? ", halted" : ""));
            return card;
        }

        private static string HaltKindLabel(string? kind) => kind switch
        {
            "kill" => "kill switch",
            "drawdown" => "drawdown kill",
            "reconcile" => "reconciliation mismatch",
            null or "" => "halt",
            _ => kind,
        };

        /// <summary>Daily loss vs the limit: OK below half the limit, NEAR LIMIT from half, BLOCKED when the backend says so (until the next day).</summary>
        private static FrameworkElement DayLossGauge(string title, TradingDayLossClass? d)
        {
            var limit = d?.LimitPct;
            var pnl = d?.PnlPct;
            var loss = pnl is double x ? Math.Max(0, -x) : (double?)null;
            string chip;
            Brush brush;
            if (d == null) { chip = "NOT REPORTED"; brush = FundPalette.Faint; }
            else if (d.Blocked) { chip = (d.Latched == true ? "BLOCKED (HOLDS) until " : "BLOCKED until ") + FundFormat.LocalTime(d.Until, "ddd HH:mm"); brush = FundPalette.Kill; }
            else if (pnl == null) { chip = "NO REFERENCE YET"; brush = FundPalette.Faint; }
            else if (limit is double l && l > 0 && loss >= l * 0.5) { chip = "NEAR LIMIT"; brush = FundPalette.Risk; }
            else { chip = "OK"; brush = FundPalette.Long; }
            var value = "today " + FundFormat.Pct(pnl, 2, signed: true) + " vs limit " + (limit is double lim ? FundFormat.Pct(-lim, 1) : FundFormat.Dash)
                        + (d?.ReferenceEquity is double re ? " · reference equity " + FundFormat.Money(re) : "");
            var help = "At the limit no new risk-increasing orders until the next day; reducing and protective orders still pass.";
            return Gauge(title, loss, limit, value, chip, brush, help);
        }

        private static FrameworkElement DrawdownGauge(TradingAccountSummary a)
        {
            var dd = a.Drawdown?.Pct;
            var kill = a.Drawdown?.KillPct;
            string chip;
            Brush brush;
            if (a.Halted && a.Halt?.Kind == "drawdown") { chip = "KILLED · HALTED"; brush = FundPalette.Kill; }
            else if (a.Halted) { chip = "HALTED (" + HaltKindLabel(a.Halt?.Kind) + ")"; brush = FundPalette.Kill; }
            else if (dd == null) { chip = "NOT VALUED"; brush = FundPalette.Faint; }
            else if (kill is double k && k > 0 && dd >= k * 0.5) { chip = "NEAR LIMIT"; brush = FundPalette.Risk; }
            else { chip = "OK"; brush = FundPalette.Long; }
            var value = FundFormat.Pct(dd, 2) + " from peak vs kill at " + FundFormat.Pct(kill, 1)
                        + (a.Account?.PeakEquity is double pk ? " · peak " + FundFormat.Money(pk) : "");
            var help = "At the limit the account is KILLED: halt, cancel all, close all, auto trading off; Resume rebases the peak.";
            return Gauge("Drawdown from peak", dd, kill, value, chip, brush, help);
        }

        private static FrameworkElement Gauge(string title, double? value, double? limit, string valueText, string chip, Brush brush, string help)
        {
            var stack = new StackPanel { Margin = new Thickness(0, 4, 0, 6) };
            var head = new Grid();
            head.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            head.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var t = FundUi.Text(title + " · " + valueText, 11, FundPalette.Text, wrap: false);
            t.ToolTip = title + " · " + valueText + "\n" + help;
            head.Children.Add(t);
            var c = Chip(chip, brush);
            Grid.SetColumn(c, 1);
            head.Children.Add(c);
            stack.Children.Add(head);
            var frac = value is double v && limit is double l && l > 0 ? Math.Clamp(v / l, 0, 1) : 0;
            var bar = new Grid { Height = 7, Margin = new Thickness(0, 4, 0, 0) };
            bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(frac, GridUnitType.Star) });
            bar.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1 - frac, GridUnitType.Star) });
            var track = new Border { Background = FundPalette.Hairline, CornerRadius = new CornerRadius(3) };
            Grid.SetColumnSpan(track, 2);
            bar.Children.Add(track);
            var fill = new Border { Background = brush, CornerRadius = new CornerRadius(3) };
            bar.Children.Add(fill);
            // half-limit tick (where NEAR LIMIT starts)
            var tick = new Border { Width = 1, Background = FundPalette.Faint, HorizontalAlignment = HorizontalAlignment.Left };
            var tickHost = new Grid();
            tickHost.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            tickHost.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetColumn(tick, 1);
            tickHost.Children.Add(tick);
            Grid.SetColumnSpan(tickHost, 2);
            bar.Children.Add(tickHost);
            stack.Children.Add(bar);
            var name = title + ": " + valueText + ". State: " + chip + ". " + help;
            AutomationProperties.SetName(stack, name);
            AutomationProperties.SetItemStatus(stack, chip);
            stack.Focusable = true;   // keyboard users can reach the gauge and hear its state
            stack.FocusVisualStyle = null;
            stack.ToolTip = name;
            return stack;
        }

        private FrameworkElement CyclesPanel()
        {
            var exp = new Expander
            {
                Header = FundUi.Text("Runtime cycles (last " + (_runtime?.Cycles?.Count ?? 0).ToString(Inv) + ", kept in the backend's memory)", 11.5, FundPalette.Text, bold: true, wrap: false),
                Foreground = FundPalette.Text,
                Margin = new Thickness(0, 4, 0, 4),
                IsExpanded = _cyclesExpanded,
            };
            exp.Expanded += (_, _) => _cyclesExpanded = true;
            exp.Collapsed += (_, _) => _cyclesExpanded = false;
            AutomationProperties.SetName(exp, "Runtime cycles");
            var list = new StackPanel { Margin = new Thickness(16, 4, 0, 0) };
            var cycles = _runtime?.Cycles ?? new List<TradingRuntimeCycle>();
            if (cycles.Count == 0) list.Children.Add(Line("No cycle since the backend started.", 10.5, FundPalette.Faint));
            foreach (var c in cycles.Take(50))
            {
                var summary = c.Summary is JsonElement s && s.ValueKind == JsonValueKind.Object ? FundFormat.Plain(s) : "";
                var brush = c.Status switch { "ok" => FundPalette.Dim, "skipped" => FundPalette.Faint, "partial" => FundPalette.Risk, "error" => FundPalette.Kill, _ => FundPalette.Dim };
                list.Children.Add(Line(TradingText.TsShort(c.StartedAt) + " → " + FundFormat.LocalTime(c.FinishedAt, "HH:mm:ss") + "  " + (c.Status ?? "?") + "  " + summary, 10.5, brush, mono: true));
            }
            exp.Content = list;
            return exp;
        }

        private bool _cyclesExpanded;

        private async Task OpenAccountAsync()
        {
            var values = FundDialogs.Form(Owner, "Open platform paper account",
                "Opens paper-main, the platform's internal paper account (simulated; same broker rules as the old engine and the backtest). "
                + "Incubating strategies trade here. The old account in the Overview is not affected.",
                new[] { ("Starting capital (USD, 1,000 – 100,000,000)", "100000"), ("Label (optional, ≤ 60 characters)", "") },
                "Open account", footnote: "Paper trading only — no real money.");
            if (values == null) return;
            if (!TradingUi.TryParseNumber(values[0], out var capital) || capital < 1000 || capital > 100_000_000)
            {
                FundDialogs.Info(Owner, "Open platform paper account", "The starting capital must be a number from 1,000 to 100,000,000.");
                return;
            }
            var label = values.Length > 1 ? values[1].Trim() : "";
            if (label.Length > 60) label = label[..60];
            var res = await PlatformClient.OpenTradingAccountAsync(capital, label.Length == 0 ? null : label);
            if (!res.Ok)
            {
                FundDialogs.Info(Owner, "Open platform paper account", PlatformClient.DescribeFailure(res));
                await RefreshAsync(force: true);
                return;
            }
            SelectTab(SecPortfolio);
            // review 2026-10-22: wait for THIS account (other broker accounts may already be listed), or for the state saying it exists
            var openedId = res.Value?.Account?.Id;
            await RefreshUntilAsync(() => (openedId != null && Accounts().Any(a => a.Id == openedId)) || _state?.Can?.OpenAccount == false,
                                    "the opened account " + (openedId ?? "(no id in the answer)") + " is not in /v2/portfolio/state yet — press ↻ Refresh");
        }

        private async Task ResumeAsync(string accountId)
        {
            var reason = TradingReasonDialog.Ask(Owner, "Resume " + accountId,
                "Clears the halt of " + accountId + " so orders may be placed again. Auto trading stays OFF — switch it on explicitly. "
                + "The drawdown peak is rebased to the current equity (otherwise the drawdown kill would fire again at once). "
                + "Refused while the account cannot be valued.", "Resume " + accountId + " (auto trading stays off)");
            if (reason == null) return;
            var res = await PlatformClient.ResumeTradingAccountAsync(accountId, reason);
            if (res.TimedOut)
                FundDialogs.Info(Owner, "Resume " + accountId, "No answer in time — the resume may have been applied. The view reloads now; check the account's state "
                                                              + "before trying again.");
            else if (!res.Ok) FundDialogs.Info(Owner, "Resume " + accountId, PlatformClient.DescribeFailure(res));
            await RefreshAsync(force: true);
        }

        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        /// <summary>
        /// S10 review C1: Market Research's Buy / Sell / Short / Cover hand-over opens THIS order dialog (POST /v2/oms/orders) pre-filled
        /// with symbol and side — the old engine's /fund/orders panel is no longer used. Loads the accounts first when needed.
        /// </summary>
        internal async Task OpenOrderFromResearchAsync(string symbol, string side)
        {
            if (_state == null) await RefreshAsync(force: true);
            await NewOrderAsync(null, symbol, side, null);
        }

        private Task NewOrderAsync(string? accountId, string? symbol, string? side, double? qty) => DialogGuard.RunAsync("trading-new-order", () => NewOrderCoreAsync(accountId, symbol, side, qty));

        private async Task NewOrderCoreAsync(string? accountId, string? symbol, string? side, double? qty)
        {
            var accounts = Accounts();
            accountId ??= accounts.FirstOrDefault()?.Id;
            var acc = Account(accountId);
            if (acc == null)
            {
                FundDialogs.Info(Owner, "New paper order", "No platform paper account is open — open paper-main in the Portfolio tab first.");
                return;
            }
            if (acc.Halted)
            {
                FundDialogs.Info(Owner, "New paper order", acc.Id + " is halted (" + HaltKindLabel(acc.Halt?.Kind) + ") — resume it first.");
                return;
            }
            if (!_details.ContainsKey(acc.Id!)) await LoadDetailAsync(acc.Id!);
            _details.TryGetValue(acc.Id!, out var detail);
            var dialog = new TradingOrderDialog(Owner, acc.Id!, detail?.Sources ?? new List<TradingSource>(), symbol, side, qty);
            if (dialog.ShowDialog() != true || dialog.Order == null)
            {
                if (dialog.SubmitTimedOut) await RefreshAsync(force: true);   // the order may exist although no answer arrived
                return;
            }
            var o = dialog.Order;
            var checks = (o.Checks ?? new List<OmsCheck>()).Select(c => (c.Ok == false ? "✗ " : "✓ ") + (c.Check ?? "?") + (string.IsNullOrWhiteSpace(c.Message) ? "" : ": " + c.Message)).ToList();
            FundDialogs.Info(Owner, o.State == "rejected" ? "Order rejected" : "Order placed",
                TradingText.OrderLine(o) + "\nClient order id: " + o.ClientOrderId
                + (checks.Count > 0 ? "\n\nPre-trade checks:\n" + string.Join("\n", checks) : ""));
            _orderAccount = acc.Id;
            _selectedOrderId = o.ClientOrderId;
            await RefreshAsync(force: true);
        }

        // =====================================================================================
        // Small UI helpers (plain text only)
        // =====================================================================================

        private static Dictionary<string, string> SourceNames(TradingAccountDetail? detail)
        {
            var names = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var s in detail?.Sources ?? new List<TradingSource>())
                if (!string.IsNullOrEmpty(s.SourceId)) names[s.SourceId!] = string.IsNullOrWhiteSpace(s.Name) ? s.SourceId! : s.Name!;
            return names;
        }

        private static string SourceName(Dictionary<string, string> names, string? id)
            => id == null ? "?" : names.TryGetValue(id, out var n) ? n : id == "_unallocated" ? "Unallocated" : id == "council-of-ais" ? "Council of AIs" : id;

        private void AddTab(Panel host, string key, string text, string name, string accelerator)
        {
            var rb = new RadioButton
            {
                GroupName = "TradingSections",
                Content = text,
                Style = TabStyle,
                IsChecked = key == SecPortfolio,
                Margin = new Thickness(0, 0, 6, 0),
                Padding = new Thickness(12, 4, 12, 5),
                FontSize = 11.5,
                Cursor = Cursors.Hand,
            };
            AutomationProperties.SetName(rb, name);
            AutomationProperties.SetAcceleratorKey(rb, accelerator);
            rb.Checked += async (_, _) => await SelectSectionAsync(key);
            _tabs.Add((rb, key));
            host.Children.Add(rb);
        }

        private void SelectTab(string key)
        {
            foreach (var (button, k) in _tabs)
                if (k == key) { button.IsChecked = true; return; }
        }

        private static Style? _tabStyle;

        private static Style TabStyle
        {
            get
            {
                if (_tabStyle != null) return _tabStyle;
                var bd = new FrameworkElementFactory(typeof(Border), "bd");
                bd.SetValue(Border.CornerRadiusProperty, new CornerRadius(6));
                bd.SetValue(Border.BackgroundProperty, Brushes.Transparent);
                bd.SetValue(Border.BorderBrushProperty, FundPalette.PanelBorder);
                bd.SetValue(Border.BorderThicknessProperty, new Thickness(1));
                var cp = new FrameworkElementFactory(typeof(ContentPresenter));
                cp.SetValue(FrameworkElement.MarginProperty, new TemplateBindingExtension(Control.PaddingProperty));
                cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Center);
                cp.SetValue(FrameworkElement.VerticalAlignmentProperty, VerticalAlignment.Center);
                bd.AppendChild(cp);
                var template = new ControlTemplate(typeof(RadioButton)) { VisualTree = bd };
                var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
                hover.Setters.Add(new Setter(Border.BackgroundProperty, FundPalette.Make(0xFF1C1C1C), "bd"));
                template.Triggers.Add(hover);
                var chk = new Trigger { Property = ToggleButton.IsCheckedProperty, Value = true };
                chk.Setters.Add(new Setter(Border.BackgroundProperty, FundPalette.Make(0xFF1F1F1F), "bd"));
                chk.Setters.Add(new Setter(Border.BorderBrushProperty, FundPalette.Focus, "bd"));
                template.Triggers.Add(chk);
                var focus = new Trigger { Property = UIElement.IsKeyboardFocusedProperty, Value = true };
                focus.Setters.Add(new Setter(Border.BorderBrushProperty, FundPalette.Focus, "bd"));
                focus.Setters.Add(new Setter(Border.BorderThicknessProperty, new Thickness(2), "bd"));
                template.Triggers.Add(focus);
                var style = new Style(typeof(RadioButton));
                style.Setters.Add(new Setter(Control.ForegroundProperty, FundPalette.Dim));
                style.Setters.Add(new Setter(Control.TemplateProperty, template));
                style.Setters.Add(new Setter(FrameworkElement.FocusVisualStyleProperty, null));
                var styleChecked = new Trigger { Property = ToggleButton.IsCheckedProperty, Value = true };
                styleChecked.Setters.Add(new Setter(Control.ForegroundProperty, FundPalette.TextBright));
                style.Triggers.Add(styleChecked);
                _tabStyle = style;
                return style;
            }
        }

        /// <summary>A table row: widths &gt; 0 are pixels, ≤ 0 is star. Cells are plain, trimmed text with the full text as tooltip.</summary>
        private static Grid Row(double[] widths, IReadOnlyList<string> cells, bool header = false, IReadOnlyList<Brush?>? brushes = null)
        {
            var g = new Grid { Margin = new Thickness(0, header ? 2 : 0, 0, header ? 2 : 0) };
            foreach (var w in widths)
                g.ColumnDefinitions.Add(new ColumnDefinition { Width = w > 0 ? new GridLength(w) : new GridLength(1, GridUnitType.Star) });
            for (var i = 0; i < cells.Count && i < widths.Length; i++)
            {
                var brush = header ? FundPalette.Faint : brushes != null && i < brushes.Count && brushes[i] != null ? brushes[i]! : FundPalette.Text;
                var t = FundUi.Text(cells[i], header ? 10 : 11, brush, bold: header, mono: !header, wrap: false);
                t.Margin = new Thickness(0, 1, 8, 1);
                t.ToolTip = cells[i];
                Grid.SetColumn(t, i);
                g.Children.Add(t);
            }
            AutomationProperties.SetName(g, string.Join(", ", cells));
            if (header) g.Children.Add(new Border { BorderBrush = FundPalette.Hairline, BorderThickness = new Thickness(0, 0, 0, 1), VerticalAlignment = VerticalAlignment.Bottom, IsHitTestVisible = false });
            if (header && widths.Length > 1) Grid.SetColumnSpan((UIElement)g.Children[g.Children.Count - 1], widths.Length);
            return g;
        }

        private static string MinutesLabel(double minutes)
        {
            var m = (int)Math.Round(minutes);
            if (m < 60) return m.ToString(Inv) + " min";
            return (m / 60).ToString(Inv) + " h" + (m % 60 == 0 ? "" : " " + (m % 60).ToString(Inv) + " min");
        }

        private static void Place(Grid grid, UIElement element, int row)
        {
            Grid.SetRow(element, row);
            grid.Children.Add(element);
        }

        private static TextBlock Line(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
            return t;
        }

        private static TextBlock SubHead(string text)
        {
            var t = FundUi.Text(text, 11, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 10, 0, 3);
            return t;
        }

        private static Border Chip(string text, Brush brush)
        {
            var b = new Border
            {
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(999),
                Padding = new Thickness(6, 0, 6, 1),
                VerticalAlignment = VerticalAlignment.Center,
                Child = FundUi.Text(text, 9.5, brush, bold: true, wrap: false),
            };
            AutomationProperties.SetName(b, text);
            return b;
        }

        private static Button SmallButton(string text, string name)
        {
            var b = FundUi.MakeButton(text);
            b.Padding = new Thickness(10, 2, 10, 2);
            b.MinHeight = 22;
            b.FontSize = 11;
            b.Margin = new Thickness(0, 4, 8, 0);
            b.HorizontalAlignment = HorizontalAlignment.Left;
            AutomationProperties.SetName(b, name);
            return b;
        }

        private static Border Callout(string text, Brush brush, Brush surface)
        {
            var b = new Border
            {
                Background = surface,
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 0, 0, 8),
                Child = FundUi.Text(text, 11, brush == FundPalette.Kill ? KillText : brush),
            };
            AutomationProperties.SetName(b, text);
            return b;
        }

        private static Border ErrorBlock(string message)
        {
            var b = new Border
            {
                Background = FundPalette.KillSurface,
                BorderBrush = FundPalette.Kill,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 4, 0, 8),
                Child = FundUi.Text(message, 11.5, KillText),
            };
            AutomationProperties.SetName(b, "Error: " + message);
            return b;
        }

        private static TextBox Selectable(string text)
        {
            var box = new TextBox
            {
                Text = text,
                IsReadOnly = true,
                BorderThickness = new Thickness(0),
                Background = Brushes.Transparent,
                Foreground = FundPalette.TextBright,
                FontFamily = FundUi.Mono,
                FontSize = 11.5,
                Padding = new Thickness(0),
                CaretBrush = FundPalette.Text,
            };
            AutomationProperties.SetName(box, text);
            return box;
        }
    }
}

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
    /// Hedge Fund → "Monitoring" (Strategy Platform stage 9 — SPEC §19.1–§19.5, exact JSON §19.11, backend notes §19.12, WPF
    /// notes §19.13). PAPER / DEMO ONLY. Sections (tabs, Ctrl+1…6; stage 9b adds Portfolio insights, SPEC §20):
    /// - Strategies: per incubating strategy the realised cumulative return vs the frozen expected band (band chart with
    ///   keyboard cursor), deviation statistics (z, rolling z, tracking error, vol / drawdown ratios, Sharpe), the decay state
    ///   and every rule's value vs threshold; a PAUSED strategy shows why (rule, numbers, the backend's reason verbatim) and
    ///   the user's decision "Keep running…" or "Demote…" (one confirmation step, optional note — fix1023) — the
    ///   backend never resumes or demotes on its own. Events and the newest checks per strategy.
    /// - Accounts &amp; brokers: equity, day P&amp;L, drawdown, halts, broker link and reconciliation status (verbatim), a link to
    ///   Keys &amp; data → Brokers, and the monitor task's status.
    /// - Alerts: the alert history (newest first) with severity, mark as seen (single / all), the toasts on/off setting;
    ///   opening an alert jumps to its strategy / account.
    /// - Reports: daily / weekly reports (numbers first, deterministic) + optional labelled AI commentary (budget key
    ///   "reports", numbers only, number check shown; the budget-exceeded message verbatim).
    /// - Tax reports: a fixed notice only (§19.15, user decision 2026-10-18 — UFOS.ai has NO tax export): the backend's wording
    ///   from GET /v2/monitoring/tax-notice, plain text; "notice not available (reason)" when it cannot be read.
    /// Every text from the backend is plain TextBlock.Text. Polls every 10 s only while visible (each GET touches the shared
    /// heartbeat); a lost connection keeps the last known state with "Connection lost (attempt n)". Toasts are posted by
    /// <see cref="MonitoringNotifier"/> (app-wide), not by this view.
    /// </summary>
    internal sealed partial class MonitoringView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(10);

        private const string SecStrategies = "strategies";
        private const string SecAccounts = "accounts";
        private const string SecAlerts = "alerts";
        private const string SecReports = "reports";
        private const string SecTax = "tax";
        private const string SecInsights = "insights";

        private readonly TextBlock _notice;
        private readonly TextBlock _monitorText;
        private readonly TextBlock _actionText;
        // fix1023 B4: the action line describes the state right after the action; it is cleared as soon as a later monitor
        // refresh shows a different strategy / decay state (e.g. "no longer paused" while the strategy is paused again).
        private bool _actionArmed;
        private string? _actionFingerprint;
        private readonly Button _runChecks;
        private readonly StackPanel _bannerHost;
        private readonly List<(RadioButton Button, string Key)> _tabs = new();
        private readonly ScrollViewer _scroll;
        private readonly DispatcherTimer _poll;

        private readonly StackPanel _strategiesPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _accountsPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _alertsPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _reportsPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _taxPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _insightsPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly Dictionary<string, string> _signatures = new(StringComparer.Ordinal);

        private MonOverview? _overview;
        private readonly Dictionary<string, MonStrategyDetail> _details = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _detailErrors = new(StringComparer.Ordinal);
        private readonly HashSet<string> _expanded = new(StringComparer.Ordinal);
        private string _section = SecStrategies;
        private string? _connectionProblem;
        private int _connectionAttempts;
        private int _seq;
        private bool _polling;
        private bool _stopped;
        private bool _busy;
        private bool _activated;

        /// <summary>"Keys &amp; data → Brokers…": the host opens Settings at its Brokers section.</summary>
        public event Action? BrokersRequested;

        public MonitoringView()
        {
            AutomationProperties.SetName(this, "Monitoring (paper and demo trading)");
            Focusable = false;
            var root = new Grid();
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            // ----- header -----
            var head = new StackPanel();
            var titleRow = new Grid();
            titleRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            titleRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            titleRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            titleRow.Children.Add(FundUi.Text("Monitoring", 14, FundPalette.TextBright, bold: true, wrap: false));
            _notice = FundUi.Text("Monitoring of paper/demo trading · simulated money", 10.5, FundPalette.Faint, wrap: false);
            _notice.VerticalAlignment = VerticalAlignment.Center;
            _notice.Margin = new Thickness(12, 0, 0, 0);
            Grid.SetColumn(_notice, 1);
            titleRow.Children.Add(_notice);
            var buttons = new StackPanel { Orientation = Orientation.Horizontal };
            _runChecks = MonUi.Small("Run decay checks now", "Run the decay checks now for newly completed sessions");
            _runChecks.ToolTip = "The monitor task runs the checks every 60 s by itself; this only runs them now (POST /v2/monitoring/decay/run).";
            _runChecks.Click += async (_, _) => await RunChecksAsync();
            buttons.Children.Add(_runChecks);
            var brokers = MonUi.Small("Brokers…", "Open Keys and data, Brokers: connection, reconciliation, routing");
            brokers.ToolTip = "Keys & data → Brokers (Ctrl+B): broker connection, reconciliation and Resolve…, routing";
            brokers.Click += (_, _) => BrokersRequested?.Invoke();
            buttons.Children.Add(brokers);
            var refresh = MonUi.Small("↻ Refresh", "Reload monitoring (F5)");
            refresh.Margin = new Thickness(0);
            refresh.Click += async (_, _) => await RefreshAsync(force: true);
            buttons.Children.Add(refresh);
            Grid.SetColumn(buttons, 2);
            titleRow.Children.Add(buttons);
            head.Children.Add(titleRow);
            _monitorText = MonUi.Line("Monitor: —", 10.5, FundPalette.Dim, mono: true);
            _monitorText.Margin = new Thickness(0, 6, 0, 0);
            AutomationProperties.SetLiveSetting(_monitorText, AutomationLiveSetting.Polite);
            head.Children.Add(_monitorText);
            _actionText = MonUi.Line("", 10.5, FundPalette.Dim);
            _actionText.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(_actionText, AutomationLiveSetting.Polite);
            head.Children.Add(_actionText);
            Place(root, FundUi.Panel(head, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 10, 8)), 0);

            _bannerHost = new StackPanel { Margin = new Thickness(0, 0, 10, 0) };
            AutomationProperties.SetLiveSetting(_bannerHost, AutomationLiveSetting.Assertive);
            AutomationProperties.SetName(_bannerHost, "Monitoring banners");
            Place(root, _bannerHost, 1);

            var tabs = new WrapPanel { Margin = new Thickness(0, 0, 10, 8) };
            KeyboardNavigation.SetDirectionalNavigation(tabs, KeyboardNavigationMode.Cycle);
            KeyboardNavigation.SetTabNavigation(tabs, KeyboardNavigationMode.Once);
            AutomationProperties.SetName(tabs, "Monitoring sections");
            AddTab(tabs, SecStrategies, "Strategies", "Strategies: realised versus expected band, deviation, decay state and decisions (Ctrl+1)", "Ctrl+1");
            AddTab(tabs, SecAccounts, "Accounts & brokers", "Accounts and brokers: equity, halts, broker link and reconciliation (Ctrl+2)", "Ctrl+2");
            AddTab(tabs, SecAlerts, "Alerts", "Alerts: history, mark as seen, toast setting (Ctrl+3)", "Ctrl+3");
            AddTab(tabs, SecReports, "Reports", "Reports: daily and weekly numbers with optional AI commentary (Ctrl+4)", "Ctrl+4");
            AddTab(tabs, SecInsights, "Portfolio insights", "Portfolio insights: your imported depots — realised and unrealised profit and loss per currency, a rough guide (Ctrl+5)", "Ctrl+5");
            AddTab(tabs, SecTax, "Tax reports", "Tax reports: UFOS.ai produces no tax data — get your tax reports from each broker (Ctrl+6)", "Ctrl+6");
            Place(root, tabs, 2);

            _scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = _strategiesPanel };
            Place(root, _scroll, 3);
            Content = root;

            _strategiesPanel.Children.Add(MonUi.Line("Loading…", 11.5, FundPalette.Faint));
            AutomationProperties.SetName(_strategiesPanel, "Monitored strategies");
            AutomationProperties.SetName(_accountsPanel, "Accounts and brokers");
            AutomationProperties.SetName(_alertsPanel, "Alert history");
            AutomationProperties.SetName(_reportsPanel, "Reports");
            AutomationProperties.SetName(_taxPanel, "Tax reports notice");
            AutomationProperties.SetName(_insightsPanel, "Portfolio insights");

            _poll = new DispatcherTimer { Interval = PollInterval };
            _poll.Tick += async (_, _) => await PollAsync();
            IsVisibleChanged += (_, _) => UpdatePolling();
            PreviewKeyDown += async (_, e) => await OnKeyAsync(e);
            MonitoringNotifier.Changed += Notifier_Changed;
        }

        // =====================================================================================
        // Lifecycle
        // =====================================================================================

        public async Task ActivateAsync()
        {
            if (_stopped) return;
            _activated = true;
            await RefreshAsync(force: true);
            UpdatePolling();
        }

        /// <summary>Window closed: stop polling for good and detach from the app-wide notifier.</summary>
        public void Shutdown()
        {
            _stopped = true;
            _poll.Stop();
            MonitoringNotifier.Changed -= Notifier_Changed;
        }

        /// <summary>A toast / banner asked for Monitoring → Alerts (optionally one alert: selected and marked as seen).</summary>
        public async Task ShowAlertAsync(long? alertId)
        {
            if (_stopped) return;
            _selectedAlertId = alertId;
            SelectTab(SecAlerts);
            if (_section != SecAlerts) await SelectSectionAsync(SecAlerts);
            else await LoadAlertsAsync(force: true);
            if (alertId is long id && _alerts.FirstOrDefault(x => x.Id == id) is MonAlert hit && !hit.Acked)
                await AckAsync(new[] { id });
        }

        /// <summary>Stage 9b: show Monitoring → Portfolio insights (MainWindow card click via HedgeFundWindow.ShowPortfolioInsights).</summary>
        public async Task ShowInsightsAsync()
        {
            if (_stopped) return;
            SelectTab(SecInsights);
            if (_section != SecInsights) await SelectSectionAsync(SecInsights);
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

        private async void Notifier_Changed(IReadOnlyList<MonAlert> fresh, int? unacked)
        {
            if (_stopped) return;
            RenderBanners();
            if (fresh.Count > 0 && IsVisible && _section == SecAlerts && !_busy) await LoadAlertsAsync(force: false);
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
                _ => -1,
            };
            if (index >= 0 && index < _tabs.Count)
            {
                e.Handled = true;
                _tabs[index].Button.IsChecked = true;
                _tabs[index].Button.Focus();
            }
            else if (e.Key == Key.B)
            {
                e.Handled = true;
                BrokersRequested?.Invoke();
            }
        }

        private Window? Owner => Window.GetWindow(this);

        // =====================================================================================
        // Loading
        // =====================================================================================

        private async Task RefreshAsync(bool force)
        {
            if (_stopped) return;
            var seq = ++_seq;
            var res = await PlatformClient.GetMonitoringOverviewAsync();
            if (seq != _seq || _stopped) return;
            if (!res.Ok || res.Value == null)
            {
                _connectionAttempts++;
                _connectionProblem = !res.Ok && res.HttpStatus == 404 && PlatformClient.ErrorCode(res) == null
                    ? "The running backend has no monitoring routes (older than stage 9) — restart it from the Hedge Fund window."
                    : res.Ok ? "Unexpected response from the backend (/v2/monitoring/overview) — backend and app versions may not match." : PlatformClient.DescribeFailure(res);
                RenderBanners();
                if (_overview == null) ShowSectionMessage(_connectionProblem);
                return;
            }
            var reconnected = _connectionProblem != null;
            _connectionProblem = null;
            _connectionAttempts = 0;
            _overview = res.Value;
            ExpireActionOnStateChange(_overview);
            MonitoringNotifier.Report(_overview.Alerts?.Unacked);
            if (force || _decayRules == null)
            {
                // §19.14 M5: basis labels + per-basis calibration for the strategy cards (a failure only hides the labels)
                var rules = await PlatformClient.GetDecayRulesAsync();
                if (_stopped) return;
                if (rules.Ok && rules.Value != null) _decayRules = rules.Value;
            }
            RenderHeader();
            RenderBanners();
            if (force || reconnected) _signatures.Clear();
            await LoadSectionAsync(_section, force);
        }

        private async Task LoadSectionAsync(string section, bool force)
        {
            switch (section)
            {
                case SecStrategies:
                    await LoadExpandedDetailsAsync();
                    RenderIfChanged(SecStrategies, new object?[] { _overview?.Strategies, _details, _detailErrors, _expanded.OrderBy(x => x).ToList() }, RenderStrategies);
                    break;
                case SecAccounts:
                    RenderIfChanged(SecAccounts, new object?[] { _overview?.Accounts, _overview?.Monitor }, RenderAccounts);
                    break;
                case SecAlerts:
                    await LoadAlertsAsync(force);
                    break;
                case SecReports:
                    if (force || _reports == null) await LoadReportsAsync();
                    break;
                case SecInsights:
                    if (force || (_insights == null && _insightsError == null)) await LoadInsightsAsync();
                    break;
                case SecTax:
                    if (force || _taxNotice == null) await LoadTaxNoticeAsync();
                    break;
            }
        }

        private async Task LoadExpandedDetailsAsync()
        {
            var live = (_overview?.Strategies ?? new List<MonStrategy>()).Select(s => s.StrategyId).Where(id => id != null).Cast<string>().ToHashSet(StringComparer.Ordinal);
            _expanded.RemoveWhere(id => !live.Contains(id));
            foreach (var id in _expanded.ToList())
            {
                var res = await PlatformClient.GetMonitoredStrategyAsync(id);
                if (_stopped) return;
                if (res.Ok && res.Value != null)
                {
                    _details[id] = res.Value;
                    _detailErrors.Remove(id);
                }
                else _detailErrors[id] = res.Ok ? "Unexpected response from the backend (/v2/monitoring/strategies/" + id + ")." : PlatformClient.DescribeFailure(res);
            }
        }

        private void RenderIfChanged(string key, object payload, Action render)
        {
            string sig;
            try { sig = JsonSerializer.Serialize(payload); }
            catch (Exception) { sig = Guid.NewGuid().ToString("N"); }
            if (_signatures.TryGetValue(key, out var old) && old == sig) return;
            _signatures[key] = sig;
            // fix1023 review B3: a re-render on a monitor tick (e.g. an expanded "Band chart, events and checks" block whose
            // detail changed after a pause / keep running) keeps the reader's scroll position; the expanded set is kept in _expanded.
            var offset = _scroll.VerticalOffset;
            render();
            if (offset > 0)
                Dispatcher.BeginInvoke(new Action(() => _scroll.ScrollToVerticalOffset(Math.Min(offset, _scroll.ScrollableHeight))),
                                       DispatcherPriority.Loaded);
        }

        private void ShowSectionMessage(string text)
        {
            var panel = PanelOf(_section);
            panel.Children.Clear();
            panel.Children.Add(MonUi.ErrorBlock(text));
        }

        private StackPanel PanelOf(string section) => section switch
        {
            SecAccounts => _accountsPanel,
            SecAlerts => _alertsPanel,
            SecReports => _reportsPanel,
            SecTax => _taxPanel,
            SecInsights => _insightsPanel,
            _ => _strategiesPanel,
        };

        private async Task SelectSectionAsync(string section)
        {
            if (_section == section && _scroll.Content == PanelOf(section)) return;
            _section = section;
            _scroll.Content = PanelOf(section);
            _scroll.ScrollToTop();
            if (_activated) await LoadSectionAsync(section, force: true);
        }

        private static string StateFingerprint(MonOverview o)
            => string.Join("|", (o.Strategies ?? new List<MonStrategy>())
                                .Select(s => (s.StrategyId ?? "?") + ":" + (s.RunId ?? "") + ":" + (s.RunStatus ?? "") + ":" + (s.Decay?.State ?? "") + ":" + (s.Decay?.Paused?.At ?? ""))
                                .OrderBy(x => x, StringComparer.Ordinal));

        private void ExpireActionOnStateChange(MonOverview o)
        {
            if (!_actionArmed) return;
            var fp = StateFingerprint(o);
            if (_actionFingerprint == null) { _actionFingerprint = fp; return; }   // first refresh after the action = its baseline
            if (fp != _actionFingerprint) SetAction(null);
        }

        private void SetAction(string? text, Brush? brush = null)
        {
            _actionArmed = !string.IsNullOrWhiteSpace(text);
            _actionFingerprint = null;
            _actionText.Text = text ?? "";
            _actionText.Foreground = brush ?? FundPalette.Dim;
            _actionText.Visibility = string.IsNullOrWhiteSpace(text) ? Visibility.Collapsed : Visibility.Visible;
            AutomationProperties.SetName(_actionText, text ?? "");
        }

        // =====================================================================================
        // Header + banners
        // =====================================================================================

        private void RenderHeader()
        {
            var o = _overview;
            if (o == null) return;
            _notice.Text = string.IsNullOrWhiteSpace(o.Notice) ? "Monitoring of paper/demo trading · simulated money" : o.Notice!;
            AutomationProperties.SetName(_notice, _notice.Text);
            var m = o.Monitor;
            var text = "Monitor task: " + (m?.Stale == true ? "MONITOR STALE" : m?.Running == true ? "running" : m?.Running == false ? "NOT running" : "—")
                       + (m?.TickInProgress == true ? " · tick running since " + TradingText.Ts(m.TickStartedAt) : "")
                       + " · last tick " + (m?.LastTickAt != null ? TradingText.Ts(m.LastTickAt) : "never")
                       + " · checks every 60 s, alerts every 30 s"
                       + (o.PaperOnly == true ? " · PAPER / DEMO ONLY" : "");
            _monitorText.Text = text;
            _monitorText.Foreground = m?.Running == true && m.Stale != true ? FundPalette.Dim : FundPalette.Risk;
            AutomationProperties.SetName(_monitorText, text);
        }

        private void RenderBanners()
        {
            _bannerHost.Children.Clear();
            if (_connectionProblem != null)
                _bannerHost.Children.Add(MonUi.Callout("Connection lost (attempt " + _connectionAttempts.ToString(Inv) + ") — showing the last known state. " + _connectionProblem,
                                                       FundPalette.Risk, MonUi.RiskSurface));
            if (_overview?.Monitor?.Stale == true)
                _bannerHost.Children.Add(MonUi.Callout("MONITOR STALE — " + (_overview!.Monitor!.TickInProgress == true ? "a monitor tick has been running since " + TradingText.Ts(_overview.Monitor.TickStartedAt) + "; " : "")
                                                       + "last finished tick " + (_overview.Monitor.LastTickAt != null ? TradingText.Ts(_overview.Monitor.LastTickAt) : "never")
                                                       + ". Decay checks, alerts and reports are not up to date (trading is not affected). Restart the Hedge Fund backend if this persists.",
                                                       FundPalette.Risk, MonUi.RiskSurface));
            var err = _overview?.Monitor?.LastError;
            if (!string.IsNullOrWhiteSpace(err))
                _bannerHost.Children.Add(MonUi.Callout("The monitor task reported a problem (trading is not affected): " + err, FundPalette.Risk, MonUi.RiskSurface));
            if (MonitoringNotifier.ToastProblem is string toastProblem)
                _bannerHost.Children.Add(BannerWithButton(toastProblem, FundPalette.Risk, MonUi.RiskSurface, "Open alerts", "Open the alert list", () => SelectTab(SecAlerts)));
            var paused = (_overview?.Strategies ?? new List<MonStrategy>()).Where(s => s.IsPaused).ToList();
            if (paused.Count > 0)
            {
                var b = BannerWithButton("■ " + paused.Count.ToString(Inv) + (paused.Count == 1 ? " strategy is" : " strategies are") + " PAUSED by the decay monitor and wait for your decision: "
                                         + string.Join(", ", paused.Select(s => s.DisplayName)) + ". They keep their positions and open or increase nothing; only their own reduce-only protective exits still run.",
                                         FundPalette.Kill, FundPalette.KillSurface, "Show", "Show the paused strategies", () => SelectTab(SecStrategies));
                _bannerHost.Children.Add(b);
            }
            var unacked = MonitoringNotifier.Unacked ?? _overview?.Alerts?.Unacked;
            if (unacked is int u && u > 0)
                _bannerHost.Children.Add(BannerWithButton(u.ToString(Inv) + (u == 1 ? " alert has" : " alerts have") + " not been marked as seen yet.",
                                                          FundPalette.Focus, MonUi.FocusSurface, "Open alerts", "Open the alert list", () => SelectTab(SecAlerts)));
        }

        private static Border BannerWithButton(string text, Brush brush, Brush surface, string buttonText, string buttonName, Action click)
        {
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var t = FundUi.Text(text, 11.5, brush);
            t.VerticalAlignment = VerticalAlignment.Center;
            grid.Children.Add(t);
            var b = MonUi.Small(buttonText, buttonName);
            b.Margin = new Thickness(10, 0, 0, 0);
            b.Click += (_, _) => click();
            Grid.SetColumn(b, 1);
            grid.Children.Add(b);
            var border = new Border
            {
                Background = surface,
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 0, 0, 8),
                Child = grid,
            };
            AutomationProperties.SetName(border, text);
            return border;
        }

        private async Task RunChecksAsync()
        {
            if (_busy) return;
            _busy = true;
            _runChecks.IsEnabled = false;
            SetAction("Running the decay checks…", FundPalette.Faint);
            try
            {
                var res = await PlatformClient.RunDecayChecksAsync();
                if (!res.Ok || res.Value == null) SetAction(res.Ok ? "Unexpected response from the backend (decay/run)." : PlatformClient.DescribeFailure(res), FundPalette.Kill);
                else
                {
                    var r = res.Value;
                    var errors = r.Errors ?? new Dictionary<string, JsonElement>();
                    SetAction("Decay checks done: " + (r.Checked ?? 0).ToString(Inv) + " check(s) stored"
                              + ((r.Paused?.Count ?? 0) > 0 ? " · PAUSED now: " + string.Join(", ", r.Paused!) : " · nothing paused")
                              + (errors.Count > 0 ? " · errors: " + string.Join("; ", errors.Select(kv => kv.Key + ": " + FundFormat.Plain(kv.Value))) : ""),
                              errors.Count > 0 || (r.Paused?.Count ?? 0) > 0 ? FundPalette.Risk : FundPalette.Dim);
                }
            }
            finally
            {
                _busy = false;
                _runChecks.IsEnabled = true;
            }
            await RefreshAsync(force: true);
        }

        // =====================================================================================
        // Small builders
        // =====================================================================================

        private void AddTab(Panel host, string key, string text, string name, string accelerator)
        {
            var rb = new RadioButton
            {
                GroupName = "MonitoringSections",
                Content = text,
                Style = MonTabs.Style,
                IsChecked = key == SecStrategies,
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

        private static void Place(Grid grid, UIElement element, int row)
        {
            Grid.SetRow(element, row);
            grid.Children.Add(element);
        }

        private static WrapPanel ChipRow(params Border[] chips)
        {
            var w = new WrapPanel { Margin = new Thickness(0, 2, 0, 4) };
            foreach (var c in chips) w.Children.Add(c);
            return w;
        }
    }

    /// <summary>The section tab style (same look as Portfolio &amp; Trading).</summary>
    internal static class MonTabs
    {
        private static Style? _style;

        public static Style Style
        {
            get
            {
                if (_style != null) return _style;
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
                _style = style;
                return style;
            }
        }
    }
}

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Ui;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>Navigation targets the Overview asks the window to open (also the backend's activity `link` vocabulary).</summary>
    internal static class OverviewTargets
    {
        public const string Library = "strategies/library";
        public const string Studio = "strategies/studio";
        public const string Loop = "strategies/loop";
        public const string Trading = "trading/portfolio";
        public const string Orders = "trading/orders";
        public const string Monitoring = "trading/monitoring";
        public const string Alerts = "trading/alerts";
        public const string Shadow = "trading/shadow";
        public const string Legacy = "trading/legacy";
        public const string Research = "research";
        public const string RiskLab = "risklab";
        public const string OptionsArchive = "settings/options-archive";

        private static readonly HashSet<string> Known = new(StringComparer.Ordinal)
        {
            Library, Studio, Loop, Trading, Orders, Monitoring, Alerts, Shadow, Legacy, Research, RiskLab, OptionsArchive,
        };

        /// <summary>The known target of a backend `link`, or null (unknown link → no navigation, never a dead link).</summary>
        public static string? Resolve(string? link)
        {
            var l = (link ?? "").Trim().ToLowerInvariant();
            return Known.Contains(l) ? l : null;
        }
    }

    /// <summary>
    /// Stage 11 Overview (design D3; contract UFOS.ai/docs/tasks/STAGE11_OVERVIEW_TASK.md §4): attention strip, market band (tickers +
    /// options structure), the active paper account with its risk row, equity curve and strategy tiles, and the right column (System,
    /// Recent activity). Read-only: nothing here trades. Every value comes from exactly one source; a missing one reads
    /// "not available (reason)". Polls only while visible (markets 60 s, OS2 state 60 s with gex / surface / calendar when the slot
    /// changes, risk 5 min, activity 15 s, system 15 s, /fund/equity 60 s); the old engine's account comes from the window's 5 s
    /// /fund/state poll (<see cref="SetFundState"/>).
    /// </summary>
    internal sealed partial class OverviewView : UserControl
    {
        private const string LogModule = "HedgeFund";
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan TickInterval = TimeSpan.FromSeconds(5);
        private static readonly TimeSpan MarketsInterval = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan OsInterval = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan RiskInterval = TimeSpan.FromMinutes(5);
        /// <summary>After the migration the account figures come from /v2/overview/risk (account_summary): polled every 60 s then.</summary>
        private static readonly TimeSpan RiskIntervalPlatform = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan ActivityInterval = TimeSpan.FromSeconds(15);
        private static readonly TimeSpan SystemInterval = TimeSpan.FromSeconds(15);
        private static readonly TimeSpan EquityInterval = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan CalendarInterval = TimeSpan.FromHours(1);
        private static readonly TimeSpan CalendarRetry = TimeSpan.FromMinutes(2);
        /// <summary>Below this window width the right column moves under the strategies card and the tickers wrap (contract §4).</summary>
        private const double NarrowWindowWidth = 1250;

        private readonly DispatcherTimer _timer;
        private bool _activated;
        private bool _stopped;
        private bool _narrow;
        private bool _layoutApplied;

        // ---- load bookkeeping
        private DateTime _marketsAt = DateTime.MinValue, _osAt = DateTime.MinValue, _riskAt = DateTime.MinValue,
                         _activityAt = DateTime.MinValue, _systemAt = DateTime.MinValue, _equityAt = DateTime.MinValue,
                         _calendarAt = DateTime.MinValue;
        private bool _marketsLoading, _osLoading, _riskLoading, _activityLoading, _systemLoading, _equityLoading;

        // ---- data (null + problem text = not available)
        private FundState? _fund;
        private bool _fundSeen, _fundConnected, _fundReplaced;
        private DateTime _fundAt = DateTime.MinValue;
        private DateTime _systemLoadedAt = DateTime.MinValue;
        private readonly Action<IReadOnlyList<MonAlert>, int?> _onAlerts;
        private OverviewMarkets? _markets;
        private string? _marketsProblem;
        private OsState? _spyState, _qqqState;
        private string? _spyStateProblem, _qqqStateProblem;
        private OsGexResponse? _gex;
        private string? _gexProblem;
        private OsSurfaceResponse? _surface;
        private string? _surfaceProblem;
        private OsCalendar? _calendar;
        private string? _calendarProblem;
        private string? _loadedSlot;
        private OverviewRisk? _risk;
        private string? _riskProblem;
        private OverviewActivity? _activity;
        private string? _activityProblem;
        private TradingState? _trading;
        private string? _tradingProblem;
        private TradingMigrationStatus? _migration;
        private string? _migrationProblem;
        private CouncilState? _council;
        private string? _councilProblem;
        private List<TradingSource>? _sources;
        private string? _sourcesProblem;
        private IncubationList? _incubation;
        private string? _incubationProblem;
        private FundEquity? _equity;
        private string? _equityProblem;
        private int _rangeDays = 91;   // 1M = 30, 3M = 91, All = 0

        /// <summary>Asks the window to open a view (one of <see cref="OverviewTargets"/>).</summary>
        public event Action<string>? NavigateRequested;

        public OverviewView()
        {
            BuildLayout();
            _timer = new DispatcherTimer { Interval = TickInterval };
            _timer.Tick += async (_, _) => await TickAsync();
            IsVisibleChanged += async (_, _) =>
            {
                UpdatePolling();
                if (IsVisible && _activated && !_stopped) await TickAsync();
            };
            SizeChanged += (_, _) => ApplyResponsive();
            _onAlerts = (_, _) => Dispatcher.BeginInvoke(new Action(() => SafeRender(RenderAttention)));
            Loaded += (_, _) =>
            {
                MonitoringNotifier.Changed -= _onAlerts;   // Loaded can fire again (re-parenting): never subscribe twice
                if (!_stopped) MonitoringNotifier.Changed += _onAlerts;
                ApplyResponsive();
            };
            Unloaded += (_, _) => MonitoringNotifier.Changed -= _onAlerts;
            RenderAll();
        }

        // =====================================================================================
        // Lifecycle (same pattern as the other views: lazy creation, polling only while visible)
        // =====================================================================================

        /// <summary>Called whenever the rail's "Overview" is selected.</summary>
        public async Task ActivateAsync()
        {
            if (_stopped) return;
            _activated = true;
            UpdatePolling();
            await TickAsync();
        }

        /// <summary>Window closed: stop polling for good.</summary>
        public void Shutdown()
        {
            _stopped = true;
            _timer.Stop();
            MonitoringNotifier.Changed -= _onAlerts;
        }

        /// <summary>
        /// The window's 5 s /fund/state poll (unchanged) hands its result over: the old engine's state, whether it answered, and
        /// whether the old engine is replaced by the Council switch-over (its /fund routes retired).
        /// </summary>
        public void SetFundState(FundState? state, bool connected, bool replaced)
        {
            if (_stopped) return;
            _fund = state;
            _fundConnected = connected;
            _fundReplaced = replaced;
            _fundSeen = true;
            _fundAt = DateTime.UtcNow;
            if (!IsVisible) return;
            SafeRender(RenderAccount);
            SafeRender(RenderAttention);
        }

        private void UpdatePolling()
        {
            if (!_stopped && _activated && IsVisible) { if (!_timer.IsEnabled) _timer.Start(); }
            else _timer.Stop();
        }

        private async Task TickAsync()
        {
            if (_stopped || !_activated || !IsVisible) return;
            var now = DateTime.UtcNow;
            var tasks = new List<Task>();
            if (!_marketsLoading && now - _marketsAt >= MarketsInterval) tasks.Add(LoadMarketsAsync());
            if (!_osLoading && now - _osAt >= OsInterval) tasks.Add(LoadOptionsStructureAsync());
            if (!_riskLoading && now - _riskAt >= (IsLegacyMode() == false ? RiskIntervalPlatform : RiskInterval)) tasks.Add(LoadRiskAsync());
            if (!_activityLoading && now - _activityAt >= ActivityInterval) tasks.Add(LoadActivityAsync());
            if (!_systemLoading && now - _systemAt >= SystemInterval) tasks.Add(LoadSystemAsync());
            if (!_equityLoading && IsLegacyMode() == true && !_fundReplaced && now - _equityAt >= EquityInterval) tasks.Add(LoadEquityAsync());
            SafeRender(RenderAttention);   // the unseen-alert count may have changed
            SafeRender(RenderAges);        // "source · age" lines
            SafeRender(UpdateScrollMode);  // late reflows (e.g. risk chips wrapping) that raised no size change of a watched element
            if (tasks.Count == 0) return;
            try { await Task.WhenAll(tasks); }
            catch (Exception ex) { Log("Overview refresh failed.", ex); }
        }

        private void SafeRender(Action render)
        {
            try { render(); }
            catch (Exception ex) { Log("Overview: a section could not be shown (" + render.Method.Name + ").", ex); }
        }

        private static void Log(string message, Exception? ex = null)
        {
            try { if (ex != null) AppLog.Warn(LogModule, message, ex); else AppLog.Warn(LogModule, message); } catch { }
        }

        private void Navigate(string target) => NavigateRequested?.Invoke(target);

        /// <summary>A failed call as plain text. Stage 11 routes that answer 404 are missing in the running backend (restart needed).</summary>
        private static string Problem<T>(PlatformResult<T> r, bool routeMissingOn404 = false)
        {
            if (r.Ok) return "empty answer from the backend";
            if (routeMissingOn404 && r.Status == PlatformCallStatus.NotFound) return "backend route missing — restart the backend";
            return HfUi.Plain(PlatformClient.DescribeFailure(r), 300);
        }

        // =====================================================================================
        // Loading
        // =====================================================================================

        private async Task LoadMarketsAsync()
        {
            _marketsLoading = true;
            _marketsAt = DateTime.UtcNow;
            try
            {
                var r = await PlatformClient.GetOverviewMarketsAsync();
                if (r.Ok && r.Value != null) { _markets = r.Value; _marketsProblem = null; }
                else { _markets = null; _marketsProblem = Problem(r, routeMissingOn404: true); }
            }
            catch (Exception ex) { _markets = null; _marketsProblem = "could not be loaded (" + ex.GetType().Name + ")"; Log("Overview markets failed.", ex); }
            finally { _marketsLoading = false; }
            SafeRender(RenderMarkets);
        }

        private async Task LoadOptionsStructureAsync()
        {
            _osLoading = true;
            _osAt = DateTime.UtcNow;
            try
            {
                var spyT = PlatformClient.GetOptionsStructureStateAsync("SPY");
                var qqqT = PlatformClient.GetOptionsStructureStateAsync("QQQ");
                await Task.WhenAll(spyT, qqqT);
                var spy = spyT.Result;
                var qqq = qqqT.Result;
                if (spy.Ok && spy.Value != null) { _spyState = spy.Value; _spyStateProblem = null; }
                else { _spyState = null; _spyStateProblem = Problem(spy, routeMissingOn404: true); }
                if (qqq.Ok && qqq.Value != null) { _qqqState = qqq.Value; _qqqStateProblem = null; }
                else { _qqqState = null; _qqqStateProblem = Problem(qqq, routeMissingOn404: true); }

                if (_spyState?.Available == true)
                {
                    // gex / surface of the SAME slot as the state; reloaded only when the slot changes (contract §4 polling)
                    var slot = string.IsNullOrWhiteSpace(_spyState.Slot) ? "latest" : _spyState.Slot!;
                    if (slot != _loadedSlot || (_gex == null && _gexProblem == null) || (_surface == null && _surfaceProblem == null))
                    {
                        var gexT = PlatformClient.GetOptionsStructureGexAsync("SPY", slot);
                        var surT = PlatformClient.GetOptionsStructureSurfaceAsync("SPY", slot);
                        await Task.WhenAll(gexT, surT);
                        var g = gexT.Result;
                        var s = surT.Result;
                        if (g.Ok && g.Value != null) { _gex = g.Value; _gexProblem = null; }
                        else { _gex = null; _gexProblem = Problem(g); }
                        if (s.Ok && s.Value != null) { _surface = s.Value; _surfaceProblem = null; }
                        else { _surface = null; _surfaceProblem = Problem(s); }
                        _loadedSlot = slot;
                        _calendarAt = DateTime.MinValue;   // a new slot: refresh the calendar too
                    }
                }
                else
                {
                    // no fresh state: older gex / surface results are not shown (no stale stand-ins)
                    _gex = null;
                    _surface = null;
                    _gexProblem = _surfaceProblem = null;
                    _loadedSlot = null;
                }
                if (DateTime.UtcNow - _calendarAt >= CalendarInterval)
                {
                    _calendarAt = DateTime.UtcNow;
                    var c = await PlatformClient.GetOptionsStructureCalendarAsync(60);
                    if (c.Ok && c.Value != null) { _calendar = c.Value; _calendarProblem = null; }
                    else
                    {
                        _calendar = null;
                        _calendarProblem = Problem(c, routeMissingOn404: true);
                        _calendarAt = DateTime.UtcNow - CalendarInterval + CalendarRetry;   // a failed read is retried after ~2 min, not an hour
                    }
                }
            }
            catch (Exception ex) { _spyStateProblem = _qqqStateProblem = "could not be loaded (" + ex.GetType().Name + ")"; _spyState = _qqqState = null; Log("Overview options structure failed.", ex); }
            finally { _osLoading = false; }
            SafeRender(RenderOptions);
        }

        private async Task LoadRiskAsync()
        {
            _riskLoading = true;
            _riskAt = DateTime.UtcNow;
            try
            {
                var r = await PlatformClient.GetOverviewRiskAsync();
                if (r.Ok && r.Value != null) { _risk = r.Value; _riskProblem = null; }
                else { _risk = null; _riskProblem = Problem(r, routeMissingOn404: true); }
            }
            catch (Exception ex) { _risk = null; _riskProblem = "could not be loaded (" + ex.GetType().Name + ")"; Log("Overview risk failed.", ex); }
            finally { _riskLoading = false; }
            SafeRender(RenderRisk);
            SafeRender(RenderAccount);   // after the migration the account figures come from this answer (account_summary)
        }

        private async Task LoadActivityAsync()
        {
            _activityLoading = true;
            _activityAt = DateTime.UtcNow;
            try
            {
                var r = await PlatformClient.GetOverviewActivityAsync(20);
                if (r.Ok && r.Value != null) { _activity = r.Value; _activityProblem = null; }
                else { _activity = null; _activityProblem = Problem(r, routeMissingOn404: true); }
            }
            catch (Exception ex) { _activity = null; _activityProblem = "could not be loaded (" + ex.GetType().Name + ")"; Log("Overview activity failed.", ex); }
            finally { _activityLoading = false; }
            SafeRender(RenderActivity);
        }

        private async Task LoadEquityAsync()
        {
            _equityLoading = true;
            _equityAt = DateTime.UtcNow;
            try
            {
                var r = await FundClient.GetEquityAsync();
                if (r.Ok && r.Value != null) { _equity = r.Value; _equityProblem = null; }
                else { _equity = null; _equityProblem = HfUi.Plain(r.Describe(), 300); }
            }
            catch (Exception ex) { _equity = null; _equityProblem = "could not be loaded (" + ex.GetType().Name + ")"; Log("Overview equity failed.", ex); }
            finally { _equityLoading = false; }
            SafeRender(RenderEquityChart);
        }

        private async Task LoadSystemAsync()
        {
            _systemLoading = true;
            _systemAt = DateTime.UtcNow;
            try
            {
                var loopsT = PlatformClient.ListLoopsAsync(1);
                var valT = PlatformClient.ListValidationsAsync(null, 50);
                var btT = PlatformClient.ListBacktestsAsync(null, 50);
                var oaT = PlatformClient.GetOptionsArchiveStatusAsync();
                var brT = PlatformClient.GetBrokersAsync();
                var dsT = PlatformClient.GetSourcesAsync();
                var migT = PlatformClient.GetMigrationAsync();
                var coT = PlatformClient.GetCouncilStateAsync();
                var trT = PlatformClient.GetTradingStateAsync();
                var incT = PlatformClient.ListIncubationAsync();
                var aiT = LoadAiBudgetAsync();
                await Task.WhenAll(loopsT, valT, btT, oaT, brT, dsT, migT, coT, trT, incT, aiT);

                var mig = migT.Result;
                if (mig.Ok && mig.Value != null) { _migration = mig.Value; _migrationProblem = null; }
                else { _migration = null; _migrationProblem = Problem(mig); }
                var co = coT.Result;
                if (co.Ok && co.Value != null) { _council = co.Value; _councilProblem = null; }
                else { _council = null; _councilProblem = Problem(co); }
                var tr = trT.Result;
                if (tr.Ok && tr.Value != null) { _trading = tr.Value; _tradingProblem = null; }
                else { _trading = null; _tradingProblem = Problem(tr); }
                var inc = incT.Result;
                if (inc.Ok && inc.Value != null) { _incubation = inc.Value; _incubationProblem = null; }
                else { _incubation = null; _incubationProblem = Problem(inc); }

                // strategy sub-ledgers of the platform paper accounts (internal, not broker accounts)
                if (_trading != null)
                {
                    var sources = new List<TradingSource>();
                    string? problem = null;
                    foreach (var a in PlatformPaperAccounts())
                    {
                        if (string.IsNullOrEmpty(a.Id)) continue;
                        var d = await PlatformClient.GetTradingAccountAsync(a.Id!);
                        if (d.Ok && d.Value != null) sources.AddRange(d.Value.Sources ?? new List<TradingSource>());
                        else { problem = Problem(d); break; }
                    }
                    _sources = problem == null ? sources : null;
                    _sourcesProblem = problem;
                }
                else
                {
                    _sources = null;
                    _sourcesProblem = "platform accounts could not be read (" + _tradingProblem + ")";
                }

                SafeRender(() => RenderSystemRows(loopsT.Result, valT.Result, btT.Result, oaT.Result, brT.Result, dsT.Result, aiT.Result));
            }
            catch (Exception ex) { Log("Overview system refresh failed.", ex); }
            finally { _systemLoading = false; _systemLoadedAt = DateTime.UtcNow; }
            SafeRender(RenderAccount);
            SafeRender(RenderTiles);
            SafeRender(RenderAttention);
        }

        /// <summary>
        /// The platform's own paper accounts (no broker accounts) — ONLY to find the strategy sub-ledgers for the tiles. The account
        /// figures (equity, today, total) come from the backend's account set (/v2/overview/risk account_summary), never summed here.
        /// </summary>
        private IEnumerable<TradingAccountSummary> PlatformPaperAccounts()
            => (_trading?.Accounts ?? new List<TradingAccountSummary>()).Where(a => !a.IsBrokerAccount && a.Paper != false);

        /// <summary>
        /// AI budget today (contract §4, rule documented here and in the tooltip): the sum of the Hedge Fund module's budget entries with
        /// period "day" from its GET /ai/budgets (Shared/AiBudgetRegistry, the authoritative backend values) — spent / limit. Any entry
        /// whose figures could not be read makes the sum "not available" (no partial sum).
        /// </summary>
        private static async Task<(string Text, string? Sub, bool Ok)> LoadAiBudgetAsync()
        {
            try
            {
                var p = AiBudgetRegistry.Providers.FirstOrDefault(x => x.ModuleId == "hedgefund");
                if (p == null) return (HfUi.NotAvailable("no Hedge Fund budget provider registered"), null, false);
                var st = await AiBudgetRegistry.LoadAsync(p, CancellationToken.None);
                if (st.Availability != AiBudgetAvailability.Ok || st.Payload == null) return (HfUi.NotAvailable(st.Message), null, false);
                var day = (st.Payload.Budgets ?? new List<AiBudgetEntry>()).Where(b => b.Period == "day").ToList();
                if (day.Count == 0) return (HfUi.NotAvailable("the module lists no daily budget"), null, false);
                var unreadable = day.FirstOrDefault(b => b.SpentUsd == null || b.LimitUsd == null);
                if (unreadable != null)
                    return (HfUi.NotAvailable((unreadable.Label ?? unreadable.Key ?? "an entry") + " could not be read"), null, false);
                var spent = day.Sum(b => b.SpentUsd!.Value);
                var limit = day.Sum(b => b.LimitUsd!.Value);
                return ("$" + spent.ToString("0.00", Inv) + " / $" + limit.ToString("0.00", Inv) + " today",
                        "sum of " + day.Count + " daily caps of the Hedge Fund module", true);
            }
            catch (Exception ex)
            {
                Log("Overview AI budget failed.", ex);
                return (HfUi.NotAvailable("could not be loaded (" + ex.GetType().Name + ")"), null, false);
            }
        }

        /// <summary>
        /// Which paper account trades (contract §1.2 rule): the old engine's while GET /v2/portfolio/migration says legacy_account.status is
        /// not "migrated", the platform's afterwards. Null = unknown (the migration status could not be read yet).
        /// </summary>
        private bool? IsLegacyMode()
        {
            var status = _migration?.LegacyAccount?.Status;
            if (_migration == null || string.IsNullOrEmpty(status)) return null;
            return status != "migrated";
        }
    }
}

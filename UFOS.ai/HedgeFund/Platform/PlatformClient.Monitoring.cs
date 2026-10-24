using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform stage 9 — monitoring (contract: backend/strategy_platform/SPEC.md §19, routes §19.10, exact JSON
    // §19.11, backend notes §19.12, WPF notes §19.13). Same rules as the rest of PlatformClient: session token on every
    // call, never throws, the backend's `error` text is shown verbatim (402 budget, 409 not_paused / busy,
    // 404 not_found, 503 no_key, 504 timeout), no retries and no
    // alternative paths. PAPER / DEMO ONLY — nothing here places an order: "demote" uses the backend's §17.2 abort path.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan MonitoringTimeout = TimeSpan.FromSeconds(15);
        private static readonly TimeSpan MonitoringActionTimeout = TimeSpan.FromSeconds(45);
        /// <summary>The commentary waits up to 90 s for the language model (§19.4) + facts, number check and storage.</summary>
        private static readonly TimeSpan CommentaryTimeout = TimeSpan.FromSeconds(120);

        public const string DecisionKeepRunning = "keep_running";
        public const string DecisionDemote = "demote";

        /// <summary>GET /v2/monitoring/overview — accounts (equity, halts, broker link, reconciliation), monitored strategies, alert counts.</summary>
        public static Task<PlatformResult<MonOverview>> GetMonitoringOverviewAsync()
            => SendAsync<MonOverview>(HttpMethod.Get, "/v2/monitoring/overview", null, MonitoringTimeout);

        /// <summary>GET /v2/monitoring/strategies/{id} → {strategy, series (oldest first), checks (newest 250), events, rules}; 404 never incubated.</summary>
        public static Task<PlatformResult<MonStrategyDetail>> GetMonitoredStrategyAsync(string strategyId)
            => SendAsync<MonStrategyDetail>(HttpMethod.Get, "/v2/monitoring/strategies/" + Esc(strategyId), null, MonitoringTimeout);

        /// <summary>
        /// POST /v2/monitoring/strategies/{id}/decision {action, reason?} — keep_running or demote (fix1023: no confirm word, note
        /// optional). 409 not_paused, 400 reason too long. Audited by the backend (decay_decision).
        /// </summary>
        public static Task<PlatformResult<MonDecisionResult>> DecideDecayAsync(string strategyId, string action, string reason)
        {
            if (action != DecisionKeepRunning && action != DecisionDemote)
                throw new ArgumentException("Unknown decision.", nameof(action));
            var body = WithNote(new Dictionary<string, object?> { ["action"] = action }, reason);   // fix1023: no "DEMOTE", note optional
            return SendAsync<MonDecisionResult>(HttpMethod.Post, "/v2/monitoring/strategies/" + Esc(strategyId) + "/decision", body, MonitoringActionTimeout);
        }

        /// <summary>GET /v2/monitoring/decay/rules → shared rules, per-basis calibration, basis labels, text (§19.14 M5).</summary>
        public static Task<PlatformResult<MonDecayRules>> GetDecayRulesAsync()
            => SendAsync<MonDecayRules>(HttpMethod.Get, "/v2/monitoring/decay/rules", null, MonitoringTimeout);

        /// <summary>POST /v2/monitoring/decay/run {} → {checked, paused: [strategy ids], errors}.</summary>
        public static Task<PlatformResult<MonDecayRunResult>> RunDecayChecksAsync()
            => SendAsync<MonDecayRunResult>(HttpMethod.Post, "/v2/monitoring/decay/run", new Dictionary<string, object?>(), MonitoringActionTimeout);

        /// <summary>GET /v2/monitoring/alerts?after=&amp;limit=&amp;unacked= → alerts newest first (id &gt; after), last_id, unacked, kinds.</summary>
        public static Task<PlatformResult<MonAlertList>> GetMonitoringAlertsAsync(long after, int limit, bool unackedOnly)
        {
            var q = "after=" + Math.Max(0, after).ToString(CultureInfo.InvariantCulture)
                    + "&limit=" + Math.Clamp(limit, 1, 500).ToString(CultureInfo.InvariantCulture)
                    + (unackedOnly ? "&unacked=1" : "");
            return SendAsync<MonAlertList>(HttpMethod.Get, "/v2/monitoring/alerts?" + q, null, MonitoringTimeout);
        }

        /// <summary>POST /v2/monitoring/alerts/ack {ids: [1..500]} → {acked, unacked}.</summary>
        public static Task<PlatformResult<MonAckResult>> AckMonitoringAlertsAsync(IReadOnlyCollection<long> ids)
        {
            var list = ids.Distinct().Take(500).ToList();
            return SendAsync<MonAckResult>(HttpMethod.Post, "/v2/monitoring/alerts/ack", new Dictionary<string, object?> { ["ids"] = list }, MonitoringTimeout);
        }

        /// <summary>POST /v2/monitoring/alerts/ack {all: true} → {acked, unacked}.</summary>
        public static Task<PlatformResult<MonAckResult>> AckAllMonitoringAlertsAsync()
            => SendAsync<MonAckResult>(HttpMethod.Post, "/v2/monitoring/alerts/ack", new Dictionary<string, object?> { ["all"] = true }, MonitoringTimeout);

        /// <summary>GET /v2/monitoring/reports?period=&amp;limit= → summaries, newest first.</summary>
        public static Task<PlatformResult<MonReportList>> ListMonitoringReportsAsync(string? period, int limit = 50)
        {
            var q = "limit=" + Math.Clamp(limit, 1, 200).ToString(CultureInfo.InvariantCulture);
            if (period == "daily" || period == "weekly") q += "&period=" + period;
            return SendAsync<MonReportList>(HttpMethod.Get, "/v2/monitoring/reports?" + q, null, MonitoringTimeout);
        }

        public static Task<PlatformResult<MonReportEnvelope>> GetMonitoringReportAsync(string reportId)
            => SendAsync<MonReportEnvelope>(HttpMethod.Get, "/v2/monitoring/reports/" + Esc(reportId), null, MonitoringTimeout);

        /// <summary>POST /v2/monitoring/reports {period, date?} (a day inside the period; default = the last complete period) → 201 {report}.</summary>
        public static Task<PlatformResult<MonReportEnvelope>> CreateMonitoringReportAsync(string period, string? date)
        {
            if (period != "daily" && period != "weekly") throw new ArgumentException("Unknown period.", nameof(period));
            var body = new Dictionary<string, object?> { ["period"] = period };
            if (!string.IsNullOrWhiteSpace(date)) body["date"] = date.Trim();
            return SendAsync<MonReportEnvelope>(HttpMethod.Post, "/v2/monitoring/reports", body, MonitoringActionTimeout);
        }

        /// <summary>POST /v2/monitoring/reports/{id}/commentary {model, language} → {commentary}. Costs money (budget key "reports"); 402 budget verbatim.</summary>
        public static Task<PlatformResult<MonCommentaryEnvelope>> CreateReportCommentaryAsync(string reportId, string model, string language)
            => SendAsync<MonCommentaryEnvelope>(HttpMethod.Post, "/v2/monitoring/reports/" + Esc(reportId) + "/commentary",
                CommentaryBody(model, language, null), CommentaryTimeout);

        /// <summary>
        /// GET /v2/monitoring/tax-notice (§19.15, user decision 2026-10-18: NO tax export in UFOS.ai) → {version, decided, notice,
        /// paper_notice, tax_export: false} — the ONE fixed wording, shown as plain text; nothing is invented when it fails.
        /// </summary>
        public static Task<PlatformResult<MonTaxNotice>> GetTaxNoticeAsync()
            => SendAsync<MonTaxNotice>(HttpMethod.Get, "/v2/monitoring/tax-notice", null, MonitoringTimeout);

        private static Dictionary<string, object?> CommentaryBody(string model, string language, string? benchmark)
        {
            if (model != ExplainModelStandard && model != ExplainModelPremium)
                throw new ArgumentException("Unknown model tier.", nameof(model));
            if (language != ExplainLanguageEnglish && language != ExplainLanguageGerman)
                throw new ArgumentException("Unknown language.", nameof(language));
            var body = new Dictionary<string, object?> { ["model"] = model, ["language"] = language };
            if (!string.IsNullOrWhiteSpace(benchmark)) body["benchmark"] = benchmark.Trim().ToUpperInvariant();
            return body;
        }

        /// <summary>
        /// A failed AI commentary as text: the backend's `error` verbatim; for 402 budget also its `reason` and the budget key
        /// (§19.4: {error, code: "budget", budget_key: "reports", reason}).
        /// </summary>
        public static string CommentaryFailureText<T>(PlatformResult<T> result)
        {
            var text = DescribeFailure(result);
            if (ErrorCode(result) != "budget" || string.IsNullOrWhiteSpace(result.RawBody)) return text;
            try
            {
                using var doc = JsonDocument.Parse(result.RawBody);
                var root = doc.RootElement;
                if (root.ValueKind == JsonValueKind.Object)
                {
                    var key = root.TryGetProperty("budget_key", out var k) && k.ValueKind == JsonValueKind.String ? k.GetString() : null;
                    var reason = root.TryGetProperty("reason", out var r) && r.ValueKind == JsonValueKind.String ? r.GetString() : null;
                    if (!string.IsNullOrWhiteSpace(reason) && reason != result.Message) text += "\n" + reason;
                    if (!string.IsNullOrWhiteSpace(key)) text += "\nBudget key: " + key + " (set it in the central AI budget window).";
                }
            }
            catch (JsonException) { /* the message above is all there is */ }
            return text;
        }
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §19.11 / §19.12 (snake_case). Tolerant: unknown fields are ignored. Computed
    // properties carry [JsonIgnore] (PropertyNameCaseInsensitive would otherwise map/collide them). Scalars whose type the
    // contract leaves open are JsonElement.
    // =====================================================================================

    internal sealed class MonOverview
    {
        [JsonPropertyName("now")] public string? Now { get; set; }
        [JsonPropertyName("paper_only")] public bool? PaperOnly { get; set; }
        [JsonPropertyName("notice")] public string? Notice { get; set; }
        [JsonPropertyName("monitor")] public MonMonitorState? Monitor { get; set; }
        [JsonPropertyName("alerts")] public MonAlertCounts? Alerts { get; set; }
        [JsonPropertyName("accounts")] public List<MonAccount>? Accounts { get; set; }
        [JsonPropertyName("strategies")] public List<MonStrategy>? Strategies { get; set; }
    }

    internal sealed class MonMonitorState
    {
        [JsonPropertyName("running")] public bool? Running { get; set; }
        [JsonPropertyName("last_tick_at")] public string? LastTickAt { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
        /// <summary>§19.14: a tick running (or no tick finished) for too long → "monitor stale".</summary>
        [JsonPropertyName("stale")] public bool? Stale { get; set; }
        [JsonPropertyName("tick_in_progress")] public bool? TickInProgress { get; set; }
        [JsonPropertyName("tick_started_at")] public string? TickStartedAt { get; set; }
    }

    internal sealed class MonAlertCounts
    {
        [JsonPropertyName("unacked")] public int? Unacked { get; set; }
        [JsonPropertyName("last_id")] public long? LastId { get; set; }
    }

    internal sealed class MonAccount
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("broker")] public string? Broker { get; set; }
        [JsonPropertyName("paper")] public bool? Paper { get; set; }
        [JsonPropertyName("equity")] public double? Equity { get; set; }
        [JsonPropertyName("day_pnl")] public double? DayPnl { get; set; }
        [JsonPropertyName("drawdown_pct")] public double? DrawdownPct { get; set; }
        [JsonPropertyName("valuation_ok")] public bool? ValuationOk { get; set; }
        [JsonPropertyName("valuation_message")] public string? ValuationMessage { get; set; }
        [JsonPropertyName("halt")] public TradingHalt? Halt { get; set; }
        [JsonPropertyName("link")] public BrokerLink? Link { get; set; }
        [JsonPropertyName("reconciliation")] public BrokerReconcileState? Reconciliation { get; set; }
    }

    /// <summary>StrategyMonitor (§19.11).</summary>
    internal sealed class MonStrategy
    {
        // Sandbox fixture label (strategy_platform/SPEC.md "Sandbox profile" S4): true + the note only on the synthetic
        // click-test fixture; absent everywhere else. Shown in red wherever this item is shown.
        [JsonPropertyName("fixture")] public bool? Fixture { get; set; }
        [JsonPropertyName("fixture_note")] public string? FixtureNote { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("run_status")] public string? RunStatus { get; set; }
        [JsonPropertyName("calendar")] public string? Calendar { get; set; }
        [JsonPropertyName("sessions")] public int? Sessions { get; set; }
        [JsonPropertyName("last_session")] public string? LastSession { get; set; }
        [JsonPropertyName("realised")] public MonRealised? Realised { get; set; }
        [JsonPropertyName("expected")] public MonExpected? Expected { get; set; }
        [JsonPropertyName("deviation")] public MonDeviation? Deviation { get; set; }
        [JsonPropertyName("decay")] public MonDecay? Decay { get; set; }
        /// <summary>§19.14: the backend could not build this entry — shown with the strategy, never hidden.</summary>
        [JsonPropertyName("error")] public string? Error { get; set; }

        [JsonIgnore] public bool IsPaused => Decay?.State == "paused";
        [JsonIgnore] public string DisplayName => string.IsNullOrWhiteSpace(Name) ? (StrategyId ?? "?") : Name!;
    }

    internal sealed class MonRealised
    {
        [JsonPropertyName("cum_log_return")] public double? CumLogReturn { get; set; }
        [JsonPropertyName("return")] public double? Return { get; set; }
        [JsonPropertyName("vol")] public double? Vol { get; set; }
        [JsonPropertyName("sharpe")] public double? Sharpe { get; set; }
        [JsonPropertyName("drawdown")] public double? Drawdown { get; set; }
        [JsonPropertyName("max_drawdown")] public double? MaxDrawdown { get; set; }
    }

    internal sealed class MonExpected
    {
        [JsonPropertyName("mean_daily")] public double? MeanDaily { get; set; }
        [JsonPropertyName("std_daily")] public double? StdDaily { get; set; }
        [JsonPropertyName("vol")] public double? Vol { get; set; }
        [JsonPropertyName("sharpe")] public double? Sharpe { get; set; }
        [JsonPropertyName("max_drawdown")] public double? MaxDrawdown { get; set; }
        [JsonPropertyName("source_run_id")] public string? SourceRunId { get; set; }
    }

    internal sealed class MonDeviation
    {
        [JsonPropertyName("z_cum")] public double? ZCum { get; set; }
        [JsonPropertyName("band")] public MonBand? Band { get; set; }
        [JsonPropertyName("in_band")] public bool? InBand { get; set; }
        [JsonPropertyName("tracking_error")] public double? TrackingError { get; set; }
        [JsonPropertyName("vol_ratio")] public double? VolRatio { get; set; }
        [JsonPropertyName("dd_ratio")] public double? DdRatio { get; set; }
        [JsonPropertyName("rolling_z")] public double? RollingZ { get; set; }
    }

    internal sealed class MonBand
    {
        [JsonPropertyName("lo")] public double? Lo { get; set; }
        [JsonPropertyName("hi")] public double? Hi { get; set; }
        [JsonPropertyName("n")] public int? N { get; set; }
    }

    internal sealed class MonDecay
    {
        [JsonPropertyName("state")] public string? State { get; set; }          // insufficient | ok | watch | paused
        [JsonPropertyName("last_check_day")] public string? LastCheckDay { get; set; }
        // fix1023 B2 (FIX1023_API.md): the newest check — "check" (scheduled decay check) | "pause" (the pause event) | null
        [JsonPropertyName("last_check_at")] public string? LastCheckAt { get; set; }
        [JsonPropertyName("last_check_source")] public string? LastCheckSource { get; set; }
        [JsonPropertyName("rules")] public List<MonRule>? Rules { get; set; }
        [JsonPropertyName("paused")] public MonPaused? Paused { get; set; }
        /// <summary>§19.14 M5: calibration set of the frozen holdout (equity_24m | equity_12m | equity_6m | crypto_12m).</summary>
        [JsonPropertyName("basis")] public string? Basis { get; set; }
        /// <summary>§19.14 M4: sessions before the latest keep-running decision (only later returns are evaluated); 0 = none.</summary>
        [JsonPropertyName("since_index")] public int? SinceIndex { get; set; }
        /// <summary>§19.14 M3: protective exits executed while paused (count for the run).</summary>
        [JsonPropertyName("protective_exits")] public int? ProtectiveExits { get; set; }
    }

    internal sealed class MonRule
    {
        [JsonPropertyName("rule")] public string? Rule { get; set; }             // return_band | drawdown | volatility
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("breached")] public bool? Breached { get; set; }
        [JsonPropertyName("count")] public int? Count { get; set; }
        [JsonPropertyName("needed")] public int? Needed { get; set; }
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("threshold")] public double? Threshold { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class MonPaused
    {
        // Sandbox force-decay (SPEC "Sandbox profile" S5): synthetic = true + the fixture keys; absent on a real pause.
        [JsonPropertyName("synthetic")] public bool? Synthetic { get; set; }
        [JsonPropertyName("fixture")] public bool? Fixture { get; set; }
        [JsonPropertyName("fixture_note")] public string? FixtureNote { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
        [JsonPropertyName("rule")] public string? Rule { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("check_day")] public string? CheckDay { get; set; }
        // fix1023 B2: the values that triggered THIS pause (from the event); synthetic pauses also carry numbers_note
        [JsonPropertyName("numbers")] public MonPauseNumbers? Numbers { get; set; }
        [JsonPropertyName("numbers_note")] public string? NumbersNote { get; set; }
    }

    internal sealed class MonPauseNumbers
    {
        [JsonPropertyName("rule")] public string? Rule { get; set; }
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("threshold")] public double? Threshold { get; set; }
        [JsonPropertyName("triggered")] public JsonElement? Triggered { get; set; }
        [JsonPropertyName("synthetic")] public bool? Synthetic { get; set; }
    }

    internal sealed class MonStrategyDetail
    {
        [JsonPropertyName("strategy")] public MonStrategy? Strategy { get; set; }
        [JsonPropertyName("series")] public List<MonPoint>? Series { get; set; }
        [JsonPropertyName("checks")] public List<MonCheck>? Checks { get; set; }
        [JsonPropertyName("events")] public List<MonEvent>? Events { get; set; }
        [JsonPropertyName("rules")] public MonDecayRules? Rules { get; set; }
        /// <summary>§19.14 M3: risk-reducing protective exits executed while paused (newest first, ≤ 100).</summary>
        [JsonPropertyName("protective_exits")] public List<MonProtectiveExit>? ProtectiveExits { get; set; }
    }

    internal sealed class MonProtectiveExit
    {
        [JsonPropertyName("alert_id")] public long? AlertId { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("client_order_id")] public string? ClientOrderId { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class MonPoint
    {
        [JsonPropertyName("day")] public string? Day { get; set; }
        [JsonPropertyName("n")] public int? N { get; set; }
        [JsonPropertyName("ret")] public double? Ret { get; set; }
        [JsonPropertyName("cum_log")] public double? CumLog { get; set; }
        [JsonPropertyName("exp_mean")] public double? ExpMean { get; set; }
        [JsonPropertyName("band_lo")] public double? BandLo { get; set; }
        [JsonPropertyName("band_hi")] public double? BandHi { get; set; }
        [JsonPropertyName("decay_lo")] public double? DecayLo { get; set; }
        [JsonPropertyName("drawdown")] public double? Drawdown { get; set; }
        [JsonPropertyName("roll_vol")] public double? RollVol { get; set; }
    }

    internal sealed class MonCheck
    {
        [JsonPropertyName("day")] public string? Day { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("triggered")] public List<string>? Triggered { get; set; }
        // {rule: {value, threshold, count, breached}}
        [JsonPropertyName("stats")] public JsonElement? Stats { get; set; }
    }

    internal sealed class MonEvent
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("action")] public string? Action { get; set; }        // paused | keep_running | demoted
        [JsonPropertyName("actor")] public string? Actor { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("rule")] public string? Rule { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
    }

    internal sealed class MonDecayRules
    {
        [JsonPropertyName("rules")] public Dictionary<string, JsonElement>? Rules { get; set; }
        [JsonPropertyName("text")] public List<string>? Text { get; set; }
        /// <summary>§19.14 M5.</summary>
        [JsonPropertyName("default_basis")] public string? DefaultBasis { get; set; }
        [JsonPropertyName("by_basis")] public Dictionary<string, Dictionary<string, JsonElement>>? ByBasis { get; set; }
        [JsonPropertyName("basis_labels")] public Dictionary<string, string>? BasisLabels { get; set; }
    }

    internal sealed class MonDecisionResult
    {
        [JsonPropertyName("strategy")] public MonStrategy? Strategy { get; set; }
        [JsonPropertyName("event")] public MonEvent? Event { get; set; }
    }

    internal sealed class MonDecayRunResult
    {
        [JsonPropertyName("checked")] public int? Checked { get; set; }
        [JsonPropertyName("paused")] public List<string>? Paused { get; set; }
        [JsonPropertyName("errors")] public Dictionary<string, JsonElement>? Errors { get; set; }
    }

    internal sealed class MonAlert
    {
        [JsonPropertyName("id")] public long Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("severity")] public string? Severity { get; set; }    // critical | warning | info
        [JsonPropertyName("title")] public string? Title { get; set; }
        [JsonPropertyName("body")] public string? Body { get; set; }
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("ref")] public string? Ref { get; set; }
        [JsonPropertyName("toast")] public bool Toast { get; set; }
        [JsonPropertyName("acked")] public bool Acked { get; set; }
        [JsonPropertyName("acked_at")] public string? AckedAt { get; set; }
        [JsonPropertyName("data")] public JsonElement? Data { get; set; }
    }

    internal sealed class MonAlertList
    {
        [JsonPropertyName("alerts")] public List<MonAlert>? Alerts { get; set; }
        [JsonPropertyName("last_id")] public long? LastId { get; set; }
        [JsonPropertyName("unacked")] public int? Unacked { get; set; }
        [JsonPropertyName("kinds")] public Dictionary<string, MonAlertKind>? Kinds { get; set; }
    }

    internal sealed class MonAlertKind
    {
        [JsonPropertyName("severity")] public string? Severity { get; set; }
        [JsonPropertyName("toast")] public bool? Toast { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
    }

    internal sealed class MonAckResult
    {
        [JsonPropertyName("acked")] public int? Acked { get; set; }
        [JsonPropertyName("unacked")] public int? Unacked { get; set; }
    }

    internal sealed class MonReportList
    {
        [JsonPropertyName("reports")] public List<MonReportSummary>? Reports { get; set; }
    }

    internal sealed class MonReportSummary
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("period")] public string? Period { get; set; }
        [JsonPropertyName("period_id")] public string? PeriodId { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("created_by")] public string? CreatedBy { get; set; }
        [JsonPropertyName("start")] public string? Start { get; set; }
        [JsonPropertyName("end")] public string? End { get; set; }
    }

    internal sealed class MonReportEnvelope
    {
        [JsonPropertyName("report")] public MonReport? Report { get; set; }
    }

    internal sealed class MonReport
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("period")] public string? Period { get; set; }
        [JsonPropertyName("period_id")] public string? PeriodId { get; set; }
        [JsonPropertyName("start")] public string? Start { get; set; }
        [JsonPropertyName("end")] public string? End { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("created_by")] public string? CreatedBy { get; set; }
        [JsonPropertyName("numbers")] public MonReportNumbers? Numbers { get; set; }
        [JsonPropertyName("facts")] public List<MonFact>? Facts { get; set; }
        [JsonPropertyName("commentaries")] public List<MonCommentary>? Commentaries { get; set; }
    }

    internal sealed class MonReportNumbers
    {
        [JsonPropertyName("accounts")] public List<MonReportAccount>? Accounts { get; set; }
        [JsonPropertyName("strategies")] public List<MonReportStrategy>? Strategies { get; set; }
        [JsonPropertyName("alerts")] public MonReportAlerts? Alerts { get; set; }
        [JsonPropertyName("brokers")] public List<MonReportBroker>? Brokers { get; set; }
    }

    internal sealed class MonReportAccount
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("paper")] public bool? Paper { get; set; }
        [JsonPropertyName("equity_start")] public double? EquityStart { get; set; }
        [JsonPropertyName("equity_end")] public double? EquityEnd { get; set; }
        [JsonPropertyName("pnl")] public double? Pnl { get; set; }
        [JsonPropertyName("return_pct")] public double? ReturnPct { get; set; }
        [JsonPropertyName("max_drawdown_pct")] public double? MaxDrawdownPct { get; set; }
        [JsonPropertyName("fees")] public double? Fees { get; set; }
        [JsonPropertyName("orders")] public MonReportOrders? Orders { get; set; }
        // count of halt alerts (§19.12) — kept raw: the contract does not fix the type
        [JsonPropertyName("halts")] public JsonElement? Halts { get; set; }
    }

    internal sealed class MonReportOrders
    {
        [JsonPropertyName("total")] public int? Total { get; set; }
        [JsonPropertyName("filled")] public int? Filled { get; set; }
        [JsonPropertyName("rejected")] public int? Rejected { get; set; }
        [JsonPropertyName("cancelled")] public int? Cancelled { get; set; }
    }

    internal sealed class MonReportStrategy
    {
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("sessions")] public int? Sessions { get; set; }
        [JsonPropertyName("log_return")] public double? LogReturn { get; set; }
        [JsonPropertyName("expected_mean")] public double? ExpectedMean { get; set; }
        [JsonPropertyName("band_lo")] public double? BandLo { get; set; }
        [JsonPropertyName("band_hi")] public double? BandHi { get; set; }
        [JsonPropertyName("z")] public double? Z { get; set; }
        [JsonPropertyName("decay_state")] public string? DecayState { get; set; }
    }

    internal sealed class MonReportAlerts
    {
        [JsonPropertyName("total")] public int? Total { get; set; }
        [JsonPropertyName("by_kind")] public Dictionary<string, JsonElement>? ByKind { get; set; }
    }

    internal sealed class MonReportBroker
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
    }

    /// <summary>One fact of a §11.1-shaped fact table: {id, label, value, unit, display}.</summary>
    internal sealed class MonFact
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("value")] public JsonElement? Value { get; set; }
        [JsonPropertyName("unit")] public string? Unit { get; set; }
        [JsonPropertyName("display")] public string? Display { get; set; }
    }

    internal sealed class MonCommentaryEnvelope
    {
        [JsonPropertyName("commentary")] public MonCommentary? Commentary { get; set; }
    }

    /// <summary>Commentary (§19.11): §11.2 output shape, number check, cost, provenance (EU AI Act Art. 50).</summary>
    internal sealed class MonCommentary
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }            // report | shadow
        [JsonPropertyName("ref_id")] public string? RefId { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("model")] public string? Model { get; set; }
        [JsonPropertyName("language")] public string? Language { get; set; }
        [JsonPropertyName("output")] public ExplanationOutput? Output { get; set; }
        [JsonPropertyName("check")] public ExplanationCheck? Check { get; set; }
        [JsonPropertyName("usage")] public JsonElement? Usage { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
    }

    /// <summary>GET /v2/monitoring/tax-notice (§19.15).</summary>
    internal sealed class MonTaxNotice
    {
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("decided")] public string? Decided { get; set; }
        [JsonPropertyName("notice")] public string? Notice { get; set; }
        [JsonPropertyName("paper_notice")] public string? PaperNotice { get; set; }
        [JsonPropertyName("tax_export")] public bool? TaxExport { get; set; }
    }
}

using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================================================
    // S10-C R3 (WPF): the Council of AIs as a platform strategy — /v2/council/* (contract: backend/strategy_platform/SPEC.md
    // §22.8, §22.11, §22.14). The switch-over and the revert are the user's decision in a dialog (CouncilDialogs.cs); nothing
    // here switches anything by itself.
    // =====================================================================================================================
    internal static partial class PlatformClient
    {
        private static readonly TimeSpan CouncilTimeout = TimeSpan.FromSeconds(15);
        /// <summary>The switch-over stops the old engine in-process (a running cycle is awaited ≤ 120 s) before its transaction.</summary>
        private static readonly TimeSpan CouncilSwitchoverTimeout = TimeSpan.FromSeconds(180);

        /// <summary>GET /v2/council/state (header, budget, last run, symbol cards).</summary>
        public static Task<PlatformResult<CouncilState>> GetCouncilStateAsync()
            => SendAsync<CouncilState>(HttpMethod.Get, "/v2/council/state", null, CouncilTimeout);

        /// <summary>GET /v2/council/runs?limit=n (newest first; legacy runs imported at R2 carry legacy = true).</summary>
        public static Task<PlatformResult<CouncilRunList>> GetCouncilRunsAsync(int limit = 30)
            => SendAsync<CouncilRunList>(HttpMethod.Get, "/v2/council/runs?limit=" + Math.Clamp(limit, 1, 200), null, CouncilTimeout);

        /// <summary>POST /v2/council/run → 202 {run_id, started, orders: {equity, crypto}} / 503 not_active.</summary>
        public static Task<PlatformResult<CouncilRunStart>> RunCouncilNowAsync()
            => SendAsync<CouncilRunStart>(HttpMethod.Post, "/v2/council/run", new Dictionary<string, object?>(), CouncilTimeout);

        /// <summary>PUT /v2/council/orders {enabled, reason?} → {orders_enabled, audit_id | cancelled_orders} / 409 halted / not_active.</summary>
        public static Task<PlatformResult<CouncilOrdersResult>> SetCouncilOrdersAsync(bool enabled, string? reason)
            => SendAsync<CouncilOrdersResult>(HttpMethod.Put, "/v2/council/orders",
                WithNote(new Dictionary<string, object?> { ["enabled"] = enabled }, reason), CouncilTimeout);

        /// <summary>GET /v2/council/params → {version, params, limits}.</summary>
        public static Task<PlatformResult<CouncilParams>> GetCouncilParamsAsync()
            => SendAsync<CouncilParams>(HttpMethod.Get, "/v2/council/params", null, CouncilTimeout);

        /// <summary>PUT /v2/council/params {ai_daily_budget_usd, reason?} → a new params version (strict; audit-first).</summary>
        public static Task<PlatformResult<CouncilParams>> SetCouncilBudgetAsync(double usd, string? reason)
            => SendAsync<CouncilParams>(HttpMethod.Put, "/v2/council/params",
                WithNote(new Dictionary<string, object?> { ["ai_daily_budget_usd"] = usd }, reason), CouncilTimeout);

        /// <summary>GET /v2/council/switchover — the dry run (writes nothing).</summary>
        public static Task<PlatformResult<CouncilSwitchoverPlan>> GetCouncilSwitchoverPlanAsync()
            => SendAsync<CouncilSwitchoverPlan>(HttpMethod.Get, "/v2/council/switchover", null, CouncilTimeout);

        /// <summary>POST /v2/council/switchover {reason?} → 200 {state: "done", …} / 409 sandbox / switchover_blocked + blockers / 503.</summary>
        public static Task<PlatformResult<CouncilSwitchoverResult>> SwitchOverToCouncilAsync(string? reason)
            => SendAsync<CouncilSwitchoverResult>(HttpMethod.Post, "/v2/council/switchover", WithNote(new Dictionary<string, object?>(), reason),
                CouncilSwitchoverTimeout);

        /// <summary>
        /// POST /v2/council/switchover/retry {reason?} (SWITCHOVER_FIX_API §3) — only while pending: re-runs the SAME attempt. 200 = the
        /// body of a successful switch-over; 503 switchover_failed {error, code, last_error} keeps the state pending.
        /// </summary>
        public static Task<PlatformResult<CouncilSwitchoverResult>> RetryCouncilSwitchoverAsync(string? reason)
            => SendAsync<CouncilSwitchoverResult>(HttpMethod.Post, "/v2/council/switchover/retry", WithNote(new Dictionary<string, object?>(), reason),
                CouncilSwitchoverTimeout);

        /// <summary>
        /// POST /v2/council/switchover/abort {reason?} (SWITCHOVER_FIX_API §5) — only while pending: back to the old engine.
        /// 200 {state: "none"|"reverted", applied, old_engine_started, old_engine_error} / 409 not_pending.
        /// </summary>
        public static Task<PlatformResult<CouncilAbortResult>> AbortCouncilSwitchoverAsync(string? reason)
            => SendAsync<CouncilAbortResult>(HttpMethod.Post, "/v2/council/switchover/abort", WithNote(new Dictionary<string, object?>(), reason),
                CouncilSwitchoverTimeout);

        /// <summary>The last_error of a 503 switchover_failed body ({error, code, last_error}), or null.</summary>
        public static string? SwitchoverLastError<T>(PlatformResult<T> result)
        {
            if (result.Ok || string.IsNullOrWhiteSpace(result.RawBody)) return null;
            try
            {
                using var doc = JsonDocument.Parse(result.RawBody);
                return doc.RootElement.ValueKind == JsonValueKind.Object && doc.RootElement.TryGetProperty("last_error", out var e)
                       && e.ValueKind == JsonValueKind.String ? e.GetString() : null;
            }
            catch (JsonException) { return null; }
        }

        /// <summary>POST /v2/council/switchover/revert {reason?} → 200 {state: "reverted", old_spend_seed_usd, old_engine_started,
        /// old_engine_error, already?} / 409 not_switched_over (none / pending — pending is aborted instead).</summary>
        public static Task<PlatformResult<CouncilRevertResult>> RevertCouncilSwitchoverAsync(string? reason)
            => SendAsync<CouncilRevertResult>(HttpMethod.Post, "/v2/council/switchover/revert", WithNote(new Dictionary<string, object?>(), reason),
                CouncilSwitchoverTimeout);
    }

    // ---- DTOs (snake_case as in SPEC §22.11; tolerant: unknown fields ignored) ----

    internal sealed class CouncilState
    {
        [JsonPropertyName("active")] public bool Active { get; set; }
        /// <summary>none | pending | done | reverted | unreadable</summary>
        [JsonPropertyName("switchover")] public string? Switchover { get; set; }
        /// <summary>idle | running | not_active | sandbox</summary>
        [JsonPropertyName("task")] public string? TaskState { get; set; }
        [JsonPropertyName("orders_enabled")] public bool OrdersEnabled { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("record")] public CouncilRecord? Record { get; set; }
        [JsonPropertyName("next_due_at")] public string? NextDueAt { get; set; }
        [JsonPropertyName("budget")] public CouncilBudget? Budget { get; set; }
        [JsonPropertyName("last_run")] public CouncilRun? LastRun { get; set; }
        [JsonPropertyName("symbols")] public List<CouncilSymbol>? Symbols { get; set; }
        [JsonPropertyName("mode")] public JsonElement? Mode { get; set; }
        /// <summary>SWITCHOVER_FIX_API §7: the stored last_error of an unfinished (pending) attempt.</summary>
        [JsonPropertyName("switchover_error")] public string? SwitchoverError { get; set; }
        [JsonIgnore] public bool SwitchedOver => Switchover == "done";
    }

    internal sealed class CouncilRecord
    {
        [JsonPropertyName("since")] public string? Since { get; set; }
        [JsonPropertyName("days")] public int? Days { get; set; }
        [JsonPropertyName("text")] public string? Text { get; set; }
    }

    internal sealed class CouncilBudget
    {
        [JsonPropertyName("limit_usd")] public double? LimitUsd { get; set; }
        [JsonPropertyName("spent_usd")] public double? SpentUsd { get; set; }
        /// <summary>ok | reached | off | unreadable</summary>
        [JsonPropertyName("status")] public string? Status { get; set; }
    }

    internal sealed class CouncilRun
    {
        [JsonPropertyName("id")] public long Id { get; set; }
        [JsonPropertyName("trigger")] public string? Trigger { get; set; }
        [JsonPropertyName("started_at")] public string? StartedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("judge_mode")] public string? JudgeMode { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        [JsonPropertyName("params_version")] public long? ParamsVersion { get; set; }
        [JsonPropertyName("orders_enabled")] public bool? OrdersEnabled { get; set; }
        [JsonPropertyName("legacy")] public bool? Legacy { get; set; }
        [JsonPropertyName("summary")] public JsonElement? Summary { get; set; }
    }

    internal sealed class CouncilRunList
    {
        [JsonPropertyName("runs")] public List<CouncilRun>? Runs { get; set; }
    }

    internal sealed class CouncilSymbol
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("action")] public string? Action { get; set; }
        [JsonPropertyName("confidence")] public double? Confidence { get; set; }
        [JsonPropertyName("judge_source")] public string? JudgeSource { get; set; }
        [JsonPropertyName("rationale")] public string? Rationale { get; set; }
        [JsonPropertyName("ai_generated")] public bool AiGenerated { get; set; }
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
        [JsonPropertyName("agents")] public List<CouncilAgent>? Agents { get; set; }
        [JsonPropertyName("target_weight")] public double? TargetWeight { get; set; }
        [JsonPropertyName("position")] public CouncilPosition? Position { get; set; }
        [JsonPropertyName("execution")] public CouncilExecution? Execution { get; set; }
        [JsonPropertyName("unavailable_reason")] public string? UnavailableReason { get; set; }
    }

    internal sealed class CouncilAgent
    {
        [JsonPropertyName("agent")] public string? Agent { get; set; }
        [JsonPropertyName("score")] public double? Score { get; set; }
        [JsonPropertyName("confidence")] public double? Confidence { get; set; }
        [JsonPropertyName("summary")] public string? Summary { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("ai_generated")] public bool AiGenerated { get; set; }
    }

    internal sealed class CouncilPosition
    {
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("weight")] public double? Weight { get; set; }
    }

    internal sealed class CouncilExecution
    {
        [JsonPropertyName("outcome")] public string? Outcome { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("client_order_id")] public string? ClientOrderId { get; set; }
        [JsonPropertyName("order_state")] public string? OrderState { get; set; }
    }

    internal sealed class CouncilRunStart
    {
        [JsonPropertyName("run_id")] public long? RunId { get; set; }
        [JsonPropertyName("started")] public bool? Started { get; set; }
        [JsonPropertyName("orders")] public JsonElement? Orders { get; set; }
    }

    internal sealed class CouncilOrdersResult
    {
        [JsonPropertyName("orders_enabled")] public bool OrdersEnabled { get; set; }
        [JsonPropertyName("audit_id")] public long? AuditId { get; set; }
        [JsonPropertyName("cancelled_orders")] public int? CancelledOrders { get; set; }
    }

    internal sealed class CouncilParams
    {
        [JsonPropertyName("version")] public long? Version { get; set; }
        [JsonPropertyName("params")] public JsonElement? Params { get; set; }
        [JsonPropertyName("limits")] public JsonElement? Limits { get; set; }
    }

    internal sealed class CouncilBlocker
    {
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class CouncilSwitchoverPlan
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("blockers")] public List<CouncilBlocker>? Blockers { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("parity_accepted")] public string? ParityAccepted { get; set; }
        [JsonPropertyName("params")] public CouncilSwitchoverParams? Params { get; set; }
        [JsonPropertyName("heartbeat_minutes")] public JsonElement? HeartbeatMinutes { get; set; }
        [JsonPropertyName("spend_seed_usd")] public double? SpendSeedUsd { get; set; }
        [JsonPropertyName("legacy")] public CouncilLegacyCounts? Legacy { get; set; }
        [JsonPropertyName("pending")] public CouncilPendingAttempt? Pending { get; set; }
        [JsonPropertyName("actions")] public CouncilSwitchoverActions? Actions { get; set; }
    }

    /// <summary>SWITCHOVER_FIX_API §2: the unfinished attempt while the state is pending.</summary>
    internal sealed class CouncilPendingAttempt
    {
        [JsonPropertyName("tx")] public string? Tx { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
        [JsonPropertyName("actor")] public string? Actor { get; set; }
        [JsonPropertyName("previous")] public string? Previous { get; set; }
        [JsonPropertyName("applied")] public bool Applied { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
        [JsonPropertyName("error_at")] public string? ErrorAt { get; set; }
    }

    /// <summary>SWITCHOVER_FIX_API §2: what the dialog may offer now.</summary>
    internal sealed class CouncilSwitchoverActions
    {
        [JsonPropertyName("run")] public bool Run { get; set; }
        [JsonPropertyName("retry")] public bool Retry { get; set; }
        [JsonPropertyName("abort")] public bool Abort { get; set; }
        [JsonPropertyName("revert")] public bool Revert { get; set; }
    }

    internal sealed class CouncilSwitchoverParams
    {
        [JsonPropertyName("from_version")] public JsonElement? FromVersion { get; set; }
        [JsonPropertyName("council")] public JsonElement? Council { get; set; }
        [JsonPropertyName("notes")] public List<string>? Notes { get; set; }
    }

    internal sealed class CouncilLegacyCounts
    {
        [JsonPropertyName("runs")] public int? Runs { get; set; }
        [JsonPropertyName("signals")] public int? Signals { get; set; }
        [JsonPropertyName("recommendations")] public int? Recommendations { get; set; }
        [JsonPropertyName("hwm_before")] public JsonElement? HwmBefore { get; set; }
        [JsonPropertyName("hwm_after")] public JsonElement? HwmAfter { get; set; }
    }

    internal sealed class CouncilSwitchoverResult
    {
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("tx")] public string? Tx { get; set; }
        [JsonPropertyName("params_version")] public long? ParamsVersion { get; set; }
        [JsonPropertyName("spend_seed_usd")] public double? SpendSeedUsd { get; set; }
        [JsonPropertyName("legacy")] public JsonElement? Legacy { get; set; }
        [JsonPropertyName("hwm")] public JsonElement? Hwm { get; set; }
        [JsonPropertyName("council_task_started")] public bool? CouncilTaskStarted { get; set; }
        [JsonPropertyName("council_task_error")] public string? CouncilTaskError { get; set; }
    }

    internal sealed class CouncilRevertResult
    {
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("old_spend_seed_usd")] public double? OldSpendSeedUsd { get; set; }
        [JsonPropertyName("old_engine_started")] public bool? OldEngineStarted { get; set; }
        [JsonPropertyName("old_engine_error")] public string? OldEngineError { get; set; }
        [JsonPropertyName("already")] public bool? Already { get; set; }
    }

    internal sealed class CouncilAbortResult
    {
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("applied")] public bool? Applied { get; set; }
        [JsonPropertyName("old_engine_started")] public bool? OldEngineStarted { get; set; }
        [JsonPropertyName("old_engine_error")] public string? OldEngineError { get; set; }
    }

    /// <summary>The Council half of POST /v2/oms/kill (SPEC §22.8): {applicable, ok, orders_enabled, cancelled_orders, error}.</summary>
    internal sealed class TradingKillCouncil
    {
        [JsonPropertyName("applicable")] public bool Applicable { get; set; }
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
        [JsonPropertyName("orders_enabled")] public bool? OrdersEnabled { get; set; }
        [JsonPropertyName("cancelled_orders")] public int? CancelledOrders { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
    }
}

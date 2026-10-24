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
    // Strategy Platform stage 4 — /v2 validation, holdout and gate-settings routes (contract:
    // backend/strategy_platform/SPEC.md §9.4/§9.5). Same rules as the rest of PlatformClient:
    // session token on every call, never throws, the backend's `error` text is shown verbatim
    // (PlatformResult.Message), no retries and no alternative paths. One job at a time (shared
    // with backtests): a second POST → 409.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan ValidationTimeout = TimeSpan.FromSeconds(20);
        // A finished validation card carries the training run, folds, grid and stress blocks.
        private static readonly TimeSpan ValidationCardTimeout = TimeSpan.FromSeconds(45);

        /// <summary>POST /v2/validations {strategy_id, version?, history_from?, auto_resume} → 202 {run_id}.</summary>
        public static Task<PlatformResult<BacktestStarted>> StartValidationAsync(string strategyId, int? version, string? historyFrom, bool autoResume)
        {
            var body = new Dictionary<string, object?> { ["strategy_id"] = strategyId };
            if (version is int v) body["version"] = v;
            if (!string.IsNullOrWhiteSpace(historyFrom)) body["history_from"] = historyFrom;
            body["auto_resume"] = autoResume;   // SPEC §10.3 (default false)
            return SendAsync<BacktestStarted>(HttpMethod.Post, "/v2/validations", body, ValidationTimeout);
        }

        /// <summary>GET /v2/validations?strategy_id= — newest first.</summary>
        public static Task<PlatformResult<ValidationRunList>> ListValidationsAsync(string? strategyId, int limit = 50)
        {
            var query = "?limit=" + limit.ToString(CultureInfo.InvariantCulture);
            if (!string.IsNullOrWhiteSpace(strategyId)) query += "&strategy_id=" + Esc(strategyId!);
            return SendAsync<ValidationRunList>(HttpMethod.Get, "/v2/validations" + query, null, ValidationTimeout);
        }

        /// <summary>GET /v2/validations/{id} — progress while running, the report when finished.</summary>
        public static Task<PlatformResult<ValidationCard>> GetValidationAsync(string runId)
            => SendAsync<ValidationCard>(HttpMethod.Get, "/v2/validations/" + Esc(runId), null, ValidationCardTimeout);

        /// <summary>POST /v2/validations/{id}/cancel (same rules as the backtest cancel).</summary>
        public static Task<PlatformResult<JsonElement>> CancelValidationAsync(string runId)
            => SendAsync<JsonElement>(HttpMethod.Post, "/v2/validations/" + Esc(runId) + "/cancel",
                new Dictionary<string, object?>(), ValidationTimeout);

        /// <summary>POST /v2/validations/{id}/exceptions {gate, reason} → the updated validation card.</summary>
        public static Task<PlatformResult<ValidationCard>> AddGateExceptionAsync(string runId, string gate, string reason)
            => SendAsync<ValidationCard>(HttpMethod.Post, "/v2/validations/" + Esc(runId) + "/exceptions",
                new Dictionary<string, object?> { ["gate"] = gate, ["reason"] = reason }, ValidationTimeout);

        /// <summary>GET /v2/strategies/{id}/holdout — eligibility, window, family/universe events.</summary>
        public static Task<PlatformResult<HoldoutStatus>> GetHoldoutAsync(string strategyId)
            => SendAsync<HoldoutStatus>(HttpMethod.Get, "/v2/strategies/" + Esc(strategyId) + "/holdout", null, StrategyTimeout);

        /// <summary>POST /v2/strategies/{id}/holdout {confirm: exact strategy name} → 202 {run_id}; wrong name → 400.</summary>
        public static Task<PlatformResult<BacktestStarted>> OpenHoldoutAsync(string strategyId, string confirm)
            => SendAsync<BacktestStarted>(HttpMethod.Post, "/v2/strategies/" + Esc(strategyId) + "/holdout",
                new Dictionary<string, object?> { ["confirm"] = confirm }, ValidationTimeout);

        public static Task<PlatformResult<GateSettings>> GetGateSettingsAsync()
            => SendAsync<GateSettings>(HttpMethod.Get, "/v2/settings/gates", null, DefaultTimeout);

        /// <summary>PUT /v2/settings/gates {preset, overrides} — audited by the backend.</summary>
        public static Task<PlatformResult<GateSettings>> PutGateSettingsAsync(string preset, Dictionary<string, double>? overrides)
            => SendAsync<GateSettings>(HttpMethod.Put, "/v2/settings/gates",
                new Dictionary<string, object?> { ["preset"] = preset, ["overrides"] = overrides ?? new Dictionary<string, double>() }, DefaultTimeout);
    }

    // =====================================================================================
    // DTOs — field names as in SPEC §9.4/§9.5 and api/routes.py (_val_list, _val_detail, _holdout_status;
    // validation/report.py, validation/jobs.py, validation/gates.get_settings). Nested report blocks are kept
    // as JsonElement and read by name in ValidationReportView (tolerant of added fields). No
    // computed DTO properties without [JsonIgnore] (PropertyNameCaseInsensitive would map them).
    // =====================================================================================

    internal sealed class ValidationRunList
    {
        [JsonPropertyName("runs")] public List<ValidationRunSummary>? Runs { get; set; }
    }

    internal sealed class ValidationRunSummary
    {
        // Sandbox fixture label (strategy_platform/SPEC.md "Sandbox profile" S4): true + the note only on the synthetic
        // click-test fixture; absent everywhere else. Shown in red wherever this item is shown.
        [JsonPropertyName("fixture")] public bool? Fixture { get; set; }
        [JsonPropertyName("fixture_note")] public string? FixtureNote { get; set; }
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("strategy_name")] public string? StrategyName { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("range")] public JsonElement? Range { get; set; }
        [JsonPropertyName("trial_number")] public int? TrialNumber { get; set; }
        [JsonPropertyName("key_metrics")] public JsonElement? KeyMetrics { get; set; }
        // merged with the exceptions (null while not done)
        [JsonPropertyName("passed")] public bool? Passed { get; set; }
        [JsonPropertyName("exceptions")] public int? Exceptions { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // api/routes.py _resume_fields (SPEC §10.3/§10.4): resume chain and error classification (null when the backend does not send them —
        // a missing `resumable` never shows the Resume button).
        [JsonPropertyName("resumed_from")] public string? ResumedFrom { get; set; }
        [JsonPropertyName("resume_count")] public int? ResumeCount { get; set; }
        [JsonPropertyName("error_kind")] public string? ErrorKind { get; set; }
        [JsonPropertyName("resumable")] public bool? Resumable { get; set; }
        [JsonPropertyName("auto_resume")] public bool? AutoResume { get; set; }
    }

    internal sealed class ValidationCard
    {
        // Sandbox fixture label (strategy_platform/SPEC.md "Sandbox profile" S4): true + the note only on the synthetic
        // click-test fixture; absent everywhere else. Shown in red wherever this item is shown.
        [JsonPropertyName("fixture")] public bool? Fixture { get; set; }
        [JsonPropertyName("fixture_note")] public string? FixtureNote { get; set; }
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("started_at")] public string? StartedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("strategy_name")] public string? StrategyName { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("content_hash")] public string? ContentHash { get; set; }
        [JsonPropertyName("trial_number")] public int? TrialNumber { get; set; }
        [JsonPropertyName("request")] public JsonElement? Request { get; set; }
        [JsonPropertyName("range")] public JsonElement? Range { get; set; }
        // validation: {version, passed (merged), passed_raw, mode, gates, folds, grid, pbo, dsr, stress, trial_count,
        //              thresholds {preset, overrides, effective}, training, ic, holdout (eligibility snapshot), exceptions}
        // holdout:    {version, passed, mode, gates [H], window, range, validation_run_id, training_sharpe, eligibility, metrics}
        [JsonPropertyName("report")] public JsonElement? Report { get; set; }
        [JsonPropertyName("passed")] public bool? Passed { get; set; }
        [JsonPropertyName("key_metrics")] public JsonElement? KeyMetrics { get; set; }
        [JsonPropertyName("warnings")] public List<BacktestWarning>? Warnings { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // api/routes.py _resume_fields (SPEC §10.3/§10.4): resume chain and error classification (null when the backend does not send them —
        // a missing `resumable` never shows the Resume button).
        [JsonPropertyName("resumed_from")] public string? ResumedFrom { get; set; }
        [JsonPropertyName("resume_count")] public int? ResumeCount { get; set; }
        [JsonPropertyName("error_kind")] public string? ErrorKind { get; set; }
        [JsonPropertyName("resumable")] public bool? Resumable { get; set; }
        [JsonPropertyName("auto_resume")] public bool? AutoResume { get; set; }
        [JsonPropertyName("progress")] public BacktestProgress? Progress { get; set; }
        // SPEC §11.1/§11.5 (X1): the stored attribution (validation/holdout: + gate_facts, facts appended) and, when it is
        // null, the backend's reason (old engine / not finished / failed) — shown verbatim, never recomputed.
        [JsonPropertyName("attribution")] public JsonElement? Attribution { get; set; }
        [JsonPropertyName("attribution_unavailable")] public string? AttributionUnavailable { get; set; }
    }

    /// <summary>{gate, status: pass|fail|exception, value, threshold, explanation, details} (SPEC §9.4).</summary>
    internal sealed class GateResult
    {
        [JsonPropertyName("gate")] public string? Gate { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("value")] public JsonElement? Value { get; set; }
        [JsonPropertyName("threshold")] public JsonElement? Threshold { get; set; }
        [JsonPropertyName("explanation")] public string? Explanation { get; set; }
        [JsonPropertyName("details")] public JsonElement? Details { get; set; }
        [JsonPropertyName("exception")] public JsonElement? Exception { get; set; }
    }

    internal sealed class HoldoutStatus
    {
        [JsonPropertyName("eligible")] public bool Eligible { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("window")] public JsonElement? Window { get; set; }
        [JsonPropertyName("unseen_from")] public string? UnseenFrom { get; set; }
        [JsonPropertyName("eligible_from")] public string? EligibleFrom { get; set; }
        // validation/families.py holdout_eligibility(): family_events / universe_events are COUNTS (int).
        [JsonPropertyName("family_events")] public JsonElement? FamilyEvents { get; set; }
        [JsonPropertyName("universe_events")] public JsonElement? UniverseEvents { get; set; }
        [JsonPropertyName("mode")] public string? Mode { get; set; }
        [JsonPropertyName("contaminated")] public bool? Contaminated { get; set; }
        [JsonPropertyName("holdout_failed_at")] public string? HoldoutFailedAt { get; set; }
        [JsonPropertyName("seen_until")] public string? SeenUntil { get; set; }
        [JsonPropertyName("universe_kind")] public string? UniverseKind { get; set; }
        [JsonPropertyName("holdout_start")] public string? HoldoutStart { get; set; }
        [JsonPropertyName("training_end")] public string? TrainingEnd { get; set; }
        // routes._holdout_status additions: the button needs eligibility AND a passing validation of the current version.
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("validation_run_id")] public string? ValidationRunId { get; set; }
        [JsonPropertyName("validation_passed")] public bool? ValidationPassed { get; set; }
        [JsonPropertyName("can_open")] public bool? CanOpen { get; set; }
        [JsonPropertyName("blocked_reason")] public string? BlockedReason { get; set; }
    }

    internal sealed class GateSettings
    {
        [JsonPropertyName("preset")] public string? Preset { get; set; }
        [JsonPropertyName("overrides")] public JsonElement? Overrides { get; set; }
        // validation/gates.py get_settings(): {preset, overrides, effective, updated_at, presets {lenient|standard|strict: {key: n}}, ranges}
        [JsonPropertyName("effective")] public JsonElement? Effective { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("presets")] public JsonElement? Presets { get; set; }
        [JsonPropertyName("ranges")] public JsonElement? Ranges { get; set; }
    }

    internal static class ValidationStatus
    {
        /// <summary>queued / running / waiting (SPEC §10.3).</summary>
        public static bool IsActive(string? status) => status == "queued" || status == "running" || status == RunResume.WaitingStatus;
    }
}

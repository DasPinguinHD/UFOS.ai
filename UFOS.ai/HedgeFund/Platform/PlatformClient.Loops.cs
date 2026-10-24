using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform stage 6 — discovery loop (contract: backend/strategy_platform/SPEC.md §16, exact JSON in
    // §16.12). Same rules as the rest of PlatformClient: session token on every call, never throws, the backend's
    // `error` text is shown verbatim (402 budget / 409 busy, not_running, not_resumable, autonomous, decided /
    // 404 not_found / 503 spend_unrecorded, no_model, settings_invalid …), no retries and no alternative paths.
    // POST /v2/loops answers 202 at once (the loop runs in the backend's orchestrator thread); the view polls
    // GET /v2/loops/{id}?events_after= every 2 s while it is visible.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan LoopTimeout = TimeSpan.FromSeconds(20);

        /// <summary>SPEC §16.12: idea text 1..4,000 characters.</summary>
        public const int LoopIdeaMaxChars = 4000;
        /// <summary>SPEC §16.12: reject reason ≤ 500 characters.</summary>
        public const int LoopRejectReasonMaxChars = 500;

        /// <summary>GET /v2/loops?limit= → {loops (newest first), ai_interaction_notice}.</summary>
        public static Task<PlatformResult<LoopList>> ListLoopsAsync(int limit = 50)
            => SendAsync<LoopList>(HttpMethod.Get, "/v2/loops?limit=" + limit.ToString(CultureInfo.InvariantCulture), null, LoopTimeout);

        /// <summary>GET /v2/loops/{id}?events_after= → the loop payload (events only newer than <paramref name="eventsAfter"/>).</summary>
        public static Task<PlatformResult<LoopDetail>> GetLoopAsync(string loopId, long? eventsAfter)
            => SendAsync<LoopDetail>(HttpMethod.Get, "/v2/loops/" + Esc(loopId)
                + (eventsAfter is long a ? "?events_after=" + a.ToString(CultureInfo.InvariantCulture) : ""), null, LoopTimeout);

        /// <summary>POST /v2/loops → 202 loop payload. Seed: exactly one of strategy (+ optional version), idea text, paper.</summary>
        public static Task<PlatformResult<LoopDetail>> StartLoopAsync(LoopStartRequest r)
        {
            Dictionary<string, object?> seed = r.SeedKind switch
            {
                "strategy" => r.StrategyVersion is int v
                    ? new Dictionary<string, object?> { ["kind"] = "strategy", ["strategy_id"] = r.StrategyId, ["version"] = v }
                    : new Dictionary<string, object?> { ["kind"] = "strategy", ["strategy_id"] = r.StrategyId },
                "idea" => new Dictionary<string, object?> { ["kind"] = "idea", ["text"] = r.IdeaText },
                "paper" => new Dictionary<string, object?> { ["kind"] = "paper", ["paper_id"] = r.PaperId },
                _ => throw new ArgumentException("Unknown seed kind.", nameof(r)),
            };
            if (r.Model != ExplainModelStandard && r.Model != ExplainModelPremium)
                throw new ArgumentException("Unknown model tier.", nameof(r));
            var body = new Dictionary<string, object?>
            {
                ["seed"] = seed,
                ["variants"] = r.Variants,
                ["generations"] = r.Generations,
                ["search_points"] = r.SearchPoints,
                ["model"] = r.Model,
                ["history_from"] = r.HistoryFrom2000 ? "2000" : "2016",
                ["auto_resume"] = r.AutoResume,
            };
            return SendAsync<LoopDetail>(HttpMethod.Post, "/v2/loops", body, LoopTimeout);
        }

        /// <summary>GET /v2/loops/estimate?… → planned backtests / AI calls / USD + projected trials (shown before "Start").</summary>
        public static Task<PlatformResult<LoopEstimate>> EstimateLoopAsync(string? strategyId, bool idea, int variants, int generations, int searchPoints, string model)
        {
            var inv = CultureInfo.InvariantCulture;
            var q = "?idea=" + (idea ? "true" : "false")
                    + "&variants=" + variants.ToString(inv)
                    + "&generations=" + generations.ToString(inv)
                    + "&search_points=" + searchPoints.ToString(inv)
                    + "&model=" + Uri.EscapeDataString(model);
            if (!string.IsNullOrWhiteSpace(strategyId)) q += "&strategy_id=" + Esc(strategyId!);
            return SendAsync<LoopEstimate>(HttpMethod.Get, "/v2/loops/estimate" + q, null, LoopTimeout);
        }

        /// <summary>POST /v2/loops/{id}/cancel (no body) — stops after the current step, cancels the loop's running job.</summary>
        public static Task<PlatformResult<LoopDetail>> CancelLoopAsync(string loopId)
            => SendAsync<LoopDetail>(HttpMethod.Post, "/v2/loops/" + Esc(loopId) + "/cancel", null, LoopTimeout);

        /// <summary>POST /v2/loops/{id}/pause (no body) — the running job finishes, then the slot is free.</summary>
        public static Task<PlatformResult<LoopDetail>> PauseLoopAsync(string loopId)
            => SendAsync<LoopDetail>(HttpMethod.Post, "/v2/loops/" + Esc(loopId) + "/pause", null, LoopTimeout);

        /// <summary>POST /v2/loops/{id}/resume (no body) — continues from the stored events (paused, paused_budget, interrupted).</summary>
        public static Task<PlatformResult<LoopDetail>> ResumeLoopAsync(string loopId)
            => SendAsync<LoopDetail>(HttpMethod.Post, "/v2/loops/" + Esc(loopId) + "/resume", null, LoopTimeout);

        /// <summary>POST /v2/loops/{id}/finalists/{variant_id}/approve (no body) — the validation (G1–G8, no holdout) is submitted when the slot is free.</summary>
        public static Task<PlatformResult<LoopDetail>> ApproveLoopFinalistAsync(string loopId, string variantId)
            => SendAsync<LoopDetail>(HttpMethod.Post, "/v2/loops/" + Esc(loopId) + "/finalists/" + Esc(variantId) + "/approve", null, LoopTimeout);

        /// <summary>POST /v2/loops/{id}/finalists/{variant_id}/reject {reason?: ≤ 500}.</summary>
        public static Task<PlatformResult<LoopDetail>> RejectLoopFinalistAsync(string loopId, string variantId, string? reason)
        {
            var body = new Dictionary<string, object?>();
            if (!string.IsNullOrWhiteSpace(reason)) body["reason"] = reason!.Trim();
            return SendAsync<LoopDetail>(HttpMethod.Post, "/v2/loops/" + Esc(loopId) + "/finalists/" + Esc(variantId) + "/reject", body, LoopTimeout);
        }

        /// <summary>GET /v2/settings/loop → {autonomous, budget (descriptor), defaults, limits, autonomous_note, holdout_note}.</summary>
        public static Task<PlatformResult<LoopSettings>> GetLoopSettingsAsync()
            => SendAsync<LoopSettings>(HttpMethod.Get, "/v2/settings/loop", null, DefaultTimeout);

        /// <summary>PUT /v2/settings/loop {autonomous?, monthly_usd?, daily_usd?, per_run_usd?} (≥ 1 field, 0..200, 0 = off) — audited.</summary>
        public static Task<PlatformResult<LoopSettings>> PutLoopSettingsAsync(bool? autonomous, double? monthlyUsd, double? dailyUsd, double? perRunUsd)
        {
            var body = new Dictionary<string, object?>();
            if (autonomous is bool a) body["autonomous"] = a;
            if (monthlyUsd is double m) body["monthly_usd"] = m;
            if (dailyUsd is double d) body["daily_usd"] = d;
            if (perRunUsd is double p) body["per_run_usd"] = p;
            if (body.Count == 0) throw new ArgumentException("At least one setting is required.");
            return SendAsync<LoopSettings>(HttpMethod.Put, "/v2/settings/loop", body, DefaultTimeout);
        }

        /// <summary>The `loop_id` of a 409 body (busy: one loop at a time; or a manual job refused while a loop job holds the slot).</summary>
        public static string? ReadConflictLoopId<T>(PlatformResult<T> result)
        {
            if (string.IsNullOrWhiteSpace(result.RawBody)) return null;
            try
            {
                using var doc = JsonDocument.Parse(result.RawBody);
                var root = doc.RootElement;
                if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("loop_id", out var p)
                    && p.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(p.GetString()))
                    return p.GetString();
            }
            catch (JsonException) { /* non-JSON body: no loop id */ }
            return null;
        }

        /// <summary>The `code` field of an error body (e.g. "busy", "budget"), or null.</summary>
        public static string? ReadErrorCode<T>(PlatformResult<T> result)
        {
            if (string.IsNullOrWhiteSpace(result.RawBody)) return null;
            try
            {
                using var doc = JsonDocument.Parse(result.RawBody);
                var root = doc.RootElement;
                if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("code", out var p) && p.ValueKind == JsonValueKind.String)
                    return p.GetString();
            }
            catch (JsonException) { /* non-JSON body */ }
            return null;
        }
    }

    /// <summary>Input of <see cref="PlatformClient.StartLoopAsync"/> (built by the start dialog).</summary>
    internal sealed class LoopStartRequest
    {
        /// <summary>strategy | idea | paper</summary>
        public string SeedKind { get; init; } = "strategy";
        public string? StrategyId { get; init; }
        public int? StrategyVersion { get; init; }
        public string? IdeaText { get; init; }
        public string? PaperId { get; init; }
        public int Variants { get; init; } = 6;
        public int Generations { get; init; } = 4;
        public int SearchPoints { get; init; } = 1;
        public string Model { get; init; } = PlatformClient.ExplainModelStandard;
        public bool HistoryFrom2000 { get; init; }
        public bool AutoResume { get; init; } = true;
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §16.12. Blocks whose shape varies or is only read for display
    // (progress, counts, trials — may be {unavailable} —, budget, score, key_metrics, selection, AI output,
    // number check, provenance, usage, event data) stay JsonElement and are read by name (BtJson) in LoopView.
    // Computed accessors are [JsonIgnore] (PropertyNameCaseInsensitive would otherwise collide with real fields).
    // Seed texts, strategy names and AI texts are untrusted: plain TextBlocks only.
    // =====================================================================================

    internal sealed class LoopList
    {
        [JsonPropertyName("loops")] public List<LoopSummary>? Loops { get; set; }
        [JsonPropertyName("ai_interaction_notice")] public string? AiInteractionNotice { get; set; }
    }

    internal sealed class LoopSummary
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("status_reason")] public string? StatusReason { get; set; }
        [JsonPropertyName("seed")] public LoopSeed? Seed { get; set; }
        [JsonPropertyName("seed_text")] public string? SeedText { get; set; }
        [JsonPropertyName("root_strategy_id")] public string? RootStrategyId { get; set; }
        [JsonPropertyName("family_id")] public string? FamilyId { get; set; }
        [JsonPropertyName("config")] public LoopConfig? Config { get; set; }
        [JsonPropertyName("generations_done")] public int? GenerationsDone { get; set; }
        [JsonPropertyName("best_score")] public double? BestScore { get; set; }
        [JsonPropertyName("finalists")] public int? Finalists { get; set; }
        [JsonPropertyName("backtests_started")] public int? BacktestsStarted { get; set; }
    }

    /// <summary>{kind: strategy, strategy_id, version?} | {kind: idea, text} | {kind: paper, paper_id} (+ strategy_id once resolved).</summary>
    internal sealed class LoopSeed
    {
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("paper_id")] public string? PaperId { get; set; }
        [JsonPropertyName("text")] public string? Text { get; set; }
    }

    /// <summary>The loop's config, incl. the "fully autonomous" switch snapshot at its start.</summary>
    internal sealed class LoopConfig
    {
        [JsonPropertyName("variants")] public int? Variants { get; set; }
        [JsonPropertyName("generations")] public int? Generations { get; set; }
        [JsonPropertyName("search_points")] public int? SearchPoints { get; set; }
        [JsonPropertyName("model")] public string? Model { get; set; }
        [JsonPropertyName("history_from")] public string? HistoryFrom { get; set; }
        [JsonPropertyName("auto_resume")] public bool? AutoResume { get; set; }
        [JsonPropertyName("autonomous")] public bool? Autonomous { get; set; }
    }

    internal sealed class LoopDetail
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        /// <summary>running | waiting_slot | waiting_connection | paused | paused_budget | interrupted | done | failed | cancelled</summary>
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("persisted_status")] public string? PersistedStatus { get; set; }
        [JsonPropertyName("status_reason")] public string? StatusReason { get; set; }
        [JsonPropertyName("active")] public bool? Active { get; set; }
        [JsonPropertyName("seed")] public LoopSeed? Seed { get; set; }
        [JsonPropertyName("root_strategy_id")] public string? RootStrategyId { get; set; }
        [JsonPropertyName("family_id")] public string? FamilyId { get; set; }
        [JsonPropertyName("config")] public LoopConfig? Config { get; set; }
        // {phase, message, updated_at, generation, variant_id, slot, run_id, percent} | null
        [JsonPropertyName("progress")] public JsonElement? Progress { get; set; }
        // §8.11 progress of the loop's running job {phase, percent, message, updated_at} | null
        [JsonPropertyName("job_progress")] public JsonElement? JobProgress { get; set; }
        // {backtests_started, backtests_planned, variants, failed, duplicates, ai_messages}
        [JsonPropertyName("counts")] public JsonElement? Counts { get; set; }
        // {trials_now, planned_backtests, trials_projected, sr0_now, sr0_projected, …, note} | {unavailable}
        [JsonPropertyName("trials")] public JsonElement? Trials { get; set; }
        // {spent_run_usd, ai_messages_cost_usd, limits {monthly_usd, daily_usd, per_run_usd} | {unavailable}, paused_reason}
        [JsonPropertyName("budget")] public JsonElement? Budget { get; set; }
        [JsonPropertyName("run_cap_reached")] public bool? RunCapReached { get; set; }
        [JsonPropertyName("generations")] public List<LoopGeneration>? Generations { get; set; }
        [JsonPropertyName("finalists")] public List<LoopFinalist>? Finalists { get; set; }
        [JsonPropertyName("finalists_note")] public string? FinalistsNote { get; set; }
        [JsonPropertyName("done_note")] public string? DoneNote { get; set; }
        [JsonPropertyName("notes")] public List<string>? Notes { get; set; }
        [JsonPropertyName("events")] public List<LoopEvent>? Events { get; set; }
        [JsonPropertyName("last_event_id")] public long? LastEventId { get; set; }
        [JsonPropertyName("can")] public LoopCan? Can { get; set; }
        [JsonPropertyName("ai_interaction_notice")] public string? AiInteractionNotice { get; set; }
        [JsonPropertyName("holdout_note")] public string? HoldoutNote { get; set; }
    }

    internal sealed class LoopCan
    {
        [JsonPropertyName("cancel")] public bool? Cancel { get; set; }
        [JsonPropertyName("pause")] public bool? Pause { get; set; }
        [JsonPropertyName("resume")] public bool? Resume { get; set; }
    }

    internal sealed class LoopGeneration
    {
        [JsonPropertyName("generation")] public int? Generation { get; set; }
        [JsonPropertyName("variants")] public List<LoopVariant>? Variants { get; set; }
        // {elites, refine, culled, duplicates {vid: {of, correlation}}, k, c, reasons {vid: text}, ranking} | null
        [JsonPropertyName("selection")] public JsonElement? Selection { get; set; }
    }

    internal sealed class LoopVariant
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("generation")] public int? Generation { get; set; }
        [JsonPropertyName("slot")] public int? Slot { get; set; }
        /// <summary>seed | mutation | refined | child | elite</summary>
        [JsonPropertyName("origin")] public string? Origin { get; set; }
        [JsonPropertyName("parent_variant_id")] public string? ParentVariantId { get; set; }
        [JsonPropertyName("carried_from")] public string? CarriedFrom { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("strategy_name")] public string? StrategyName { get; set; }
        [JsonPropertyName("versions")] public List<LoopVariantVersion>? Versions { get; set; }
        [JsonPropertyName("chosen_version")] public int? ChosenVersion { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        /// <summary>planned | ai | saved | backtesting | scored | failed | duplicate</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        // score card (§16.2) | null
        [JsonPropertyName("score")] public JsonElement? Score { get; set; }
        // {sharpe, total_return, cagr, max_drawdown} | null
        [JsonPropertyName("key_metrics")] public JsonElement? KeyMetrics { get; set; }
        /// <summary>passed | failed | pending | skipped | null</summary>
        [JsonPropertyName("topic_check")] public string? TopicCheck { get; set; }
        [JsonPropertyName("diagnosis")] public LoopAiMessage? Diagnosis { get; set; }
        [JsonPropertyName("refinement")] public LoopAiMessage? Refinement { get; set; }
    }

    internal sealed class LoopVariantVersion
    {
        [JsonPropertyName("version")] public int? Version { get; set; }
        /// <summary>base | search</summary>
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("parameter")] public string? Parameter { get; set; }
        [JsonPropertyName("value")] public JsonElement? Value { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("run_status")] public string? RunStatus { get; set; }
        [JsonPropertyName("reused")] public bool? Reused { get; set; }
        [JsonPropertyName("score")] public double? Score { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
    }

    /// <summary>One AI text of a variant (diagnosis | refinement). `output` is shown ONLY when not null.</summary>
    internal sealed class LoopAiMessage
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonIgnore] public string? Id => StudioJson.IdText(IdRaw);
        [JsonPropertyName("role")] public string? Role { get; set; }
        [JsonPropertyName("pending")] public bool? Pending { get; set; }
        [JsonPropertyName("discarded")] public bool? Discarded { get; set; }
        [JsonPropertyName("discarded_reason")] public string? DiscardedReason { get; set; }
        [JsonPropertyName("topic_check")] public string? TopicCheck { get; set; }
        // diagnosis {summary, weaknesses, ideas} | refinement {kind, reply, assumptions, operator_requests, spec, valid,
        // validation, repaired, repair_errors, task, aspect, operator_requests_label} | draft_invalid {validation, spec} | null
        [JsonPropertyName("output")] public JsonElement? Output { get; set; }
        // {ok, unverified: [{text, offset, section}], unknown_ids} | null (diagnosis only)
        [JsonPropertyName("check")] public JsonElement? Check { get; set; }
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
        // {cost_usd, estimate_usd, prompt_tokens, completion_tokens}
        [JsonPropertyName("usage")] public JsonElement? Usage { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
    }

    internal sealed class LoopFinalist
    {
        [JsonPropertyName("variant_id")] public string? VariantId { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("evaluated_version")] public int? EvaluatedVersion { get; set; }
        [JsonPropertyName("promoted")] public bool? Promoted { get; set; }
        [JsonPropertyName("score")] public double? Score { get; set; }
        [JsonPropertyName("sharpe")] public double? Sharpe { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        /// <summary>proposed | approved | rejected | validating | passed_gates | failed_gates | validation_failed |
        /// validation_cancelled | validation_start_failed</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("validation_run_id")] public string? ValidationRunId { get; set; }
        [JsonPropertyName("validation_status")] public string? ValidationStatus { get; set; }
        [JsonPropertyName("validation_error")] public string? ValidationError { get; set; }
        [JsonPropertyName("passed")] public bool? Passed { get; set; }
        /// <summary>"candidate" | null — the next lifecycle step the USER may take (the loop never changes a lifecycle state).</summary>
        [JsonPropertyName("eligible_state")] public string? EligibleState { get; set; }
        [JsonPropertyName("reject_reason")] public string? RejectReason { get; set; }
        [JsonPropertyName("decided_at")] public string? DecidedAt { get; set; }
    }

    internal sealed class LoopEvent
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("event")] public string? Event { get; set; }
        [JsonPropertyName("generation")] public int? Generation { get; set; }
        [JsonPropertyName("variant_id")] public string? VariantId { get; set; }
        [JsonPropertyName("data")] public JsonElement? Data { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
    }

    internal sealed class LoopEstimate
    {
        [JsonPropertyName("backtests_planned")] public int? BacktestsPlanned { get; set; }
        [JsonPropertyName("ai_calls_planned")] public int? AiCallsPlanned { get; set; }
        [JsonPropertyName("estimate_per_call_usd")] public double? EstimatePerCallUsd { get; set; }
        [JsonPropertyName("estimate_usd")] public double? EstimateUsd { get; set; }
        [JsonPropertyName("validations_if_autonomous")] public int? ValidationsIfAutonomous { get; set; }
        [JsonPropertyName("trials")] public JsonElement? Trials { get; set; }
        // budget descriptor (same as GET /v2/settings/loop `budget`)
        [JsonPropertyName("budget")] public JsonElement? Budget { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    /// <summary>GET/PUT /v2/settings/loop. Nullable on purpose: a missing field is shown as unset, never a default.</summary>
    internal sealed class LoopSettings
    {
        [JsonPropertyName("autonomous")] public bool? Autonomous { get; set; }
        // descriptor {key, module, label, cost_log_label, features, limits [{key, label, value, default, min, max, period}],
        // spent {today_usd, month_usd}, day, month, enabled, zero_means, paused_reason}
        [JsonPropertyName("budget")] public JsonElement? Budget { get; set; }
        [JsonPropertyName("defaults")] public LoopDefaults? Defaults { get; set; }
        // {variants: [2, 8], generations: [1, 5], search_points: [0, 3], max_backtests: 120}
        [JsonPropertyName("limits")] public JsonElement? Limits { get; set; }
        [JsonPropertyName("autonomous_note")] public string? AutonomousNote { get; set; }
        [JsonPropertyName("holdout_note")] public string? HoldoutNote { get; set; }
    }

    internal sealed class LoopDefaults
    {
        [JsonPropertyName("variants")] public int? Variants { get; set; }
        [JsonPropertyName("generations")] public int? Generations { get; set; }
        [JsonPropertyName("search_points")] public int? SearchPoints { get; set; }
    }

    /// <summary>Display texts shared by the Loop view and its dialogs (computed from the backend's values, no defaults invented).</summary>
    internal static class LoopText
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        public static readonly string[] Terminal = { "done", "failed", "cancelled" };

        public static bool IsTerminal(string? status) => status != null && Array.IndexOf(Terminal, status) >= 0;

        public static string StatusLabel(string? status) => status switch
        {
            "running" => "Running",
            "waiting_slot" => "Waiting for the backtest slot",
            "waiting_connection" => "Waiting for the connection",
            "paused" => "Paused",
            "paused_budget" => "Paused — loop AI budget reached",
            "interrupted" => "Interrupted",
            "done" => "Done",
            "failed" => "Failed",
            "cancelled" => "Cancelled",
            null or "" => "Status not reported",
            _ => status!,
        };

        public static System.Windows.Media.Brush StatusBrush(string? status) => status switch
        {
            "running" => FundPalette.Focus,
            "waiting_slot" or "waiting_connection" or "paused" or "paused_budget" or "interrupted" => FundPalette.Risk,
            "done" => FundPalette.Long,
            "failed" => FundPalette.Kill,
            _ => FundPalette.Dim,
        };

        /// <summary>"Strategy s-… v2" / "Idea: …" / "Paper p-…" — <paramref name="names"/> maps strategy ids to names when known.</summary>
        public static string SeedLabel(LoopSeed? seed, string? seedText, IReadOnlyDictionary<string, string>? names, int maxIdeaChars = 70)
        {
            switch (seed?.Kind)
            {
                case "strategy":
                {
                    var id = seed.StrategyId ?? "?";
                    var name = id != "?" && names != null && names.TryGetValue(id, out var n) && !string.IsNullOrWhiteSpace(n) ? n + " (" + id + ")" : id;
                    return "Strategy " + name + (seed.Version is int v ? " · v" + v.ToString(Inv) : "");
                }
                case "idea":
                {
                    var text = (seedText ?? seed.Text ?? "").Replace('\r', ' ').Replace('\n', ' ').Trim();
                    if (text.Length > maxIdeaChars) text = text[..maxIdeaChars].TrimEnd() + "…";
                    return "Idea: " + (text.Length == 0 ? "(no text)" : text);
                }
                case "paper":
                    return "Paper " + (seed.PaperId ?? "?");
                default:
                    return "Seed not reported";
            }
        }

        /// <summary>"Family trials: 5 now → 35 after this loop; G4 bar (expected max Sharpe) 1.61 → 2.65", or the backend's reason.</summary>
        public static string TrialsLine(JsonElement? trials)
        {
            if (trials is not JsonElement t || t.ValueKind != JsonValueKind.Object) return "Projected G4 impact: not reported by the backend.";
            if (BtJson.Str(t, "unavailable") is { Length: > 0 } why) return "Projected G4 impact unavailable: " + why;
            var now = BtJson.Num(t, "trials_now");
            var proj = BtJson.Num(t, "trials_projected");
            var s0 = BtJson.Num(t, "sr0_now");
            var s1 = BtJson.Num(t, "sr0_projected");
            return "Family trials: " + (now is double a ? a.ToString("0", Inv) : "—") + " now → " + (proj is double b ? b.ToString("0", Inv) : "—")
                   + " after this loop" + (BtJson.Num(t, "validations_planned") is double vp && vp > 0 ? " (incl. " + vp.ToString("0", Inv) + " finalist validations)" : "")
                   + "; G4 bar (expected max Sharpe) " + FundFormat.Num(s0) + " → " + FundFormat.Num(s1)
                   + (BtJson.Str(t, "variance_source") == "minimum" ? " (trial variance at its floor)" : "");
        }

        /// <summary>A limit value of a budget descriptor (`limits[key].value`), or null.</summary>
        public static double? DescriptorLimit(JsonElement? budget, string key)
        {
            foreach (var l in BtJson.Array(budget, "limits"))
                if (BtJson.Str(l, "key") == key) return BtJson.Num(l, "value");
            return null;
        }

        /// <summary>"Loop AI budget: per run $1.00 · today $0.40 of $2.00 (UTC 2026-10-17) · month 2026-10 $1.23 of $10.00".</summary>
        public static string DescriptorLine(JsonElement? budget)
        {
            if (budget is not JsonElement b || b.ValueKind != JsonValueKind.Object) return "Loop AI budget: not reported by the backend.";
            var text = "Loop AI budget: per run " + Usd2OrUnset(DescriptorLimit(b, "per_run_usd"))
                       + " · today" + (BtJson.Str(b, "day") is { Length: > 0 } d ? " (UTC " + d + ")" : "") + " "
                       + FundFormat.Usd(BtJson.Num(b, "spent", "today_usd")) + " of " + Usd2OrUnset(DescriptorLimit(b, "daily_usd"))
                       + " · month" + (BtJson.Str(b, "month") is { Length: > 0 } m ? " " + m : "") + " "
                       + FundFormat.Usd(BtJson.Num(b, "spent", "month_usd")) + " of " + Usd2OrUnset(DescriptorLimit(b, "monthly_usd"));
            if (BtJson.Bool(b, "enabled") == false) text += " · loop AI is off (a limit is 0)";
            if (BtJson.Str(b, "paused_reason") is { Length: > 0 } p) text += " · paused: " + p;
            return text;
        }

        public static string Usd2OrUnset(double? v) => v is double x ? FundFormat.Usd2(x) : "(not set)";

        public static string TierLabel(string? tier) => tier switch
        {
            "standard" => "Standard",
            "premium" => "Premium",
            null or "" => "tier not reported",
            _ => tier!,
        };

        public static string Score(double? v) => v is double x ? (x < 0 ? FundFormat.Minus + Math.Abs(x).ToString("0.000", Inv) : x.ToString("0.000", Inv)) : "—";
    }
}

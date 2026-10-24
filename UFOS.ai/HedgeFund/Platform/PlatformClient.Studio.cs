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
    // Strategy Platform stage 5a — Strategy Studio + ideation budget (contract: backend/strategy_platform/
    // SPEC.md §13.1/§13.3, routes/JSON §13.5). Same rules as the rest of PlatformClient: session token on
    // every call, never throws, the backend's `error` text is shown verbatim (402 budget / 409 busy /
    // 502 bad_json / 503 no_key, spend_unrecorded / 504 timeout …), no retries and no alternative paths.
    // POST …/messages is synchronous: up to 2 LLM calls (answer + one repair round) of ≤ 90 s each, so the
    // client waits longer than that (StudioMessageTimeout); the UI stays responsive (async).
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan StudioTimeout = TimeSpan.FromSeconds(20);
        /// <summary>2 × 90 s LLM + prompt build, validation, storage and the backtest dispatch.</summary>
        private static readonly TimeSpan StudioMessageTimeout = TimeSpan.FromSeconds(210);

        /// <summary>SPEC §13.5: text 1..4,000 characters.</summary>
        public const int StudioMaxMessageChars = 4000;
        /// <summary>SPEC §13.5: title ≤ 120 characters.</summary>
        public const int StudioMaxTitleChars = 120;

        /// <summary>GET /v2/studio/sessions?limit= → {sessions (newest first)}.</summary>
        public static Task<PlatformResult<StudioSessionList>> ListStudioSessionsAsync(int limit = 50)
            => SendAsync<StudioSessionList>(HttpMethod.Get, "/v2/studio/sessions?limit=" + limit.ToString(CultureInfo.InvariantCulture), null, StudioTimeout);

        /// <summary>POST /v2/studio/sessions {title?} → the new session.</summary>
        public static Task<PlatformResult<StudioSession>> CreateStudioSessionAsync(string? title)
        {
            var body = new Dictionary<string, object?>();
            if (!string.IsNullOrWhiteSpace(title)) body["title"] = title!.Trim();
            return SendAsync<StudioSession>(HttpMethod.Post, "/v2/studio/sessions", body, StudioTimeout);
        }

        /// <summary>GET /v2/studio/sessions/{id} → {session, messages (oldest first), versions, runs, trial_count, queue, estimate_usd, budget}.</summary>
        public static Task<PlatformResult<StudioSessionDetail>> GetStudioSessionAsync(string sessionId)
            => SendAsync<StudioSessionDetail>(HttpMethod.Get, "/v2/studio/sessions/" + Esc(sessionId), null, StudioTimeout);

        /// <summary>POST /v2/studio/sessions/{id}/messages {text, model} → {user_message, assistant_message, notes}. Costs money.</summary>
        public static Task<PlatformResult<StudioPostResult>> PostStudioMessageAsync(string sessionId, string text, string model)
        {
            if (model != ExplainModelStandard && model != ExplainModelPremium)
                throw new ArgumentException("Unknown model tier.", nameof(model));
            var body = new Dictionary<string, object?> { ["text"] = text, ["model"] = model };
            return SendAsync<StudioPostResult>(HttpMethod.Post, "/v2/studio/sessions/" + Esc(sessionId) + "/messages", body, StudioMessageTimeout);
        }

        /// <summary>POST /v2/studio/sessions/{id}/drafts/{message_id}/cancel (no body) → draft_backtest; 409 not_waiting.
        /// A started run is cancelled with <see cref="CancelBacktestAsync"/>.</summary>
        public static Task<PlatformResult<DraftBacktest>> CancelStudioDraftAsync(string sessionId, string messageId)
            => SendAsync<DraftBacktest>(HttpMethod.Post, "/v2/studio/sessions/" + Esc(sessionId) + "/drafts/" + Esc(messageId) + "/cancel", null, StudioTimeout);

        /// <summary>POST /v2/studio/sessions/{id}/drafts/{message_id}/save {confirm: true} — saves a draft that
        /// requires_confirmation as a new version (demotes the strategy to draft) and starts/queues its backtest.
        /// 409 not_confirmable / stale and 400 carry the backend's text verbatim.</summary>
        public static Task<PlatformResult<StudioDraftSaved>> SaveStudioDraftAsync(string sessionId, string messageId)
            => SendAsync<StudioDraftSaved>(HttpMethod.Post, "/v2/studio/sessions/" + Esc(sessionId) + "/drafts/" + Esc(messageId) + "/save",
                new Dictionary<string, object?> { ["confirm"] = true }, StudioTimeout);

        /// <summary>GET /v2/settings/ideation → budget, spend of the UTC month/day, limits.</summary>
        public static Task<PlatformResult<IdeationSettings>> GetIdeationSettingsAsync()
            => SendAsync<IdeationSettings>(HttpMethod.Get, "/v2/settings/ideation", null, DefaultTimeout);

        /// <summary>PUT /v2/settings/ideation {monthly_budget_usd, daily_cap_usd} (both required, 0..200, 0 = off) — audited.</summary>
        public static Task<PlatformResult<IdeationSettings>> PutIdeationSettingsAsync(double monthlyBudgetUsd, double dailyCapUsd)
            => SendAsync<IdeationSettings>(HttpMethod.Put, "/v2/settings/ideation",
                new Dictionary<string, object?> { ["monthly_budget_usd"] = monthlyBudgetUsd, ["daily_cap_usd"] = dailyCapUsd }, DefaultTimeout);
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §13.5. Ids whose JSON type the contract does not fix (message ids)
    // are kept raw and read as text ([JsonIgnore] accessors — PropertyNameCaseInsensitive would otherwise map them).
    // spec / key_metrics / usage / provenance stay JsonElement (read by name in StudioView).
    // =====================================================================================

    internal sealed class StudioSessionList
    {
        [JsonPropertyName("sessions")] public List<StudioSession>? Sessions { get; set; }
        /// <summary>Fixed EU AI Act Art. 50(1) notice ("You are interacting with an AI system …") — pinned in the Studio.</summary>
        [JsonPropertyName("ai_interaction_notice")] public string? AiInteractionNotice { get; set; }
    }

    internal sealed class StudioSession
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("title")] public string? Title { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("family_id")] public string? FamilyId { get; set; }
        [JsonPropertyName("trial_count")] public int? TrialCount { get; set; }
        [JsonPropertyName("message_count")] public int? MessageCount { get; set; }
        [JsonPropertyName("last_message_at")] public string? LastMessageAt { get; set; }
    }

    internal sealed class StudioSessionDetail
    {
        [JsonPropertyName("session")] public StudioSession? Session { get; set; }
        [JsonPropertyName("messages")] public List<StudioMessage>? Messages { get; set; }
        [JsonPropertyName("versions")] public List<StudioVersion>? Versions { get; set; }
        [JsonPropertyName("runs")] public List<StudioRun>? Runs { get; set; }
        [JsonPropertyName("trial_count")] public int? TrialCount { get; set; }
        /// <summary>Drafts waiting for the backtest slot (FIFO).</summary>
        [JsonPropertyName("queue")] public List<DraftBacktest>? Queue { get; set; }
        /// <summary>Pre-call estimate of the next message per tier (same {standard, premium} shape as §11.6).</summary>
        [JsonPropertyName("estimate_usd")] public ExplainEstimate? EstimateUsd { get; set; }
        [JsonPropertyName("budget")] public IdeationSettings? Budget { get; set; }
        /// <summary>Fixed EU AI Act Art. 50(1) notice — pinned at the top of the session and above the composer.</summary>
        [JsonPropertyName("ai_interaction_notice")] public string? AiInteractionNotice { get; set; }
    }

    internal sealed class StudioVersion
    {
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("content_hash")] public string? ContentHash { get; set; }
        [JsonPropertyName("message_id")] public JsonElement? MessageIdRaw { get; set; }
        [JsonIgnore] public string? MessageId => StudioJson.IdText(MessageIdRaw);
    }

    internal sealed class StudioRun
    {
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("key_metrics")] public JsonElement? KeyMetrics { get; set; }
    }

    internal sealed class StudioPostResult
    {
        [JsonPropertyName("user_message")] public StudioMessage? UserMessage { get; set; }
        [JsonPropertyName("assistant_message")] public StudioMessage? AssistantMessage { get; set; }
        [JsonPropertyName("notes")] public List<StudioMessage>? Notes { get; set; }
    }

    internal sealed class StudioMessage
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonIgnore] public string? Id => StudioJson.IdText(IdRaw);
        [JsonPropertyName("session_id")] public string? SessionId { get; set; }
        /// <summary>user | assistant | system_note</summary>
        [JsonPropertyName("role")] public string? Role { get; set; }
        /// <summary>Assistant messages: draft | clarify | off_topic (null for user / system_note).</summary>
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        /// <summary>An AI answer the backend's scope guard discarded: text is empty and NOT shown; cost still booked.</summary>
        [JsonPropertyName("discarded")] public bool? Discarded { get; set; }
        [JsonPropertyName("discarded_reason")] public string? DiscardedReason { get; set; }
        /// <summary>passed | failed | skipped — the independent topic check of the displayable texts (§13.5 review H1).</summary>
        [JsonPropertyName("topic_check")] public string? TopicCheck { get; set; }
        [JsonPropertyName("text")] public string? Text { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("spec_version")] public int? SpecVersion { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("operator_requests")] public List<StudioOperatorRequest>? OperatorRequests { get; set; }
        [JsonPropertyName("assumptions")] public List<string>? Assumptions { get; set; }
        [JsonPropertyName("draft")] public StudioDraft? Draft { get; set; }
        [JsonPropertyName("memory_used")] public List<StudioMemoryFact>? MemoryUsed { get; set; }
        [JsonPropertyName("model")] public string? Model { get; set; }
        [JsonPropertyName("model_tier")] public string? ModelTier { get; set; }
        // {…EU AI Act label…, model_tier, prompt_hash, session_id} — read with AiContent / FundAi
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
        // {cost_usd, prompt_tokens, completion_tokens, calls, estimate_usd}
        [JsonPropertyName("usage")] public JsonElement? Usage { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
    }

    /// <summary>A missing operator the AI proposes — text only, never executed ("unverified AI suggestion").</summary>
    internal sealed class StudioOperatorRequest
    {
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("purpose")] public string? Purpose { get; set; }
        [JsonPropertyName("signature")] public string? Signature { get; set; }
    }

    internal sealed class StudioDraft
    {
        /// <summary>The proposed StrategySpec as returned by the AI (after repair).</summary>
        [JsonPropertyName("spec")] public JsonElement? Spec { get; set; }
        [JsonPropertyName("valid")] public bool? Valid { get; set; }
        [JsonPropertyName("validation")] public StrategyValidation? Validation { get; set; }
        [JsonPropertyName("repaired")] public bool? Repaired { get; set; }
        [JsonPropertyName("repair_errors")] public List<StrategyIssue>? RepairErrors { get; set; }
        [JsonPropertyName("repair_skipped")] public string? RepairSkipped { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("unchanged")] public bool? Unchanged { get; set; }
        [JsonPropertyName("backtest")] public DraftBacktest? Backtest { get; set; }
        // Review fix M1 (SPEC §13.5): a valid follow-up draft of a candidate/validated/retired strategy is NOT saved
        // automatically — "demotes <state> to draft" until the user confirms (SaveStudioDraftAsync).
        [JsonPropertyName("requires_confirmation")] public string? RequiresConfirmation { get; set; }
        /// <summary>A valid draft that could not be saved (valid stays true); the reason in <see cref="NotSavedReason"/>.</summary>
        [JsonPropertyName("not_saved")] public bool? NotSaved { get; set; }
        [JsonPropertyName("not_saved_reason")] public string? NotSavedReason { get; set; }
        [JsonPropertyName("saved_by_confirmation")] public bool? SavedByConfirmation { get; set; }
        /// <summary>The stored version the draft builds on (the spec diff compares against it).</summary>
        [JsonPropertyName("base_version")] public int? BaseVersion { get; set; }
        /// <summary>Paper confirm (§14.7): set when the draft answer was discarded (off_topic, topic_check_failed, draft_invalid …).</summary>
        [JsonPropertyName("discarded_reason")] public string? DiscardedReason { get; set; }
    }

    /// <summary>POST …/drafts/{message_id}/save → {draft, notes}.</summary>
    internal sealed class StudioDraftSaved
    {
        [JsonPropertyName("draft")] public StudioDraft? Draft { get; set; }
        [JsonPropertyName("notes")] public List<StudioMessage>? Notes { get; set; }
    }

    internal sealed class DraftBacktest
    {
        [JsonPropertyName("message_id")] public JsonElement? MessageIdRaw { get; set; }
        [JsonIgnore] public string? MessageId => StudioJson.IdText(MessageIdRaw);
        /// <summary>waiting | started | cancelled | start_failed | not_queued</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        /// <summary>1-based queue position (waiting only).</summary>
        [JsonPropertyName("position")] public int? Position { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        /// <summary>queued | running | waiting | done | failed | cancelled | null</summary>
        [JsonPropertyName("run_status")] public string? RunStatus { get; set; }
        // {total_return, cagr, sharpe, max_drawdown, volatility}
        [JsonPropertyName("key_metrics")] public JsonElement? KeyMetrics { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    /// <summary>memory_fact_short (in messages): computed facts only, no AI text.</summary>
    internal sealed class StudioMemoryFact
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("family_id")] public string? FamilyId { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("text")] public string? Text { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
    }

    /// <summary>GET/PUT /v2/settings/ideation. Nullable on purpose: a missing field is shown as unset, never a default.</summary>
    internal sealed class IdeationSettings
    {
        [JsonPropertyName("monthly_budget_usd")] public double? MonthlyBudgetUsd { get; set; }
        [JsonPropertyName("daily_cap_usd")] public double? DailyCapUsd { get; set; }
        [JsonPropertyName("spent_month_usd")] public double? SpentMonthUsd { get; set; }
        [JsonPropertyName("spent_today_usd")] public double? SpentTodayUsd { get; set; }
        [JsonPropertyName("remaining_month_usd")] public double? RemainingMonthUsd { get; set; }
        [JsonPropertyName("remaining_today_usd")] public double? RemainingTodayUsd { get; set; }
        [JsonPropertyName("month")] public string? Month { get; set; }
        [JsonPropertyName("day")] public string? Day { get; set; }
        [JsonPropertyName("enabled")] public bool? Enabled { get; set; }
        [JsonPropertyName("default_monthly_budget_usd")] public double? DefaultMonthlyBudgetUsd { get; set; }
        [JsonPropertyName("default_daily_cap_usd")] public double? DefaultDailyCapUsd { get; set; }
        [JsonPropertyName("max_monthly_budget_usd")] public double? MaxMonthlyBudgetUsd { get; set; }
        [JsonPropertyName("max_daily_cap_usd")] public double? MaxDailyCapUsd { get; set; }
        [JsonPropertyName("paused_reason")] public string? PausedReason { get; set; }
    }

    internal static class StudioJson
    {
        /// <summary>A raw id (string or number) as text, or null when absent.</summary>
        public static string? IdText(JsonElement? e)
        {
            var s = StrategyJson.Scalar(e);
            return string.IsNullOrEmpty(s) ? null : s;
        }

        /// <summary>The ideation budget line: "Ideation AI budget: month 2026-10 $1.23 of $10.00 · today $0.40 of $2.00".</summary>
        public static string BudgetLine(IdeationSettings b)
        {
            var text = "Ideation AI budget: month" + (string.IsNullOrWhiteSpace(b.Month) ? "" : " " + b.Month) + " "
                       + FundFormat.Usd(b.SpentMonthUsd) + " of " + (b.MonthlyBudgetUsd is double m ? FundFormat.Usd2(m) : "(not set)")
                       + " · today" + (string.IsNullOrWhiteSpace(b.Day) ? "" : " (UTC " + b.Day + ")") + " "
                       + FundFormat.Usd(b.SpentTodayUsd) + " of " + (b.DailyCapUsd is double d ? FundFormat.Usd2(d) : "(not set)");
            if (b.Enabled == false) text += " · AI ideation is off (monthly budget 0)";
            if (!string.IsNullOrWhiteSpace(b.PausedReason)) text += " · paused: " + b.PausedReason;
            return text;
        }
    }
}

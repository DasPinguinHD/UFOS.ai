using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform stage 5b — investment committee (contract: backend/strategy_platform/SPEC.md §14.3/§14.5,
    // routes/JSON §14.7). Same rules as the rest of PlatformClient: session token, never throws, the backend's
    // `error` text verbatim (busy, no_validation, budget …), no retries. POST returns 202 at once; the five role
    // calls run in the backend and are polled with GET /v2/committee/{id}. The memo is advisory only.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan CommitteeTimeout = TimeSpan.FromSeconds(20);
        // the estimate builds the five prompts once
        private static readonly TimeSpan CommitteeEstimateTimeout = TimeSpan.FromSeconds(30);

        public static Task<PlatformResult<CommitteeStarted>> StartCommitteeAsync(string strategyId, int? version, string? validationRunId, string model)
        {
            if (model != ExplainModelStandard && model != ExplainModelPremium) throw new ArgumentException("Unknown model tier.", nameof(model));
            var body = new Dictionary<string, object?> { ["strategy_id"] = strategyId, ["model"] = model };
            if (version is int v) body["version"] = v;
            if (!string.IsNullOrWhiteSpace(validationRunId)) body["validation_run_id"] = validationRunId;
            return SendAsync<CommitteeStarted>(HttpMethod.Post, "/v2/committee", body, CommitteeTimeout);
        }

        /// <summary>GET /v2/committee/estimate?strategy_id=&amp;version=&amp;validation_run_id=&amp;model= → {estimate_usd, roles}.</summary>
        public static Task<PlatformResult<CommitteeEstimate>> GetCommitteeEstimateAsync(string strategyId, int? version, string? validationRunId, string model)
        {
            var q = "?strategy_id=" + Esc(strategyId) + "&model=" + Esc(model);
            if (version is int v) q += "&version=" + v.ToString(CultureInfo.InvariantCulture);
            if (!string.IsNullOrWhiteSpace(validationRunId)) q += "&validation_run_id=" + Esc(validationRunId!);
            return SendAsync<CommitteeEstimate>(HttpMethod.Get, "/v2/committee/estimate" + q, null, CommitteeEstimateTimeout);
        }

        /// <summary>GET /v2/committee?strategy_id=&amp;validation_run_id=&amp;limit= → {runs} (newest first).</summary>
        public static Task<PlatformResult<CommitteeRunList>> ListCommitteeRunsAsync(string? strategyId, string? validationRunId, int limit = 20)
        {
            var q = "?limit=" + limit.ToString(CultureInfo.InvariantCulture);
            if (!string.IsNullOrWhiteSpace(strategyId)) q += "&strategy_id=" + Esc(strategyId!);
            if (!string.IsNullOrWhiteSpace(validationRunId)) q += "&validation_run_id=" + Esc(validationRunId!);
            return SendAsync<CommitteeRunList>(HttpMethod.Get, "/v2/committee" + q, null, CommitteeTimeout);
        }

        /// <summary>GET /v2/committee/{id} → {run, messages (role order), memo, memo_check}.</summary>
        public static Task<PlatformResult<CommitteeDetail>> GetCommitteeAsync(string runId)
            => SendAsync<CommitteeDetail>(HttpMethod.Get, "/v2/committee/" + Esc(runId), null, CommitteeTimeout);

        /// <summary>POST /v2/committee/{id}/cancel (no body) — stops before the next role (charged so far).</summary>
        public static Task<PlatformResult<CommitteeStarted>> CancelCommitteeAsync(string runId)
            => SendAsync<CommitteeStarted>(HttpMethod.Post, "/v2/committee/" + Esc(runId) + "/cancel", null, CommitteeTimeout);

        public static Task<PlatformResult<CommitteeSettings>> GetCommitteeSettingsAsync()
            => SendAsync<CommitteeSettings>(HttpMethod.Get, "/v2/settings/committee", null, DefaultTimeout);

        /// <summary>PUT /v2/settings/committee {auto_after_validation, model} — audited.</summary>
        public static Task<PlatformResult<CommitteeSettings>> PutCommitteeSettingsAsync(bool autoAfterValidation, string model)
            => SendAsync<CommitteeSettings>(HttpMethod.Put, "/v2/settings/committee",
                new Dictionary<string, object?> { ["auto_after_validation"] = autoAfterValidation, ["model"] = model }, DefaultTimeout);
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §14.7. Role outputs and the memo are AI output (labelled in the UI);
    // check = the §11.2 number check ({ok, unverified [{text, offset, section}], unknown_ids}).
    // =====================================================================================

    internal sealed class CommitteeStarted
    {
        [JsonPropertyName("run")] public CommitteeRun? Run { get; set; }
    }

    internal sealed class CommitteeEstimate
    {
        [JsonPropertyName("estimate_usd")] public double? EstimateUsd { get; set; }
        [JsonPropertyName("roles")] public int? Roles { get; set; }
    }

    internal sealed class CommitteeRunList
    {
        [JsonPropertyName("runs")] public List<CommitteeRun>? Runs { get; set; }
    }

    internal sealed class CommitteeRun
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("validation_run_id")] public string? ValidationRunId { get; set; }
        [JsonPropertyName("model_tier")] public string? ModelTier { get; set; }
        [JsonPropertyName("model")] public string? Model { get; set; }
        /// <summary>manual | auto</summary>
        [JsonPropertyName("trigger")] public string? Trigger { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        /// <summary>running | done | failed | cancelled | skipped | interrupted</summary>
        [JsonPropertyName("status")] public string? Status { get; set; }
        /// <summary>e.g. "committee skipped: &lt;budget message&gt;" — shown verbatim.</summary>
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("progress")] public CommitteeProgress? Progress { get; set; }
        [JsonPropertyName("roles")] public List<string>? Roles { get; set; }
        [JsonPropertyName("holdout_included")] public bool? HoldoutIncluded { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
    }

    internal sealed class CommitteeProgress
    {
        [JsonPropertyName("done")] public int? Done { get; set; }
        [JsonPropertyName("total")] public int? Total { get; set; }
        [JsonPropertyName("current_role")] public string? CurrentRole { get; set; }
    }

    internal sealed class CommitteeDetail
    {
        [JsonPropertyName("run")] public CommitteeRun? Run { get; set; }
        [JsonPropertyName("messages")] public List<CommitteeMessage>? Messages { get; set; }
        [JsonPropertyName("memo")] public CommitteeMemo? Memo { get; set; }
        [JsonPropertyName("memo_check")] public ExplanationCheck? MemoCheck { get; set; }
    }

    internal sealed class CommitteeMessage
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("role")] public string? Role { get; set; }
        [JsonPropertyName("seq")] public int? Seq { get; set; }
        /// <summary>roles 1–4: {summary, points}; chair: the memo — kept raw and read by name.</summary>
        [JsonPropertyName("output")] public JsonElement? Output { get; set; }
        [JsonPropertyName("check")] public ExplanationCheck? Check { get; set; }
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
        [JsonPropertyName("usage")] public JsonElement? Usage { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        /// <summary>true when the role's text failed the topic check: output.summary is then the backend's fixed note (non-AI).</summary>
        [JsonPropertyName("discarded")] public bool? Discarded { get; set; }
        [JsonPropertyName("discarded_reason")] public string? DiscardedReason { get; set; }
    }

    internal sealed class CommitteeMemo
    {
        [JsonPropertyName("summary")] public string? Summary { get; set; }
        [JsonPropertyName("arguments_for")] public List<string>? ArgumentsFor { get; set; }
        [JsonPropertyName("arguments_against")] public List<string>? ArgumentsAgainst { get; set; }
        [JsonPropertyName("confidence")] public CommitteeConfidence? Confidence { get; set; }
        [JsonPropertyName("sizing")] public CommitteeSizing? Sizing { get; set; }
        [JsonPropertyName("stop_rules")] public List<string>? StopRules { get; set; }
        [JsonPropertyName("open_questions")] public List<string>? OpenQuestions { get; set; }
    }

    internal sealed class CommitteeConfidence
    {
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class CommitteeSizing
    {
        [JsonPropertyName("percent_of_capital")] public double? PercentOfCapital { get; set; }
        [JsonPropertyName("rules")] public List<string>? Rules { get; set; }
    }

    internal sealed class CommitteeSettings
    {
        [JsonPropertyName("auto_after_validation")] public bool? AutoAfterValidation { get; set; }
        /// <summary>standard | premium</summary>
        [JsonPropertyName("model")] public string? Model { get; set; }
    }
}

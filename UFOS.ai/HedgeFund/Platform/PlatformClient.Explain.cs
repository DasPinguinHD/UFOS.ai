using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform §11 — "Explain": AI explanations of a run's attribution (contract:
    // backend/strategy_platform/SPEC.md §11.2, X2 notes §11.6). Same rules as the rest of
    // PlatformClient: session token on every call, never throws, the backend's `error` text is
    // shown verbatim (PlatformResult.Message — 402 budget, 409 not done / no attribution / busy,
    // 502 bad_json / llm_error, 503 no_key, 504 timeout), no retries and no alternative paths.
    // The POST is synchronous: the backend waits up to 90 s for the language model, so the client
    // waits longer than that (ExplainTimeout) — the UI stays responsive (async).
    // =====================================================================================

    internal static partial class PlatformClient
    {
        /// <summary>LLM timeout 90 s (SPEC §11.2) + prompt build, number check and storage.</summary>
        private static readonly TimeSpan ExplainTimeout = TimeSpan.FromSeconds(120);
        // GET …/explanations builds the prompt once for the cost estimate — a little more than the default.
        private static readonly TimeSpan ExplainListTimeout = TimeSpan.FromSeconds(30);

        public const string ExplainModelStandard = "standard";
        public const string ExplainModelPremium = "premium";
        public const string ExplainLanguageEnglish = "en";
        public const string ExplainLanguageGerman = "de";

        private static string ExplainBase(bool validation, string runId)
            => (validation ? "/v2/validations/" : "/v2/backtests/") + Esc(runId) + "/explanations";

        /// <summary>GET /v2/backtests/{id}/explanations or /v2/validations/{id}/explanations (validation + holdout runs)
        /// → {run_id, explanations (newest first), explainable, unavailable_reason, estimate_usd {standard, premium} | null, budget}.</summary>
        public static Task<PlatformResult<ExplanationList>> ListExplanationsAsync(bool validation, string runId)
            => SendAsync<ExplanationList>(HttpMethod.Get, ExplainBase(validation, runId), null, ExplainListTimeout);

        /// <summary>POST …/explanations {model: standard|premium, language: en|de} → 200 the stored explanation (costs money;
        /// a regenerate is a new row). Errors carry the backend's message verbatim.</summary>
        public static Task<PlatformResult<RunExplanation>> CreateExplanationAsync(bool validation, string runId, string model, string language)
        {
            if (model != ExplainModelStandard && model != ExplainModelPremium)
                throw new ArgumentException("Unknown model tier.", nameof(model));
            if (language != ExplainLanguageEnglish && language != ExplainLanguageGerman)
                throw new ArgumentException("Unknown language.", nameof(language));
            var body = new Dictionary<string, object?> { ["model"] = model, ["language"] = language };
            return SendAsync<RunExplanation>(HttpMethod.Post, ExplainBase(validation, runId), body, ExplainTimeout);
        }

        /// <summary>GET /v2/settings/explain → {daily_budget_usd, spent_today_usd, remaining_usd, day, enabled, default…, max…}.</summary>
        public static Task<PlatformResult<ExplainSettings>> GetExplainSettingsAsync()
            => SendAsync<ExplainSettings>(HttpMethod.Get, "/v2/settings/explain", null, DefaultTimeout);

        /// <summary>PUT /v2/settings/explain {daily_budget_usd: 0..50} (0 = off) — validated and audited by the backend.</summary>
        public static Task<PlatformResult<ExplainSettings>> PutExplainSettingsAsync(double dailyBudgetUsd)
            => SendAsync<ExplainSettings>(HttpMethod.Put, "/v2/settings/explain",
                new Dictionary<string, object?> { ["daily_budget_usd"] = dailyBudgetUsd }, DefaultTimeout);
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §11.6 (backtest/explain.py explanation_out, list_for,
    // get_settings). No computed DTO properties without [JsonIgnore] (PropertyNameCaseInsensitive
    // would map them). usage / provenance are kept raw (provenance is read with AiContent).
    // =====================================================================================

    internal sealed class ExplanationList
    {
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("explanations")] public List<RunExplanation>? Explanations { get; set; }
        [JsonPropertyName("explainable")] public bool? Explainable { get; set; }
        [JsonPropertyName("unavailable_reason")] public string? UnavailableReason { get; set; }
        [JsonPropertyName("estimate_usd")] public ExplainEstimate? EstimateUsd { get; set; }
        [JsonPropertyName("budget")] public ExplainSettings? Budget { get; set; }
    }

    /// <summary>Pre-call cost estimate per model tier (USD).</summary>
    internal sealed class ExplainEstimate
    {
        [JsonPropertyName("standard")] public double? Standard { get; set; }
        [JsonPropertyName("premium")] public double? Premium { get; set; }
    }

    internal sealed class RunExplanation
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("run_kind")] public string? RunKind { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        /// <summary>The model id OpenRouter answered with.</summary>
        [JsonPropertyName("model")] public string? Model { get; set; }
        [JsonPropertyName("model_tier")] public string? ModelTier { get; set; }
        [JsonPropertyName("language")] public string? Language { get; set; }
        [JsonPropertyName("facts_hash")] public string? FactsHash { get; set; }
        [JsonPropertyName("output")] public ExplanationOutput? Output { get; set; }
        [JsonPropertyName("check")] public ExplanationCheck? Check { get; set; }
        // {cost_usd, prompt_tokens, completion_tokens, estimate_usd, cost_estimated, prompt_chars, facts_total, facts_sent, facts_omitted}
        [JsonPropertyName("usage")] public JsonElement? Usage { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        // {ai_generated, generator, provider, model, generated_at, human_reviewed, label, model_tier} (EU AI Act Art. 50)
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
    }

    internal sealed class ExplanationOutput
    {
        [JsonPropertyName("summary")] public string? Summary { get; set; }
        [JsonPropertyName("sections")] public List<ExplanationSection>? Sections { get; set; }
        [JsonPropertyName("hypotheses")] public List<string>? Hypotheses { get; set; }
        [JsonPropertyName("limits")] public List<string>? Limits { get; set; }
    }

    internal sealed class ExplanationSection
    {
        [JsonPropertyName("title")] public string? Title { get; set; }
        [JsonPropertyName("text")] public string? Text { get; set; }
    }

    /// <summary>Number check (SPEC §11.2/§11.6): numbers in the text that are not in the fact table — shown, never removed.</summary>
    internal sealed class ExplanationCheck
    {
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
        [JsonPropertyName("unverified")] public List<UnverifiedNumber>? Unverified { get; set; }
        [JsonPropertyName("unknown_ids")] public List<string>? UnknownIds { get; set; }
    }

    internal sealed class UnverifiedNumber
    {
        [JsonPropertyName("text")] public string? Text { get; set; }
        /// <summary>Character offset (Python str index = Unicode code points) inside the field named by <see cref="Section"/>.</summary>
        [JsonPropertyName("offset")] public int? Offset { get; set; }
        /// <summary>summary | sections.&lt;i&gt;.title | sections.&lt;i&gt;.text | hypotheses.&lt;i&gt; | limits.&lt;i&gt;</summary>
        [JsonPropertyName("section")] public string? Section { get; set; }
    }

    /// <summary>GET/PUT /v2/settings/explain. Nullable on purpose: a missing field is shown as unset, never a default.</summary>
    internal sealed class ExplainSettings
    {
        [JsonPropertyName("daily_budget_usd")] public double? DailyBudgetUsd { get; set; }
        [JsonPropertyName("spent_today_usd")] public double? SpentTodayUsd { get; set; }
        [JsonPropertyName("remaining_usd")] public double? RemainingUsd { get; set; }
        [JsonPropertyName("day")] public string? Day { get; set; }
        [JsonPropertyName("enabled")] public bool? Enabled { get; set; }
        [JsonPropertyName("default_daily_budget_usd")] public double? DefaultDailyBudgetUsd { get; set; }
        [JsonPropertyName("max_daily_budget_usd")] public double? MaxDailyBudgetUsd { get; set; }
    }
}

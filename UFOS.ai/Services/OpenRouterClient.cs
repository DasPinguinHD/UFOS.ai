using System;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using UFOS.ai.Shared;

namespace UFOS.ai.Services
{
    // Result of an LLM call together with its provenance, so the UI can label it
    // (EU AI Act Art. 50: "✦ AI-GENERATED · <model> · <time>", see Shared/AiContent.cs).
    // Model is the one OpenRouter actually routed to (response "model" field), falling
    // back to the configured one. CostUsd/PromptTokens/CompletionTokens come from the
    // response's "usage" object (usage.cost = credits actually charged by OpenRouter,
    // 1 credit = 1 USD); null when OpenRouter didn't report them.
    public sealed record AiTextResult(
        string Text,
        string Model,
        DateTimeOffset GeneratedAt,
        decimal? CostUsd = null,
        int? PromptTokens = null,
        int? CompletionTokens = null);

    // Same REST API LiveStreamAgent/backend/llm_analyst.py calls (OpenAI-compatible
    // chat/completions), just invoked directly from C# via HttpClient instead of the
    // openai Python SDK, since this is the only place the WPF app itself talks to an
    // LLM. S10-E (no .env): the key is the shared OpenRouter credential in the Windows Credential Manager
    // (Shared/CredentialCatalog.cs, set in ⚙ Keys & data), base URL / model come from
    // %LOCALAPPDATA%\UFOS.ai\app-settings.json (Shared/MainAppSettings.cs, code defaults when absent).
    // AI budget (review follow-up of task B, docs/ai-budgets-spec.md §10.1): every call is checked
    // against the app-side daily cap BEFORE it is sent (Shared/AiBudgetAppCaps.cs, fail closed —
    // throws AiBudgetBlockedException with a readable message) and booked with usage.cost afterwards.
    public sealed class OpenRouterClient
    {
        // LLM completions can take longer than the 10s used for market/news polling -
        // a dedicated client keeps that timeout from affecting the overview refresh.
        private static readonly HttpClient HttpClient = new() { Timeout = TimeSpan.FromSeconds(45) };

        /// <summary>True when the shared OpenRouter key is stored (also when it can't be checked: the call then reports why).</summary>
        public bool IsConfigured
        {
            get
            {
                try { return CredentialVault.GetState(CredentialCatalog.Get(CredentialCatalog.OpenRouterApiKey).Target) != VaultState.NotSet; }
                catch (CredentialVaultException) { return true; }
            }
        }

        /// <summary>The key for the Authorization header only. Throws with a readable message (never the value) when missing.</summary>
        private static string ReadApiKey()
        {
            var entry = CredentialCatalog.Get(CredentialCatalog.OpenRouterApiKey);
            string? key;
            try { key = CredentialVault.ReadValue(entry.Target, entry.Label); }
            catch (CredentialVaultException ex) { throw new InvalidOperationException(ex.Message); }
            return key ?? throw new InvalidOperationException("The OpenRouter API key is not set — enter it in ⚙ Keys & data (Hedge Fund window).");
        }

        private const string OpenRouterHost = "openrouter.ai";

        /// <summary>
        /// OPENROUTER_BASE_URL must be an absolute https URL on host openrouter.ai (default port, no user info, no query or
        /// fragment); anything else throws with a readable message (shown as "AI summary failed: …") and no request is sent.
        /// </summary>
        private static Uri ValidateBaseUrl(string baseUrl)
        {
            if (!Uri.TryCreate(baseUrl.Trim(), UriKind.Absolute, out var uri)
                || uri.Scheme != Uri.UriSchemeHttps
                || !string.Equals(uri.Host, OpenRouterHost, StringComparison.OrdinalIgnoreCase)
                || !uri.IsDefaultPort
                || !string.IsNullOrEmpty(uri.UserInfo)
                || !string.IsNullOrEmpty(uri.Query)
                || !string.IsNullOrEmpty(uri.Fragment))
            {
                throw new InvalidOperationException(
                    "openrouter_base_url in app-settings.json must be an https URL on " + OpenRouterHost +
                    " (e.g. https://openrouter.ai/api/v1) — the request was not sent, so the API key never left the app.");
            }
            return uri;
        }

        public async Task<AiTextResult?> SummarizeArticleAsync(string title, string teaserText)
        {
            var apiKey = ReadApiKey();
            (string baseUrl, string model) settings;
            try { settings = MainAppSettings.Load(); }
            catch (MainAppSettingsException ex) { throw new InvalidOperationException(ex.Message); }
            var baseUrl = settings.baseUrl;
            // The API key goes out as a Bearer header to whatever OPENROUTER_BASE_URL names: only https to
            // openrouter.ai is accepted (S10-A2 L1), checked BEFORE the budget reservation — nothing is sent otherwise.
            var baseUri = ValidateBaseUrl(baseUrl);
            var model = settings.model;

            // BEFORE the call: reserve the estimate (throws AiBudgetBlockedException when the cap blocks).
            var caps = AiBudgetAppCaps.Instance;
            var reservation = caps.Reserve(AiBudgetAppCaps.SummaryKey);
            var settled = false;
            try
            {
                var userContent = string.IsNullOrWhiteSpace(teaserText)
                    ? $"Title: {title}"
                    : $"Title: {title}\n\n{teaserText}";

                var requestBody = new JsonObject
                {
                    ["model"] = model,
                    ["messages"] = new JsonArray
                    {
                        new JsonObject { ["role"] = "system", ["content"] = "You are a concise financial news summarizer. Summarize the given article in 2-3 sentences." },
                        new JsonObject { ["role"] = "user", ["content"] = userContent },
                    },
                };

                using var request = new HttpRequestMessage(HttpMethod.Post, $"{baseUri.AbsoluteUri.TrimEnd('/')}/chat/completions")
                {
                    Content = new StringContent(requestBody.ToJsonString(), Encoding.UTF8, "application/json"),
                };
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);

                using var response = await HttpClient.SendAsync(request).ConfigureAwait(false);
                response.EnsureSuccessStatusCode();

                await using var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false);
                using var doc = await JsonDocument.ParseAsync(stream).ConfigureAwait(false);

                var usedModel = doc.RootElement.TryGetProperty("model", out var m) && m.ValueKind == JsonValueKind.String
                    ? m.GetString() ?? model
                    : model;

                // Record what this request cost before looking at the text: OpenRouter
                // charges for it even if the model returned nothing usable.
                decimal? cost = null;
                int? promptTokens = null;
                int? completionTokens = null;
                if (doc.RootElement.TryGetProperty("usage", out var usage) && usage.ValueKind == JsonValueKind.Object)
                {
                    cost = AiCostLedger.GetDecimal(usage, "cost");
                    promptTokens = AiCostLedger.GetInt(usage, "prompt_tokens");
                    completionTokens = AiCostLedger.GetInt(usage, "completion_tokens");
                }
                // Book the reported cost against the daily cap (no reported cost -> the estimate, fail closed).
                caps.Settle(reservation, cost);
                settled = true;
                AiCostLedger.Record(
                    module: "UFOS.ai",
                    feature: "article-summary",
                    featureLabel: "Article · Summarize with AI",
                    model: usedModel,
                    costUsd: cost,
                    promptTokens: promptTokens,
                    completionTokens: completionTokens);

                var text = doc.RootElement
                    .GetProperty("choices")[0]
                    .GetProperty("message")
                    .GetProperty("content")
                    .GetString()
                    ?.Trim();
                if (string.IsNullOrWhiteSpace(text))
                {
                    return null;
                }

                return new AiTextResult(text, usedModel, DateTimeOffset.Now, cost, promptTokens, completionTokens);
            }
            catch (Exception ex) when (!settled)
            {
                // Rejected / never sent -> nothing booked; timeout or broken answer -> the estimate.
                caps.ChargeForException(reservation, ex);
                throw;
            }
        }
    }
}

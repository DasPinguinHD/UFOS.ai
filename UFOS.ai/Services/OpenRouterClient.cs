using System;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;

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
    // LLM. Key/base URL/model come from EnvConfig (UFOS.ai/.env).
    public sealed class OpenRouterClient
    {
        // LLM completions can take longer than the 10s used for market/news polling -
        // a dedicated client keeps that timeout from affecting the overview refresh.
        private static readonly HttpClient HttpClient = new() { Timeout = TimeSpan.FromSeconds(45) };

        public bool IsConfigured => !string.IsNullOrWhiteSpace(EnvConfig.Get("OPENROUTER_API_KEY"));

        public async Task<AiTextResult?> SummarizeArticleAsync(string title, string teaserText)
        {
            var apiKey = EnvConfig.Get("OPENROUTER_API_KEY");
            if (string.IsNullOrWhiteSpace(apiKey))
            {
                return null;
            }

            var baseUrl = EnvConfig.Get("OPENROUTER_BASE_URL") ?? "https://openrouter.ai/api/v1";
            var model = EnvConfig.Get("OPENROUTER_MODEL") ?? "deepseek/deepseek-chat-v3-0324";

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

            using var request = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl.TrimEnd('/')}/chat/completions")
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
    }
}

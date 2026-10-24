using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Newsroom;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform stage 5b — papers → strategy + replication (contract: backend/strategy_platform/
    // SPEC.md §14.1/§14.2/§14.5, routes/JSON §14.7). Same rules as the rest of PlatformClient: session token,
    // never throws, the backend's `error` text verbatim (url_not_allowed, pdf_invalid, too_large, ssrn_login,
    // fetch_failed/rejected, extract_timeout, budget …), no retries and no alternative paths. The PDF upload is
    // the only multipart call (one part "file"); everything else is JSON.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        /// <summary>SPEC §14.1: PDF ≤ 20 MB (checked here too, before the upload).</summary>
        public const long PaperMaxPdfBytes = 20L * 1024 * 1024;
        /// <summary>SPEC §14.7: pasted text 1..200,000 characters, title ≤ 300.</summary>
        public const int PaperMaxTextChars = 200000;
        public const int PaperMaxTitleChars = 300;
        /// <summary>SPEC §14.7: confirmed text fields ≤ 2,000 characters.</summary>
        public const int PaperMaxFieldChars = 2000;

        // fetch (30 s) / PDF text extraction in a subprocess (60 s) happen inside POST /v2/papers
        private static readonly TimeSpan PaperAddTimeout = TimeSpan.FromSeconds(120);
        private static readonly TimeSpan PaperTimeout = TimeSpan.FromSeconds(20);
        // one LLM call (90 s) for the extraction; the confirmation runs the Studio draft rules (≤ 2 calls)
        private static readonly TimeSpan PaperExtractTimeout = TimeSpan.FromSeconds(120);
        private static readonly TimeSpan PaperConfirmTimeout = TimeSpan.FromSeconds(210);

        /// <summary>POST /v2/papers multipart (one part "file", application/pdf) → paper.</summary>
        public static async Task<PlatformResult<PaperInfo>> AddPaperPdfAsync(byte[] pdf, string fileName)
        {
            try
            {
                using var client = NewsroomBackend.CreateClient(PaperAddTimeout);
                using var request = new HttpRequestMessage(HttpMethod.Post, NewsroomBackend.BaseUrl + "/v2/papers");
                var form = new MultipartFormDataContent();
                var part = new ByteArrayContent(pdf);
                part.Headers.ContentType = new MediaTypeHeaderValue("application/pdf");
                form.Add(part, "file", fileName);
                request.Content = form;
                using var resp = await client.SendAsync(request);
                string text;
                try { text = await resp.Content.ReadAsStringAsync(); }
                catch { text = ""; }
                if (!resp.IsSuccessStatusCode) return Fail<PaperInfo>(resp.StatusCode, text);
                try
                {
                    var value = string.IsNullOrWhiteSpace(text) ? null : JsonSerializer.Deserialize<PaperInfo>(text, Json);
                    return new PlatformResult<PaperInfo> { Status = PlatformCallStatus.Ok, Value = value, HttpStatus = (int)resp.StatusCode };
                }
                catch (Exception ex) when (ex is JsonException || ex is InvalidOperationException || ex is NotSupportedException)
                {
                    return new PlatformResult<PaperInfo>
                    {
                        Status = PlatformCallStatus.Error,
                        HttpStatus = (int)resp.StatusCode,
                        Message = "Unexpected response from the backend (/v2/papers) — backend and app versions may not match. Details: " + ex.Message,
                    };
                }
            }
            catch (Exception ex)
            {
                var timedOut = ex is TaskCanceledException || ex is OperationCanceledException;
                return new PlatformResult<PaperInfo>
                {
                    Status = PlatformCallStatus.Offline,
                    Message = timedOut ? "The backend did not answer in time." : BackendNotRunning,
                };
            }
        }

        /// <summary>POST /v2/papers {text, title?} → paper.</summary>
        public static Task<PlatformResult<PaperInfo>> AddPaperTextAsync(string text, string? title)
        {
            var body = new Dictionary<string, object?> { ["text"] = text };
            if (!string.IsNullOrWhiteSpace(title)) body["title"] = title!.Trim();
            return SendAsync<PaperInfo>(HttpMethod.Post, "/v2/papers", body, PaperAddTimeout);
        }

        /// <summary>POST /v2/papers {url} (arXiv / SSRN only; the backend enforces the allow-list) → paper.</summary>
        public static Task<PlatformResult<PaperInfo>> AddPaperUrlAsync(string url)
            => SendAsync<PaperInfo>(HttpMethod.Post, "/v2/papers", new Dictionary<string, object?> { ["url"] = url }, PaperAddTimeout);

        public static Task<PlatformResult<PaperList>> ListPapersAsync(int limit = 50)
            => SendAsync<PaperList>(HttpMethod.Get, "/v2/papers?limit=" + limit.ToString(CultureInfo.InvariantCulture), null, PaperTimeout);

        /// <summary>GET /v2/papers/{id} → {paper, extractions (newest first), confirmation, replication, text_preview}.</summary>
        public static Task<PlatformResult<PaperDetail>> GetPaperAsync(string paperId)
            => SendAsync<PaperDetail>(HttpMethod.Get, "/v2/papers/" + Esc(paperId), null, PaperTimeout);

        /// <summary>POST /v2/papers/{id}/extract {model} → extraction (unconfirmed; nothing runs). Costs money.</summary>
        public static Task<PlatformResult<PaperExtraction>> ExtractPaperAsync(string paperId, string model)
        {
            if (model != ExplainModelStandard && model != ExplainModelPremium) throw new ArgumentException("Unknown model tier.", nameof(model));
            return SendAsync<PaperExtraction>(HttpMethod.Post, "/v2/papers/" + Esc(paperId) + "/extract",
                new Dictionary<string, object?> { ["model"] = model }, PaperExtractTimeout);
        }

        /// <summary>POST /v2/papers/{id}/confirm {extraction_id, model, sample_start, sample_end, reported_sharpe, …} →
        /// {confirmation, draft, replication}. <paramref name="body"/> holds exactly the user-confirmed fields.</summary>
        public static Task<PlatformResult<PaperConfirmResult>> ConfirmPaperAsync(string paperId, Dictionary<string, object?> body)
            => SendAsync<PaperConfirmResult>(HttpMethod.Post, "/v2/papers/" + Esc(paperId) + "/confirm", body, PaperConfirmTimeout);

        public static Task<PlatformResult<PaperReplication>> GetPaperReplicationAsync(string paperId)
            => SendAsync<PaperReplication>(HttpMethod.Get, "/v2/papers/" + Esc(paperId) + "/replication", null, PaperTimeout);
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §14.7. Paper titles/authors, extracted fields and source quotes are
    // UNTRUSTED text (plain TextBlocks / TextBoxes only).
    // =====================================================================================

    internal sealed class PaperList
    {
        [JsonPropertyName("papers")] public List<PaperInfo>? Papers { get; set; }
    }

    internal sealed class PaperInfo
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        /// <summary>pdf | text | arxiv | ssrn</summary>
        [JsonPropertyName("source_kind")] public string? SourceKind { get; set; }
        [JsonPropertyName("url")] public string? Url { get; set; }
        [JsonPropertyName("filename")] public string? Filename { get; set; }
        [JsonPropertyName("sha256")] public string? Sha256 { get; set; }
        [JsonPropertyName("title")] public string? Title { get; set; }
        [JsonPropertyName("authors")] public string? Authors { get; set; }
        [JsonPropertyName("year")] public int? Year { get; set; }
        [JsonPropertyName("text_chars")] public int? TextChars { get; set; }
        [JsonPropertyName("pages")] public int? Pages { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("duplicate")] public bool? Duplicate { get; set; }
        /// <summary>new | extracted | not_relevant | confirmed</summary>
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("latest_extraction_id")] public long? LatestExtractionId { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
    }

    internal sealed class PaperDetail
    {
        [JsonPropertyName("paper")] public PaperInfo? Paper { get; set; }
        [JsonPropertyName("extractions")] public List<PaperExtraction>? Extractions { get; set; }
        [JsonPropertyName("confirmation")] public PaperConfirmation? Confirmation { get; set; }
        [JsonPropertyName("replication")] public PaperReplication? Replication { get; set; }
        [JsonPropertyName("text_preview")] public string? TextPreview { get; set; }
    }

    internal sealed class PaperExtraction
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("paper_id")] public string? PaperId { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("model")] public string? Model { get; set; }
        [JsonPropertyName("model_tier")] public string? ModelTier { get; set; }
        [JsonPropertyName("relevant")] public bool? Relevant { get; set; }
        [JsonPropertyName("not_relevant_reason")] public string? NotRelevantReason { get; set; }
        [JsonPropertyName("fields")] public PaperFields? Fields { get; set; }
        /// <summary>Fixed non-AI text when relevant is false.</summary>
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
        [JsonPropertyName("usage")] public JsonElement? Usage { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        [JsonPropertyName("text_used_chars")] public int? TextUsedChars { get; set; }
        [JsonPropertyName("text_truncated")] public bool? TextTruncated { get; set; }
        /// <summary>null | topic_check_failed (§14.7 review fixes) — then relevant is false and only the fixed note is shown.</summary>
        [JsonPropertyName("discarded_reason")] public string? DiscardedReason { get; set; }
    }

    internal sealed class PaperFields
    {
        [JsonPropertyName("hypothesis")] public PaperField? Hypothesis { get; set; }
        [JsonPropertyName("universe")] public PaperField? Universe { get; set; }
        [JsonPropertyName("signal")] public PaperField? Signal { get; set; }
        [JsonPropertyName("rebalancing")] public PaperField? Rebalancing { get; set; }
        [JsonPropertyName("holding_period")] public PaperField? HoldingPeriod { get; set; }
        [JsonPropertyName("sample_period")] public PaperPeriodField? SamplePeriod { get; set; }
        [JsonPropertyName("reported")] public PaperReported? Reported { get; set; }
        [JsonPropertyName("caveats")] public List<string>? Caveats { get; set; }
    }

    /// <summary>{value, source} — source = the ≤ 300-char quote / page reference the AI took the value from.</summary>
    internal sealed class PaperField
    {
        [JsonPropertyName("value")] public string? Value { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
    }

    internal sealed class PaperPeriodField
    {
        [JsonPropertyName("start")] public string? Start { get; set; }
        [JsonPropertyName("end")] public string? End { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
    }

    internal sealed class PaperReported
    {
        [JsonPropertyName("sharpe")] public double? Sharpe { get; set; }
        [JsonPropertyName("cagr")] public double? Cagr { get; set; }
        [JsonPropertyName("max_dd")] public double? MaxDd { get; set; }
        /// <summary>net | gross | unknown</summary>
        [JsonPropertyName("basis")] public string? Basis { get; set; }
        [JsonPropertyName("costs_assumed")] public string? CostsAssumed { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
    }

    internal sealed class PaperConfirmation
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("paper_id")] public string? PaperId { get; set; }
        [JsonPropertyName("extraction_id")] public long? ExtractionId { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("confirmed")] public JsonElement? Confirmed { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
    }

    internal sealed class PaperConfirmResult
    {
        [JsonPropertyName("confirmation")] public PaperConfirmation? Confirmation { get; set; }
        /// <summary>§13.5 draft shape.</summary>
        [JsonPropertyName("draft")] public StudioDraft? Draft { get; set; }
        [JsonPropertyName("replication")] public PaperReplication? Replication { get; set; }
        /// <summary>The backend's fixed (non-AI) note when the draft was discarded (e.g. the Studio scope note).</summary>
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class PaperReplication
    {
        /// <summary>none | waiting | started | start_failed</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("run_status")] public string? RunStatus { get; set; }
        [JsonPropertyName("paper_period")] public PaperPeriodField? PaperPeriod { get; set; }
        [JsonPropertyName("period_used")] public PaperPeriodField? PeriodUsed { get; set; }
        [JsonPropertyName("clamped")] public bool? Clamped { get; set; }
        [JsonPropertyName("clamp_notes")] public List<string>? ClampNotes { get; set; }
        [JsonPropertyName("reported_sharpe")] public double? ReportedSharpe { get; set; }
        [JsonPropertyName("our_sharpe")] public double? OurSharpe { get; set; }
        [JsonPropertyName("tolerance")] public double? Tolerance { get; set; }
        /// <summary>true | false | null (null until the run is done)</summary>
        [JsonPropertyName("replicated")] public bool? Replicated { get; set; }
        /// <summary>replicated | not replicated | pending | failed</summary>
        [JsonPropertyName("verdict")] public string? Verdict { get; set; }
        [JsonPropertyName("notes")] public List<string>? Notes { get; set; }
        [JsonPropertyName("key_metrics")] public JsonElement? KeyMetrics { get; set; }
    }
}

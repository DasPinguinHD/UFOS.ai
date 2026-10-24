using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Newsroom;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Typed client for the Strategy Platform /v2 routes (contract:
    // backend/strategy_platform/SPEC.md §4). Same rules as Fund/FundClient.cs:
    // - every request carries the backend's session token (NewsroomBackend.CreateClient);
    // - calls never throw: they return a PlatformResult with a status and a short, safe message;
    // - 503 {error, kind} (UserFacingError) and 400 {error, errors} bodies are shown as sent;
    // - no key VALUE is ever sent: /v2/settings/keys/changed only carries {name, action}.
    // =====================================================================================

    internal enum PlatformCallStatus { Ok, Offline, Unauthorized, NotFound, BadRequest, Unavailable, Error, Conflict }

    internal sealed class PlatformResult<T>
    {
        public PlatformCallStatus Status { get; init; }
        public T? Value { get; init; }
        public int? HttpStatus { get; init; }
        public string Message { get; init; } = "";
        /// <summary>Response body of a failed call (e.g. the ValidationResult of a 400), for callers that need more than Message.</summary>
        public string? RawBody { get; init; }
        public bool Ok => Status == PlatformCallStatus.Ok;
        public bool Offline => Status == PlatformCallStatus.Offline;
        /// <summary>Retest 2026-10-23: the client gave up waiting (timeout) — for a write the backend may still have done it.</summary>
        public bool TimedOut { get; init; }
    }

    internal static partial class PlatformClient
    {
        /// <summary>
        /// fix1023 (FIX1023_API.md A): audit notes are optional — the request carries <c>reason</c> only when the user typed one
        /// (trimmed); confirm words are no longer sent for the non-exception actions. Returns <paramref name="body"/>.
        /// </summary>
        internal static Dictionary<string, object?> WithNote(Dictionary<string, object?> body, string? note)
        {
            var n = (note ?? "").Trim();
            if (n.Length > 0) body["reason"] = n;
            return body;
        }

        public const string BackendNotRunning = "Backend not running — start the Hedge Fund window first.";

        private static readonly JsonSerializerOptions Json = new()
        {
            PropertyNameCaseInsensitive = true,
            NumberHandling = JsonNumberHandling.AllowReadingFromString,
        };

        private static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(8);

        public static Task<PlatformResult<KeyStatusList>> GetKeysAsync()
            => SendAsync<KeyStatusList>(HttpMethod.Get, "/v2/settings/keys", null, DefaultTimeout);

        /// <summary>Audit entry for a key change. <paramref name="action"/>: "set" | "removed" | "imported". Never a value.</summary>
        public static Task<PlatformResult<JsonElement>> ReportKeyChangedAsync(string name, string action)
        {
            if (action != "set" && action != "removed" && action != "imported")
                throw new ArgumentException("Unknown action.", nameof(action));
            var body = new Dictionary<string, object?> { ["name"] = name, ["action"] = action };
            return SendAsync<JsonElement>(HttpMethod.Post, "/v2/settings/keys/changed", body, DefaultTimeout);
        }

        public static Task<PlatformResult<DataSourceList>> GetSourcesAsync()
            => SendAsync<DataSourceList>(HttpMethod.Get, "/v2/data/sources", null, DefaultTimeout);

        /// <summary>POST /v2/data/test — a few real requests to the source. 180 s timeout: the backend's
        /// HTTP gate may wait up to 120 s (rate limit / retries) before it answers.</summary>
        public static Task<PlatformResult<DataTestResult>> TestSourceAsync(string source)
            => SendAsync<DataTestResult>(HttpMethod.Post, "/v2/data/test",
                new Dictionary<string, object?> { ["source"] = source }, TimeSpan.FromSeconds(180));

        // ---------- plumbing ----------

        private static async Task<PlatformResult<T>> SendAsync<T>(HttpMethod method, string path, object? body, TimeSpan timeout)
        {
            try
            {
                using var client = NewsroomBackend.CreateClient(timeout);
                using var request = new HttpRequestMessage(method, NewsroomBackend.BaseUrl + path);
                if (body != null)
                {
                    request.Content = new StringContent(JsonSerializer.Serialize(body, Json), Encoding.UTF8, "application/json");
                }
                // dlgguard (click-test 2026-10-23, "Start incubation…" hang): the body is read and parsed OFF the UI thread —
                // a large payload (e.g. GET /v2/strategies) used to be deserialized on the dispatcher and froze the window.
                // The caller's own await resumes on its context as before; nothing below touches UI state.
                using var resp = await client.SendAsync(request).ConfigureAwait(false);
                string text;
                try { text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false); }
                catch { text = ""; }
                if (!resp.IsSuccessStatusCode) return Fail<T>(resp.StatusCode, text);

                try
                {
                    var value = string.IsNullOrWhiteSpace(text) ? default : JsonSerializer.Deserialize<T>(text, Json);
                    return new PlatformResult<T> { Status = PlatformCallStatus.Ok, Value = value, HttpStatus = (int)resp.StatusCode };
                }
                catch (Exception ex) when (ex is JsonException || ex is InvalidOperationException || ex is NotSupportedException)
                {
                    return new PlatformResult<T>
                    {
                        Status = PlatformCallStatus.Error,
                        HttpStatus = (int)resp.StatusCode,
                        Message = "Unexpected response from the backend (" + path + ") — backend and app versions may not match. Details: " + ex.Message,
                    };
                }
            }
            catch (Exception ex)
            {
                var timedOut = ex is TaskCanceledException || ex is OperationCanceledException;
                return new PlatformResult<T>
                {
                    Status = PlatformCallStatus.Offline,
                    Message = timedOut ? "The backend did not answer in time." : BackendNotRunning,
                    TimedOut = timedOut,
                };
            }
        }

        private static PlatformResult<T> Fail<T>(HttpStatusCode code, string body)
        {
            // The backend's `error` text is shown verbatim (stage 2, SPEC §7.11/§7.12); other bodies
            // ({errors: [...]}) are summarised as before.
            var verbatim = ReadErrorField(body);
            var errors = FundClient.ParseErrors(body);
            var detail = verbatim ?? (errors.Count > 0 ? string.Join("; ", errors) : null);
            var (status, message) = code switch
            {
                HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden =>
                    (PlatformCallStatus.Unauthorized, "Not authorized — the backend's session token did not match (restart the Hedge Fund backend)."),
                HttpStatusCode.NotFound =>
                    (PlatformCallStatus.NotFound, verbatim ?? "The running backend has no /v2 API — it may be outdated; restart it from the Hedge Fund window."),
                HttpStatusCode.Conflict =>
                    (PlatformCallStatus.Conflict, detail ?? "Refused by the backend (conflict, HTTP 409)."),
                HttpStatusCode.BadRequest or HttpStatusCode.UnprocessableEntity =>
                    (PlatformCallStatus.BadRequest, detail ?? "Rejected by the backend (invalid input)."),
                HttpStatusCode.ServiceUnavailable =>
                    (PlatformCallStatus.Unavailable, detail ?? "The backend is temporarily unavailable (HTTP 503)."),
                _ => (PlatformCallStatus.Error, detail ?? "Backend error (HTTP " + (int)code + ")."),
            };
            return new PlatformResult<T> { Status = status, HttpStatus = (int)code, Message = message, RawBody = body };
        }

        /// <summary>The string `error` field of a JSON error body, unchanged (no truncation), or null.</summary>
        private static string? ReadErrorField(string body)
        {
            if (string.IsNullOrWhiteSpace(body)) return null;
            try
            {
                using var doc = JsonDocument.Parse(body);
                if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                    doc.RootElement.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String &&
                    !string.IsNullOrWhiteSpace(e.GetString()))
                    return e.GetString();
            }
            catch (JsonException) { /* non-JSON body (e.g. plain-text 500): the caller shows the HTTP status message */ }
            return null;
        }
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §4 (snake_case). Tolerant: unknown fields ignored.
    // =====================================================================================

    internal sealed class KeyStatusList
    {
        [JsonPropertyName("keys")] public List<KeyStatus>? Keys { get; set; }
    }

    /// <summary>One entry of secrets.status() — never contains a value.</summary>
    internal sealed class KeyStatus
    {
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("target")] public string? Target { get; set; }
        [JsonPropertyName("present")] public bool? Present { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
    }

    internal sealed class DataSourceList
    {
        [JsonPropertyName("sources")] public List<DataSourceStatus>? Sources { get; set; }
    }

    internal sealed class DataSourceStatus
    {
        [JsonPropertyName("data_type")] public string? DataType { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("last_ok_at")] public string? LastOkAt { get; set; }
        [JsonPropertyName("last_error_at")] public string? LastErrorAt { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        // Shape not fixed by SPEC §4 (a string or {licence/class, note}) — kept raw, see LicenceText.
        [JsonPropertyName("licence")] public JsonElement? Licence { get; set; }

        public string LicenceText
        {
            get
            {
                if (Licence is not JsonElement e) return "—";
                switch (e.ValueKind)
                {
                    case JsonValueKind.String:
                        return string.IsNullOrWhiteSpace(e.GetString()) ? "—" : e.GetString()!;
                    case JsonValueKind.Object:
                        var parts = new List<string>();
                        foreach (var key in new[] { "label", "licence", "license", "class", "kind", "status", "type" })
                        {
                            if (e.TryGetProperty(key, out var p) && p.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(p.GetString()))
                            {
                                parts.Add(p.GetString()!);
                                break;
                            }
                        }
                        if (e.TryGetProperty("note", out var note) && note.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(note.GetString()))
                            parts.Add(note.GetString()!);
                        return parts.Count > 0 ? string.Join(" — ", parts) : FundFormat.Plain(e);
                    case JsonValueKind.Null:
                    case JsonValueKind.Undefined:
                        return "—";
                    default:
                        return FundFormat.Plain(e);
                }
            }
        }
    }

    internal sealed class DataTestResult
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("checks")] public List<DataTestCheck>? Checks { get; set; }
    }

    internal sealed class DataTestCheck
    {
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }
}

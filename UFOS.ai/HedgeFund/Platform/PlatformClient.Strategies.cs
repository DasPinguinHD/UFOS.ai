using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform stage 2 — /v2 strategy routes (contract: backend/strategy_platform/
    // SPEC.md §7.11). Same rules as the rest of PlatformClient: session token on every call,
    // never throws, the backend's `error` text is shown verbatim (PlatformResult.Message),
    // no retries and no alternative paths. Strategy texts are data — the UI shows them as
    // plain text only (§7.6).
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan StrategyTimeout = TimeSpan.FromSeconds(15);
        // Preview loads daily bars; the backend bounds it at 60 s wall time (§7.7).
        private static readonly TimeSpan PreviewTimeout = TimeSpan.FromSeconds(90);

        /// <summary>Upper bound of a strategy file / editor text in UTF-8 bytes (SPEC §7.2 MAX_TEXT_BYTES).</summary>
        public const int MaxStrategyTextBytes = 262144;

        public static Task<PlatformResult<DslCatalogue>> GetOperatorsAsync()
            => SendAsync<DslCatalogue>(HttpMethod.Get, "/v2/dsl/operators", null, DefaultTimeout);

        public static Task<PlatformResult<StrategyList>> GetStrategiesAsync()
            => SendAsync<StrategyList>(HttpMethod.Get, "/v2/strategies", null, StrategyTimeout);

        public static Task<PlatformResult<StrategyDetail>> GetStrategyAsync(string id)
            => SendAsync<StrategyDetail>(HttpMethod.Get, "/v2/strategies/" + Esc(id), null, StrategyTimeout);

        public static Task<PlatformResult<StrategyVersionDetail>> GetStrategyVersionAsync(string id, int version)
            => SendAsync<StrategyVersionDetail>(HttpMethod.Get, "/v2/strategies/" + Esc(id) + "/versions/" + version, null, StrategyTimeout);

        /// <summary>POST /v2/strategies/validate — 200 with a ValidationResult even when invalid.</summary>
        public static Task<PlatformResult<StrategyValidation>> ValidateStrategyAsync(string text)
            => SendAsync<StrategyValidation>(HttpMethod.Post, "/v2/strategies/validate",
                new Dictionary<string, object?> { ["text"] = text }, DefaultTimeout);

        /// <summary>POST /v2/strategies — new strategy (origin "user") or an import (origin "import",
        /// file NAME only + SHA-256 of the bytes). 400 carries a ValidationResult (see <see cref="ReadValidation"/>).</summary>
        public static Task<PlatformResult<StrategyCreated>> CreateStrategyAsync(string text, string origin, string? fileName = null, string? fileSha256 = null)
        {
            if (origin != "user" && origin != "import") throw new ArgumentException("Unknown origin.", nameof(origin));
            var body = new Dictionary<string, object?> { ["text"] = text, ["origin"] = origin };
            if (fileName != null) body["file_name"] = fileName;
            if (fileSha256 != null) body["file_sha256"] = fileSha256;
            return SendAsync<StrategyCreated>(HttpMethod.Post, "/v2/strategies", body, StrategyTimeout);
        }

        /// <summary>PUT /v2/strategies/{id} — new version on top of <paramref name="baseVersion"/>; 409 = stale base or library strategy.</summary>
        public static Task<PlatformResult<StrategyCreated>> UpdateStrategyAsync(string id, string text, int baseVersion)
            => SendAsync<StrategyCreated>(HttpMethod.Put, "/v2/strategies/" + Esc(id),
                new Dictionary<string, object?> { ["text"] = text, ["base_version"] = baseVersion }, StrategyTimeout);

        public static Task<PlatformResult<StrategyCreated>> DuplicateStrategyAsync(string id, string? name)
        {
            var body = new Dictionary<string, object?>();
            if (!string.IsNullOrWhiteSpace(name)) body["name"] = name;
            return SendAsync<StrategyCreated>(HttpMethod.Post, "/v2/strategies/" + Esc(id) + "/duplicate", body, StrategyTimeout);
        }

        public static Task<PlatformResult<JsonElement>> TransitionStrategyAsync(string id, string to, string reason)
            => SendAsync<JsonElement>(HttpMethod.Post, "/v2/strategies/" + Esc(id) + "/transition",
                WithNote(new Dictionary<string, object?> { ["to"] = to }, reason), StrategyTimeout);   // fix1023: note optional

        public static Task<PlatformResult<StrategyPreview>> PreviewStrategyAsync(string id, int? version)
        {
            var body = new Dictionary<string, object?>();
            if (version is int v) body["version"] = v;
            return SendAsync<StrategyPreview>(HttpMethod.Post, "/v2/strategies/" + Esc(id) + "/preview", body, PreviewTimeout);
        }

        public static Task<PlatformResult<StrategyExport>> ExportStrategyAsync(string id, int version)
            => SendAsync<StrategyExport>(HttpMethod.Get, "/v2/strategies/" + Esc(id) + "/versions/" + version + "/export", null, StrategyTimeout);

        /// <summary>The ValidationResult inside a 400 body of POST/PUT /v2/strategies, or null.</summary>
        public static StrategyValidation? ReadValidation<T>(PlatformResult<T> result)
        {
            // Nothing is hidden here: result.Message already carries the backend's `error` text. This only
            // adds the structured list when the body has the ValidationResult shape (errors = array); other
            // 400 bodies ({error, errors: {field: msg}}) legitimately have none.
            if (string.IsNullOrWhiteSpace(result.RawBody)) return null;
            JsonDocument doc;
            try { doc = JsonDocument.Parse(result.RawBody); }
            catch (JsonException) { return null; } // non-JSON body: Message shows the HTTP status text
            using (doc)
            {
                var root = doc.RootElement;
                if (root.ValueKind != JsonValueKind.Object) return null;
                var hasErrors = root.TryGetProperty("errors", out var errs) && errs.ValueKind == JsonValueKind.Array;
                var hasWarnings = root.TryGetProperty("warnings", out var warns) && warns.ValueKind == JsonValueKind.Array;
                if (!hasErrors && !hasWarnings) return null;
                var v = root.Deserialize<StrategyValidation>(Json);
                return v != null && ((v.Errors?.Count ?? 0) > 0 || (v.Warnings?.Count ?? 0) > 0) ? v : null;
            }
        }

        private static string Esc(string id) => Uri.EscapeDataString(id ?? "");
    }

    // =====================================================================================
    // DTOs — field names as in SPEC §7.11 (snake_case). Tolerant: unknown fields are ignored.
    // =====================================================================================

    /// <summary>Lifecycle states in board order (SPEC §7.10).</summary>
    internal static class StrategyStates
    {
        public static readonly string[] All = { "idea", "draft", "backtested", "candidate", "validated", "incubation", "live", "scaled", "retired" };

        public static string Label(string? state) => string.IsNullOrEmpty(state)
            ? "—"
            : char.ToUpperInvariant(state[0]) + state[1..];
    }

    internal static class StrategyJson
    {
        /// <summary>A JSON scalar as display text (ids may be numbers or strings).</summary>
        public static string Scalar(JsonElement? e) => e is not JsonElement v ? "" : v.ValueKind switch
        {
            JsonValueKind.String => v.GetString() ?? "",
            JsonValueKind.Number => v.GetRawText(),
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            JsonValueKind.Null or JsonValueKind.Undefined => "",
            _ => v.GetRawText(),
        };

        /// <summary>ai_generated is false | true | "unknown" (SPEC §7.6).</summary>
        public static bool IsUnknown(JsonElement? e)
            => e is JsonElement v && v.ValueKind == JsonValueKind.String && string.Equals(v.GetString(), "unknown", StringComparison.OrdinalIgnoreCase);

        public static bool IsTrue(JsonElement? e) => e is JsonElement v && v.ValueKind == JsonValueKind.True;

        public static string? Str(JsonElement e, string name)
            => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var p) && p.ValueKind != JsonValueKind.Null ? Scalar(p) : null;
    }

    internal sealed class StrategyList
    {
        [JsonPropertyName("strategies")] public List<StrategySummary>? Strategies { get; set; }
        // Result of the alpha-library seeding at backend start ({ok, at, message, created, updated, unchanged, errors}).
        [JsonPropertyName("library_status")] public LibrarySeedStatus? LibraryStatus { get; set; }
    }

    internal sealed class LibrarySeedStatus
    {
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("errors")] public List<JsonElement>? Errors { get; set; }
    }

    internal sealed class StrategySummary
    {
        // Sandbox fixture label (strategy_platform/SPEC.md "Sandbox profile" S4): true + the note only on the synthetic
        // click-test fixture; absent everywhere else. Shown in red wherever this item is shown.
        [JsonPropertyName("fixture")] public bool? Fixture { get; set; }
        [JsonPropertyName("fixture_note")] public string? FixtureNote { get; set; }
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("origin")] public string? Origin { get; set; }
        [JsonPropertyName("category")] public string? Category { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("current_version")] public int? CurrentVersion { get; set; }
        [JsonPropertyName("content_hash")] public string? ContentHash { get; set; }
        [JsonPropertyName("tags")] public List<string>? Tags { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("ai_generated")] public JsonElement? AiGenerated { get; set; }
        [JsonPropertyName("external_text")] public bool ExternalText { get; set; }
        [JsonPropertyName("library_key")] public string? LibraryKey { get; set; }
        [JsonPropertyName("valid")] public bool? Valid { get; set; }
        // Library metadata, when the list carries it (otherwise read from the version route).
        [JsonPropertyName("library")] public LibraryMeta? Library { get; set; }
        // Stage 4 family chips (lifecycle/registry.family_chips, SPEC §9.7): {family_id, contaminated,
        // holdout_failed_until (eligibility date of a failed family, else null), trial_count}.
        [JsonPropertyName("family_id")] public string? FamilyId { get; set; }
        [JsonPropertyName("contaminated")] public bool? Contaminated { get; set; }
        [JsonPropertyName("holdout_failed_until")] public string? HoldoutFailedUntil { get; set; }
        [JsonPropertyName("trial_count")] public int? TrialCount { get; set; }
        [JsonPropertyName("has_exception")] public bool? HasException { get; set; }

        [JsonIgnore] public string Id => StrategyJson.Scalar(IdRaw);
        [JsonIgnore] public bool IsLibrary => string.Equals(Origin, "library", StringComparison.OrdinalIgnoreCase) || !string.IsNullOrEmpty(LibraryKey);
    }

    internal sealed class LibraryMeta
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("family")] public string? Family { get; set; }
        [JsonPropertyName("reference")] public string? Reference { get; set; }
        [JsonPropertyName("expected_holding_days")] public JsonElement? ExpectedHoldingDays { get; set; }
        [JsonPropertyName("notes")] public string? Notes { get; set; }

        [JsonIgnore] public string HoldingDaysText => StrategyJson.Scalar(ExpectedHoldingDays) is { Length: > 0 } s ? s : "—";
    }

    internal sealed class StrategyDetail
    {
        [JsonPropertyName("strategy")] public StrategySummary? Strategy { get; set; }
        [JsonPropertyName("versions")] public List<StrategyVersionInfo>? Versions { get; set; }
        [JsonPropertyName("transitions")] public List<StrategyTransition>? Transitions { get; set; }
        [JsonPropertyName("allowed_transitions")] public List<AllowedTransition>? AllowedTransitions { get; set; }
    }

    internal sealed class StrategyVersionInfo
    {
        [JsonPropertyName("version")] public int Version { get; set; }
        [JsonPropertyName("content_hash")] public string? ContentHash { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("origin")] public string? Origin { get; set; }
    }

    internal sealed class StrategyTransition
    {
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("from_state")] public string? FromState { get; set; }
        [JsonPropertyName("to_state")] public string? ToState { get; set; }
        [JsonPropertyName("actor")] public string? Actor { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class AllowedTransition
    {
        [JsonPropertyName("to")] public string? To { get; set; }
        [JsonPropertyName("allowed")] public bool Allowed { get; set; }
        [JsonPropertyName("reason_if_not")] public string? ReasonIfNot { get; set; }
    }

    internal sealed class StrategyVersionDetail
    {
        [JsonPropertyName("spec")] public JsonElement? Spec { get; set; }
        [JsonPropertyName("spec_text")] public string? SpecText { get; set; }
        [JsonPropertyName("envelope")] public StrategyEnvelope? Envelope { get; set; }
        [JsonPropertyName("validation")] public StrategyValidation? Validation { get; set; }
        [JsonPropertyName("summary")] public StrategySummaryTexts? Summary { get; set; }
        [JsonPropertyName("library")] public LibraryMeta? Library { get; set; }
    }

    /// <summary>System-owned envelope of a version (SPEC §7.6) — nothing in a file can set it.</summary>
    internal sealed class StrategyEnvelope
    {
        [JsonPropertyName("strategy_id")] public JsonElement? StrategyId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("content_hash")] public string? ContentHash { get; set; }
        [JsonPropertyName("origin")] public string? Origin { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("created_by")] public string? CreatedBy { get; set; }
        [JsonPropertyName("parent")] public JsonElement? Parent { get; set; }
        [JsonPropertyName("ai_generated")] public JsonElement? AiGenerated { get; set; }
        [JsonPropertyName("external_text")] public bool ExternalText { get; set; }
        [JsonPropertyName("claimed")] public JsonElement? Claimed { get; set; }
        [JsonPropertyName("source_file")] public StrategySourceFile? SourceFile { get; set; }
    }

    internal sealed class StrategySourceFile
    {
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("sha256")] public string? Sha256 { get; set; }
        [JsonPropertyName("bytes")] public long? Bytes { get; set; }
    }

    internal sealed class StrategySummaryTexts
    {
        [JsonPropertyName("universe_text")] public string? UniverseText { get; set; }
        [JsonPropertyName("signal_text")] public string? SignalText { get; set; }
        [JsonPropertyName("features_text")] public Dictionary<string, string>? FeaturesText { get; set; }
        [JsonPropertyName("portfolio_text")] public string? PortfolioText { get; set; }
        [JsonPropertyName("rebalance_text")] public string? RebalanceText { get; set; }
        [JsonPropertyName("risk_text")] public string? RiskText { get; set; }
    }

    /// <summary>ValidationResult (SPEC §7.5).</summary>
    internal sealed class StrategyValidation
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("errors")] public List<StrategyIssue>? Errors { get; set; }
        [JsonPropertyName("warnings")] public List<StrategyIssue>? Warnings { get; set; }
        [JsonPropertyName("facts")] public JsonElement? Facts { get; set; }
    }

    internal sealed class StrategyIssue
    {
        [JsonPropertyName("path")] public string? Path { get; set; }
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("line")] public int? Line { get; set; }
        [JsonPropertyName("column")] public int? Column { get; set; }

        [JsonIgnore] public string Position => Line is int l ? "line " + l + (Column is int c ? ", col " + c : "") : "";
    }

    internal sealed class StrategyCreated
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        // PUT: true when the text had the same content hash as the base version (no new version stored).
        [JsonPropertyName("unchanged")] public bool Unchanged { get; set; }
        [JsonIgnore] public string Id => StrategyJson.Scalar(IdRaw);
    }

    internal sealed class StrategyPreview
    {
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("rows")] public List<StrategyPreviewRow>? Rows { get; set; }
        [JsonPropertyName("warnings")] public List<JsonElement>? Warnings { get; set; }
        [JsonPropertyName("data")] public StrategyPreviewData? Data { get; set; }
    }

    internal sealed class StrategyPreviewRow
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("signal")] public double? Signal { get; set; }
        [JsonPropertyName("rank")] public double? Rank { get; set; }
        [JsonPropertyName("target_weight")] public double? TargetWeight { get; set; }
    }

    internal sealed class StrategyPreviewData
    {
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("from")] public string? From { get; set; }
        [JsonPropertyName("to")] public string? To { get; set; }
        [JsonPropertyName("symbols_missing")] public List<string>? SymbolsMissing { get; set; }
    }

    internal sealed class StrategyExport
    {
        [JsonPropertyName("file_name")] public string? FileName { get; set; }
        [JsonPropertyName("text")] public string? Text { get; set; }
    }

    /// <summary>GET /v2/dsl/operators — operators (name, category, signature, description), fields, ext features.</summary>
    internal sealed class DslCatalogue
    {
        [JsonPropertyName("operators")] public List<DslOperator>? Operators { get; set; }
        [JsonPropertyName("fields")] public JsonElement? Fields { get; set; }
        [JsonPropertyName("ext")] public JsonElement? Ext { get; set; }
    }

    internal sealed class DslOperator
    {
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("category")] public string? Category { get; set; }
        [JsonPropertyName("signature")] public string? Signature { get; set; }
        [JsonPropertyName("description")] public string? Description { get; set; }
    }
}

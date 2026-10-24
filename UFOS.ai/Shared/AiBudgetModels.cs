using System;
using System.Collections.Generic;
using System.Text;
using System.Text.Json.Serialization;

namespace UFOS.ai.Shared
{
    // DTOs of the module payload, schema 1 (contract UFOS.ai/docs/ai-budgets-spec.md §4.2).
    // Rules: [JsonPropertyName] on EVERY property (snake_case), no computed properties here (if one is
    // ever added it needs [JsonIgnore] — the PropertyNameCaseInsensitive collision bug), nullable
    // numbers where the backend may send null, unknown extra fields are ignored (forward compatible).
    // Text fields are plain text only; AiBudgetRegistry sanitizes them (control characters removed,
    // length capped) before anything is shown.

    /// <summary>GET/PUT /ai/budgets response (module payload).</summary>
    internal sealed class AiBudgetPayload
    {
        [JsonPropertyName("schema")] public int Schema { get; set; }
        [JsonPropertyName("module")] public string? Module { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
        [JsonPropertyName("day")] public string? Day { get; set; }
        [JsonPropertyName("month")] public string? Month { get; set; }
        [JsonPropertyName("budgets")] public List<AiBudgetEntry>? Budgets { get; set; }
        /// <summary>Only in a PUT 200 response: what the backend stored.</summary>
        [JsonPropertyName("changed")] public AiBudgetChange? Changed { get; set; }
    }

    /// <summary>One budget (§4.2). limit/spent/remaining are null when the entry could not be read.</summary>
    internal sealed class AiBudgetEntry
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("group")] public string? Group { get; set; }
        /// <summary>"day" | "month" (UTC).</summary>
        [JsonPropertyName("period")] public string? Period { get; set; }
        [JsonPropertyName("period_id")] public string? PeriodId { get; set; }
        [JsonPropertyName("limit_usd")] public decimal? LimitUsd { get; set; }
        [JsonPropertyName("default_usd")] public decimal? DefaultUsd { get; set; }
        [JsonPropertyName("min")] public decimal? Min { get; set; }
        [JsonPropertyName("max")] public decimal? Max { get; set; }
        [JsonPropertyName("step")] public decimal? Step { get; set; }
        [JsonPropertyName("spent_usd")] public decimal? SpentUsd { get; set; }
        [JsonPropertyName("remaining_usd")] public decimal? RemainingUsd { get; set; }
        [JsonPropertyName("enabled")] public bool Enabled { get; set; }
        /// <summary>"ok" | "blocked" | "paused" | "error".</summary>
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("editable")] public bool Editable { get; set; }
        /// <summary>"immediately" | "next_cycle".</summary>
        [JsonPropertyName("applies")] public string? Applies { get; set; }
        [JsonPropertyName("notes")] public string? Notes { get; set; }
        [JsonPropertyName("cost_log")] public List<AiBudgetCostLogRef>? CostLog { get; set; }
    }

    /// <summary>A cost-log (module, feature) pair a budget covers — for "from the cost log" figures only.</summary>
    internal sealed class AiBudgetCostLogRef
    {
        [JsonPropertyName("module")] public string? Module { get; set; }
        [JsonPropertyName("feature")] public string? Feature { get; set; }
    }

    /// <summary>PUT 200 "changed": {key, old, new} (old is null when an invalid stored value was replaced).</summary>
    internal sealed class AiBudgetChange
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("old")] public decimal? Old { get; set; }
        [JsonPropertyName("new")] public decimal? New { get; set; }
    }

    /// <summary>Why a module's budgets can (not) be shown — contract §6.1 availability order.</summary>
    internal enum AiBudgetAvailability
    {
        /// <summary>Payload received and valid.</summary>
        Ok,
        /// <summary>Backend folder (with main.py) not found next to the app.</summary>
        NotInstalled,
        /// <summary>No token file, /health failed, or the token was refused twice.</summary>
        NotRunning,
        /// <summary>GET /ai/budgets 404 — backend older than this app.</summary>
        Outdated,
        /// <summary>Wrong module/schema, unparsable, &gt; 256 KB or &gt; 64 budgets.</summary>
        Invalid,
        /// <summary>Backend reachable but GET /ai/budgets failed otherwise (HTTP 5xx/400, timeout).
        /// Addition to the contract's list (WPF notes): shows the backend's error text verbatim.</summary>
        Error,
    }

    /// <summary>Result of <see cref="AiBudgetRegistry.LoadAsync"/>.</summary>
    internal sealed class AiBudgetModuleState
    {
        public AiBudgetModuleState(IAiBudgetProvider provider, AiBudgetAvailability availability, string message,
                                   AiBudgetPayload? payload)
        {
            Provider = provider;
            Availability = availability;
            Message = message;
            Payload = payload;
            LoadedAtUtc = DateTimeOffset.UtcNow;
        }

        public IAiBudgetProvider Provider { get; }
        public AiBudgetAvailability Availability { get; }
        /// <summary>User-facing state text (empty for Ok).</summary>
        public string Message { get; }
        /// <summary>Only for <see cref="AiBudgetAvailability.Ok"/>.</summary>
        public AiBudgetPayload? Payload { get; }
        public DateTimeOffset LoadedAtUtc { get; }
    }

    /// <summary>Result of <see cref="AiBudgetRegistry.PutAsync"/>.</summary>
    internal sealed class AiBudgetPutResult
    {
        private AiBudgetPutResult(bool ok, int? statusCode, string? error, AiBudgetPayload? payload)
        {
            Ok = ok;
            StatusCode = statusCode;
            Error = error;
            Payload = payload;
        }

        public bool Ok { get; }
        /// <summary>HTTP status, null when no answer was received.</summary>
        public int? StatusCode { get; }
        /// <summary>Backend error text (verbatim, sanitized) or a transport message; null on success.</summary>
        public string? Error { get; }
        /// <summary>Re-render from this (never from the typed value) — only on success.</summary>
        public AiBudgetPayload? Payload { get; }

        public static AiBudgetPutResult Success(AiBudgetPayload payload) => new(true, 200, null, payload);
        public static AiBudgetPutResult Failure(int? statusCode, string error) => new(false, statusCode, error, null);
    }

    /// <summary>Plain-text hygiene for payload text (labels, notes, messages, error texts).</summary>
    internal static class AiBudgetText
    {
        public const int MaxLength = 300;

        /// <summary>Control characters (except line breaks/tabs → space) removed, trimmed, capped.</summary>
        public static string? Clean(string? text, int maxLength = MaxLength)
        {
            if (text is null) return null;
            var sb = new StringBuilder(Math.Min(text.Length, maxLength + 1));
            foreach (var ch in text)
            {
                if (sb.Length > maxLength) break;
                if (ch == '\r' || ch == '\n' || ch == '\t') { sb.Append(' '); continue; }
                if (char.IsControl(ch)) continue;
                // Bidi overrides/isolates could reorder the visible text — drop them.
                if (ch is >= '‪' and <= '‮' or >= '⁦' and <= '⁩') continue;
                sb.Append(ch);
            }
            var cleaned = sb.ToString().Trim();
            if (cleaned.Length > maxLength) cleaned = cleaned.Substring(0, maxLength - 1).TrimEnd() + "…";
            return cleaned;
        }
    }
}

using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // OS1 options archive (SPY/QQQ) — READ-ONLY views + registration text + audit (contract: backend/strategy_platform/
    // SPEC.md §21, API scratch contract OS1_API.md 2026-10-24 + Round 2). The archive is its own process (Windows scheduled
    // task, started right after registering and at every logon); the backend never starts processes and sends no commands.
    // The WPF builds the task XML from a fixed template and calls schtasks.exe itself (OptionsArchiveDialogs.cs, OaTask) —
    // only after the user's confirmation — and posts the result to the audit route. No PowerShell anywhere.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan OptionsArchiveTimeout = TimeSpan.FromSeconds(15);

        /// <summary>GET /v2/options-archive/status — works without a DB ("not_set_up").</summary>
        public static Task<PlatformResult<OaStatus>> GetOptionsArchiveStatusAsync()
            => SendAsync<OaStatus>(HttpMethod.Get, "/v2/options-archive/status", null, OptionsArchiveTimeout);

        /// <summary>GET /v2/options-archive/gaps?from=&amp;to= (≤ 31 days; both null = today NY).</summary>
        public static Task<PlatformResult<OaGapReport>> GetOptionsArchiveGapsAsync(string? from, string? to)
        {
            var q = from == null && to == null ? ""
                : "?from=" + Uri.EscapeDataString(from ?? to ?? "") + "&to=" + Uri.EscapeDataString(to ?? from ?? "");
            return SendAsync<OaGapReport>(HttpMethod.Get, "/v2/options-archive/gaps" + q, null, OptionsArchiveTimeout);
        }

        /// <summary>GET /v2/options-archive/runs?limit= (1…200), newest first.</summary>
        public static Task<PlatformResult<OaRunList>> GetOptionsArchiveRunsAsync(int limit = 50)
            => SendAsync<OaRunList>(HttpMethod.Get, "/v2/options-archive/runs?limit=" + Math.Clamp(limit, 1, 200).ToString(System.Globalization.CultureInfo.InvariantCulture),
                                    null, OptionsArchiveTimeout);

        /// <summary>GET /v2/options-archive/registration — DATA only (Round 2): task name, paths, availability, summary. No commands.</summary>
        public static Task<PlatformResult<OaRegistration>> GetOptionsArchiveRegistrationAsync()
            => SendAsync<OaRegistration>(HttpMethod.Get, "/v2/options-archive/registration", null, OptionsArchiveTimeout);

        /// <summary>POST /v2/options-archive/audit {action, detail?} — one audit row (detail ≤ 300 characters, no secrets).</summary>
        public static Task<PlatformResult<JsonElement>> PostOptionsArchiveAuditAsync(string action, string? detail)
        {
            var body = new Dictionary<string, object?> { ["action"] = action };
            var d = (detail ?? "").Trim();
            if (d.Length > 0) body["detail"] = d.Length > 300 ? d.Substring(0, 299) + "…" : d;
            return SendAsync<JsonElement>(HttpMethod.Post, "/v2/options-archive/audit", body, OptionsArchiveTimeout);
        }
    }

    internal sealed class OaStatus
    {
        [JsonPropertyName("state")] public string? State { get; set; }   // not_set_up | ok | degraded | error
        [JsonPropertyName("db")] public OaDb? Db { get; set; }
        [JsonPropertyName("service")] public OaService? Service { get; set; }
        [JsonPropertyName("last_run")] public OaRun? LastRun { get; set; }
        [JsonPropertyName("next_slot")] public OaNextSlot? NextSlot { get; set; }
        [JsonPropertyName("today")] public OaToday? Today { get; set; }
        [JsonPropertyName("counts")] public OaCounts? Counts { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
        [JsonPropertyName("config")] public JsonElement? Config { get; set; }
        [JsonPropertyName("paths")] public OaPaths? Paths { get; set; }
        // OS1 Round 2
        [JsonPropertyName("sandbox")] public bool? Sandbox { get; set; }
        [JsonPropertyName("actions_enabled")] public bool? ActionsEnabled { get; set; }
        [JsonPropertyName("actions_note")] public string? ActionsNote { get; set; }
        /// <summary>Written by run-once / run-slot only (never the service state or heartbeat).</summary>
        [JsonPropertyName("manual")] public OaManual? Manual { get; set; }

        public bool IsNotSetUp => State == "not_set_up";
    }

    internal sealed class OaManual
    {
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("run_ids")] public List<long>? RunIds { get; set; }
        [JsonPropertyName("exit_code")] public int? ExitCode { get; set; }
    }

    internal sealed class OaDb
    {
        [JsonPropertyName("exists")] public bool? Exists { get; set; }
        [JsonPropertyName("schema_version")] public int? SchemaVersion { get; set; }
        [JsonPropertyName("size_bytes")] public long? SizeBytes { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
    }

    internal sealed class OaService
    {
        [JsonPropertyName("status_file")] public bool? StatusFile { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }   // idle | collecting | error | stopped | null
        [JsonPropertyName("heartbeat_at")] public string? HeartbeatAt { get; set; }
        [JsonPropertyName("alive")] public bool? Alive { get; set; }
        [JsonPropertyName("pid")] public int? Pid { get; set; }
        [JsonPropertyName("version")] public string? Version { get; set; }
    }

    internal sealed class OaRun
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("slot_ny")] public string? SlotNy { get; set; }
        [JsonPropertyName("started_at")] public string? StartedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }   // ok | partial | failed | late
        [JsonPropertyName("requests")] public int? Requests { get; set; }
        [JsonPropertyName("pages")] public int? Pages { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // OS1 Round 2: server Date − PC clock (null = unknown); "manual" / "manual:<slot>" for off-time runs
        [JsonPropertyName("skew_ms")] public long? SkewMs { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
    }

    internal sealed class OaNextSlot
    {
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("slot_ny")] public string? SlotNy { get; set; }
        [JsonPropertyName("slot_utc")] public string? SlotUtc { get; set; }
    }

    internal sealed class OaToday
    {
        [JsonPropertyName("date_ny")] public string? DateNy { get; set; }
        [JsonPropertyName("trading_day")] public bool? TradingDay { get; set; }
        [JsonPropertyName("early_close")] public bool? EarlyClose { get; set; }
        [JsonPropertyName("expected_slots")] public int? ExpectedSlots { get; set; }
        [JsonPropertyName("collected")] public int? Collected { get; set; }
        [JsonPropertyName("partial")] public int? Partial { get; set; }
        [JsonPropertyName("failed")] public int? Failed { get; set; }
        [JsonPropertyName("missing")] public int? Missing { get; set; }
        [JsonPropertyName("pending")] public int? Pending { get; set; }
        // OS1 Round 2: late slots; fixed 09:55 / 15:25 slots that are late / failed / missing (a gap for H1/H2)
        [JsonPropertyName("late")] public int? Late { get; set; }
        [JsonPropertyName("fixed_slot_gaps")] public JsonElement? FixedSlotGaps { get; set; }
    }

    internal sealed class OaCounts
    {
        [JsonPropertyName("runs")] public long? Runs { get; set; }
        [JsonPropertyName("snapshots")] public long? Snapshots { get; set; }
        [JsonPropertyName("oi_rows")] public long? OiRows { get; set; }
        [JsonPropertyName("fred_rows")] public long? FredRows { get; set; }
        [JsonPropertyName("gaps")] public long? Gaps { get; set; }
    }

    internal sealed class OaPaths
    {
        [JsonPropertyName("db")] public string? Db { get; set; }
        [JsonPropertyName("status_file")] public string? StatusFile { get; set; }
        [JsonPropertyName("log")] public string? Log { get; set; }
    }

    internal sealed class OaGapReport
    {
        [JsonPropertyName("days")] public List<OaGapDay>? Days { get; set; }
    }

    internal sealed class OaGapDay
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("trading_day")] public bool? TradingDay { get; set; }
        [JsonPropertyName("early_close")] public bool? EarlyClose { get; set; }
        [JsonPropertyName("slots")] public List<OaSlot>? Slots { get; set; }
        [JsonPropertyName("gap_rows")] public List<OaGapRow>? GapRows { get; set; }
        [JsonPropertyName("gap_rows_truncated")] public bool? GapRowsTruncated { get; set; }
    }

    internal sealed class OaSlot
    {
        [JsonPropertyName("slot_ny")] public string? SlotNy { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }   // ok | partial | failed | late | missing | pending
        [JsonPropertyName("run_id")] public long? RunId { get; set; }
        [JsonPropertyName("gaps")] public int? Gaps { get; set; }
    }

    internal sealed class OaGapRow
    {
        [JsonPropertyName("slot_ny")] public string? SlotNy { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("underlying")] public string? Underlying { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("what")] public string? What { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class OaRunList
    {
        [JsonPropertyName("runs")] public List<OaRun>? Runs { get; set; }
    }

    /// <summary>OS1 Round 2: DATA only — the WPF validates every path and builds the task XML / schtasks calls itself.</summary>
    internal sealed class OaRegistration
    {
        [JsonPropertyName("task_name")] public string? TaskName { get; set; }
        [JsonPropertyName("available")] public bool? Available { get; set; }
        [JsonPropertyName("unavailable_reason")] public string? UnavailableReason { get; set; }
        [JsonPropertyName("sandbox")] public bool? Sandbox { get; set; }
        [JsonPropertyName("backend_dir")] public string? BackendDir { get; set; }
        [JsonPropertyName("workdir")] public string? Workdir { get; set; }
        [JsonPropertyName("pythonw")] public string? Pythonw { get; set; }
        [JsonPropertyName("python")] public string? Python { get; set; }
        [JsonPropertyName("tool")] public string? Tool { get; set; }
        [JsonPropertyName("service_args")] public List<string>? ServiceArgs { get; set; }
        [JsonPropertyName("run_once_args")] public List<string>? RunOnceArgs { get; set; }
        [JsonPropertyName("summary")] public OaSummary? Summary { get; set; }
    }

    internal sealed class OaSummary
    {
        [JsonPropertyName("what")] public string? What { get; set; }
        [JsonPropertyName("when")] public string? When { get; set; }
        [JsonPropertyName("account")] public string? Account { get; set; }
        [JsonPropertyName("keys")] public string? Keys { get; set; }
        [JsonPropertyName("data")] public string? Data { get; set; }
        [JsonPropertyName("stop")] public string? Stop { get; set; }
    }
}

using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>The answer of a Disclosures call: the value, or a readable problem (never both).</summary>
    internal sealed record DisclosuresResult<T>(T? Value, string? Problem, int Status) where T : class
    {
        public bool Ok => Problem == null && Value != null;
    }

    /// <summary>
    /// Client of the HedgeFund backend's Disclosures API (UFOS.ai/docs/tasks/INSIDER_RET_API.md §7; backend
    /// newsroom/disclosures_api.py) — 127.0.0.1:8867 with the session token (NewsroomBackend.CreateClient). Read-only except
    /// the user's backfill button (POST …/backfill). Failures are returned as readable text, never swallowed. No AI.
    /// </summary>
    internal static class DisclosuresClient
    {
        private const string Base = "/newsroom/disclosures";
        private static readonly TimeSpan ReadTimeout = TimeSpan.FromSeconds(15);

        public static Task<DisclosuresResult<InsiderListResponse>> InsiderListAsync(string query)
            => GetAsync<InsiderListResponse>(Base + "/insider" + (string.IsNullOrEmpty(query) ? "" : "?" + query));

        public static Task<DisclosuresResult<InsiderDetail>> InsiderDetailAsync(string id)
            => GetAsync<InsiderDetail>(Base + "/insider/" + Uri.EscapeDataString(id));

        public static Task<DisclosuresResult<TrackRecordResponse>> TrackRecordAsync(string source, int horizon, string start, string group, int days)
            => GetAsync<TrackRecordResponse>(Base + "/track-record?source=" + Uri.EscapeDataString(source) + "&horizon=" + horizon + "&start=" + Uri.EscapeDataString(start)
                                             + "&group=" + Uri.EscapeDataString(group) + "&days=" + days);

        /// <param name="kinds">"insider", "congress" or "insider,congress" (each marker carries its kind).</param>
        public static Task<DisclosuresResult<DisclosureMarkersResponse>> MarkersAsync(string ticker, DateTime from, DateTime to, string kinds)
            => GetAsync<DisclosureMarkersResponse>(Base + "/markers?ticker=" + Uri.EscapeDataString(ticker) + "&from=" + from.ToString("yyyy-MM-dd")
                                                   + "&to=" + to.ToString("yyyy-MM-dd") + "&kinds=" + Uri.EscapeDataString(kinds));

        /// <summary>GET …/members?q= — congress filers in the store (+ which are watched).</summary>
        public static Task<DisclosuresResult<MembersResponse>> MembersAsync(string query)
            => GetAsync<MembersResponse>(Base + "/members" + (string.IsNullOrWhiteSpace(query) ? "" : "?q=" + Uri.EscapeDataString(query.Trim())));

        /// <summary>PUT …/watch {"members": [...]} — replaces the watched member list; returns the stored list.</summary>
        public static async Task<DisclosuresResult<WatchResponse>> WatchAsync(IReadOnlyCollection<string> members)
        {
            try
            {
                using var client = NewsroomBackend.CreateClient(ReadTimeout);
                using var content = new StringContent(JsonSerializer.Serialize(new { members }), Encoding.UTF8, "application/json");
                using var resp = await client.PutAsync(NewsroomBackend.BaseUrl + Base + "/watch", content);
                var body = await resp.Content.ReadAsStringAsync();
                var status = (int)resp.StatusCode;
                if (!resp.IsSuccessStatusCode) return new(null, Problem(status, body, Base + "/watch"), status);
                var value = JsonSerializer.Deserialize<WatchResponse>(body);
                return value == null ? new(null, "empty answer from the HedgeFund backend", status) : new(value, null, status);
            }
            catch (Exception ex) when (ex is HttpRequestException || ex is TaskCanceledException || ex is JsonException)
            {
                return new(null, Describe(ex), 0);
            }
        }

        public static Task<DisclosuresResult<CongressListResponse>> CongressListAsync(string query)
            => GetAsync<CongressListResponse>(Base + "/congress" + (string.IsNullOrEmpty(query) ? "" : "?" + query));

        public static Task<DisclosuresResult<CongressDetail>> CongressDetailAsync(string id)
            => GetAsync<CongressDetail>(Base + "/congress/" + Uri.EscapeDataString(id));

        public static Task<DisclosuresResult<DisclosuresStatus>> StatusAsync() => GetAsync<DisclosuresStatus>(Base + "/status");

        /// <summary>POST …/backfill {"action"}: the user's Start / Pause / Resume. 409 → the backend's reason.</summary>
        public static async Task<DisclosuresResult<BackfillActionResponse>> BackfillAsync(string action)
        {
            try
            {
                using var client = NewsroomBackend.CreateClient(ReadTimeout);
                using var content = new StringContent(JsonSerializer.Serialize(new { action }), Encoding.UTF8, "application/json");
                using var resp = await client.PostAsync(NewsroomBackend.BaseUrl + Base + "/backfill", content);
                var body = await resp.Content.ReadAsStringAsync();
                var status = (int)resp.StatusCode;
                if (!resp.IsSuccessStatusCode) return new(null, Problem(status, body, Base + "/backfill"), status);
                var value = JsonSerializer.Deserialize<BackfillActionResponse>(body);
                return value == null ? new(null, "empty answer from the HedgeFund backend", status) : new(value, null, status);
            }
            catch (Exception ex) when (ex is HttpRequestException || ex is TaskCanceledException || ex is JsonException)
            {
                return new(null, Describe(ex), 0);
            }
        }

        private static async Task<DisclosuresResult<T>> GetAsync<T>(string pathAndQuery) where T : class
        {
            try
            {
                using var client = NewsroomBackend.CreateClient(ReadTimeout);
                using var resp = await client.GetAsync(NewsroomBackend.BaseUrl + pathAndQuery);
                var body = await resp.Content.ReadAsStringAsync();
                var status = (int)resp.StatusCode;
                if (!resp.IsSuccessStatusCode) return new(null, Problem(status, body, pathAndQuery.Split('?')[0]), status);
                var value = JsonSerializer.Deserialize<T>(body);
                return value == null ? new(null, "empty answer from the HedgeFund backend", status) : new(value, null, status);
            }
            catch (Exception ex) when (ex is HttpRequestException || ex is TaskCanceledException || ex is JsonException)
            {
                return new(null, Describe(ex), 0);
            }
        }

        private static string Describe(Exception ex) => ex switch
        {
            TaskCanceledException => "the HedgeFund backend did not answer within " + ReadTimeout.TotalSeconds + " s",
            JsonException => "unexpected answer from the HedgeFund backend (" + ex.Message + ")",
            _ => "the HedgeFund backend isn't answering (status: " + NewsroomBackend.Status + ")",
        };

        private static string Problem(int status, string body, string path)
        {
            string? error = null;
            try
            {
                using var doc = JsonDocument.Parse(body);
                if (doc.RootElement.ValueKind == JsonValueKind.Object && doc.RootElement.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String)
                    error = e.GetString();
            }
            catch (JsonException) { /* a non-JSON error page: the status below says enough */ }
            if (status == (int)HttpStatusCode.NotFound && error == null)
                return "the running HedgeFund backend has no " + path + " route yet — restart it from the Hedge Fund window";
            return (error ?? "HTTP " + status) + (status == 503 ? " (store unavailable)" : "");
        }
    }

    // ------------------------------------------------------------------ DTOs (INSIDER_RET_API.md §7)

    internal sealed class ReturnChip
    {
        [JsonPropertyName("start")] public string? Start { get; set; }
        [JsonPropertyName("horizon")] public int Horizon { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("sessions_done")] public int? SessionsDone { get; set; }
        [JsonPropertyName("excess")] public double? Excess { get; set; }
        [JsonPropertyName("r_stock")] public double? RStock { get; set; }
        [JsonPropertyName("etf")] public string? Etf { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal class InsiderRow
    {
        [JsonPropertyName("id")] public string Id { get; set; } = "";
        [JsonPropertyName("ticker")] public string? Ticker { get; set; }
        [JsonPropertyName("issuer_name")] public string? IssuerName { get; set; }
        [JsonPropertyName("owner_name")] public string? OwnerName { get; set; }
        [JsonPropertyName("role_class")] public string? RoleClass { get; set; }
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("value_usd")] public double? ValueUsd { get; set; }
        [JsonPropertyName("shares")] public double? Shares { get; set; }
        [JsonPropertyName("trade_date_first")] public string? TradeDateFirst { get; set; }
        [JsonPropertyName("accepted_at")] public string? AcceptedAt { get; set; }
        [JsonPropertyName("filing_date")] public string? FilingDate { get; set; }
        [JsonPropertyName("reporting_delay_days")] public int? ReportingDelayDays { get; set; }
        [JsonPropertyName("plan_10b5_1")] public bool Plan10b51 { get; set; }
        [JsonPropertyName("cluster")] public bool Cluster { get; set; }
        [JsonPropertyName("form")] public string? Form { get; set; }
        [JsonPropertyName("measurable")] public bool Measurable { get; set; }
        [JsonPropertyName("not_measurable_reason")] public string? NotMeasurableReason { get; set; }
        [JsonPropertyName("chip")] public ReturnChip? Chip { get; set; }
    }

    internal sealed class DisclosureSourceState
    {
        [JsonPropertyName("backfill")] public string? Backfill { get; set; }
        [JsonPropertyName("last_daily_job")] public string? LastDailyJob { get; set; }
        [JsonPropertyName("daily_job_state")] public string? DailyJobState { get; set; }
    }

    internal sealed class InsiderListResponse
    {
        [JsonPropertyName("total_all")] public int TotalAll { get; set; }
        [JsonPropertyName("total_matching")] public int TotalMatching { get; set; }
        [JsonPropertyName("shown")] public int Shown { get; set; }
        [JsonPropertyName("offset")] public int Offset { get; set; }
        [JsonPropertyName("rows")] public List<InsiderRow>? Rows { get; set; }
        [JsonPropertyName("source")] public DisclosureSourceState? Source { get; set; }
    }

    internal sealed class HorizonReturn
    {
        [JsonPropertyName("h")] public int H { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("sessions_done")] public int? SessionsDone { get; set; }
        [JsonPropertyName("exit_date")] public string? ExitDate { get; set; }
        [JsonPropertyName("r_stock")] public double? RStock { get; set; }
        [JsonPropertyName("r_etf")] public double? REtf { get; set; }
        [JsonPropertyName("excess")] public double? Excess { get; set; }
        [JsonPropertyName("score")] public double? Score { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class StartReturns
    {
        [JsonPropertyName("entry_ts")] public string? EntryTs { get; set; }
        [JsonPropertyName("entry_basis")] public string? EntryBasis { get; set; }
        [JsonPropertyName("entry_price")] public double? EntryPrice { get; set; }
        [JsonPropertyName("etf")] public string? Etf { get; set; }
        [JsonPropertyName("etf_entry_price")] public double? EtfEntryPrice { get; set; }
        [JsonPropertyName("horizons")] public List<HorizonReturn>? Horizons { get; set; }
    }

    internal sealed class DetailReturns
    {
        [JsonPropertyName("filing")] public StartReturns? Filing { get; set; }
        [JsonPropertyName("trade_date")] public StartReturns? TradeDate { get; set; }
    }

    internal sealed class InsiderDetail : InsiderRow
    {
        [JsonPropertyName("roles_raw")] public string? RolesRaw { get; set; }
        [JsonPropertyName("co_reporters")] public int? CoReporters { get; set; }
        [JsonPropertyName("avg_price")] public double? AvgPrice { get; set; }
        [JsonPropertyName("trade_date_last")] public string? TradeDateLast { get; set; }
        [JsonPropertyName("plan_source")] public string? PlanSource { get; set; }
        [JsonPropertyName("official_url")] public string? OfficialUrl { get; set; }
        [JsonPropertyName("issuer_cik")] public string? IssuerCik { get; set; }
        [JsonPropertyName("sic")] public int? Sic { get; set; }
        [JsonPropertyName("sic_description")] public string? SicDescription { get; set; }
        [JsonPropertyName("etf")] public string? Etf { get; set; }
        [JsonPropertyName("etf_reason")] public string? EtfReason { get; set; }
        [JsonPropertyName("returns")] public DetailReturns? Returns { get; set; }
        [JsonPropertyName("notes")] public List<string>? Notes { get; set; }
    }

    internal sealed class TrackGroup
    {
        [JsonPropertyName("group")] public string? Group { get; set; }
        [JsonPropertyName("n")] public int N { get; set; }
        [JsonPropertyName("n_running")] public int NRunning { get; set; }
        [JsonPropertyName("n_not_measurable")] public int NNotMeasurable { get; set; }
        [JsonPropertyName("mean_score")] public double? MeanScore { get; set; }
        [JsonPropertyName("median_score")] public double? MedianScore { get; set; }
        [JsonPropertyName("hit_rate")] public double? HitRate { get; set; }
        [JsonPropertyName("small_sample")] public bool SmallSample { get; set; }
    }

    internal sealed class TrackRecordResponse
    {
        [JsonPropertyName("caveat")] public string? Caveat { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("horizon")] public int Horizon { get; set; }
        [JsonPropertyName("start")] public string? Start { get; set; }
        [JsonPropertyName("group")] public string? Group { get; set; }
        [JsonPropertyName("days")] public int Days { get; set; }
        [JsonPropertyName("groups")] public List<TrackGroup>? Groups { get; set; }
    }

    internal sealed class DisclosureMarker
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("count")] public int Count { get; set; }
        [JsonPropertyName("value_usd")] public double? ValueUsd { get; set; }
    }

    internal sealed class DisclosureMarkersResponse
    {
        [JsonPropertyName("ticker")] public string? Ticker { get; set; }
        [JsonPropertyName("dated_by")] public string? DatedBy { get; set; }
        [JsonPropertyName("markers")] public List<DisclosureMarker>? Markers { get; set; }
    }

    internal sealed class CongressMember
    {
        [JsonPropertyName("id")] public string Id { get; set; } = "";
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("chamber")] public string? Chamber { get; set; }
        [JsonPropertyName("party")] public string? Party { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("trade_count")] public int TradeCount { get; set; }
        [JsonPropertyName("watched")] public bool Watched { get; set; }
    }

    internal sealed class MembersResponse
    {
        [JsonPropertyName("members")] public List<CongressMember>? Members { get; set; }
        [JsonPropertyName("watched")] public List<string>? Watched { get; set; }
    }

    internal sealed class WatchResponse
    {
        [JsonPropertyName("watched")] public List<string>? Watched { get; set; }
    }

    internal sealed class BackfillGap
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class BackfillStep
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("progress")] public JsonElement? Progress { get; set; }
        [JsonPropertyName("gaps")] public List<BackfillGap>? Gaps { get; set; }
    }

    internal sealed class BackfillEstimate
    {
        [JsonPropertyName("requests_sec")] public long? RequestsSec { get; set; }
        [JsonPropertyName("requests_alpaca")] public long? RequestsAlpaca { get; set; }
        [JsonPropertyName("hours")] public string? Hours { get; set; }
    }

    internal sealed class BackfillStatus
    {
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
        [JsonPropertyName("progress")] public JsonElement? Progress { get; set; }
        [JsonPropertyName("steps")] public List<BackfillStep>? Steps { get; set; }
        [JsonPropertyName("running")] public bool Running { get; set; }
        [JsonPropertyName("estimate")] public BackfillEstimate? Estimate { get; set; }
    }

    internal sealed class DailyJobStatus
    {
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("last_run")] public string? LastRun { get; set; }
        [JsonPropertyName("next_run")] public string? NextRun { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
    }

    internal sealed class DisclosureCounts
    {
        [JsonPropertyName("trades")] public long Trades { get; set; }
        [JsonPropertyName("measurable")] public long Measurable { get; set; }
        [JsonPropertyName("complete_20d")] public long Complete20d { get; set; }
    }

    internal sealed class SicUnmapped
    {
        [JsonPropertyName("sic")] public int? Sic { get; set; }
        [JsonPropertyName("n")] public int N { get; set; }
    }

    internal sealed class DisclosureStorage
    {
        [JsonPropertyName("newsroom_db_bytes")] public long? NewsroomDbBytes { get; set; }
        [JsonPropertyName("hedgefund_db_bytes")] public long? HedgefundDbBytes { get; set; }
    }

    internal sealed class DisclosuresStatus
    {
        [JsonPropertyName("backfill")] public BackfillStatus? Backfill { get; set; }
        [JsonPropertyName("daily_job")] public DailyJobStatus? DailyJob { get; set; }
        [JsonPropertyName("counts")] public DisclosureCounts? Counts { get; set; }
        [JsonPropertyName("sic_unmapped")] public List<SicUnmapped>? SicUnmapped { get; set; }
        [JsonPropertyName("storage")] public DisclosureStorage? Storage { get; set; }
        [JsonPropertyName("sec_interval_seconds")] public double? SecIntervalSeconds { get; set; }
        [JsonPropertyName("congress")] public CongressStatus? Congress { get; set; }
    }

    // ------------------------------------------------------------------ Congress (CONGRESS_API.md §6, H's slice a)

    internal class CongressRow
    {
        [JsonPropertyName("id")] public string Id { get; set; } = "";
        [JsonPropertyName("ticker")] public string? Ticker { get; set; }
        [JsonPropertyName("asset_name")] public string? AssetName { get; set; }
        [JsonPropertyName("member_id")] public string? MemberId { get; set; }
        [JsonPropertyName("member_name")] public string? MemberName { get; set; }
        [JsonPropertyName("chamber")] public string? Chamber { get; set; }
        [JsonPropertyName("party")] public string? Party { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        /// <summary>self | spouse | joint | child | other</summary>
        [JsonPropertyName("owner_type")] public string? OwnerType { get; set; }
        /// <summary>P | S | X (other transaction type, not measured)</summary>
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("amount_label")] public string? AmountLabel { get; set; }
        [JsonPropertyName("amount_low")] public double? AmountLow { get; set; }
        [JsonPropertyName("amount_high")] public double? AmountHigh { get; set; }
        [JsonPropertyName("transaction_date")] public string? TransactionDate { get; set; }
        [JsonPropertyName("filing_date")] public string? FilingDate { get; set; }
        [JsonPropertyName("days_to_file")] public int? DaysToFile { get; set; }
        [JsonPropertyName("is_late")] public bool? IsLate { get; set; }
        [JsonPropertyName("measurable")] public bool Measurable { get; set; }
        [JsonPropertyName("not_measurable_reason")] public string? NotMeasurableReason { get; set; }
        [JsonPropertyName("chip")] public ReturnChip? Chip { get; set; }
    }

    internal sealed class CongressSourceState
    {
        [JsonPropertyName("updated_through")] public string? UpdatedThrough { get; set; }
        [JsonPropertyName("stale")] public bool Stale { get; set; }
        [JsonPropertyName("last_success")] public string? LastSuccess { get; set; }
        [JsonPropertyName("terms")] public string? Terms { get; set; }
    }

    internal sealed class CongressListResponse
    {
        [JsonPropertyName("total_all")] public int TotalAll { get; set; }
        [JsonPropertyName("total_matching")] public int TotalMatching { get; set; }
        [JsonPropertyName("shown")] public int Shown { get; set; }
        [JsonPropertyName("offset")] public int Offset { get; set; }
        [JsonPropertyName("rows")] public List<CongressRow>? Rows { get; set; }
        [JsonPropertyName("source")] public CongressSourceState? Source { get; set; }
    }

    internal sealed class CongressDetail : CongressRow
    {
        [JsonPropertyName("official_url")] public string? OfficialUrl { get; set; }
        [JsonPropertyName("asset_type")] public string? AssetType { get; set; }
        [JsonPropertyName("filing_type")] public string? FilingType { get; set; }
        [JsonPropertyName("comment")] public string? Comment { get; set; }
        [JsonPropertyName("office")] public string? Office { get; set; }
        [JsonPropertyName("returns")] public DetailReturns? Returns { get; set; }
        [JsonPropertyName("notes")] public List<string>? Notes { get; set; }
    }

    internal sealed class CongressStatus
    {
        [JsonPropertyName("last_pull")] public string? LastPull { get; set; }
        [JsonPropertyName("last_success")] public string? LastSuccess { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
        [JsonPropertyName("updated_through")] public string? UpdatedThrough { get; set; }
        [JsonPropertyName("stale")] public bool Stale { get; set; }
        [JsonPropertyName("rows")] public long? Rows { get; set; }
        [JsonPropertyName("skipped")] public JsonElement? Skipped { get; set; }
        [JsonPropertyName("asset_types")] public JsonElement? AssetTypes { get; set; }
        [JsonPropertyName("measurable_asset_types")] public List<string>? MeasurableAssetTypes { get; set; }
        [JsonPropertyName("terms")] public string? Terms { get; set; }
    }

    internal sealed class BackfillActionResponse
    {
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("backfill")] public BackfillStatus? Backfill { get; set; }
    }
}

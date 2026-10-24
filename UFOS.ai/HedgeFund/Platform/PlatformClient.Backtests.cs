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
    // Strategy Platform stage 3 — /v2 backtest routes (contract: backend/strategy_platform/
    // SPEC.md §8.5). Same rules as the rest of PlatformClient: session token on every call,
    // never throws, the backend's `error` text is shown verbatim (PlatformResult.Message),
    // no retries and no alternative paths. One backtest at a time: a second POST → 409.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan BacktestTimeout = TimeSpan.FromSeconds(20);
        // A finished run card carries the daily curve (up to ~27 years × 5 series) — larger body.
        private static readonly TimeSpan BacktestCardTimeout = TimeSpan.FromSeconds(45);

        /// <summary>POST /v2/backtests → 202 {run_id}. Optional fields are only sent when set.</summary>
        public static Task<PlatformResult<BacktestStarted>> StartBacktestAsync(BacktestRequest request)
        {
            var body = new Dictionary<string, object?> { ["strategy_id"] = request.StrategyId };
            if (request.Version is int v) body["version"] = v;
            if (!string.IsNullOrWhiteSpace(request.Start)) body["start"] = request.Start;
            if (!string.IsNullOrWhiteSpace(request.HistoryFrom)) body["history_from"] = request.HistoryFrom;
            if (!string.IsNullOrWhiteSpace(request.End)) body["end"] = request.End;
            if (!string.IsNullOrWhiteSpace(request.Benchmark)) body["benchmark"] = request.Benchmark;
            if (request.Capital is double c) body["capital"] = c;
            body["stress"] = request.Stress;
            body["auto_resume"] = request.AutoResume;   // SPEC §10.3 (default false)
            return SendAsync<BacktestStarted>(HttpMethod.Post, "/v2/backtests", body, BacktestTimeout);
        }

        /// <summary>GET /v2/backtests?strategy_id=&amp;limit= — newest first.</summary>
        public static Task<PlatformResult<BacktestRunList>> ListBacktestsAsync(string? strategyId, int limit = 50)
        {
            var query = "?limit=" + limit.ToString(CultureInfo.InvariantCulture);
            if (!string.IsNullOrWhiteSpace(strategyId)) query += "&strategy_id=" + Esc(strategyId!);
            return SendAsync<BacktestRunList>(HttpMethod.Get, "/v2/backtests" + query, null, BacktestTimeout);
        }

        /// <summary>GET /v2/backtests/{id} — full run card (+ progress while running).</summary>
        public static Task<PlatformResult<BacktestCard>> GetBacktestAsync(string runId)
            => SendAsync<BacktestCard>(HttpMethod.Get, "/v2/backtests/" + Esc(runId), null, BacktestCardTimeout);

        public static Task<PlatformResult<JsonElement>> CancelBacktestAsync(string runId)
            => SendAsync<JsonElement>(HttpMethod.Post, "/v2/backtests/" + Esc(runId) + "/cancel",
                new Dictionary<string, object?>(), BacktestTimeout);

        /// <summary>The id of the running backtest named in a 409 body (field run_id / id), or null.</summary>
        public static string? ReadConflictRunId<T>(PlatformResult<T> result)
        {
            if (string.IsNullOrWhiteSpace(result.RawBody)) return null;
            JsonDocument doc;
            try { doc = JsonDocument.Parse(result.RawBody); }
            catch (JsonException) { return null; } // non-JSON body: the caller looks the running run up in the list
            using (doc)
            {
                var root = doc.RootElement;
                if (root.ValueKind != JsonValueKind.Object) return null;
                foreach (var key in new[] { "run_id", "running_run_id", "id" })
                {
                    if (root.TryGetProperty(key, out var p) && p.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(p.GetString()))
                        return p.GetString();
                }
                return null;
            }
        }
    }

    // =====================================================================================
    // DTOs — field names as in SPEC §8.5 / §8.10 / §8.11 and api/routes.py (_bt_list, _bt_detail). Nested result
    // blocks (metrics, stress, data, licences, …) are kept as JsonElement and read by name in
    // BacktestReportView — tolerant of added fields, and a missing field shows "—" with the
    // backend's `unavailable` reason where one exists. No computed DTO properties without
    // [JsonIgnore] (PropertyNameCaseInsensitive would map them).
    // =====================================================================================

    internal sealed class BacktestRequest
    {
        public string StrategyId { get; init; } = "";
        public int? Version { get; init; }
        public string? Start { get; init; }
        /// <summary>Data load start without an explicit start: null/"2016" = 2016-01-04, "2000" = 2000-01-03 (SPEC §8.5).</summary>
        public string? HistoryFrom { get; init; }
        public string? End { get; init; }
        public string? Benchmark { get; init; }
        public double? Capital { get; init; }
        public bool Stress { get; init; }
        /// <summary>SPEC §10.3: on a connection error wait (status "waiting") and resume automatically, at most 5 times.</summary>
        public bool AutoResume { get; init; }
    }

    internal sealed class BacktestStarted
    {
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
    }

    internal sealed class BacktestRunList
    {
        [JsonPropertyName("runs")] public List<BacktestRunSummary>? Runs { get; set; }
    }

    internal sealed class BacktestRunSummary
    {
        // Sandbox fixture label (strategy_platform/SPEC.md "Sandbox profile" S4): true + the note only on the synthetic
        // click-test fixture; absent everywhere else. Shown in red wherever this item is shown.
        [JsonPropertyName("fixture")] public bool? Fixture { get; set; }
        [JsonPropertyName("fixture_note")] public string? FixtureNote { get; set; }
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("strategy_name")] public string? StrategyName { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("request")] public JsonElement? Request { get; set; }
        [JsonPropertyName("range")] public JsonElement? Range { get; set; }
        [JsonPropertyName("key_metrics")] public JsonElement? KeyMetrics { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // api/routes.py _resume_fields (SPEC §10.3/§10.4): resume chain and error classification (null when the backend does not send them —
        // a missing `resumable` never shows the Resume button).
        [JsonPropertyName("resumed_from")] public string? ResumedFrom { get; set; }
        [JsonPropertyName("resume_count")] public int? ResumeCount { get; set; }
        [JsonPropertyName("error_kind")] public string? ErrorKind { get; set; }
        [JsonPropertyName("resumable")] public bool? Resumable { get; set; }
        [JsonPropertyName("auto_resume")] public bool? AutoResume { get; set; }
    }

    internal sealed class BacktestCard
    {
        // Sandbox fixture label (strategy_platform/SPEC.md "Sandbox profile" S4): true + the note only on the synthetic
        // click-test fixture; absent everywhere else. Shown in red wherever this item is shown.
        [JsonPropertyName("fixture")] public bool? Fixture { get; set; }
        [JsonPropertyName("fixture_note")] public string? FixtureNote { get; set; }
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("started_at")] public string? StartedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("strategy_name")] public string? StrategyName { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("content_hash")] public string? ContentHash { get; set; }
        [JsonPropertyName("seed")] public long? Seed { get; set; }
        [JsonPropertyName("engine_version")] public string? EngineVersion { get; set; }
        [JsonPropertyName("trial_number")] public int? TrialNumber { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("request")] public JsonElement? Request { get; set; }
        [JsonPropertyName("range")] public JsonElement? Range { get; set; }
        // Detail route: [{symbol, asset_class, role, excluded, prices {sources, ranges [{source, first, last, rows}]},
        //                volume {source, first, last, rows}, dividends, seam [{adjustment, ok, factor, checked_at, stats}]}]
        [JsonPropertyName("data")] public JsonElement? Data { get; set; }
        [JsonPropertyName("seam_date")] public string? SeamDate { get; set; }
        [JsonPropertyName("spec")] public JsonElement? Spec { get; set; }
        [JsonPropertyName("key_metrics")] public JsonElement? KeyMetrics { get; set; }
        [JsonPropertyName("licences")] public JsonElement? Licences { get; set; }
        [JsonPropertyName("cost_model")] public JsonElement? CostModel { get; set; }
        [JsonPropertyName("metrics")] public JsonElement? Metrics { get; set; }
        [JsonPropertyName("curve")] public BacktestCurve? Curve { get; set; }
        [JsonPropertyName("trades")] public JsonElement? Trades { get; set; }
        [JsonPropertyName("warnings")] public List<BacktestWarning>? Warnings { get; set; }
        [JsonPropertyName("stress")] public JsonElement? Stress { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // api/routes.py _resume_fields (SPEC §10.3/§10.4): resume chain and error classification (null when the backend does not send them —
        // a missing `resumable` never shows the Resume button).
        [JsonPropertyName("resumed_from")] public string? ResumedFrom { get; set; }
        [JsonPropertyName("resume_count")] public int? ResumeCount { get; set; }
        [JsonPropertyName("error_kind")] public string? ErrorKind { get; set; }
        [JsonPropertyName("resumable")] public bool? Resumable { get; set; }
        [JsonPropertyName("auto_resume")] public bool? AutoResume { get; set; }
        [JsonPropertyName("progress")] public BacktestProgress? Progress { get; set; }
        // SPEC §11.1/§11.5 (X1): the stored attribution (validation/holdout: + gate_facts, facts appended) and, when it is
        // null, the backend's reason (old engine / not finished / failed) — shown verbatim, never recomputed.
        [JsonPropertyName("attribution")] public JsonElement? Attribution { get; set; }
        [JsonPropertyName("attribution_unavailable")] public string? AttributionUnavailable { get; set; }
    }

    internal sealed class BacktestCurve
    {
        [JsonPropertyName("dates")] public List<string>? Dates { get; set; }
        [JsonPropertyName("equity")] public List<double?>? Equity { get; set; }
        [JsonPropertyName("benchmark")] public List<double?>? Benchmark { get; set; }
        [JsonPropertyName("gross_exposure")] public List<double?>? GrossExposure { get; set; }
        [JsonPropertyName("net_exposure")] public List<double?>? NetExposure { get; set; }
        [JsonPropertyName("drawdown_pct")] public List<double?>? DrawdownPct { get; set; }
    }

    internal sealed class BacktestWarning
    {
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("count")] public int? Count { get; set; }
    }

    internal sealed class BacktestProgress
    {
        [JsonPropertyName("phase")] public string? Phase { get; set; }
        [JsonPropertyName("percent")] public double? Percent { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
    }

    internal static class BacktestStatus
    {
        /// <summary>queued / running / waiting (SPEC §10.3: a waiting run holds the job slot, is polled and cancellable).</summary>
        public static bool IsActive(string? status) => status == "queued" || status == "running" || status == RunResume.WaitingStatus;
    }

    /// <summary>Reading helpers for the JsonElement blocks of a run card.</summary>
    internal static class BtJson
    {
        public static JsonElement? Get(JsonElement? e, params string[] path)
        {
            if (e is not JsonElement cur) return null;
            foreach (var key in path)
            {
                if (cur.ValueKind != JsonValueKind.Object || !cur.TryGetProperty(key, out var next)) return null;
                cur = next;
            }
            return cur.ValueKind == JsonValueKind.Null || cur.ValueKind == JsonValueKind.Undefined ? null : cur;
        }

        public static double? Num(JsonElement? e, params string[] path)
        {
            var v = Get(e, path);
            if (v is not JsonElement x) return null;
            if (x.ValueKind == JsonValueKind.Number && x.TryGetDouble(out var d)) return d;
            if (x.ValueKind == JsonValueKind.String && double.TryParse(x.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var s)) return s;
            return null;
        }

        public static string? Str(JsonElement? e, params string[] path)
        {
            var v = Get(e, path);
            return v is JsonElement x ? StrategyJson.Scalar(x) : null;
        }

        public static bool? Bool(JsonElement? e, params string[] path)
        {
            var v = Get(e, path);
            if (v is not JsonElement x) return null;
            if (x.ValueKind == JsonValueKind.True) return true;
            if (x.ValueKind == JsonValueKind.False) return false;
            return null;
        }

        public static IEnumerable<JsonElement> Array(JsonElement? e, params string[] path)
        {
            var v = Get(e, path);
            if (v is JsonElement x && x.ValueKind == JsonValueKind.Array)
                foreach (var item in x.EnumerateArray()) yield return item;
        }

        public static IEnumerable<JsonProperty> Props(JsonElement? e, params string[] path)
        {
            var v = Get(e, path);
            if (v is JsonElement x && x.ValueKind == JsonValueKind.Object)
                foreach (var p in x.EnumerateObject()) yield return p;
        }
    }
}

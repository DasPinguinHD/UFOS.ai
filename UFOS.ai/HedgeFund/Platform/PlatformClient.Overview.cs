using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Stage 11 Overview API (strategy_platform/SPEC.md §26; task UFOS.ai/docs/tasks/STAGE11_OVERVIEW_TASK.md §1):
    // GET /v2/overview/markets | risk | activity — read-only, token-protected like every /v2 route. Tolerant DTOs:
    // a missing field is null and the UI shows "not available (reason)"; nothing here substitutes a value.
    // =====================================================================================
    internal static partial class PlatformClient
    {
        // markets: the backend caches 60 s, a cold answer may wait for one Alpaca / FRED round
        private static readonly TimeSpan OverviewMarketsTimeout = TimeSpan.FromSeconds(25);
        private static readonly TimeSpan OverviewTimeout = TimeSpan.FromSeconds(12);

        /// <summary>GET /v2/overview/markets → {generated_at, items:[…]} (SPY, QQQ, IWM, GLD, BTC, US10Y, EURUSD, VIX in this order).</summary>
        public static Task<PlatformResult<OverviewMarkets>> GetOverviewMarketsAsync()
            => SendAsync<OverviewMarkets>(HttpMethod.Get, "/v2/overview/markets", null, OverviewMarketsTimeout);

        /// <summary>GET /v2/overview/risk → risk figures of the active paper account (legacy until migrated, then platform).</summary>
        public static Task<PlatformResult<OverviewRisk>> GetOverviewRiskAsync()
            => SendAsync<OverviewRisk>(HttpMethod.Get, "/v2/overview/risk", null, OverviewTimeout);

        /// <summary>GET /v2/overview/activity?limit= → merged feed, newest first, plus the state of each source.</summary>
        public static Task<PlatformResult<OverviewActivity>> GetOverviewActivityAsync(int limit = 20)
            => SendAsync<OverviewActivity>(HttpMethod.Get,
                "/v2/overview/activity?limit=" + Math.Clamp(limit, 1, 100).ToString(CultureInfo.InvariantCulture), null, OverviewTimeout);
    }

    internal sealed class OverviewMarkets
    {
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
        [JsonPropertyName("items")] public List<OverviewMarketItem>? Items { get; set; }
        [JsonPropertyName("cached")] public bool? Cached { get; set; }
        [JsonPropertyName("cache_seconds")] public double? CacheSeconds { get; set; }
    }

    /// <summary>One ticker: {key, label, available, reason, value, change, change_unit, series, source, as_of, age_minutes, session, note}.</summary>
    internal sealed class OverviewMarketItem
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("available")] public bool? Available { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("change")] public double? Change { get; set; }
        /// <summary>"pct" | "bp" | "pt".</summary>
        [JsonPropertyName("change_unit")] public string? ChangeUnit { get; set; }
        [JsonPropertyName("series")] public List<OverviewPoint>? Series { get; set; }
        /// <summary>Why the series is missing while the value may still be available.</summary>
        [JsonPropertyName("series_reason")] public string? SeriesReason { get; set; }
        /// <summary>VIX (OS2 archive): carries the OS2 `provisional` flag and the age of its OS2 slot.</summary>
        [JsonPropertyName("provisional")] public bool? Provisional { get; set; }
        [JsonPropertyName("slot_age_minutes")] public double? SlotAgeMinutes { get; set; }
        /// <summary>"Alpaca" | "FRED" | "Archive".</summary>
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("age_minutes")] public double? AgeMinutes { get; set; }
        /// <summary>"open" | "closed" | "daily".</summary>
        [JsonPropertyName("session")] public string? Session { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class OverviewPoint
    {
        // t: ISO-8601 (expected) — kept raw so an epoch number does not make the whole payload unreadable
        [JsonPropertyName("t")] public JsonElement? T { get; set; }
        [JsonPropertyName("v")] public double? V { get; set; }
    }

    internal sealed class OverviewRisk
    {
        /// <summary>"legacy" | "platform".</summary>
        [JsonPropertyName("account")] public string? Account { get; set; }
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("n_daily_returns")] public double? NDailyReturns { get; set; }
        [JsonPropertyName("account_ids")] public JsonElement? AccountIds { get; set; }
        [JsonPropertyName("details")] public JsonElement? Details { get; set; }
        /// <summary>The active paper account set as the backend's ONE helper sees it (absent on an older backend).</summary>
        [JsonPropertyName("account_summary")] public OverviewAccountSummary? AccountSummary { get; set; }
        /// <summary>Broker / demo accounts the account set leaves out (kept raw).</summary>
        [JsonPropertyName("excluded_ids")] public JsonElement? ExcludedIds { get; set; }
        [JsonPropertyName("figures")] public OverviewRiskFigures? Figures { get; set; }
    }

    /// <summary>{ids, n, equity, day_pnl, total_pnl, as_of, summed, reason} — null values + reason when unreadable.</summary>
    internal sealed class OverviewAccountSummary
    {
        [JsonPropertyName("ids")] public JsonElement? Ids { get; set; }
        [JsonPropertyName("n")] public double? N { get; set; }
        [JsonPropertyName("available")] public bool? Available { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("equity")] public double? Equity { get; set; }
        [JsonPropertyName("day_pnl")] public double? DayPnl { get; set; }
        [JsonPropertyName("total_pnl")] public double? TotalPnl { get; set; }
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("summed")] public bool? Summed { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class OverviewRiskFigures
    {
        [JsonPropertyName("var99_1d")] public OverviewFigure? Var99 { get; set; }
        [JsonPropertyName("es975_1d")] public OverviewFigure? Es975 { get; set; }
        [JsonPropertyName("net_exposure_pct")] public OverviewFigure? NetExposurePct { get; set; }
        [JsonPropertyName("gross_exposure_pct")] public OverviewFigure? GrossExposurePct { get; set; }
        [JsonPropertyName("max_drawdown_pct")] public OverviewFigure? MaxDrawdownPct { get; set; }
        [JsonPropertyName("vol20_ann_pct")] public OverviewFigure? Vol20AnnPct { get; set; }
    }

    /// <summary>{value, unit, available, reason, method}.</summary>
    internal sealed class OverviewFigure
    {
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("unit")] public string? Unit { get; set; }
        [JsonPropertyName("available")] public bool? Available { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("method")] public string? Method { get; set; }

        [JsonIgnore] public bool IsAvailable => Available == true && Value is double v && !double.IsNaN(v) && !double.IsInfinity(v);
    }

    internal sealed class OverviewActivity
    {
        [JsonPropertyName("items")] public List<OverviewActivityItem>? Items { get; set; }
        /// <summary>{source name: "ok" | "not available (reason)"} — values kept raw (plain text in the UI).</summary>
        [JsonPropertyName("sources")] public Dictionary<string, JsonElement>? Sources { get; set; }

        /// <summary>The sources that are not "ok", as "name: text".</summary>
        public List<string> ProblemSources()
        {
            var list = new List<string>();
            if (Sources == null) return list;
            foreach (var kv in Sources)
            {
                var text = kv.Value.ValueKind == JsonValueKind.String ? kv.Value.GetString() ?? "" : FundFormat.Plain(kv.Value);
                if (!string.Equals(text.Trim(), "ok", StringComparison.OrdinalIgnoreCase)) list.Add(kv.Key + ": " + text);
            }
            return list;
        }
    }

    /// <summary>{ts, kind, title, detail, severity ("info"|"warning"|"error"), link} — plain text only.</summary>
    internal sealed class OverviewActivityItem
    {
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("title")] public string? Title { get; set; }
        [JsonPropertyName("detail")] public string? Detail { get; set; }
        [JsonPropertyName("severity")] public string? Severity { get; set; }
        [JsonPropertyName("link")] public string? Link { get; set; }
    }
}

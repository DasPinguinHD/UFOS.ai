using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Ui;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // OS2 options-structure routes (strategy_platform/SPEC.md §21.12; backend api/options_structure_routes.py):
    // GET /v2/options-structure/state | gex | surface | calendar — read-only views of the archive-based OS2 results.
    // Archive only: no live fallback. `provisional` must be shown wherever these values appear (stage 11 contract §0).
    // A 404 (unknown_slot) on gex / surface carries the backend's reason (PlatformResult.Message).
    // =====================================================================================
    internal static partial class PlatformClient
    {
        private static readonly TimeSpan OptionsStructureTimeout = TimeSpan.FromSeconds(15);

        private static string OsSymbol(string symbol) => symbol == "QQQ" ? "QQQ" : "SPY";   // the backend accepts SPY | QQQ only

        /// <summary>GET /v2/options-structure/state?symbol= → newest regime snapshot with its age (available:false + reason when stale).</summary>
        public static Task<PlatformResult<OsState>> GetOptionsStructureStateAsync(string symbol)
            => SendAsync<OsState>(HttpMethod.Get, "/v2/options-structure/state?symbol=" + OsSymbol(symbol), null, OptionsStructureTimeout);

        /// <summary>GET /v2/options-structure/gex?symbol=&amp;slot= (slot "latest" or the slot time, ISO-8601).</summary>
        public static Task<PlatformResult<OsGexResponse>> GetOptionsStructureGexAsync(string symbol, string slot = "latest")
            => SendAsync<OsGexResponse>(HttpMethod.Get, "/v2/options-structure/gex?symbol=" + OsSymbol(symbol) + "&slot=" + Uri.EscapeDataString(slot),
                null, OptionsStructureTimeout);

        /// <summary>GET /v2/options-structure/surface?symbol=&amp;slot=.</summary>
        public static Task<PlatformResult<OsSurfaceResponse>> GetOptionsStructureSurfaceAsync(string symbol, string slot = "latest")
            => SendAsync<OsSurfaceResponse>(HttpMethod.Get, "/v2/options-structure/surface?symbol=" + OsSymbol(symbol) + "&slot=" + Uri.EscapeDataString(slot),
                null, OptionsStructureTimeout);

        /// <summary>GET /v2/options-structure/calendar?days= → expirations, FOMC and the next monthly / quarterly / VIX expiration.</summary>
        public static Task<PlatformResult<OsCalendar>> GetOptionsStructureCalendarAsync(int days = 60)
            => SendAsync<OsCalendar>(HttpMethod.Get,
                "/v2/options-structure/calendar?days=" + Math.Clamp(days, 1, 400).ToString(CultureInfo.InvariantCulture), null, OptionsStructureTimeout);
    }

    internal static class OsText
    {
        /// <summary>The backend's reasons list as one plain line ("; "-joined), or null.</summary>
        public static string? Reasons(List<JsonElement>? reasons)
        {
            if (reasons == null || reasons.Count == 0) return null;
            var parts = reasons.Select(r => r.ValueKind == JsonValueKind.String ? r.GetString() ?? "" : FundFormat.Plain(r))
                               .Where(s => !string.IsNullOrWhiteSpace(s)).ToList();
            return parts.Count == 0 ? null : HfUi.Plain(string.Join("; ", parts), 300);   // untrusted backend text: plain, capped
        }

        /// <summary>Sign → "positive" | "negative" | "zero"; null when unknown.</summary>
        public static string? SignWord(double? sign)
            => sign is double s ? (s > 0 ? "positive" : s < 0 ? "negative" : "zero") : null;
    }

    internal sealed class OsState
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("available")] public bool? Available { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("slot")] public string? Slot { get; set; }
        [JsonPropertyName("available_at")] public string? AvailableAt { get; set; }
        [JsonPropertyName("age_minutes")] public double? AgeMinutes { get; set; }
        [JsonPropertyName("run_id")] public JsonElement? RunId { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("reasons")] public List<JsonElement>? Reasons { get; set; }
        [JsonPropertyName("regime")] public OsRegime? Regime { get; set; }
        [JsonPropertyName("model_version")] public JsonElement? ModelVersion { get; set; }
        [JsonPropertyName("provisional")] public bool? Provisional { get; set; }
        [JsonPropertyName("provisional_keys")] public JsonElement? ProvisionalKeys { get; set; }
    }

    /// <summary>The regime snapshot (regime.py): sign and flip from variant A; signs / agree over A, B, C.</summary>
    internal sealed class OsRegime
    {
        [JsonPropertyName("gex_sign")] public double? GexSign { get; set; }
        [JsonPropertyName("signs")] public Dictionary<string, double?>? Signs { get; set; }
        [JsonPropertyName("agree")] public bool? Agree { get; set; }
        [JsonPropertyName("gex_total")] public Dictionary<string, double?>? GexTotal { get; set; }
        [JsonPropertyName("gex_status")] public string? GexStatus { get; set; }
        [JsonPropertyName("flip")] public double? Flip { get; set; }
        [JsonPropertyName("flip_distance_pct")] public double? FlipDistancePct { get; set; }
        [JsonPropertyName("call_wall")] public double? CallWall { get; set; }
        [JsonPropertyName("put_wall")] public double? PutWall { get; set; }
        [JsonPropertyName("em_position")] public double? EmPosition { get; set; }
        [JsonPropertyName("vix_label")] public string? VixLabel { get; set; }
        [JsonPropertyName("vrp_sign")] public double? VrpSign { get; set; }
        [JsonPropertyName("spot")] public double? Spot { get; set; }
        [JsonPropertyName("next_event")] public JsonElement? NextEvent { get; set; }
        [JsonPropertyName("gex_evaluable")] public JsonElement? GexEvaluable { get; set; }
    }

    internal sealed class OsGexResponse
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("slot")] public string? Slot { get; set; }
        [JsonPropertyName("available_at")] public string? AvailableAt { get; set; }
        [JsonPropertyName("computed_at")] public string? ComputedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("reasons")] public List<JsonElement>? Reasons { get; set; }
        [JsonPropertyName("provisional")] public bool? Provisional { get; set; }
        [JsonPropertyName("gex")] public OsGex? Gex { get; set; }
    }

    internal sealed class OsGex
    {
        [JsonPropertyName("spot")] public double? Spot { get; set; }
        [JsonPropertyName("variants")] public Dictionary<string, JsonElement>? Variants { get; set; }
        [JsonPropertyName("per_strike")] public List<OsStrike>? PerStrike { get; set; }
        [JsonPropertyName("flip")] public Dictionary<string, OsFlip?>? Flip { get; set; }
        [JsonPropertyName("agree")] public bool? Agree { get; set; }
        [JsonPropertyName("signs")] public Dictionary<string, double?>? Signs { get; set; }
        [JsonPropertyName("available")] public bool? Available { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("reasons")] public List<JsonElement>? Reasons { get; set; }
    }

    internal sealed class OsStrike
    {
        [JsonPropertyName("strike")] public double? Strike { get; set; }
        [JsonPropertyName("call_gex")] public double? CallGex { get; set; }
        [JsonPropertyName("put_gex")] public double? PutGex { get; set; }
        [JsonPropertyName("gex_A")] public double? GexA { get; set; }
        [JsonPropertyName("gex_B")] public double? GexB { get; set; }
        [JsonPropertyName("gex_C")] public double? GexC { get; set; }
    }

    internal sealed class OsFlip
    {
        [JsonPropertyName("level")] public double? Level { get; set; }
        [JsonPropertyName("distance_pct")] public double? DistancePct { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class OsSurfaceResponse
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("slot")] public string? Slot { get; set; }
        [JsonPropertyName("available_at")] public string? AvailableAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("reasons")] public List<JsonElement>? Reasons { get; set; }
        [JsonPropertyName("provisional")] public bool? Provisional { get; set; }
        [JsonPropertyName("surface")] public OsSurface? Surface { get; set; }
    }

    internal sealed class OsSurface
    {
        [JsonPropertyName("expected_move")] public OsExpectedMoves? ExpectedMove { get; set; }
        [JsonPropertyName("vix_structure")] public OsVixStructure? VixStructure { get; set; }
        [JsonPropertyName("reasons")] public List<JsonElement>? Reasons { get; set; }
    }

    internal sealed class OsExpectedMoves
    {
        [JsonPropertyName("today")] public OsExpectedMove? Today { get; set; }
        [JsonPropertyName("next")] public OsExpectedMove? Next { get; set; }
    }

    /// <summary>surface.py expected_move(): straddle (ATM straddle mid), iv_based (model straddle), one_sigma, strike, reasons.</summary>
    internal sealed class OsExpectedMove
    {
        [JsonPropertyName("expiration")] public string? Expiration { get; set; }
        [JsonPropertyName("straddle")] public double? Straddle { get; set; }
        [JsonPropertyName("iv_based")] public double? IvBased { get; set; }
        [JsonPropertyName("one_sigma")] public double? OneSigma { get; set; }
        [JsonPropertyName("strike")] public double? Strike { get; set; }
        [JsonPropertyName("reasons")] public List<JsonElement>? Reasons { get; set; }
    }

    /// <summary>surface.py vix_structure(): FRED VIXCLS vs VXVCLS (VIX3M) of the same date; label contango / backwardation / flat.</summary>
    internal sealed class OsVixStructure
    {
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("slope")] public double? Slope { get; set; }
        [JsonPropertyName("ratio")] public double? Ratio { get; set; }
        [JsonPropertyName("vix")] public OsFredValue? Vix { get; set; }
        [JsonPropertyName("vxv")] public OsFredValue? Vxv { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class OsFredValue
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("value")] public double? Value { get; set; }
    }

    internal sealed class OsCalendar
    {
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("features")] public OsCalendarFeatures? Features { get; set; }
    }

    internal sealed class OsCalendarFeatures
    {
        [JsonPropertyName("as_of_date")] public string? AsOfDate { get; set; }
        [JsonPropertyName("monthly_expiration")] public OsDateDays? MonthlyExpiration { get; set; }
        [JsonPropertyName("quarterly_expiration")] public OsDateDays? QuarterlyExpiration { get; set; }
        [JsonPropertyName("vix_expiration")] public OsDateDays? VixExpiration { get; set; }
        [JsonPropertyName("reasons")] public List<JsonElement>? Reasons { get; set; }
    }

    internal sealed class OsDateDays
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("days")] public double? Days { get; set; }
    }
}

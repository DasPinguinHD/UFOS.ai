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
    // Strategy Platform stage 9b — portfolio insights (contract: backend/strategy_platform/SPEC.md §20, exact JSON §20.4).
    // ONLY the Shadow Account imports (your real depots); amounts in the account / instrument currency, NO FX conversion,
    // sums per currency. A rough guide only — not tax data (the payload's `label` / `tax_notice` are shown verbatim, never a
    // second copy). Computed locally; the GETs do not touch the trading heartbeat. Same client rules as the rest of
    // PlatformClient (session token, never throws, backend errors verbatim, no stand-in values).
    // =====================================================================================

    internal static partial class PlatformClient
    {
        /// <summary>The detail computation may read daily closes through the data layer (unrealised P&amp;L).</summary>
        private static readonly TimeSpan InsightsTimeout = TimeSpan.FromSeconds(90);

        /// <summary>GET /v2/insights/summary → the card payload (headline figures per currency).</summary>
        public static Task<PlatformResult<InsSummary>> GetInsightsSummaryAsync()
            => SendAsync<InsSummary>(HttpMethod.Get, "/v2/insights/summary", null, InsightsTimeout);

        /// <summary>GET /v2/insights/portfolio?year=&amp;account= → the detail payload; 404 unknown account, 400 bad year.</summary>
        public static Task<PlatformResult<InsPortfolio>> GetInsightsPortfolioAsync(int? year, string? accountId)
        {
            var q = new List<string>();
            if (year is int y) q.Add("year=" + y.ToString(CultureInfo.InvariantCulture));
            if (!string.IsNullOrWhiteSpace(accountId)) q.Add("account=" + Esc(accountId));
            return SendAsync<InsPortfolio>(HttpMethod.Get, "/v2/insights/portfolio" + (q.Count > 0 ? "?" + string.Join("&", q) : ""), null, InsightsTimeout);
        }
    }

    // ----- DTOs exactly §20.4 (snake_case; tolerant; no computed property without [JsonIgnore]) -----

    internal sealed class InsSummary
    {
        [JsonPropertyName("state")] public string? State { get; set; }               // ok | incomplete | no_imports
        [JsonPropertyName("reasons")] public List<string>? Reasons { get; set; }
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
        [JsonPropertyName("year")] public int? Year { get; set; }
        [JsonPropertyName("previous_year")] public int? PreviousYear { get; set; }
        [JsonPropertyName("accounts")] public int? Accounts { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("tax_notice")] public string? TaxNotice { get; set; }
        [JsonPropertyName("currencies")] public List<InsSummaryCurrency>? Currencies { get; set; }
        [JsonPropertyName("headline")] public string? Headline { get; set; }
        [JsonPropertyName("detail")] public string? Detail { get; set; }
        [JsonPropertyName("as_of")] public InsAsOf? AsOf { get; set; }
    }

    internal sealed class InsSummaryCurrency
    {
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("realised_net")] public double? RealisedNet { get; set; }
        /// <summary>§20.9: stocks AND ETFs / funds (was realised_shares).</summary>
        [JsonPropertyName("realised_brokerage")] public double? RealisedBrokerage { get; set; }
        [JsonPropertyName("realised_other")] public double? RealisedOther { get; set; }
        [JsonPropertyName("realised_unclassified")] public double? RealisedUnclassified { get; set; }
        [JsonPropertyName("dividends")] public double? Dividends { get; set; }
        [JsonPropertyName("interest")] public double? Interest { get; set; }
        [JsonPropertyName("withholding")] public double? Withholding { get; set; }
        [JsonPropertyName("fees")] public double? Fees { get; set; }
        /// <summary>§20.9: tax refunds as reported in the import.</summary>
        [JsonPropertyName("tax_refunds")] public double? TaxRefunds { get; set; }
        /// <summary>§20.7: tax on sell rows as reported in the import (not computed).</summary>
        [JsonPropertyName("taxes_on_sales")] public double? TaxesOnSales { get; set; }
        [JsonPropertyName("positions")] public int? Positions { get; set; }
        [JsonPropertyName("valued")] public int? Valued { get; set; }
        [JsonPropertyName("unrealised")] public double? Unrealised { get; set; }
        [JsonPropertyName("market_value")] public double? MarketValue { get; set; }
        [JsonPropertyName("coverage_pct")] public double? CoveragePct { get; set; }
        [JsonPropertyName("previous")] public InsSummaryPrevious? Previous { get; set; }
    }

    internal sealed class InsSummaryPrevious
    {
        [JsonPropertyName("realised_net")] public double? RealisedNet { get; set; }
        [JsonPropertyName("dividends")] public double? Dividends { get; set; }
        [JsonPropertyName("fees")] public double? Fees { get; set; }
    }

    internal sealed class InsAsOf
    {
        [JsonPropertyName("valuation")] public string? Valuation { get; set; }
        [JsonPropertyName("market")] public string? Market { get; set; }
    }

    internal sealed class InsPortfolio
    {
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("reasons")] public List<string>? Reasons { get; set; }
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
        [JsonPropertyName("year")] public int? Year { get; set; }
        [JsonPropertyName("previous_year")] public int? PreviousYear { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("tax_notice")] public string? TaxNotice { get; set; }
        [JsonPropertyName("privacy")] public string? Privacy { get; set; }
        [JsonPropertyName("limits")] public List<string>? Limits { get; set; }
        [JsonPropertyName("accounts")] public List<InsAccount>? Accounts { get; set; }
        [JsonPropertyName("totals")] public InsTotals? Totals { get; set; }
        [JsonPropertyName("yoy")] public List<InsYoy>? Yoy { get; set; }
        /// <summary>§20.9: [{key, label}] of the realised buckets (brokerage / other / unclassified).</summary>
        [JsonPropertyName("buckets")] public List<InsBucketLabel>? Buckets { get; set; }
        /// <summary>Optional (as in the summary); the view shows it when the backend sends it.</summary>
        [JsonPropertyName("as_of")] public InsAsOf? AsOf { get; set; }
    }

    internal sealed class InsAccount
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("templates")] public List<string>? Templates { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("reasons")] public List<string>? Reasons { get; set; }
        [JsonPropertyName("years")] public Dictionary<string, InsYearBlock>? Years { get; set; }
        [JsonPropertyName("positions")] public List<InsPosition>? Positions { get; set; }
        [JsonPropertyName("allocation")] public List<InsAllocation>? Allocation { get; set; }
        [JsonPropertyName("not_used")] public List<InsNotUsed>? NotUsed { get; set; }
        [JsonPropertyName("rejected_rows")] public List<InsRejected>? RejectedRows { get; set; }
        /// <summary>§20.9: pending private-market orders (pre-payments) — not positions.</summary>
        [JsonPropertyName("pending")] public List<InsPending>? Pending { get; set; }
        /// <summary>§20.9 / §19.16: corporate actions without their partner row (position stays open; reason in reasons).</summary>
        [JsonPropertyName("unpaired_corporate_actions")] public List<InsUnpaired>? UnpairedCorporateActions { get; set; }
    }

    internal sealed class InsBucketLabel
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
    }

    internal sealed class InsPending
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("amount")] public double? Amount { get; set; }
        [JsonPropertyName("fee")] public double? Fee { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class InsUnpaired
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("ext_id")] public string? ExtId { get; set; }
    }

    internal sealed class InsTotals
    {
        [JsonPropertyName("years")] public Dictionary<string, InsYearBlock>? Years { get; set; }
        [JsonPropertyName("allocation")] public List<InsAllocation>? Allocation { get; set; }
    }

    internal sealed class InsYearBlock
    {
        [JsonPropertyName("currencies")] public List<InsYearCurrency>? Currencies { get; set; }
    }

    internal sealed class InsYearCurrency
    {
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("realised")] public InsRealised? Realised { get; set; }
        [JsonPropertyName("dividends")] public double? Dividends { get; set; }
        [JsonPropertyName("withholding")] public double? Withholding { get; set; }
        [JsonPropertyName("interest")] public double? Interest { get; set; }
        [JsonPropertyName("trading_fees")] public double? TradingFees { get; set; }
        [JsonPropertyName("other_fees")] public double? OtherFees { get; set; }
        [JsonPropertyName("fees")] public double? Fees { get; set; }
        /// <summary>§20.7: tax on sell rows as reported in the import (not computed).</summary>
        [JsonPropertyName("taxes_on_sales")] public double? TaxesOnSales { get; set; }
        /// <summary>§20.9: tax refunds as reported in the import.</summary>
        [JsonPropertyName("tax_refunds")] public double? TaxRefunds { get; set; }
    }

    internal sealed class InsRealised
    {
        /// <summary>§20.9: stocks AND ETFs / funds (was shares).</summary>
        [JsonPropertyName("brokerage")] public InsBucket? Brokerage { get; set; }
        [JsonPropertyName("other")] public InsBucket? Other { get; set; }
        [JsonPropertyName("unclassified")] public InsBucket? Unclassified { get; set; }
        [JsonPropertyName("net")] public double? Net { get; set; }
    }

    internal sealed class InsBucket
    {
        [JsonPropertyName("gains")] public double? Gains { get; set; }
        [JsonPropertyName("losses")] public double? Losses { get; set; }
        [JsonPropertyName("net")] public double? Net { get; set; }
        [JsonPropertyName("parts")] public int? Parts { get; set; }
    }

    internal sealed class InsPosition
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("isin")] public string? Isin { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        /// <summary>§20.7: the broker's quantity from an import snapshot (null without one).</summary>
        [JsonPropertyName("broker_qty")] public double? BrokerQty { get; set; }
        [JsonPropertyName("cost")] public double? Cost { get; set; }
        [JsonPropertyName("avg_cost")] public double? AvgCost { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("price_source")] public string? PriceSource { get; set; }        // valuation | market | market_other_currency | none (§20.11)
        /// <summary>§20.11: alpaca_us | eodhd | kraken_public | null.</summary>
        [JsonPropertyName("price_provider")] public string? PriceProvider { get; set; }
        /// <summary>§20.10/§20.11: user_mapping | import | fixed_pair | null.</summary>
        [JsonPropertyName("symbol_source")] public string? SymbolSource { get; set; }
        /// <summary>§20.10/§20.11: the ticker / listing / pair the price came from.</summary>
        [JsonPropertyName("market_symbol")] public string? MarketSymbol { get; set; }
        /// <summary>§20.10/§20.11: the backend's source text, e.g. "EODHD close of SAP.XETRA (EUR, your listing)" — shown verbatim.</summary>
        [JsonPropertyName("price_detail")] public string? PriceDetail { get; set; }
        /// <summary>§20.11: only for market_other_currency — the value in the LISTING currency (never converted, no P&amp;L).</summary>
        [JsonPropertyName("listing")] public InsListingValue? Listing { get; set; }
        [JsonPropertyName("valuation_source")] public string? ValuationSource { get; set; } // import | user | null
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("unrealised")] public double? Unrealised { get; set; }
        [JsonPropertyName("unrealised_pct")] public double? UnrealisedPct { get; set; }
        [JsonPropertyName("day_change")] public double? DayChange { get; set; }
        [JsonPropertyName("weight")] public double? Weight { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class InsListingValue
    {
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
    }

    internal sealed class InsAllocation
    {
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("market_value")] public double? MarketValue { get; set; }
        [JsonPropertyName("cost_valued")] public double? CostValued { get; set; }
        [JsonPropertyName("cost_total")] public double? CostTotal { get; set; }
        [JsonPropertyName("coverage_pct")] public double? CoveragePct { get; set; }
        [JsonPropertyName("unrealised")] public double? Unrealised { get; set; }
        [JsonPropertyName("day_change")] public double? DayChange { get; set; }
        [JsonPropertyName("concentration")] public InsConcentration? Concentration { get; set; }
        /// <summary>§20.5: open positions / valued positions in this currency.</summary>
        [JsonPropertyName("positions")] public int? Positions { get; set; }
        [JsonPropertyName("valued")] public int? Valued { get; set; }
    }

    internal sealed class InsConcentration
    {
        [JsonPropertyName("hhi")] public double? Hhi { get; set; }
        [JsonPropertyName("effective_n")] public double? EffectiveN { get; set; }
        [JsonPropertyName("top1_pct")] public double? Top1Pct { get; set; }
        [JsonPropertyName("top3_pct")] public double? Top3Pct { get; set; }
    }

    internal sealed class InsNotUsed
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class InsRejected
    {
        [JsonPropertyName("import_id")] public string? ImportId { get; set; }
        [JsonPropertyName("filename")] public string? FileName { get; set; }
        [JsonPropertyName("rejected")] public int? Rejected { get; set; }
    }

    internal sealed class InsYoy
    {
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("realised_net")] public InsDelta? RealisedNet { get; set; }
        [JsonPropertyName("dividends")] public InsDelta? Dividends { get; set; }
        [JsonPropertyName("withholding")] public InsDelta? Withholding { get; set; }
        [JsonPropertyName("interest")] public InsDelta? Interest { get; set; }
        [JsonPropertyName("fees")] public InsDelta? Fees { get; set; }
    }

    internal sealed class InsDelta
    {
        [JsonPropertyName("current")] public double? Current { get; set; }
        [JsonPropertyName("previous")] public double? Previous { get; set; }
        [JsonPropertyName("delta")] public double? Delta { get; set; }
    }
}

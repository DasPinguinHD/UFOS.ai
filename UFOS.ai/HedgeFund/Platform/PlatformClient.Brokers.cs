using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform stage 8 — brokers (contract: backend/strategy_platform/SPEC.md §18, routes §18.9, exact JSON
    // §18.10, deviations §18.11, WPF notes §18.12). Same rules as the rest of PlatformClient: session token on every call,
    // never throws, the backend's `error` text is shown verbatim (409 test_failed / not_flat / already_connected /
    // too_small / reconcile_mismatch / not_connected / working_orders / incubating / routed / halted / not_reconciled /
    // wrong_venue / nothing_to_resolve / unreachable), no retries and no alternative paths.
    // PAPER / DEMO TRADING ONLY: the live guard is READ here — PUT /v2/brokers/{id}/live-guard and
    // POST …/live-guard/check are deliberately NOT implemented in the app (no control could switch live on; the backend
    // refuses live in this build anyway). The global kill switch stays the toolbar button (POST /fund/kill); the per-broker
    // kill below only acts on one broker account.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        public const string AlpacaPaperId = "alpaca-paper";
        public const string KrakenDemoId = "kraken-demo";          // retired 2026-10-19 (§18.13)
        public const string BinanceDemoId = "binance-demo";        // stage 8b (§18.14): Binance Spot demo, long only, USDT
        // Stage 10 (S10-K): the ACTIVE broker accounts. KrakenDemoId stays only to name / block an existing retired kraken-demo row.
        public static readonly IReadOnlyList<string> BrokerIds = new[] { AlpacaPaperId, BinanceDemoId };

        /// <summary>SPEC §18.6: kill reasons are at most 200 characters (optional).</summary>
        public const int BrokerKillReasonMax = 200;

        private static readonly TimeSpan BrokerReadTimeout = TimeSpan.FromSeconds(15);
        // Broker calls go through the backend's http_gate (rate limit, retries of reads) — a test / connect /
        // reconcile can take a while on weak Wi-Fi; the app waits instead of guessing.
        private static readonly TimeSpan BrokerTestTimeout = TimeSpan.FromSeconds(150);
        private static readonly TimeSpan BrokerConnectTimeout = TimeSpan.FromSeconds(180);
        private static readonly TimeSpan BrokerActionTimeout = TimeSpan.FromSeconds(120);

        // fix1023: the typed confirm phrases ("CONNECT …", "ROUTE TO …", "ACCEPT BROKER VALUES") were removed — one confirmation step.

        public static bool IsBrokerId(string? id) => id == AlpacaPaperId || id == BinanceDemoId || id == KrakenDemoId;

        /// <summary>GET /v2/brokers → both broker accounts' status + the paper-only notice.</summary>
        public static Task<PlatformResult<BrokerList>> GetBrokersAsync()
            => SendAsync<BrokerList>(HttpMethod.Get, "/v2/brokers", null, BrokerReadTimeout);

        /// <summary>GET /v2/brokers/{id} → status + positions / open orders of the LAST snapshot, reconciliations, events.</summary>
        public static Task<PlatformResult<BrokerDetail>> GetBrokerAsync(string accountId)
            => SendAsync<BrokerDetail>(HttpMethod.Get, "/v2/brokers/" + Esc(accountId), null, BrokerReadTimeout);

        /// <summary>POST /v2/brokers/{id}/test {} → checks (SDK, keys, paper/demo endpoint, account, positions, open orders, markets).</summary>
        public static Task<PlatformResult<BrokerTestResult>> TestBrokerAsync(string accountId)
            => SendAsync<BrokerTestResult>(HttpMethod.Post, "/v2/brokers/" + Esc(accountId) + "/test",
                new Dictionary<string, object?>(), BrokerTestTimeout);

        /// <summary>
        /// POST /v2/brokers/{id}/connect {reason?, starting_balances?} (fix1023: no confirm phrase, note optional); 409 test_failed / not_flat / too_small /
        /// balances_changed / platform_orders_open / … (body kept in RawBody). <paramref name="startingBalances"/> — binance-demo's
        /// first connect only (§18.14 item 7): the non-zero balances the user saw in Test and confirmed ({ASSET: total}).
        /// </summary>
        public static Task<PlatformResult<BrokerActionResult>> ConnectBrokerAsync(string accountId, string? reason,
                                                                               IReadOnlyDictionary<string, double>? startingBalances = null)
        {
            // fix1023: no confirm phrase is sent any more (the server no longer requires it); the note only when typed
            var body = WithNote(new Dictionary<string, object?>(), reason);
            if (startingBalances != null) body["starting_balances"] = startingBalances;
            return SendAsync<BrokerActionResult>(HttpMethod.Post, "/v2/brokers/" + Esc(accountId) + "/connect", body, BrokerConnectTimeout);
        }

        /// <summary>POST /v2/brokers/{id}/disconnect {reason}; 409 not_connected / not_flat / working_orders / incubating / routed.</summary>
        public static Task<PlatformResult<BrokerActionResult>> DisconnectBrokerAsync(string accountId, string reason)
            => SendAsync<BrokerActionResult>(HttpMethod.Post, "/v2/brokers/" + Esc(accountId) + "/disconnect",
                WithNote(new Dictionary<string, object?>(), reason), BrokerActionTimeout);

        /// <summary>POST /v2/brokers/{id}/reconcile {} → {reconciliation, broker}.</summary>
        public static Task<PlatformResult<BrokerActionResult>> ReconcileBrokerAsync(string accountId)
            => SendAsync<BrokerActionResult>(HttpMethod.Post, "/v2/brokers/" + Esc(accountId) + "/reconcile",
                new Dictionary<string, object?>(), BrokerActionTimeout);

        /// <summary>
        /// POST /v2/brokers/{id}/resolve {action: recheck | cancel_unknown_orders | accept_broker, reason?} — the user's manual
        /// resolution of a reconciliation mismatch (no auto-correction). fix1023: no confirm phrase, note optional.
        /// </summary>
        public static Task<PlatformResult<BrokerActionResult>> ResolveBrokerAsync(string accountId, string action, string? reason)
        {
            if (action != "recheck" && action != "cancel_unknown_orders" && action != "accept_broker")
                throw new ArgumentException("Unknown action.", nameof(action));
            var body = WithNote(new Dictionary<string, object?> { ["action"] = action }, reason);   // fix1023: no confirm phrase
            return SendAsync<BrokerActionResult>(HttpMethod.Post, "/v2/brokers/" + Esc(accountId) + "/resolve", body, BrokerActionTimeout);
        }

        /// <summary>POST /v2/brokers/{id}/kill {reason?} → 200 always (ok / error / note per broker). Only THIS broker account.</summary>
        public static Task<PlatformResult<BrokerKillResult>> KillBrokerAsync(string accountId, string? reason)
        {
            var body = new Dictionary<string, object?>();
            var r = (reason ?? "").Trim();
            if (r.Length > 0) body["reason"] = r.Length > BrokerKillReasonMax ? r.Substring(0, BrokerKillReasonMax) : r;
            return SendAsync<BrokerKillResult>(HttpMethod.Post, "/v2/brokers/" + Esc(accountId) + "/kill", body, BrokerActionTimeout);
        }

        /// <summary>GET /v2/brokers/{id}/live-guard → the four conditions + capital cap (read only; live is OFF in this build).</summary>
        public static Task<PlatformResult<BrokerLiveGuard>> GetBrokerLiveGuardAsync(string accountId)
            => SendAsync<BrokerLiveGuard>(HttpMethod.Get, "/v2/brokers/" + Esc(accountId) + "/live-guard", null, BrokerReadTimeout);

        /// <summary>GET /v2/brokers/routing → the route (account) of every validated / incubating / live strategy.</summary>
        public static Task<PlatformResult<BrokerRouting>> GetBrokerRoutingAsync()
            => SendAsync<BrokerRouting>(HttpMethod.Get, "/v2/brokers/routing", null, BrokerReadTimeout);

        /// <summary>POST /v2/brokers/routing {strategy_id, account_id, reason?} (fix1023: no confirm phrase); 409 not_connected / halted / not_reconciled / wrong_venue / incubating / not_flat.</summary>
        public static Task<PlatformResult<BrokerRouteResult>> SetBrokerRouteAsync(string strategyId, string accountId, string? reason)
            => SendAsync<BrokerRouteResult>(HttpMethod.Post, "/v2/brokers/routing",
                WithNote(new Dictionary<string, object?>
                {
                    ["strategy_id"] = strategyId,
                    ["account_id"] = accountId,   // fix1023: no "ROUTE TO …" phrase is sent any more
                }, reason), TradingActionTimeout);

        /// <summary>The 409 body of a refused connect ({error, code, checks?, positions?, open_orders?}) — null when absent / not JSON.</summary>
        public static BrokerRefusal? ReadBrokerRefusal(string? rawBody)
        {
            if (string.IsNullOrWhiteSpace(rawBody)) return null;
            try { return JsonSerializer.Deserialize<BrokerRefusal>(rawBody, Json); }
            catch (Exception ex) when (ex is JsonException || ex is NotSupportedException || ex is InvalidOperationException) { return null; }
        }
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §18.10 / §18.11 (snake_case). Tolerant: unknown fields are ignored; uncertain
    // scalar shapes are kept as JsonElement; computed properties carry [JsonIgnore] (PropertyNameCaseInsensitive collisions).
    // =====================================================================================

    internal sealed class BrokerList
    {
        [JsonPropertyName("brokers")] public List<BrokerStatus>? Brokers { get; set; }
        [JsonPropertyName("paper_only")] public bool? PaperOnly { get; set; }
        [JsonPropertyName("notice")] public string? Notice { get; set; }
        [JsonPropertyName("live_trading")] public BrokerLiveTrading? LiveTrading { get; set; }
    }

    /// <summary>`live_trading` {enabled, message} of GET /v2/brokers and `live_guard` {build_allows_live, message} of BrokerStatus.</summary>
    internal sealed class BrokerLiveTrading
    {
        [JsonPropertyName("enabled")] public bool? Enabled { get; set; }
        [JsonPropertyName("build_allows_live")] public bool? BuildAllowsLive { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class BrokerStatus
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("broker")] public string? Broker { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("venue")] public string? Venue { get; set; }
        [JsonPropertyName("instruments")] public string? Instruments { get; set; }
        [JsonPropertyName("paper")] public bool? Paper { get; set; }
        [JsonPropertyName("live")] public bool? Live { get; set; }
        [JsonPropertyName("endpoint")] public string? Endpoint { get; set; }
        [JsonPropertyName("sdk")] public BrokerSdk? Sdk { get; set; }
        [JsonPropertyName("keys")] public List<BrokerKey>? Keys { get; set; }
        [JsonPropertyName("connection")] public BrokerConnection? Connection { get; set; }
        [JsonPropertyName("link")] public BrokerLink? Link { get; set; }
        [JsonPropertyName("reconciliation")] public BrokerReconcileState? Reconciliation { get; set; }
        [JsonPropertyName("halt")] public TradingHalt? Halt { get; set; }
        [JsonPropertyName("snapshot")] public BrokerSnapshot? Snapshot { get; set; }
        [JsonPropertyName("working_orders")] public int? WorkingOrders { get; set; }
        [JsonPropertyName("routes")] public List<BrokerRouteRef>? Routes { get; set; }
        [JsonPropertyName("capabilities")] public Dictionary<string, JsonElement>? Capabilities { get; set; }
        [JsonPropertyName("notes")] public List<string>? Notes { get; set; }
        [JsonPropertyName("terms")] public BrokerTerms? Terms { get; set; }
        [JsonPropertyName("live_guard")] public BrokerLiveTrading? LiveGuard { get; set; }
        [JsonPropertyName("can")] public BrokerCan? Can { get; set; }
        // §18.13: a kill that has not verified the broker flat yet {pending, at, not_flat}
        [JsonPropertyName("kill")] public BrokerKillState? Kill { get; set; }
        // §18.13 (2026-10-19): "active" | "retired"; a retired broker (Kraken demo) is not connectable / routable
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("retired")] public BrokerRetired? Retired { get; set; }
        // §18.14: the account chip ("DEMO · SIMULATED FUNDS"), its money ("USD" | "USDT") and the note ("USDT — not converted")
        [JsonPropertyName("chip")] public string? Chip { get; set; }
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("currency_note")] public string? CurrencyNote { get; set; }
        // §18.14 binance-demo: the confirmed starting balances; missing → blocked until "Confirm starting balances…" (null elsewhere)
        [JsonPropertyName("baseline")] public BrokerBaseline? Baseline { get; set; }

        [JsonIgnore] public bool IsBaselineMissing => Baseline?.Missing == true;
        [JsonIgnore] public bool IsRetired => State == "retired" || Retired != null;
        [JsonIgnore] public bool IsUsdt => Currency == "USDT";
        [JsonIgnore] public bool IsConnected => Connection?.Status == "connected";
        [JsonIgnore] public bool IsHalted => Halt?.Active == true;
    }

    internal sealed class BrokerSdk
    {
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("installed")] public bool? Installed { get; set; }
        [JsonPropertyName("version")] public string? Version { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    /// <summary>Status only — never a value.</summary>
    internal sealed class BrokerKey
    {
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("present")] public bool? Present { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
    }

    internal sealed class BrokerConnection
    {
        [JsonPropertyName("status")] public string? Status { get; set; }       // never_connected | connected | disconnected
        [JsonPropertyName("since")] public string? Since { get; set; }
        [JsonPropertyName("by")] public string? By { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class BrokerLink
    {
        [JsonPropertyName("state")] public string? State { get; set; }         // ok | waiting | auth_failed | keys_missing | not_installed | error | idle
        [JsonPropertyName("attempt")] public int? Attempt { get; set; }
        [JsonPropertyName("next_retry_at")] public string? NextRetryAt { get; set; }
        [JsonPropertyName("last_ok_at")] public string? LastOkAt { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class BrokerReconcileState
    {
        [JsonPropertyName("status")] public string? Status { get; set; }       // ok | mismatch | unreachable | never
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
        [JsonPropertyName("trigger")] public string? Trigger { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("mismatches")] public List<BrokerMismatch>? Mismatches { get; set; }
    }

    internal sealed class BrokerMismatch
    {
        [JsonPropertyName("kind")] public string? Kind { get; set; }           // position | cash | unknown_order | missing_order
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class BrokerSnapshot
    {
        [JsonPropertyName("at")] public string? At { get; set; }
        [JsonPropertyName("account")] public BrokerAccountSnapshot? Account { get; set; }
        [JsonPropertyName("positions_count")] public int? PositionsCount { get; set; }
        [JsonPropertyName("open_orders_count")] public int? OpenOrdersCount { get; set; }
    }

    internal sealed class BrokerAccountSnapshot
    {
        [JsonPropertyName("equity")] public double? Equity { get; set; }
        [JsonPropertyName("cash")] public double? Cash { get; set; }
        [JsonPropertyName("cash_basis")] public double? CashBasis { get; set; }
        [JsonPropertyName("buying_power")] public double? BuyingPower { get; set; }
        [JsonPropertyName("initial_margin")] public double? InitialMargin { get; set; }
        [JsonPropertyName("maintenance_margin")] public double? MaintenanceMargin { get; set; }
        [JsonPropertyName("unrealized_pnl")] public double? UnrealizedPnl { get; set; }
        [JsonPropertyName("status")] public JsonElement? Status { get; set; }
        [JsonPropertyName("pattern_day_trader")] public bool? PatternDayTrader { get; set; }
        [JsonPropertyName("daytrade_count")] public JsonElement? DaytradeCount { get; set; }
        [JsonPropertyName("multiplier")] public JsonElement? Multiplier { get; set; }
        [JsonPropertyName("shorting_enabled")] public bool? ShortingEnabled { get; set; }
        [JsonPropertyName("trading_blocked")] public bool? TradingBlocked { get; set; }
        // §18.14 binance-demo: amounts in USDT (never converted), the non-zero balances, whether the baseline is set
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("currency_note")] public string? CurrencyNote { get; set; }
        [JsonPropertyName("balances")] public List<BrokerBalance>? Balances { get; set; }
        [JsonPropertyName("baseline_set")] public bool? BaselineSet { get; set; }
    }

    /// <summary>§18.14: binance-demo's baseline {required, set, missing, balances {ASSET: total}, message}.</summary>
    internal sealed class BrokerBaseline
    {
        [JsonPropertyName("required")] public bool? Required { get; set; }
        [JsonPropertyName("set")] public bool? Set { get; set; }
        [JsonPropertyName("missing")] public bool? Missing { get; set; }
        [JsonPropertyName("balances")] public Dictionary<string, double>? Balances { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    /// <summary>§18.14: one non-zero balance of the Binance demo account, as reported (asset units; USDT not converted).</summary>
    internal sealed class BrokerBalance
    {
        [JsonPropertyName("asset")] public string? Asset { get; set; }
        [JsonPropertyName("free")] public double? Free { get; set; }
        [JsonPropertyName("locked")] public double? Locked { get; set; }
        [JsonPropertyName("total")] public double? Total { get; set; }
    }

    internal sealed class BrokerRetired
    {
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("since")] public string? Since { get; set; }
        [JsonPropertyName("sources")] public List<string>? Sources { get; set; }
    }

    internal sealed class BrokerRouteRef
    {
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
    }

    internal sealed class BrokerTerms
    {
        [JsonPropertyName("summary")] public string? Summary { get; set; }
        [JsonPropertyName("verify")] public List<string>? Verify { get; set; }
    }

    internal sealed class BrokerCan
    {
        [JsonPropertyName("test")] public bool? Test { get; set; }
        [JsonPropertyName("connect")] public bool? Connect { get; set; }
        [JsonPropertyName("disconnect")] public bool? Disconnect { get; set; }
        [JsonPropertyName("reconcile")] public bool? Reconcile { get; set; }
        [JsonPropertyName("resolve")] public bool? Resolve { get; set; }
        [JsonPropertyName("kill")] public bool? Kill { get; set; }
    }

    internal sealed class BrokerDetail
    {
        [JsonPropertyName("broker")] public BrokerStatus? Broker { get; set; }
        [JsonPropertyName("positions")] public List<BrokerPosition>? Positions { get; set; }
        [JsonPropertyName("open_orders")] public List<BrokerOrder>? OpenOrders { get; set; }
        [JsonPropertyName("reconciliations")] public List<BrokerReconciliation>? Reconciliations { get; set; }
        [JsonPropertyName("events")] public List<BrokerEvent>? Events { get; set; }
    }

    internal sealed class BrokerPosition
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("broker_symbol")] public string? BrokerSymbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("avg_price")] public double? AvgPrice { get; set; }
        [JsonPropertyName("mark_price")] public double? MarkPrice { get; set; }
        [JsonPropertyName("market_value")] public double? MarketValue { get; set; }
        [JsonPropertyName("unrealized_pnl")] public double? UnrealizedPnl { get; set; }
        [JsonPropertyName("liquidation_price")] public double? LiquidationPrice { get; set; }
        [JsonPropertyName("initial_margin")] public double? InitialMargin { get; set; }
        [JsonPropertyName("maintenance_margin")] public double? MaintenanceMargin { get; set; }
        [JsonPropertyName("leverage")] public double? Leverage { get; set; }
        // §18.14 binance-demo: a position is balance − baseline; the kill does not sell dust / below-baseline / unmapped assets
        [JsonPropertyName("asset")] public string? Asset { get; set; }
        [JsonPropertyName("balance")] public double? Balance { get; set; }
        [JsonPropertyName("baseline")] public double? Baseline { get; set; }
        [JsonPropertyName("kill_skip")] public string? KillSkip { get; set; }
    }

    internal sealed class BrokerOrder
    {
        [JsonPropertyName("broker_order_id")] public JsonElement? BrokerOrderIdRaw { get; set; }
        [JsonPropertyName("client_order_id")] public JsonElement? ClientOrderIdRaw { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("broker_symbol")] public string? BrokerSymbol { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("filled_qty")] public double? FilledQty { get; set; }
        [JsonPropertyName("type")] public string? Type { get; set; }
        [JsonPropertyName("limit_price")] public double? LimitPrice { get; set; }
        [JsonPropertyName("stop_price")] public double? StopPrice { get; set; }
        [JsonPropertyName("tif")] public string? Tif { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("broker_status")] public string? BrokerStatus { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("known")] public bool? Known { get; set; }

        [JsonIgnore] public string BrokerOrderIdText => StrategyJson.Scalar(BrokerOrderIdRaw);
        [JsonIgnore] public string ClientOrderIdText => StrategyJson.Scalar(ClientOrderIdRaw);
    }

    internal sealed class BrokerEvent
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("title")] public string? Title { get; set; }
        [JsonPropertyName("body")] public string? Body { get; set; }
    }

    internal sealed class BrokerReconciliation
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
        [JsonPropertyName("trigger")] public string? Trigger { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }       // ok | mismatch | unreachable
        [JsonPropertyName("positions")] public List<BrokerReconPosition>? Positions { get; set; }
        [JsonPropertyName("cash")] public BrokerReconCash? Cash { get; set; }
        [JsonPropertyName("open_orders")] public BrokerReconOrders? OpenOrders { get; set; }
        [JsonPropertyName("mismatches")] public List<BrokerMismatch>? Mismatches { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("halted")] public bool? Halted { get; set; }
    }

    internal sealed class BrokerReconPosition
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("expected")] public double? Expected { get; set; }
        [JsonPropertyName("broker")] public double? Broker { get; set; }
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
    }

    internal sealed class BrokerReconCash
    {
        [JsonPropertyName("basis")] public string? Basis { get; set; }
        [JsonPropertyName("expected")] public double? Expected { get; set; }
        [JsonPropertyName("broker")] public double? Broker { get; set; }
        [JsonPropertyName("diff")] public double? Diff { get; set; }
        [JsonPropertyName("tolerance")] public double? Tolerance { get; set; }
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
    }

    internal sealed class BrokerReconOrders
    {
        [JsonPropertyName("unknown_at_broker")] public List<BrokerOrder>? UnknownAtBroker { get; set; }
        [JsonPropertyName("missing_at_broker")] public List<BrokerMissingOrder>? MissingAtBroker { get; set; }
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
    }

    internal sealed class BrokerMissingOrder
    {
        [JsonPropertyName("client_order_id")] public string? ClientOrderId { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
    }

    internal sealed class BrokerTestResult
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("endpoint")] public string? Endpoint { get; set; }
        [JsonPropertyName("paper")] public bool? Paper { get; set; }
        [JsonPropertyName("checks")] public List<DataTestCheck>? Checks { get; set; }
        [JsonPropertyName("flat")] public bool? Flat { get; set; }
        [JsonPropertyName("cash_basis")] public double? CashBasis { get; set; }
        [JsonPropertyName("positions")] public List<BrokerPosition>? Positions { get; set; }
        [JsonPropertyName("open_orders")] public List<BrokerOrder>? OpenOrders { get; set; }
        // §18.14 binance-demo (read-only test): balances to confirm at the first connect, UFOS_ vs other open orders
        [JsonPropertyName("currency")] public string? Currency { get; set; }
        [JsonPropertyName("currency_note")] public string? CurrencyNote { get; set; }
        [JsonPropertyName("balances")] public List<BrokerBalance>? Balances { get; set; }
        [JsonPropertyName("platform_open_orders")] public int? PlatformOpenOrders { get; set; }
        [JsonPropertyName("foreign_open_orders")] public int? ForeignOpenOrders { get; set; }
        [JsonPropertyName("server_time")] public JsonElement? ServerTime { get; set; }
    }

    /// <summary>connect / disconnect / reconcile / resolve answers: {broker, reconciliation?, resolution?}.</summary>
    internal sealed class BrokerActionResult
    {
        [JsonPropertyName("broker")] public BrokerStatus? Broker { get; set; }
        [JsonPropertyName("reconciliation")] public BrokerReconciliation? Reconciliation { get; set; }
        [JsonPropertyName("resolution")] public BrokerResolution? Resolution { get; set; }
    }

    internal sealed class BrokerResolution
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("action")] public string? Action { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("detail")] public JsonElement? Detail { get; set; }
    }

    /// <summary>A refused broker request ({error, code, checks?, positions?, open_orders?}, SPEC §18.10).</summary>
    internal sealed class BrokerRefusal
    {
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("checks")] public List<DataTestCheck>? Checks { get; set; }
        [JsonPropertyName("positions")] public List<BrokerPosition>? Positions { get; set; }
        [JsonPropertyName("open_orders")] public List<BrokerOrder>? OpenOrders { get; set; }
        // §18.14 409 balances_changed: the fresh balances to confirm again
        [JsonPropertyName("balances")] public List<BrokerBalance>? Balances { get; set; }
    }

    internal sealed class BrokerKillResult
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("cancelled_at_broker")] public int? CancelledAtBroker { get; set; }
        [JsonPropertyName("oms_cancelled")] public int? OmsCancelled { get; set; }
        // OmsOrders, or {symbol, state: "error", reject_reason} when a closing order could not be placed
        [JsonPropertyName("orders")] public List<OmsOrder>? Orders { get; set; }
        [JsonPropertyName("halt")] public TradingHalt? Halt { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        // §18.13 verified kill: flat true (verified) | false (positions remain) | null (positions unreadable)
        [JsonPropertyName("flat")] public bool? Flat { get; set; }
        [JsonPropertyName("pending")] public bool? Pending { get; set; }
        [JsonPropertyName("open_orders_left")] public int? OpenOrdersLeft { get; set; }
        [JsonPropertyName("attempts")] public int? Attempts { get; set; }
        [JsonPropertyName("not_flat")] public List<BrokerKillNotFlat>? NotFlat { get; set; }
        /// <summary>§18.14: orders that are not the platform's (Binance: no UFOS_ prefix), left untouched (not a failure; 0 / null on Alpaca).</summary>
        [JsonPropertyName("untouched_orders")] public int? UntouchedOrders { get; set; }
        [JsonPropertyName("untouched_note")] public string? UntouchedNote { get; set; }
        /// <summary>§18.14: positions the kill did not sell (dust below LOT_SIZE / NOTIONAL, below the starting balance, unmapped) — counted as flat.</summary>
        [JsonPropertyName("skipped")] public List<BrokerKillSkipped>? Skipped { get; set; }
        /// <summary>§18.14 review fix: cancels the broker refused (counted, with the reasons — the kill is then not ok).</summary>
        [JsonPropertyName("refused")] public List<BrokerKillRefused>? Refused { get; set; }
    }

    internal sealed class BrokerKillRefused
    {
        [JsonPropertyName("client_order_id")] public string? ClientOrderId { get; set; }
        [JsonPropertyName("broker_order_id")] public string? BrokerOrderId { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class BrokerKillSkipped
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    /// <summary>§18.13: one position the broker still holds after a kill.</summary>
    internal sealed class BrokerKillNotFlat
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("broker_qty")] public double? BrokerQty { get; set; }
        [JsonPropertyName("close_order")] public string? CloseOrder { get; set; }
        [JsonPropertyName("close_state")] public string? CloseState { get; set; }
        [JsonPropertyName("reject_reason")] public string? RejectReason { get; set; }
    }

    /// <summary>§18.13 BrokerStatus.kill — {pending, at, not_flat}.</summary>
    internal sealed class BrokerKillState
    {
        [JsonPropertyName("pending")] public bool? Pending { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
        [JsonPropertyName("not_flat")] public List<BrokerKillNotFlat>? NotFlat { get; set; }
        /// <summary>Shown when the backend sends them (§18.13 review fix); absent → nothing is shown.</summary>
        [JsonPropertyName("untouched_orders")] public int? UntouchedOrders { get; set; }
        [JsonPropertyName("untouched_note")] public string? UntouchedNote { get; set; }
    }

    internal sealed class BrokerLiveGuard
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("build_allows_live")] public bool? BuildAllowsLive { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("settings")] public BrokerLiveGuardSettings? Settings { get; set; }
        [JsonPropertyName("confirmation_phrase")] public string? ConfirmationPhrase { get; set; }
        [JsonPropertyName("conditions")] public List<BrokerLiveCondition>? Conditions { get; set; }
    }

    internal sealed class BrokerLiveGuardSettings
    {
        [JsonPropertyName("live_flag")] public bool? LiveFlag { get; set; }
        [JsonPropertyName("capital_cap_usd")] public double? CapitalCapUsd { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("updated_by")] public string? UpdatedBy { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class BrokerLiveCondition
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class BrokerRouting
    {
        [JsonPropertyName("default_account_id")] public string? DefaultAccountId { get; set; }
        [JsonPropertyName("routes")] public List<BrokerRoute>? Routes { get; set; }
    }

    internal sealed class BrokerRoute
    {
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("universe")] public string? Universe { get; set; }   // equity | crypto | mixed | null (unknown)
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("since")] public string? Since { get; set; }
        [JsonPropertyName("by")] public string? By { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("allowed_accounts")] public List<string>? AllowedAccounts { get; set; }
        [JsonPropertyName("running_incubation")] public string? RunningIncubation { get; set; }
        // §18.13: a route to a retired broker is shown blocked (never moved silently) — the user re-routes
        [JsonPropertyName("blocked")] public bool? Blocked { get; set; }
        [JsonPropertyName("blocked_reason")] public string? BlockedReason { get; set; }
        // §18.14: venues the universe would allow but the strategy cannot use (binance-demo: long_only) — with the reason
        [JsonPropertyName("refused_accounts")] public List<BrokerRefusedAccount>? RefusedAccounts { get; set; }
    }

    internal sealed class BrokerRefusedAccount
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class BrokerRouteResult
    {
        [JsonPropertyName("route")] public BrokerRoute? Route { get; set; }
    }
}

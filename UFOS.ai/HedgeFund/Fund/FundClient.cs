using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Newsroom;

namespace UFOS.ai.HedgeFund.Fund
{
    // =====================================================================================
    // Typed client for the HedgeFund autonomous loop API (/fund/*, backend/fund/api.py).
    // Contract: HedgeFund/backend/fund/SPEC.md §11. PAPER TRADING ONLY — there is no live
    // trading code path anywhere; every order is simulated by the backend's paper broker.
    //
    // - Every request carries the backend's session token (X-UFOS-Token) via
    //   NewsroomBackend.CreateClient -> Shared/BackendAuth.cs (security by default).
    // - Calls never throw: they return a FundResult with a status (Ok / Offline /
    //   Unauthorized / NotFound / Conflict / BadRequest / Error) and a short, safe message,
    //   so the UI can show the state instead of crashing.
    // - DTOs are tolerant: unknown fields are ignored, numbers may arrive as strings,
    //   free-form parts (params, limits, details, provenance) stay JsonElement.
    // =====================================================================================

    internal enum FundCallStatus { Ok, Offline, Unauthorized, NotFound, Conflict, BadRequest, Error }

    internal sealed class FundResult<T>
    {
        public FundCallStatus Status { get; init; }
        public T? Value { get; init; }
        public int? HttpStatus { get; init; }
        public string Message { get; init; } = "";
        public IReadOnlyList<string> Errors { get; init; } = Array.Empty<string>();
        public bool Ok => Status == FundCallStatus.Ok;

        /// <summary>One line for the UI ("Engine offline", "Not authorized …", validation errors …).</summary>
        public string Describe()
        {
            if (Errors.Count > 0) return string.Join("; ", Errors);
            return string.IsNullOrWhiteSpace(Message) ? Status.ToString() : Message;
        }
    }

    internal static class FundClient
    {
        internal static readonly JsonSerializerOptions Json = new()
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            PropertyNameCaseInsensitive = true,
            NumberHandling = JsonNumberHandling.AllowReadingFromString | JsonNumberHandling.AllowNamedFloatingPointLiterals,
        };

        private static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(8);

        // ---------- endpoints (SPEC §11) ----------

        public static Task<FundResult<FundState>> GetStateAsync()
            => SendAsync<FundState>(HttpMethod.Get, "/fund/state", null, DefaultTimeout);

        public static Task<FundResult<FundParamsResponse>> GetParamsAsync()
            => SendAsync<FundParamsResponse>(HttpMethod.Get, "/fund/params", null, DefaultTimeout);

        /// <summary>PUT /fund/params with a partial params dict (snake_case keys as in SPEC §3).</summary>
        public static Task<FundResult<FundParamsPutResponse>> PutParamsAsync(IDictionary<string, object?> partial)
            => SendAsync<FundParamsPutResponse>(HttpMethod.Put, "/fund/params", partial, DefaultTimeout);

        public static Task<FundResult<JsonElement>> RunNowAsync()
            => SendAsync<JsonElement>(HttpMethod.Post, "/fund/run", new Dictionary<string, object?>(), DefaultTimeout);

        public static Task<FundResult<FundAutopilotResponse>> SetAutopilotAsync(bool enabled)
            => SendAsync<FundAutopilotResponse>(HttpMethod.Post, "/fund/autopilot",
                new Dictionary<string, object?> { ["enabled"] = enabled }, DefaultTimeout);

        public static Task<FundResult<FundKillResponse>> KillAsync(string? reason)
        {
            var body = new Dictionary<string, object?>();
            if (!string.IsNullOrWhiteSpace(reason)) body["reason"] = reason.Trim();
            return SendAsync<FundKillResponse>(HttpMethod.Post, "/fund/kill", body, TimeSpan.FromSeconds(30));
        }

        public static Task<FundResult<JsonElement>> ResumeAsync()
            => SendAsync<JsonElement>(HttpMethod.Post, "/fund/resume", new Dictionary<string, object?>(), DefaultTimeout);

        public static Task<FundResult<List<FundRunSummary>>> GetRunsAsync(int limit = 50)
            => SendAsync<List<FundRunSummary>>(HttpMethod.Get, "/fund/runs?limit=" + limit.ToString(CultureInfo.InvariantCulture), null, DefaultTimeout);

        public static Task<FundResult<FundRunDetails>> GetRunAsync(long id)
            => SendAsync<FundRunDetails>(HttpMethod.Get, "/fund/runs/" + id.ToString(CultureInfo.InvariantCulture), null, TimeSpan.FromSeconds(15));

        /// <summary>POST /fund/orders — a manual simulated paper order. Exactly one of qty / notional.</summary>
        public static Task<FundResult<FundOrder>> SubmitOrderAsync(string symbol, string side, double? qty, double? notional)
        {
            var body = new Dictionary<string, object?> { ["symbol"] = symbol, ["side"] = side };
            if (qty.HasValue) body["qty"] = qty.Value;
            else if (notional.HasValue) body["notional"] = notional.Value;
            return SendAsync<FundOrder>(HttpMethod.Post, "/fund/orders", body, TimeSpan.FromSeconds(30));
        }

        public static Task<FundResult<List<FundOrder>>> ExecuteRecommendationAsync(long recommendationId)
            => SendAsync<List<FundOrder>>(HttpMethod.Post,
                "/fund/recommendations/" + recommendationId.ToString(CultureInfo.InvariantCulture) + "/execute",
                new Dictionary<string, object?>(), TimeSpan.FromSeconds(30));

        public static Task<FundResult<FundEquity>> GetEquityAsync(string? sinceIso = null)
            => SendAsync<FundEquity>(HttpMethod.Get,
                "/fund/equity" + (string.IsNullOrEmpty(sinceIso) ? "" : "?since=" + Uri.EscapeDataString(sinceIso)),
                null, TimeSpan.FromSeconds(15));

        public static Task<FundResult<List<FundEvent>>> GetEventsAsync(long afterId)
            => SendAsync<List<FundEvent>>(HttpMethod.Get, "/fund/events?after_id=" + afterId.ToString(CultureInfo.InvariantCulture), null, DefaultTimeout);

        /// <summary>POST /fund/backtest — rule-only, can take a while (price history download).</summary>
        public static Task<FundResult<FundBacktestResult>> RunBacktestAsync(IReadOnlyList<string>? symbols, string? start, string? end)
        {
            var body = new Dictionary<string, object?>();
            if (symbols != null && symbols.Count > 0) body["symbols"] = symbols.ToList();
            if (!string.IsNullOrEmpty(start)) body["start"] = start;
            if (!string.IsNullOrEmpty(end)) body["end"] = end;
            return SendAsync<FundBacktestResult>(HttpMethod.Post, "/fund/backtest", body, TimeSpan.FromMinutes(5));
        }

        /// <summary>GET /fund/export/{orders|decisions}.csv as raw bytes.</summary>
        public static async Task<FundResult<byte[]>> DownloadCsvAsync(string fileName)
        {
            if (fileName != "orders.csv" && fileName != "decisions.csv")
                return new FundResult<byte[]> { Status = FundCallStatus.Error, Message = "Unknown export." };
            try
            {
                using var client = NewsroomBackend.CreateClient(TimeSpan.FromSeconds(60));
                using var resp = await client.GetAsync(NewsroomBackend.BaseUrl + "/fund/export/" + fileName);
                if (!resp.IsSuccessStatusCode)
                {
                    var text = await SafeReadAsync(resp);
                    return Fail<byte[]>(resp.StatusCode, text);
                }
                var bytes = await resp.Content.ReadAsByteArrayAsync();
                return new FundResult<byte[]> { Status = FundCallStatus.Ok, Value = bytes, HttpStatus = (int)resp.StatusCode };
            }
            catch (Exception ex)
            {
                return Offline<byte[]>(ex);
            }
        }

        // ---------- plumbing ----------

        private static async Task<FundResult<T>> SendAsync<T>(HttpMethod method, string path, object? body, TimeSpan timeout)
        {
            try
            {
                using var client = NewsroomBackend.CreateClient(timeout);
                using var request = new HttpRequestMessage(method, NewsroomBackend.BaseUrl + path);
                if (body != null)
                {
                    request.Content = new StringContent(JsonSerializer.Serialize(body, Json), Encoding.UTF8, "application/json");
                }
                using var resp = await client.SendAsync(request);
                var text = await SafeReadAsync(resp);
                if (!resp.IsSuccessStatusCode) return Fail<T>(resp.StatusCode, text);

                T? value;
                try
                {
                    value = string.IsNullOrWhiteSpace(text) ? default : JsonSerializer.Deserialize<T>(text, Json);
                }
                catch (Exception)
                {
                    return new FundResult<T>
                    {
                        Status = FundCallStatus.Error,
                        HttpStatus = (int)resp.StatusCode,
                        Message = "Unexpected response from the engine (" + path + ") — backend and app versions may not match.",
                    };
                }
                return new FundResult<T> { Status = FundCallStatus.Ok, Value = value, HttpStatus = (int)resp.StatusCode };
            }
            catch (Exception ex)
            {
                return Offline<T>(ex);
            }
        }

        private static async Task<string> SafeReadAsync(HttpResponseMessage resp)
        {
            try { return await resp.Content.ReadAsStringAsync(); }
            catch { return ""; }
        }

        private static FundResult<T> Offline<T>(Exception ex)
        {
            var timeout = ex is TaskCanceledException || ex is OperationCanceledException;
            return new FundResult<T>
            {
                Status = FundCallStatus.Offline,
                Message = timeout ? "Engine did not answer in time." : "Engine offline — the Hedge Fund backend is not reachable.",
            };
        }

        private static FundResult<T> Fail<T>(HttpStatusCode code, string body)
        {
            var errors = ParseErrors(body);
            var status = code switch
            {
                HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden => FundCallStatus.Unauthorized,
                HttpStatusCode.NotFound => FundCallStatus.NotFound,
                HttpStatusCode.Conflict => FundCallStatus.Conflict,
                HttpStatusCode.BadRequest or HttpStatusCode.UnprocessableEntity => FundCallStatus.BadRequest,
                _ => FundCallStatus.Error,
            };
            var message = status switch
            {
                FundCallStatus.Unauthorized => "Not authorized — the backend's session token did not match (restart the Hedge Fund backend).",
                FundCallStatus.NotFound => "Not found — the running backend may be outdated (no /fund API) or the item no longer exists.",
                FundCallStatus.Conflict => "Refused by the engine (conflict).",
                FundCallStatus.BadRequest => "Rejected by the engine (invalid input).",
                _ => "Engine error (HTTP " + (int)code + ").",
            };
            if (code == HttpStatusCode.ServiceUnavailable)
            {
                // 503 {error: "audit log unavailable"} (backend 2026-09-26): the engine refuses
                // state-changing actions while it cannot write its audit trail. Say so plainly.
                var audit = errors.Any(e => e.Contains("audit", StringComparison.OrdinalIgnoreCase));
                message = audit
                    ? "The engine's audit log is unavailable (disk full, file locked or deleted?) — the action was refused because it could not be recorded. See the Backend Log."
                    : "Engine temporarily unavailable (HTTP 503).";
                if (audit) errors = new[] { message };
            }
            return new FundResult<T> { Status = status, HttpStatus = (int)code, Message = message, Errors = errors };
        }

        /// <summary>Reads {"errors": [...] | {key: msg}} or {"error": "..."} bodies into short lines.</summary>
        internal static IReadOnlyList<string> ParseErrors(string body)
        {
            var list = new List<string>();
            if (string.IsNullOrWhiteSpace(body)) return list;
            try
            {
                using var doc = JsonDocument.Parse(body);
                var root = doc.RootElement;
                if (root.ValueKind != JsonValueKind.Object) return list;
                if (root.TryGetProperty("errors", out var errs))
                {
                    if (errs.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var e in errs.EnumerateArray()) list.Add(FundFormat.Plain(e));
                    }
                    else if (errs.ValueKind == JsonValueKind.Object)
                    {
                        foreach (var p in errs.EnumerateObject()) list.Add(p.Name + ": " + FundFormat.Plain(p.Value));
                    }
                    else list.Add(FundFormat.Plain(errs));
                }
                foreach (var key in new[] { "error", "message", "detail", "reason" })
                {
                    if (list.Count == 0 && root.TryGetProperty(key, out var e) && e.ValueKind == JsonValueKind.String)
                        list.Add(e.GetString() ?? "");
                }
            }
            catch { /* not JSON (e.g. a plain-text 500) — keep the generic message */ }
            return list.Where(s => !string.IsNullOrWhiteSpace(s)).Select(s => s.Length > 300 ? s[..300] + "…" : s).ToList();
        }
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §11 (snake_case).
    // =====================================================================================

    internal sealed class FundState
    {
        [JsonPropertyName("now")] public string? Now { get; set; }
        [JsonPropertyName("market")] public FundMarket? Market { get; set; }
        [JsonPropertyName("engine")] public FundEngine? Engine { get; set; }
        [JsonPropertyName("autopilot")] public bool Autopilot { get; set; }
        [JsonPropertyName("halted")] public FundHalt? Halted { get; set; }
        [JsonPropertyName("params_version")] public int? ParamsVersion { get; set; }
        [JsonPropertyName("pending_params")] public bool PendingParams { get; set; }
        [JsonPropertyName("ai_budget")] public FundAiBudget? AiBudget { get; set; }
        [JsonPropertyName("account")] public FundAccount? Account { get; set; }
        [JsonPropertyName("positions")] public List<FundPosition>? Positions { get; set; }
        [JsonPropertyName("pending_orders")] public List<FundOrder>? PendingOrders { get; set; }
        [JsonPropertyName("symbols")] public List<FundSymbolState>? Symbols { get; set; }
        // Dead-man switch (backend 2026-09-26, optional): when the engine last heard from this
        // app (any authenticated request). The autopilot switches itself off after
        // params.autopilot_heartbeat_minutes without the app. Absent on older backends.
        // Kept raw: ISO string expected, epoch seconds tolerated (see LastClientSeenAt).
        [JsonPropertyName("last_client_seen")] public JsonElement? LastClientSeen { get; set; }

        public DateTimeOffset? LastClientSeenAt
        {
            get
            {
                if (LastClientSeen is not JsonElement e) return null;
                if (e.ValueKind == JsonValueKind.String) return FundFormat.ParseTs(e.GetString());
                if (e.ValueKind == JsonValueKind.Number && e.TryGetDouble(out var secs) && secs > 0 && secs < 1e11)
                {
                    try { return DateTimeOffset.FromUnixTimeMilliseconds((long)(secs * 1000)); } catch { return null; }
                }
                return null;
            }
        }
    }

    internal sealed class FundMarket
    {
        [JsonPropertyName("open")] public bool Open { get; set; }
        [JsonPropertyName("session")] public string? Session { get; set; }
        [JsonPropertyName("next_open")] public string? NextOpen { get; set; }
        [JsonPropertyName("next_close")] public string? NextClose { get; set; }
        [JsonPropertyName("trading_day")] public string? TradingDay { get; set; }
        [JsonPropertyName("delayed_quotes")] public bool DelayedQuotes { get; set; } = true;
    }

    internal sealed class FundEngine
    {
        [JsonPropertyName("running")] public bool Running { get; set; }
        [JsonPropertyName("cycle_in_progress")] public bool CycleInProgress { get; set; }
        [JsonPropertyName("next_run_at")] public string? NextRunAt { get; set; }
        [JsonPropertyName("interval_minutes")] public double? IntervalMinutes { get; set; }
        [JsonPropertyName("last_run")] public FundRunInfo? LastRun { get; set; }
        // Dead-man switch (2026-09-26, optional): minutes without the app until the autopilot is
        // switched off, and the moment that happens if nothing is heard from the app (null = n/a).
        [JsonPropertyName("autopilot_heartbeat_minutes")] public double? AutopilotHeartbeatMinutes { get; set; }
        [JsonPropertyName("autopilot_off_at")] public string? AutopilotOffAt { get; set; }
        // 24/7 crypto (2026-09-26, optional): "session" = scheduled cycles during the US regular
        // session; "manual_only" = outside it, no scheduled cycles (next_run_at is null) — only
        // "Run now" runs a full cycle. next_risk_watch_at: the next light risk watch (no agents,
        // no AI, no new orders) while crypto positions are open, else null.
        [JsonPropertyName("schedule_mode")] public string? ScheduleMode { get; set; }
        [JsonPropertyName("next_risk_watch_at")] public string? NextRiskWatchAt { get; set; }
        // Backend final (2026-09-26, optional): next scheduled full cycle (the next session run,
        // also while manual_only), whether the crypto risk watch is on, and its interval.
        [JsonPropertyName("next_session_run_at")] public string? NextSessionRunAt { get; set; }
        [JsonPropertyName("crypto_risk_watch")] [JsonConverter(typeof(FlexBoolConverter))] public bool? CryptoRiskWatch { get; set; }
        [JsonPropertyName("risk_watch_interval_minutes")] public double? RiskWatchIntervalMinutes { get; set; }

        public bool IsManualOnly => string.Equals(ScheduleMode, "manual_only", StringComparison.OrdinalIgnoreCase);
    }

    internal sealed class FundRunInfo
    {
        [JsonPropertyName("id")] public long Id { get; set; }
        [JsonPropertyName("started_at")] public string? StartedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("judge_mode")] public string? JudgeMode { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // Optional (2026-09-26): "manual" (Run now), "scheduled", "startup", "risk-watch", …
        [JsonPropertyName("trigger")] public string? Trigger { get; set; }
        [JsonPropertyName("params_version")] public int? ParamsVersion { get; set; }
    }

    internal sealed class FundHalt
    {
        [JsonPropertyName("active")] public bool Active { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
    }

    internal sealed class FundAiBudget
    {
        [JsonPropertyName("limit_usd")] public double? LimitUsd { get; set; }
        [JsonPropertyName("spent_today_usd")] public double? SpentTodayUsd { get; set; }
        [JsonPropertyName("exhausted")] public bool Exhausted { get; set; }
        // True when today's AI spend could not be read — the engine then fails closed (no AI calls).
        [JsonPropertyName("spend_unreadable")] [JsonConverter(typeof(FlexBoolConverter))] public bool? SpendUnreadable { get; set; }
    }

    internal sealed class FundAccount
    {
        [JsonPropertyName("equity")] public double? Equity { get; set; }
        [JsonPropertyName("cash")] public double? Cash { get; set; }
        [JsonPropertyName("long_mv")] public double? LongMv { get; set; }
        [JsonPropertyName("short_mv")] public double? ShortMv { get; set; }
        [JsonPropertyName("buying_power")] public double? BuyingPower { get; set; }
        [JsonPropertyName("day_pnl")] public double? DayPnl { get; set; }
        [JsonPropertyName("total_pnl")] public double? TotalPnl { get; set; }
        [JsonPropertyName("total_pnl_pct")] public double? TotalPnlPct { get; set; }
        [JsonPropertyName("peak_equity")] public double? PeakEquity { get; set; }
        [JsonPropertyName("drawdown_pct")] public double? DrawdownPct { get; set; }
        [JsonPropertyName("drawdown_stop_pct")] public double? DrawdownStopPct { get; set; }
        [JsonPropertyName("short_exposure_pct")] public double? ShortExposurePct { get; set; }
        [JsonPropertyName("maintenance_required")] public double? MaintenanceRequired { get; set; }
        [JsonPropertyName("margin_excess")] public double? MarginExcess { get; set; }
        // Crypto perps (2026-09-26, optional): funding booked so far (signed, + = received by us)
        // and the excess over the crypto maintenance margin (null = no crypto margin in use).
        [JsonPropertyName("funding_total")] public double? FundingTotal { get; set; }
        [JsonPropertyName("crypto_margin_excess")] public double? CryptoMarginExcess { get; set; }
        [JsonPropertyName("starting_capital")] public double? StartingCapital { get; set; }
        [JsonPropertyName("reset_at")] public string? ResetAt { get; set; }
        // Account state unreadable (2026-09-26): ALL fields above are null, "unreadable": true and
        // "error" says why; autopilot, drawdown checks and snapshots are paused for that run.
        [JsonPropertyName("unreadable")] [JsonConverter(typeof(FlexBoolConverter))] public bool? Unreadable { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
    }

    internal sealed class FundPosition
    {
        [JsonPropertyName("symbol")] public string Symbol { get; set; } = "";
        // Nullable (2026-09-26 review): one null qty must not make the whole /fund/state unreadable.
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("avg_price")] public double? AvgPrice { get; set; }
        [JsonPropertyName("last_price")] public double? LastPrice { get; set; }
        [JsonPropertyName("market_value")] public double? MarketValue { get; set; }
        [JsonPropertyName("weight")] public double? Weight { get; set; }
        [JsonPropertyName("unrealized_pnl")] public double? UnrealizedPnl { get; set; }
        [JsonPropertyName("realized_pnl")] public double? RealizedPnl { get; set; }
        [JsonPropertyName("opened_at")] public string? OpenedAt { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("borrow_rate_pct")] public double? BorrowRatePct { get; set; }
        // 2026-09-26 (optional): "crypto" | "equity"; instrument "spot" | "perp" | "stock".
        // Crypto quantities are fractional (up to 8 decimals).
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("instrument")] public string? Instrument { get; set; }

        public bool IsCrypto => string.Equals(AssetClass, "crypto", StringComparison.OrdinalIgnoreCase);
        public bool IsPerp => string.Equals(Instrument, "perp", StringComparison.OrdinalIgnoreCase);

        public bool IsShort => (Qty ?? 0) < 0 || string.Equals(Side, "short", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>An orders row (GET /fund/orders, run details, pending_orders) or an OrderResult (POST responses).</summary>
    internal sealed class FundOrder
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("order_id")] public long? OrderId { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("fill_price")] public double? FillPrice { get; set; }
        [JsonPropertyName("fees")] public double? Fees { get; set; }
        [JsonPropertyName("notional")] public double? Notional { get; set; }
        [JsonPropertyName("reject_reason")] public string? RejectReason { get; set; }
        [JsonPropertyName("origin")] public string? Origin { get; set; }
        [JsonPropertyName("filled_at")] public string? FilledAt { get; set; }
        [JsonPropertyName("run_id")] public long? RunId { get; set; }
        [JsonPropertyName("recommendation_id")] public long? RecommendationId { get; set; }

        public long? EffectiveId => OrderId ?? Id;
    }

    /// <summary>A cash-ledger row (fills, fees, borrow, dividends, funding, liquidation_fee …). qty may be fractional (crypto).</summary>
    internal sealed class FundLedgerEntry
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("amount")] public double? Amount { get; set; }
        [JsonPropertyName("order_id")] public long? OrderId { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    /// <summary>Asset-class helpers shared by the dashboard (crypto symbols: BTC-USD, ETH-USD, …).</summary>
    internal static class FundAssets
    {
        private static readonly System.Text.RegularExpressions.Regex CryptoPattern =
            new("^[A-Z0-9]{2,10}-USD$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);
        private static readonly System.Text.RegularExpressions.Regex EquityPattern =
            new("^[A-Z][A-Z0-9.\\-]{0,9}$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);

        /// <summary>Crypto symbol pattern of the engine (SPEC §3, 2026-09-26): ^[A-Z0-9]{2,10}-USD$.</summary>
        public static bool LooksLikeCrypto(string? symbol) => !string.IsNullOrEmpty(symbol) && CryptoPattern.IsMatch(symbol);

        /// <summary>Equity/ETF ticker or crypto pair.</summary>
        public static bool IsValidSymbol(string? symbol)
            => !string.IsNullOrEmpty(symbol) && (EquityPattern.IsMatch(symbol) || CryptoPattern.IsMatch(symbol));

        /// <summary>"BTC-USD" -> "BTC" (unit name for quantities).</summary>
        public static string BaseAsset(string symbol)
        {
            var i = symbol.LastIndexOf("-USD", StringComparison.Ordinal);
            return i > 0 ? symbol[..i] : symbol;
        }
    }

    internal sealed class FundSymbolState
    {
        [JsonPropertyName("symbol")] public string Symbol { get; set; } = "";
        [JsonPropertyName("quote")] public FundQuote? Quote { get; set; }
        [JsonPropertyName("agents")] public List<FundAgentSignal>? Agents { get; set; }
        [JsonPropertyName("recommendation")] public FundRecommendation? Recommendation { get; set; }
        // 2026-09-26 (optional): "crypto" | "equity" and whether the symbol can fill right now
        // (crypto 24/7; equities only during the US regular session).
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("tradable_now")] [JsonConverter(typeof(FlexBoolConverter))] public bool? TradableNow { get; set; }

        public bool IsCrypto => string.Equals(AssetClass, "crypto", StringComparison.OrdinalIgnoreCase);
    }

    internal sealed class FundQuote
    {
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("prev_close")] public double? PrevClose { get; set; }
        [JsonPropertyName("change_pct")] public double? ChangePct { get; set; }
        [JsonPropertyName("delayed")] public bool Delayed { get; set; } = true;
        [JsonPropertyName("ts")] public string? Ts { get; set; }
    }

    internal sealed class FundAgentSignal
    {
        [JsonPropertyName("agent")] public string Agent { get; set; } = "";
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        // Nullable: a null score/confidence (e.g. an unavailable agent) must not break /fund/state.
        [JsonPropertyName("score")] public double? Score { get; set; }
        [JsonPropertyName("confidence")] public double? Confidence { get; set; }
        [JsonPropertyName("summary")] public string? Summary { get; set; }
        [JsonPropertyName("details")] public JsonElement? Details { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
    }

    internal sealed class FundRecommendation
    {
        [JsonPropertyName("id")] public long Id { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("action")] public string? Action { get; set; }
        [JsonPropertyName("confidence")] public double? Confidence { get; set; }
        [JsonPropertyName("target_weight")] public double? TargetWeight { get; set; }
        [JsonPropertyName("rule_score")] public double? RuleScore { get; set; }
        [JsonPropertyName("rationale")] public string? Rationale { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("model")] public string? Model { get; set; }
        [JsonPropertyName("provenance")] public JsonElement? Provenance { get; set; }
        [JsonPropertyName("usage")] public FundUsage? Usage { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("skip_reason")] public string? SkipReason { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("run_id")] public long? RunId { get; set; }
        [JsonPropertyName("current_weight")] public double? CurrentWeight { get; set; }
        [JsonPropertyName("order_id")] public long? OrderId { get; set; }
    }

    internal sealed class FundUsage
    {
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        // Tolerance: newsroom.ai usage dicts may call it "cost" (OpenRouter's usage.cost).
        [JsonPropertyName("cost")] public double? Cost { get; set; }
        [JsonPropertyName("prompt_tokens")] public long? PromptTokens { get; set; }
        [JsonPropertyName("completion_tokens")] public long? CompletionTokens { get; set; }
        // fund/llm.py split_usage(): cost_usd/tokens are this symbol's SHARE of one batch call;
        // batch_cost_usd is the whole call, batch_size the number of symbols in it. Optional —
        // absent in older rows and null after merge_usage (premium_on_flip).
        [JsonPropertyName("batch_cost_usd")] public double? BatchCostUsd { get; set; }
        [JsonPropertyName("batch_size")] public int? BatchSize { get; set; }
        [JsonPropertyName("cost_estimated")] [JsonConverter(typeof(FlexBoolConverter))] public bool? CostEstimated { get; set; }

        public double? EffectiveCost => CostUsd ?? Cost;
    }

    internal sealed class FundParamsResponse
    {
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("params")] public JsonElement? Params { get; set; }
        [JsonPropertyName("defaults")] public JsonElement? Defaults { get; set; }
        [JsonPropertyName("limits")] public JsonElement? Limits { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
    }

    internal sealed class FundParamsPutResponse
    {
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("params")] public JsonElement? Params { get; set; }
        [JsonPropertyName("effective_from")] public string? EffectiveFrom { get; set; }
    }

    internal sealed class FundAutopilotResponse
    {
        [JsonPropertyName("autopilot")] public bool Autopilot { get; set; }
    }

    internal sealed class FundKillResponse
    {
        // SPEC: {halted, orders:[...]} — "halted" may be a bool or the halt object; kept raw.
        [JsonPropertyName("halted")] public JsonElement? Halted { get; set; }
        [JsonPropertyName("orders")] public List<FundOrder>? Orders { get; set; }
    }

    internal sealed class FundRunSummary
    {
        [JsonPropertyName("id")] public long Id { get; set; }
        [JsonPropertyName("started_at")] public string? StartedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("trigger")] public string? Trigger { get; set; }
        [JsonPropertyName("params_version")] public int? ParamsVersion { get; set; }
        [JsonPropertyName("market_open")] [JsonConverter(typeof(FlexBoolConverter))] public bool? MarketOpen { get; set; }
        [JsonPropertyName("judge_mode")] public string? JudgeMode { get; set; }
        [JsonPropertyName("models")] public JsonElement? Models { get; set; }
        [JsonPropertyName("cost_usd")] public double? CostUsd { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("recommendations")] public int? Recommendations { get; set; }
        [JsonPropertyName("orders")] public int? Orders { get; set; }
        [JsonPropertyName("flips")] public List<JsonElement>? Flips { get; set; }
    }

    internal sealed class FundRunDetails
    {
        [JsonPropertyName("run")] public FundRunSummary? Run { get; set; }
        [JsonPropertyName("params")] public JsonElement? Params { get; set; }
        [JsonPropertyName("agents")] public List<FundAgentSignal>? Agents { get; set; }
        [JsonPropertyName("recommendations")] public List<FundRecommendation>? Recommendations { get; set; }
        [JsonPropertyName("orders")] public List<FundOrder>? Orders { get; set; }
    }

    internal sealed class FundEquity
    {
        [JsonPropertyName("points")] public List<FundEquityPoint>? Points { get; set; }
        [JsonPropertyName("starting_capital")] public double? StartingCapital { get; set; }
        [JsonPropertyName("reset_at")] public string? ResetAt { get; set; }
    }

    internal sealed class FundEquityPoint
    {
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("equity")] public double? Equity { get; set; }
        [JsonPropertyName("cash")] public double? Cash { get; set; }
        [JsonPropertyName("long_mv")] public double? LongMv { get; set; }
        [JsonPropertyName("short_mv")] public double? ShortMv { get; set; }
        [JsonPropertyName("benchmark_equity")] public double? BenchmarkEquity { get; set; }
        [JsonPropertyName("drawdown_pct")] public double? DrawdownPct { get; set; }
    }

    internal sealed class FundEvent
    {
        [JsonPropertyName("id")] public long Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("title")] public string? Title { get; set; }
        [JsonPropertyName("body")] public string? Body { get; set; }
    }

    internal sealed class FundBacktestResult
    {
        [JsonPropertyName("symbols")] public List<string>? Symbols { get; set; }
        [JsonPropertyName("start")] public string? Start { get; set; }
        [JsonPropertyName("end")] public string? End { get; set; }
        [JsonPropertyName("portfolio")] public FundBacktestStats? Portfolio { get; set; }
        [JsonPropertyName("benchmark")] public JsonElement? Benchmark { get; set; }
        [JsonPropertyName("per_symbol")] public List<JsonElement>? PerSymbol { get; set; }
        [JsonPropertyName("equity")] public List<FundBacktestPoint>? Equity { get; set; }
        [JsonPropertyName("notes")] public List<string>? Notes { get; set; }
    }

    internal sealed class FundBacktestStats
    {
        [JsonPropertyName("cagr_pct")] public double? CagrPct { get; set; }
        [JsonPropertyName("total_return_pct")] public double? TotalReturnPct { get; set; }
        [JsonPropertyName("max_drawdown_pct")] public double? MaxDrawdownPct { get; set; }
        [JsonPropertyName("sharpe")] public double? Sharpe { get; set; }
        [JsonPropertyName("trades")] public double? Trades { get; set; }
        [JsonPropertyName("hit_rate_pct")] public double? HitRatePct { get; set; }
        [JsonPropertyName("exposure_pct")] public double? ExposurePct { get; set; }
    }

    internal sealed class FundBacktestPoint
    {
        [JsonPropertyName("date")] public string? Date { get; set; }
        [JsonPropertyName("equity")] public double? Equity { get; set; }
        [JsonPropertyName("benchmark")] public double? Benchmark { get; set; }
    }

    /// <summary>Accepts true/false, 0/1 and "true"/"1" (SQLite INTEGER flags) for a bool?.</summary>
    internal sealed class FlexBoolConverter : JsonConverter<bool?>
    {
        public override bool? Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
        {
            switch (reader.TokenType)
            {
                case JsonTokenType.True: return true;
                case JsonTokenType.False: return false;
                case JsonTokenType.Number: return reader.TryGetDouble(out var d) && Math.Abs(d) > 0.5;
                case JsonTokenType.String:
                    var s = reader.GetString();
                    return s == "1" || string.Equals(s, "true", StringComparison.OrdinalIgnoreCase);
                case JsonTokenType.Null: return null;
                default:
                    reader.Skip();
                    return null;
            }
        }

        public override void Write(Utf8JsonWriter writer, bool? value, JsonSerializerOptions options)
        {
            if (value.HasValue) writer.WriteBooleanValue(value.Value);
            else writer.WriteNullValue();
        }
    }
}

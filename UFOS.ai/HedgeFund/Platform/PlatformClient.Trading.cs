using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Strategy Platform stage 7 — portfolio, risk, OMS (paper) (contract: backend/strategy_platform/SPEC.md §17,
    // routes §17.10, exact JSON §17.11, deviations §17.12, WPF notes §17.13). Same rules as the rest of PlatformClient:
    // session token on every call, never throws, the backend's `error` text is shown verbatim (409 halted / not_open /
    // account_exists / not_cancellable / not_validated / already_incubating / account_not_open / locked / not_running /
    // migration_blocked, 404 not_found, 503 valuation_paused / audit_unavailable), no retries and no alternative paths.
    // PAPER TRADING ONLY: there is no live route; the kill switch is NOT here — the pinned toolbar button
    // (POST /fund/kill) already runs the old engine's kill AND the platform kill (§17.1), so the view only shows its scope.
    // =====================================================================================

    internal static partial class PlatformClient
    {
        private static readonly TimeSpan TradingTimeout = TimeSpan.FromSeconds(15);
        private static readonly TimeSpan TradingActionTimeout = TimeSpan.FromSeconds(30);
        /// <summary>
        /// Retest 2026-10-23 (an order was filled after the client gave up at 30 s): order submit and resume may wait on quotes through
        /// the backend's HTTP gate (bucket wait up to 120 s, 3 attempts with 1 s / 3 s back-off) — the client waits as long.
        /// </summary>
        private static readonly TimeSpan TradingSubmitTimeout = TimeSpan.FromSeconds(150);

        /// <summary>The text for an order submit whose answer did not arrive in time (it may have been placed).</summary>
        public const string OrderNoAnswerText = "No answer in time — the order may have been placed. Check Orders before retrying; retrying keeps the same client order id, so a duplicate is refused.";
        private static readonly TimeSpan MigrationTimeout = TimeSpan.FromSeconds(90);

        public const string MainAccountId = "paper-main";
        public const string CouncilAccountId = "paper-council";
        /// <summary>SPEC §17.10: reason texts 3..500 characters (settings, rebalance, resume, incubation, migration).</summary>
        public const int TradingReasonMin = 3;
        public const int TradingReasonMax = 500;

        /// <summary>GET /v2/portfolio/state — auto trading, runtime, legacy account (coexistence), account summaries.</summary>
        public static Task<PlatformResult<TradingState>> GetTradingStateAsync()
            => SendAsync<TradingState>(HttpMethod.Get, "/v2/portfolio/state", null, TradingTimeout);

        /// <summary>POST /v2/portfolio/accounts → 201 {account}; 409 account_exists.</summary>
        public static Task<PlatformResult<TradingAccountResult>> OpenTradingAccountAsync(double startingCapital, string? label)
        {
            var body = new Dictionary<string, object?> { ["starting_capital"] = startingCapital };
            if (!string.IsNullOrWhiteSpace(label)) body["label"] = label.Trim();
            return SendAsync<TradingAccountResult>(HttpMethod.Post, "/v2/portfolio/accounts", body, TradingActionTimeout);
        }

        /// <summary>GET /v2/portfolio/accounts/{id} → {account, positions, working_orders, sources, settings}.</summary>
        public static Task<PlatformResult<TradingAccountDetail>> GetTradingAccountAsync(string accountId)
            => SendAsync<TradingAccountDetail>(HttpMethod.Get, "/v2/portfolio/accounts/" + Esc(accountId), null, TradingTimeout);

        /// <summary>GET /v2/portfolio/sources/{source_id}/ledger?account_id=&amp;limit= → entries, newest first.</summary>
        public static Task<PlatformResult<TradingLedger>> GetSourceLedgerAsync(string accountId, string sourceId, int limit = 200)
            => SendAsync<TradingLedger>(HttpMethod.Get, "/v2/portfolio/sources/" + Esc(sourceId) + "/ledger?account_id=" + Esc(accountId)
                + "&limit=" + Math.Clamp(limit, 1, 1000).ToString(CultureInfo.InvariantCulture), null, TradingTimeout);

        /// <summary>GET /v2/portfolio/allocation?account_id= → latest allocation (null before the first), params, next rebalance.</summary>
        public static Task<PlatformResult<TradingAllocationPayload>> GetAllocationAsync(string accountId)
            => SendAsync<TradingAllocationPayload>(HttpMethod.Get, "/v2/portfolio/allocation?account_id=" + Esc(accountId), null, TradingTimeout);

        /// <summary>POST /v2/portfolio/allocation/rebalance {account_id, reason} → the same payload.</summary>
        public static Task<PlatformResult<TradingAllocationPayload>> RebalanceAsync(string accountId, string reason)
            => SendAsync<TradingAllocationPayload>(HttpMethod.Post, "/v2/portfolio/allocation/rebalance",
                WithNote(new Dictionary<string, object?> { ["account_id"] = accountId }, reason), TradingActionTimeout);

        /// <summary>GET /v2/portfolio/settings?account_id= → settings, defaults, bounds (groups pre_trade / post_trade / allocator).</summary>
        public static Task<PlatformResult<TradingSettings>> GetTradingSettingsAsync(string accountId)
            => SendAsync<TradingSettings>(HttpMethod.Get, "/v2/portfolio/settings?account_id=" + Esc(accountId), null, TradingTimeout);

        /// <summary>PUT /v2/portfolio/settings {account_id, reason, changes} — bounds enforced by the backend (400 per key, never clamped).</summary>
        public static Task<PlatformResult<TradingSettings>> PutTradingSettingsAsync(string accountId, string reason, IReadOnlyDictionary<string, double> changes)
        {
            var c = new Dictionary<string, object?>();
            foreach (var kv in changes) c[kv.Key] = kv.Value;
            return SendAsync<TradingSettings>(HttpMethod.Put, "/v2/portfolio/settings",
                WithNote(new Dictionary<string, object?> { ["account_id"] = accountId, ["changes"] = c }, reason), TradingActionTimeout);
        }

        /// <summary>GET /v2/portfolio/migration → {legacy_account, migration, can_migrate}.</summary>
        public static Task<PlatformResult<TradingMigrationStatus>> GetMigrationAsync()
            => SendAsync<TradingMigrationStatus>(HttpMethod.Get, "/v2/portfolio/migration", null, TradingTimeout);

        /// <summary>POST /v2/portfolio/migration/dry-run {} → report; nothing is written except an audit row.</summary>
        public static Task<PlatformResult<TradingDryRun>> MigrationDryRunAsync()
            => SendAsync<TradingDryRun>(HttpMethod.Post, "/v2/portfolio/migration/dry-run", new Dictionary<string, object?>(), MigrationTimeout);

        /// <summary>POST /v2/portfolio/migration {reason?} (fix1023: no confirm word, note optional) — the hard switch; 409 migration_blocked with blockers.</summary>
        public static Task<PlatformResult<TradingMigrationResult>> MigrateAsync(string? reason)
            => SendAsync<TradingMigrationResult>(HttpMethod.Post, "/v2/portfolio/migration",
                WithNote(new Dictionary<string, object?>(), reason), MigrationTimeout);   // fix1023: no "MIGRATE" is sent any more

        /// <summary>GET /v2/oms/orders?account_id=&amp;state=&amp;limit= → orders, newest first.</summary>
        public static Task<PlatformResult<OmsOrderList>> ListOmsOrdersAsync(string? accountId, string? state, int limit)
        {
            var q = new List<string> { "limit=" + Math.Clamp(limit, 1, 500).ToString(CultureInfo.InvariantCulture) };
            if (!string.IsNullOrEmpty(accountId)) q.Add("account_id=" + Esc(accountId));
            if (!string.IsNullOrEmpty(state)) q.Add("state=" + Esc(state));
            return SendAsync<OmsOrderList>(HttpMethod.Get, "/v2/oms/orders?" + string.Join("&", q), null, TradingTimeout);
        }

        /// <summary>GET /v2/oms/orders/{client_order_id} → {order, events}.</summary>
        public static Task<PlatformResult<OmsOrderDetail>> GetOmsOrderAsync(string clientOrderId)
            => SendAsync<OmsOrderDetail>(HttpMethod.Get, "/v2/oms/orders/" + Esc(clientOrderId), null, TradingTimeout);

        /// <summary>
        /// POST /v2/oms/orders — a manual simulated paper order. Exactly one of qty / notional. The client order id is a
        /// fresh GUID ("N" format, 32 hex characters — matches ^[A-Za-z0-9_-]{8,64}$ and none of the reserved engine
        /// prefixes); a repeated id returns the stored order (200), so a retry by the user never sends twice.
        /// A failed pre-trade check comes back as an order in state "rejected" with its checks (201).
        /// </summary>
        public static Task<PlatformResult<OmsOrderResult>> PlaceOmsOrderAsync(string accountId, string symbol, string side, double? qty,
                                                                              double? notional, string? sourceId, string? reason, string clientOrderId)
        {
            if (side != "buy" && side != "sell" && side != "short" && side != "cover")
                throw new ArgumentException("Unknown side.", nameof(side));
            var body = new Dictionary<string, object?>
            {
                ["account_id"] = accountId,
                ["symbol"] = symbol,
                ["side"] = side,
                ["client_order_id"] = clientOrderId,
            };
            if (qty.HasValue) body["qty"] = qty.Value;
            else if (notional.HasValue) body["notional"] = notional.Value;
            if (!string.IsNullOrWhiteSpace(sourceId)) body["source_id"] = sourceId;
            if (!string.IsNullOrWhiteSpace(reason)) body["reason"] = reason.Trim();
            return SendAsync<OmsOrderResult>(HttpMethod.Post, "/v2/oms/orders", body, TradingSubmitTimeout);
        }

        /// <summary>POST /v2/oms/orders/{client_order_id}/cancel {} → {order}; 409 not_cancellable.</summary>
        public static Task<PlatformResult<OmsOrderResult>> CancelOmsOrderAsync(string clientOrderId)
            => SendAsync<OmsOrderResult>(HttpMethod.Post, "/v2/oms/orders/" + Esc(clientOrderId) + "/cancel",
                new Dictionary<string, object?>(), TradingActionTimeout);

        /// <summary>GET /v2/oms/fills?account_id=&amp;after=&amp;limit= → fills oldest first + next_after (cursor).</summary>
        public static Task<PlatformResult<OmsFillList>> GetFillsAsync(string accountId, long after, int limit = 500)
            => SendAsync<OmsFillList>(HttpMethod.Get, "/v2/oms/fills?account_id=" + Esc(accountId)
                + "&after=" + Math.Max(0, after).ToString(CultureInfo.InvariantCulture)
                + "&limit=" + Math.Clamp(limit, 1, 500).ToString(CultureInfo.InvariantCulture), null, TradingTimeout);

        /// <summary>POST /v2/oms/autotrade {enabled} → {autotrade}; 409 halted when enabling while an account is halted.</summary>
        public static Task<PlatformResult<TradingAutoTradeResult>> SetAutoTradeAsync(bool enabled)
            => SendAsync<TradingAutoTradeResult>(HttpMethod.Post, "/v2/oms/autotrade",
                new Dictionary<string, object?> { ["enabled"] = enabled }, TradingActionTimeout);

        /// <summary>POST /v2/oms/resume {account_id, reason} → {account}; auto trading stays off, the peak is rebased; 503 when not valued.</summary>
        public static Task<PlatformResult<TradingAccountResult>> ResumeTradingAccountAsync(string accountId, string reason)
            => SendAsync<TradingAccountResult>(HttpMethod.Post, "/v2/oms/resume",
                WithNote(new Dictionary<string, object?> { ["account_id"] = accountId }, reason), TradingSubmitTimeout);

        /// <summary>GET /v2/oms/runtime → runtime status + the last 50 cycles (kept in memory by the backend).</summary>
        public static Task<PlatformResult<TradingRuntime>> GetRuntimeAsync()
            => SendAsync<TradingRuntime>(HttpMethod.Get, "/v2/oms/runtime", null, TradingTimeout);

        /// <summary>POST /v2/oms/runtime/run {} → 202 {queued}.</summary>
        public static Task<PlatformResult<JsonElement>> RunRuntimeCycleAsync()
            => SendAsync<JsonElement>(HttpMethod.Post, "/v2/oms/runtime/run", new Dictionary<string, object?>(), TradingActionTimeout);

        /// <summary>GET /v2/incubation[?strategy_id=] → runs (newest first) + length defaults {length_sessions, min, max}.</summary>
        public static Task<PlatformResult<IncubationList>> ListIncubationAsync(string? strategyId = null, DateTimeOffset? startedAfter = null)
        {
            // fix1023 round 3: started_after = ISO with offset (UTC "Z", whole seconds) → only runs created at or after it, newest first
            var q = new List<string>();
            if (!string.IsNullOrEmpty(strategyId)) q.Add("strategy_id=" + Esc(strategyId));
            if (startedAfter is DateTimeOffset t)
            {
                var utc = t.ToUniversalTime();
                utc = utc.AddTicks(-(utc.Ticks % TimeSpan.TicksPerSecond));
                q.Add("started_after=" + Uri.EscapeDataString(utc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", System.Globalization.CultureInfo.InvariantCulture)));
            }
            return SendAsync<IncubationList>(HttpMethod.Get, "/v2/incubation" + (q.Count == 0 ? "" : "?" + string.Join("&", q)), null, TradingTimeout);
        }

        /// <summary>GET /v2/incubation/eligibility?strategy_id= → {eligible, reason, calendar, defaults, account_id, start_date_if_now}.</summary>
        public static Task<PlatformResult<IncubationEligibility>> GetIncubationEligibilityAsync(string strategyId)
            => SendAsync<IncubationEligibility>(HttpMethod.Get, "/v2/incubation/eligibility?strategy_id=" + Esc(strategyId), null, TradingTimeout);

        /// <summary>
        /// POST /v2/incubation {strategy_id, length_sessions, reason} → 201 {run, strategy}. The length is immutable afterwards.
        /// Stage 8 (SPEC §18.2): account_id is NOT sent — the backend uses the strategy's route (default paper-main; Settings →
        /// Brokers → Routing). Sending paper-main for a strategy routed to a broker would be refused with 409 route_mismatch.
        /// </summary>
        public static Task<PlatformResult<IncubationStartResult>> StartIncubationAsync(string strategyId, int lengthSessions, string reason)
            => SendAsync<IncubationStartResult>(HttpMethod.Post, "/v2/incubation",
                WithNote(new Dictionary<string, object?>
                {
                    ["strategy_id"] = strategyId,
                    ["length_sessions"] = lengthSessions,
                }, reason), TradingSubmitTimeout);   // fix1023: note optional; a double start → 409 already_incubating; dlgguard: 150 s like an order

        /// <summary>GET /v2/incubation/{id} → {run, events (newest 300), decisions (newest 100)}.</summary>
        public static Task<PlatformResult<IncubationDetail>> GetIncubationAsync(string runId)
            => SendAsync<IncubationDetail>(HttpMethod.Get, "/v2/incubation/" + Esc(runId), null, TradingTimeout);

        /// <summary>POST /v2/incubation/{id}/abort {reason} → {run, strategy}; 409 not_running.</summary>
        public static Task<PlatformResult<IncubationStartResult>> AbortIncubationAsync(string runId, string reason)
            => SendAsync<IncubationStartResult>(HttpMethod.Post, "/v2/incubation/" + Esc(runId) + "/abort",
                WithNote(new Dictionary<string, object?>(), reason), TradingActionTimeout);

        /// <summary>
        /// The message of a failed call plus the backend's field errors (400 {error: "invalid input", errors: {field: msg}}),
        /// verbatim — the generic Fail() keeps only the `error` text.
        /// </summary>
        public static string DescribeFailure<T>(PlatformResult<T> result)
        {
            if (result.Ok) return "";
            var message = string.IsNullOrWhiteSpace(result.Message) ? result.Status.ToString() : result.Message;
            if (string.IsNullOrWhiteSpace(result.RawBody)) return message;
            var lines = new List<string>();
            try
            {
                using var doc = JsonDocument.Parse(result.RawBody);
                var root = doc.RootElement;
                if (root.ValueKind == JsonValueKind.Object)
                {
                    if (root.TryGetProperty("errors", out var errs) && errs.ValueKind == JsonValueKind.Object)
                        foreach (var p in errs.EnumerateObject())
                            lines.Add((p.Name == "_" ? "" : p.Name + ": ") + FundFormat.Plain(p.Value));
                    if (root.TryGetProperty("blockers", out var bl) && bl.ValueKind == JsonValueKind.Array)
                        foreach (var b in bl.EnumerateArray())
                            if (b.ValueKind == JsonValueKind.Object && b.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String)
                                lines.Add("Blocker: " + m.GetString());
                }
            }
            catch (JsonException) { /* non-JSON body: the status message is all there is */ }
            return lines.Count == 0 ? message : message + "\n" + string.Join("\n", lines);
        }

        /// <summary>The `code` of a failed call's JSON body ({error, code, …}), or null.</summary>
        public static string? ErrorCode<T>(PlatformResult<T> result)
        {
            if (result.Ok || string.IsNullOrWhiteSpace(result.RawBody)) return null;
            try
            {
                using var doc = JsonDocument.Parse(result.RawBody);
                return doc.RootElement.ValueKind == JsonValueKind.Object && doc.RootElement.TryGetProperty("code", out var c)
                       && c.ValueKind == JsonValueKind.String ? c.GetString() : null;
            }
            catch (JsonException) { return null; }
        }

        /// <summary>
        /// A failed POST /v2/portfolio/migration as a clear message (§17.14 M2): 409 migration_blocked (blockers listed with code
        /// and message; "migration_running" = a concurrent request), 503 migration_failed (nothing was moved, the old account is
        /// active again). The backend's own texts follow verbatim.
        /// </summary>
        public static string MigrationFailureText(PlatformResult<TradingMigrationResult> result)
        {
            var code = ErrorCode(result);
            var backend = string.IsNullOrWhiteSpace(result.Message) ? "" : result.Message;
            var blockers = new List<(string Code, string Message)>();
            if (!string.IsNullOrWhiteSpace(result.RawBody))
            {
                try
                {
                    using var doc = JsonDocument.Parse(result.RawBody);
                    if (doc.RootElement.ValueKind == JsonValueKind.Object && doc.RootElement.TryGetProperty("blockers", out var bl) && bl.ValueKind == JsonValueKind.Array)
                        foreach (var b in bl.EnumerateArray())
                            if (b.ValueKind == JsonValueKind.Object)
                                blockers.Add((b.TryGetProperty("code", out var c) && c.ValueKind == JsonValueKind.String ? c.GetString() ?? "?" : "?",
                                              b.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() ?? "" : ""));
                }
                catch (JsonException) { /* not JSON — the status message below */ }
            }
            switch (code)
            {
                case "migration_blocked":
                    var head = blockers.Any(b => b.Code == "migration_running")
                        ? "Refused: another migration request is already running. Nothing was changed by this request — wait a moment, then run a new dry run."
                        : "Refused — the migration is blocked. Nothing was moved; the old account stays with its current owner.";
                    return head + (blockers.Count > 0 ? "\n" + string.Join("\n", blockers.Select(b => "• " + b.Code + ": " + b.Message)) : "")
                           + (backend.Length > 0 ? "\nBackend: " + backend : "");
                case "migration_failed":
                    return "Migration FAILED — nothing was moved; the old paper account is active again (still traded by the old engine)."
                           + (backend.Length > 0 ? "\nBackend: " + backend : "") + "\nRun a new dry run before trying again.";
                default:
                    return DescribeFailure(result);
            }
        }

        /// <summary>The platform half of a POST /fund/kill response (§17.12: always present on a stage-7 backend), or null.</summary>
        /// <summary>
        /// POST /v2/oms/kill {reason?} → 200 always: {platform: {ok, error, accounts}, legacy: {ok, error, halted}} — the platform kill
        /// (every platform account, brokers included) AND the old engine's kill (control.kill_all; S10 review C1: the toolbar kill moved
        /// here from /fund/kill). 150 s: the broker half waits for every broker account to confirm flat.
        /// </summary>
        public static Task<PlatformResult<TradingKillAllResult>> KillAllAsync(string? reason)
            => SendAsync<TradingKillAllResult>(HttpMethod.Post, "/v2/oms/kill", WithNote(new Dictionary<string, object?>(), reason), TradingSubmitTimeout);

        public static TradingKillHalf? ReadKillHalf(JsonElement? platform)
        {
            if (platform is not JsonElement e || e.ValueKind != JsonValueKind.Object) return null;
            try { return e.Deserialize<TradingKillHalf>(Json); }
            catch (Exception ex) when (ex is JsonException || ex is NotSupportedException || ex is InvalidOperationException) { return null; }
        }
    }

    // =====================================================================================
    // Display helpers shared by the view and its dialogs (plain text only).
    // =====================================================================================

    internal static class TradingText
    {
        public static string OrderStateLabel(string? state) => state switch
        {
            "new" => "New (stored, not sent)",
            "submitted" => "Submitted",
            "accepted" => "Accepted (working)",
            "partially_filled" => "Partially filled",
            "filled" => "Filled",
            "cancelled" => "Cancelled",
            "rejected" => "Rejected",
            "expired" => "Expired",
            null or "" => "—",
            _ => state,
        };

        public static string[] OrderStates { get; } = new[] { "new", "submitted", "accepted", "partially_filled", "filled", "cancelled", "rejected", "expired" };
        public static string[] OrderOrigins { get; } = new[] { "runtime", "manual", "kill", "drawdown", "margin" };

        public static string OriginLabel(string? origin) => origin switch
        {
            "runtime" => "runtime (strategy)",
            "manual" => "manual (you)",
            "kill" => "kill switch",
            "drawdown" => "drawdown kill",
            "margin" => "broker margin call",
            null or "" => "—",
            _ => origin,
        };

        public static string AccountLabel(string? id) => id switch
        {
            PlatformClient.MainAccountId => "paper-main",
            PlatformClient.CouncilAccountId => "paper-council (migrated)",
            null or "" => "—",
            _ => id,
        };

        public static string Ts(string? iso) => FundFormat.LocalTime(iso, "yyyy-MM-dd HH:mm:ss");
        public static string TsShort(string? iso) => FundFormat.LocalTime(iso, "MM-dd HH:mm:ss");

        public static string Number(JsonElement? e)
        {
            if (e is not JsonElement v) return FundFormat.Dash;
            return v.ValueKind switch
            {
                JsonValueKind.Number => v.TryGetDouble(out var d) ? d.ToString("0.####", CultureInfo.InvariantCulture) : v.GetRawText(),
                JsonValueKind.String => v.GetString() ?? FundFormat.Dash,
                JsonValueKind.Null or JsonValueKind.Undefined => FundFormat.Dash,
                _ => FundFormat.Plain(v),
            };
        }

        public static double? Double(JsonElement? e)
        {
            if (e is not JsonElement v) return null;
            if (v.ValueKind == JsonValueKind.Number && v.TryGetDouble(out var d)) return d;
            if (v.ValueKind == JsonValueKind.String && double.TryParse(v.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var s)) return s;
            return null;
        }

        /// <summary>One line per order for a result popup: "BUY 10 SPY · filled @ 615.20" / "SELL SPY · rejected: reason (verbatim)".</summary>
        public static string OrderLine(OmsOrder o)
        {
            var side = (o.Side ?? "?").ToUpperInvariant();
            var qty = o.Qty.HasValue ? FundFormat.Qty(o.Qty) + " " : o.NotionalReq.HasValue ? FundFormat.Money(o.NotionalReq) + " of " : "";
            var text = side + " " + qty + (o.Symbol ?? "?") + " · " + OrderStateLabel(o.State);
            if (o.FilledQty is double f && f > 0) text += " · filled " + FundFormat.Qty(f) + (o.AvgFillPrice.HasValue ? " @ " + FundFormat.Num(o.AvgFillPrice, 2) : "");
            if (!string.IsNullOrWhiteSpace(o.RejectReason)) text += " · " + o.RejectReason;
            return text;
        }
    }

    // =====================================================================================
    // DTOs — field names exactly as in SPEC §17.11 / §17.12 (snake_case). Tolerant: unknown fields are ignored;
    // computed properties carry [JsonIgnore] (PropertyNameCaseInsensitive name collisions).
    // =====================================================================================

    internal sealed class TradingState
    {
        [JsonPropertyName("now")] public string? Now { get; set; }
        [JsonPropertyName("paper_only")] public bool? PaperOnly { get; set; }
        [JsonPropertyName("notice")] public string? Notice { get; set; }
        [JsonPropertyName("autotrade")] public TradingAutoTrade? AutoTrade { get; set; }
        [JsonPropertyName("runtime")] public TradingRuntime? Runtime { get; set; }
        [JsonPropertyName("legacy_account")] public TradingLegacyAccount? LegacyAccount { get; set; }
        [JsonPropertyName("accounts")] public List<TradingAccountSummary>? Accounts { get; set; }
        [JsonPropertyName("can")] public TradingCan? Can { get; set; }
    }

    internal sealed class TradingCan
    {
        [JsonPropertyName("open_account")] public bool? OpenAccount { get; set; }
        [JsonPropertyName("migrate")] public bool? Migrate { get; set; }
    }

    internal sealed class TradingAutoTrade
    {
        [JsonPropertyName("enabled")] public bool Enabled { get; set; }
        [JsonPropertyName("heartbeat_minutes")] public double? HeartbeatMinutes { get; set; }
        [JsonPropertyName("last_client_seen")] public string? LastClientSeen { get; set; }
        [JsonPropertyName("off_at")] public string? OffAt { get; set; }
        [JsonPropertyName("changed_at")] public string? ChangedAt { get; set; }
        [JsonPropertyName("changed_by")] public string? ChangedBy { get; set; }
    }

    internal sealed class TradingRuntime
    {
        [JsonPropertyName("running")] public bool? Running { get; set; }
        [JsonPropertyName("cycle_in_progress")] public bool? CycleInProgress { get; set; }
        [JsonPropertyName("last_cycle_at")] public string? LastCycleAt { get; set; }
        [JsonPropertyName("next_cycle_at")] public string? NextCycleAt { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
        // only GET /v2/oms/runtime carries the cycles (newest first, last 50, in memory)
        [JsonPropertyName("cycles")] public List<TradingRuntimeCycle>? Cycles { get; set; }
    }

    internal sealed class TradingRuntimeCycle
    {
        [JsonPropertyName("started_at")] public string? StartedAt { get; set; }
        [JsonPropertyName("finished_at")] public string? FinishedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("summary")] public JsonElement? Summary { get; set; }
    }

    internal sealed class TradingLegacyAccount
    {
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("owner")] public string? Owner { get; set; }
        [JsonPropertyName("migrated_at")] public string? MigratedAt { get; set; }
        [JsonPropertyName("migration_id")] public JsonElement? MigrationIdRaw { get; set; }

        [JsonIgnore] public string MigrationIdText => StrategyJson.Scalar(MigrationIdRaw);
    }

    internal sealed class TradingAccountResult
    {
        [JsonPropertyName("account")] public TradingAccountSummary? Account { get; set; }
    }

    internal sealed class TradingAccountSummary
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("broker")] public string? Broker { get; set; }
        [JsonPropertyName("paper")] public bool? Paper { get; set; }
        [JsonPropertyName("origin")] public string? Origin { get; set; }
        [JsonPropertyName("opened_at")] public string? OpenedAt { get; set; }
        [JsonPropertyName("account")] public TradingAccountView? Account { get; set; }
        [JsonPropertyName("valuation")] public TradingValuation? Valuation { get; set; }
        [JsonPropertyName("halt")] public TradingHalt? Halt { get; set; }
        [JsonPropertyName("day_loss")] public TradingDayLoss? DayLoss { get; set; }
        [JsonPropertyName("drawdown")] public TradingDrawdown? Drawdown { get; set; }
        [JsonPropertyName("positions_count")] public int? PositionsCount { get; set; }
        [JsonPropertyName("working_orders")] public int? WorkingOrders { get; set; }
        [JsonPropertyName("sources")] public List<string>? Sources { get; set; }
        // stage 8 (SPEC §18.10): broker accounts only — {state, message} / {status, at, reason}; null on internal accounts
        [JsonPropertyName("broker_link")] public BrokerLink? BrokerLink { get; set; }
        [JsonPropertyName("reconciliation")] public BrokerReconcileState? Reconciliation { get; set; }

        [JsonIgnore] public bool Halted => Halt?.Active == true;
        [JsonIgnore] public bool IsBrokerAccount => PlatformClient.IsBrokerId(Id);
    }

    /// <summary>The old /fund/state `account` keys (PaperBroker.account).</summary>
    internal sealed class TradingAccountView
    {
        [JsonPropertyName("equity")] public double? Equity { get; set; }
        [JsonPropertyName("cash")] public double? Cash { get; set; }
        [JsonPropertyName("long_mv")] public double? LongMv { get; set; }
        [JsonPropertyName("short_mv")] public double? ShortMv { get; set; }
        [JsonPropertyName("buying_power")] public double? BuyingPower { get; set; }
        [JsonPropertyName("peak_equity")] public double? PeakEquity { get; set; }
        [JsonPropertyName("drawdown_pct")] public double? DrawdownPct { get; set; }
        [JsonPropertyName("maintenance_required")] public double? MaintenanceRequired { get; set; }
        [JsonPropertyName("margin_excess")] public double? MarginExcess { get; set; }
        [JsonPropertyName("short_exposure_pct")] public double? ShortExposurePct { get; set; }
        [JsonPropertyName("starting_capital")] public double? StartingCapital { get; set; }
        [JsonPropertyName("reset_at")] public string? ResetAt { get; set; }
        [JsonPropertyName("day_pnl")] public double? DayPnl { get; set; }
        [JsonPropertyName("total_pnl")] public double? TotalPnl { get; set; }
        [JsonPropertyName("total_pnl_pct")] public double? TotalPnlPct { get; set; }
        [JsonPropertyName("funding_total")] public double? FundingTotal { get; set; }
        [JsonPropertyName("crypto_margin_excess")] public double? CryptoMarginExcess { get; set; }

        /// <summary>Gross exposure in % of equity ((long + short market value) / equity), null when not valued.</summary>
        [JsonIgnore]
        public double? GrossPct => Equity is double e && e > 0 && LongMv is double l && ShortMv is double s ? (l + Math.Abs(s)) / e * 100.0 : null;

        /// <summary>Net exposure in % of equity ((long − short market value) / equity).</summary>
        [JsonIgnore]
        public double? NetPct => Equity is double e && e > 0 && LongMv is double l && ShortMv is double s ? (l - Math.Abs(s)) / e * 100.0 : null;
    }

    internal sealed class TradingValuation
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class TradingHalt
    {
        [JsonPropertyName("active")] public bool Active { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("at")] public string? At { get; set; }
    }

    internal sealed class TradingDayLoss
    {
        [JsonPropertyName("equity")] public TradingDayLossClass? Equity { get; set; }
        [JsonPropertyName("crypto")] public TradingDayLossClass? Crypto { get; set; }
    }

    internal sealed class TradingDayLossClass
    {
        [JsonPropertyName("pnl_pct")] public double? PnlPct { get; set; }
        [JsonPropertyName("reference_equity")] public double? ReferenceEquity { get; set; }
        [JsonPropertyName("limit_pct")] public double? LimitPct { get; set; }
        [JsonPropertyName("blocked")] public bool Blocked { get; set; }
        [JsonPropertyName("until")] public string? Until { get; set; }
        // review fix M1 (§17.14): the block was recorded today and holds until `until`, even if the price recovers
        [JsonPropertyName("latched")] public bool? Latched { get; set; }
    }

    internal sealed class TradingDrawdown
    {
        [JsonPropertyName("pct")] public double? Pct { get; set; }
        [JsonPropertyName("kill_pct")] public double? KillPct { get; set; }
    }

    internal sealed class TradingAccountDetail
    {
        [JsonPropertyName("account")] public TradingAccountSummary? Account { get; set; }
        [JsonPropertyName("positions")] public List<TradingPosition>? Positions { get; set; }
        [JsonPropertyName("working_orders")] public List<OmsOrder>? WorkingOrders { get; set; }
        [JsonPropertyName("sources")] public List<TradingSource>? Sources { get; set; }
        [JsonPropertyName("settings")] public TradingSettings? Settings { get; set; }
    }

    internal sealed class TradingPosition
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("instrument")] public string? Instrument { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("avg_price")] public double? AvgPrice { get; set; }
        [JsonPropertyName("last_price")] public double? LastPrice { get; set; }
        [JsonPropertyName("market_value")] public double? MarketValue { get; set; }
        [JsonPropertyName("weight")] public double? Weight { get; set; }
        [JsonPropertyName("unrealized_pnl")] public double? UnrealizedPnl { get; set; }
        [JsonPropertyName("realized_pnl")] public double? RealizedPnl { get; set; }
        [JsonPropertyName("opened_at")] public string? OpenedAt { get; set; }
        [JsonPropertyName("borrow_rate_pct")] public double? BorrowRatePct { get; set; }
        [JsonPropertyName("by_source")] public List<TradingSourceQty>? BySource { get; set; }
    }

    internal sealed class TradingSourceQty
    {
        [JsonPropertyName("source_id")] public string? SourceId { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
    }

    internal sealed class TradingSource
    {
        [JsonPropertyName("source_id")] public string? SourceId { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("incubation_id")] public string? IncubationId { get; set; }
        [JsonPropertyName("allocation_weight")] public double? AllocationWeight { get; set; }
        [JsonPropertyName("capital_in")] public double? CapitalIn { get; set; }
        [JsonPropertyName("cash")] public double? Cash { get; set; }
        [JsonPropertyName("equity")] public double? Equity { get; set; }
        [JsonPropertyName("valued")] public bool? Valued { get; set; }
        [JsonPropertyName("positions")] public List<TradingSourcePosition>? Positions { get; set; }
        [JsonPropertyName("pnl")] public TradingPnl? Pnl { get; set; }
        [JsonPropertyName("costs")] public TradingCosts? Costs { get; set; }
        [JsonPropertyName("exposure")] public TradingExposure? Exposure { get; set; }
    }

    internal sealed class TradingSourcePosition
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("avg_price")] public double? AvgPrice { get; set; }
        [JsonPropertyName("last_price")] public double? LastPrice { get; set; }
        [JsonPropertyName("market_value")] public double? MarketValue { get; set; }
        [JsonPropertyName("unrealized_pnl")] public double? UnrealizedPnl { get; set; }
    }

    internal sealed class TradingPnl
    {
        [JsonPropertyName("realized")] public double? Realized { get; set; }
        [JsonPropertyName("unrealized")] public double? Unrealized { get; set; }
        [JsonPropertyName("total")] public double? Total { get; set; }
    }

    internal sealed class TradingCosts
    {
        [JsonPropertyName("fees")] public double? Fees { get; set; }
        [JsonPropertyName("slippage")] public double? Slippage { get; set; }
        [JsonPropertyName("borrow")] public double? Borrow { get; set; }
        [JsonPropertyName("funding")] public double? Funding { get; set; }
        [JsonPropertyName("dividends")] public double? Dividends { get; set; }
        [JsonPropertyName("liquidation")] public double? Liquidation { get; set; }
    }

    internal sealed class TradingExposure
    {
        [JsonPropertyName("long_mv")] public double? LongMv { get; set; }
        [JsonPropertyName("short_mv")] public double? ShortMv { get; set; }
        [JsonPropertyName("gross_pct")] public double? GrossPct { get; set; }
        [JsonPropertyName("net_pct")] public double? NetPct { get; set; }
    }

    internal sealed class TradingLedger
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("source_id")] public string? SourceId { get; set; }
        [JsonPropertyName("entries")] public List<TradingLedgerEntry>? Entries { get; set; }
    }

    internal sealed class TradingLedgerEntry
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("amount")] public double? Amount { get; set; }
        [JsonPropertyName("ref")] public string? Ref { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class TradingAllocationPayload
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("allocation")] public TradingAllocation? Allocation { get; set; }
        [JsonPropertyName("params")] public JsonElement? Params { get; set; }
        [JsonPropertyName("next_rebalance")] public string? NextRebalance { get; set; }
    }

    internal sealed class TradingAllocation
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("actor")] public string? Actor { get; set; }
        [JsonPropertyName("pool")] public double? Pool { get; set; }
        [JsonPropertyName("unallocated_weight")] public double? UnallocatedWeight { get; set; }
        [JsonPropertyName("weights")] public List<TradingAllocationWeight>? Weights { get; set; }
        [JsonPropertyName("transfers")] public List<TradingTransfer>? Transfers { get; set; }
        // review fix L2 (§17.14): "ok" | "no_allocation" (+ reason) — a no-allocation result moved NO capital
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("status_reason")] public string? StatusReason { get; set; }

        [JsonIgnore] public string IdText => StrategyJson.Scalar(IdRaw);
        [JsonIgnore] public bool IsNoAllocation => Status == "no_allocation";
    }

    internal sealed class TradingAllocationWeight
    {
        [JsonPropertyName("source_id")] public string? SourceId { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("weight")] public double? Weight { get; set; }
        [JsonPropertyName("target_capital")] public double? TargetCapital { get; set; }
        [JsonPropertyName("vol")] public double? Vol { get; set; }
        [JsonPropertyName("vol_source")] public string? VolSource { get; set; }
        [JsonPropertyName("mean_corr")] public double? MeanCorr { get; set; }
        [JsonPropertyName("penalty")] public double? Penalty { get; set; }
        [JsonPropertyName("capped")] public bool? Capped { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class TradingTransfer
    {
        [JsonPropertyName("source_id")] public string? SourceId { get; set; }
        [JsonPropertyName("amount")] public double? Amount { get; set; }
    }

    internal sealed class TradingSettings
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("updated_by")] public string? UpdatedBy { get; set; }
        // numbers per key (kept raw: whole numbers for the count/seconds keys, fractions for the allocator)
        [JsonPropertyName("settings")] public Dictionary<string, JsonElement>? Values { get; set; }
        [JsonPropertyName("defaults")] public Dictionary<string, JsonElement>? Defaults { get; set; }
        [JsonPropertyName("bounds")] public Dictionary<string, TradingSettingBound>? Bounds { get; set; }
    }

    internal sealed class TradingSettingBound
    {
        [JsonPropertyName("min")] public double? Min { get; set; }
        [JsonPropertyName("max")] public double? Max { get; set; }
        [JsonPropertyName("step")] public double? Step { get; set; }
        [JsonPropertyName("unit")] public string? Unit { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("group")] public string? Group { get; set; }
    }

    internal sealed class TradingMigrationStatus
    {
        [JsonPropertyName("legacy_account")] public TradingLegacyAccount? LegacyAccount { get; set; }
        [JsonPropertyName("migration")] public TradingMigrationRecord? Migration { get; set; }
        [JsonPropertyName("can_migrate")] public bool? CanMigrate { get; set; }
    }

    internal sealed class TradingMigrationRecord
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("summary")] public JsonElement? Summary { get; set; }

        [JsonIgnore] public string IdText => StrategyJson.Scalar(IdRaw);
    }

    internal sealed class TradingDryRun
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("blockers")] public List<TradingBlocker>? Blockers { get; set; }
        [JsonPropertyName("legacy")] public TradingMigrationLegacy? Legacy { get; set; }
        [JsonPropertyName("target_account_id")] public string? TargetAccountId { get; set; }
        [JsonPropertyName("source_id")] public string? SourceId { get; set; }
        [JsonPropertyName("settings")] public JsonElement? Settings { get; set; }
        [JsonPropertyName("broker_params")] public JsonElement? BrokerParams { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class TradingBlocker
    {
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class TradingMigrationLegacy
    {
        [JsonPropertyName("orders")] public int? Orders { get; set; }
        [JsonPropertyName("ledger_rows")] public int? LedgerRows { get; set; }
        [JsonPropertyName("equity_points")] public int? EquityPoints { get; set; }
        [JsonPropertyName("cash")] public double? Cash { get; set; }
        [JsonPropertyName("starting_capital")] public double? StartingCapital { get; set; }
        [JsonPropertyName("peak_equity")] public double? PeakEquity { get; set; }
        [JsonPropertyName("reset_at")] public string? ResetAt { get; set; }
        [JsonPropertyName("halted")] public bool? Halted { get; set; }
        [JsonPropertyName("autopilot")] public bool? Autopilot { get; set; }
        [JsonPropertyName("pending_orders")] public int? PendingOrders { get; set; }
        [JsonPropertyName("positions")] public List<TradingMigrationPosition>? Positions { get; set; }
    }

    internal sealed class TradingMigrationPosition
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("avg_price")] public double? AvgPrice { get; set; }
    }

    internal sealed class TradingMigrationResult
    {
        [JsonPropertyName("migration")] public TradingMigrationRecord? Migration { get; set; }
        [JsonPropertyName("account")] public TradingAccountSummary? Account { get; set; }
    }

    internal sealed class OmsOrder
    {
        [JsonPropertyName("client_order_id")] public string? ClientOrderId { get; set; }
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("notional_req")] public double? NotionalReq { get; set; }
        [JsonPropertyName("filled_qty")] public double? FilledQty { get; set; }
        [JsonPropertyName("avg_fill_price")] public double? AvgFillPrice { get; set; }
        [JsonPropertyName("fees")] public double? Fees { get; set; }
        [JsonPropertyName("notional")] public double? Notional { get; set; }
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("terminal")] public bool? Terminal { get; set; }
        [JsonPropertyName("origin")] public string? Origin { get; set; }
        [JsonPropertyName("tif")] public string? Tif { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("ref_price")] public double? RefPrice { get; set; }
        [JsonPropertyName("broker_order_id")] public JsonElement? BrokerOrderIdRaw { get; set; }
        [JsonPropertyName("reject_reason")] public string? RejectReason { get; set; }
        [JsonPropertyName("attempts")] public int? Attempts { get; set; }
        [JsonPropertyName("allow_reduce")] public bool? AllowReduce { get; set; }
        [JsonPropertyName("allocations")] public List<TradingSourceQty>? Allocations { get; set; }
        [JsonPropertyName("checks")] public List<OmsCheck>? Checks { get; set; }
        [JsonPropertyName("execution_id")] public string? ExecutionId { get; set; }

        [JsonIgnore] public string BrokerOrderIdText => StrategyJson.Scalar(BrokerOrderIdRaw);
        [JsonIgnore] public bool IsTerminal => Terminal == true || State is "filled" or "cancelled" or "rejected" or "expired";
        [JsonIgnore] public bool HasFailedCheck => Checks != null && Checks.Any(c => c.Ok == false);
    }

    internal sealed class OmsCheck
    {
        [JsonPropertyName("check")] public string? Check { get; set; }
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
    }

    internal sealed class OmsOrderList
    {
        [JsonPropertyName("orders")] public List<OmsOrder>? Orders { get; set; }
    }

    internal sealed class OmsOrderResult
    {
        [JsonPropertyName("order")] public OmsOrder? Order { get; set; }
    }

    internal sealed class OmsOrderDetail
    {
        [JsonPropertyName("order")] public OmsOrder? Order { get; set; }
        [JsonPropertyName("events")] public List<OmsOrderEvent>? Events { get; set; }
    }

    internal sealed class OmsOrderEvent
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("from_state")] public string? FromState { get; set; }
        [JsonPropertyName("to_state")] public string? ToState { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("data")] public JsonElement? Data { get; set; }
    }

    internal sealed class OmsFillList
    {
        [JsonPropertyName("fills")] public List<OmsFill>? Fills { get; set; }
        [JsonPropertyName("next_after")] public long? NextAfter { get; set; }
    }

    internal sealed class OmsFill
    {
        [JsonPropertyName("id")] public long Id { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("client_order_id")] public string? ClientOrderId { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("fees")] public double? Fees { get; set; }
        [JsonPropertyName("notional")] public double? Notional { get; set; }
    }

    internal sealed class TradingAutoTradeResult
    {
        [JsonPropertyName("autotrade")] public TradingAutoTrade? AutoTrade { get; set; }
    }

    /// <summary>The platform half of a kill: {ok, error, accounts: [{account_id, cancelled, orders, error?}]}.</summary>
    /// <summary>POST /v2/oms/kill: each half reports its own outcome.</summary>
    internal sealed class TradingKillAllResult
    {
        [JsonPropertyName("platform")] public JsonElement? Platform { get; set; }
        [JsonPropertyName("legacy")] public TradingKillLegacy? Legacy { get; set; }
        /// <summary>S10-C (SPEC §22.8): {applicable: false} before the switch-over; after it the forced-off Council switch.</summary>
        [JsonPropertyName("council")] public TradingKillCouncil? Council { get; set; }
    }

    /// <summary>
    /// The old engine's half of /v2/oms/kill (SPEC §22.8): before the switch-over {ok, error, halted, orders} (its closing orders passed
    /// through); after it {ok: true, applicable: false, note: "not applicable — switched over to the Council"} (never a failure).
    /// </summary>
    internal sealed class TradingKillLegacy
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("halted")] public bool? Halted { get; set; }
        [JsonPropertyName("applicable")] public bool? Applicable { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("orders")] public List<JsonElement>? Orders { get; set; }
    }

    internal sealed class TradingKillHalf
    {
        [JsonPropertyName("ok")] public bool Ok { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("accounts")] public List<TradingKillAccount>? Accounts { get; set; }
    }

    internal sealed class TradingKillAccount
    {
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("cancelled")] public int? Cancelled { get; set; }
        // OmsOrders, or {symbol, state: "error", reject_reason} when a closing order could not be placed (§17.12)
        [JsonPropertyName("orders")] public List<OmsOrder>? Orders { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // stage 8 (SPEC §18.6): broker accounts add the broker half {ok, error, cancelled_at_broker, note}
        [JsonPropertyName("broker")] public TradingKillBroker? Broker { get; set; }
    }

    internal sealed class TradingKillBroker
    {
        [JsonPropertyName("ok")] public bool? Ok { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("cancelled_at_broker")] public int? CancelledAtBroker { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        // §18.13 verified kill
        [JsonPropertyName("flat")] public bool? Flat { get; set; }
        [JsonPropertyName("pending")] public bool? Pending { get; set; }
        [JsonPropertyName("not_flat")] public List<BrokerKillNotFlat>? NotFlat { get; set; }
        /// <summary>§18.13 review fix — shown when the backend sends them (not a failure, does not change "flat").</summary>
        [JsonPropertyName("untouched_orders")] public int? UntouchedOrders { get; set; }
        [JsonPropertyName("untouched_note")] public string? UntouchedNote { get; set; }
    }

    internal sealed class IncubationList
    {
        [JsonPropertyName("runs")] public List<IncubationRun>? Runs { get; set; }
        [JsonPropertyName("defaults")] public IncubationDefaults? Defaults { get; set; }
    }

    internal sealed class IncubationDefaults
    {
        [JsonPropertyName("length_sessions")] public int? LengthSessions { get; set; }
        [JsonPropertyName("min")] public int? Min { get; set; }
        [JsonPropertyName("max")] public int? Max { get; set; }
    }

    internal sealed class IncubationRun
    {
        // Sandbox fixture label (strategy_platform/SPEC.md "Sandbox profile" S4): true + the note only on the synthetic
        // click-test fixture; absent everywhere else. Shown in red wherever this item is shown.
        [JsonPropertyName("fixture")] public bool? Fixture { get; set; }
        [JsonPropertyName("fixture_note")] public string? FixtureNote { get; set; }
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("strategy_name")] public string? StrategyName { get; set; }
        [JsonPropertyName("version")] public int? Version { get; set; }
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("source_id")] public string? SourceId { get; set; }
        [JsonPropertyName("length_sessions")] public int? LengthSessions { get; set; }
        [JsonPropertyName("calendar")] public string? Calendar { get; set; }
        [JsonPropertyName("start_date")] public string? StartDate { get; set; }
        [JsonPropertyName("end_date")] public string? EndDate { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("created_by")] public string? CreatedBy { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("progress")] public IncubationProgress? Progress { get; set; }
        [JsonPropertyName("realised")] public IncubationRealised? Realised { get; set; }
        [JsonPropertyName("expected")] public IncubationExpected? Expected { get; set; }
        [JsonPropertyName("band")] public IncubationBand? Band { get; set; }
        [JsonPropertyName("tracking_error")] public double? TrackingError { get; set; }
        [JsonPropertyName("result")] public string? Result { get; set; }
        [JsonPropertyName("result_reasons")] public List<JsonElement>? ResultReasons { get; set; }
        [JsonPropertyName("live_eligible")] public bool? LiveEligible { get; set; }
        [JsonPropertyName("ended_at")] public string? EndedAt { get; set; }
        [JsonPropertyName("end_reason")] public string? EndReason { get; set; }

        [JsonIgnore] public bool IsRunning => Status == "running";
    }

    internal sealed class IncubationProgress
    {
        [JsonPropertyName("sessions_elapsed")] public int? SessionsElapsed { get; set; }
        [JsonPropertyName("sessions_total")] public int? SessionsTotal { get; set; }
        [JsonPropertyName("traded")] public int? Traded { get; set; }
        [JsonPropertyName("missed")] public int? Missed { get; set; }
        [JsonPropertyName("last_session")] public string? LastSession { get; set; }
    }

    internal sealed class IncubationRealised
    {
        [JsonPropertyName("return")] public double? Return { get; set; }
        [JsonPropertyName("log_return")] public double? LogReturn { get; set; }
        [JsonPropertyName("vol")] public double? Vol { get; set; }
        [JsonPropertyName("sharpe")] public double? Sharpe { get; set; }
        [JsonPropertyName("max_drawdown")] public double? MaxDrawdown { get; set; }
        [JsonPropertyName("equity")] public double? Equity { get; set; }
    }

    internal sealed class IncubationExpected
    {
        [JsonPropertyName("mean_daily")] public double? MeanDaily { get; set; }
        [JsonPropertyName("std_daily")] public double? StdDaily { get; set; }
        [JsonPropertyName("vol")] public double? Vol { get; set; }
        [JsonPropertyName("max_drawdown")] public double? MaxDrawdown { get; set; }
        [JsonPropertyName("source_run_id")] public string? SourceRunId { get; set; }
        [JsonPropertyName("sessions")] public int? Sessions { get; set; }
    }

    internal sealed class IncubationBand
    {
        [JsonPropertyName("lo")] public double? Lo { get; set; }
        [JsonPropertyName("hi")] public double? Hi { get; set; }
        [JsonPropertyName("n")] public int? N { get; set; }
    }

    internal sealed class IncubationEligibility
    {
        [JsonPropertyName("eligible")] public bool Eligible { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("calendar")] public string? Calendar { get; set; }
        [JsonPropertyName("defaults")] public IncubationDefaults? Defaults { get; set; }
        [JsonPropertyName("account_id")] public string? AccountId { get; set; }
        [JsonPropertyName("start_date_if_now")] public string? StartDateIfNow { get; set; }
    }

    internal sealed class IncubationStartResult
    {
        [JsonPropertyName("run")] public IncubationRun? Run { get; set; }
        [JsonPropertyName("strategy")] public JsonElement? Strategy { get; set; }
        /// <summary>fix1023 round 3: {status: done | failed (+ error) | pending (+ note — continues in the background)}.</summary>
        [JsonPropertyName("allocation")] public IncubationAllocation? Allocation { get; set; }
    }

    internal sealed class IncubationAllocation
    {
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class IncubationDetail
    {
        [JsonPropertyName("run")] public IncubationRun? Run { get; set; }
        [JsonPropertyName("events")] public List<IncubationEvent>? Events { get; set; }
        [JsonPropertyName("decisions")] public List<IncubationDecision>? Decisions { get; set; }
        /// <summary>fix1023 round 2: the run's decay decisions (paused / keep_running / demoted), oldest first — also for ended runs.</summary>
        [JsonPropertyName("decay_events")] public List<IncubationDecayEvent>? DecayEvents { get; set; }
    }

    internal sealed class IncubationDecayEvent
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("action")] public string? Action { get; set; }   // paused | keep_running | demoted
        [JsonPropertyName("actor")] public string? Actor { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("rule")] public string? Rule { get; set; }
        [JsonPropertyName("check_day")] public string? CheckDay { get; set; }
        [JsonPropertyName("synthetic")] public bool? Synthetic { get; set; }
    }

    internal sealed class IncubationEvent
    {
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("event")] public string? Event { get; set; }
        [JsonPropertyName("data")] public JsonElement? Data { get; set; }
    }

    internal sealed class IncubationDecision
    {
        // null for the in-memory latest failed attempt (§17.12) — never persisted
        [JsonPropertyName("id")] public JsonElement? IdRaw { get; set; }
        [JsonPropertyName("bar_date")] public string? BarDate { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("status")] public string? Status { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("targets")] public Dictionary<string, double?>? Targets { get; set; }
        [JsonPropertyName("force")] public List<string>? Force { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        // fix1023 round 3: a decision still pending when the run was aborted → status "cancelled" (never executed)
        [JsonPropertyName("cancelled_at")] public string? CancelledAt { get; set; }
        [JsonPropertyName("cancelled_note")] public string? CancelledNote { get; set; }

        [JsonIgnore] public bool IsTransient => IdRaw is not JsonElement e || e.ValueKind == JsonValueKind.Null || e.ValueKind == JsonValueKind.Undefined;
    }
}

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using UFOS.ai.Shared;

namespace UFOS.ai.Services
{
    /// <summary>State of the MainWindow "Portfolio Insight" card (Strategy Platform stage 9b, HedgeFund SPEC §20.3).</summary>
    public enum PortfolioInsightState { Ok, Incomplete, NoImports, BackendNotRunning, Unavailable }

    /// <summary>
    /// What the card shows. <see cref="Lines"/> = realised P&amp;L of the current year, one line per currency (at most
    /// <see cref="HedgeFundPortfolioInsightProvider.MaxLines"/>; <see cref="MoreLines"/> = the currencies not shown); <see cref="Detail"/>
    /// = the payload's `detail` (unrealised P&amp;L with coverage, verbatim); <see cref="Label"/> = the payload's fixed label
    /// (verbatim, never a second copy); <see cref="Message"/> = the reason for the not-running / unavailable states. No numbers
    /// are ever invented: every figure comes from GET /v2/insights/summary. Not AI output (no AI badge).
    /// </summary>
    /// <remarks>
    /// Structured fields (review 2026-10-19 — the card renders from these, never from the display strings): filled straight from
    /// the summary JSON. <c>Realised</c> = currencies[].realised_net (non-zero only, like the headline); <c>Unrealised</c>
    /// = currencies with positions: unrealised + coverage_pct, or NothingValued when no position is valued; <c>UnpricedCount</c>
    /// = the backend's "unpriced" field; <c>DepotCount</c> =
    /// accounts; <c>PricesAsOf</c> = as_of (valuation / market close); <c>ComputedAt</c> = generated_at (local time);
    /// <c>Disclaimer</c> = label (verbatim). Null = not reported. <c>Sandbox</c> = the backend's public GET /health JSON says
    /// <c>"mode": "sandbox"</c> (HedgeFund strategy_platform/SPEC.md "Sandbox profile" S2) — the card shows a small "SANDBOX" chip.
    /// </remarks>
    public sealed record PortfolioInsightCard(
        PortfolioInsightState State,
        IReadOnlyList<string> Lines,
        int MoreLines,
        string? Detail,
        string? Label,
        string? AsOf,
        string? Message,
        IReadOnlyList<string> Reasons,
        int? Year = null,
        IReadOnlyList<PortfolioRealised>? Realised = null,
        IReadOnlyList<PortfolioUnrealised>? Unrealised = null,
        int? UnpricedCount = null,
        int? DepotCount = null,
        string? PricesAsOf = null,
        string? ComputedAt = null,
        string? Disclaimer = null,
        bool Sandbox = false);

    /// <summary>Realised P&amp;L of the year in one currency (never summed across currencies).</summary>
    public sealed record PortfolioRealised(double Amount, string Currency);

    /// <summary>Unrealised P&amp;L of one currency: Amount + CoveragePct when valued, else NothingValued (no number shown).</summary>
    public sealed record PortfolioUnrealised(string Currency, double? Amount, int? CoveragePct, bool NothingValued);

    // Extension seam for the "Portfolio Insight" card. Since stage 9b (2026-10-18) the real provider is the HedgeFund
    // backend's portfolio insights (GET /v2/insights/summary — ONLY the user's imported broker statements, the Shadow
    // Account; per currency, no FX conversion; a rough guide, not tax data).
    public interface IPortfolioInsightProvider
    {
        Task<PortfolioInsightCard> GetCardAsync(CancellationToken ct = default);
    }

    /// <summary>
    /// Reaches the HedgeFund backend exactly like the AI-budget window (Shared/AiBudgetRegistry.cs, provider "hedgefund"):
    /// BackendAuth.FindBackendDir("HedgeFund", "backend"); GET /health WITHOUT a token (2 s) — fails → "not running";
    /// BackendAuth.ReadToken on EVERY call (no token file → "not running"); then GET /v2/insights/summary with the token in
    /// X-UFOS-Token (10 s); a 401 re-reads the token once (the backend writes a new one at each start). Fixed base URL
    /// http://127.0.0.1:8867, no proxy, no redirects, no cookies. Nothing is cached, nothing is logged except failures (never
    /// the token). The summary GET does not touch the trading heartbeat (§20.2).
    /// </summary>
    public sealed class HedgeFundPortfolioInsightProvider : IPortfolioInsightProvider
    {
        public const int MaxLines = 3;
        private const int Port = 8867;
        private const int MaxPayloadBytes = 256 * 1024;
        private static readonly TimeSpan HealthTimeout = TimeSpan.FromSeconds(2);
        private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(10);
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        public const string NotRunningText = "Hedge Fund backend not running — click to start";   // §20.8: a click on the card starts it
        public const string NoImportsText = "No broker statements imported yet — Hedge Fund → Shadow Account";

        private static readonly HttpClient Http = new(new SocketsHttpHandler
        {
            UseProxy = false,
            AllowAutoRedirect = false,
            UseCookies = false,
        })
        {
            Timeout = Timeout.InfiniteTimeSpan,   // per-request timeouts via linked CancellationTokenSources
        };

        private static Uri BuildUri(string path) => new UriBuilder(Uri.UriSchemeHttp, "127.0.0.1", Port, path).Uri;

        public async Task<PortfolioInsightCard> GetCardAsync(CancellationToken ct = default)
        {
            var health = new HealthProbe();
            var card = await GetCardCoreAsync(health, ct);
            return health.Sandbox && card.State != PortfolioInsightState.BackendNotRunning ? card with { Sandbox = true } : card;
        }

        /// <summary>What the /health answer said besides its status code (sandbox profile, 2026-10-23).</summary>
        private sealed class HealthProbe
        {
            public bool Sandbox;
        }

        private async Task<PortfolioInsightCard> GetCardCoreAsync(HealthProbe health, CancellationToken ct)
        {
            var dir = BackendAuth.FindBackendDir("HedgeFund", "backend");
            if (dir == null) return Card(PortfolioInsightState.BackendNotRunning, NotRunningText);
            if (!await IsHealthyAsync(health, ct)) return Card(PortfolioInsightState.BackendNotRunning, NotRunningText);
            var token = BackendAuth.ReadToken(dir);
            if (token == null) return Card(PortfolioInsightState.BackendNotRunning, NotRunningText);

            (HttpStatusCode Status, string? Body) r;
            try
            {
                r = await GetSummaryAsync(token, ct);
                if (r.Status == HttpStatusCode.Unauthorized)
                {
                    var fresh = BackendAuth.ReadToken(dir);   // the backend restarted meanwhile (new token)
                    if (fresh == null) return Card(PortfolioInsightState.BackendNotRunning, NotRunningText);
                    r = await GetSummaryAsync(fresh, ct);
                }
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                return Card(PortfolioInsightState.Unavailable, "not available (the Hedge Fund backend did not answer within " + RequestTimeout.TotalSeconds.ToString("0", Inv) + " s)");
            }
            catch (HttpRequestException)
            {
                // /health answered a moment ago, the request could not connect: it just stopped.
                return Card(PortfolioInsightState.BackendNotRunning, NotRunningText);
            }

            if (r.Body == null) return Card(PortfolioInsightState.Unavailable, "not available (the answer was larger than " + (MaxPayloadBytes / 1024).ToString(Inv) + " KB)");
            if ((int)r.Status < 200 || (int)r.Status > 299)
            {
                var err = ErrorField(r.Body);
                var reason = r.Status switch
                {
                    HttpStatusCode.Unauthorized => "the backend's session token did not match — restart Hedge Fund",
                    HttpStatusCode.NotFound when err == null => "the running Hedge Fund backend has no insights route — restart it from the Hedge Fund window",
                    _ => "HTTP " + ((int)r.Status).ToString(Inv) + (err != null ? ": " + err : ""),
                };
                return Card(PortfolioInsightState.Unavailable, "not available (" + reason + ")");
            }
            try
            {
                using var doc = JsonDocument.Parse(r.Body);
                return FromPayload(doc.RootElement);
            }
            catch (JsonException)
            {
                return Card(PortfolioInsightState.Unavailable, "not available (unexpected answer from the Hedge Fund backend)");
            }
        }

        private static PortfolioInsightCard Card(PortfolioInsightState state, string message)
            => new(state, Array.Empty<string>(), 0, null, null, null, message, Array.Empty<string>());

        /// <summary>
        /// GET /health: the status code alone decides "running". Since the sandbox profile (2026-10-23) the body is JSON
        /// <c>{"status": "ok", "mode": "normal" | "sandbox", …}</c>; its <c>mode</c> only sets <see cref="HealthProbe.Sandbox"/> — an
        /// older plain-text body, a larger one (&gt; 4 KB) or unreadable JSON leaves it false and never changes the result.
        /// </summary>
        private static async Task<bool> IsHealthyAsync(HealthProbe health, CancellationToken ct)
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(HealthTimeout);
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, BuildUri("/health"));
                using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, cts.Token);
                if (!resp.IsSuccessStatusCode) return false;
                health.Sandbox = await ReadSandboxModeAsync(resp, cts.Token);
                return true;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception)
            {
                return false;   // refused connection / timeout → not running
            }
        }

        private static async Task<bool> ReadSandboxModeAsync(HttpResponseMessage resp, CancellationToken ct)
        {
            const int MaxHealthBytes = 4 * 1024;
            try
            {
                if (resp.Content.Headers.ContentLength is long length && length > MaxHealthBytes) return false;
                var bytes = await resp.Content.ReadAsByteArrayAsync(ct);
                if (bytes.Length == 0 || bytes.Length > MaxHealthBytes) return false;
                using var doc = JsonDocument.Parse(bytes);
                return doc.RootElement.ValueKind == JsonValueKind.Object
                       && doc.RootElement.TryGetProperty("mode", out var mode) && mode.ValueKind == JsonValueKind.String
                       && string.Equals(mode.GetString(), "sandbox", StringComparison.OrdinalIgnoreCase);
            }
            catch (JsonException) { return false; }
            catch (HttpRequestException) { return false; }
            catch (OperationCanceledException) { return false; }   // the body did not arrive in time: mode unknown, the 200 still counts
        }

        /// <summary>One GET /v2/insights/summary incl. reading the body (≤ 256 KB; null = too large) within the request timeout.</summary>
        private static async Task<(HttpStatusCode Status, string? Body)> GetSummaryAsync(string token, CancellationToken ct)
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(RequestTimeout);
            using var req = new HttpRequestMessage(HttpMethod.Get, BuildUri("/v2/insights/summary"));
            req.Headers.TryAddWithoutValidation(BackendAuth.HeaderName, token);
            req.Headers.Accept.ParseAdd("application/json");
            using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, cts.Token);
            if (resp.Content.Headers.ContentLength is long length && length > MaxPayloadBytes) return (resp.StatusCode, null);
            var bytes = await resp.Content.ReadAsByteArrayAsync(cts.Token);
            if (bytes.Length > MaxPayloadBytes) return (resp.StatusCode, null);
            return (resp.StatusCode, System.Text.Encoding.UTF8.GetString(bytes));
        }

        private static string? ErrorField(string body)
        {
            try
            {
                using var doc = JsonDocument.Parse(body);
                return doc.RootElement.ValueKind == JsonValueKind.Object && doc.RootElement.TryGetProperty("error", out var e)
                       && e.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(e.GetString()) ? e.GetString() : null;
            }
            catch (JsonException) { return null; }
        }

        private static string? Str(JsonElement o, string name)
            => o.ValueKind == JsonValueKind.Object && o.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

        private static double? Num(JsonElement o, string name)
            => o.ValueKind == JsonValueKind.Object && o.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetDouble(out var d) ? d : null;

        /// <summary>"+1,234.56 EUR" / "−20.00 USD" (typographic minus, invariant culture).</summary>
        public static string Signed(double value, string currency)
        {
            var abs = Math.Abs(value).ToString("N2", Inv);
            var sign = Math.Round(value, 2) > 0 ? "+" : Math.Round(value, 2) < 0 ? "−" : "";
            return sign + abs + " " + currency;
        }

        private static PortfolioInsightCard FromPayload(JsonElement root)
        {
            // a body that is not a JSON object (array, string, null …) — TryGetProperty would throw InvalidOperationException
            if (root.ValueKind != JsonValueKind.Object)
                return Card(PortfolioInsightState.Unavailable, "not available (unexpected response from the backend)");
            var state = Str(root, "state");
            var label = Str(root, "label");
            var reasons = new List<string>();
            if (root.TryGetProperty("reasons", out var rs) && rs.ValueKind == JsonValueKind.Array)
                foreach (var x in rs.EnumerateArray())
                    if (x.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(x.GetString())) reasons.Add(x.GetString()!);
            if (state == "no_imports")
                return new PortfolioInsightCard(PortfolioInsightState.NoImports, Array.Empty<string>(), 0, null, label, null, NoImportsText, reasons);
            if (state != "ok" && state != "incomplete")
                return Card(PortfolioInsightState.Unavailable, "not available (the backend reported no state)");

            int? yearNumber = root.TryGetProperty("year", out var y) && y.ValueKind == JsonValueKind.Number && y.TryGetInt32(out var yi) ? yi : null;
            var year = yearNumber?.ToString(Inv);
            var lines = new List<string>();
            var more = 0;
            var realised = new List<PortfolioRealised>();
            var unrealised = new List<PortfolioUnrealised>();
            // the backend's own count (same definition as its "N position(s) without a price source" reason; positions valued
            // only in another currency are NOT unpriced) — never derived from positions - valued
            int? unpricedCount = Num(root, "unpriced") is double up ? (int)Math.Round(up) : null;
            if (root.TryGetProperty("currencies", out var cs) && cs.ValueKind == JsonValueKind.Array)
            {
                foreach (var c in cs.EnumerateArray())
                {
                    var cur = Str(c, "currency");
                    if (cur == null) continue;
                    // unrealised / coverage per currency (only currencies holding positions), as the backend's detail does
                    if (Num(c, "positions") is double positions && positions > 0)
                    {
                        var valued = Num(c, "valued") ?? 0;
                        var amount = Num(c, "unrealised");
                        var coverage = Num(c, "coverage_pct");
                        unrealised.Add(valued > 0 && amount is double am && coverage is double cv
                            ? new PortfolioUnrealised(cur, am, (int)Math.Round(cv, MidpointRounding.AwayFromZero), false)
                            : new PortfolioUnrealised(cur, null, null, true));
                    }
                    if (Num(c, "realised_net") is not double net) continue;
                    if (Math.Round(net, 2) == 0) continue;   // like the backend's headline (§20.5): currencies with net 0 are omitted
                    realised.Add(new PortfolioRealised(net, cur));
                    if (lines.Count < MaxLines) lines.Add("Realised " + (year ?? "this year") + ": " + Signed(net, cur));
                    else more++;
                }
            }
            int? depotCount = root.TryGetProperty("accounts", out var ac) && ac.ValueKind == JsonValueKind.Number && ac.TryGetInt32(out var acn) ? acn : null;
            if (lines.Count == 0 && Str(root, "headline") is string headline) lines.Add(headline);   // the backend's own headline ("Realised 2026: nothing realised yet")

            string? pricesAsOf = null;
            if (root.TryGetProperty("as_of", out var a) && a.ValueKind == JsonValueKind.Object)
            {
                var parts = new List<string>();
                if (Str(a, "valuation") is string v) parts.Add("valuation " + v);
                if (Str(a, "market") is string m) parts.Add("market close " + m);
                if (parts.Count > 0) pricesAsOf = "Prices as of " + string.Join(" · ", parts);
            }
            string? computedAt = null;
            if (Str(root, "generated_at") is string gen && DateTimeOffset.TryParse(gen, Inv, DateTimeStyles.AssumeUniversal, out var g))
                computedAt = "computed " + g.ToLocalTime().ToString("yyyy-MM-dd HH:mm", Inv);
            var asOf = pricesAsOf != null && computedAt != null ? pricesAsOf + " · " + computedAt : pricesAsOf ?? computedAt;
            return new PortfolioInsightCard(state == "ok" ? PortfolioInsightState.Ok : PortfolioInsightState.Incomplete,
                                            lines, more, Str(root, "detail"), label, asOf, null, reasons,
                                            yearNumber, realised, unrealised, unpricedCount, depotCount, pricesAsOf, computedAt, label);
        }
    }
}

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    // =====================================================================================
    // Typed client for the Market Research routes /research/* of the HedgeFund backend
    // (contract: backend/market_research/SPEC.md §1). Same rules as Platform/PlatformClient.cs:
    // - base URL and session token come from the existing helper (NewsroomBackend.BaseUrl /
    //   NewsroomBackend.CreateClient → Shared/BackendAuth.cs, header X-UFOS-Token); the token is
    //   re-read once on a 401 and the request retried once (SPEC §1.1);
    // - calls never throw: they return a PlatformResult with a status and the message the WPF shows
    //   (SPEC §1.3, texts as specified there; the backend's `error` text verbatim where it says so);
    // - no retries beyond that one token re-read, no alternative paths (no-fallback rule).
    // Static on purpose: the one Research view (rail section or pop-out) is the only caller, so
    // there is never a second client or a second polling loop.
    // =====================================================================================

    internal static class ResearchClient
    {
        private const string LogModule = "HedgeFund";

        public const string NotReachableMessage = "Research unavailable: the HedgeFund backend is not reachable.";
        public const string TokenRejectedMessage = "Research unavailable: the backend rejected the app's token — restart the HedgeFund backend.";
        public const string InternalErrorMessage = "Research unavailable: internal error (see backend.log).";
        public const string OldBackendMessage = "Research needs a newer backend — restart the HedgeFund backend.";

        /// <summary>Shared by the REST calls and <see cref="ResearchSocket"/> (same DTOs, same rules).</summary>
        internal static readonly JsonSerializerOptions Json = new()
        {
            PropertyNameCaseInsensitive = true,
            NumberHandling = JsonNumberHandling.AllowReadingFromString,
        };

        /// <summary>SPEC §1.3: "connection refused / timeout 5 s".</summary>
        private static readonly TimeSpan StatusTimeout = TimeSpan.FromSeconds(5);

        /// <summary>
        /// Data routes (MR2a): Research requests may wait up to 30 s in the shared Alpaca rate gate
        /// (SPEC §4.4 RESEARCH_ACQUIRE_TIMEOUT_SECONDS) plus the fetch itself.
        /// </summary>
        private static readonly TimeSpan DataTimeout = TimeSpan.FromSeconds(60);

        /// <summary>GET /research/feeds/status (SPEC §1.2) — no query parameters, no body.</summary>
        public static Task<PlatformResult<FeedsStatus>> GetFeedsStatusAsync()
            => SendAsync<FeedsStatus>(HttpMethod.Get, "/research/feeds/status", null, StatusTimeout);

        // ---------- MR2a data routes (SPEC §4.2 + "MR2a notes") ----------

        /// <summary>GET /research/assets?q=&amp;limit= — q 1–32 chars (letters, digits, space, <c>.-/</c>), limit 1–50.</summary>
        public static Task<PlatformResult<AssetSearchResult>> SearchAssetsAsync(string query, int limit)
            => SendAsync<AssetSearchResult>(HttpMethod.Get,
                "/research/assets?q=" + Uri.EscapeDataString(query) + "&limit=" + Math.Clamp(limit, 1, 50).ToString(CultureInfo.InvariantCulture),
                null, DataTimeout, dataRoute: true);

        /// <summary>GET /research/market/status (calendar only, no Alpaca request).</summary>
        public static Task<PlatformResult<MarketStatus>> GetMarketStatusAsync()
            => SendAsync<MarketStatus>(HttpMethod.Get, "/research/market/status", null, StatusTimeout, dataRoute: true);

        /// <summary>GET /research/symbols/{s}/snapshot (header values, §7.4).</summary>
        public static Task<PlatformResult<SymbolSnapshot>> GetSnapshotAsync(string symbol)
            => SendAsync<SymbolSnapshot>(HttpMethod.Get, "/research/symbols/" + Uri.EscapeDataString(symbol) + "/snapshot",
                null, DataTimeout, dataRoute: true);

        /// <summary>GET /research/symbols/{s}/stats (key stats, performance, 52W).</summary>
        public static Task<PlatformResult<SymbolStats>> GetStatsAsync(string symbol)
            => SendAsync<SymbolStats>(HttpMethod.Get, "/research/symbols/" + Uri.EscapeDataString(symbol) + "/stats",
                null, DataTimeout, dataRoute: true);

        /// <summary>
        /// GET /research/symbols/{s}/bars?tf=&amp;session=&amp;before=&amp;limit= (oldest first). <paramref name="session"/>
        /// only for equities (<c>rth</c>/<c>eth</c>) — the backend answers 400 when it is given for crypto, so
        /// pass null there; <paramref name="beforeIso"/> = exclusive upper bound (paging left, UTC ISO);
        /// <paramref name="limit"/> null = the backend's initial window (§7.2).
        /// </summary>
        public static Task<PlatformResult<BarsResponse>> GetBarsAsync(string symbol, string tf, string? session, string? beforeIso, int? limit)
        {
            var path = new StringBuilder("/research/symbols/").Append(Uri.EscapeDataString(symbol))
                .Append("/bars?tf=").Append(Uri.EscapeDataString(tf));
            if (session != null) path.Append("&session=").Append(Uri.EscapeDataString(session));
            if (beforeIso != null) path.Append("&before=").Append(Uri.EscapeDataString(beforeIso));
            if (limit != null) path.Append("&limit=").Append(limit.Value.ToString(CultureInfo.InvariantCulture));
            return SendAsync<BarsResponse>(HttpMethod.Get, path.ToString(), null, DataTimeout, dataRoute: true);
        }

        // ---------- MR4 workspace routes (SPEC "MR4 backend notes": watchlists, quotes, session, recent, palette) ----------

        /// <summary>Workspace routes read/write the local Research DB (no Alpaca request), except quotes/palette.</summary>
        private static readonly TimeSpan WorkspaceTimeout = TimeSpan.FromSeconds(10);

        /// <summary>GET /research/watchlists → <c>{lists, limits}</c> (by position; no list is seeded).</summary>
        public static Task<PlatformResult<WatchlistsResponse>> GetWatchlistsAsync()
            => SendAsync<WatchlistsResponse>(HttpMethod.Get, "/research/watchlists", null, WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>POST /research/watchlists {name} → 201 <c>{id, lists, limits}</c> (appended at the end).</summary>
        public static Task<PlatformResult<WatchlistsResponse>> CreateWatchlistAsync(string name)
            => SendAsync<WatchlistsResponse>(HttpMethod.Post, "/research/watchlists", new Dictionary<string, object> { ["name"] = name },
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>PATCH /research/watchlists/{id} {name?, collapsed?, position?} (≥ 1 key) → <c>{lists, limits}</c>.</summary>
        public static Task<PlatformResult<WatchlistsResponse>> PatchWatchlistAsync(string id, string? name, bool? collapsed, int? position)
        {
            var body = new Dictionary<string, object>();
            if (name != null) body["name"] = name;
            if (collapsed != null) body["collapsed"] = collapsed.Value;
            if (position != null) body["position"] = position.Value;
            return SendAsync<WatchlistsResponse>(HttpMethod.Patch, "/research/watchlists/" + Uri.EscapeDataString(id), body,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);
        }

        /// <summary>DELETE /research/watchlists/{id} → <c>{lists, limits}</c>.</summary>
        public static Task<PlatformResult<WatchlistsResponse>> DeleteWatchlistAsync(string id)
            => SendAsync<WatchlistsResponse>(HttpMethod.Delete, "/research/watchlists/" + Uri.EscapeDataString(id), null,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>PUT /research/watchlists/{id}/items {symbols} — replaces the ordered items (reorder = the new order).</summary>
        public static Task<PlatformResult<WatchlistsResponse>> PutWatchlistItemsAsync(string id, IReadOnlyList<string> symbols)
            => SendAsync<WatchlistsResponse>(HttpMethod.Put, "/research/watchlists/" + Uri.EscapeDataString(id) + "/items",
                new Dictionary<string, object> { ["symbols"] = symbols }, WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>POST /research/watchlists/{id}/items {symbol} — appended (404 SymbolNotCovered, 409 already in / full).</summary>
        public static Task<PlatformResult<WatchlistsResponse>> AddWatchlistItemAsync(string id, string symbol)
            => SendAsync<WatchlistsResponse>(HttpMethod.Post, "/research/watchlists/" + Uri.EscapeDataString(id) + "/items",
                new Dictionary<string, object> { ["symbol"] = symbol }, DataTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>DELETE /research/watchlists/{id}/items/{symbol} → <c>{lists, limits}</c>.</summary>
        public static Task<PlatformResult<WatchlistsResponse>> RemoveWatchlistItemAsync(string id, string symbol)
            => SendAsync<WatchlistsResponse>(HttpMethod.Delete,
                "/research/watchlists/" + Uri.EscapeDataString(id) + "/items/" + Uri.EscapeDataString(symbol), null,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>GET /research/watchlists/{id}/quotes — live state + last stream trade + prev close per item (never a snapshot).</summary>
        public static Task<PlatformResult<WatchlistQuotes>> GetWatchlistQuotesAsync(string id)
            => SendAsync<WatchlistQuotes>(HttpMethod.Get, "/research/watchlists/" + Uri.EscapeDataString(id) + "/quotes", null,
                DataTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>GET /research/session (tabs/session restore; nothing saved → <c>saved: false</c>).</summary>
        public static Task<PlatformResult<SessionState>> GetSessionAsync()
            => SendAsync<SessionState>(HttpMethod.Get, "/research/session", null, WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>PUT /research/session — body exactly {tabs, active, sidebar}; answer = the GET shape.</summary>
        public static Task<PlatformResult<SessionState>> PutSessionAsync(object body)
            => SendAsync<SessionState>(HttpMethod.Put, "/research/session", body, WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>POST /research/recent {symbol} → <c>{recent: [..]}</c> (newest first, ≤ 20; 404 SymbolNotCovered).</summary>
        public static Task<PlatformResult<RecentSymbols>> AddRecentAsync(string symbol)
            => SendAsync<RecentSymbols>(HttpMethod.Post, "/research/recent", new Dictionary<string, object> { ["symbol"] = symbol },
                DataTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>GET /research/palette?q=&amp;limit= — ranked search (q empty = recent symbols).</summary>
        public static Task<PlatformResult<PaletteResult>> GetPaletteAsync(string query, int limit)
        {
            var path = "/research/palette?limit=" + Math.Clamp(limit, 1, 50).ToString(CultureInfo.InvariantCulture);
            if (!string.IsNullOrEmpty(query)) path += "&q=" + Uri.EscapeDataString(query);
            return SendAsync<PaletteResult>(HttpMethod.Get, path, null, DataTimeout, dataRoute: true, workspaceRoute: true);
        }

        // ---------- MR5 drawings, chart settings, indicators ("MR5 backend notes") ----------

        /// <summary>GET /research/drawings?symbol= → all drawings of the symbol (created order) + split-check state.</summary>
        public static Task<PlatformResult<DrawingsResponse>> GetDrawingsAsync(string symbol)
            => SendAsync<DrawingsResponse>(HttpMethod.Get, "/research/drawings?symbol=" + Uri.EscapeDataString(symbol), null,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>POST /research/drawings {symbol, tool, points, style?, name?, text?, locked?, visible?, timeframes?, alert_id?} → 201 drawing.</summary>
        public static Task<PlatformResult<DrawingDto>> CreateDrawingAsync(IReadOnlyDictionary<string, object?> body)
            => SendAsync<DrawingDto>(HttpMethod.Post, "/research/drawings", body, WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>PATCH /research/drawings/{id} — only the changed keys ⊆ {points, style, name, text, locked, visible, timeframes, alert_id}.</summary>
        public static Task<PlatformResult<DrawingDto>> PatchDrawingAsync(string id, IReadOnlyDictionary<string, object?> changes)
            => SendAsync<DrawingDto>(HttpMethod.Patch, "/research/drawings/" + Uri.EscapeDataString(id), changes,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>DELETE /research/drawings/{id} → <c>{deleted: id}</c> (locked → 409, unknown → 404).</summary>
        public static Task<PlatformResult<JsonElement>> DeleteDrawingAsync(string id)
            => SendAsync<JsonElement>(HttpMethod.Delete, "/research/drawings/" + Uri.EscapeDataString(id), null,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>DELETE /research/drawings?symbol=&amp;confirm=true → <c>{symbol, deleted: n}</c> (after the UI confirmation).</summary>
        public static Task<PlatformResult<JsonElement>> DeleteAllDrawingsAsync(string symbol)
            => SendAsync<JsonElement>(HttpMethod.Delete, "/research/drawings?symbol=" + Uri.EscapeDataString(symbol) + "&confirm=true", null,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>GET /research/drawings/export?symbol= → the file object (saved as &lt;SYMBOL&gt;-drawings.json).</summary>
        public static Task<PlatformResult<JsonElement>> ExportDrawingsAsync(string symbol)
            => SendAsync<JsonElement>(HttpMethod.Get, "/research/drawings/export?symbol=" + Uri.EscapeDataString(symbol), null,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>
        /// POST /research/drawings/import?symbol=&amp;mode=append|replace, body = the file object as compact UTF-8 JSON
        /// (<see cref="CompactJson"/> — exactly the bytes whose size the view checked against MAX_IMPORT_BYTES; all or nothing).
        /// </summary>
        public static Task<PlatformResult<DrawingImportResult>> ImportDrawingsAsync(string symbol, string mode, string compactJson)
            => SendAsync<DrawingImportResult>(HttpMethod.Post,
                "/research/drawings/import?symbol=" + Uri.EscapeDataString(symbol) + "&mode=" + (mode == "replace" ? "replace" : "append"),
                new RawJsonBody(compactJson), DataTimeout, dataRoute: true, workspaceRoute: true, fieldPrefix: "Not imported: ");

        /// <summary>
        /// Compact JSON with non-ASCII text kept as UTF-8 (not \uXXXX), so the byte count equals what the backend measures
        /// (raw UTF-8 of the compact body). Only for request bodies to our own backend — never rendered as HTML.
        /// </summary>
        public static string CompactJson(JsonElement element)
            => JsonSerializer.Serialize(element, new JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping });

        /// <summary>GET /research/chart-settings?symbol= (defaults when nothing was saved: candles, [], off, rth/null).</summary>
        public static Task<PlatformResult<ChartSettingsResponse>> GetChartSettingsAsync(string symbol)
            => SendAsync<ChartSettingsResponse>(HttpMethod.Get, "/research/chart-settings?symbol=" + Uri.EscapeDataString(symbol), null,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>PUT /research/chart-settings?symbol= body = exactly {type, indicators, magnet, session}.</summary>
        public static Task<PlatformResult<ChartSettingsResponse>> PutChartSettingsAsync(string symbol, string type, IReadOnlyList<string> indicators,
                                                                                         string magnet, string? session)
            => SendAsync<ChartSettingsResponse>(HttpMethod.Put, "/research/chart-settings?symbol=" + Uri.EscapeDataString(symbol),
                new Dictionary<string, object?> { ["type"] = type, ["indicators"] = indicators, ["magnet"] = magnet, ["session"] = session },
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true, fieldPrefix: "Chart settings not saved: ");

        /// <summary>
        /// GET /research/symbols/{s}/indicators?tf=&amp;session=&amp;before=&amp;limit=&amp;ind= — same window as /bars (same
        /// parameters), <paramref name="ind"/> = comma-separated specs (≤ 400 chars, ≤ 8 specs, ≤ 3 sub-panes).
        /// </summary>
        public static Task<PlatformResult<IndicatorsResponse>> GetIndicatorsAsync(string symbol, string tf, string? session, string? beforeIso,
                                                                                  int? limit, string ind, string? cmpBaseIso = null)
        {
            var path = new StringBuilder("/research/symbols/").Append(Uri.EscapeDataString(symbol))
                .Append("/indicators?tf=").Append(Uri.EscapeDataString(tf));
            if (session != null) path.Append("&session=").Append(Uri.EscapeDataString(session));
            if (beforeIso != null) path.Append("&before=").Append(Uri.EscapeDataString(beforeIso));
            if (limit != null) path.Append("&limit=").Append(limit.Value.ToString(CultureInfo.InvariantCulture));
            path.Append("&ind=").Append(Uri.EscapeDataString(ind));
            // Compare overlay: one fixed base bar for every request of this chart load (initial, paging, live refresh),
            // so merged pages never jump (review H1; backend `cmp_base`).
            if (cmpBaseIso != null) path.Append("&cmp_base=").Append(Uri.EscapeDataString(cmpBaseIso));
            return SendAsync<IndicatorsResponse>(HttpMethod.Get, path.ToString(), null, DataTimeout, dataRoute: true, workspaceRoute: true,
                fieldPrefix: "Indicators not loaded: ");
        }

        // ---------- MR6 news + logos (SPEC "MR6 backend notes") ----------

        /// <summary>GET /research/symbols/{s}/news?limit=&amp;before= (newest first; 503 "News unavailable: …" verbatim).</summary>
        public static Task<PlatformResult<NewsList>> GetNewsAsync(string symbol, int limit, string? beforeIso)
        {
            var path = "/research/symbols/" + Uri.EscapeDataString(symbol) + "/news?limit=" +
                       Math.Clamp(limit, 1, 50).ToString(CultureInfo.InvariantCulture);
            if (beforeIso != null) path += "&before=" + Uri.EscapeDataString(beforeIso);
            return SendAsync<NewsList>(HttpMethod.Get, path, null, DataTimeout, dataRoute: true);
        }

        /// <summary>GET /research/news/settings (thumbnails switch + privacy note + logo plan state).</summary>
        public static Task<PlatformResult<NewsSettings>> GetNewsSettingsAsync()
            => SendAsync<NewsSettings>(HttpMethod.Get, "/research/news/settings", null, StatusTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>PUT /research/news/settings {thumbnails} (exactly that key).</summary>
        public static Task<PlatformResult<NewsSettings>> PutNewsSettingsAsync(bool thumbnails)
            => SendAsync<NewsSettings>(HttpMethod.Put, "/research/news/settings", new Dictionary<string, object> { ["thumbnails"] = thumbnails },
                StatusTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>
        /// GET of a binary Research route (news thumbnail proxy, symbol logo) — ONLY the HedgeFund backend
        /// (127.0.0.1:8867) with the token header, never a third-party host. The body is read up to
        /// <paramref name="maxBytes"/> (Content-Length first, then while reading); a larger answer is refused.
        /// Never throws; a non-200 answer carries the backend's `error` / `kind`.
        /// </summary>
        public static async Task<BinaryResult> GetBinaryAsync(string path, int maxBytes, TimeSpan timeout)
        {
            if (!path.StartsWith("/research/", StringComparison.Ordinal) || path.Contains("..", StringComparison.Ordinal) ||
                path.Contains("//", StringComparison.Ordinal) || path.Contains('\\'))
                return new BinaryResult { Error = "refused: not a Research route" };
            for (var attempt = 1; attempt <= 2; attempt++)
            {
                try
                {
                    using var client = NewsroomBackend.CreateClient(timeout);
                    using var resp = await client.GetAsync(NewsroomBackend.BaseUrl + path, HttpCompletionOption.ResponseHeadersRead);
                    var code = (int)resp.StatusCode;
                    if ((code == 401 || code == 403) && attempt == 1) continue; // re-read the token once (§1.1)
                    if (code == 200)
                    {
                        var length = resp.Content.Headers.ContentLength;
                        if (length is long l && l > maxBytes)
                            return new BinaryResult { Status = code, Error = "the image is larger than " + (maxBytes / 1024) + " KB" };
                        await using var stream = await resp.Content.ReadAsStreamAsync();
                        using var buffer = new System.IO.MemoryStream();
                        var chunk = new byte[16 * 1024];
                        int read;
                        while ((read = await stream.ReadAsync(chunk.AsMemory(0, chunk.Length))) > 0)
                        {
                            if (buffer.Length + read > maxBytes)
                                return new BinaryResult { Status = code, Error = "the image is larger than " + (maxBytes / 1024) + " KB" };
                            buffer.Write(chunk, 0, read);
                        }
                        return new BinaryResult
                        {
                            Ok = true,
                            Status = code,
                            Bytes = buffer.ToArray(),
                            ContentType = resp.Content.Headers.ContentType?.MediaType,
                        };
                    }
                    string text;
                    try
                    {
                        var raw = await resp.Content.ReadAsStringAsync();
                        text = raw.Length > 65536 ? raw.Substring(0, 65536) : raw;
                    }
                    catch { text = ""; }
                    return new BinaryResult
                    {
                        Status = code,
                        Error = ReadErrorField(text) ?? (code == 401 || code == 403 ? TokenRejectedMessage : "HTTP " + code),
                        Kind = ReadErrorKind(text),
                    };
                }
                catch (Exception)
                {
                    return new BinaryResult { Error = NotReachableMessage };
                }
            }
            return new BinaryResult { Status = 401, Error = TokenRejectedMessage };
        }

        // ---------- MR7 alerts ("MR7 backend notes") ----------

        /// <summary>GET /research/alerts → <c>{alerts (oldest first), limits, runtime}</c>.</summary>
        public static Task<PlatformResult<AlertsResponse>> GetAlertsAsync()
            => SendAsync<AlertsResponse>(HttpMethod.Get, "/research/alerts", null, WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>POST /research/alerts {symbol, kind, params, cooldown_seconds?, enabled?} → 201 alert.</summary>
        public static Task<PlatformResult<AlertDto>> CreateAlertAsync(IReadOnlyDictionary<string, object?> body)
            => SendAsync<AlertDto>(HttpMethod.Post, "/research/alerts", body, DataTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>PATCH /research/alerts/{id} {params?, cooldown_seconds?, enabled?} (≥ 1 key) → 200 alert.</summary>
        public static Task<PlatformResult<AlertDto>> PatchAlertAsync(string id, IReadOnlyDictionary<string, object?> changes)
            => SendAsync<AlertDto>(HttpMethod.Patch, "/research/alerts/" + Uri.EscapeDataString(id), changes,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>DELETE /research/alerts/{id} → <c>{deleted: id}</c> (the log rows stay).</summary>
        public static Task<PlatformResult<JsonElement>> DeleteAlertAsync(string id)
            => SendAsync<JsonElement>(HttpMethod.Delete, "/research/alerts/" + Uri.EscapeDataString(id), null,
                WorkspaceTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>GET /research/alerts/log?limit=1…500&amp;alert_id= → newest first (90-day retention).</summary>
        public static Task<PlatformResult<AlertLogResponse>> GetAlertLogAsync(int limit, string? alertId)
        {
            var path = "/research/alerts/log?limit=" + Math.Clamp(limit, 1, 500).ToString(CultureInfo.InvariantCulture);
            if (alertId != null) path += "&alert_id=" + Uri.EscapeDataString(alertId);
            return SendAsync<AlertLogResponse>(HttpMethod.Get, path, null, WorkspaceTimeout, dataRoute: true, workspaceRoute: true);
        }

        // ---------- MR8 integrations (SPEC "MR8 backend notes") — all GET, read-only; no order route exists ----------

        /// <summary>GET /research/portfolio — the HedgeFund paper account as the engine stored it (read-only).</summary>
        public static Task<PlatformResult<PortfolioState>> GetPortfolioAsync()
            => SendAsync<PortfolioState>(HttpMethod.Get, "/research/portfolio", null, WorkspaceTimeout, dataRoute: true);

        /// <summary>GET /research/symbols/{s}/markers?from=&amp;to=&amp;kinds= (insider, calendar; Newsroom data).</summary>
        public static Task<PlatformResult<MarkersResponse>> GetMarkersAsync(string symbol, string? fromIso, string? toIso, string kinds)
        {
            var path = new StringBuilder("/research/symbols/").Append(Uri.EscapeDataString(symbol)).Append("/markers?kinds=")
                .Append(Uri.EscapeDataString(kinds));
            if (fromIso != null) path.Append("&from=").Append(Uri.EscapeDataString(fromIso));
            if (toIso != null) path.Append("&to=").Append(Uri.EscapeDataString(toIso));
            return SendAsync<MarkersResponse>(HttpMethod.Get, path.ToString(), null, WorkspaceTimeout, dataRoute: true);
        }

        /// <summary>GET /research/backtests/{run_id}/markers?symbol= (fills, best/worst periods, drawdowns of a finished run).</summary>
        public static Task<PlatformResult<MarkersResponse>> GetBacktestMarkersAsync(string runId, string symbol)
            => SendAsync<MarkersResponse>(HttpMethod.Get,
                "/research/backtests/" + Uri.EscapeDataString(runId) + "/markers?symbol=" + Uri.EscapeDataString(symbol),
                null, WorkspaceTimeout, dataRoute: true);

        /// <summary>GET /research/watchlists/{id}/universe — the list as Strategy Platform universe input (no strategy write).</summary>
        public static Task<PlatformResult<WatchlistUniverse>> GetWatchlistUniverseAsync(string id)
            => SendAsync<WatchlistUniverse>(HttpMethod.Get, "/research/watchlists/" + Uri.EscapeDataString(id) + "/universe", null,
                DataTimeout, dataRoute: true, workspaceRoute: true);

        /// <summary>The <c>kind</c> field of a JSON error body (e.g. SymbolNotCovered, SecretMissing), or null.</summary>
        public static string? ReadErrorKind(string? body) => ReadStringField(body, "kind");

        // ---------- plumbing ----------

        private static async Task<PlatformResult<T>> SendAsync<T>(HttpMethod method, string path, object? body, TimeSpan timeout,
                                                                  bool dataRoute = false, bool workspaceRoute = false,
                                                                  string fieldPrefix = "Not saved: ")
        {
            for (var attempt = 1; attempt <= 2; attempt++)
            {
                HttpStatusCode code;
                string text;
                try
                {
                    // A new client per attempt: BackendAuth reads the token file on every creation, so the
                    // second attempt carries the token of a backend that restarted in between.
                    using var client = NewsroomBackend.CreateClient(timeout);
                    using var request = new HttpRequestMessage(method, NewsroomBackend.BaseUrl + path);
                    if (body != null)
                        request.Content = new StringContent(body is RawJsonBody raw ? raw.Json : JsonSerializer.Serialize(body, Json),
                                                            Encoding.UTF8, "application/json");
                    using var resp = await client.SendAsync(request);
                    code = resp.StatusCode;
                    try { text = await resp.Content.ReadAsStringAsync(); }
                    catch { text = ""; }
                }
                catch (Exception)
                {
                    // Connection refused, timeout, DNS/socket errors: SPEC §1.3 row 1 (polling continues).
                    return new PlatformResult<T> { Status = PlatformCallStatus.Offline, Message = NotReachableMessage };
                }

                if ((code == HttpStatusCode.Unauthorized || code == HttpStatusCode.Forbidden) && attempt == 1)
                    continue; // SPEC §1.1: re-read the token once and retry once

                if ((int)code >= 200 && (int)code < 300)
                {
                    try
                    {
                        var value = string.IsNullOrWhiteSpace(text) ? default : JsonSerializer.Deserialize<T>(text, Json);
                        return new PlatformResult<T> { Status = PlatformCallStatus.Ok, Value = value, HttpStatus = (int)code };
                    }
                    catch (Exception ex) when (ex is JsonException || ex is InvalidOperationException || ex is NotSupportedException)
                    {
                        AppLog.Warn(LogModule, "Market Research: unexpected response from " + path + ".", ex);
                        return new PlatformResult<T>
                        {
                            Status = PlatformCallStatus.Error,
                            HttpStatus = (int)code,
                            Message = "Research unavailable: unexpected response from the backend — backend and app versions may not match.",
                        };
                    }
                }
                return Fail<T>(code, text, path, dataRoute, workspaceRoute, fieldPrefix);
            }
            // Only reached when the retry after a 401/403 was rejected again (the loop `continue`s once).
            return new PlatformResult<T> { Status = PlatformCallStatus.Unauthorized, HttpStatus = 401, Message = TokenRejectedMessage };
        }

        // dataRoute (MR2a routes): the backend's `error` text is the message the chart card shows, verbatim —
        // 404 SymbolNotCovered ("Not available from Alpaca …"), 503 SecretMissing / DataSourceUnavailable /
        // SIP plan / CalendarUnavailable, 400 "Intraday history older than 30 days …". A 404 WITHOUT a JSON
        // `error` is aiohttp's own "route not found" → old backend; 503 ResearchUnavailable keeps the prefix.
        // workspaceRoute (MR4 routes): 409 conflicts ("A watchlist named … already exists.", "At most 20 watchlists …")
        // and 400 "invalid input" field messages (name too long, duplicates …) are user-facing → shown verbatim.
        private static PlatformResult<T> Fail<T>(HttpStatusCode code, string body, string path, bool dataRoute = false,
                                                 bool workspaceRoute = false, string fieldPrefix = "Not saved: ")
        {
            var error = ReadErrorField(body);
            if (workspaceRoute && error != null)
            {
                if (code == HttpStatusCode.Conflict)
                    return new PlatformResult<T> { Status = PlatformCallStatus.Conflict, HttpStatus = 409, Message = error, RawBody = body };
                if (code == HttpStatusCode.BadRequest && error == "invalid input")
                {
                    var fieldErrors = FundClient.ParseErrors(body);
                    AppLog.Warn(LogModule, "Market Research: " + path + " rejected the input (HTTP 400): " + string.Join("; ", fieldErrors));
                    return new PlatformResult<T>
                    {
                        Status = PlatformCallStatus.BadRequest,
                        HttpStatus = 400,
                        Message = fieldErrors.Count > 0 ? fieldPrefix + string.Join("; ", fieldErrors) : fieldPrefix + "the backend rejected the input.",
                        RawBody = body,
                    };
                }
            }
            if (dataRoute && error != null)
            {
                var kind = ReadErrorKind(body);
                if (code == HttpStatusCode.NotFound)
                    return new PlatformResult<T> { Status = PlatformCallStatus.NotFound, HttpStatus = 404, Message = error, RawBody = body };
                if (code == HttpStatusCode.ServiceUnavailable && kind != "ResearchUnavailable")
                    return new PlatformResult<T> { Status = PlatformCallStatus.Unavailable, HttpStatus = 503, Message = error, RawBody = body };
                if (code == HttpStatusCode.BadRequest && error != "invalid input")
                {
                    AppLog.Warn(LogModule, "Market Research: " + path + " rejected the request (HTTP 400): " + error);
                    return new PlatformResult<T> { Status = PlatformCallStatus.BadRequest, HttpStatus = 400, Message = error, RawBody = body };
                }
            }
            PlatformCallStatus status;
            string message;
            switch (code)
            {
                case HttpStatusCode.Unauthorized:
                case HttpStatusCode.Forbidden:
                    status = PlatformCallStatus.Unauthorized;
                    message = TokenRejectedMessage;
                    break;
                case HttpStatusCode.ServiceUnavailable:
                    // Research package failed to load (503 stub route, kind "ResearchUnavailable"): `error` verbatim.
                    status = PlatformCallStatus.Unavailable;
                    message = "Research unavailable: " + (error ?? "the backend is temporarily unavailable (HTTP 503).");
                    break;
                case HttpStatusCode.InternalServerError:
                    status = PlatformCallStatus.Error;
                    message = InternalErrorMessage;
                    break;
                case HttpStatusCode.NotFound:
                    status = PlatformCallStatus.NotFound;
                    message = OldBackendMessage;
                    break;
                case HttpStatusCode.BadRequest:
                case HttpStatusCode.UnprocessableEntity:
                {
                    // A 400 here is a client bug (SPEC §1.3): log the details, show `error`.
                    var errors = FundClient.ParseErrors(body);
                    AppLog.Warn(LogModule, "Market Research: " + path + " rejected the request (HTTP " + (int)code + "): "
                                           + string.Join("; ", errors));
                    status = PlatformCallStatus.BadRequest;
                    message = "Research request rejected by the backend: " + (error ?? "invalid input") + ".";
                    break;
                }
                default:
                    status = PlatformCallStatus.Error;
                    message = "Research unavailable: " + (error ?? "backend error (HTTP " + (int)code + ").");
                    break;
            }
            return new PlatformResult<T> { Status = status, HttpStatus = (int)code, Message = message, RawBody = body };
        }

        /// <summary>The string `error` field of a JSON error body, unchanged, or null.</summary>
        private static string? ReadErrorField(string body) => ReadStringField(body, "error");

        private static string? ReadStringField(string? body, string name)
        {
            if (string.IsNullOrWhiteSpace(body)) return null;
            try
            {
                using var doc = JsonDocument.Parse(body);
                if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                    doc.RootElement.TryGetProperty(name, out var e) && e.ValueKind == JsonValueKind.String &&
                    !string.IsNullOrWhiteSpace(e.GetString()))
                    return e.GetString();
            }
            catch (JsonException) { /* non-JSON body: the caller's status text is used */ }
            return null;
        }
    }

    // =====================================================================================
    // DTOs — field names exactly as in market_research/SPEC.md §1.2 (snake_case). Tolerant: unknown
    // fields are ignored (forward compatible). Everything nullable: a missing value is shown as
    // missing, never replaced by a default (no-fallback rule). Computed properties carry
    // [JsonIgnore] (PropertyNameCaseInsensitive collision bug).
    // =====================================================================================

    internal sealed class FeedsStatus
    {
        [JsonPropertyName("schema")] public int? Schema { get; set; }
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
        [JsonPropertyName("research")] public ResearchStateInfo? Research { get; set; }
        [JsonPropertyName("store")] public ResearchStoreInfo? Store { get; set; }
        [JsonPropertyName("tasks")] public List<ResearchTaskInfo>? Tasks { get; set; }
        [JsonPropertyName("feeds")] public List<FeedInfo>? Feeds { get; set; }
        [JsonPropertyName("live_count")] public int? LiveCount { get; set; }
        [JsonPropertyName("feed_count")] public int? FeedCount { get; set; }
        [JsonPropertyName("demand")] public ResearchDemandInfo? Demand { get; set; }
        [JsonPropertyName("budget")] public ResearchBudgetInfo? Budget { get; set; }
    }

    internal sealed class ResearchStateInfo
    {
        /// <summary>starting · ok · unavailable · stopped</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("since")] public string? Since { get; set; }
    }

    internal sealed class ResearchStoreInfo
    {
        /// <summary>ok · unavailable · not_opened</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("file")] public string? File { get; set; }
        [JsonPropertyName("schema_version")] public int? SchemaVersion { get; set; }
    }

    internal sealed class ResearchTaskInfo
    {
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        /// <summary>pending · running · restarting · failed · stopped</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("restarts")] public int? Restarts { get; set; }
        [JsonPropertyName("max_restarts")] public int? MaxRestarts { get; set; }
        [JsonPropertyName("last_error")] public string? LastError { get; set; }
        [JsonPropertyName("last_error_at")] public string? LastErrorAt { get; set; }
        [JsonPropertyName("next_restart_at")] public string? NextRestartAt { get; set; }
    }

    internal sealed class FeedInfo
    {
        /// <summary>stocks · crypto · news (fixed order)</summary>
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("feed_label")] public string? FeedLabel { get; set; }
        /// <summary>off · connecting · live · waiting · unavailable</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("attempt")] public int? Attempt { get; set; }
        [JsonPropertyName("connected_since")] public string? ConnectedSince { get; set; }
        [JsonPropertyName("last_message_at")] public string? LastMessageAt { get; set; }
        [JsonPropertyName("symbols")] public int? Symbols { get; set; }
        [JsonPropertyName("symbol_limit")] public int? SymbolLimit { get; set; }

        /// <summary>SPEC §1.5 tooltip, e.g. "Stocks · IEX · real time — off: Live data is not built yet (stage MR2)."</summary>
        [JsonIgnore]
        public string DescriptionText
        {
            get
            {
                var name = string.IsNullOrWhiteSpace(Label) ? (Id ?? "Feed") : Label!;
                if (!string.IsNullOrWhiteSpace(FeedLabel)) name += " · " + FeedLabel;
                var state = string.IsNullOrWhiteSpace(State) ? "state not reported" : State!;
                return string.IsNullOrWhiteSpace(Message) ? name + " — " + state : name + " — " + state + ": " + Message;
            }
        }
    }

    internal sealed class ResearchDemandInfo
    {
        [JsonPropertyName("views")] public int? Views { get; set; }
        [JsonPropertyName("alerts")] public int? Alerts { get; set; }
        [JsonPropertyName("streams_wanted")] public bool? StreamsWanted { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class ResearchBudgetInfo
    {
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("shared_per_minute")] public int? SharedPerMinute { get; set; }
        [JsonPropertyName("research_max_per_minute")] public int? ResearchMaxPerMinute { get; set; }
        [JsonPropertyName("research_used_last_minute")] public int? ResearchUsedLastMinute { get; set; }
        [JsonPropertyName("waiting_seconds")] public double? WaitingSeconds { get; set; }
    }

    // =====================================================================================
    // MR2a data DTOs — field names exactly as in market_research/SPEC.md "MR2a notes" (snake_case).
    // Same rules as above: unknown fields ignored, everything nullable (a missing value stays missing,
    // never a default), no computed properties without [JsonIgnore]. Times are UTC ISO strings;
    // the conversion to epoch ms for the chart happens ONLY in Chart/ChartHost.cs (SPEC §0 Time).
    // =====================================================================================

    internal sealed class AssetSearchResult
    {
        [JsonPropertyName("items")] public List<AssetItem>? Items { get; set; }
        [JsonPropertyName("refreshed_at")] public string? RefreshedAt { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
    }

    internal sealed class AssetItem
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        /// <summary>equity · crypto</summary>
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        /// <summary>stock · etf · crypto</summary>
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("exchange")] public string? Exchange { get; set; }
        [JsonPropertyName("tradable")] public bool? Tradable { get; set; }
    }

    /// <summary>SPEC §4.1 <c>Num</c>: a value WITH its feed; null value comes with a reason.</summary>
    internal sealed class NumValue
    {
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("feed")] public string? Feed { get; set; }
        [JsonPropertyName("feed_label")] public string? FeedLabel { get; set; }
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    /// <summary>The equities or crypto part of GET /research/market/status (also <c>snapshot.market</c>).</summary>
    internal sealed class MarketPart
    {
        /// <summary>equities: open · pre_market · after_hours · closed · holiday; crypto: open</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("session_date")] public string? SessionDate { get; set; }
        [JsonPropertyName("early_close")] public bool? EarlyClose { get; set; }
        [JsonPropertyName("opens_at")] public string? OpensAt { get; set; }
        [JsonPropertyName("closes_at")] public string? ClosesAt { get; set; }
        [JsonPropertyName("next_open_at")] public string? NextOpenAt { get; set; }
        [JsonPropertyName("next_close_at")] public string? NextCloseAt { get; set; }
        [JsonPropertyName("day_start")] public string? DayStart { get; set; }
        [JsonPropertyName("day_end")] public string? DayEnd { get; set; }
        [JsonPropertyName("tz")] public string? Tz { get; set; }
    }

    internal sealed class MarketStatus
    {
        [JsonPropertyName("equities")] public MarketPart? Equities { get; set; }
        [JsonPropertyName("crypto")] public MarketPart? Crypto { get; set; }
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
    }

    internal sealed class SymbolSnapshot
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("exchange")] public string? Exchange { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("last")] public NumValue? Last { get; set; }
        [JsonPropertyName("prev_close")] public NumValue? PrevClose { get; set; }
        [JsonPropertyName("change")] public NumValue? Change { get; set; }
        [JsonPropertyName("change_pct")] public NumValue? ChangePct { get; set; }
        [JsonPropertyName("day_low")] public NumValue? DayLow { get; set; }
        [JsonPropertyName("day_high")] public NumValue? DayHigh { get; set; }
        [JsonPropertyName("volume")] public NumValue? Volume { get; set; }
        [JsonPropertyName("market")] public MarketPart? Market { get; set; }
    }

    internal sealed class PerformanceEntry
    {
        [JsonPropertyName("pct")] public NumValue? Pct { get; set; }
        [JsonPropertyName("base_date")] public string? BaseDate { get; set; }
    }

    internal sealed class Range52Weeks
    {
        [JsonPropertyName("low")] public NumValue? Low { get; set; }
        [JsonPropertyName("high")] public NumValue? High { get; set; }
        /// <summary>MR4: (last − low) / (high − low) · 100, not clamped (Num, <c>computed</c>).</summary>
        [JsonPropertyName("position_pct")] public NumValue? PositionPct { get; set; }
        [JsonPropertyName("from")] public string? From { get; set; }
        [JsonPropertyName("to")] public string? To { get; set; }
    }

    internal sealed class SymbolStats
    {
        [JsonPropertyName("open")] public NumValue? Open { get; set; }
        [JsonPropertyName("vwap")] public NumValue? Vwap { get; set; }
        [JsonPropertyName("avg_volume_30d")] public NumValue? AvgVolume30d { get; set; }
        [JsonPropertyName("exchange")] public string? Exchange { get; set; }
        /// <summary>Keys "1W", "1M", "3M", "6M", "YTD", "1Y".</summary>
        [JsonPropertyName("performance")] public Dictionary<string, PerformanceEntry>? Performance { get; set; }
        [JsonPropertyName("range_52w")] public Range52Weeks? Range52w { get; set; }
        [JsonPropertyName("notes")] public List<string>? Notes { get; set; }
    }

    /// <summary>A bar as the API sends it (REST /bars, WS candle / sip_replace): <c>t</c> = bar start, UTC ISO.</summary>
    internal sealed class ApiBar
    {
        [JsonPropertyName("t")] public string? T { get; set; }
        [JsonPropertyName("o")] public double? O { get; set; }
        [JsonPropertyName("h")] public double? H { get; set; }
        [JsonPropertyName("l")] public double? L { get; set; }
        [JsonPropertyName("c")] public double? C { get; set; }
        [JsonPropertyName("v")] public double? V { get; set; }
        [JsonPropertyName("vwap")] public double? Vwap { get; set; }
        [JsonPropertyName("n")] public double? N { get; set; }
        /// <summary>sip · iex_tail · sip_daily · crypto</summary>
        [JsonPropertyName("feed")] public string? Feed { get; set; }
        [JsonPropertyName("final")] public bool? Final { get; set; }
        /// <summary>Session flag pre · rth · post (intraday stock bars; absent for crypto / 1D / 1W).</summary>
        [JsonPropertyName("s")] public string? S { get; set; }
    }

    internal sealed class BarsResponse
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("tf")] public string? Tf { get; set; }
        /// <summary>rth · eth (equities) · utc (crypto)</summary>
        [JsonPropertyName("session")] public string? Session { get; set; }
        [JsonPropertyName("bars")] public List<ApiBar>? Bars { get; set; }
        [JsonPropertyName("has_more_left")] public bool? HasMoreLeft { get; set; }
        [JsonPropertyName("sip_through")] public string? SipThrough { get; set; }
        [JsonPropertyName("tz")] public string? Tz { get; set; }
    }

    // =====================================================================================
    // MR4 workspace DTOs — field names exactly as in market_research/SPEC.md "MR4 backend notes".
    // Same rules: unknown fields ignored, everything nullable, no computed property without [JsonIgnore].
    // Watchlist names and asset names are untrusted text (plain TextBlocks only).
    // =====================================================================================

    internal sealed class WatchlistLimits
    {
        [JsonPropertyName("max_lists")] public int? MaxLists { get; set; }
        [JsonPropertyName("max_items")] public int? MaxItems { get; set; }
        [JsonPropertyName("max_name_chars")] public int? MaxNameChars { get; set; }
    }

    /// <summary>Answer of every watchlist route (GET / POST / PATCH / DELETE / items): <c>{lists, limits}</c> (+ <c>id</c> on POST).</summary>
    internal sealed class WatchlistsResponse
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("lists")] public List<WatchlistDto>? Lists { get; set; }
        [JsonPropertyName("limits")] public WatchlistLimits? Limits { get; set; }
    }

    internal sealed class WatchlistDto
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("position")] public int? Position { get; set; }
        [JsonPropertyName("collapsed")] public bool? Collapsed { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("items")] public List<WatchlistItemDto>? Items { get; set; }
    }

    internal sealed class WatchlistItemDto
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("position")] public int? Position { get; set; }
        [JsonPropertyName("added_at")] public string? AddedAt { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        /// <summary>equity · crypto</summary>
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("exchange")] public string? Exchange { get; set; }
        /// <summary>false = no longer in Alpaca's asset list (name/kind/exchange null; the row stays).</summary>
        [JsonPropertyName("covered")] public bool? Covered { get; set; }
    }

    /// <summary>GET /research/watchlists/{id}/quotes.</summary>
    internal sealed class WatchlistQuotes
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
        [JsonPropertyName("complete")] public bool? Complete { get; set; }
        [JsonPropertyName("retry_after_seconds")] public double? RetryAfterSeconds { get; set; }
        [JsonPropertyName("stream_limit")] public int? StreamLimit { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("items")] public List<WatchlistQuoteItem>? Items { get; set; }
    }

    internal sealed class WatchlistQuoteItem
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("covered")] public bool? Covered { get; set; }
        [JsonPropertyName("live")] public bool? Live { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("last")] public NumValue? Last { get; set; }
        [JsonPropertyName("prev_close")] public NumValue? PrevClose { get; set; }
        [JsonPropertyName("change")] public NumValue? Change { get; set; }
        [JsonPropertyName("change_pct")] public NumValue? ChangePct { get; set; }
    }

    /// <summary>GET/PUT /research/session.</summary>
    internal sealed class SessionState
    {
        [JsonPropertyName("schema")] public int? Schema { get; set; }
        [JsonPropertyName("tabs")] public List<SessionTab>? Tabs { get; set; }
        /// <summary>Index of the active tab; null iff no tabs.</summary>
        [JsonPropertyName("active")] public int? Active { get; set; }
        [JsonPropertyName("sidebar")] public SessionSidebar? Sidebar { get; set; }
        [JsonPropertyName("saved")] public bool? Saved { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        /// <summary>Set when a stored session could not be read and was ignored (answered like "nothing saved").</summary>
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("limits")] public SessionLimits? Limits { get; set; }
    }

    internal sealed class SessionTab
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("tf")] public string? Tf { get; set; }
        /// <summary>candles · line · area</summary>
        [JsonPropertyName("type")] public string? Type { get; set; }
        /// <summary>rth · eth (stocks/ETFs) · utc (crypto)</summary>
        [JsonPropertyName("session")] public string? Session { get; set; }
    }

    internal sealed class SessionSidebar
    {
        [JsonPropertyName("collapsed")] public bool? Collapsed { get; set; }
        /// <summary>overview · symbol · alerts (· portfolio · macro from MR8)</summary>
        [JsonPropertyName("section")] public string? Section { get; set; }
    }

    internal sealed class SessionLimits
    {
        [JsonPropertyName("max_tabs")] public int? MaxTabs { get; set; }
    }

    internal sealed class RecentSymbols
    {
        [JsonPropertyName("recent")] public List<string>? Recent { get; set; }
    }

    /// <summary>GET /research/palette.</summary>
    internal sealed class PaletteResult
    {
        [JsonPropertyName("q")] public string? Q { get; set; }
        [JsonPropertyName("recent")] public List<string>? Recent { get; set; }
        [JsonPropertyName("items")] public List<PaletteItem>? Items { get; set; }
        [JsonPropertyName("refreshed_at")] public string? RefreshedAt { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
    }

    internal sealed class PaletteItem
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("asset_class")] public string? AssetClass { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("exchange")] public string? Exchange { get; set; }
        [JsonPropertyName("tradable")] public bool? Tradable { get; set; }
        /// <summary>recent · exact · symbol_prefix · name_word · name_substring</summary>
        [JsonPropertyName("match")] public string? Match { get; set; }
        [JsonPropertyName("recent")] public bool? Recent { get; set; }
        [JsonPropertyName("watchlists")] public List<string>? Watchlists { get; set; }
    }

    // =====================================================================================
    // MR5 DTOs — exactly as "MR5 backend notes" (every key always present; nullable here = shown as missing).
    // Point `t` stays ISO here; ChartHost.cs converts to/from epoch ms (SPEC §0 Time).
    // =====================================================================================

    internal sealed class DrawingPointDto
    {
        [JsonPropertyName("t")] public string? T { get; set; }
        [JsonPropertyName("p")] public double? P { get; set; }
    }

    internal sealed class DrawingStyleDto
    {
        /// <summary>accent · up · down · caution · text · muted · blue · violet</summary>
        [JsonPropertyName("color")] public string? Color { get; set; }
        [JsonPropertyName("width")] public int? Width { get; set; }
        /// <summary>solid · dashed</summary>
        [JsonPropertyName("dash")] public string? Dash { get; set; }
        /// <summary>fibonacciLine only: null = default levels.</summary>
        [JsonPropertyName("levels")] public List<double>? Levels { get; set; }
    }

    internal sealed class PriceBasisDto
    {
        [JsonPropertyName("as_of")] public string? AsOf { get; set; }
        [JsonPropertyName("factor")] public double? Factor { get; set; }
        [JsonPropertyName("rescales")] public int? Rescales { get; set; }
    }

    internal sealed class DrawingDto
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("tool")] public string? Tool { get; set; }
        [JsonPropertyName("points")] public List<DrawingPointDto>? Points { get; set; }
        [JsonPropertyName("style")] public DrawingStyleDto? Style { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("text")] public string? Text { get; set; }
        [JsonPropertyName("locked")] public bool? Locked { get; set; }
        [JsonPropertyName("visible")] public bool? Visible { get; set; }
        /// <summary><c>"all"</c> or a list of timeframes — kept raw (two JSON shapes).</summary>
        [JsonPropertyName("timeframes")] public JsonElement Timeframes { get; set; }
        [JsonPropertyName("alert_id")] public string? AlertId { get; set; }
        /// <summary>Revision 3: the page's create id (idempotent POST), or null.</summary>
        [JsonPropertyName("client_id")] public string? ClientId { get; set; }
        [JsonPropertyName("price_basis")] public PriceBasisDto? PriceBasis { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }

        /// <summary>null = all timeframes; else the listed ones (invalid shape → null + IsTimeframesValid false).</summary>
        [JsonIgnore]
        public List<string>? TimeframeList
        {
            get
            {
                if (Timeframes.ValueKind != JsonValueKind.Array) return null;
                var list = new List<string>();
                foreach (var e in Timeframes.EnumerateArray())
                    if (e.ValueKind == JsonValueKind.String && e.GetString() is { } tf) list.Add(tf);
                return list;
            }
        }
    }

    internal sealed class SplitCheckDto
    {
        /// <summary>ok · failed · pending · not_applicable</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("checked_at")] public string? CheckedAt { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class DrawingsResponse
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("drawings")] public List<DrawingDto>? Drawings { get; set; }
        [JsonPropertyName("count")] public int? Count { get; set; }
        [JsonPropertyName("limit")] public int? Limit { get; set; }
        [JsonPropertyName("split_check")] public SplitCheckDto? SplitCheck { get; set; }
    }

    internal sealed class DrawingImportResult
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("imported")] public int? Imported { get; set; }
        [JsonPropertyName("replaced")] public int? Replaced { get; set; }
        [JsonPropertyName("drawings")] public List<DrawingDto>? Drawings { get; set; }
    }

    internal sealed class ChartSettingsDto
    {
        /// <summary>candles · line · area</summary>
        [JsonPropertyName("type")] public string? Type { get; set; }
        [JsonPropertyName("indicators")] public List<string>? Indicators { get; set; }
        /// <summary>off · weak · strong</summary>
        [JsonPropertyName("magnet")] public string? Magnet { get; set; }
        /// <summary>rth · eth (equities) · null (crypto)</summary>
        [JsonPropertyName("session")] public string? Session { get; set; }
    }

    internal sealed class ChartSettingsResponse
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("saved")] public bool? Saved { get; set; }
        [JsonPropertyName("settings")] public ChartSettingsDto? Settings { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
    }

    internal sealed class IndicatorInfoDto
    {
        [JsonPropertyName("key")] public string? Key { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        /// <summary>overlay · sub</summary>
        [JsonPropertyName("pane")] public string? Pane { get; set; }
        [JsonPropertyName("outputs")] public List<string>? Outputs { get; set; }
        [JsonPropertyName("params")] public JsonElement Params { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("warmup_bars")] public int? WarmupBars { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class IndicatorsResponse
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("tf")] public string? Tf { get; set; }
        [JsonPropertyName("session")] public string? Session { get; set; }
        [JsonPropertyName("t")] public List<string>? T { get; set; }
        [JsonPropertyName("series")] public Dictionary<string, List<double?>>? Series { get; set; }
        [JsonPropertyName("indicators")] public List<IndicatorInfoDto>? Indicators { get; set; }
        [JsonPropertyName("has_more_left")] public bool? HasMoreLeft { get; set; }
        [JsonPropertyName("sip_through")] public string? SipThrough { get; set; }
        [JsonPropertyName("tz")] public string? Tz { get; set; }
        [JsonPropertyName("warmup_loaded")] public int? WarmupLoaded { get; set; }
        [JsonPropertyName("last_final")] public bool? LastFinal { get; set; }
    }

    /// <summary>A request body that is already serialized JSON (sent unchanged).</summary>
    internal sealed class RawJsonBody
    {
        public RawJsonBody(string json) { Json = json; }
        public string Json { get; }
    }

    // =====================================================================================
    // MR6 news / logos DTOs — field names exactly as in market_research/SPEC.md "MR6 backend notes".
    // Headline, summary, publisher are sanitised plain text from the backend — still shown ONLY in TextBlocks.
    // =====================================================================================

    /// <summary>GET /research/symbols/{s}/news.</summary>
    internal sealed class NewsList
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("source_label")] public string? SourceLabel { get; set; }
        [JsonPropertyName("items")] public List<NewsItem>? Items { get; set; }
        [JsonPropertyName("thumbnails")] public bool? Thumbnails { get; set; }
        [JsonPropertyName("fetched_at")] public string? FetchedAt { get; set; }
        [JsonPropertyName("has_more")] public bool? HasMore { get; set; }
        [JsonPropertyName("next_before")] public string? NextBefore { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    /// <summary>One news item (REST list and WS <c>news</c> message).</summary>
    internal sealed class NewsItem
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("headline")] public string? Headline { get; set; }
        [JsonPropertyName("summary")] public string? Summary { get; set; }
        [JsonPropertyName("publisher")] public string? Publisher { get; set; }
        [JsonPropertyName("source_label")] public string? SourceLabel { get; set; }
        /// <summary>http/https only (checked by the backend AND again before opening).</summary>
        [JsonPropertyName("url")] public string? Url { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("symbols")] public List<string>? Symbols { get; set; }
        [JsonPropertyName("images")] public List<NewsImage>? Images { get; set; }
    }

    internal sealed class NewsImage
    {
        /// <summary>thumb · small</summary>
        [JsonPropertyName("size")] public string? Size { get; set; }
        /// <summary>Backend proxy path <c>/research/news/images/&lt;64 hex&gt;</c> — never a third-party URL.</summary>
        [JsonPropertyName("path")] public string? Path { get; set; }
    }

    /// <summary>GET/PUT /research/news/settings.</summary>
    internal sealed class NewsSettings
    {
        [JsonPropertyName("thumbnails")] public bool? Thumbnails { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("source_label")] public string? SourceLabel { get; set; }
        [JsonPropertyName("logos")] public LogosState? Logos { get; set; }
    }

    internal sealed class LogosState
    {
        /// <summary>unknown · available · not_on_plan</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("checked_at")] public string? CheckedAt { get; set; }
    }

    /// <summary>Answer of <see cref="ResearchClient.GetBinaryAsync"/> (not JSON; never serialized).</summary>
    internal sealed class BinaryResult
    {
        public bool Ok { get; init; }
        public int Status { get; init; }
        public byte[]? Bytes { get; init; }
        public string? ContentType { get; init; }
        public string? Error { get; init; }
        public string? Kind { get; init; }
    }

    // =====================================================================================
    // MR7 alert DTOs — exactly as "MR7 backend notes" (every key always present).
    // =====================================================================================

    internal sealed class AlertParamsDto
    {
        [JsonPropertyName("level")] public double? Level { get; set; }
        /// <summary>up · down (· any for line_cross / day_move)</summary>
        [JsonPropertyName("direction")] public string? Direction { get; set; }
        [JsonPropertyName("drawing_id")] public string? DrawingId { get; set; }
        [JsonPropertyName("pct")] public double? Pct { get; set; }
    }

    internal sealed class AlertLastPrice
    {
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
    }

    internal sealed class AlertLevels
    {
        [JsonPropertyName("up")] public double? Up { get; set; }
        [JsonPropertyName("down")] public double? Down { get; set; }
    }

    internal sealed class AlertStatusDto
    {
        /// <summary>armed · waiting · cooldown · not_live · disabled · not_running</summary>
        [JsonPropertyName("state")] public string? State { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("live")] public bool? Live { get; set; }
        [JsonPropertyName("level")] public double? Level { get; set; }
        [JsonPropertyName("last_price")] public AlertLastPrice? LastPrice { get; set; }
        [JsonPropertyName("prev_close")] public double? PrevClose { get; set; }
        [JsonPropertyName("cooldown_until")] public string? CooldownUntil { get; set; }
        [JsonPropertyName("levels")] public AlertLevels? Levels { get; set; }
    }

    internal sealed class AlertDto
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        /// <summary>price_cross · line_cross · day_move · news</summary>
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("params")] public AlertParamsDto? Params { get; set; }
        [JsonPropertyName("cooldown_seconds")] public int? CooldownSeconds { get; set; }
        [JsonPropertyName("enabled")] public bool? Enabled { get; set; }
        [JsonPropertyName("drawing_id")] public string? DrawingId { get; set; }
        [JsonPropertyName("last_triggered_at")] public string? LastTriggeredAt { get; set; }
        [JsonPropertyName("disabled_reason")] public string? DisabledReason { get; set; }
        [JsonPropertyName("created_at")] public string? CreatedAt { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("status")] public AlertStatusDto? Status { get; set; }
    }

    internal sealed class AlertLimitsDto
    {
        [JsonPropertyName("max_alerts")] public int? MaxAlerts { get; set; }
        [JsonPropertyName("cooldown_default")] public int? CooldownDefault { get; set; }
        [JsonPropertyName("cooldown_min")] public int? CooldownMin { get; set; }
        [JsonPropertyName("cooldown_max")] public int? CooldownMax { get; set; }
        [JsonPropertyName("day_move_pct_min")] public double? DayMovePctMin { get; set; }
        [JsonPropertyName("day_move_pct_max")] public double? DayMovePctMax { get; set; }
    }

    internal sealed class AlertRuntimeDto
    {
        [JsonPropertyName("running")] public bool? Running { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
        [JsonPropertyName("store_error")] public string? StoreError { get; set; }
        [JsonPropertyName("active")] public int? Active { get; set; }
    }

    internal sealed class AlertsResponse
    {
        [JsonPropertyName("alerts")] public List<AlertDto>? Alerts { get; set; }
        [JsonPropertyName("limits")] public AlertLimitsDto? Limits { get; set; }
        [JsonPropertyName("runtime")] public AlertRuntimeDto? Runtime { get; set; }
    }

    internal sealed class AlertLogItem
    {
        [JsonPropertyName("id")] public long? Id { get; set; }
        [JsonPropertyName("alert_id")] public string? AlertId { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("detail")] public JsonElement Detail { get; set; }
    }

    internal sealed class AlertLogResponse
    {
        [JsonPropertyName("items")] public List<AlertLogItem>? Items { get; set; }
        [JsonPropertyName("limit")] public int? Limit { get; set; }
        [JsonPropertyName("retention_days")] public int? RetentionDays { get; set; }
    }

    // =====================================================================================
    // MR8 integration DTOs — field names exactly as in market_research/SPEC.md "MR8 backend notes".
    // =====================================================================================

    internal sealed class PortfolioState
    {
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("read_only")] public bool? ReadOnly { get; set; }
        [JsonPropertyName("paper")] public bool? Paper { get; set; }
        [JsonPropertyName("generated_at")] public string? GeneratedAt { get; set; }
        [JsonPropertyName("cash")] public double? Cash { get; set; }
        [JsonPropertyName("equity")] public PortfolioEquity? Equity { get; set; }
        [JsonPropertyName("starting_capital")] public double? StartingCapital { get; set; }
        [JsonPropertyName("positions")] public List<PortfolioPosition>? Positions { get; set; }
        [JsonPropertyName("totals")] public PortfolioTotals? Totals { get; set; }
        [JsonPropertyName("engine")] public PortfolioEngine? Engine { get; set; }
        [JsonPropertyName("marks_note")] public string? MarksNote { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class PortfolioEquity
    {
        [JsonPropertyName("value")] public double? Value { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("drawdown_pct")] public double? DrawdownPct { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class PortfolioPosition
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("qty")] public double? Qty { get; set; }
        /// <summary>long · short</summary>
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("avg_price")] public double? AvgPrice { get; set; }
        [JsonPropertyName("last_price")] public double? LastPrice { get; set; }
        [JsonPropertyName("market_value")] public double? MarketValue { get; set; }
        [JsonPropertyName("unrealized_pnl")] public double? UnrealizedPnl { get; set; }
        [JsonPropertyName("unrealized_pct")] public double? UnrealizedPct { get; set; }
        [JsonPropertyName("realized_pnl")] public double? RealizedPnl { get; set; }
        [JsonPropertyName("opened_at")] public string? OpenedAt { get; set; }
        [JsonPropertyName("updated_at")] public string? UpdatedAt { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class PortfolioTotals
    {
        [JsonPropertyName("positions")] public int? Positions { get; set; }
        [JsonPropertyName("long_mv")] public double? LongMv { get; set; }
        [JsonPropertyName("short_mv")] public double? ShortMv { get; set; }
        [JsonPropertyName("unrealized_pnl")] public double? UnrealizedPnl { get; set; }
        [JsonPropertyName("complete")] public bool? Complete { get; set; }
    }

    internal sealed class PortfolioEngine
    {
        [JsonPropertyName("autopilot")] public bool? Autopilot { get; set; }
        [JsonPropertyName("halted")] public bool? Halted { get; set; }
    }

    /// <summary>GET …/markers (symbol: insider/fomc/release) and GET /research/backtests/{run}/markers (fills, periods, drawdowns).</summary>
    internal sealed class MarkersResponse
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("run_id")] public string? RunId { get; set; }
        [JsonPropertyName("strategy_id")] public string? StrategyId { get; set; }
        [JsonPropertyName("from")] public string? From { get; set; }
        [JsonPropertyName("to")] public string? To { get; set; }
        [JsonPropertyName("items")] public List<MarkerItem>? Items { get; set; }
        [JsonPropertyName("truncated")] public bool? Truncated { get; set; }
        /// <summary>Part → reason (always <c>earnings</c> for symbol markers; <c>fills</c> for old run cards …).</summary>
        [JsonPropertyName("unavailable")] public Dictionary<string, string>? Unavailable { get; set; }
        [JsonPropertyName("coverage")] public Dictionary<string, string>? Coverage { get; set; }
    }

    internal sealed class MarkerItem
    {
        [JsonPropertyName("t")] public string? T { get; set; }
        [JsonPropertyName("t_end")] public string? TEnd { get; set; }
        /// <summary>insider · fomc · release · fill · period_best · period_worst · drawdown</summary>
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        /// <summary>buy · sell · short · cover · null</summary>
        [JsonPropertyName("side")] public string? Side { get; set; }
        [JsonPropertyName("label")] public string? Label { get; set; }
        [JsonPropertyName("detail")] public JsonElement? Detail { get; set; }
    }

    internal sealed class WatchlistUniverse
    {
        [JsonPropertyName("id")] public string? Id { get; set; }
        [JsonPropertyName("name")] public string? Name { get; set; }
        [JsonPropertyName("universe")] public UniverseSymbols? Universe { get; set; }
        [JsonPropertyName("count")] public int? Count { get; set; }
        [JsonPropertyName("asset_classes")] public List<string>? AssetClasses { get; set; }
        [JsonPropertyName("excluded")] public List<UniverseExcluded>? Excluded { get; set; }
        [JsonPropertyName("max_symbols")] public int? MaxSymbols { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("note")] public string? Note { get; set; }
    }

    internal sealed class UniverseSymbols
    {
        [JsonPropertyName("symbols")] public List<string>? Symbols { get; set; }
    }

    internal sealed class UniverseExcluded
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }
}

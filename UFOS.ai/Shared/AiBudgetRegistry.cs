using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using UFOS.ai.Logging;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Client side of the module backends' <c>GET/PUT /ai/budgets</c> (contract
    /// UFOS.ai/docs/ai-budgets-spec.md §4 + §6.1). Used by the MainWindow "AI budgets" window.
    ///
    /// Availability order of <see cref="LoadAsync"/>: backend folder not found → NotInstalled; no
    /// token file → NotRunning; GET /health fails (2 s) → NotRunning; GET /ai/budgets 401 → token
    /// re-read once, still 401 → NotRunning ("refresh"); 404 → Outdated; payload module ≠ ModuleId,
    /// schema ≠ 1, &gt; 256 KB, &gt; 64 budgets or unparsable → Invalid; other HTTP error / timeout →
    /// Error (backend text verbatim); else Ok.
    ///
    /// Security (§6.4): loopback URLs built from the provider's Port only (never from payload data),
    /// the per-start token (BackendAuth.ReadToken, read on EVERY call) only in the X-UFOS-Token header
    /// and never logged or shown, no proxy, no redirects, no cookies, no Origin header (PUT is refused
    /// by the backends when one is present), responses capped at 256 KB, all payload text sanitized to
    /// plain text. A PUT is only sent for a user edit (the window decides), one budget per request.
    ///
    /// New module? See IAiBudgetProvider / AiBudgetProviders (one entry there, nothing here).
    /// Linked into every module csproj (..\Shared\*.cs) — no main-app types.
    /// </summary>
    internal static class AiBudgetRegistry
    {
        public const int MaxPayloadBytes = 256 * 1024;
        public const int MaxBudgets = 64;
        public const int MaxKeyLength = 64;
        private const int MaxCostLogRefs = 16;
        private const string LogModule = "AI budgets";

        private static readonly TimeSpan HealthTimeout = TimeSpan.FromSeconds(2);
        private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(10);

        private static readonly HttpClient Http = new(new SocketsHttpHandler
        {
            UseProxy = false,
            AllowAutoRedirect = false,
            UseCookies = false,
        })
        {
            // Per-request timeouts via linked CancellationTokenSources (2 s health, 10 s budgets).
            Timeout = Timeout.InfiniteTimeSpan,
        };

        private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNameCaseInsensitive = false };

        private static readonly Lazy<IReadOnlyList<IAiBudgetProvider>> LazyProviders = new(BuildProviders);

        /// <summary>= <see cref="AiBudgetProviders.All"/>, checked at first use (duplicate/empty module
        /// ids or an invalid port throw InvalidOperationException — a programming error).</summary>
        public static IReadOnlyList<IAiBudgetProvider> Providers => LazyProviders.Value;

        private static IReadOnlyList<IAiBudgetProvider> BuildProviders()
        {
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var p in AiBudgetProviders.All)
            {
                if (string.IsNullOrWhiteSpace(p.ModuleId) || !seen.Add(p.ModuleId))
                    throw new InvalidOperationException($"AiBudgetProviders: duplicate or empty module id '{p.ModuleId}'.");
                if (p is IAiBudgetLocalSource) continue;   // app-local: no port, no backend folder
                if (p.Port is < 1 or > 65535)
                    throw new InvalidOperationException($"AiBudgetProviders: invalid port {p.Port} for '{p.ModuleId}'.");
                if (p.BackendRelativeDir is null || p.BackendRelativeDir.Length == 0)
                    throw new InvalidOperationException($"AiBudgetProviders: no backend folder for '{p.ModuleId}'.");
            }
            return AiBudgetProviders.All;
        }

        // ===================== Texts =====================

        /// <summary>"Hedge Fund backend not running — start the module to edit its budgets" (contract §0).</summary>
        public static string NotRunningText(IAiBudgetProvider p) =>
            $"{p.Label} backend not running — start the module to edit its budgets";

        private static string NotInstalledText(IAiBudgetProvider p) =>
            $"{p.Label} backend not installed — folder {string.Join("\\", p.BackendRelativeDir)} (with main.py) was not found next to the app";

        private static string OutdatedText(IAiBudgetProvider p) =>
            $"{p.Label} backend is older than this app (no AI budget API) — restart the module to edit its budgets";

        // ===================== GET =====================

        /// <summary>Loads one module's budgets. Never throws except OperationCanceledException for
        /// <paramref name="ct"/>.</summary>
        public static async Task<AiBudgetModuleState> LoadAsync(IAiBudgetProvider p, CancellationToken ct)
        {
            if (p is null) throw new ArgumentNullException(nameof(p));
            if (p is IAiBudgetLocalSource local) return await LoadLocalAsync(local, ct).ConfigureAwait(false);
            var dir = BackendAuth.FindBackendDir(p.BackendRelativeDir);
            if (dir is null) return new AiBudgetModuleState(p, AiBudgetAvailability.NotInstalled, NotInstalledText(p), null);

            var token = BackendAuth.ReadToken(dir);
            if (token is null) return new AiBudgetModuleState(p, AiBudgetAvailability.NotRunning, NotRunningText(p), null);

            try
            {
                if (!await IsHealthyAsync(p, ct).ConfigureAwait(false))
                    return new AiBudgetModuleState(p, AiBudgetAvailability.NotRunning, NotRunningText(p), null);

                for (var attempt = 0; ; attempt++)
                {
                    var r = await SendAsync(HttpMethod.Get, p, token, null, RequestTimeout, ct).ConfigureAwait(false);
                    if (r.Status == HttpStatusCode.Unauthorized)
                    {
                        // The backend restarted (new token) between our read and the request: re-read once.
                        token = BackendAuth.ReadToken(dir);
                        if (attempt == 0 && token is not null) continue;
                        return new AiBudgetModuleState(p, AiBudgetAvailability.NotRunning,
                            NotRunningText(p) + " (the backend refused the API token — it may have just restarted; press Refresh)", null);
                    }
                    if (r.Status == HttpStatusCode.NotFound)
                        return new AiBudgetModuleState(p, AiBudgetAvailability.Outdated, OutdatedText(p), null);
                    if (r.TooLarge)
                        return new AiBudgetModuleState(p, AiBudgetAvailability.Invalid,
                            $"{p.Label} backend sent an unusable budget list (larger than 256 KB) — see its backend.log", null);
                    if (!IsSuccess(r.Status))
                        return new AiBudgetModuleState(p, AiBudgetAvailability.Error,
                            $"{p.Label} backend answered HTTP {(int)r.Status}: {ErrorText(r.Body)}", null);

                    if (!TryParsePayload(p, r.Body, out var payload, out var reason))
                        return new AiBudgetModuleState(p, AiBudgetAvailability.Invalid,
                            $"{p.Label} backend sent an unusable budget list ({reason}) — restart the module; see its backend.log", null);
                    return new AiBudgetModuleState(p, AiBudgetAvailability.Ok, string.Empty, payload);
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (OperationCanceledException)
            {
                return new AiBudgetModuleState(p, AiBudgetAvailability.Error,
                    $"{p.Label} backend did not answer within {RequestTimeout.TotalSeconds:0} s — press Refresh", null);
            }
            catch (HttpRequestException)
            {
                // /health answered a moment ago, the budget request could not connect: it just stopped.
                return new AiBudgetModuleState(p, AiBudgetAvailability.NotRunning, NotRunningText(p), null);
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, $"Loading the AI budgets of {p.ModuleId} failed", ex); } catch { }
                return new AiBudgetModuleState(p, AiBudgetAvailability.Error,
                    $"{p.Label}: the AI budgets could not be loaded ({ex.GetType().Name}) — see the Backend Log", null);
            }
        }

        // ===================== PUT =====================

        /// <summary>
        /// Stores ONE budget (<c>PUT /ai/budgets {"key", "limit_usd"}</c>). Success carries the
        /// backend's payload (re-render from it, never from the typed value). Failure carries the
        /// backend's text verbatim: 400 → <c>errors.limit_usd</c> (else the other field errors),
        /// 403/503/other → <c>error</c>. Never throws except OperationCanceledException for <paramref name="ct"/>.
        /// </summary>
        public static async Task<AiBudgetPutResult> PutAsync(IAiBudgetProvider p, string key, decimal limitUsd, CancellationToken ct)
        {
            if (p is null) throw new ArgumentNullException(nameof(p));
            if (string.IsNullOrEmpty(key) || key.Length > MaxKeyLength)
                return AiBudgetPutResult.Failure(null, "invalid budget key");
            if (p is IAiBudgetLocalSource local)
            {
                try
                {
                    return await Task.Run(() => local.LocalPut(key, limitUsd), ct).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception ex)
                {
                    try { AppLog.Warn(LogModule, $"Saving app-local AI budget {p.ModuleId}/{key} failed", ex); } catch { }
                    return AiBudgetPutResult.Failure(null, $"The budget could not be saved ({ex.GetType().Name}) — see the Backend Log.");
                }
            }

            var dir = BackendAuth.FindBackendDir(p.BackendRelativeDir);
            if (dir is null) return AiBudgetPutResult.Failure(null, NotInstalledText(p));
            var token = BackendAuth.ReadToken(dir);
            if (token is null) return AiBudgetPutResult.Failure(null, NotRunningText(p));

            byte[] body;
            using (var buffer = new MemoryStream())
            {
                using (var writer = new Utf8JsonWriter(buffer))
                {
                    writer.WriteStartObject();
                    writer.WriteString("key", key);
                    writer.WriteNumber("limit_usd", limitUsd);
                    writer.WriteEndObject();
                }
                body = buffer.ToArray();
            }

            try
            {
                for (var attempt = 0; ; attempt++)
                {
                    var r = await SendAsync(HttpMethod.Put, p, token, body, RequestTimeout, ct).ConfigureAwait(false);
                    var code = (int)r.Status;
                    if (r.Status == HttpStatusCode.Unauthorized)
                    {
                        // Refused by the token check before anything was changed -> safe to retry once.
                        token = BackendAuth.ReadToken(dir);
                        if (attempt == 0 && token is not null) continue;
                        return AiBudgetPutResult.Failure(code,
                            $"The {p.Label} backend refused the API token (it may have just restarted) — press Refresh and try again.");
                    }
                    if (r.TooLarge)
                        return AiBudgetPutResult.Failure(code, "The backend's answer was larger than 256 KB — press Refresh to see the stored value.");
                    if (IsSuccess(r.Status))
                    {
                        if (TryParsePayload(p, r.Body, out var payload, out var reason) && payload is not null)
                            return AiBudgetPutResult.Success(payload);
                        return AiBudgetPutResult.Failure(code,
                            $"The change was sent but the answer was unusable ({reason}) — press Refresh to see the stored value.");
                    }
                    if (r.Status == HttpStatusCode.NotFound)
                        return AiBudgetPutResult.Failure(code, OutdatedText(p));
                    try { AppLog.Info(LogModule, $"PUT /ai/budgets {p.ModuleId}/{key} refused: HTTP {code}"); } catch { }
                    return AiBudgetPutResult.Failure(code, ErrorText(r.Body));
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (OperationCanceledException)
            {
                return AiBudgetPutResult.Failure(null,
                    $"The {p.Label} backend did not answer within {RequestTimeout.TotalSeconds:0} s — the change may or may not have been stored; press Refresh to see the stored value.");
            }
            catch (HttpRequestException)
            {
                return AiBudgetPutResult.Failure(null,
                    $"Could not reach the {p.Label} backend — it may have stopped. Start the module and try again.");
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, $"Saving AI budget {p.ModuleId}/{key} failed", ex); } catch { }
                return AiBudgetPutResult.Failure(null, $"The budget could not be saved ({ex.GetType().Name}) — see the Backend Log.");
            }
        }

        // ===================== App-local providers (§5 step 2b) =====================

        private static async Task<AiBudgetModuleState> LoadLocalAsync(IAiBudgetLocalSource local, CancellationToken ct)
        {
            try
            {
                var payload = await Task.Run(() => local.LocalPayload(), ct).ConfigureAwait(false);
                if (payload.Schema != 1 || !string.Equals(payload.Module, local.ModuleId, StringComparison.Ordinal) ||
                    payload.Budgets is null || payload.Budgets.Count > MaxBudgets)
                {
                    return new AiBudgetModuleState(local, AiBudgetAvailability.Invalid, $"{local.Label}: unusable budget list", null);
                }
                return new AiBudgetModuleState(local, AiBudgetAvailability.Ok, string.Empty, payload);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, $"Loading the app-local AI budgets of {local.ModuleId} failed", ex); } catch { }
                return new AiBudgetModuleState(local, AiBudgetAvailability.Error,
                    $"{local.Label}: the AI budgets could not be loaded ({ex.GetType().Name}) — see the Backend Log", null);
            }
        }

        // ===================== HTTP =====================

        private sealed record RawResponse(HttpStatusCode Status, byte[]? Body, bool TooLarge);

        private static bool IsSuccess(HttpStatusCode status) => (int)status is >= 200 and <= 299;

        private static Uri BuildUri(IAiBudgetProvider p, string path) =>
            new UriBuilder(Uri.UriSchemeHttp, "127.0.0.1", p.Port, path).Uri;

        private static async Task<bool> IsHealthyAsync(IAiBudgetProvider p, CancellationToken ct)
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(HealthTimeout);
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, BuildUri(p, "/health"));
                using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, cts.Token).ConfigureAwait(false);
                return resp.IsSuccessStatusCode;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception)
            {
                return false;   // refused connection / timeout -> not running
            }
        }

        /// <summary>One request to /ai/budgets incl. reading the body (≤ 256 KB) within <paramref name="timeout"/>.</summary>
        private static async Task<RawResponse> SendAsync(HttpMethod method, IAiBudgetProvider p, string token,
                                                         byte[]? jsonBody, TimeSpan timeout, CancellationToken ct)
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(timeout);
            using var req = new HttpRequestMessage(method, BuildUri(p, "/ai/budgets"));
            req.Headers.TryAddWithoutValidation(BackendAuth.HeaderName, token);
            req.Headers.Accept.ParseAdd("application/json");
            if (jsonBody is not null)
            {
                req.Content = new ByteArrayContent(jsonBody);
                req.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json") { CharSet = "utf-8" };
            }

            using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, cts.Token).ConfigureAwait(false);
            if (resp.Content.Headers.ContentLength is long length && length > MaxPayloadBytes)
                return new RawResponse(resp.StatusCode, null, true);

            using var stream = await resp.Content.ReadAsStreamAsync(cts.Token).ConfigureAwait(false);
            using var buffer = new MemoryStream();
            var chunk = new byte[16 * 1024];
            int read;
            while ((read = await stream.ReadAsync(chunk.AsMemory(0, chunk.Length), cts.Token).ConfigureAwait(false)) > 0)
            {
                buffer.Write(chunk, 0, read);
                if (buffer.Length > MaxPayloadBytes) return new RawResponse(resp.StatusCode, null, true);
            }
            return new RawResponse(resp.StatusCode, buffer.ToArray(), false);
        }

        // ===================== Parsing =====================

        /// <summary>Validates + sanitizes a module payload (schema 1, module id, ≤ 64 budgets, keys).</summary>
        private static bool TryParsePayload(IAiBudgetProvider p, byte[]? body, out AiBudgetPayload? payload, out string reason)
        {
            payload = null;
            reason = string.Empty;
            if (body is null || body.Length == 0) { reason = "empty answer"; return false; }

            AiBudgetPayload? parsed;
            try
            {
                parsed = JsonSerializer.Deserialize<AiBudgetPayload>(body, JsonOptions);
            }
            catch (Exception ex) when (ex is JsonException or NotSupportedException or InvalidOperationException or ArgumentException)
            {
                reason = "not a schema-1 budget list";
                return false;
            }
            if (parsed is null) { reason = "empty answer"; return false; }
            if (parsed.Schema != 1) { reason = $"schema {parsed.Schema}, this app understands schema 1"; return false; }
            if (!string.Equals(parsed.Module, p.ModuleId, StringComparison.Ordinal))
            {
                reason = $"module \"{AiBudgetText.Clean(parsed.Module, 40)}\", expected \"{p.ModuleId}\"";
                return false;
            }
            if (parsed.Budgets is null) { reason = "no budget list"; return false; }
            if (parsed.Budgets.Count > MaxBudgets) { reason = $"more than {MaxBudgets} budgets"; return false; }

            var keys = new HashSet<string>(StringComparer.Ordinal);
            foreach (var e in parsed.Budgets)
            {
                if (e is null) { reason = "an empty budget entry"; return false; }
                var key = e.Key;
                if (string.IsNullOrEmpty(key) || key.Length > MaxKeyLength || AiBudgetText.Clean(key, MaxKeyLength) != key)
                {
                    reason = "a budget without a valid key";
                    return false;
                }
                if (!keys.Add(key)) { reason = $"duplicate budget key \"{key}\""; return false; }

                e.Label = AiBudgetText.Clean(e.Label);
                if (string.IsNullOrEmpty(e.Label)) e.Label = key;
                e.Group = AiBudgetText.Clean(e.Group, 80);
                e.Period = AiBudgetText.Clean(e.Period, 20);
                e.PeriodId = AiBudgetText.Clean(e.PeriodId, 20);
                e.Status = AiBudgetText.Clean(e.Status, 20);
                e.Message = AiBudgetText.Clean(e.Message);
                e.Applies = AiBudgetText.Clean(e.Applies, 20);
                e.Notes = AiBudgetText.Clean(e.Notes);
                if (e.CostLog is not null)
                {
                    e.CostLog = e.CostLog
                        .Where(c => c is not null && !string.IsNullOrEmpty(c.Module) && !string.IsNullOrEmpty(c.Feature))
                        .Take(MaxCostLogRefs)
                        .Select(c => new AiBudgetCostLogRef { Module = AiBudgetText.Clean(c.Module, 64), Feature = AiBudgetText.Clean(c.Feature, 64) })
                        .ToList();
                }
            }

            parsed.Label = AiBudgetText.Clean(parsed.Label, 80);
            parsed.GeneratedAt = AiBudgetText.Clean(parsed.GeneratedAt, 40);
            parsed.Day = AiBudgetText.Clean(parsed.Day, 20);
            parsed.Month = AiBudgetText.Clean(parsed.Month, 20);
            if (parsed.Changed is not null) parsed.Changed.Key = AiBudgetText.Clean(parsed.Changed.Key, MaxKeyLength);
            payload = parsed;
            return true;
        }

        /// <summary>
        /// Readable text of an error body {error, errors: {field: message}} (verbatim, sanitized):
        /// errors.limit_usd when present, else "error (field: message; …)", else error.
        /// </summary>
        private static string ErrorText(byte[]? body)
        {
            const string none = "no error text in the answer (see the module's backend.log)";
            if (body is null || body.Length == 0) return none;
            try
            {
                using var doc = JsonDocument.Parse(body);
                var root = doc.RootElement;
                if (root.ValueKind != JsonValueKind.Object) return none;

                string? error = root.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String
                    ? AiBudgetText.Clean(e.GetString()) : null;

                if (root.TryGetProperty("errors", out var errs) && errs.ValueKind == JsonValueKind.Object)
                {
                    if (errs.TryGetProperty("limit_usd", out var lim) && lim.ValueKind == JsonValueKind.String)
                    {
                        var text = AiBudgetText.Clean(lim.GetString());
                        if (!string.IsNullOrEmpty(text)) return text;
                    }
                    var parts = new List<string>();
                    foreach (var prop in errs.EnumerateObject())
                    {
                        if (parts.Count >= 4) break;
                        if (prop.Value.ValueKind != JsonValueKind.String) continue;
                        var field = AiBudgetText.Clean(prop.Name, 40);
                        var msg = AiBudgetText.Clean(prop.Value.GetString(), 120);
                        parts.Add(field == "_" ? msg ?? string.Empty : $"{field}: {msg}");
                    }
                    if (parts.Count > 0)
                        return (string.IsNullOrEmpty(error) ? string.Empty : error + " — ") + string.Join("; ", parts);
                }
                return string.IsNullOrEmpty(error) ? none : error;
            }
            catch (JsonException)
            {
                return none;
            }
        }
    }
}

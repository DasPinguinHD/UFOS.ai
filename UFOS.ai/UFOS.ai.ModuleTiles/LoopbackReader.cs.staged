using System;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace UFOS.ai.ModuleTiles
{
    /// <summary>Answer of a backend's GET /health.</summary>
    /// <param name="Running">The status code was 2xx within the timeout.</param>
    /// <param name="Sandbox">The body said <c>"mode": "sandbox"</c> (sandbox profile, 2026-10-23).</param>
    public sealed record LoopbackHealth(bool Running, bool Sandbox);

    /// <summary>Answer of a token GET: Body is the JSON text of a 2xx answer, otherwise Problem says why (user-facing).</summary>
    public sealed record LoopbackResponse(int Status, string? Body, string? Problem)
    {
        public bool Ok => Problem == null && Body != null;
    }

    /// <summary>
    /// Read-only access of a tile provider to a RUNNING module backend on 127.0.0.1 (TILES_API §3): GET /health without a token
    /// (800 ms) decides "running" — the tile NEVER starts a backend; then GET requests with the backend's session token
    /// (X-UFOS-Token, read from its data/api-token file by the caller's <c>readToken</c> on every call; a 401 re-reads it once).
    /// ≤ 3 s per request, no retry, no proxy, no redirects, no cookies, answers ≤ 256 KB. Only GET exists here.
    /// </summary>
    public sealed class LoopbackReader
    {
        public const string TokenHeader = "X-UFOS-Token";
        private static readonly TimeSpan HealthTimeout = TimeSpan.FromMilliseconds(800);
        private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(3);
        private const int MaxBodyBytes = 256 * 1024;

        private static readonly HttpClient Http = new(new SocketsHttpHandler
        {
            UseProxy = false,
            AllowAutoRedirect = false,
            UseCookies = false,
        })
        {
            Timeout = Timeout.InfiniteTimeSpan,   // per-request timeouts via linked CancellationTokenSources
        };

        private readonly int _port;
        private readonly Func<string?> _readToken;
        private readonly string _backendName;

        /// <param name="backendName">For messages, e.g. "Global Monitoring backend".</param>
        public LoopbackReader(int port, Func<string?> readToken, string backendName)
        {
            _port = port;
            _readToken = readToken ?? throw new ArgumentNullException(nameof(readToken));
            _backendName = backendName;
        }

        private Uri BuildUri(string pathAndQuery) => new("http://127.0.0.1:" + _port + pathAndQuery, UriKind.Absolute);

        public async Task<LoopbackHealth> HealthAsync(CancellationToken ct)
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(HealthTimeout);
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, BuildUri("/health"));
                using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, cts.Token).ConfigureAwait(false);
                if (!resp.IsSuccessStatusCode) return new LoopbackHealth(false, false);
                var sandbox = false;
                try
                {
                    var bytes = await resp.Content.ReadAsByteArrayAsync(cts.Token).ConfigureAwait(false);
                    if (bytes.Length > 0 && bytes.Length <= 4096)
                    {
                        using var doc = JsonDocument.Parse(bytes);
                        sandbox = doc.RootElement.ValueKind == JsonValueKind.Object
                                  && doc.RootElement.TryGetProperty("mode", out var m) && m.ValueKind == JsonValueKind.String
                                  && string.Equals(m.GetString(), "sandbox", StringComparison.OrdinalIgnoreCase);
                    }
                }
                catch (JsonException) { /* an older plain-text body: running, mode unknown = not sandbox */ }
                return new LoopbackHealth(true, sandbox);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception)
            {
                return new LoopbackHealth(false, false);   // refused / timeout → not running
            }
        }

        public async Task<LoopbackResponse> GetJsonAsync(string pathAndQuery, CancellationToken ct)
        {
            var token = _readToken();
            if (token == null) return new LoopbackResponse(0, null, "the " + _backendName + " has no session token yet");
            var r = await SendAsync(pathAndQuery, token, ct).ConfigureAwait(false);
            if (r.Status == (int)HttpStatusCode.Unauthorized && _readToken() is string fresh && fresh != token)
                r = await SendAsync(pathAndQuery, fresh, ct).ConfigureAwait(false);   // restarted meanwhile (new token)
            return r;
        }

        private async Task<LoopbackResponse> SendAsync(string pathAndQuery, string token, CancellationToken ct)
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(RequestTimeout);
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, BuildUri(pathAndQuery));
                req.Headers.TryAddWithoutValidation(TokenHeader, token);
                req.Headers.Accept.ParseAdd("application/json");
                using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, cts.Token).ConfigureAwait(false);
                var status = (int)resp.StatusCode;
                if (resp.Content.Headers.ContentLength is long len && len > MaxBodyBytes)
                    return new LoopbackResponse(status, null, "the answer is larger than " + MaxBodyBytes / 1024 + " KB");
                var bytes = await resp.Content.ReadAsByteArrayAsync(cts.Token).ConfigureAwait(false);
                if (bytes.Length > MaxBodyBytes) return new LoopbackResponse(status, null, "the answer is larger than " + MaxBodyBytes / 1024 + " KB");
                var text = Encoding.UTF8.GetString(bytes);
                if (status < 200 || status > 299)
                {
                    var reason = status switch
                    {
                        401 => "the " + _backendName + "'s session token did not match",
                        404 => "the running " + _backendName + " has no " + pathAndQuery.Split('?')[0] + " route — restart it from its window",
                        _ => "HTTP " + status,
                    };
                    return new LoopbackResponse(status, null, reason);
                }
                return new LoopbackResponse(status, text, null);
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                return new LoopbackResponse(0, null, "the " + _backendName + " did not answer within " + RequestTimeout.TotalSeconds + " s");
            }
            catch (HttpRequestException)
            {
                return new LoopbackResponse(0, null, "the " + _backendName + " stopped answering");
            }
        }
    }
}

using System;
using System.Buffers;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Net.NetworkInformation;
using System.Net.Http;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace UFOS.ai
{
    public class WebSocketService : IDisposable
    {
        private readonly Uri _uri;
        private ClientWebSocket? _ws;
        private CancellationTokenSource? _cts;
        private static readonly string _debugLogPath = Path.Combine(Path.GetTempPath(), "UFOS.ai_ws_debug.log");

        // Typed events
        public event Action<TickerMessage>? OnTicker;
        public event Action<int>? OnConfidence;
        public event Action<VerdictMessage>? OnVerdict;
        public event Action<MarketUpdateMessage>? OnMarketUpdate;
        public event Action<string>? OnTranscript;
        public event Action<string>? OnConnectionStatus;
        public event Action<string>? OnRawMessage;

        public WebSocketService(string url)
        {
            _uri = new Uri(url);
        }

        private void EmitConnectionStatus(string s)
        {
            try { File.AppendAllText(_debugLogPath, DateTime.Now.ToString("o") + " [WS STATUS] " + s + "\n"); } catch { }
            try { OnConnectionStatus?.Invoke(s); } catch { }
        }

        private void EmitRaw(string raw)
        {
            try { File.AppendAllText(_debugLogPath, DateTime.Now.ToString("o") + " [WS RAW] " + raw + "\n"); } catch { }
            try { OnRawMessage?.Invoke(raw); } catch { }
        }

        public async Task StartAsync(CancellationToken cancellation = default)
        {
            _cts = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
            _ws = new ClientWebSocket();
            try
            {
                EmitConnectionStatus("Starting");
                await ConnectWithRetryAsync(_cts.Token).ConfigureAwait(false);
                _ = Task.Run(() => ReceiveLoop(_cts.Token));
            }
            catch (Exception ex)
            {
                EmitRaw($"[WS START ERROR] {ex.ToString()}");
                EmitConnectionStatus("Error");
                throw;
            }
        }

        private async Task ConnectWithRetryAsync(CancellationToken ct)
        {
            var delay = TimeSpan.FromSeconds(1);
            var attempt = 0;
            while (!ct.IsCancellationRequested)
            {
                attempt++;
                try
                {
                    // Quick health-check to avoid futile TCP connect attempts when backend has died.
                    try
                    {
                        using var hc = new HttpClient();
                        hc.Timeout = TimeSpan.FromSeconds(2);
                        var hresp = await hc.GetAsync("http://127.0.0.1:8766/health", ct).ConfigureAwait(false);
                        if (!hresp.IsSuccessStatusCode)
                        {
                            EmitRaw($"[WS HEALTH] backend unhealthy, status={hresp.StatusCode}");
                            try { await Task.Delay(delay, ct).ConfigureAwait(false); } catch { }
                            delay = TimeSpan.FromSeconds(Math.Min(30, delay.TotalSeconds * 2));
                            continue;
                        }
                    }
                    catch (OperationCanceledException) { throw; }
                    catch (Exception he)
                    {
                        // health endpoint not reachable — skip connect attempt and retry after backoff
                        EmitRaw($"[WS HEALTH ERR] {he.GetType().Name}: {he.Message}");
                        try { await Task.Delay(delay, ct).ConfigureAwait(false); } catch { }
                        delay = TimeSpan.FromSeconds(Math.Min(30, delay.TotalSeconds * 2));
                        continue;
                    }

                    // Ensure we use a fresh ClientWebSocket for each connect attempt.
                    try { _ws?.Dispose(); } catch { }
                    _ws = new ClientWebSocket();

                    try { EmitRaw($"[WS CONNECT ATTEMPT] {attempt} -> {_uri}"); } catch { }

                    // Wrap ConnectAsync with a timeout to avoid indefinite hangs
                    var linked = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    linked.CancelAfter(TimeSpan.FromSeconds(10));
                    try
                    {
                        await _ws.ConnectAsync(_uri, linked.Token).ConfigureAwait(false);
                    }
                    finally
                    {
                        try { linked.Dispose(); } catch { }
                    }

                    EmitConnectionStatus("Connected");

                    // Try to send a small ping to verify send path works and provoke server-side reaction if any
                    try
                    {
                        var ping = Encoding.UTF8.GetBytes("{\"type\":\"ping\"}");
                        await _ws.SendAsync(new ArraySegment<byte>(ping), WebSocketMessageType.Text, true, ct).ConfigureAwait(false);
                        try { EmitRaw("[WS SENT PING]"); } catch { }
                    }
                    catch (Exception sex)
                    {
                        try { EmitRaw($"[WS SEND ERROR] {sex.GetType().Name}: {sex.ToString()}"); } catch { }
                    }

                    return;
                }
                catch (Exception ex)
                {
                    // report connecting + error details for diagnostics
                    EmitConnectionStatus("Connecting");
                    try { EmitRaw($"[WS CONNECT ERROR] attempt={attempt} ex={ex.ToString()}"); } catch { }
                    // also record current TCP listeners and connections to help debug intermittent refusal
                    try
                    {
                        var props = IPGlobalProperties.GetIPGlobalProperties();
                        var listeners = props.GetActiveTcpListeners();
                        var listenerInfo = string.Join(",", listeners.Select(l => $"{l.Address}:{l.Port}"));
                        EmitRaw($"[WS NETLISTENERS] {listenerInfo}");
                        var conns = props.GetActiveTcpConnections();
                        var connInfo = string.Join(",", conns.Select(c => $"{c.LocalEndPoint.Address}:{c.LocalEndPoint.Port}->{c.RemoteEndPoint.Address}:{c.RemoteEndPoint.Port}({c.State})"));
                        EmitRaw($"[WS NETCONNS] {connInfo}");
                    }
                    catch { }
                    try { await Task.Delay(delay, ct).ConfigureAwait(false); } catch { }
                    delay = TimeSpan.FromSeconds(Math.Min(30, delay.TotalSeconds * 2));
                }
            }
        }

        private async Task ReceiveLoop(CancellationToken ct)
        {
            var buffer = new byte[8192];
            try
            {
                while (!ct.IsCancellationRequested && _ws != null && _ws.State == WebSocketState.Open)
                {
                    var seg = new ArraySegment<byte>(buffer);
                    var result = await _ws.ReceiveAsync(seg, ct).ConfigureAwait(false);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        try { await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, string.Empty, ct).ConfigureAwait(false); } catch { }
                        OnConnectionStatus?.Invoke("Disconnected");
                        // try to reconnect
                        await ConnectWithRetryAsync(ct).ConfigureAwait(false);
                        continue;
                    }

                    int count = result.Count;
                    while (!result.EndOfMessage)
                    {
                        if (count >= buffer.Length) Array.Resize(ref buffer, buffer.Length * 2);
                        seg = new ArraySegment<byte>(buffer, count, buffer.Length - count);
                        result = await _ws.ReceiveAsync(seg, ct).ConfigureAwait(false);
                        count += result.Count;
                    }

                    var message = Encoding.UTF8.GetString(buffer, 0, count);
                    EmitRaw(message);
                    TryDispatchMessage(message);
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception ex)
            {
                try { OnRawMessage?.Invoke($"[WS RECEIVE ERROR] {ex.GetType().Name}: {ex.Message}"); } catch { }
                OnConnectionStatus?.Invoke("Error");
            }
        }

        private void TryDispatchMessage(string raw)
        {
            try
            {
                using var doc = JsonDocument.Parse(raw);
                if (!doc.RootElement.TryGetProperty("type", out var typeEl))
                {
                    // no type, ignore or send raw
                    return;
                }

                var type = typeEl.GetString() ?? string.Empty;
                switch (type.ToLowerInvariant())
                {
                    case "market_update":
                        if (doc.RootElement.TryGetProperty("payload", out var payload) && payload.ValueKind == JsonValueKind.Object)
                        {
                            var mu = new MarketUpdateMessage();
                            if (payload.TryGetProperty("timestamp", out var mts) && mts.ValueKind == JsonValueKind.String)
                            {
                                if (System.DateTime.TryParse(mts.GetString(), out var parsed)) mu.Timestamp = parsed;
                            }
                            if (payload.TryGetProperty("quotes", out var quotes) && quotes.ValueKind == JsonValueKind.Object)
                            {
                                foreach (var prop in quotes.EnumerateObject())
                                {
                                    try
                                    {
                                        var q = new Quote();
                                        var obj = prop.Value;
                                        if (obj.TryGetProperty("price", out var p) && p.ValueKind == JsonValueKind.Number) q.Price = p.GetDouble();
                                        if (obj.TryGetProperty("change_percent", out var cp) && (cp.ValueKind == JsonValueKind.Number || cp.ValueKind == JsonValueKind.String))
                                        {
                                            try { q.ChangePercent = cp.GetDouble(); }
                                            catch
                                            {
                                                if (double.TryParse(cp.GetString(), out var parsed)) q.ChangePercent = parsed;
                                            }
                                        }
                                        mu.Quotes[prop.Name] = q;
                                    }
                                    catch { }
                                }
                            }
                            OnMarketUpdate?.Invoke(mu);
                        }
                        break;
                    case "transcript":
                        if (doc.RootElement.TryGetProperty("payload", out var tpayload) && tpayload.ValueKind == JsonValueKind.Object)
                        {
                            if (tpayload.TryGetProperty("text", out var ttext) && ttext.ValueKind == JsonValueKind.String)
                            {
                                try { OnTranscript?.Invoke(ttext.GetString() ?? string.Empty); } catch { }
                            }
                        }
                        break;
                    case "ticker":
                        var t = new TickerMessage();
                        if (doc.RootElement.TryGetProperty("index", out var idx)) t.Index = idx.GetInt32();
                        if (doc.RootElement.TryGetProperty("name", out var nm)) t.Name = nm.GetString() ?? string.Empty;
                        if (doc.RootElement.TryGetProperty("value", out var val)) t.Value = val.GetString() ?? string.Empty;
                        if (doc.RootElement.TryGetProperty("tendency", out var ten)) t.Tendency = ten.GetString() ?? string.Empty;
                        OnTicker?.Invoke(t);
                        break;
                    case "confidence":
                        if (doc.RootElement.TryGetProperty("value", out var vconf) && vconf.TryGetInt32(out var vi))
                        {
                            OnConfidence?.Invoke(vi);
                        }
                        break;
                    case "verdict":
                        var vm = new VerdictMessage();
                        // verdict payload may be nested under a "payload" object (backend broadcasts as {type, payload})
                        JsonElement payloadEl;
                        if (doc.RootElement.TryGetProperty("payload", out payloadEl) && payloadEl.ValueKind == JsonValueKind.Object)
                        {
                            var src = payloadEl;
                            if (src.TryGetProperty("verdict", out var v)) vm.Verdict = v.GetString() ?? string.Empty;
                            if (src.TryGetProperty("ticker", out var tt)) vm.Ticker = tt.GetString() ?? string.Empty;
                            if (src.TryGetProperty("reason", out var rr)) vm.Reason = rr.GetString() ?? string.Empty;
                            if (src.TryGetProperty("confidence", out var cv) && cv.TryGetInt32(out var civ)) vm.Confidence = civ;
                        }
                        else
                        {
                            // fallback to top-level properties
                            if (doc.RootElement.TryGetProperty("verdict", out var v)) vm.Verdict = v.GetString() ?? string.Empty;
                            if (doc.RootElement.TryGetProperty("ticker", out var tt)) vm.Ticker = tt.GetString() ?? string.Empty;
                            if (doc.RootElement.TryGetProperty("reason", out var rr)) vm.Reason = rr.GetString() ?? string.Empty;
                            if (doc.RootElement.TryGetProperty("confidence", out var cv) && cv.TryGetInt32(out var civ)) vm.Confidence = civ;
                        }
                        OnVerdict?.Invoke(vm);
                        break;
                    case "connection":
                        if (doc.RootElement.TryGetProperty("status", out var st))
                        {
                            OnConnectionStatus?.Invoke(st.GetString() ?? string.Empty);
                        }
                        break;
                    default:
                        // unknown type
                        break;
                }
            }
            catch { }
        }

        public async Task StopAsync()
        {
            try
            {
                _cts?.Cancel();
                if (_ws != null && (_ws.State == WebSocketState.Open || _ws.State == WebSocketState.CloseReceived))
                {
                    await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "client", CancellationToken.None).ConfigureAwait(false);
                }
            }
            catch { }
            finally
            {
                _ws?.Dispose();
                _ws = null;
                _cts?.Dispose();
                _cts = null;
            }
        }

        public void Dispose()
        {
            _ = StopAsync();
        }
    }

    // DTOs
    public class TickerMessage { public int Index { get; set; } public string Name { get; set; } = string.Empty; public string Value { get; set; } = string.Empty; public string Tendency { get; set; } = string.Empty; }
    public class VerdictMessage { public string Verdict { get; set; } = string.Empty; public string Ticker { get; set; } = string.Empty; public string Reason { get; set; } = string.Empty; public int Confidence { get; set; } }
    public class ConfidenceMessage { public int Value { get; set; } }
    public class Quote { public double Price { get; set; } public double ChangePercent { get; set; } }
    public class MarketUpdateMessage { public System.DateTime Timestamp { get; set; } public System.Collections.Generic.Dictionary<string, Quote> Quotes { get; set; } = new(); }
}


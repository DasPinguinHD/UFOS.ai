using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>Connection state of the one Research WebSocket (SPEC §4.3.1).</summary>
    internal enum ResearchSocketState
    {
        /// <summary>Not wanted (no visible Research view) — closed.</summary>
        Off,
        /// <summary>Connecting / waiting for the first <c>feeds</c> message after <c>hello</c>.</summary>
        Connecting,
        /// <summary>Handshake done (first <c>feeds</c> received, topics subscribed): live.</summary>
        Connected,
        /// <summary>Lost / not reachable: reconnect backoff, <see cref="ResearchSocket.Attempt"/> ≥ 1.</summary>
        Waiting,
        /// <summary>Refused (token / protocol error / Research unavailable / old backend): <see cref="ResearchSocket.Message"/>.</summary>
        Refused,
    }

    /// <summary>
    /// The ONE client of <c>ws://127.0.0.1:8867/research/ws</c> per app (SPEC §4.3, §4.3.1, MR2b notes).
    /// - opened only while a Research view is shown (<see cref="SetWanted"/>), closed (1000) ≤ 5 s after it is
    ///   hidden or minimized (grace <see cref="HideGrace"/>, so a quick rail switch does not reconnect);
    /// - token ONLY in the <c>X-UFOS-Token</c> request header (Shared/BackendAuth.cs, re-read at every connect);
    /// - handshake after every (re)connect: hello{visible} → first <c>feeds</c> (≤ 10 s) → ONE subscribe with
    ///   the full topic set from <see cref="TopicProvider"/> → <see cref="Resynced"/> (open charts re-fetch REST bars);
    /// - reconnect backoff 1, 2, 5, 10, 30 s, then 30 s; 1013 starts at 5 s; the counter resets after a
    ///   connection that lasted ≥ 60 s; close codes 1008/1009 (and an oversized frame) = protocol error →
    ///   message + ONE retry after 60 s, then no automatic retry until the view is shown again; HTTP 401 on the
    ///   upgrade → token re-read + one immediate retry, then "token rejected" (re-checked every 60 s);
    ///   503 → Research unavailable (retry 30 s); 404 → old backend (re-checked every 60 s);
    /// - receive buffer capped at the SPEC frame cap (1 MiB, <see cref="MaxFrameBytes"/>): a larger message is
    ///   a protocol error (close 1009);
    /// - every event is raised on the UI thread (the loop runs on the dispatcher's context; ReceiveAsync is
    ///   asynchronous I/O, the UI never blocks).
    /// No fallback: while the socket is down nothing is replayed or invented — the view polls
    /// /research/feeds/status (§4.3.1 "WS vs polling") and marks live values as not live.
    /// </summary>
    internal sealed class ResearchSocket
    {
        private const string LogModule = "HedgeFund";

        public static ResearchSocket Instance { get; } = new();

        public const string Url = "ws://127.0.0.1:8867/research/ws";
        /// <summary>SPEC §4.4 <c>WS_MAX_SERVER_MESSAGE_BYTES</c> (one server frame/message).</summary>
        public const int MaxFrameBytes = 1_048_576;
        /// <summary>SPEC §4.4 <c>WS_MAX_MESSAGE_BYTES</c> (client → server).</summary>
        private const int MaxClientMessageBytes = 16_384;
        /// <summary>SPEC §4.4 <c>WS_MAX_SUBSCRIPTIONS</c>.</summary>
        public const int MaxTopics = 64;

        private static readonly int[] BackoffSeconds = { 1, 2, 5, 10, 30 };
        private static readonly TimeSpan StableAfter = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan FeedsTimeout = TimeSpan.FromSeconds(10);
        private static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(10);
        private static readonly TimeSpan HideGrace = TimeSpan.FromSeconds(3);
        private static readonly TimeSpan CloseTimeout = TimeSpan.FromSeconds(2);

        public const string ProtocolErrorMessage = "Live connection refused by the backend (protocol error) — see backend.log.";
        public const string ResearchUnavailableMessage = "Research unavailable: the backend refused the live connection (HTTP 503) — see backend.log. Trading is not affected.";

        private readonly SemaphoreSlim _sendLock = new(1, 1);
        private readonly HashSet<string> _subscribed = new(StringComparer.Ordinal);
        // The order the server holds (it prioritises the 30-symbol stock limit by subscription order).
        private readonly List<string> _subscribedOrder = new();
        private Dispatcher? _dispatcher;
        private CancellationTokenSource? _loopCts;
        private Task? _loopTask;
        private ClientWebSocket? _socket;
        private DispatcherTimer? _hideTimer;
        private bool _wanted;          // = _viewWanted || _alertsWanted
        private bool _viewWanted;      // a Research view is shown
        private bool _alertsWanted;    // MR7: an alert rule is enabled (toasts)
        private bool _restartWhenStopped;
        private bool _handshakeDone;
        private int _protocolRefusals;
        private readonly HashSet<string> _loggedUnknownTypes = new(StringComparer.Ordinal);

        private ResearchSocket() { }

        public ResearchSocketState State { get; private set; } = ResearchSocketState.Off;
        /// <summary>Reconnect attempt n (≥ 1 while <see cref="ResearchSocketState.Waiting"/>), else 0.</summary>
        public int Attempt { get; private set; }
        /// <summary>When the next connection attempt runs (UTC) while waiting/refused, else null.</summary>
        public DateTime? NextTryUtc { get; private set; }
        /// <summary>Plain-text reason while <see cref="ResearchSocketState.Refused"/> (or the last loss reason while waiting).</summary>
        public string? Message { get; private set; }
        /// <summary>When the current handshake sent its subscribe (UTC) — charts loaded before that re-fetch.</summary>
        public DateTime? SubscribedAtUtc { get; private set; }

        /// <summary>The full ordered topic set (tabs/charts first, then watchlists, alerts, <c>feeds</c>).</summary>
        public Func<IReadOnlyList<string>>? TopicProvider { get; set; }

        public event EventHandler? StateChanged;
        public event EventHandler<FeedsStatus>? FeedsReceived;
        public event EventHandler<WsTrade>? TradeReceived;
        public event EventHandler<WsCandle>? CandleReceived;
        public event EventHandler<WsSipReplace>? SipReplaceReceived;
        public event EventHandler<WsSymbolStatus>? SymbolStatusReceived;
        public event EventHandler<WsError>? ServerErrorReceived;
        /// <summary>MR6: a news item for a subscribed <c>news:&lt;SYM&gt;</c> topic (plain text fields, see NewsItem).</summary>
        public event EventHandler<WsNews>? NewsReceived;
        /// <summary>MR7: an alert fired (topic <c>alerts</c>) — toast + alert log.</summary>
        public event EventHandler<WsAlert>? AlertReceived;
        /// <summary>Handshake finished after a (re)connect: everything is re-subscribed; open charts re-fetch bars (§4.3.1 step 4).</summary>
        public event EventHandler? Resynced;

        /// <summary>
        /// "A Research view is shown (visible, host not minimized)". true → connect (if not yet) and report
        /// visible; false → report invisible at once and close after a short grace (≤ 5 s, §4.3.1).
        /// Must be called on the UI thread.
        /// </summary>
        public void SetWanted(bool wanted)
        {
            _dispatcher ??= Dispatcher.CurrentDispatcher;
            if (_viewWanted != wanted)
            {
                _viewWanted = wanted;
                // MR7: the socket may stay open for alert toasts — tell the server whether a view is shown (demand).
                if (_handshakeDone && _wanted) _ = SendAsync(new { op = "visible", visible = wanted });
                // Review L4 (2026-10-17): hidden → only the alert topics stay subscribed (CurrentTopics), so the hub stops
                // building candles / SIP tail refreshes for an invisible chart; shown → the view's full set again.
                if (_handshakeDone && State == ResearchSocketState.Connected) _ = SyncTopicsAsync();
            }
            ApplyWanted();
        }

        /// <summary>
        /// MR7: keep the connection open (visible: false) while at least one alert rule is enabled, so `alert` messages
        /// reach <see cref="AlertReceived"/> (toasts) even when no Research view is shown. Called by
        /// <see cref="ResearchAlertNotifier"/> on the UI thread.
        /// </summary>
        public void SetAlertsWanted(bool wanted)
        {
            _dispatcher ??= Dispatcher.CurrentDispatcher;
            if (_alertsWanted == wanted) return;
            _alertsWanted = wanted;
            if (_handshakeDone && State == ResearchSocketState.Connected) _ = SyncTopicsAsync(); // add/remove `alerts`
            ApplyWanted();
        }

        public bool AlertsWanted => _alertsWanted;

        private void ApplyWanted()
        {
            var wanted = _viewWanted || _alertsWanted;
            if (wanted)
            {
                _hideTimer?.Stop();
                if (_wanted) return;
                _wanted = true;
                if (_loopTask == null || _loopTask.IsCompleted)
                {
                    StartLoop();
                }
                else if (_loopCts == null)
                {
                    // The old socket is still closing (≤ 2 s): start again once it is gone — never two sockets.
                    _restartWhenStopped = true;
                }
                else if (_handshakeDone)
                {
                    _ = SendAsync(new { op = "visible", visible = _viewWanted });
                }
                return;
            }
            if (!_wanted) return;
            _wanted = false;
            if (_handshakeDone) _ = SendAsync(new { op = "visible", visible = false });
            _hideTimer ??= CreateHideTimer();
            _hideTimer.Stop();
            _hideTimer.Start();
        }

        /// <summary>The view's topic set changed (tab opened/closed, timeframe/session switched): diff → (un)subscribe.</summary>
        public void TopicsChanged()
        {
            if (!_handshakeDone || State != ResearchSocketState.Connected) return; // the next handshake subscribes everything
            _ = SyncTopicsAsync();
        }

        /// <summary>Closes for good (app/window shutting down).</summary>
        public void Shutdown()
        {
            _wanted = false;
            _viewWanted = false;
            _alertsWanted = false;
            _restartWhenStopped = false;
            _hideTimer?.Stop();
            StopLoop();
        }

        private void StartLoop()
        {
            _protocolRefusals = 0; // shown (again): a refused protocol state may be retried once more
            var cts = new CancellationTokenSource();
            _loopCts = cts;
            _loopTask = RunLoopAsync(cts);
        }

        private DispatcherTimer CreateHideTimer()
        {
            var timer = new DispatcherTimer(DispatcherPriority.Background, _dispatcher ?? Dispatcher.CurrentDispatcher) { Interval = HideGrace };
            timer.Tick += (_, _) =>
            {
                timer.Stop();
                if (!_wanted) StopLoop();
            };
            return timer;
        }

        private void StopLoop()
        {
            var cts = _loopCts;
            var socket = _socket;
            if (cts == null) return;
            _loopCts = null;
            // Clean close 1000 first (the receive loop then sees the server's close answer); cancel after ≤ 2 s.
            if (socket != null && socket.State == WebSocketState.Open)
                _ = CloseQuietlyAsync(socket, WebSocketCloseStatus.NormalClosure, "view hidden");
            cts.CancelAfter(CloseTimeout);
        }

        // =====================================================================================
        // Connection loop
        // =====================================================================================

        private enum OutcomeKind { Cancelled, NotReachable, Lost, Overloaded, ProtocolError, Unauthorized, Unavailable, OldBackend }

        private readonly record struct Outcome(OutcomeKind Kind, TimeSpan Lasted, string? Detail);

        private async Task RunLoopAsync(CancellationTokenSource cts)
        {
            var ct = cts.Token;
            // StopLoop() detaches the CTS first (clean close), then cancels after ≤ 2 s: a detached loop is stopping.
            bool Stopping() => ct.IsCancellationRequested || !ReferenceEquals(_loopCts, cts);
            var attempt = 0;
            var tokenRetried = false;
            try
            {
                while (!Stopping())
                {
                    var outcome = await ConnectOnceAsync(ct);
                    if (Stopping() || outcome.Kind == OutcomeKind.Cancelled) break;

                    if (outcome.Lasted >= StableAfter)
                    {
                        attempt = 0;       // §4.3.1: reset after a connection that lasted ≥ 60 s
                        tokenRetried = false;
                        _protocolRefusals = 0;
                    }

                    TimeSpan delay;
                    switch (outcome.Kind)
                    {
                        case OutcomeKind.Unauthorized:
                            if (!tokenRetried)
                            {
                                tokenRetried = true; // §1.1: re-read the token once (next connect reads the file again) and retry once
                                continue;
                            }
                            delay = TimeSpan.FromSeconds(60);
                            SetState(ResearchSocketState.Refused, 0, delay, ResearchClient.TokenRejectedMessage);
                            break;
                        case OutcomeKind.Unavailable:
                            delay = TimeSpan.FromSeconds(30);
                            SetState(ResearchSocketState.Refused, 0, delay, ResearchUnavailableMessage);
                            break;
                        case OutcomeKind.OldBackend:
                            delay = TimeSpan.FromSeconds(60);
                            SetState(ResearchSocketState.Refused, 0, delay, ResearchClient.OldBackendMessage);
                            break;
                        case OutcomeKind.ProtocolError:
                            _protocolRefusals++;
                            AppLog.Warn(LogModule, "Market Research live connection: protocol error (" + (outcome.Detail ?? "unknown") + ").");
                            if (_protocolRefusals > 1)
                            {
                                // §4.3.1: no automatic reconnect loop — wait until the view is shown again.
                                SetState(ResearchSocketState.Refused, 0, null, ProtocolErrorMessage);
                                return;
                            }
                            delay = TimeSpan.FromSeconds(60);
                            SetState(ResearchSocketState.Refused, 0, delay, ProtocolErrorMessage);
                            break;
                        default:
                        {
                            tokenRetried = false;
                            attempt++;
                            var floor = outcome.Kind == OutcomeKind.Overloaded ? 2 : 0; // 1013 → backoff starts at 5 s
                            delay = TimeSpan.FromSeconds(BackoffSeconds[Math.Min(attempt - 1 + floor, BackoffSeconds.Length - 1)]);
                            var reason = outcome.Kind == OutcomeKind.NotReachable
                                ? "Live connection to the backend could not be opened"
                                : "Live connection to the backend lost";
                            SetState(ResearchSocketState.Waiting, attempt, delay, reason);
                            break;
                        }
                    }
                    try { await Task.Delay(delay, ct); }
                    catch (OperationCanceledException) { break; }
                }
            }
            catch (Exception ex)
            {
                // Never let the loop crash the app; the view shows the state and polling continues.
                AppLog.Warn(LogModule, "Market Research live connection loop stopped unexpectedly.", ex);
                SetState(ResearchSocketState.Refused, 0, null, "Live connection stopped unexpectedly — see the Backend Log. Hide and show Research to retry.");
                return;
            }
            SetState(ResearchSocketState.Off, 0, null, null);
            if (_restartWhenStopped)
            {
                _restartWhenStopped = false;
                // Shown again while this loop was closing: start a fresh loop after this task has completed.
                if (_wanted) _dispatcher?.BeginInvoke(DispatcherPriority.Background, new Action(() =>
                {
                    if (_wanted && (_loopTask == null || _loopTask.IsCompleted)) StartLoop();
                }));
            }
        }

        private async Task<Outcome> ConnectOnceAsync(CancellationToken ct)
        {
            _handshakeDone = false;
            _subscribed.Clear();
            _subscribedOrder.Clear();
            SubscribedAtUtc = null;
            if (State != ResearchSocketState.Waiting && State != ResearchSocketState.Refused)
                SetState(ResearchSocketState.Connecting, 0, null, null);

            using var socket = new ClientWebSocket();
            socket.Options.CollectHttpResponseDetails = true; // HTTP status of a refused upgrade (401/404/503)
            var token = BackendAuth.ReadToken(NewsroomBackend.BackendDir); // re-read at every connect (new backend = new token)
            if (token != null) socket.Options.SetRequestHeader(BackendAuth.HeaderName, token);
            // Weak Wi-Fi: our own pings detect a dead connection (the server also sends heartbeats every 20 s).
            socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(15);
            socket.Options.KeepAliveTimeout = TimeSpan.FromSeconds(10);

            using (var connectCts = CancellationTokenSource.CreateLinkedTokenSource(ct))
            {
                connectCts.CancelAfter(ConnectTimeout);
                try
                {
                    await socket.ConnectAsync(new Uri(Url), connectCts.Token);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    return new Outcome(OutcomeKind.Cancelled, TimeSpan.Zero, null);
                }
                catch (Exception)
                {
                    var status = (int)socket.HttpStatusCode;
                    return status switch
                    {
                        401 or 403 => new Outcome(OutcomeKind.Unauthorized, TimeSpan.Zero, "HTTP " + status),
                        503 => new Outcome(OutcomeKind.Unavailable, TimeSpan.Zero, "HTTP 503"),
                        404 => new Outcome(OutcomeKind.OldBackend, TimeSpan.Zero, "HTTP 404"),
                        _ => new Outcome(OutcomeKind.NotReachable, TimeSpan.Zero, status == 0 ? null : "HTTP " + status),
                    };
                }
            }

            _socket = socket;
            var connectedAt = DateTime.UtcNow;
            try
            {
                if (State != ResearchSocketState.Connecting) SetState(ResearchSocketState.Connecting, Attempt, null, null);
                await SendAsync(new { op = "hello", visible = _viewWanted }); // false = open only for alert toasts (MR7)
                return await ReceiveLoopAsync(socket, connectedAt, ct);
            }
            finally
            {
                _socket = null;
                _handshakeDone = false;
                _subscribed.Clear();
                _subscribedOrder.Clear();
                if (socket.State == WebSocketState.Open || socket.State == WebSocketState.CloseReceived)
                    await CloseQuietlyAsync(socket, WebSocketCloseStatus.NormalClosure, "closing");
            }
        }

        private async Task<Outcome> ReceiveLoopAsync(ClientWebSocket socket, DateTime connectedAt, CancellationToken ct)
        {
            var buffer = new byte[64 * 1024];
            using var message = new MemoryStream();
            using var feedsCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            feedsCts.CancelAfter(FeedsTimeout); // handshake step 2: first feeds within 10 s, else reconnect
            TimeSpan Lasted() => DateTime.UtcNow - connectedAt;

            while (true)
            {
                message.SetLength(0);
                ValueWebSocketReceiveResult result;
                try
                {
                    do
                    {
                        var receiveToken = _handshakeDone ? ct : feedsCts.Token;
                        result = await socket.ReceiveAsync(buffer.AsMemory(), receiveToken);
                        if (result.MessageType == WebSocketMessageType.Close) break;
                        if (message.Length + result.Count > MaxFrameBytes)
                        {
                            await CloseQuietlyAsync(socket, WebSocketCloseStatus.MessageTooBig, "message larger than 1 MiB");
                            return new Outcome(OutcomeKind.ProtocolError, Lasted(), "server message larger than 1 MiB");
                        }
                        message.Write(buffer, 0, result.Count);
                    } while (!result.EndOfMessage);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    return new Outcome(OutcomeKind.Cancelled, Lasted(), null);
                }
                catch (OperationCanceledException)
                {
                    return new Outcome(OutcomeKind.Lost, Lasted(), "no feeds message within 10 s after hello");
                }
                catch (Exception ex) when (ex is WebSocketException || ex is IOException || ex is ObjectDisposedException)
                {
                    return new Outcome(OutcomeKind.Lost, Lasted(), ex.GetType().Name);
                }

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    var code = socket.CloseStatus.HasValue ? (int)socket.CloseStatus.Value : 1005;
                    var detail = "close " + code + (string.IsNullOrEmpty(socket.CloseStatusDescription) ? "" : " " + socket.CloseStatusDescription);
                    if (ct.IsCancellationRequested) return new Outcome(OutcomeKind.Cancelled, Lasted(), detail);
                    return code switch
                    {
                        1008 or 1009 => new Outcome(OutcomeKind.ProtocolError, Lasted(), detail),
                        1013 => new Outcome(OutcomeKind.Overloaded, Lasted(), detail),
                        _ => new Outcome(OutcomeKind.Lost, Lasted(), detail), // 1000/1001/1011/…: reconnect while still wanted
                    };
                }
                if (result.MessageType != WebSocketMessageType.Text)
                {
                    LogOnce("binary", "Market Research live connection: ignored a binary frame (only JSON text is expected).");
                    continue;
                }

                await HandleMessageAsync(message.GetBuffer().AsMemory(0, (int)message.Length));
            }
        }

        // =====================================================================================
        // Messages (MR2b notes "/research/ws — exact messages")
        // =====================================================================================

        private async Task HandleMessageAsync(ReadOnlyMemory<byte> utf8)
        {
            JsonDocument doc;
            try { doc = JsonDocument.Parse(utf8, new JsonDocumentOptions { MaxDepth = 32 }); }
            catch (JsonException)
            {
                LogOnce("badjson", "Market Research live connection: the backend sent a message that is not valid JSON.");
                return;
            }
            using (doc)
            {
                var root = doc.RootElement;
                if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("type", out var typeEl) || typeEl.ValueKind != JsonValueKind.String)
                    return;
                var type = typeEl.GetString();
                try
                {
                    switch (type)
                    {
                        case "feeds":
                        {
                            if (!root.TryGetProperty("data", out var data)) return;
                            var feeds = data.Deserialize<FeedsStatus>(ResearchClient.Json);
                            if (feeds == null) return;
                            if (!_handshakeDone) await CompleteHandshakeAsync();
                            Raise(FeedsReceived, feeds);
                            break;
                        }
                        case "trade":
                            Raise(TradeReceived, root.Deserialize<WsTrade>(ResearchClient.Json));
                            break;
                        case "candle":
                            Raise(CandleReceived, root.Deserialize<WsCandle>(ResearchClient.Json));
                            break;
                        case "sip_replace":
                            Raise(SipReplaceReceived, root.Deserialize<WsSipReplace>(ResearchClient.Json));
                            break;
                        case "symbol_status":
                            Raise(SymbolStatusReceived, root.Deserialize<WsSymbolStatus>(ResearchClient.Json));
                            break;
                        case "error":
                        {
                            var error = root.Deserialize<WsError>(ResearchClient.Json);
                            if (error == null) return;
                            // bad_message = a client bug: log only (§4.3.1); everything else is also shown by the view.
                            AppLog.Warn(LogModule, "Market Research live connection: backend error " + (error.Code ?? "?") + ": " + (error.Message ?? ""));
                            if (error.Code != "bad_message") Raise(ServerErrorReceived, error);
                            break;
                        }
                        case "news": // {"type":"news","item":{…}} — topic news:<SYM> (MR6 backend notes)
                            Raise(NewsReceived, root.Deserialize<WsNews>(ResearchClient.Json));
                            break;
                        case "alert": // MR7 {"type":"alert", alert_id, symbol, kind, title, message, ts, price, level, log_id, logged}
                            Raise(AlertReceived, root.Deserialize<WsAlert>(ResearchClient.Json));
                            break;
                        default:
                            LogOnce("type:" + type, "Market Research live connection: ignored unknown message type '" + Clip(type, 40) + "'.");
                            break;
                    }
                }
                catch (Exception ex) when (ex is JsonException || ex is InvalidOperationException || ex is NotSupportedException)
                {
                    LogOnce("shape:" + type, "Market Research live connection: unexpected '" + Clip(type, 40) + "' message shape — backend and app versions may not match.");
                }
            }
        }

        // A failing view handler must not stop the connection loop: logged, the message is skipped.
        private void Raise<T>(EventHandler<T>? handler, T? value) where T : class
        {
            if (value == null || handler == null) return;
            try { handler(this, value); }
            catch (Exception ex) { AppLog.Warn(LogModule, "Market Research: a live-message handler failed.", ex); }
        }

        private async Task CompleteHandshakeAsync()
        {
            _handshakeDone = true;
            Attempt = 0;
            SetState(ResearchSocketState.Connected, 0, null, null);
            var topics = CurrentTopics();
            if (topics.Count > 0)
            {
                SubscribedAtUtc = DateTime.UtcNow;
                if (await SendAsync(new { op = "subscribe", topics }))
                    foreach (var t in topics) { _subscribed.Add(t); _subscribedOrder.Add(t); }
            }
            try { Resynced?.Invoke(this, EventArgs.Empty); }
            catch (Exception ex) { AppLog.Warn(LogModule, "Market Research: the re-sync handler failed.", ex); }
        }

        private List<string> CurrentTopics()
        {
            IReadOnlyList<string> raw;
            // Review L4: without a shown view only `alerts` (added below while alert toasts are wanted) is subscribed.
            try { raw = _viewWanted ? TopicProvider?.Invoke() ?? Array.Empty<string>() : Array.Empty<string>(); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: building the live topic list failed.", ex);
                raw = Array.Empty<string>();
            }
            var list = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var t in raw)
            {
                if (string.IsNullOrWhiteSpace(t) || t.Length > 64 || !seen.Add(t)) continue; // WS_MAX_TOPIC_CHARS 64
                if (list.Count >= MaxTopics)
                {
                    AppLog.Warn(LogModule, "Market Research: more than " + MaxTopics + " live topics — the rest is not subscribed.");
                    break;
                }
                list.Add(t);
            }
            // MR7: `alerts` whenever alert toasts are wanted (before `feeds`, §4.3.1 order), also without a view.
            if (_alertsWanted && !seen.Contains("alerts") && list.Count < MaxTopics)
            {
                var feeds = list.IndexOf("feeds");
                if (feeds >= 0) list.Insert(feeds, "alerts");
                else list.Add("alerts");
            }
            return list;
        }

        /// <summary>
        /// Brings the server's topic set to <see cref="TopicProvider"/>'s order. The hub ranks the 30-symbol stock
        /// limit by subscription order (MR2b/MR4 notes), so a diff that would only APPEND a topic that belongs in
        /// front (e.g. the new active chart after a tab switch, with ≥ 30 watchlist stocks) re-subscribes the whole
        /// set in priority order instead: unsubscribe everything, then ONE subscribe (chart first). Pure removals and
        /// additions at the end stay a cheap diff.
        /// </summary>
        private async Task SyncTopicsAsync()
        {
            var wanted = CurrentTopics();
            var wantedSet = new HashSet<string>(wanted, StringComparer.Ordinal);
            var keptInServerOrder = _subscribedOrder.Where(wantedSet.Contains).ToList();
            var keptInWantedOrder = wanted.Where(_subscribed.Contains).ToList();
            var add = wanted.Where(t => !_subscribed.Contains(t)).ToList();
            var remove = _subscribedOrder.Where(t => !wantedSet.Contains(t)).ToList();
            var appendOnly = keptInServerOrder.SequenceEqual(keptInWantedOrder) &&
                             wanted.Take(keptInWantedOrder.Count).SequenceEqual(keptInWantedOrder);
            if (!appendOnly)
            {
                if (_subscribedOrder.Count > 0)
                {
                    var all = _subscribedOrder.ToList();
                    if (!await SendAsync(new { op = "unsubscribe", topics = all })) return; // socket down: the next handshake subscribes in order
                    _subscribed.Clear();
                    _subscribedOrder.Clear();
                }
                if (wanted.Count > 0 && await SendAsync(new { op = "subscribe", topics = wanted }))
                    foreach (var t in wanted) { _subscribed.Add(t); _subscribedOrder.Add(t); }
                return;
            }
            if (remove.Count > 0 && await SendAsync(new { op = "unsubscribe", topics = remove }))
                foreach (var t in remove) { _subscribed.Remove(t); _subscribedOrder.Remove(t); }
            if (add.Count > 0 && await SendAsync(new { op = "subscribe", topics = add }))
                foreach (var t in add) { _subscribed.Add(t); _subscribedOrder.Add(t); }
        }

        /// <summary>Sends one client message (JSON object, ≤ 16 KiB); false when the socket is not open.</summary>
        private async Task<bool> SendAsync(object payload)
        {
            var socket = _socket;
            if (socket == null || socket.State != WebSocketState.Open) return false;
            var bytes = JsonSerializer.SerializeToUtf8Bytes(payload);
            if (bytes.Length > MaxClientMessageBytes)
            {
                AppLog.Warn(LogModule, "Market Research: a live-connection message was larger than 16 KiB and was not sent.");
                return false;
            }
            await _sendLock.WaitAsync();
            try
            {
                if (socket.State != WebSocketState.Open) return false;
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await socket.SendAsync(bytes.AsMemory(), WebSocketMessageType.Text, true, cts.Token);
                return true;
            }
            catch (Exception ex) when (ex is WebSocketException || ex is IOException || ex is OperationCanceledException || ex is ObjectDisposedException)
            {
                return false; // the receive loop notices the broken connection and reconnects
            }
            finally
            {
                _sendLock.Release();
            }
        }

        private async Task CloseQuietlyAsync(ClientWebSocket socket, WebSocketCloseStatus status, string description)
        {
            await _sendLock.WaitAsync();
            try
            {
                using var cts = new CancellationTokenSource(CloseTimeout);
                await socket.CloseOutputAsync(status, description, cts.Token);
            }
            catch { /* already closed/aborted */ }
            finally
            {
                _sendLock.Release();
            }
        }

        private void SetState(ResearchSocketState state, int attempt, TimeSpan? nextTryIn, string? message)
        {
            State = state;
            Attempt = attempt;
            NextTryUtc = nextTryIn.HasValue ? DateTime.UtcNow + nextTryIn.Value : null;
            Message = message;
            try { StateChanged?.Invoke(this, EventArgs.Empty); }
            catch (Exception ex) { AppLog.Warn(LogModule, "Market Research: a live-state handler failed.", ex); }
        }

        /// <summary>Status-bar / tooltip text for the current state (SPEC §4.3.1 wording).</summary>
        public string DescribeState()
        {
            switch (State)
            {
                case ResearchSocketState.Connected: return "Live connection to the backend: connected.";
                case ResearchSocketState.Connecting: return "Live connection to the backend: connecting…";
                case ResearchSocketState.Waiting:
                {
                    var seconds = NextTryUtc.HasValue ? Math.Max(0, (int)Math.Ceiling((NextTryUtc.Value - DateTime.UtcNow).TotalSeconds)) : 0;
                    return (Message ?? "Live connection to the backend lost") + " — waiting (attempt " + Attempt + ", next try in " + seconds + " s).";
                }
                case ResearchSocketState.Refused:
                    return (Message ?? "Live connection refused.") + (NextTryUtc.HasValue ? "" : " Hide and show Research to retry.");
                default: return "Live connection to the backend: off (Research is not shown).";
            }
        }

        private void LogOnce(string key, string text)
        {
            if (_loggedUnknownTypes.Count < 64 && _loggedUnknownTypes.Add(key)) AppLog.Warn(LogModule, text);
        }

        private static string Clip(string? text, int max) => text == null ? "" : (text.Length <= max ? text : text.Substring(0, max) + "…");
    }

    // =====================================================================================
    // Server → client messages (MR2b notes). `ts` / bar `t` = UTC ISO strings.
    // =====================================================================================

    /// <summary>MR6 <c>{"type":"news","item":{…}}</c> — <see cref="NewsItem"/> is defined in ResearchClient.cs (MR6-WPF).</summary>
    internal sealed class WsNews
    {
        [JsonPropertyName("item")] public NewsItem? Item { get; set; }
    }

    /// <summary>MR7 <c>{"type":"alert", …}</c> ("MR7 backend notes"; news alerts: price/level null).</summary>
    internal sealed class WsAlert
    {
        [JsonPropertyName("alert_id")] public string? AlertId { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("kind")] public string? Kind { get; set; }
        [JsonPropertyName("title")] public string? Title { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("level")] public double? Level { get; set; }
        [JsonPropertyName("log_id")] public long? LogId { get; set; }
        [JsonPropertyName("logged")] public bool? Logged { get; set; }
    }

    internal sealed class WsTrade
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("price")] public double? Price { get; set; }
        [JsonPropertyName("size")] public double? Size { get; set; }
        [JsonPropertyName("ts")] public string? Ts { get; set; }
        /// <summary>iex · crypto</summary>
        [JsonPropertyName("feed")] public string? Feed { get; set; }
    }

    internal sealed class WsCandle
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("tf")] public string? Tf { get; set; }
        [JsonPropertyName("session")] public string? Session { get; set; }
        [JsonPropertyName("bar")] public ApiBar? Bar { get; set; }
    }

    internal sealed class WsSipReplace
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("tf")] public string? Tf { get; set; }
        [JsonPropertyName("session")] public string? Session { get; set; }
        [JsonPropertyName("bars")] public List<ApiBar>? Bars { get; set; }
        [JsonPropertyName("part")] public int? Part { get; set; }
        [JsonPropertyName("parts")] public int? Parts { get; set; }
    }

    internal sealed class WsSymbolStatus
    {
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("live")] public bool? Live { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
    }

    internal sealed class WsError
    {
        [JsonPropertyName("code")] public string? Code { get; set; }
        [JsonPropertyName("message")] public string? Message { get; set; }
        /// <summary>Set for <c>candle_unavailable</c> (e.g. <c>candle:AAPL:1m:rth</c>).</summary>
        [JsonPropertyName("topic")] public string? Topic { get; set; }
    }
}

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// "Brokers" section of Settings (= this Keys &amp; data window; Strategy Platform stage 8 — SPEC §18, exact JSON §18.10,
    /// deviations §18.11, WPF notes §18.12). PAPER / DEMO TRADING ONLY.
    ///
    /// One card per adapter: the internal paper broker (paper-main / paper-council, simulated in the backend — nothing to
    /// connect or reconcile), Alpaca paper and Binance Spot demo (§18.14; the Kraken Futures demo is retired): connection state, link state incl. "waiting for broker
    /// (attempt n)" verbatim, SDK (ccxt not installed → the backend's text verbatim), keys (status only), the LAST broker
    /// snapshot with its time (never shown as current), reconciliation OK / HALTED with the mismatch details verbatim and
    /// the manual "Resolve…" flow (no auto-correction), "Test connection", "Connect…" (one confirmation step since fix1023, the backend's
    /// preconditions — flat, no open orders, ≥ $1,000 — and its refusal verbatim incl. the positions found), "Disconnect…",
    /// per-broker kill (cancel all at the broker + flatten, confirmation). The toolbar ■ Kill switch stays the GLOBAL one.
    /// Routing per strategy (one confirmation step, consequences explained) and the live guard as a read-only checklist with
    /// every item OFF and the fixed text — there is no control that could enable live.
    ///
    /// Polls GET /v2/brokers (+ portfolio state, routing, open detail expanders) every 5 s while the window is visible and
    /// not minimised; a section is re-rendered only when its data changed (the focused control is restored by name). A
    /// failed poll keeps the last state with "Connection lost (attempt n)".
    /// </summary>
    public partial class KeysAndDataWindow
    {
        private static readonly CultureInfo BrokerInv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan BrokerPollInterval = TimeSpan.FromSeconds(5);
        private const string LiveDisabledText = "Live trading is disabled in this build (stage 12 requires your explicit go)";

        private DispatcherTimer? _brokerPoll;
        private bool _brokersClosed;
        private bool _brokersPolling;
        private int _brokersSeq;
        private string? _brokersSignature;

        private BrokerList? _brokers;
        private string? _brokersError;
        private int _brokersAttempts;
        private TradingState? _brokerTrading;
        private string? _brokerTradingError;
        private BrokerRouting? _routing;
        private string? _routingError;
        private readonly Dictionary<string, BrokerLiveGuard> _liveGuards = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _liveGuardErrors = new(StringComparer.Ordinal);
        private readonly Dictionary<string, BrokerDetail> _brokerDetails = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _brokerDetailErrors = new(StringComparer.Ordinal);
        private readonly HashSet<string> _brokerExpanded = new(StringComparer.Ordinal);
        private readonly HashSet<string> _brokerInfoExpanded = new(StringComparer.Ordinal);
        private readonly Dictionary<string, BrokerTestResult> _brokerTests = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _brokerTestAt = new(StringComparer.Ordinal);
        private readonly Dictionary<string, (string Text, string Tone)> _brokerMessages = new(StringComparer.Ordinal);
        private readonly HashSet<string> _brokerBusy = new(StringComparer.Ordinal);

        /// <summary>Called from the constructor: poll timer, visibility / minimise / close hooks, F5 and Ctrl+B.</summary>
        private void InitBrokers()
        {
            _brokerPoll = new DispatcherTimer { Interval = BrokerPollInterval };
            BrokersHost.RequestBringIntoView += BrokersHost_RequestBringIntoView;
            _brokerPoll.Tick += async (_, _) => await PollBrokersAsync();
            IsVisibleChanged += (_, _) => UpdateBrokerPolling();
            StateChanged += (_, _) => UpdateBrokerPolling();
            Closed += (_, _) =>
            {
                _brokersClosed = true;
                _brokerPoll?.Stop();
            };
            PreviewKeyDown += async (_, e) =>
            {
                var ctrl = (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control;
                if (e.Key == Key.F5)
                {
                    e.Handled = true;
                    try
                    {
                        RenderKeys();
                        await Task.WhenAll(RefreshBackendKeysAsync(), RefreshSourcesAsync(), RefreshNetworkAsync(), RefreshBrokersAsync(force: true));
                    }
                    catch (Exception ex) { AppLog.Warn(LogModule, "Keys & data: refresh (F5) failed.", ex); }
                }
                else if (ctrl && e.Key == Key.B)
                {
                    e.Handled = true;
                    ShowBrokersSection();
                }
            };
        }

        /// <summary>
        /// Scrolls Settings → Brokers to the TOP of the view and focuses its refresh button (used by "Brokers…" and Ctrl+B).
        /// Live click-test fix 2026-10-18: BringIntoView() did nothing because the Brokers panel (the second section) is normally
        /// already partly inside the viewport — so "Brokers…" on an open window changed nothing visible. The offset is set explicitly.
        /// </summary>
        public void ShowBrokersSection()
        {
            try
            {
                MainScroll.UpdateLayout();
                var top = BrokersPanel.TranslatePoint(new Point(0, 0), MainScroll).Y;
                MainScroll.ScrollToVerticalOffset(Math.Max(0, MainScroll.VerticalOffset + top - 8));
                MainScroll.UpdateLayout();
                _suppressBringIntoView = true;
                try { BrokersRefreshButton.Focus(); }
                finally { _suppressBringIntoView = false; }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: could not scroll to Brokers.", ex);
            }
        }

        /// <summary>
        /// Live click-test fix 2026-10-18 (wheel "40 ticks ≈ 40 px"): the Brokers section is rebuilt on every 5-s poll whose data changed
        /// (the trading state is part of its signature) and the previously focused broker control was re-focused — Focus() raises
        /// RequestBringIntoView, so the ScrollViewer was pulled back to that control every few seconds and wheel scrolling barely moved.
        /// Re-render focus restores no longer scroll (see RenderBrokersIfChanged); the wheel scrolls a fixed, normal step.
        /// </summary>
        private bool _suppressBringIntoView;

        private void MainScroll_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
        {
            if (e.Handled || e.Delta == 0) return;
            // retest 2026-10-23: queued LineUp / LineDown (every notch counts) instead of an offset computed from the stale VerticalOffset
            ChartTokens.ScrollByWheel(MainScroll, e.Delta);
            e.Handled = true;
        }

        private void BrokersHost_RequestBringIntoView(object sender, RequestBringIntoViewEventArgs e)
        {
            if (_suppressBringIntoView) e.Handled = true;
        }

        /// <summary>Focuses a re-created broker control without scrolling the window (keyboard users keep their place).</summary>
        private void RestoreFocusWithoutScroll(UIElement target)
        {
            _ = Dispatcher.BeginInvoke(new Action(() =>
            {
                if (_brokersClosed) return;
                var offset = MainScroll.VerticalOffset;
                _suppressBringIntoView = true;
                try { target.Focus(); }
                finally { _suppressBringIntoView = false; }
                if (Math.Abs(MainScroll.VerticalOffset - offset) > 0.5) MainScroll.ScrollToVerticalOffset(offset);
            }), DispatcherPriority.Input);
        }

        private void UpdateBrokerPolling()
        {
            if (_brokerPoll == null) return;
            if (!_brokersClosed && IsLoaded && IsVisible && WindowState != WindowState.Minimized) { if (!_brokerPoll.IsEnabled) _brokerPoll.Start(); }
            else _brokerPoll.Stop();
        }

        private async Task PollBrokersAsync()
        {
            if (_brokersPolling || _brokersClosed || !IsVisible || _brokerBusy.Count > 0) return;
            _brokersPolling = true;
            try { await RefreshBrokersAsync(force: false); }
            catch (Exception ex) { AppLog.Warn(LogModule, "Keys & data: broker poll failed.", ex); }
            finally { _brokersPolling = false; }
        }

        private async void BrokersRefreshButton_Click(object sender, RoutedEventArgs e)
        {
            try { await RefreshBrokersAsync(force: true); }
            catch (Exception ex) { AppLog.Warn(LogModule, "Keys & data: broker refresh failed.", ex); }
        }

        // =====================================================================================
        // Loading
        // =====================================================================================

        private async Task RefreshBrokersAsync(bool force)
        {
            if (_brokersClosed) return;
            var seq = ++_brokersSeq;
            if (_brokers == null && _brokersError == null) SetBrokersStatus("Loading…", FundPalette.Faint);
            var brokersTask = PlatformClient.GetBrokersAsync();
            var tradingTask = PlatformClient.GetTradingStateAsync();
            var routingTask = PlatformClient.GetBrokerRoutingAsync();
            await Task.WhenAll(brokersTask, tradingTask, routingTask);
            if (seq != _brokersSeq || _brokersClosed) return;

            var br = brokersTask.Result;
            if (br.Ok && br.Value?.Brokers != null)
            {
                _brokers = br.Value;
                _brokersError = null;
                _brokersAttempts = 0;
            }
            else
            {
                _brokersAttempts++;
                _brokersError = br.Ok ? "Unexpected response from the backend (/v2/brokers) — backend and app versions may not match."
                              : br.Offline ? br.Message
                              : br.Status == PlatformCallStatus.NotFound ? "The running backend has no broker routes (/v2/brokers) — it is older than stage 8; restart it from the Hedge Fund window."
                              : PlatformClient.DescribeFailure(br);
            }
            var tr = tradingTask.Result;
            if (tr.Ok && tr.Value != null) { _brokerTrading = tr.Value; _brokerTradingError = null; }
            else _brokerTradingError = tr.Ok ? "Unexpected response from the backend (/v2/portfolio/state)." : PlatformClient.DescribeFailure(tr);
            var rr = routingTask.Result;
            if (rr.Ok && rr.Value != null) { _routing = rr.Value; _routingError = null; }
            else _routingError = rr.Ok ? "Unexpected response from the backend (/v2/brokers/routing)." : PlatformClient.DescribeFailure(rr);

            if (_brokers != null && (force || _liveGuards.Count + _liveGuardErrors.Count == 0))
            {
                foreach (var id in PlatformClient.BrokerIds)
                {
                    var lg = await PlatformClient.GetBrokerLiveGuardAsync(id);
                    if (lg.Ok && lg.Value != null) { _liveGuards[id] = lg.Value; _liveGuardErrors.Remove(id); }
                    else _liveGuardErrors[id] = lg.Ok ? "Unexpected response from the backend (live guard)." : PlatformClient.DescribeFailure(lg);
                }
            }
            foreach (var id in _brokerExpanded.ToList()) await LoadBrokerDetailAsync(id);
            if (seq != _brokersSeq || _brokersClosed) return;

            var stamp = DateTime.Now.ToString("HH:mm:ss", BrokerInv);
            if (_brokersError != null && _brokers != null)
                SetBrokersStatus("Connection lost (attempt " + _brokersAttempts.ToString(BrokerInv) + ") — showing the last known state from before, retrying every 5 s. " + _brokersError, FundPalette.Risk);
            else if (_brokersError != null)
                SetBrokersStatus(_brokersError, FundPalette.Risk);
            else
                SetBrokersStatus((_brokers?.Notice ?? "Paper/demo trading only · simulated money") + " · updated " + stamp
                                 + " · refreshes every 5 s while this window is open (each read also counts as contact for the dead-man switch)", FundPalette.Faint);
            RenderBrokersIfChanged(force);
            UpdateBrokerPolling();
        }

        private async Task LoadBrokerDetailAsync(string id)
        {
            var res = await PlatformClient.GetBrokerAsync(id);
            if (_brokersClosed) return;
            if (res.Ok && res.Value != null) { _brokerDetails[id] = res.Value; _brokerDetailErrors.Remove(id); }
            else _brokerDetailErrors[id] = res.Ok ? "Unexpected response from the backend (/v2/brokers/" + id + ")." : PlatformClient.DescribeFailure(res);
        }

        private void SetBrokersStatus(string text, Brush brush)
        {
            BrokersStatusText.Text = text;
            BrokersStatusText.Foreground = brush;
            AutomationProperties.SetName(BrokersStatusText, text);
        }

        // =====================================================================================
        // Rendering
        // =====================================================================================

        private void RenderBrokersIfChanged(bool force)
        {
            string sig;
            try
            {
                sig = JsonSerializer.Serialize(new object?[]
                {
                    _brokers, _brokerTrading?.Accounts, _brokerTrading?.AutoTrade, _routing, _routingError, _brokerTradingError,
                    _liveGuards, _liveGuardErrors, _brokerDetails, _brokerDetailErrors, _brokerExpanded.OrderBy(x => x, StringComparer.Ordinal),
                    _brokerTests, _brokerMessages.Select(kv => kv.Key + "|" + kv.Value.Text + "|" + kv.Value.Tone), _brokerBusy.OrderBy(x => x, StringComparer.Ordinal),
                    _brokers == null ? _brokersError : null,
                });
            }
            catch (Exception) { sig = Guid.NewGuid().ToString("N"); }
            if (!force && sig == _brokersSignature) return;
            _brokersSignature = sig;

            // keep the keyboard focus across a re-render (same control = same automation name)
            string? focusedName = null;
            if (Keyboard.FocusedElement is FrameworkElement fe && fe.IsDescendantOf(BrokersHost))
                focusedName = AutomationProperties.GetName(fe);
            var offset = MainScroll.VerticalOffset;
            RenderBrokers();
            MainScroll.ScrollToVerticalOffset(offset);   // a re-render never moves the view
            if (!string.IsNullOrEmpty(focusedName))
            {
                var target = FindByName(BrokersHost, focusedName);
                if (target != null) RestoreFocusWithoutScroll(target);
            }
        }

        private static UIElement? FindByName(DependencyObject root, string name)
        {
            foreach (var child in LogicalTreeHelper.GetChildren(root))
            {
                if (child is not DependencyObject d) continue;
                if (d is UIElement u && u.Focusable && AutomationProperties.GetName(d) == name) return u;
                var found = FindByName(d, name);
                if (found != null) return found;
            }
            return null;
        }

        private void RenderBrokers()
        {
            var host = BrokersHost;
            host.Children.Clear();
            if (_brokers == null)
            {
                host.Children.Add(BLine(_brokersError ?? "Loading…", 11.5, _brokersError != null ? FundPalette.Risk : FundPalette.Faint));
                return;
            }

            host.Children.Add(BCallout("■ The kill switch in the Hedge Fund toolbar stays the GLOBAL one: it halts the old engine and every platform account — "
                                       + "including connected broker accounts (it cancels their open orders at the broker and closes the positions the broker "
                                       + "reports). The \"Kill this broker…\" buttons below act on ONE broker account only.", FundPalette.Kill, FundPalette.KillSurface));

            host.Children.Add(InternalPaperCard());
            foreach (var b in _brokers.Brokers ?? new List<BrokerStatus>())
                if (!string.IsNullOrEmpty(b.Id)) host.Children.Add(BrokerCard(b));
            host.Children.Add(RoutingPanel());
            host.Children.Add(LiveGuardPanel());
        }

        // ----- internal paper broker -----

        private FrameworkElement InternalPaperCard()
        {
            var stack = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text("Internal paper broker  ", 13, FundPalette.TextBright, bold: true, wrap: false));
            head.Children.Add(FundUi.Text("internal_paper  ", 11, FundPalette.Faint, mono: true, wrap: false));
            head.Children.Add(BChip("PAPER · SIMULATED", FundPalette.Neutral));
            head.Children.Add(BGap());
            head.Children.Add(BChip("ALWAYS AVAILABLE (IN THE BACKEND)", FundPalette.Long));
            stack.Children.Add(head);
            stack.Children.Add(BLine("Simulated in the Hedge Fund backend with the backtest's fees, slippage, margin, borrow and crypto rules — nothing to connect. "
                                     + "Reconciliation: not applicable (the internal paper broker IS the book). Every strategy trades here (paper-main) until you "
                                     + "route it to a broker below.", 10.5, FundPalette.Dim));
            if (_brokerTradingError != null && _brokerTrading == null)
            {
                stack.Children.Add(BError("Account summary not available: " + _brokerTradingError));
            }
            else
            {
                var internals = (_brokerTrading?.Accounts ?? new List<TradingAccountSummary>())
                    .Where(a => !string.IsNullOrEmpty(a.Id) && !PlatformClient.IsBrokerId(a.Id)).ToList();
                if (internals.Count == 0)
                    stack.Children.Add(BLine("paper-main is not open yet — open it in Portfolio & Trading → Portfolio.", 11, FundPalette.Faint));
                foreach (var a in internals)
                {
                    var row = new WrapPanel { Margin = new Thickness(0, 6, 0, 0) };
                    row.Children.Add(FundUi.Text((a.Id ?? "?") + "  ", 11.5, FundPalette.Text, bold: true, mono: true, wrap: false));
                    row.Children.Add(a.Halted ? BChip("HALTED · " + BrokerText.HaltKind(a.Halt?.Kind).ToUpperInvariant(), FundPalette.Kill) : BChip("TRADING ALLOWED", FundPalette.Long));
                    if (a.Valuation is { Ok: false })
                    {
                        row.Children.Add(BGap());
                        row.Children.Add(BChip("VALUATION PAUSED", FundPalette.Risk));
                    }
                    stack.Children.Add(row);
                    var v = a.Account;
                    var text = v == null
                        ? "Not valued: " + (a.Valuation?.Message ?? "no current quote") + " — no values are shown until a current quote exists."
                        : "Equity " + FundFormat.Money(v.Equity) + " · cash " + FundFormat.Money(v.Cash) + " · buying power " + FundFormat.Money(v.BuyingPower)
                          + " · day P&L " + FundFormat.Money(v.DayPnl, signed: true);
                    text += " · " + (a.PositionsCount?.ToString(BrokerInv) ?? "—") + " positions · " + (a.WorkingOrders?.ToString(BrokerInv) ?? "—") + " working orders";
                    stack.Children.Add(BLine(text, 11, v == null ? FundPalette.Risk : FundPalette.Text, mono: v != null));
                    if (a.Halted)
                        stack.Children.Add(BLine("Halted since " + TradingText.Ts(a.Halt?.At) + " — " + (a.Halt?.Reason ?? "no reason given") + ". Resume it in Portfolio & Trading → Portfolio.", 10.5, FundPalette.Kill));
                }
            }
            var card = FundUi.Panel(stack, new Thickness(14, 10, 14, 10), new Thickness(0, 8, 0, 8));
            AutomationProperties.SetName(card, "Internal paper broker, simulated, always available");
            return card;
        }

        // ----- one broker account -----

        private FrameworkElement BrokerCard(BrokerStatus b)
        {
            if (b.IsRetired) return RetiredBrokerCard(b);
            var id = b.Id!;
            var binance = id == PlatformClient.BinanceDemoId;
            var usdt = b.IsUsdt;                 // §18.14: the account's money is USDT — shown as USDT, never converted
            string Money(double? v, bool signed = false) => usdt ? BrokerText.Usdt(v, signed) : FundFormat.Money(v, signed: signed);
            var busy = _brokerBusy.Contains(id);
            var stack = new StackPanel();

            // head + chips
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text((string.IsNullOrWhiteSpace(b.Label) ? id : b.Label!) + "  ", 13, FundPalette.TextBright, bold: true, wrap: false));
            head.Children.Add(FundUi.Text(id + "  ", 11, FundPalette.Faint, mono: true, wrap: false));
            head.Children.Add(BChip(!string.IsNullOrWhiteSpace(b.Chip) ? b.Chip! : (binance ? "DEMO · SIMULATED FUNDS" : "PAPER · SIMULATED MONEY"), FundPalette.Neutral));
            if (usdt)
            {
                head.Children.Add(BGap());
                head.Children.Add(BChip("USDT", FundPalette.Neutral));
            }
            var (cText, cBrush) = BrokerText.ConnectionChip(b.Connection);
            head.Children.Add(BGap());
            head.Children.Add(BChip(cText, cBrush));
            var (lText, lBrush) = BrokerText.LinkChip(b.Link);
            head.Children.Add(BGap());
            head.Children.Add(BChip(lText, lBrush));
            if (b.IsConnected || b.Reconciliation?.Status is "mismatch" || (b.IsHalted && b.Halt?.Kind == "reconcile"))
            {
                var (rText, rBrush) = BrokerText.ReconcileChip(b.Reconciliation, b.Halt);
                head.Children.Add(BGap());
                head.Children.Add(BChip(rText, rBrush));
            }
            if (b.IsHalted && b.Halt?.Kind != "reconcile")
            {
                head.Children.Add(BGap());
                head.Children.Add(BChip("HALTED · " + BrokerText.HaltKind(b.Halt?.Kind).ToUpperInvariant(), FundPalette.Kill));
            }
            stack.Children.Add(head);
            stack.Children.Add(BLine((b.Venue ?? "—") + " · " + (b.Instruments ?? "—"), 10.5, FundPalette.Dim));
            if (usdt)
                stack.Children.Add(BLine("Amounts in USDT — " + (string.IsNullOrWhiteSpace(b.CurrencyNote) ? "not converted" : b.CurrencyNote!.Replace("USDT — ", ""))
                                         + ". Spot, long only: no short, no margin, no leverage. The starting balances you confirm at the first connect are the "
                                         + "baseline — the platform's positions are the changes since then.", 10.5, FundPalette.Risk));
            stack.Children.Add(BLine("Endpoint " + (b.Endpoint ?? "—") + " (paper/demo, asserted by the backend — no live endpoint exists in this build)", 10.5, FundPalette.Faint, mono: true));

            // SDK
            var sdk = b.Sdk;
            if (sdk?.Installed == false)
                stack.Children.Add(BCallout((sdk.Message ?? ((sdk.Name ?? "The SDK") + " is not installed")), FundPalette.Kill, FundPalette.KillSurface));
            else
                stack.Children.Add(BLine("SDK: " + (sdk?.Name ?? "—") + (sdk?.Version != null ? " " + sdk.Version : "") + (sdk?.Installed == true ? " · installed" : " · not reported"), 10.5, FundPalette.Faint));

            // keys (status only)
            var keys = b.Keys ?? new List<BrokerKey>();
            if (keys.Count > 0)
            {
                var missing = keys.Where(k => k.Present != true).ToList();
                var keyText = "Keys: " + string.Join(" · ", keys.Select(k => (k.Label ?? k.Name ?? "key") + (k.Present == true ? " ● set" : k.Error != null ? " — " + k.Error : " ✕ not set")));
                if (missing.Count > 0) keyText += " — set them in the Keys section above.";
                stack.Children.Add(BLine(keyText, 10.5, missing.Count > 0 ? FundPalette.Risk : FundPalette.Faint));
            }

            // connection + link
            var conn = b.Connection;
            var connText = conn?.Status switch
            {
                "connected" => "Connected since " + TradingText.Ts(conn!.Since) + (conn.By != null ? " by " + conn.By : "") + (string.IsNullOrWhiteSpace(conn.Reason) ? "" : " — " + conn.Reason),
                "disconnected" => "Disconnected since " + TradingText.Ts(conn!.Since) + (string.IsNullOrWhiteSpace(conn.Reason) ? "" : " — " + conn.Reason) + ". Reconnecting continues the mirror book (the connect reconciliation must be OK).",
                "never_connected" => binance
                    ? "Never connected. Test the connection, then connect (first connect: no open orders with the platform's UFOS_ prefix, at least 1,000 USDT, and you confirm the starting balances the test shows — the demo stays pre-funded, no flat rule)."
                    : "Never connected. Test the connection, then connect (first connect: the broker account must be flat with no open orders and hold at least $1,000).",
                _ => "Connection state not reported.",
            };
            stack.Children.Add(BLine(connText, 11, FundPalette.Text));
            var link = b.Link;
            if (link != null)
            {
                var linkBrush = link.State switch { "ok" => FundPalette.Dim, "waiting" => FundPalette.Risk, "idle" => FundPalette.Faint, _ => FundPalette.Kill };
                // Live click-test 2026-10-18: the link state is REACHABILITY (the backend's message "Connected · last answer …" read as
                // the connect state next to "Never connected"). State "ok" gets its own wording; other states keep the backend text.
                if (link.State == "ok")
                    stack.Children.Add(BLine("Broker reachable — the last request reached the broker" + (link.LastOkAt != null ? " · last answer " + TradingText.Ts(link.LastOkAt) : "")
                                             + (conn?.Status == "connected" ? "" : " (this is not a connection — connect below)"), 11, linkBrush));
                else if (!string.IsNullOrWhiteSpace(link.Message)) stack.Children.Add(BLine(link.Message!, 11, linkBrush));
                var extra = new List<string>();
                if (link.State == "waiting" && link.NextRetryAt != null) extra.Add("next automatic try " + TradingText.Ts(link.NextRetryAt) + " (your actions here always try at once)");
                if (link.LastOkAt != null && link.State != "ok") extra.Add("last answer " + TradingText.Ts(link.LastOkAt));
                if (!string.IsNullOrWhiteSpace(link.LastError)) extra.Add("last error: " + link.LastError);
                if (extra.Count > 0) stack.Children.Add(BLine(string.Join(" · ", extra), 10.5, FundPalette.Faint));
            }

            // §18.13: a kill that has not verified the broker flat (pending / positions left) — red, verbatim
            if (b.Kill is { } ks && (ks.Pending == true || (ks.NotFlat?.Count ?? 0) > 0))
            {
                var kt = BrokerText.KillFlatText((ks.NotFlat?.Count ?? 0) > 0 ? false : (bool?)null, ks.Pending, ks.NotFlat, null);
                stack.Children.Add(BCallout("■ Kill pending since " + TradingText.Ts(ks.At) + "\n" + (kt ?? "The kill is still pending."), FundPalette.Kill, FundPalette.KillSurface));
                if (BrokerText.UntouchedText(ks.UntouchedOrders, ks.UntouchedNote) is string kut) stack.Children.Add(BLine("ℹ " + kut, 10.5, FundPalette.Dim));
            }

            // halt (kill / drawdown / reconcile)
            if (b.IsHalted)
            {
                var hk = b.Halt?.Kind;
                var how = hk == "reconcile"
                    ? "Resolve the reconciliation below — only an OK result after your resolution clears this halt (a later OK check alone does not)."
                    : "Resume it in Portfolio & Trading → Portfolio (needs a reconciliation OK; auto trading stays off).";
                stack.Children.Add(BCallout("■ Halted (" + BrokerText.HaltKind(hk) + ") since " + TradingText.Ts(b.Halt?.At) + " — " + (b.Halt?.Reason ?? "no reason given")
                                            + "\nNo new orders on this broker account. " + how, FundPalette.Kill, FundPalette.KillSurface));
            }

            // §18.14 review fix: binance-demo without its confirmed baseline — blocked (never assumed empty); confirm again
            if (b.IsBaselineMissing)
            {
                stack.Children.Add(BCallout("■ " + (b.Baseline?.Message ?? "Baseline missing — confirm the starting balances.")
                                            + "\nNo new orders and no reconciliation on this account until you confirm the starting balances again: "
                                            + "Test connection, then \"Confirm starting balances…\" below.", FundPalette.Kill, FundPalette.KillSurface));
            }

            // account summary (last snapshot — never shown as current)
            stack.Children.Add(BSubHead("Account at the broker"));
            var snap = b.Snapshot;
            var acc = snap?.Account;
            if (acc == null)
            {
                stack.Children.Add(BLine("No broker snapshot yet — the values appear after the first successful read (connect / reconcile). No stand-in values.", 11, FundPalette.Faint));
            }
            else
            {
                stack.Children.Add(BLine("Last snapshot " + TradingText.Ts(snap!.At) + " (updated by reconciliations — not a live quote)", 10.5, FundPalette.Faint));
                var cols = new Grid { Margin = new Thickness(0, 4, 0, 0) };
                cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });
                cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                var c1 = new StackPanel();
                if (!binance)
                    c1.Children.Add(FundUi.KeyValue("Equity", Money(acc.Equity), FundPalette.TextBright));
                c1.Children.Add(FundUi.KeyValue(binance ? "USDT total (cash basis)" : "Cash (cash basis)", Money(acc.CashBasis ?? acc.Cash)));
                c1.Children.Add(FundUi.KeyValue(binance ? "USDT free" : "Buying power", Money(acc.BuyingPower)));
                c1.Children.Add(FundUi.KeyValue("Positions · open orders at the broker",
                    (snap.PositionsCount?.ToString(BrokerInv) ?? "—") + " · " + (snap.OpenOrdersCount?.ToString(BrokerInv) ?? "—")));
                c1.Children.Add(FundUi.KeyValue("Working OMS orders", b.WorkingOrders?.ToString(BrokerInv) ?? "—"));
                var c2 = new StackPanel();
                if (binance)
                {
                    c2.Children.Add(FundUi.KeyValue("Balances (as reported)", BrokerText.Balances(acc.Balances)));
                    c2.Children.Add(FundUi.KeyValue("Baseline", acc.BaselineSet == true ? "confirmed at the first connect" : "not set yet (set at the first connect)"));
                    c2.Children.Add(FundUi.KeyValue("Shorting", "not possible (spot, long only)"));
                }
                else
                {
                    c2.Children.Add(FundUi.KeyValue("Account status", StrategyJson.Scalar(acc.Status) is { Length: > 0 } st ? st : "—"));
                    c2.Children.Add(FundUi.KeyValue("Pattern day trader · day trades", YesNo(acc.PatternDayTrader) + " · " + TradingText.Number(acc.DaytradeCount)));
                    c2.Children.Add(FundUi.KeyValue("Margin multiplier", TradingText.Number(acc.Multiplier)));
                    c2.Children.Add(FundUi.KeyValue("Shorting enabled · trading blocked", YesNo(acc.ShortingEnabled) + " · " + YesNo(acc.TradingBlocked),
                        acc.TradingBlocked == true ? FundPalette.Kill : null));
                }
                cols.Children.Add(c1);
                Grid.SetColumn(c2, 2);
                cols.Children.Add(c2);
                stack.Children.Add(cols);
                stack.Children.Add(BLine(binance
                    ? "Reconciled: the USDT total and every position (balance − baseline). The demo reports no equity; the platform values its book with its own quotes."
                    : "Equity is shown but not reconciled (the broker marks with its own prices); cash and every position are.",
                    10, FundPalette.Faint));
            }

            // reconciliation
            stack.Children.Add(BSubHead("Reconciliation"));
            var rec = b.Reconciliation;
            var (recText, recBrush) = BrokerText.ReconcileChip(rec, b.Halt);
            var recHead = new WrapPanel();
            recHead.Children.Add(BChip(recText, recBrush));
            recHead.Children.Add(FundUi.Text("   " + (rec?.At != null ? "last " + TradingText.Ts(rec.At) + " · trigger " + (rec.Trigger ?? "—") : "none yet"), 10.5, FundPalette.Faint, wrap: false));
            stack.Children.Add(recHead);
            if (rec?.Status == "mismatch" || (b.IsHalted && b.Halt?.Kind == "reconcile"))
            {
                var reason = !string.IsNullOrWhiteSpace(rec?.Reason) ? rec!.Reason! : (b.Halt?.Reason ?? "No reason reported.");
                var mm = rec?.Mismatches ?? new List<BrokerMismatch>();
                var text = reason + (mm.Count == 0 ? "" : "\n" + string.Join("\n", mm.Select(m => "• " + (m.Kind ?? "?") + (string.IsNullOrWhiteSpace(m.Symbol) ? "" : " · " + m.Symbol) + " — " + (m.Message ?? ""))));
                stack.Children.Add(BCallout(text + "\nNothing is corrected automatically — use Resolve… (recheck after fixing it at the broker, cancel unknown orders, or accept the broker's values).",
                                            FundPalette.Kill, FundPalette.KillSurface));
            }
            else if (rec?.Status == "unreachable")
            {
                stack.Children.Add(BCallout("The broker could not be read: " + (rec.Reason ?? "no reason reported") + " — no halt, but no new orders until a reconciliation is OK.",
                                            FundPalette.Risk, FundPalette.Make(0x1FD9A54B)));
            }
            else if (rec?.Status == "ok")
            {
                stack.Children.Add(BLine("Positions, cash" + (binance ? " (USDT)" : "") + " and open orders match the platform's expected book.", 10.5, FundPalette.Dim));
            }
            var recButtons = new WrapPanel();
            var recon = BButton("Reconcile now", "Reconcile " + id + " now", busy || b.Can?.Reconcile != true);
            recon.ToolTip = b.Can?.Reconcile == true ? "Reads the broker once and compares positions, cash and open orders (POST /v2/brokers/" + id + "/reconcile)." : "Only while connected.";
            recon.Click += async (_, _) => await BrokerActionSafeAsync(id, () => ReconcileBrokerAsync(id));
            recButtons.Children.Add(recon);
            var resolve = BButton("Resolve…", "Resolve the reconciliation mismatch of " + id, busy || b.Can?.Resolve != true);
            resolve.ToolTip = b.Can?.Resolve == true ? "Choose how to resolve the mismatch — you decide, nothing is corrected automatically." : "Only when a mismatch or a reconciliation halt exists.";
            resolve.Click += async (_, _) => await BrokerActionSafeAsync(id, () => ResolveBrokerAsync(b));
            recButtons.Children.Add(resolve);
            stack.Children.Add(recButtons);

            // routes
            var routes = b.Routes ?? new List<BrokerRouteRef>();
            stack.Children.Add(BLine(routes.Count == 0 ? "No strategy is routed here." : "Routed here: " + string.Join(", ", routes.Select(r => (r.Name ?? "?") + " (" + r.StrategyId + ")")),
                                     10.5, FundPalette.Dim));

            // actions
            stack.Children.Add(BSubHead("Connection"));
            var actions = new WrapPanel();
            var test = BButton(busy ? "Working…" : "Test connection", "Test the connection to " + id, busy || b.Can?.Test == false);
            test.ToolTip = "Checks the SDK, the keys, the paper/demo endpoint and reads account, positions and open orders"
                           + (binance ? ", the server time, the markets (exchangeInfo) and the balances" : "") + " — sends no order.";
            test.Click += async (_, _) => await BrokerActionSafeAsync(id, () => TestBrokerAsync(id));
            actions.Children.Add(test);
            var connectText = b.IsBaselineMissing && b.IsConnected ? "Confirm starting balances…" : conn?.Status == "disconnected" ? "Reconnect…" : "Connect…";
            var connect = BButton(connectText, (b.IsBaselineMissing && b.IsConnected ? "Confirm the starting balances of " : "Connect ") + id, busy || b.Can?.Connect != true);
            connect.Click += async (_, _) => await BrokerActionSafeAsync(id, () => ConnectBrokerAsync(b));
            actions.Children.Add(connect);
            var disconnect = BButton("Disconnect…", "Disconnect " + id, busy || b.Can?.Disconnect != true);
            disconnect.ToolTip = "Only when the platform and the broker are flat, no orders work, no incubation runs and no strategy is routed here.";
            disconnect.Click += async (_, _) => await BrokerActionSafeAsync(id, () => DisconnectBrokerAsync(b));
            actions.Children.Add(disconnect);
            var kill = FundUi.MakeButton("■ Kill this broker…", FundPalette.KillSurface, FundPalette.Kill, FundPalette.Kill);
            kill.Padding = new Thickness(10, 2, 10, 2);
            kill.MinHeight = 22;
            kill.FontSize = 11;
            kill.Margin = new Thickness(0, 4, 8, 0);
            kill.IsEnabled = !busy && b.Can?.Kill == true;
            kill.ToolTip = b.Can?.Kill == true ? "Halts " + id + ", cancels ALL open orders at the broker and closes every position the broker reports (asks first)." : "Only while connected.";
            AutomationProperties.SetName(kill, "Kill switch for " + id + " only: cancel all and flatten");
            kill.Click += async (_, _) => await BrokerActionSafeAsync(id, () => KillBrokerAsync(id));
            actions.Children.Add(kill);
            stack.Children.Add(actions);

            if (_brokerMessages.TryGetValue(id, out var msg) && !string.IsNullOrWhiteSpace(msg.Text))
            {
                var tone = msg.Tone switch { "ok" => FundPalette.Long, "risk" => FundPalette.Risk, "error" => FundPalette.Kill, _ => FundPalette.Dim };
                var m = BLine(msg.Text, 11, tone);
                AutomationProperties.SetLiveSetting(m, msg.Tone == "error" ? AutomationLiveSetting.Assertive : AutomationLiveSetting.Polite);
                AutomationProperties.SetName(m, msg.Text);
                stack.Children.Add(m);
            }
            if (_brokerTests.TryGetValue(id, out var t)) stack.Children.Add(TestResultPanel(id, t));

            // details (loaded while expanded) + capabilities / notes / terms
            stack.Children.Add(DetailsExpander(id));
            stack.Children.Add(InfoExpander(b));

            var card = FundUi.Panel(stack, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 8));
            AutomationProperties.SetName(card, (b.Label ?? id) + ": " + cText.ToLowerInvariant() + ", " + lText.ToLowerInvariant()
                                               + (b.IsHalted ? ", halted (" + BrokerText.HaltKind(b.Halt?.Kind) + ")" : ""));
            return card;
        }

        // SPEC §18.13 (2026-10-19): a RETIRED broker (Kraken decommissioned its Futures demo) — chip, reason, sources, the key
        // status with the removal hint, every action disabled (the backend answers 409 "retired" anyway).
        private FrameworkElement RetiredBrokerCard(BrokerStatus b)
        {
            var id = b.Id ?? "?";
            var stack = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text((string.IsNullOrWhiteSpace(b.Label) ? id : b.Label!) + "  ", 13, FundPalette.Faint, bold: true, wrap: false));
            head.Children.Add(FundUi.Text(id + "  ", 11, FundPalette.Faint, mono: true, wrap: false));
            head.Children.Add(BChip("RETIRED", FundPalette.Kill));
            stack.Children.Add(head);
            var reason = b.Retired?.Reason ?? "retired";
            stack.Children.Add(BLine("Retired" + (b.Retired?.Since != null ? " since " + b.Retired!.Since : "") + ": " + reason + ".", 11.5, FundPalette.Text));
            var sources = b.Retired?.Sources ?? new List<string>();
            if (sources.Count > 0)
                stack.Children.Add(BLine("Source: " + string.Join(" · ", sources), 10.5, FundPalette.Faint, mono: true));
            stack.Children.Add(BLine("Not connectable and not routable; strategies still routed here are shown as blocked under Routing — re-route them. "
                                     + "Crypto runs on binance-demo (Binance Spot demo) now.", 10.5, FundPalette.Dim));
            var keys = b.Keys ?? new List<BrokerKey>();
            if (keys.Count > 0)
            {
                stack.Children.Add(BLine("Keys: " + string.Join(" · ", keys.Select(k => (k.Label ?? k.Name ?? "key") + (k.Present == true ? " ● set" : k.Error != null ? " — " + k.Error : " ✕ not set"))), 10.5, FundPalette.Faint));
                stack.Children.Add(BLine("These keys are no longer used — you can remove them in Keys & data → Keys.", 10.5, FundPalette.Faint));
            }
            var actions = new WrapPanel();
            foreach (var label in new[] { "Test connection", "Connect…", "Disconnect…", "Reconcile now", "■ Kill this broker…" })
            {
                var btn = BButton(label, label + " " + id + " (retired)", true);
                btn.ToolTip = "Retired — " + reason;
                actions.Children.Add(btn);
            }
            stack.Children.Add(actions);
            if (b.Notes != null && b.Notes.Count > 0) stack.Children.Add(InfoExpander(b));
            var card = FundUi.Panel(stack, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 8));
            AutomationProperties.SetName(card, (b.Label ?? id) + ": retired — " + reason);
            return card;
        }

        private FrameworkElement TestResultPanel(string id, BrokerTestResult t)
        {
            var p = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
            _brokerTestAt.TryGetValue(id, out var at);
            p.Children.Add(BLine((t.Ok ? "✓ Test reached the broker (not connected by this)" : "✕ Test did not reach the broker") + (at != null ? " · " + at : "") + " · endpoint " + (t.Endpoint ?? "—"),
                                 11.5, t.Ok ? FundPalette.Long : FundPalette.Kill));
            foreach (var c in t.Checks ?? new List<DataTestCheck>())
            {
                var line = BLine((c.Ok ? "✓ " : "✕ ") + (c.Name ?? "check") + (string.IsNullOrWhiteSpace(c.Message) ? "" : " — " + c.Message), 11, c.Ok ? FundPalette.Text : FundPalette.Kill);
                line.Margin = new Thickness(12, 1, 0, 1);
                p.Children.Add(line);
            }
            if (t.Currency == "USDT")
            {
                // §18.14 binance-demo: no flat rule — the balances below are what a first connect asks you to confirm (the baseline)
                if (t.CashBasis.HasValue) p.Children.Add(BLine("USDT total at the broker: " + BrokerText.Usdt(t.CashBasis) + " (" + (t.CurrencyNote ?? "USDT — not converted") + ")", 11, FundPalette.Text, mono: true));
                p.Children.Add(BLine("Balances (as reported): " + BrokerText.Balances(t.Balances, 20), 11, FundPalette.Text, mono: true));
                p.Children.Add(BLine("Open orders: " + (t.PlatformOpenOrders?.ToString(BrokerInv) ?? "—") + " with the platform's UFOS_ prefix · "
                                     + (t.ForeignOpenOrders?.ToString(BrokerInv) ?? "—") + " other (never touched by the platform)", 11, FundPalette.Dim));
                if ((t.PlatformOpenOrders ?? 0) > 0)
                    p.Children.Add(BCallout("Open orders with the platform's UFOS_ prefix exist — a first connect is refused until they are cancelled at demo.binance.com.",
                                            FundPalette.Risk, FundPalette.Make(0x1FD9A54B)));
            }
            else if (t.CashBasis.HasValue) p.Children.Add(BLine("Cash basis at the broker: " + FundFormat.Money(t.CashBasis), 11, FundPalette.Text, mono: true));
            if (t.Flat == true && t.Currency != "USDT") p.Children.Add(BLine("The broker account is flat (no positions, no open orders).", 11, FundPalette.Dim));
            if (t.Flat == false && t.Currency != "USDT")
            {
                var lines = new List<string> { "The broker account is NOT flat — a first connect is refused until it is (the platform never imports foreign positions; close / cancel them at the broker"
                                               + (id == PlatformClient.AlpacaPaperId ? " or reset the paper account at alpaca.markets" : " or reset the demo account") + ")." };
                lines.AddRange((t.Positions ?? new List<BrokerPosition>()).Take(20).Select(x => "• position " + BrokerText.PositionLine(x)));
                lines.AddRange((t.OpenOrders ?? new List<BrokerOrder>()).Take(20).Select(x => "• open order " + BrokerText.OrderLine(x)));
                p.Children.Add(BCallout(string.Join("\n", lines), FundPalette.Risk, FundPalette.Make(0x1FD9A54B)));
            }
            AutomationProperties.SetName(p, "Connection test result for " + id + ": " + (t.Ok ? "OK" : "failed"));
            return p;
        }

        private FrameworkElement DetailsExpander(string id)
        {
            var exp = new Expander
            {
                Header = FundUi.Text("Broker details — positions, open orders, reconciliation history, events", 11, FundPalette.Text, wrap: false),
                IsExpanded = _brokerExpanded.Contains(id),
                Foreground = FundPalette.Text,
                Margin = new Thickness(0, 8, 0, 0),
            };
            AutomationProperties.SetName(exp, "Broker details of " + id);
            var body = new StackPanel { Margin = new Thickness(18, 4, 0, 0) };
            if (exp.IsExpanded)
            {
                if (_brokerDetailErrors.TryGetValue(id, out var err)) body.Children.Add(BError(err));
                if (_brokerDetails.TryGetValue(id, out var d)) FillDetails(body, d);
                else if (err == null) body.Children.Add(BLine("Loading…", 11, FundPalette.Faint));
            }
            exp.Content = body;
            exp.Expanded += async (_, _) =>
            {
                if (!_brokerExpanded.Add(id)) return;
                try
                {
                    await LoadBrokerDetailAsync(id);
                    RenderBrokersIfChanged(force: true);
                }
                catch (Exception ex) { AppLog.Warn(LogModule, "Keys & data: broker details failed.", ex); }
            };
            exp.Collapsed += (_, _) =>
            {
                if (_brokerExpanded.Remove(id)) RenderBrokersIfChanged(force: true);
            };
            return exp;
        }

        private static void FillDetails(StackPanel body, BrokerDetail d)
        {
            body.Children.Add(BSubHead("Positions at the broker (last snapshot " + TradingText.Ts(d.Broker?.Snapshot?.At) + ")"));
            var pos = d.Positions ?? new List<BrokerPosition>();
            if (pos.Count == 0) body.Children.Add(BLine("None.", 10.5, FundPalette.Faint));
            foreach (var p in pos) body.Children.Add(BLine(BrokerText.PositionLine(p), 10.5, FundPalette.Text, mono: true));
            body.Children.Add(BSubHead("Open orders at the broker"));
            var oo = d.OpenOrders ?? new List<BrokerOrder>();
            if (oo.Count == 0) body.Children.Add(BLine("None.", 10.5, FundPalette.Faint));
            foreach (var o in oo) body.Children.Add(BLine(BrokerText.OrderLine(o), 10.5, o.Known == true ? FundPalette.Text : FundPalette.Risk, mono: true));
            body.Children.Add(BSubHead("Reconciliations (newest 20)"));
            var recs = d.Reconciliations ?? new List<BrokerReconciliation>();
            if (recs.Count == 0) body.Children.Add(BLine("None yet.", 10.5, FundPalette.Faint));
            foreach (var r in recs)
            {
                var text = TradingText.Ts(r.At) + " · " + (r.Trigger ?? "—") + " · " + (r.Status ?? "—").ToUpperInvariant();
                if (r.Cash != null)
                    text += " · " + (r.Cash.Basis ?? "cash") + " expected " + FundFormat.Money(r.Cash.Expected) + " vs broker " + FundFormat.Money(r.Cash.Broker)
                            + " (tolerance " + FundFormat.Money(r.Cash.Tolerance) + ")";
                if (!string.IsNullOrWhiteSpace(r.Reason)) text += "\n   " + r.Reason;
                else if (!string.IsNullOrWhiteSpace(r.Error)) text += "\n   " + r.Error;
                foreach (var m in r.Mismatches ?? new List<BrokerMismatch>()) text += "\n   • " + (m.Message ?? m.Kind ?? "");
                body.Children.Add(BLine(text, 10.5, r.Status == "ok" ? FundPalette.Dim : r.Status == "mismatch" ? FundPalette.Kill : FundPalette.Risk, mono: true));
            }
            body.Children.Add(BSubHead("Events (newest 50)"));
            var ev = d.Events ?? new List<BrokerEvent>();
            if (ev.Count == 0) body.Children.Add(BLine("None.", 10.5, FundPalette.Faint));
            foreach (var e in ev)
                body.Children.Add(BLine(TradingText.Ts(e.Ts) + " · " + (e.Kind ?? "—") + (string.IsNullOrWhiteSpace(e.Symbol) ? "" : " · " + e.Symbol) + " · " + (e.Title ?? "")
                                        + (string.IsNullOrWhiteSpace(e.Body) ? "" : "\n   " + e.Body), 10.5, FundPalette.Dim));
        }

        private FrameworkElement InfoExpander(BrokerStatus b)
        {
            var body = new StackPanel { Margin = new Thickness(18, 4, 0, 0) };
            foreach (var kv in b.Capabilities ?? new Dictionary<string, JsonElement>())
            {
                var v = kv.Value.ValueKind == JsonValueKind.Array
                    ? string.Join(", ", kv.Value.EnumerateArray().Select(x => StrategyJson.Scalar(x)))
                    : kv.Value.ValueKind is JsonValueKind.True ? "yes" : kv.Value.ValueKind is JsonValueKind.False ? "no" : StrategyJson.Scalar(kv.Value);
                body.Children.Add(FundUi.KeyValue(FundFormat.Label(kv.Key), v.Length == 0 ? "—" : v, null, 10.5));
            }
            foreach (var n in b.Notes ?? new List<string>()) body.Children.Add(BLine("• " + n, 10.5, FundPalette.Dim));
            if (b.Terms != null)
            {
                if (!string.IsNullOrWhiteSpace(b.Terms.Summary)) body.Children.Add(BLine("Terms: " + b.Terms.Summary, 10.5, FundPalette.Dim));
                foreach (var v in b.Terms.Verify ?? new List<string>()) body.Children.Add(BLine("Verify: " + v, 10.5, FundPalette.Risk));
            }
            var key = b.Id ?? "";
            var exp = new Expander
            {
                Header = FundUi.Text("Capabilities, notes and terms (verify)", 11, FundPalette.Text, wrap: false),
                IsExpanded = _brokerInfoExpanded.Contains(key),
                Foreground = FundPalette.Text,
                Margin = new Thickness(0, 4, 0, 0),
                Content = body,
            };
            AutomationProperties.SetName(exp, "Capabilities, notes and terms of " + (b.Id ?? "the broker"));
            exp.Expanded += (_, _) => _brokerInfoExpanded.Add(key);
            exp.Collapsed += (_, _) => _brokerInfoExpanded.Remove(key);
            return exp;
        }

        // ----- routing -----

        private FrameworkElement RoutingPanel()
        {
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text("Routing — which account each strategy trades on", 12.5, FundPalette.TextBright, bold: true));
            stack.Children.Add(BLine("Default for every strategy: " + (_routing?.DefaultAccountId ?? PlatformClient.MainAccountId)
                                     + " (internal paper broker). A route decides the account of the strategy's NEXT incubation run. Switching needs: the target "
                                     + "connected, not halted, its last reconciliation OK; no running incubation; the strategy flat everywhere. Venue rule: "
                                     + "stocks/ETFs → alpaca-paper, crypto → binance-demo (spot, long only — long_only strategies only) or paper-main, mixed → "
                                     + "paper-main only (the Kraken demo is retired).", 10.5, FundPalette.Dim));
            if (_routingError != null) stack.Children.Add(BError("Routing not available: " + _routingError));
            var routes = _routing?.Routes ?? new List<BrokerRoute>();
            if (_routing != null && routes.Count == 0)
                stack.Children.Add(BLine("No validated, incubating or live strategy yet — routes appear here once a strategy passed its holdout.", 11, FundPalette.Faint));
            var brokers = (_brokers?.Brokers ?? new List<BrokerStatus>()).Where(x => !string.IsNullOrEmpty(x.Id))
                                                                         .GroupBy(x => x.Id!, StringComparer.Ordinal)
                                                                         .ToDictionary(g => g.Key, g => g.Last(), StringComparer.Ordinal);
            foreach (var r in routes)
            {
                if (string.IsNullOrEmpty(r.StrategyId)) continue;
                var row = new Grid { Margin = new Thickness(0, 6, 0, 0) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var info = new StackPanel();
                var top = new WrapPanel();
                top.Children.Add(FundUi.Text((r.Name ?? r.StrategyId) + "  ", 11.5, FundPalette.Text, bold: true, wrap: false));
                top.Children.Add(FundUi.Text(r.StrategyId + "  ", 10.5, FundPalette.Faint, mono: true, wrap: false));
                top.Children.Add(BChip("→ " + (r.AccountId ?? "—").ToUpperInvariant(), PlatformClient.IsBrokerId(r.AccountId) ? FundPalette.Focus : FundPalette.Neutral));
                info.Children.Add(top);
                var detail = "state " + (r.State ?? "—") + " · universe " + BrokerRouteDialog.UniverseLabel(r.Universe)
                             + " · allowed: " + string.Join(", ", r.AllowedAccounts ?? new List<string>())
                             + (r.Since != null ? " · since " + TradingText.Ts(r.Since) + (r.By != null ? " by " + r.By : "") : " · default")
                             + (string.IsNullOrWhiteSpace(r.Reason) ? "" : " · " + r.Reason)
                             + (string.IsNullOrWhiteSpace(r.RunningIncubation) ? "" : " · incubation running (" + r.RunningIncubation + ") — the account is fixed until it ends");
                info.Children.Add(BLine(detail, 10.5, FundPalette.Dim));
                foreach (var rf in r.RefusedAccounts ?? new List<BrokerRefusedAccount>())      // §18.14: e.g. binance-demo (long only)
                    info.Children.Add(BLine("not on " + (rf.AccountId ?? "?") + ": " + (rf.Reason ?? rf.Code ?? "refused"), 10.5, FundPalette.Faint));
                if (r.Blocked == true)      // §18.13: a route to a retired broker — never moved silently, the user re-routes
                    info.Children.Add(BLine("BLOCKED — " + (r.BlockedReason ?? "the target broker is retired — re-route the strategy"), 10.5, FundPalette.Kill));
                row.Children.Add(info);
                var change = BButton("Change route…", "Change the route of " + (r.Name ?? r.StrategyId), _brokerBusy.Count > 0);
                change.VerticalAlignment = VerticalAlignment.Center;
                change.Click += async (_, _) => await ChangeRouteSafeAsync(r, brokers);
                Grid.SetColumn(change, 1);
                row.Children.Add(change);
                stack.Children.Add(row);
            }
            if (_brokerMessages.TryGetValue("routing", out var msg) && !string.IsNullOrWhiteSpace(msg.Text))
            {
                var m = BLine(msg.Text, 11, msg.Tone == "ok" ? FundPalette.Long : msg.Tone == "error" ? FundPalette.Kill : FundPalette.Dim);
                AutomationProperties.SetLiveSetting(m, AutomationLiveSetting.Polite);
                AutomationProperties.SetName(m, msg.Text);
                stack.Children.Add(m);
            }
            var card = FundUi.Panel(stack, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 8));
            AutomationProperties.SetName(card, "Routing of strategies to accounts");
            return card;
        }

        // ----- live guard (read only, every item OFF) -----

        private FrameworkElement LiveGuardPanel()
        {
            var stack = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text("Live guard  ", 12.5, FundPalette.TextBright, bold: true, wrap: false));
            head.Children.Add(BChip("LIVE TRADING: OFF", FundPalette.Kill));
            stack.Children.Add(head);
            stack.Children.Add(BCallout(LiveDisabledText, FundPalette.Kill, FundPalette.KillSurface));
            stack.Children.Add(BLine("Built but switched off: live would need ALL four conditions below AND a build that allows it. This build refuses live in every "
                                     + "case, has no live endpoint, and the app has no control that could switch live on. Read only.", 10.5, FundPalette.Dim));
            foreach (var id in PlatformClient.BrokerIds)
            {
                stack.Children.Add(BSubHead(BrokerText.AccountTitle(id)));
                stack.Children.Add(GuardItem("Build allows live trading", "OFF", "hard-coded off in this build"));
                if (_liveGuardErrors.TryGetValue(id, out var err))
                {
                    stack.Children.Add(BError("Live guard status not available: " + err));
                    continue;
                }
                if (!_liveGuards.TryGetValue(id, out var g))
                {
                    stack.Children.Add(BLine("Loading…", 10.5, FundPalette.Faint));
                    continue;
                }
                if (g.BuildAllowsLive == true)
                    stack.Children.Add(BError("Unexpected: the backend reports build_allows_live = true. Do not trade; report this — this app build never enables live."));
                var conds = g.Conditions ?? new List<BrokerLiveCondition>();
                var labels = new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    ["live_flag"] = "1. Live flag switched on for this broker",
                    // live click-test 2026-10-18: the backend's phrase "GO LIVE KRAKEN-DEMO WITH REAL MONEY" contradicts a demo account —
                    // the WPF names the live counterpart neutrally and does not print the phrase
                    ["typed_confirmation"] = "2. Typed confirmation for the live counterpart of " + id + " (not built)",
                    ["strategy_live"] = "3. Strategy in state Live",
                    ["capital_cap"] = "4. Capital cap set",
                };
                foreach (var key in new[] { "live_flag", "typed_confirmation", "strategy_live", "capital_cap" })
                {
                    var c = conds.FirstOrDefault(x => x.Id == key);
                    var state = c?.Ok == true ? "SET — no effect (live is off in this build)" : "OFF";
                    stack.Children.Add(GuardItem(labels[key], state, c?.Message ?? (c == null ? "not reported" : null)));
                }
                var cap = g.Settings?.CapitalCapUsd;
                stack.Children.Add(GuardItem("Capital cap", cap.HasValue ? FundFormat.Money(cap) + " — no effect (live is off)" : "OFF — not set", null));
            }
            var card = FundUi.Panel(stack, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 8));
            AutomationProperties.SetName(card, "Live guard: " + LiveDisabledText);
            return card;
        }

        private static FrameworkElement GuardItem(string label, string state, string? message)
        {
            var g = new Grid { Margin = new Thickness(0, 2, 0, 2), Focusable = true };
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(22) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.Children.Add(FundUi.Text("☐", 12, FundPalette.Faint, wrap: false));
            var text = FundUi.Text(label + (string.IsNullOrWhiteSpace(message) ? "" : " — " + message), 11, FundPalette.Text);
            Grid.SetColumn(text, 1);
            g.Children.Add(text);
            var chip = BChip(state, state.StartsWith("OFF", StringComparison.Ordinal) ? FundPalette.Kill : FundPalette.Risk);
            chip.Margin = new Thickness(10, 0, 0, 0);
            Grid.SetColumn(chip, 2);
            g.Children.Add(chip);
            AutomationProperties.SetName(g, label + ": " + state + (string.IsNullOrWhiteSpace(message) ? "" : ", " + message));
            AutomationProperties.SetItemStatus(g, state);
            return g;
        }

        // =====================================================================================
        // Actions (one at a time per broker; the backend decides and refuses verbatim)
        // =====================================================================================

        /// <summary>
        /// Live click-test 2026-10-22: raised after a broker action (connect / disconnect / resolve / reconcile / kill / route change)
        /// so the host reloads Portfolio &amp; Trading at once instead of waiting for its next poll.
        /// </summary>
        public event Action? BrokersChanged;

        private async Task BrokerActionSafeAsync(string id, Func<Task> action)
        {
            if (_brokerBusy.Contains(id)) return;
            // the control that started the action gets the focus back after the re-render (same automation name)
            var startedBy = Keyboard.FocusedElement is FrameworkElement fe ? AutomationProperties.GetName(fe) : null;
            _brokerBusy.Add(id);
            RenderBrokersIfChanged(force: true);
            try { await action(); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: broker action on " + id + " failed unexpectedly.", ex);
                _brokerMessages[id] = ("The action could not be completed (" + ex.GetType().Name + "). Press ↻ Refresh to see the current state.", "error");
            }
            finally { _brokerBusy.Remove(id); }
            try { await RefreshBrokersAsync(force: true); }
            catch (Exception ex) { AppLog.Warn(LogModule, "Keys & data: broker refresh failed.", ex); }
            BrokersChanged?.Invoke();
            if (!string.IsNullOrEmpty(startedBy) && FindByName(BrokersHost, startedBy) is UIElement again)
                RestoreFocusWithoutScroll(again);   // CS4014 fix: the DispatcherOperation is discarded explicitly there
        }

        private async Task TestBrokerAsync(string id)
        {
            _brokerMessages[id] = ("Testing… (the backend reads the broker through its rate-limited gate — can take a while on a weak connection)", "info");
            RenderBrokersIfChanged(force: true);
            var res = await PlatformClient.TestBrokerAsync(id);
            if (res.Ok && res.Value != null)
            {
                _brokerTests[id] = res.Value;
                _brokerTestAt[id] = "tested " + DateTime.Now.ToString("HH:mm:ss", BrokerInv);
                _brokerMessages.Remove(id);
            }
            else
            {
                _brokerTests.Remove(id);
                _brokerMessages[id] = ("✕ Test not run: " + (res.Ok ? "the backend sent an empty test result." : PlatformClient.DescribeFailure(res)), "error");
            }
        }

        private Task ConnectBrokerAsync(BrokerStatus b)
        {
            var id = b.Id!;
            if (id == PlatformClient.BinanceDemoId) return ConnectBinanceAsync(b);
            var first = b.Connection?.Status != "disconnected";
            var message = (first
                    ? "First connect of " + BrokerText.AccountTitle(id) + ". The backend runs the connection test again and then requires:\n"
                      + "• the account at the broker is FLAT — no positions and no open orders (the platform never imports foreign positions; close / cancel them "
                      + "at the broker" + (id == PlatformClient.AlpacaPaperId ? " or reset the paper account at alpaca.markets" : " or reset the demo account") + " first);\n"
                      + "• its cash is at least $1,000 — it becomes the opening balance of the platform's mirror book.\n\n"
                    : "Reconnect of " + BrokerText.AccountTitle(id) + ". The mirror book continues; the connect reconciliation must be OK — a cash or position "
                      + "change at the broker in the meantime is a mismatch that halts the account until you resolve it.\n\n")
                + "After connecting: the account appears in Portfolio & Trading with its own risk limits (defaults); it is reconciled at every backend start, "
                + "every runtime cycle and every 5 minutes — any mismatch HALTS it (no auto-correction). Strategies stay on paper-main until you route them here. "
                + "Paper / demo only — simulated money, no live endpoint.";
            var extra = new List<UIElement>();
            if (_brokerTests.TryGetValue(id, out var t))
                extra.Add(TradingUi.Note("Last test here: " + (t.Ok ? "OK" : "FAILED") + (t.Flat == true ? " · flat" : t.Flat == false ? " · NOT flat" : "")
                                         + (t.CashBasis.HasValue ? " · cash basis " + FundFormat.Money(t.CashBasis) : ""), t.Ok && t.Flat != false ? FundPalette.Dim : FundPalette.Risk));
            else
                extra.Add(TradingUi.Note("Tip: run \"Test connection\" first to see whether the account is flat.", FundPalette.Faint));

            BrokerActionResult? done = null;
            var dialog = new BrokerConfirmDialog(this, (first ? "Connect " : "Reconnect ") + id, message, extra,
                (first ? "Connect " : "Reconnect ") + id, danger: false, async reason =>
                {
                    var res = await PlatformClient.ConnectBrokerAsync(id, reason);
                    if (res.Ok && res.Value != null) { done = res.Value; return null; }
                    return res.Ok ? "Unexpected response from the backend (connect)." : BrokerText.Refusal(res);
                });
            if (dialog.ShowDialog() != true || done == null) return Task.CompletedTask;
            SettleBroker(id, connected: true);
            var rec = done.Reconciliation;
            _brokerMessages[id] = rec?.Status == "ok"
                ? ("✓ Connected · reconciliation OK. Route strategies to " + id + " in the routing list below.", "ok")
                : ("Connected, but the reconciliation is " + (rec?.Status ?? "unknown") + ": " + (rec?.Reason ?? rec?.Error ?? "no reason reported"), "error");
            return Task.CompletedTask;
        }

        /// <summary>
        /// §18.14 item 7: the first connect of binance-demo — NO flat rule (the demo is pre-funded); instead no open UFOS_ orders, ≥ 1,000 USDT
        /// and the user confirms the starting balances of the last Test (they become the baseline; the backend reads them again and answers
        /// 409 balances_changed when they differ). A reconnect continues the mirror book and its baseline (no balances to confirm).
        /// </summary>
        private Task ConnectBinanceAsync(BrokerStatus b)
        {
            var id = b.Id!;
            // balances are confirmed on the first connect AND whenever the baseline is missing (never assumed empty)
            var rebase = b.IsBaselineMissing;
            var first = rebase || b.Connection?.Status != "disconnected";
            _brokerTests.TryGetValue(id, out var t);
            // a missing baseline makes "Positions readable" fail by design — every other check must be OK
            var testUsable = t != null && t.Balances != null
                             && (t.Ok || (rebase && (t.Checks ?? new List<DataTestCheck>()).All(c => c.Ok || c.Name == "Positions readable")));
            if (first && !testUsable)
            {
                _brokerMessages[id] = ("Run \"Test connection\" first — the first connect asks you to confirm the starting balances the test shows.", "risk");
                return Task.CompletedTask;
            }
            var message = (rebase
                    ? "The baseline of " + BrokerText.AccountTitle(id) + " is missing (e.g. a restored database) — the platform cannot tell its positions "
                      + "from the demo's pre-funded holdings and blocks the account. Confirm the CURRENT balances below as the new baseline; the "
                      + "reconciliation that follows compares the platform's book with them (a difference halts the account until you resolve it).\n\n"
                    : first
                    ? "First connect of " + BrokerText.AccountTitle(id) + ". The Binance demo comes pre-funded, so there is NO flat rule. Instead the backend "
                      + "runs the test again and requires:\n"
                      + "• no open orders with the platform's UFOS_ prefix (other open orders stay and are never touched);\n"
                      + "• at least 1,000 USDT — the USDT total becomes the opening cash of the platform's mirror book;\n"
                      + "• your confirmation of the starting balances below — they are the BASELINE: the pre-funded holdings are not the platform's, "
                      + "its positions and P&L are the changes since now. If a balance changes before you confirm, the backend refuses (test again).\n\n"
                    : "Reconnect of " + BrokerText.AccountTitle(id) + ". The mirror book and the baseline confirmed at the first connect continue; the connect "
                      + "reconciliation must be OK — a balance change at the broker in the meantime is a mismatch that halts the account until you resolve it.\n\n")
                + "Amounts are USDT — not converted. Spot, long only: a short (or a sell below 0) is blocked, never converted. Paper / demo only — simulated "
                + "funds, no live endpoint.";
            var extra = new List<UIElement>();
            Dictionary<string, double>? balances = null;
            if (first && t?.Balances != null)
            {
                balances = new Dictionary<string, double>();
                foreach (var x in t.Balances)
                    if (!string.IsNullOrWhiteSpace(x.Asset) && x.Total is double tot) balances[x.Asset!] = tot;
                extra.Add(TradingUi.Note("Starting balances (Test " + (_brokerTestAt.TryGetValue(id, out var at) ? at : "earlier") + "): "
                                         + BrokerText.Balances(t.Balances, 30), FundPalette.Text));
                // fix1023: no "I confirm" tick — the button itself says that these balances become the baseline
                extra.Add(TradingUi.Note("The button below records these balances as the baseline (they are not the platform's).", FundPalette.Dim));
            }
            BrokerActionResult? done = null;
            var dialog = new BrokerConfirmDialog(this, (rebase ? "Confirm starting balances — " : first ? "Connect " : "Reconnect ") + id, message, extra,
                rebase ? "Record these balances as the new baseline" : first ? "Connect " + id + " — record these balances as the baseline" : "Reconnect " + id,
                danger: false, async reason =>
                {
                    var res = await PlatformClient.ConnectBrokerAsync(id, reason, first ? balances : null);
                    if (res.Ok && res.Value != null) { done = res.Value; return null; }
                    if (!res.Ok && PlatformClient.ReadBrokerRefusal(res.RawBody) is { Code: "balances_changed" })
                        return BrokerText.Refusal(res) + " Close this dialog, run \"Test connection\" again and confirm the new balances.";
                    return res.Ok ? "Unexpected response from the backend (connect)." : BrokerText.Refusal(res);
                });
            if (dialog.ShowDialog() != true || done == null) return Task.CompletedTask;
            SettleBroker(id, connected: true);
            var rec = done.Reconciliation;
            _brokerMessages[id] = rec?.Status == "ok"
                ? ("✓ Connected · baseline recorded · reconciliation OK. Route long-only crypto strategies to " + id + " in the routing list below.", "ok")
                : ("Connected, but the reconciliation is " + (rec?.Status ?? "unknown") + ": " + (rec?.Reason ?? rec?.Error ?? "no reason reported"), "error");
            return Task.CompletedTask;
        }

        private async Task DisconnectBrokerAsync(BrokerStatus b)
        {
            var id = b.Id!;
            var reason = TradingReasonDialog.Ask(this, "Disconnect " + id,
                "Disconnects " + BrokerText.AccountTitle(id) + " from the platform. The backend refuses unless the platform AND the broker are flat, no orders "
                + "work, no incubation runs there and no strategy is routed there. The account's history stays; a later reconnect continues its mirror book. "
                + "Nothing is sent to the broker.", "Disconnect", danger: true);
            if (reason == null) return;
            var res = await PlatformClient.DisconnectBrokerAsync(id, reason);
            _brokerMessages[id] = res.Ok ? ("Disconnected.", "ok") : ("Not disconnected: " + BrokerText.Refusal(res), "error");
            if (res.Ok || res.TimedOut) SettleBroker(id, connected: false);
        }

        /// <summary>autorefresh (2026-10-24): after connect / disconnect the window polls fast until the broker list shows the expected state (max 2 min).</summary>
        private void SettleBroker(string id, bool connected)
        {
            AddSettle("broker:" + id, () => _brokers?.Brokers?.FirstOrDefault(x => x.Id == id) is BrokerStatus b && b.IsConnected == connected, () =>
            {
                _brokerMessages[id] = ("Still not confirmed after 2 minutes: " + id + " is not " + (connected ? "connected" : "disconnected") + " in the broker list — "
                                       + "press ↻ Refresh or check the backend log.", "risk");
                RenderBrokersIfChanged(force: true);
            });
        }

        private async Task ReconcileBrokerAsync(string id)
        {
            _brokerMessages[id] = ("Reconciling…", "info");
            RenderBrokersIfChanged(force: true);
            var res = await PlatformClient.ReconcileBrokerAsync(id);
            if (!res.Ok || res.Value?.Reconciliation == null)
            {
                _brokerMessages[id] = ("Reconciliation not run: " + (res.Ok ? "unexpected response from the backend." : BrokerText.Refusal(res)), "error");
                return;
            }
            var r = res.Value.Reconciliation;
            _brokerMessages[id] = r.Status switch
            {
                "ok" => ("✓ Reconciliation OK at " + TradingText.Ts(r.At) + (res.Value.Broker?.Halt is { Active: true, Kind: "reconcile" } ? " — the reconcile halt stays until you resolve it." : "."), "ok"),
                "mismatch" => ("✕ Mismatch — " + (r.Reason ?? "no reason reported") + " The account is halted; use Resolve….", "error"),
                _ => ("The broker could not be read: " + (r.Error ?? r.Reason ?? "no reason reported"), "risk"),
            };
        }

        private Task ResolveBrokerAsync(BrokerStatus b)
        {
            var id = b.Id!;
            var dialog = new BrokerResolveDialog(this, b);
            if (dialog.ShowDialog() != true || dialog.Result == null) return Task.CompletedTask;
            var r = dialog.Result.Reconciliation;
            var what = dialog.Action switch { "accept_broker" => "accepted the broker values", "cancel_unknown_orders" => "cancelled the unknown orders", _ => "rechecked" };
            _brokerMessages[id] = r?.Status == "ok"
                ? ("✓ Resolved (" + what + ") — reconciliation OK, the reconcile halt is cleared. Auto trading stays off.", "ok")
                : ("Resolution recorded (" + what + "), but the reconciliation is " + (r?.Status ?? "unknown") + ": " + (r?.Reason ?? r?.Error ?? "no reason reported")
                   + " — the account stays halted.", "error");
            return Task.CompletedTask;
        }

        private async Task KillBrokerAsync(string id)
        {
            var values = FundDialogs.Form(this, "Kill " + id + " only",
                "This acts on " + BrokerText.AccountTitle(id) + " ONLY (the toolbar ■ Kill switch is the global one):\n\n"
                + "• the broker account is halted — no new orders;\n"
                + (id == PlatformClient.BinanceDemoId
                    ? "• the platform's open orders at the broker (UFOS_ prefix) are cancelled — other orders on the demo account are left untouched and reported;\n"
                      + "• every position of the platform (balance − the confirmed baseline) is sold with market orders; dust below Binance's LOT_SIZE / NOTIONAL "
                      + "minimum cannot be sold and is listed; the pre-funded holdings are never sold;\n"
                    : "• ALL open orders at the broker are cancelled (also orders the platform does not know) and the platform's working orders there;\n"
                      + "• every position the BROKER reports is closed with market orders;\n")
                + "• if the broker cannot be reached, the halt is set now and the positions are closed automatically as soon as it answers.\n\n"
                + "Resume afterwards in Portfolio & Trading → Portfolio (needs a reconciliation OK). Paper / demo only — simulated money.",
                new[] { ("Reason (optional, up to 200 characters, stored in the audit log)", "") },
                "■ Cancel all and flatten " + id, danger: true);
            if (values == null) return;
            var res = await PlatformClient.KillBrokerAsync(id, values.Length > 0 ? values[0] : null);
            if (!res.Ok || res.Value == null)
            {
                var text = res.TimedOut
                    ? "No answer in time — the kill of " + id + " may be running or done (it waits for the broker to confirm). The broker list reloads; "
                      + "check its state before trying again."
                    : "The backend did not confirm the kill of " + id + ": " + (res.Ok ? "empty response." : PlatformClient.DescribeFailure(res))
                           + "\n\nUse the toolbar ■ Kill switch, or check the account at the broker.";
                _brokerMessages[id] = (text, "error");
                FundDialogs.Info(this, "Kill " + id + " — not confirmed", text);
                return;
            }
            var k = res.Value;
            var orders = k.Orders ?? new List<OmsOrder>();
            var failed = orders.Count(o => o.State == "error" || o.State == "rejected");
            var lines = new List<string>
            {
                (k.Ok && k.Flat == true ? "Halted. " : "Halted, but NOT fully confirmed — " + (k.Error ?? "see below") + ". ")
                + (k.CancelledAtBroker ?? 0).ToString(BrokerInv) + " order(s) cancelled at the broker, " + (k.OmsCancelled ?? 0).ToString(BrokerInv)
                + " working OMS order(s) cancelled, " + (orders.Count == 0 ? "no positions to close." : orders.Count.ToString(BrokerInv) + " closing order(s):"),
            };
            lines.AddRange(orders.Take(12).Select(o => "• " + TradingText.OrderLine(o)));
            if (orders.Count > 12) lines.Add("• …");
            if (!string.IsNullOrWhiteSpace(k.Note)) lines.Add(k.Note!);
            if (failed > 0) lines.Add(failed.ToString(BrokerInv) + " closing order(s) failed (reasons above) — those positions are still open; close them at the broker.");
            if (k.Attempts is int att && att > 0) lines.Add("Close attempts: " + att.ToString(BrokerInv) + ".");
            var flatText = BrokerText.KillFlatText(k.Flat, k.Pending, k.NotFlat, k.OpenOrdersLeft);
            if (flatText != null) lines.Add(flatText);
            else lines.Add("Verified: the broker reports no positions and no open orders.");
            // §18.13 / §18.14: orders that are not the platform's — informational, not a failure (does not affect "clean" / the flat wording)
            if (BrokerText.UntouchedText(k.UntouchedOrders, k.UntouchedNote) is string untouched) lines.Add("ℹ " + untouched);
            // §18.14: positions the kill did not sell (dust / below the baseline / unmapped) — counted as flat, listed verbatim
            // §18.14 review fix: cancels the broker refused — counted with the reasons (the kill is not clean then)
            var refusedCancels = k.Refused ?? new List<BrokerKillRefused>();
            if (refusedCancels.Count > 0)
                lines.Add("✕ " + refusedCancels.Count.ToString(BrokerInv) + " cancel(s) refused by the broker — those orders may still be open:\n"
                          + string.Join("\n", refusedCancels.Take(12).Select(r => "• " + (r.ClientOrderId ?? r.BrokerOrderId ?? "?") + " — " + (r.Reason ?? "no reason reported"))));
            foreach (var sk in (k.Skipped ?? new List<BrokerKillSkipped>()).Take(12))
                lines.Add("ℹ not sold: " + (sk.Symbol ?? "?") + " " + BrokerText.Qty(sk.Qty) + " — " + (sk.Reason ?? "no reason reported"));
            var clean = k.Ok && k.Flat == true && failed == 0 && flatText == null && refusedCancels.Count == 0;
            var summary = string.Join("\n", lines);
            _brokerMessages[id] = (summary, clean ? "ok" : "error");
            FundDialogs.Info(this, BrokerText.KillTitle(id, k.Ok && flatText == null, k.Flat, failed), summary);
        }

        private async Task ChangeRouteSafeAsync(BrokerRoute route, IReadOnlyDictionary<string, BrokerStatus> brokers)
        {
            try
            {
                var dialog = new BrokerRouteDialog(this, route, brokers);
                if (dialog.ShowDialog() == true && dialog.Result != null)
                    _brokerMessages["routing"] = ("✓ " + (dialog.Result.Name ?? dialog.Result.StrategyId) + " is now routed to " + dialog.Result.AccountId
                                                  + " — its next incubation run starts there.", "ok");
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: route dialog failed.", ex);
                _brokerMessages["routing"] = ("The route dialog could not be shown (" + ex.GetType().Name + ") — nothing was changed.", "error");
            }
            try { await RefreshBrokersAsync(force: true); }
            catch (Exception ex) { AppLog.Warn(LogModule, "Keys & data: broker refresh failed.", ex); }
            BrokersChanged?.Invoke();
        }

        // =====================================================================================
        // Small factories (plain text only)
        // =====================================================================================

        private static string YesNo(bool? v) => v == true ? "yes" : v == false ? "no" : "—";

        private static TextBlock BLine(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 2, 0, 2);
            return t;
        }

        private static TextBlock BSubHead(string text)
        {
            var t = FundUi.Text(text, 11, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 10, 0, 3);
            return t;
        }

        private static Border BGap() => new() { Width = 4 };

        private static Border BChip(string text, Brush brush)
        {
            var b = new Border
            {
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(999),
                Padding = new Thickness(6, 0, 6, 1),
                VerticalAlignment = VerticalAlignment.Center,
                Child = FundUi.Text(text, 9.5, brush, bold: true, wrap: false),
            };
            AutomationProperties.SetName(b, text);
            return b;
        }

        private static Button BButton(string text, string name, bool disabled)
        {
            var b = FundUi.MakeButton(text);
            b.Padding = new Thickness(10, 2, 10, 2);
            b.MinHeight = 22;
            b.FontSize = 11;
            b.Margin = new Thickness(0, 4, 8, 0);
            b.IsEnabled = !disabled;
            AutomationProperties.SetName(b, name);
            return b;
        }

        private static Border BCallout(string text, Brush brush, Brush surface)
        {
            var b = new Border
            {
                Background = surface,
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 4, 0, 6),
                Child = FundUi.Text(text, 11, brush == FundPalette.Kill ? FundPalette.Make(0xFFF4A197) : brush),
            };
            AutomationProperties.SetName(b, text);
            return b;
        }

        private static Border BError(string message)
        {
            var b = BCallout(message, FundPalette.Kill, FundPalette.KillSurface);
            AutomationProperties.SetName(b, "Error: " + message);
            AutomationProperties.SetLiveSetting(b, AutomationLiveSetting.Assertive);
            return b;
        }
    }
}

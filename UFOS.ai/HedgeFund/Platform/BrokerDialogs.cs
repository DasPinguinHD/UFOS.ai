using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Dialogs of Settings → Brokers (Strategy Platform stage 8 — SPEC §18, WPF notes §18.12). Chrome: FundPopupWindow
    // (borderless, Esc closes). Nothing is sent before the user confirms (one step, optional note — fix1023); every
    // request goes through PlatformClient; the backend's answer (400 field errors, 409 reasons incl. checks / positions /
    // open orders) is shown inline, verbatim. Broker texts (mismatch messages, reject reasons) are plain text.
    // PAPER / DEMO TRADING ONLY — no dialog here can enable live trading.
    // =====================================================================================

    /// <summary>Display helpers for broker states (plain text, no stand-in values).</summary>
    internal static class BrokerText
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        public static (string Text, Brush Brush) ConnectionChip(BrokerConnection? c) => (c?.Status) switch
        {
            "connected" => ("CONNECTED", FundPalette.Long),
            "disconnected" => ("DISCONNECTED", FundPalette.Faint),
            "never_connected" => ("NOT CONNECTED", FundPalette.Faint),
            null or "" => ("CONNECTION —", FundPalette.Faint),
            _ => (c!.Status!.ToUpperInvariant(), FundPalette.Dim),
        };

        public static (string Text, Brush Brush) LinkChip(BrokerLink? l) => (l?.State) switch
        {
            "ok" => ("BROKER REACHABLE", FundPalette.Long),   // live click-test 2026-10-18: reachability, not the connect state
            "waiting" => ("WAITING FOR BROKER (ATTEMPT " + (l!.Attempt ?? 0).ToString(Inv) + ")", FundPalette.Risk),
            "auth_failed" => ("KEYS REJECTED", FundPalette.Kill),
            "keys_missing" => ("KEYS MISSING", FundPalette.Risk),
            "not_installed" => ("SDK NOT INSTALLED", FundPalette.Kill),
            "error" => ("LINK ERROR", FundPalette.Kill),
            "idle" => ("LINK IDLE", FundPalette.Faint),
            null or "" => ("LINK —", FundPalette.Faint),
            _ => ("LINK " + l!.State!.ToUpperInvariant(), FundPalette.Dim),
        };

        /// <summary>OK / HALTED · MISMATCH / UNREACHABLE / NOT RECONCILED YET — a reconcile halt is sticky (§18.11).</summary>
        public static (string Text, Brush Brush) ReconcileChip(BrokerReconcileState? r, TradingHalt? halt)
        {
            var reconcileHalt = halt?.Active == true && halt.Kind == "reconcile";
            return (r?.Status) switch
            {
                "mismatch" => ("HALTED · RECONCILIATION MISMATCH", FundPalette.Kill),
                "ok" when reconcileHalt => ("HALTED · RESOLVE REQUIRED (last check OK)", FundPalette.Kill),
                "ok" => ("RECONCILIATION OK", FundPalette.Long),
                "unreachable" => ("RECONCILIATION: BROKER UNREACHABLE", FundPalette.Risk),
                "never" or null or "" => (reconcileHalt ? "HALTED · RECONCILIATION" : "NOT RECONCILED YET", reconcileHalt ? FundPalette.Kill : FundPalette.Faint),
                _ => ("RECONCILIATION " + r!.Status!.ToUpperInvariant(), FundPalette.Dim),
            };
        }

        public static string HaltKind(string? kind) => kind switch
        {
            "kill" => "kill switch",
            "drawdown" => "drawdown kill",
            "reconcile" => "reconciliation mismatch",
            null or "" => "halt",
            _ => kind,
        };

        public static string PositionLine(BrokerPosition p)
        {
            var text = (p.Symbol ?? "?") + (string.IsNullOrWhiteSpace(p.BrokerSymbol) || p.BrokerSymbol == p.Symbol ? "" : " (" + p.BrokerSymbol + ")")
                       + " · " + (p.Side ?? "—") + " " + FundFormat.Qty(p.Qty);
            if (p.AvgPrice.HasValue) text += " @ " + FundFormat.Num(p.AvgPrice, 2);
            if (p.MarkPrice.HasValue) text += " · mark " + FundFormat.Num(p.MarkPrice, 2);
            if (p.MarketValue.HasValue) text += " · value " + FundFormat.Money(p.MarketValue);
            if (p.UnrealizedPnl.HasValue) text += " · unrealised " + FundFormat.Money(p.UnrealizedPnl, signed: true);
            if (p.LiquidationPrice.HasValue) text += " · liquidation " + FundFormat.Num(p.LiquidationPrice, 2);
            if (p.InitialMargin.HasValue || p.MaintenanceMargin.HasValue)
                text += " · margin init " + FundFormat.Money(p.InitialMargin) + " / maint " + FundFormat.Money(p.MaintenanceMargin);
            if (p.Leverage.HasValue) text += " · leverage " + FundFormat.Num(p.Leverage, 2) + "×";
            return text;
        }

        public static string OrderLine(BrokerOrder o)
        {
            var text = (o.Side ?? "?").ToUpperInvariant() + " " + FundFormat.Qty(o.Qty) + " " + (o.Symbol ?? o.BrokerSymbol ?? "?")
                       + " · " + (o.Type ?? "—") + (o.LimitPrice.HasValue ? " limit " + FundFormat.Num(o.LimitPrice, 2) : "")
                       + (o.StopPrice.HasValue ? " stop " + FundFormat.Num(o.StopPrice, 2) : "") + " · " + (o.Tif ?? "—")
                       + " · " + (o.Status ?? "—") + (string.IsNullOrWhiteSpace(o.BrokerStatus) ? "" : " (" + o.BrokerStatus + ")");
            if (o.FilledQty is double f && f > 0) text += " · filled " + FundFormat.Qty(f);
            text += o.Known == true ? " · known to the OMS" : " · NOT known to the OMS";
            var cid = o.ClientOrderIdText;
            var bid = o.BrokerOrderIdText;
            if (cid.Length > 0) text += " · client id " + cid;
            if (bid.Length > 0) text += " · broker id " + bid;
            return text;
        }

        /// <summary>
        /// §18.14: orders that are not the platform's (Binance: no UFOS_ prefix) that the kill left
        /// untouched — informational only (not a failure, never part of the "flat" wording). Null when there are none / the backend sent
        /// nothing. The backend's note is shown verbatim (it names the reason); without one a neutral line.
        /// </summary>
        public static string? UntouchedText(int? untouchedOrders, string? untouchedNote)
        {
            var n = untouchedOrders ?? 0;
            if (n <= 0 && string.IsNullOrWhiteSpace(untouchedNote)) return null;
            if (!string.IsNullOrWhiteSpace(untouchedNote)) return untouchedNote!.Trim();
            return n.ToString(Inv) + " order(s) that are not the platform's were left untouched.";
        }

        /// <summary>§18.14: an amount in USDT as reported — "1,234.50 USDT" (never converted to USD, no $ sign).</summary>
        public static string Usdt(double? value, bool signed = false)
        {
            if (value is not double v || double.IsNaN(v) || double.IsInfinity(v)) return FundFormat.Dash;
            return (signed && v > 0 ? "+" : "") + FundFormat.Num(v, 2) + " USDT";
        }

        /// <summary>§18.14: an asset quantity as reported (up to 8 decimals, trailing zeros dropped).</summary>
        public static string Qty(double? value)
        {
            if (value is not double v || double.IsNaN(v) || double.IsInfinity(v)) return FundFormat.Dash;
            return v.ToString("0.########", Inv);
        }

        /// <summary>§18.14: "USDT 5,000.00 · BTC 0.05 · ETH 1 …" (at most <paramref name="max"/>, then "+ n more").</summary>
        public static string Balances(IReadOnlyList<BrokerBalance>? list, int max = 10)
        {
            if (list == null || list.Count == 0) return "none reported";
            var parts = list.Take(max).Select(x => (x.Asset ?? "?") + " " + (x.Asset == "USDT" ? FundFormat.Num(x.Total, 2) : Qty(x.Total))
                                                  + ((x.Locked ?? 0) > 0 ? " (" + Qty(x.Locked) + " locked)" : ""));
            return string.Join(" · ", parts) + (list.Count > max ? " · + " + (list.Count - max).ToString(Inv) + " more" : "");
        }


        /// <summary>
        /// §18.13 verified kill: null when the broker is verified flat and nothing is pending; else the red/amber text —
        /// "Broker NOT flat — positions left: …" (per symbol verbatim: qty, close order + state, reject reason) or "flat state
        /// unknown", the open orders left and that the account stays halted.
        /// </summary>
        public static string? KillFlatText(bool? flat, bool? pending, IReadOnlyList<BrokerKillNotFlat>? notFlat, int? openOrdersLeft)
        {
            var left = notFlat ?? new List<BrokerKillNotFlat>();
            if (flat == true && pending != true && left.Count == 0 && (openOrdersLeft ?? 0) == 0) return null;
            var lines = new List<string>();
            if (flat == false || left.Count > 0)
                lines.Add("Broker NOT flat — positions left: " + (left.Count == 0 ? "(not listed)"
                          : string.Join(", ", left.Select(n => (n.Symbol ?? "?") + " " + FundFormat.Qty(n.BrokerQty)))));
            else if (flat == null)
                lines.Add("Broker flat state unknown — the broker's positions could not be read.");
            else
                lines.Add("Broker flat, but the kill is still pending.");
            foreach (var n in left)
                lines.Add("• " + (n.Symbol ?? "?") + " " + FundFormat.Qty(n.BrokerQty) + " at the broker"
                          + (string.IsNullOrWhiteSpace(n.CloseOrder) ? " · no close order" : " · close order " + n.CloseOrder + " " + (n.CloseState ?? "—"))
                          + (string.IsNullOrWhiteSpace(n.RejectReason) ? "" : " — " + n.RejectReason));
            if (openOrdersLeft is int o && o > 0) lines.Add("Open orders left at the broker: " + o.ToString(Inv) + ".");
            lines.Add("The account stays HALTED" + (pending == true ? "; the kill retries automatically every 15 s until the broker is flat (resume is refused meanwhile)." : "."));
            return string.Join("\n", lines);
        }

        /// <summary>Dialog title of a kill: "halted and flat" ONLY when flat is verified true.</summary>
        public static string KillTitle(string id, bool ok, bool? flat, int failed) =>
            flat == true && ok && failed == 0 ? "Kill " + id + " — halted and flat"
            : flat == false ? "Kill " + id + " — NOT flat, check the results"
            : flat == null ? "Kill " + id + " — flat state unknown, check the results"
            : "Kill " + id + " — check the results";

        /// <summary>The backend's refusal verbatim plus the details of a refused connect (failed checks, positions, open orders).</summary>
        public static string Refusal<T>(PlatformResult<T> result)
        {
            // retest 2026-10-23: a write (connect / disconnect / reconcile / resolve) whose answer did not arrive may still have happened
            if (result.TimedOut)
                return "No answer in time — the action may have gone through. Close this dialog; the broker list reloads — check the state before trying again.";
            var text = PlatformClient.DescribeFailure(result);
            var r = PlatformClient.ReadBrokerRefusal(result.RawBody);
            if (r == null) return text;
            var lines = new List<string>();
            foreach (var c in r.Checks ?? new List<DataTestCheck>())
                if (!c.Ok) lines.Add("✕ " + (c.Name ?? "check") + (string.IsNullOrWhiteSpace(c.Message) ? "" : " — " + c.Message));
            var positions = r.Positions ?? new List<BrokerPosition>();
            if (positions.Count > 0)
            {
                lines.Add("Positions at the broker (" + positions.Count.ToString(Inv) + "):");
                lines.AddRange(positions.Take(20).Select(p => "• " + PositionLine(p)));
                if (positions.Count > 20) lines.Add("• …");
            }
            var orders = r.OpenOrders ?? new List<BrokerOrder>();
            if (orders.Count > 0)
            {
                lines.Add("Open orders at the broker (" + orders.Count.ToString(Inv) + "):");
                lines.AddRange(orders.Take(20).Select(o => "• " + OrderLine(o)));
                if (orders.Count > 20) lines.Add("• …");
            }
            return lines.Count == 0 ? text : text + "\n" + string.Join("\n", lines);
        }

        public static string AccountTitle(string? id) => id switch
        {
            PlatformClient.MainAccountId => "paper-main (internal paper broker)",
            PlatformClient.AlpacaPaperId => "alpaca-paper (Alpaca paper account)",
            PlatformClient.KrakenDemoId => "kraken-demo (Kraken Futures demo — retired)",
            PlatformClient.BinanceDemoId => "binance-demo (Binance Spot demo)",
            null or "" => "—",
            _ => id,
        };
    }

    /// <summary>
    /// A broker action with ONE confirmation step (fix1023, 2026-10-23): the consequences above, an optional audit note and a
    /// precise button text (fix1023: no typed phrase, no confirm phrase sent). <c>submit</c> receives the note and returns null on success or the backend's text
    /// (shown inline, verbatim, the dialog stays open).
    /// </summary>
    internal sealed class BrokerConfirmDialog : FundPopupWindow
    {
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private readonly Func<string, Task<string?>> _submit;   // the optional note → null on success, else the backend's text
        private bool _busy;
        private bool _closed;

        public BrokerConfirmDialog(Window? owner, string title, string message, IEnumerable<UIElement>? extra,
                                   string confirmText, bool danger, Func<string, Task<string?>> submit)
            : base(owner, title, 620, 600, resizable: true)
        {
            _submit = submit;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text(title, 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(message, 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 8);
            msg.LineHeight = 18;
            stack.Children.Add(msg);
            if (extra != null) foreach (var e in extra) stack.Children.Add(e);
            stack.Children.Add(TradingUi.Label(TradingUi.NoteLabel));
            _reason = TradingUi.NoteBox("Optional note for: " + title);
            stack.Children.Add(_reason);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = danger ? TradingUi.Danger(confirmText) : TradingUi.Primary(confirmText);
            AutomationProperties.SetName(_ok, confirmText);
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_closed) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            _reason.TextChanged += (_, _) => Update();
            Closed += (_, _) => _closed = true;
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) => _reason.Focus();
            Update();
        }

        private void Update() => _ok.IsEnabled = !_busy && TradingUi.ValidNote(_reason.Text);

        private async Task SubmitAsync()
        {
            if (_busy || !TradingUi.ValidNote(_reason.Text)) return;
            _busy = true;
            TradingUi.SetBusy(_ok, true, "Sending…");
            Update();
            TradingUi.ShowError(_error, "Sending — the backend talks to the broker, this can take a while on a weak connection…");
            _error.Foreground = FundPalette.Faint;
            string? problem;
            try { problem = await _submit(_reason.Text.Trim()); }
            catch (Exception ex) { problem = "The request could not be sent (" + ex.GetType().Name + ")."; }
            _busy = false;
            TradingUi.SetBusy(_ok, false);
            if (_closed) return;
            _error.Foreground = FundPalette.Kill;
            if (problem == null)
            {
                DialogResult = true;
                return;
            }
            TradingUi.ShowError(_error, problem);
            Update();
        }
    }

    /// <summary>
    /// "Resolve…" a reconciliation mismatch (SPEC §18.5) — the user decides, the platform never corrects on its own:
    /// recheck (after fixing it at the broker), cancel the open orders unknown to the OMS, or accept the broker's values
    /// (one confirmation step since fix1023, no confirm phrase). Each action ends with a new reconciliation; OK clears the reconcile halt (auto
    /// trading stays off). The mismatch details are shown verbatim.
    /// </summary>
    internal sealed class BrokerResolveDialog : FundPopupWindow
    {
        private readonly string _accountId;
        private readonly RadioButton _recheck;
        private readonly RadioButton _cancelUnknown;
        private readonly RadioButton _accept;
        private readonly StackPanel _acceptPanel;
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private bool _busy;
        private bool _closed;

        public BrokerActionResult? Result { get; private set; }
        public string? Action { get; private set; }

        public BrokerResolveDialog(Window? owner, BrokerStatus broker)
            : base(owner, "Resolve reconciliation — " + (broker.Id ?? "?"), 680, 720, resizable: true)
        {
            _accountId = broker.Id ?? "";
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Resolve the reconciliation of " + BrokerText.AccountTitle(broker.Id), 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text(
                "The platform's expected book (mirror) and the broker disagree, so this broker account is HALTED: no new orders at all, also no "
                + "reducing ones — only the kill switch (it uses the broker's own positions) still works. Nothing is corrected automatically. "
                + "Choose how to resolve it; every action ends with a new reconciliation, and only an OK result clears the halt. Auto trading "
                + "is not switched on by it. Paper / demo trading only.", 12, FundPalette.Dim);
            intro.Margin = new Thickness(0, 8, 0, 8);
            intro.LineHeight = 18;
            stack.Children.Add(intro);

            stack.Children.Add(TradingUi.Head("Mismatch (as reported by the backend)"));
            var r = broker.Reconciliation;
            var reason = string.IsNullOrWhiteSpace(r?.Reason) ? (broker.Halt?.Reason ?? "No reason reported.") : r!.Reason!;
            stack.Children.Add(TradingUi.Callout(reason, FundPalette.Kill, FundPalette.KillSurface));
            foreach (var m in r?.Mismatches ?? new List<BrokerMismatch>())
                stack.Children.Add(TradingUi.Note("• " + (m.Kind ?? "?") + (string.IsNullOrWhiteSpace(m.Symbol) ? "" : " · " + m.Symbol) + " — " + (m.Message ?? ""), FundPalette.Text));
            stack.Children.Add(TradingUi.Note("Last reconciliation " + TradingText.Ts(r?.At) + " · trigger " + (r?.Trigger ?? "—") + " · status " + (r?.Status ?? "—"), FundPalette.Faint));

            stack.Children.Add(TradingUi.Head("Action"));
            _recheck = TradingUi.Radio("resolve", "Recheck — I fixed it at the broker (e.g. cancelled a stray order, closed a position)", "Recheck: reconcile again after fixing it at the broker", true);
            _cancelUnknown = TradingUi.Radio("resolve", "Cancel the open orders the OMS does not know (at the broker), then reconcile", "Cancel unknown orders at the broker, then reconcile", false);
            _accept = TradingUi.Radio("resolve", "Accept the broker's values — the mirror adopts the broker's positions, average prices and cash", "Accept the broker values into the mirror book", false);
            foreach (var rb in new[] { _recheck, _cancelUnknown, _accept })
            {
                rb.Margin = new Thickness(0, 2, 0, 4);
                stack.Children.Add(rb);
            }
            _acceptPanel = new StackPanel { Margin = new Thickness(22, 2, 0, 0), Visibility = Visibility.Collapsed };
            _acceptPanel.Children.Add(TradingUi.Callout(
                "Accepting sets the mirror's positions and average prices to the broker's, books one cash adjustment to \"Unallocated\" and "
                + "moves changed positions to \"Unallocated\" (the strategies' sub-ledgers record the transfer); OMS orders that are not open at "
                + "the broker are marked cancelled. Use it only when the broker's numbers are right (e.g. a dividend, a fee or a liquidation the "
                + "platform could not see). It cannot be undone — only corrected by trading.", FundPalette.Risk, FundPalette.Make(0x1FD9A54B)));
            stack.Children.Add(_acceptPanel);

            stack.Children.Add(TradingUi.Label(TradingUi.NoteLabel));
            _reason = TradingUi.NoteBox("Optional note for the resolution");
            stack.Children.Add(_reason);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = TradingUi.Primary("Resolve");
            _ok.IsEnabled = false;
            AutomationProperties.SetName(_ok, "Run the chosen resolution");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_closed) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            foreach (var rb in new[] { _recheck, _cancelUnknown, _accept })
            {
                rb.Checked += (_, _) => Update();
            }
            _reason.TextChanged += (_, _) => Update();
            Closed += (_, _) => _closed = true;
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) => _recheck.Focus();
            Update();
        }

        private string SelectedAction() => _accept.IsChecked == true ? "accept_broker" : _cancelUnknown.IsChecked == true ? "cancel_unknown_orders" : "recheck";

        private void Update()
        {
            var action = SelectedAction();
            _acceptPanel.Visibility = action == "accept_broker" ? Visibility.Visible : Visibility.Collapsed;
            if (!_busy)
            {
                var text = action switch
                {
                    "accept_broker" => "Accept broker values — overwrite the mirror of " + _accountId,
                    "cancel_unknown_orders" => "Cancel unknown orders at the broker, then reconcile",
                    _ => "Recheck — reconcile " + _accountId + " again",
                };
                _ok.Content = text;
                AutomationProperties.SetName(_ok, text);
            }
            _ok.IsEnabled = !_busy && TradingUi.ValidNote(_reason.Text);
        }

        private async Task SubmitAsync()
        {
            var action = SelectedAction();
            if (_busy || !TradingUi.ValidNote(_reason.Text)) return;
            _busy = true;
            TradingUi.SetBusy(_ok, true, "Resolving…");
            Update();
            TradingUi.ShowError(_error, "Resolving — the backend reads the broker and reconciles again…");
            _error.Foreground = FundPalette.Faint;
            var res = await PlatformClient.ResolveBrokerAsync(_accountId, action, _reason.Text.Trim());
            _busy = false;
            TradingUi.SetBusy(_ok, false);
            if (_closed) return;
            _error.Foreground = FundPalette.Kill;
            if (res.Ok && res.Value != null)
            {
                Result = res.Value;
                Action = action;
                DialogResult = true;
                return;
            }
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend (POST /v2/brokers/" + _accountId + "/resolve)." : BrokerText.Refusal(res));
            Update();
        }
    }

    /// <summary>
    /// Change the route (account) of one strategy (SPEC §18.2): the internal paper account paper-main or a connected broker
    /// account allowed by the venue rule. The consequences of the target are explained; one confirmation step with an optional
    /// note (fix1023 — no "ROUTE TO …" phrase any more).
    /// The backend checks everything again (connected, not halted, reconciled, no running incubation, flat) and refuses
    /// with its reason verbatim.
    /// </summary>
    internal sealed class BrokerRouteDialog : FundPopupWindow
    {
        private readonly BrokerRoute _route;
        private readonly List<(RadioButton Button, string AccountId)> _targets = new();
        private readonly TextBlock _consequences;
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private readonly IReadOnlyDictionary<string, BrokerStatus> _brokers;
        private bool _busy;
        private bool _closed;

        public BrokerRoute? Result { get; private set; }

        public BrokerRouteDialog(Window? owner, BrokerRoute route, IReadOnlyDictionary<string, BrokerStatus> brokers)
            : base(owner, "Route strategy — " + (route.Name ?? route.StrategyId ?? "?"), 680, 720, resizable: true)
        {
            _route = route;
            _brokers = brokers;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Where should \"" + (route.Name ?? route.StrategyId) + "\" trade?", 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text(
                "Strategy " + (route.StrategyId ?? "?") + " · state " + (route.State ?? "—") + " · universe " + UniverseLabel(route.Universe)
                + " · currently routed to " + BrokerText.AccountTitle(route.AccountId)
                + (route.Since != null ? " since " + TradingText.Ts(route.Since) + (route.By != null ? " by " + route.By : "") : " (default)") + ".\n"
                + "The route decides the account of the strategy's NEXT incubation run (a running run keeps its account). Venue rule: stocks/ETFs → "
                + "alpaca-paper, crypto → binance-demo (spot, LONG ONLY — only long_only strategies) or paper-main, a mixed universe → paper-main only "
                + "(the Kraken demo is retired, §18.13).", 12, FundPalette.Dim);
            intro.Margin = new Thickness(0, 8, 0, 8);
            intro.LineHeight = 18;
            stack.Children.Add(intro);
            if (!string.IsNullOrWhiteSpace(route.RunningIncubation))
                stack.Children.Add(TradingUi.Callout("This strategy has a running incubation run (" + route.RunningIncubation + ") — its account is fixed. "
                                                     + "The backend refuses a route change until the run is aborted or finished.", FundPalette.Risk, FundPalette.Make(0x1FD9A54B)));

            stack.Children.Add(TradingUi.Head("Target account"));
            var allowed = route.AllowedAccounts ?? new List<string> { PlatformClient.MainAccountId };
            if (route.Blocked == true)
                stack.Children.Add(TradingUi.Callout("BLOCKED — " + (route.BlockedReason ?? "the current target broker is retired") + ". Choose another account.",
                                                     FundPalette.Kill, FundPalette.KillSurface));
            foreach (var id in new[] { PlatformClient.MainAccountId, PlatformClient.AlpacaPaperId, PlatformClient.BinanceDemoId })   // S10-K: no retired kraken-demo target
            {
                // §18.13 (2026-10-19): a retired broker is never offered as a target
                if (brokers.TryGetValue(id, out var target) && target.IsRetired) continue;
                var why = TargetProblem(id, allowed);
                var text = BrokerText.AccountTitle(id) + (id == route.AccountId ? " — current" : "") + (why != null ? " — " + why : "");
                var rb = TradingUi.Radio("route-target", text, "Route to " + id + (why != null ? ", not possible: " + why : ""), false);
                rb.Margin = new Thickness(0, 2, 0, 4);
                rb.IsEnabled = why == null && id != route.AccountId;
                rb.Checked += (_, _) => Update();
                _targets.Add((rb, id));
                stack.Children.Add(rb);
            }

            _consequences = FundUi.Text("Choose a target account.", 11.5, FundPalette.Text);
            _consequences.LineHeight = 17;
            var consBorder = new Border
            {
                Background = FundPalette.Make(0x1A7C8CF8),
                BorderBrush = FundPalette.Focus,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 7, 10, 8),
                Margin = new Thickness(0, 8, 0, 8),
                Child = _consequences,
            };
            AutomationProperties.SetLiveSetting(_consequences, AutomationLiveSetting.Polite);
            stack.Children.Add(consBorder);

            stack.Children.Add(TradingUi.Label(TradingUi.NoteLabel));
            _reason = TradingUi.NoteBox("Optional note for the route change");
            stack.Children.Add(_reason);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = TradingUi.Primary("Switch route");
            _ok.IsEnabled = false;
            AutomationProperties.SetName(_ok, "Switch the route");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_closed) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            _reason.TextChanged += (_, _) => Update();
            Closed += (_, _) => _closed = true;
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) =>
            {
                var first = _targets.FirstOrDefault(t => t.Button.IsEnabled).Button;
                if (first != null) first.Focus(); else cancel.Focus();
            };
            Update();
        }

        public static string UniverseLabel(string? u) => u switch
        {
            "equity" => "stocks/ETFs",
            "crypto" => "crypto",
            "mixed" => "mixed (stocks + crypto)",
            null or "" => "unknown",
            _ => u,
        };

        /// <summary>Why a target cannot be chosen (display only — the backend decides), or null.</summary>
        private string? TargetProblem(string id, IReadOnlyCollection<string> allowed)
        {
            // §18.14: binance-demo is spot, long only — the backend names the reason (refused_accounts, code long_only)
            var refused = _route.RefusedAccounts?.FirstOrDefault(r => r.AccountId == id);
            if (refused != null)
                return refused.Code == "long_only" ? "long only — this strategy needs shorts (portfolio mode is not long_only)" : (refused.Reason ?? "refused by the backend");
            if (!allowed.Contains(id))
                return "not allowed for a " + UniverseLabel(_route.Universe) + " universe (venue rule)";
            if (!PlatformClient.IsBrokerId(id)) return null;
            if (!_brokers.TryGetValue(id, out var b)) return "broker status not loaded";
            if (!b.IsConnected) return "not connected (connect it above first)";
            if (b.IsHalted) return "halted (" + BrokerText.HaltKind(b.Halt?.Kind) + ") — resolve / resume it first";
            if (b.Reconciliation?.Status != "ok") return "last reconciliation is not OK — reconcile it first";
            return null;
        }

        private string? Selected() => _targets.FirstOrDefault(t => t.Button.IsChecked == true).AccountId;

        private void Update()
        {
            var target = Selected();
            if (target == null)
            {
                _consequences.Text = "Choose a target account.";
                if (!_busy) _ok.Content = "Switch route";
                _ok.IsEnabled = false;
                return;
            }
            if (!_busy)
            {
                var okText = "Route " + (_route.Name ?? _route.StrategyId ?? "the strategy") + " to " + target;
                _ok.Content = okText;
                AutomationProperties.SetName(_ok, okText);
            }
            _consequences.Text = target switch
            {
                PlatformClient.MainAccountId =>
                    "What happens: the strategy's next incubation run trades on paper-main, the platform's INTERNAL paper broker (simulated fills "
                    + "with the backtest's costs, slippage, margin and borrow rules). No broker is involved.",
                PlatformClient.AlpacaPaperId =>
                    "What happens: the strategy's next incubation run sends its orders to your Alpaca PAPER account (paper-api.alpaca.markets — "
                    + "simulated money at Alpaca). Fills come from Alpaca's paper engine (whole shares, regular session, easy-to-borrow shorts "
                    + "only, pattern-day-trader protection below $25,000). The platform reconciles every cycle: any difference between its "
                    + "expected book and Alpaca halts the account until you resolve it. A weak connection pauses new orders (\"waiting for "
                    + "broker\") — orders are never sent twice. No real money, no live endpoint.",
                PlatformClient.BinanceDemoId =>
                    "What happens: the strategy's next incubation run sends its orders to your Binance Spot DEMO account (demo-api.binance.com — "
                    + "simulated funds; prices similar to the live exchange, same filters). SPOT, LONG ONLY: buys and sells of BASE/USDT, no short, no "
                    + "margin, no leverage — a short signal is blocked for that order (visible event), never converted. Amounts are USDT — not "
                    + "converted. Orders carry the UFOS_ prefix; Binance's filters (LOT_SIZE, PRICE_FILTER, NOTIONAL) are checked before sending. The "
                    + "platform reconciles every cycle: any difference halts the account until you resolve it. No real money, no live endpoint.",
                _ => "",
            };
            AutomationProperties.SetName(_consequences, _consequences.Text);
            _ok.IsEnabled = !_busy && TradingUi.ValidNote(_reason.Text);
        }

        private async Task SubmitAsync()
        {
            var target = Selected();
            if (_busy || target == null || _route.StrategyId == null) return;
            if (!TradingUi.ValidNote(_reason.Text)) return;
            _busy = true;
            TradingUi.SetBusy(_ok, true, "Switching…");
            Update();
            TradingUi.ShowError(_error, null);
            var res = await PlatformClient.SetBrokerRouteAsync(_route.StrategyId, target, _reason.Text.Trim());
            _busy = false;
            TradingUi.SetBusy(_ok, false);
            if (_closed) return;
            if (res.Ok && res.Value?.Route != null)
            {
                Result = res.Value.Route;
                DialogResult = true;
                return;
            }
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend (POST /v2/brokers/routing)." : BrokerText.Refusal(res));
            Update();
        }
    }
}

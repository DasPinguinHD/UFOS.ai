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
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>Sections Strategies, Netting, Orders, Incubation, Migration and Risk limits of <see cref="TradingView"/> (SPEC §17.13).</summary>
    internal sealed partial class TradingView
    {
        // ----- strategies / allocation -----
        private TradingAllocationPayload? _allocation;
        private string? _allocationError;
        private readonly Dictionary<string, bool> _ledgerOpen = new(StringComparer.Ordinal);

        // ----- incubation -----
        private List<IncubationRun> _runs = new();
        private IncubationDefaults? _incDefaults;
        private string? _runsError;
        private string? _selectedRunId;
        private readonly Dictionary<string, IncubationDetail> _runDetails = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _runDetailErrors = new(StringComparer.Ordinal);
        private bool _suppressRunSelection;

        // ----- orders -----
        private string? _orderAccount;      // null = all accounts
        private string? _orderState;        // null = all states (server-side filter)
        private string? _orderOrigin;       // null = all origins (client-side)
        private string _orderSymbol = "";   // client-side, contains
        private List<OmsOrder> _orders = new();
        private string? _ordersError;
        private string? _selectedOrderId;
        private OmsOrderDetail? _orderDetail;
        private string? _orderDetailError;
        private readonly Dictionary<string, List<OmsFill>> _fills = new(StringComparer.Ordinal);
        private readonly Dictionary<string, long> _fillCursor = new(StringComparer.Ordinal);
        private readonly Dictionary<string, string> _fillErrors = new(StringComparer.Ordinal);
        private ListBox? _orderList;
        private StackPanel? _orderFilters;
        private StackPanel? _orderDetailHost;
        private TextBlock? _orderCount;
        private bool _suppressOrderSelection;
        private const int OrderLimit = 200;
        private const int MaxFillsKept = 5000;

        // ----- migration -----
        private TradingMigrationStatus? _migration;
        private string? _migrationError;
        private TradingDryRun? _dryRun;
        private DateTime? _dryRunAt;
        private string? _dryRunError;

        // ----- risk limits -----
        private TradingSettings? _settings;
        private string? _settingsError;
        private readonly Dictionary<string, TextBox> _settingInputs = new(StringComparer.Ordinal);
        private Button? _settingsSave;
        private TextBlock? _settingsHint;

        // =====================================================================================
        // Shared: account selector, incubation runs
        // =====================================================================================

        private FrameworkElement AccountSelector(string group)
        {
            var row = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            var label = FundUi.Text("Account  ", 11, FundPalette.Faint, wrap: false);
            label.VerticalAlignment = VerticalAlignment.Center;
            row.Children.Add(label);
            foreach (var a in Accounts())
            {
                var id = a.Id!;
                var rb = TradingUi.Radio(group, id + (a.Halted ? " (halted)" : ""), "Account " + id, id == _accountId);
                rb.Checked += async (_, _) =>
                {
                    if (_accountId == id) return;
                    _accountId = id;
                    _signatures.Remove(_section);
                    await LoadSectionAsync(_section, force: true);
                };
                row.Children.Add(rb);
            }
            AutomationProperties.SetName(row, "Choose the platform account");
            return row;
        }

        private bool NoAccount(StackPanel panel)
        {
            if (_accountId != null) return false;
            panel.Children.Clear();
            panel.Children.Add(Line(_state == null ? "Loading…" : "No platform paper account is open yet — open paper-main in the Portfolio tab.", 11.5, FundPalette.Faint));
            return true;
        }

        private async Task LoadRunsAsync()
        {
            var res = await PlatformClient.ListIncubationAsync();
            if (_stopped) return;
            if (res.Ok && res.Value != null)
            {
                _runs = res.Value.Runs ?? new List<IncubationRun>();
                _incDefaults = res.Value.Defaults ?? _incDefaults;
                _runsError = null;
            }
            else _runsError = res.Ok ? "Unexpected response from the backend (/v2/incubation)." : PlatformClient.DescribeFailure(res);
        }

        private async Task LoadRunDetailAsync(string runId)
        {
            var res = await PlatformClient.GetIncubationAsync(runId);
            if (_stopped) return;
            if (res.Ok && res.Value != null)
            {
                _runDetails[runId] = res.Value;
                _runDetailErrors.Remove(runId);
            }
            else _runDetailErrors[runId] = res.Ok ? "Unexpected response from the backend (/v2/incubation/" + runId + ")." : PlatformClient.DescribeFailure(res);
        }

        private IncubationRun? RunOf(TradingSource s)
        {
            if (!string.IsNullOrEmpty(s.IncubationId))
            {
                var r = _runs.FirstOrDefault(x => x.Id == s.IncubationId);
                if (r != null) return r;
            }
            return _runs.FirstOrDefault(x => x.SourceId == s.SourceId && x.AccountId == _accountId);
        }

        /// <summary>"DAY 12 OF 60" — completed sessions of the fixed length.</summary>
        private static string DayText(IncubationRun r)
            => "DAY " + (r.Progress?.SessionsElapsed ?? 0).ToString(CultureInfo.InvariantCulture) + " OF "
               + (r.LengthSessions ?? r.Progress?.SessionsTotal ?? 0).ToString(CultureInfo.InvariantCulture);

        private static string DayHelp(IncubationRun r)
            => (r.Progress?.SessionsElapsed ?? 0).ToString(CultureInfo.InvariantCulture) + " of "
               + (r.LengthSessions ?? 0).ToString(CultureInfo.InvariantCulture) + " sessions completed (traded "
               + (r.Progress?.Traded ?? 0).ToString(CultureInfo.InvariantCulture) + ", missed " + (r.Progress?.Missed ?? 0).ToString(CultureInfo.InvariantCulture)
               + "). The length " + (r.LengthSessions ?? 0).ToString(CultureInfo.InvariantCulture) + " was fixed at the start and cannot change. Runs "
               + (r.StartDate ?? "?") + " → " + (r.EndDate ?? "?") + " (" + (r.Calendar == "utc" ? "UTC days" : "NYSE sessions") + ").";

        private static Brush ResultBrush(string? result) => result switch
        {
            "passed" => FundPalette.Long,
            "failed" => FundPalette.Kill,
            "inconclusive" => FundPalette.Risk,
            _ => FundPalette.Dim,
        };

        /// <summary>State chips of a source: incubating day x of N (N immutable) / incubation complete / active / released, plus paused.</summary>
        private List<(string Text, Brush Color, string Help)> SourceStates(TradingSource s, TradingAccountSummary? acc)
        {
            var list = new List<(string, Brush, string)>();
            switch (s.Kind)
            {
                case "unallocated":
                    list.Add(("CASH POOL", FundPalette.Faint, "Cash not allocated to a strategy (deposits, released capital, manual orders booked here)."));
                    return list;
                case "council":
                    list.Add(("HELD · NO TARGET SOURCE", FundPalette.Neutral, "The migrated positions of the old account. They are held until the Council of AIs becomes a platform "
                                                                               + "strategy (stage 10); you can reduce or close them with manual paper orders."));
                    return list;
            }
            var run = RunOf(s);
            if (FundUi.FixtureText(run?.Fixture, run?.FixtureNote) is string fixtureText)   // sandbox fixture (SPEC "Sandbox profile" S4)
                list.Add(("SANDBOX FIXTURE · NO REAL VALIDATION", FundPalette.Kill, fixtureText));
            if (s.State == "incubation" && run?.IsRunning == true)
                list.Add(("INCUBATING · " + DayText(run), FundPalette.Focus, DayHelp(run)));
            else if (s.State == "incubation" && run?.Status == "completed")
                list.Add(("INCUBATION COMPLETE · " + (run.Result ?? "no result").ToUpperInvariant(), ResultBrush(run.Result),
                          "The run is complete; the strategy keeps trading on paper (state incubation) until you abort or retire it. Live trading is off."));
            else if (s.State == "live" || s.State == "scaled")
                list.Add(("ACTIVE", FundPalette.Long, "Promoted beyond incubation."));
            else
                list.Add(("RELEASED · " + StrategyStates.Label(s.State).ToUpperInvariant(), FundPalette.Faint,
                          "No longer incubating: its positions are closed to 0 at the next tradable moment, then its cash returns to Unallocated."));
            if (acc?.Halted == true)
                list.Add(("PAUSED · ACCOUNT HALTED", FundPalette.Kill, "The account is halted — no new orders until you resume it."));
            else if (_state?.AutoTrade?.Enabled != true)
                list.Add(("PAUSED · AUTO TRADING OFF", FundPalette.Risk, "The runtime still decides and records targets but sends no orders; incubation sessions count as missed."));
            return list;
        }

        // =====================================================================================
        // Strategies (sub-ledgers + allocation)
        // =====================================================================================

        private async Task LoadStrategiesAsync()
        {
            if (NoAccount(_strategiesPanel)) return;
            var id = _accountId!;
            await LoadDetailAsync(id);
            var al = await PlatformClient.GetAllocationAsync(id);
            if (_stopped) return;
            if (al.Ok && al.Value != null) { _allocation = al.Value; _allocationError = null; }
            else { _allocation = null; _allocationError = al.Ok ? "Unexpected response from the backend (/v2/portfolio/allocation)." : PlatformClient.DescribeFailure(al); }
            await LoadRunsAsync();
            if (_accountId != id) return;
            RenderIfChanged(SecStrategies, new object?[]
            {
                id, _details.GetValueOrDefault(id), _detailErrors.GetValueOrDefault(id), _allocation, _allocationError, _runs, _runsError,
                _state?.AutoTrade?.Enabled, Account(id)?.Halt,
            }, RenderStrategies);
        }

        private void RenderStrategies()
        {
            var p = _strategiesPanel;
            p.Children.Clear();
            var id = _accountId;
            if (id == null) return;
            p.Children.Add(AccountSelector("PtAcctStrategies"));
            _details.TryGetValue(id, out var detail);
            var acc = Account(id);

            // ----- allocation -----
            var box = new StackPanel();
            box.Children.Add(FundUi.Text("Allocation · inverse volatility with a correlation penalty", 12, FundPalette.TextBright, bold: true));
            box.Children.Add(Line("raw = (1 / vol) ÷ (1 + λ · mean positive correlation with the other strategies) → normalised → strategies below the minimum weight get 0 → "
                                  + "capped at the maximum weight (the excess stays unallocated cash, it is not redistributed) → × max total. "
                                  + "Vol = annualised std of the strategy's own sub-ledger returns (≥ 20 sessions), else of its holdout curve. "
                                  + "Rebalanced monthly, when a strategy enters or leaves incubation, or on your request; only drifts beyond the band move capital.",
                                  10.5, FundPalette.Dim));
            if (_allocationError != null) box.Children.Add(ErrorBlock(_allocationError));
            var al = _allocation?.Allocation;
            if (_allocation != null && al == null)
                box.Children.Add(Line("No allocation yet — the first one happens when a strategy enters incubation (or with \"Rebalance now\").", 11, FundPalette.Faint));
            if (al != null && al.IsNoAllocation)
            {
                // review fix L2 (§17.14): no strategy got a weight > 0 — NO capital moved; the backend's reason verbatim
                box.Children.Add(Callout("No allocation — no capital moved (capital stays where it is). "
                                         + (string.IsNullOrWhiteSpace(al.StatusReason) ? "The backend gave no reason." : al.StatusReason!), FundPalette.Risk, RiskSurface));
            }
            if (al != null)
            {
                box.Children.Add(FundUi.KeyValue("Latest allocation", "#" + al.IdText + " · " + TradingText.Ts(al.CreatedAt) + " · by " + (al.Actor ?? "—")));
                box.Children.Add(FundUi.KeyValue("Reason", al.Reason ?? "—"));
                box.Children.Add(FundUi.KeyValue("Pool · unallocated", FundFormat.Money(al.Pool) + " · " + (al.IsNoAllocation ? "— (no allocation)" : FundFormat.Fraction(al.UnallocatedWeight, 1))));
                foreach (var t in al.Transfers ?? new List<TradingTransfer>())
                    box.Children.Add(Line("Transfer " + FundFormat.Money(t.Amount, signed: true) + " → " + SourceName(SourceNames(detail), t.SourceId), 10.5, FundPalette.Dim, mono: true));
            }
            if (_allocation?.Params is JsonElement prm && prm.ValueKind == JsonValueKind.Object)
                box.Children.Add(Line("Parameters: " + string.Join(" · ", prm.EnumerateObject().Select(x => x.Name + " " + FundFormat.Plain(x.Value)))
                                      + (string.IsNullOrEmpty(_allocation.NextRebalance) ? "" : " · next monthly rebalance " + _allocation.NextRebalance), 10.5, FundPalette.Faint, mono: true));
            var reb = SmallButton("Rebalance now…", "Rebalance the allocation of " + id + " now (asks for confirmation)");
            reb.ToolTip = "Recomputes the weights now and moves capital for strategies beyond the drift band (virtual transfers inside the account)";
            InFlightButton(reb, "rebalance:" + id, () => RebalanceAsync(id));
            box.Children.Add(reb);
            p.Children.Add(FundUi.Panel(box));

            // ----- sources -----
            if (_detailErrors.TryGetValue(id, out var derr)) p.Children.Add(ErrorBlock(derr));
            if (_runsError != null) p.Children.Add(ErrorBlock("Incubation runs: " + _runsError));
            var sources = detail?.Sources ?? new List<TradingSource>();
            if (detail == null) p.Children.Add(Line("Loading…", 11, FundPalette.Faint));
            else if (sources.Count == 0) p.Children.Add(Line("No sources in this account yet.", 11, FundPalette.Faint));
            var order = new Dictionary<string, int>(StringComparer.Ordinal) { ["strategy"] = 0, ["council"] = 1, ["unallocated"] = 2 };
            foreach (var s in sources.OrderBy(x => order.TryGetValue(x.Kind ?? "", out var k) ? k : 3).ThenBy(x => x.Name, StringComparer.OrdinalIgnoreCase))
                p.Children.Add(SourceCard(id, s, acc, al));
            p.Children.Add(Line("Invariant (checked by the backend's tests): the sub-ledgers' cash adds up to the account's cash and their quantities to the account's "
                                + "quantity per symbol. P&L is average-cost; costs are by kind (dividends signed: + received).", 10.5, FundPalette.Faint));
        }

        private FrameworkElement SourceCard(string accountId, TradingSource s, TradingAccountSummary? acc, TradingAllocation? al)
        {
            var stack = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text((string.IsNullOrWhiteSpace(s.Name) ? s.SourceId : s.Name) + "  ", 12.5, FundPalette.TextBright, bold: true, wrap: false));
            if (s.Version is int ver) head.Children.Add(FundUi.Text("v" + ver.ToString(Inv) + "  ", 11, FundPalette.Faint, wrap: false));
            head.Children.Add(FundUi.Text((s.SourceId ?? "") + "  ", 10.5, FundPalette.Faint, mono: true, wrap: false));
            foreach (var (text, brush, help) in SourceStates(s, acc))
            {
                var chip = Chip(text, brush);
                chip.ToolTip = help;
                AutomationProperties.SetHelpText(chip, help);
                head.Children.Add(chip);
                head.Children.Add(new Border { Width = 4 });
            }
            stack.Children.Add(head);

            var w = al?.Weights?.FirstOrDefault(x => x.SourceId == s.SourceId);
            if (s.Kind == "strategy")
            {
                var wText = al?.IsNoAllocation == true ? "— (no allocation made)" : FundFormat.Fraction(s.AllocationWeight ?? w?.Weight, 1);
                var detailText = w == null ? "not in the latest allocation"
                    : "target capital " + FundFormat.Money(w.TargetCapital) + " · vol " + FundFormat.Fraction(w.Vol, 1) + " (" + (w.VolSource ?? "—") + ")"
                      + " · mean corr " + FundFormat.Num(w.MeanCorr, 2) + " · penalty ×" + FundFormat.Num(w.Penalty, 2) + (w.Capped == true ? " · CAPPED" : "")
                      + (string.IsNullOrWhiteSpace(w.Note) ? "" : " · " + w.Note);
                stack.Children.Add(FundUi.KeyValue("Allocation weight", wText + "  (" + detailText + ")", FundPalette.TextBright));
            }
            var cols = new Grid { Margin = new Thickness(0, 4, 0, 0) };
            for (var i = 0; i < 3; i++)
            {
                cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                if (i < 2) cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });
            }
            var c1 = new StackPanel();
            c1.Children.Add(FundUi.KeyValue("Capital in", FundFormat.Money(s.CapitalIn)));
            c1.Children.Add(FundUi.KeyValue("Cash", FundFormat.Money(s.Cash)));
            c1.Children.Add(FundUi.KeyValue("Equity", FundFormat.Money(s.Equity) + (s.Valued == false ? " (not valued)" : ""), s.Valued == false ? FundPalette.Risk : FundPalette.TextBright));
            var c2 = new StackPanel();
            c2.Children.Add(FundUi.KeyValue("P&L realised", FundFormat.Money(s.Pnl?.Realized, signed: true), FundPalette.ForSigned(s.Pnl?.Realized)));
            c2.Children.Add(FundUi.KeyValue("P&L unrealised", FundFormat.Money(s.Pnl?.Unrealized, signed: true), FundPalette.ForSigned(s.Pnl?.Unrealized)));
            c2.Children.Add(FundUi.KeyValue("P&L total", FundFormat.Money(s.Pnl?.Total, signed: true), FundPalette.ForSigned(s.Pnl?.Total)));
            var c3 = new StackPanel();
            c3.Children.Add(FundUi.KeyValue("Long · short MV", FundFormat.Money(s.Exposure?.LongMv) + " · " + FundFormat.Money(s.Exposure?.ShortMv)));
            c3.Children.Add(FundUi.KeyValue("Gross · net exposure", FundFormat.Pct(s.Exposure?.GrossPct, 1) + " · " + FundFormat.Pct(s.Exposure?.NetPct, 1, signed: true)));
            var costTotal = (s.Costs?.Fees ?? 0) + (s.Costs?.Borrow ?? 0) + (s.Costs?.Liquidation ?? 0);   // funding and dividends are signed — shown separately below
            c3.Children.Add(FundUi.KeyValue("Fees + borrow + liquidation", FundFormat.Money(s.Costs == null ? null : costTotal)));
            cols.Children.Add(c1);
            Grid.SetColumn(c2, 2);
            cols.Children.Add(c2);
            Grid.SetColumn(c3, 4);
            cols.Children.Add(c3);
            stack.Children.Add(cols);
            if (s.Costs != null)
                stack.Children.Add(Line("Costs · fees " + FundFormat.Money(s.Costs.Fees) + " · slippage " + FundFormat.Money(s.Costs.Slippage) + " (informational, in the fill price) · borrow "
                                        + FundFormat.Money(s.Costs.Borrow) + " · funding " + FundFormat.Funding(s.Costs.Funding) + " · dividends " + FundFormat.Money(s.Costs.Dividends, signed: true)
                                        + " · liquidation " + FundFormat.Money(s.Costs.Liquidation), 10.5, FundPalette.Dim, mono: true));
            var positions = s.Positions ?? new List<TradingSourcePosition>();
            if (positions.Count > 0)
            {
                var widths = new double[] { 90, 80, 80, 80, 100, 0 };
                stack.Children.Add(Row(widths, new[] { "Symbol", "Qty", "Avg", "Last", "Market value", "Unrealised" }, header: true));
                foreach (var pos in positions)
                    stack.Children.Add(Row(widths, new[] { pos.Symbol ?? "?", FundFormat.Qty(pos.Qty), FundFormat.Num(pos.AvgPrice, 2), FundFormat.Num(pos.LastPrice, 2),
                                                           FundFormat.Money(pos.MarketValue), FundFormat.Money(pos.UnrealizedPnl, signed: true) },
                                           brushes: new Brush?[] { FundPalette.TextBright, null, null, null, null, FundPalette.ForSigned(pos.UnrealizedPnl) }));
            }
            else stack.Children.Add(Line("No positions.", 10.5, FundPalette.Faint));
            if (!string.IsNullOrEmpty(s.SourceId)) stack.Children.Add(LedgerExpander(accountId, s.SourceId!));
            var card = FundUi.Panel(stack, margin: new Thickness(0, 0, 0, 8));
            AutomationProperties.SetName(card, "Sub-ledger " + (s.Name ?? s.SourceId) + ", equity " + FundFormat.Money(s.Equity) + ", total P and L " + FundFormat.Money(s.Pnl?.Total, signed: true));
            return card;
        }

        private FrameworkElement LedgerExpander(string accountId, string sourceId)
        {
            var key = accountId + "|" + sourceId;
            var exp = new Expander
            {
                Header = FundUi.Text("Ledger entries (newest 200)", 11, FundPalette.Text, wrap: false),
                Foreground = FundPalette.Text,
                Margin = new Thickness(0, 6, 0, 0),
                IsExpanded = _ledgerOpen.TryGetValue(key, out var open) && open,
            };
            AutomationProperties.SetName(exp, "Ledger entries of " + sourceId);
            var host = new StackPanel { Margin = new Thickness(16, 4, 0, 0) };
            exp.Content = host;
            async Task FillAsync()
            {
                host.Children.Clear();
                host.Children.Add(Line("Loading…", 10.5, FundPalette.Faint));
                var res = await PlatformClient.GetSourceLedgerAsync(accountId, sourceId, 200);
                host.Children.Clear();
                if (!res.Ok || res.Value == null)
                {
                    host.Children.Add(ErrorBlock(res.Ok ? "Unexpected response from the backend (ledger)." : PlatformClient.DescribeFailure(res)));
                    return;
                }
                var entries = res.Value.Entries ?? new List<TradingLedgerEntry>();
                if (entries.Count == 0) { host.Children.Add(Line("No entries.", 10.5, FundPalette.Faint)); return; }
                var widths = new double[] { 120, 90, 80, 80, 80, 100, 0 };
                host.Children.Add(Row(widths, new[] { "Time", "Kind", "Symbol", "Qty", "Price", "Amount", "Ref · note" }, header: true));
                foreach (var e in entries)
                    host.Children.Add(Row(widths, new[] { TradingText.TsShort(e.Ts), e.Kind ?? "?", e.Symbol ?? "", e.Qty is double q && Math.Abs(q) > 0 ? FundFormat.Qty(q) : "",
                                                          e.Price.HasValue ? FundFormat.Num(e.Price, 2) : "", FundFormat.Money(e.Amount, signed: true),
                                                          string.Join(" · ", new[] { e.Ref, e.Note }.Where(x => !string.IsNullOrWhiteSpace(x))) },
                                          brushes: new Brush?[] { FundPalette.Faint, null, null, null, null, FundPalette.ForSigned(e.Amount), FundPalette.Dim }));
            }
            exp.Expanded += async (_, _) => { _ledgerOpen[key] = true; await FillAsync(); };
            exp.Collapsed += (_, _) => _ledgerOpen[key] = false;
            if (exp.IsExpanded) _ = FillAsync();
            return exp;
        }

        private async Task RebalanceAsync(string accountId)
        {
            var reason = TradingReasonDialog.Ask(Owner, "Rebalance " + accountId,
                "Recomputes the allocation now (inverse volatility with the correlation penalty) and moves capital between Unallocated and the strategies whose "
                + "target differs by more than the drift band. Capital moves are virtual (inside the account); the runtime then trades each strategy to its targets.",
                "Rebalance " + accountId + " now");
            if (reason == null) return;
            var res = await PlatformClient.RebalanceAsync(accountId, reason);
            if (!res.Ok) FundDialogs.Info(Owner, "Rebalance", PlatformClient.DescribeFailure(res));
            else if (res.Value?.Allocation is TradingAllocation done && done.IsNoAllocation)
                FundDialogs.Info(Owner, "Rebalance — no allocation", "No allocation was made and NO capital moved (capital stays where it is).\n\n"
                                                                     + (string.IsNullOrWhiteSpace(done.StatusReason) ? "The backend gave no reason." : done.StatusReason!));
            _signatures.Remove(SecStrategies);
            await LoadSectionAsync(_section, force: true);
        }

        // =====================================================================================
        // Netting (per instrument: target vs current, contributing strategies)
        // =====================================================================================

        private async Task LoadNettingAsync()
        {
            if (NoAccount(_nettingPanel)) return;
            var id = _accountId!;
            await LoadDetailAsync(id);
            await LoadRunsAsync();
            var runIds = (_details.GetValueOrDefault(id)?.Sources ?? new List<TradingSource>())
                         .Where(s => s.Kind == "strategy").Select(RunOf).Where(r => r?.Id != null).Select(r => r!.Id!).Distinct().ToList();
            foreach (var r in runIds) await LoadRunDetailAsync(r);
            if (_accountId != id) return;
            RenderIfChanged(SecNetting, new object?[]
            {
                id, _details.GetValueOrDefault(id), _detailErrors.GetValueOrDefault(id), _runs, runIds.Select(r => _runDetails.GetValueOrDefault(r)).ToList(),
                runIds.Select(r => _runDetailErrors.GetValueOrDefault(r)).ToList(),
            }, RenderNetting);
        }

        private static bool IsCrypto(string symbol, string? assetClass)
            => assetClass == "crypto" || symbol.EndsWith("-USD", StringComparison.OrdinalIgnoreCase) || symbol.EndsWith("-USDT", StringComparison.OrdinalIgnoreCase)
               || symbol.EndsWith("-USDC", StringComparison.OrdinalIgnoreCase);

        /// <summary>The runtime's sizing formula (§17.3): sign(w) · round_qty(|w · equity / quote|), round_qty = down to whole shares / 8 decimals.</summary>
        private static double? ImpliedQty(double weight, double? equity, double? price, bool crypto)
        {
            if (equity is not double e || price is not double px || px <= 0) return null;
            var raw = Math.Abs(weight * e / px);
            var q = crypto ? Math.Floor(raw * 1e8 + 1e-6) / 1e8 : Math.Floor(raw + 1e-9);
            return weight < 0 ? -q : q;
        }

        private void RenderNetting()
        {
            var p = _nettingPanel;
            p.Children.Clear();
            var id = _accountId;
            if (id == null) return;
            p.Children.Add(AccountSelector("PtAcctNetting"));
            p.Children.Add(FundUi.Panel(Line(
                "Netting (§17.5): per instrument the strategies' deltas (target − own quantity) are netted first. Opposite deltas are crossed INTERNALLY at the "
                + "current quote (sub-ledger \"cross\" entries, no fee, no market order — no self-crossing); only the residual becomes ONE account order "
                + "(two legs when the account position flips sign), allocated pro rata to the strategies whose delta points that way; reductions first. "
                + "Targets below are the strategies' latest recorded decisions (weights); the quantities marked ≈ are implied with the runtime's formula "
                + "(weight × the strategy's equity ÷ the last price, rounded down) — the runtime re-computes them with its live quote and the pre-trade checks "
                + "before any order.", 10.5, FundPalette.Dim), new Thickness(12, 8, 12, 8)));
            if (_detailErrors.TryGetValue(id, out var derr)) p.Children.Add(ErrorBlock(derr));
            if (!_details.TryGetValue(id, out var detail)) { p.Children.Add(Line("Loading…", 11, FundPalette.Faint)); return; }
            var names = SourceNames(detail);
            var sources = detail.Sources ?? new List<TradingSource>();
            var positions = detail.Positions ?? new List<TradingPosition>();
            var working = detail.WorkingOrders ?? new List<OmsOrder>();

            // latest decision with targets per strategy source
            var decisions = new Dictionary<string, (IncubationDecision? Decision, IncubationDecision? Failure, IncubationRun? Run)>(StringComparer.Ordinal);
            foreach (var s in sources.Where(x => x.Kind == "strategy" && x.SourceId != null))
            {
                var run = RunOf(s);
                IncubationDecision? dec = null, fail = null;
                if (run?.Id != null && _runDetails.TryGetValue(run.Id, out var rd))
                {
                    var list = rd.Decisions ?? new List<IncubationDecision>();
                    dec = list.FirstOrDefault(d => !d.IsTransient && d.Targets != null);
                    fail = list.FirstOrDefault(d => d.IsTransient);
                }
                decisions[s.SourceId!] = (dec, fail, run);
            }

            // failing decisions (data not there yet, source down) — shown verbatim, never replaced by an older signal
            foreach (var kv in decisions.Where(k => k.Value.Failure != null))
                p.Children.Add(Callout(SourceName(names, kv.Key) + ": latest decision attempt failed at " + TradingText.Ts(kv.Value.Failure!.CreatedAt) + " — "
                                       + (kv.Value.Failure.Error ?? "no reason given") + " (retried every cycle)", FundPalette.Risk, RiskSurface));
            foreach (var kv in decisions)
                if (kv.Value.Run?.Id != null && _runDetailErrors.TryGetValue(kv.Value.Run.Id, out var rerr))
                    p.Children.Add(ErrorBlock(SourceName(names, kv.Key) + ": " + rerr));

            var symbols = new SortedSet<string>(StringComparer.Ordinal);
            foreach (var pos in positions) if (!string.IsNullOrEmpty(pos.Symbol)) symbols.Add(pos.Symbol!);
            foreach (var kv in decisions) foreach (var t in kv.Value.Decision?.Targets?.Keys ?? Enumerable.Empty<string>()) symbols.Add(t);
            foreach (var o in working) if (!string.IsNullOrEmpty(o.Symbol)) symbols.Add(o.Symbol!);
            if (symbols.Count == 0)
            {
                p.Children.Add(Line("No positions, targets or working orders in this account.", 11, FundPalette.Faint));
                return;
            }

            foreach (var sym in symbols)
            {
                var pos = positions.FirstOrDefault(x => x.Symbol == sym);
                var price = pos?.LastPrice ?? sources.SelectMany(x => x.Positions ?? new List<TradingSourcePosition>()).FirstOrDefault(x => x.Symbol == sym)?.LastPrice;
                var crypto = IsCrypto(sym, pos?.AssetClass);
                var accountQty = pos?.Qty ?? 0;
                var stack = new StackPanel();
                var head = new WrapPanel();
                head.Children.Add(FundUi.Text(sym + "  ", 12.5, FundPalette.TextBright, bold: true, mono: true, wrap: false));
                head.Children.Add(FundUi.Text("account position " + FundFormat.Qty(accountQty) + " · last " + FundFormat.Num(price, 2), 11, FundPalette.Dim, wrap: false));
                stack.Children.Add(head);

                var widths = new double[] { 200, 90, 150, 110, 0 };
                stack.Children.Add(Row(widths, new[] { "Contributor", "Current qty", "Target weight (decision)", "≈ Target qty", "Note" }, header: true));
                double netTarget = 0;
                var computable = true;
                foreach (var s in sources.Where(x => x.SourceId != null))
                {
                    var cur = pos?.BySource?.FirstOrDefault(b => b.SourceId == s.SourceId)?.Qty
                              ?? s.Positions?.FirstOrDefault(x => x.Symbol == sym)?.Qty ?? 0;
                    string weightText, targetText, note;
                    double? target;
                    if (s.Kind == "strategy" && decisions.TryGetValue(s.SourceId!, out var d) && d.Decision != null)
                    {
                        var wgt = d.Decision.Targets != null && d.Decision.Targets.TryGetValue(sym, out var wv) && wv is double wd ? wd : 0.0;
                        var force = d.Decision.Force?.Contains(sym) == true;
                        if (force) wgt = 0;
                        target = ImpliedQty(wgt, s.Equity, price, crypto);
                        if (wgt == 0) target = 0;
                        weightText = FundFormat.Fraction(wgt, 1) + " (" + (d.Decision.BarDate ?? "?") + ", " + (d.Decision.Status ?? "?") + ")";
                        targetText = target.HasValue ? "≈ " + FundFormat.Qty(target) : "—";
                        note = force ? "forced to 0 (stop / exit)" : target == null ? "no current quote in the view" : s.State == "incubation" ? "" : "released → 0";
                    }
                    else if (s.Kind == "strategy")
                    {
                        if (Math.Abs(cur) < 1e-12) continue;
                        weightText = "—";
                        target = null;
                        targetText = "—";
                        note = "no recorded decision yet";
                    }
                    else
                    {
                        if (Math.Abs(cur) < 1e-12) continue;
                        weightText = "no target source";
                        target = cur;   // held: no target source moves it
                        targetText = "held";
                        note = s.Kind == "council" ? "migrated positions, held until stage 10" : "manual orders booked to Unallocated";
                    }
                    if (target is double tv) netTarget += tv; else computable = false;
                    stack.Children.Add(Row(widths, new[] { SourceName(names, s.SourceId), FundFormat.Qty(cur), weightText, targetText, note },
                                           brushes: new Brush?[] { FundPalette.Text, null, FundPalette.Dim, FundPalette.TextBright, FundPalette.Faint }));
                }
                if (computable)
                {
                    var residual = netTarget - accountQty;
                    stack.Children.Add(Line("Net ≈ target " + FundFormat.Qty(netTarget) + " vs current " + FundFormat.Qty(accountQty) + " → residual ≈ "
                                            + (Math.Abs(residual) < 1e-12 ? "0 (nothing to send)" : FundFormat.Qty(residual) + " (one account order after internal crossing, if the runtime confirms it)"),
                                            11, Math.Abs(residual) < 1e-12 ? FundPalette.Long : FundPalette.Risk));
                }
                else stack.Children.Add(Line("Net target not computable in the view (a contributor has no decision or no current quote) — the runtime decides with its own live data.", 10.5, FundPalette.Faint));
                foreach (var o in working.Where(x => x.Symbol == sym))
                {
                    var alloc = string.Join(", ", (o.Allocations ?? new List<TradingSourceQty>()).Select(a => SourceName(names, a.SourceId) + " " + FundFormat.Qty(a.Qty)));
                    stack.Children.Add(Line("Working order: " + TradingText.OrderLine(o) + (alloc.Length > 0 ? " · allocated to " + alloc : "") + " · " + o.ClientOrderId, 10.5, FundPalette.Focus, mono: true));
                }
                var card = FundUi.Panel(stack, new Thickness(12, 8, 12, 8), new Thickness(0, 0, 0, 8));
                AutomationProperties.SetName(card, "Netting " + sym + ": account position " + FundFormat.Qty(accountQty));
                p.Children.Add(card);
            }
        }

        // =====================================================================================
        // Orders (OMS blotter)
        // =====================================================================================

        private void EnsureOrdersUi()
        {
            if (_orderList != null) return;
            var p = _ordersPanel;
            p.Children.Clear();

            var top = new WrapPanel { Margin = new Thickness(0, 0, 0, 4) };
            var newOrder = SmallButton("New paper order…", "New manual paper order (Ctrl+N)");
            newOrder.Margin = new Thickness(0, 0, 10, 0);
            newOrder.Click += async (_, _) => await NewOrderAsync(_orderAccount ?? _accountId, null, null, null);
            top.Children.Add(newOrder);
            var legend = FundUi.Text("States: new (stored before sending) → submitted → accepted → partially filled → filled · terminal: filled, cancelled, rejected, expired. "
                                     + "Every transition is an event; the client order id makes every order idempotent.", 10.5, FundPalette.Faint);
            legend.VerticalAlignment = VerticalAlignment.Center;
            legend.MaxWidth = 720;
            top.Children.Add(legend);
            p.Children.Add(top);

            _orderFilters = new StackPanel { Margin = new Thickness(0, 4, 0, 4) };
            AutomationProperties.SetName(_orderFilters, "Order filters");
            p.Children.Add(_orderFilters);

            _orderCount = FundUi.Text("", 10.5, FundPalette.Faint);
            _orderCount.Margin = new Thickness(0, 2, 0, 4);
            AutomationProperties.SetLiveSetting(_orderCount, AutomationLiveSetting.Polite);
            p.Children.Add(_orderCount);

            _orderList = TradingUi.List("Orders, newest first — Enter or arrow keys show the details below", 300);
            _orderList.SelectionChanged += async (_, _) =>
            {
                if (_suppressOrderSelection) return;
                var cid = (_orderList.SelectedItem as ListBoxItem)?.Tag as string;
                if (cid == null || cid == _selectedOrderId) return;
                _selectedOrderId = cid;
                await LoadOrderDetailAsync(force: true);
            };
            p.Children.Add(_orderList);
            // CHART_FULLSCREEN place 7: the blotter in focus mode (Shared/FullScreenHost; F11 in the list too, Esc exits)
            var list = _orderList;
            var focus = FundUi.FullScreenButton(list, "Orders", () => _orderCount?.Text);
            focus.Margin = new Thickness(0, 0, 10, 0);
            top.Children.Insert(1, focus);

            _orderDetailHost = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
            AutomationProperties.SetName(_orderDetailHost, "Order details");
            p.Children.Add(_orderDetailHost);
            RenderOrderFilters();
        }

        private void RenderOrderFilters()
        {
            var host = _orderFilters;
            if (host == null) return;
            host.Children.Clear();

            WrapPanel FilterRow(string label)
            {
                var row = new WrapPanel { Margin = new Thickness(0, 0, 0, 2) };
                var l = FundUi.Text(label, 10.5, FundPalette.Faint, wrap: false);
                l.Width = 60;
                l.VerticalAlignment = VerticalAlignment.Center;
                row.Children.Add(l);
                AutomationProperties.SetName(row, label + " filter");
                return row;
            }

            var accRow = FilterRow("Account");
            foreach (var (id, text) in new (string?, string)[] { (null, "All") }.Concat(Accounts().Select(a => ((string?)a.Id, a.Id!))))
            {
                var rb = TradingUi.Radio("PtFilterAccount", text, "Show orders of " + (id ?? "all accounts"), _orderAccount == id);
                rb.Checked += async (_, _) => { if (_orderAccount == id) return; _orderAccount = id; await LoadOrdersAsync(); };
                accRow.Children.Add(rb);
            }
            host.Children.Add(accRow);

            var stateRow = FilterRow("State");
            foreach (var st in new string?[] { null }.Concat(TradingText.OrderStates))
            {
                var rb = TradingUi.Radio("PtFilterState", st == null ? "All" : TradingText.OrderStateLabel(st).Split(' ')[0] + (st == "partially_filled" ? " filled" : ""),
                                         "Show orders in state " + (st == null ? "any" : TradingText.OrderStateLabel(st)), _orderState == st);
                rb.Margin = new Thickness(0, 0, 12, 4);
                rb.Checked += async (_, _) => { if (_orderState == st) return; _orderState = st; await LoadOrdersAsync(); };
                stateRow.Children.Add(rb);
            }
            host.Children.Add(stateRow);

            var originRow = FilterRow("Origin");
            foreach (var og in new string?[] { null }.Concat(TradingText.OrderOrigins))
            {
                var rb = TradingUi.Radio("PtFilterOrigin", og == null ? "All" : TradingText.OriginLabel(og), "Show orders with origin " + (og == null ? "any" : TradingText.OriginLabel(og)), _orderOrigin == og);
                rb.Margin = new Thickness(0, 0, 12, 4);
                rb.Checked += (_, _) => { if (_orderOrigin == og) return; _orderOrigin = og; _signatures.Remove("orders-list"); RenderOrderListIfChanged(); };
                originRow.Children.Add(rb);
            }
            host.Children.Add(originRow);

            var symRow = FilterRow("Symbol");
            var box = FundUi.Input(_orderSymbol);
            box.Width = 140;
            box.MaxLength = 20;
            box.CharacterCasing = CharacterCasing.Upper;
            AutomationProperties.SetName(box, "Filter orders by symbol (contains)");
            box.TextChanged += (_, _) => { _orderSymbol = box.Text.Trim(); _signatures.Remove("orders-list"); RenderOrderListIfChanged(); };
            symRow.Children.Add(box);
            host.Children.Add(symRow);
        }

        private async Task LoadOrdersAsync()
        {
            EnsureOrdersUi();
            if (_orderAccount != null && !Accounts().Any(a => a.Id == _orderAccount)) _orderAccount = null;
            var res = await PlatformClient.ListOmsOrdersAsync(_orderAccount, _orderState, OrderLimit);
            if (_stopped) return;
            if (res.Ok && res.Value != null) { _orders = res.Value.Orders ?? new List<OmsOrder>(); _ordersError = null; }
            else _ordersError = res.Ok ? "Unexpected response from the backend (/v2/oms/orders)." : PlatformClient.DescribeFailure(res);
            // the filter radios already show the user's choice — re-built only when the account list or the account filter changes (keeps keyboard focus)
            RenderIfChanged("orders-filters", new object?[] { Accounts().Select(a => a.Id).ToList(), _orderAccount }, RenderOrderFilters);
            RenderOrderListIfChanged();
            if (_selectedOrderId == null) _selectedOrderId = FilteredOrders().FirstOrDefault()?.ClientOrderId;
            await LoadOrderDetailAsync(force: false);
        }

        private List<OmsOrder> FilteredOrders()
            => _orders.Where(o => (_orderOrigin == null || o.Origin == _orderOrigin)
                                  && (_orderSymbol.Length == 0 || (o.Symbol ?? "").Contains(_orderSymbol, StringComparison.OrdinalIgnoreCase))).ToList();

        private void RenderOrderListIfChanged()
            => RenderIfChanged("orders-list", new object?[] { _orders, _ordersError, _orderOrigin, _orderSymbol, _selectedOrderId }, RenderOrderList);

        private void RenderOrderList()
        {
            var list = _orderList;
            if (list == null) return;
            var hadFocus = list.IsKeyboardFocusWithin;
            var shown = FilteredOrders();
            _suppressOrderSelection = true;
            try
            {
                list.Items.Clear();
                ListBoxItem? selected = null;
                foreach (var o in shown)
                {
                    var widths = new double[] { 108, 92, 70, 150, 120, 120, 0 };
                    var filled = (o.FilledQty is double f && f > 0 ? FundFormat.Qty(f) + (o.AvgFillPrice.HasValue ? " @ " + FundFormat.Num(o.AvgFillPrice, 2) : "") : "—");
                    var what = (o.Side ?? "?").ToUpperInvariant() + " " + (o.Qty.HasValue ? FundFormat.Qty(o.Qty) : o.NotionalReq.HasValue ? FundFormat.Money(o.NotionalReq) : "?") + " " + (o.Symbol ?? "?");
                    var row = Row(widths, new[]
                    {
                        TradingText.TsShort(o.CreatedAt), (o.AccountId ?? "").Replace("paper-", ""), TradingText.OriginLabel(o.Origin).Split(' ')[0], what, filled,
                        TradingText.OrderStateLabel(o.State), string.IsNullOrWhiteSpace(o.RejectReason) ? (o.ClientOrderId ?? "") : o.RejectReason!,
                    }, brushes: new Brush?[] { FundPalette.Faint, FundPalette.Dim, FundPalette.Dim, FundPalette.TextBright, null, StateBrush(o.State),
                                               string.IsNullOrWhiteSpace(o.RejectReason) ? FundPalette.Faint : o.State == "rejected" ? KillText : FundPalette.Risk });
                    var item = new ListBoxItem { Content = row, Tag = o.ClientOrderId, Padding = new Thickness(4, 1, 4, 1) };
                    AutomationProperties.SetName(item, what + ", " + TradingText.OrderStateLabel(o.State) + ", " + TradingText.OriginLabel(o.Origin) + ", " + TradingText.Ts(o.CreatedAt)
                                                       + (string.IsNullOrWhiteSpace(o.RejectReason) ? "" : ", reject reason: " + o.RejectReason) + ", client order id " + o.ClientOrderId);
                    list.Items.Add(item);
                    if (o.ClientOrderId == _selectedOrderId) selected = item;
                }
                if (selected != null) list.SelectedItem = selected;
                if (hadFocus && selected != null) list.Dispatcher.BeginInvoke(new Action(() => selected.Focus()));
            }
            finally { _suppressOrderSelection = false; }
            if (_orderCount != null)
            {
                _orderCount.Text = _ordersError != null ? "Orders not loaded: " + _ordersError
                    : shown.Count.ToString(Inv) + " shown of " + _orders.Count.ToString(Inv) + " loaded (newest " + OrderLimit.ToString(Inv) + " for the account / state filter)";
                _orderCount.Foreground = _ordersError != null ? FundPalette.Kill : FundPalette.Faint;
            }
        }

        private static Brush StateBrush(string? state) => state switch
        {
            "filled" => FundPalette.Long,
            "rejected" => FundPalette.Kill,
            "cancelled" or "expired" => FundPalette.Faint,
            "partially_filled" or "accepted" or "submitted" => FundPalette.Focus,
            "new" => FundPalette.Risk,
            _ => FundPalette.Dim,
        };

        private async Task LoadOrderDetailAsync(bool force)
        {
            var cid = _selectedOrderId;
            if (cid == null)
            {
                _orderDetail = null;
                RenderIfChanged("orders-detail", new object?[] { null }, RenderOrderDetail);
                return;
            }
            var res = await PlatformClient.GetOmsOrderAsync(cid);
            if (_stopped || cid != _selectedOrderId) return;
            if (res.Ok && res.Value?.Order != null) { _orderDetail = res.Value; _orderDetailError = null; }
            else { _orderDetail = null; _orderDetailError = res.Ok ? "Unexpected response from the backend (/v2/oms/orders/" + cid + ")." : PlatformClient.DescribeFailure(res); }
            var acc = _orderDetail?.Order?.AccountId;
            if (acc != null && Accounts().Any(a => a.Id == acc)) await LoadFillsAsync(acc);
            if (cid != _selectedOrderId) return;
            if (force) _signatures.Remove("orders-detail");
            var fills = acc != null && _fills.TryGetValue(acc, out var fl) ? fl.Where(x => x.ClientOrderId == cid).ToList() : new List<OmsFill>();
            RenderIfChanged("orders-detail", new object?[] { cid, _orderDetail, _orderDetailError, fills, acc != null ? _fillErrors.GetValueOrDefault(acc) : null }, RenderOrderDetail);
        }

        /// <summary>Fills are a cursor stream, oldest first: kept per account and extended incrementally.</summary>
        private async Task LoadFillsAsync(string accountId)
        {
            var after = _fillCursor.TryGetValue(accountId, out var c) ? c : 0;
            if (!_fills.TryGetValue(accountId, out var list)) { list = new List<OmsFill>(); _fills[accountId] = list; }
            for (var page = 0; page < 20; page++)
            {
                var res = await PlatformClient.GetFillsAsync(accountId, after, 500);
                if (_stopped) return;
                if (!res.Ok || res.Value == null)
                {
                    _fillErrors[accountId] = res.Ok ? "Unexpected response from the backend (/v2/oms/fills)." : PlatformClient.DescribeFailure(res);
                    return;
                }
                _fillErrors.Remove(accountId);
                var batch = res.Value.Fills ?? new List<OmsFill>();
                list.AddRange(batch);
                if (res.Value.NextAfter is long next) after = next;
                if (batch.Count < 500) break;
            }
            if (list.Count > MaxFillsKept) list.RemoveRange(0, list.Count - MaxFillsKept);
            _fillCursor[accountId] = after;
        }

        private void RenderOrderDetail()
        {
            var host = _orderDetailHost;
            if (host == null) return;
            host.Children.Clear();
            if (_orderDetailError != null) { host.Children.Add(ErrorBlock(_orderDetailError)); return; }
            var o = _orderDetail?.Order;
            if (o == null)
            {
                host.Children.Add(Line(_orders.Count == 0 ? "No orders yet." : "Select an order to see its details.", 11, FundPalette.Faint));
                return;
            }
            var stack = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text(TradingText.OrderLine(o).Split(" · ")[0] + "  ", 13, FundPalette.TextBright, bold: true, wrap: false));
            head.Children.Add(Chip(TradingText.OrderStateLabel(o.State).ToUpperInvariant(), StateBrush(o.State)));
            head.Children.Add(new Border { Width = 4 });
            head.Children.Add(Chip(o.IsTerminal ? "TERMINAL" : "WORKING", o.IsTerminal ? FundPalette.Faint : FundPalette.Focus));
            head.Children.Add(new Border { Width = 4 });
            head.Children.Add(Chip(TradingText.OriginLabel(o.Origin).ToUpperInvariant(), FundPalette.Neutral));
            stack.Children.Add(head);

            var idRow = new Grid { Margin = new Thickness(0, 6, 0, 2) };
            idRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(130) });
            idRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            idRow.Children.Add(FundUi.Text("Client order id", 11.5, FundPalette.Dim, wrap: false));
            var idBox = Selectable(o.ClientOrderId ?? "");
            AutomationProperties.SetName(idBox, "Client order id " + (o.ClientOrderId ?? ""));
            Grid.SetColumn(idBox, 1);
            idRow.Children.Add(idBox);
            stack.Children.Add(idRow);

            if (!string.IsNullOrWhiteSpace(o.RejectReason))
                stack.Children.Add(o.State == "rejected"
                    ? Callout("Rejected: " + o.RejectReason, FundPalette.Kill, FundPalette.KillSurface)
                    : Callout((o.State == "cancelled" ? "Cancel reason: " : o.State == "expired" ? "Expired: " : "Note: ") + o.RejectReason, FundPalette.Risk, RiskSurface));

            var cols = new Grid();
            cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });
            cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var c1 = new StackPanel();
            c1.Children.Add(FundUi.KeyValue("Account", TradingText.AccountLabel(o.AccountId)));
            c1.Children.Add(FundUi.KeyValue("Symbol · class", (o.Symbol ?? "?") + " · " + (o.AssetClass ?? "—")));
            c1.Children.Add(FundUi.KeyValue("Side", o.Side ?? "—"));
            c1.Children.Add(FundUi.KeyValue("Quantity", o.Qty.HasValue ? FundFormat.Qty(o.Qty) : "—"));
            if (o.NotionalReq.HasValue) c1.Children.Add(FundUi.KeyValue("Requested notional", FundFormat.Money(o.NotionalReq)));
            c1.Children.Add(FundUi.KeyValue("Filled", FundFormat.Qty(o.FilledQty) + (o.AvgFillPrice.HasValue ? " @ " + FundFormat.Num(o.AvgFillPrice, 4) : "")));
            c1.Children.Add(FundUi.KeyValue("Notional · fees", FundFormat.Money(o.Notional) + " · " + FundFormat.Money(o.Fees)));
            c1.Children.Add(FundUi.KeyValue("Reference price", FundFormat.Num(o.RefPrice, 4)));
            var c2 = new StackPanel();
            c2.Children.Add(FundUi.KeyValue("Created", TradingText.Ts(o.CreatedAt)));
            c2.Children.Add(FundUi.KeyValue("Updated", TradingText.Ts(o.UpdatedAt)));
            c2.Children.Add(FundUi.KeyValue("Time in force", o.Tif ?? "—"));
            c2.Children.Add(FundUi.KeyValue("Broker order id", o.BrokerOrderIdText.Length == 0 ? "—" : o.BrokerOrderIdText));
            c2.Children.Add(FundUi.KeyValue("Send attempts", o.Attempts?.ToString(Inv) ?? "—"));
            c2.Children.Add(FundUi.KeyValue("Execution", string.IsNullOrEmpty(o.ExecutionId) ? "—" : o.ExecutionId!));
            c2.Children.Add(FundUi.KeyValue("Reduce-only allowed", o.AllowReduce == true ? "yes" : "no"));
            cols.Children.Add(c1);
            Grid.SetColumn(c2, 2);
            cols.Children.Add(c2);
            stack.Children.Add(cols);
            if (!string.IsNullOrWhiteSpace(o.Reason)) stack.Children.Add(FundUi.KeyValue("Reason", o.Reason!));

            var names = SourceNames(_details.GetValueOrDefault(o.AccountId ?? ""));
            stack.Children.Add(SubHead("Allocation to sub-ledgers"));
            var allocs = o.Allocations ?? new List<TradingSourceQty>();
            if (allocs.Count == 0) stack.Children.Add(Line("None recorded (kill / margin orders go to the sources holding the symbol in the reducing direction).", 10.5, FundPalette.Faint));
            foreach (var a in allocs) stack.Children.Add(Line(SourceName(names, a.SourceId) + " · " + FundFormat.Qty(a.Qty), 10.5, FundPalette.Dim, mono: true));

            stack.Children.Add(SubHead("Pre-trade checks"));
            var checks = o.Checks ?? new List<OmsCheck>();
            if (checks.Count == 0) stack.Children.Add(Line("No check results stored with this order.", 10.5, FundPalette.Faint));
            foreach (var c in checks)
            {
                var text = (c.Ok == false ? "✗ " : c.Ok == true ? "✓ " : "? ") + FundFormat.Label(c.Check) + (string.IsNullOrWhiteSpace(c.Message) ? "" : " — " + c.Message);
                var t = Line(text, 11, c.Ok == false ? KillText : c.Ok == true ? FundPalette.Long : FundPalette.Dim);
                AutomationProperties.SetName(t, (c.Ok == false ? "Failed: " : c.Ok == true ? "Passed: " : "Unknown: ") + FundFormat.Label(c.Check) + (string.IsNullOrWhiteSpace(c.Message) ? "" : ", " + c.Message));
                stack.Children.Add(t);
            }

            stack.Children.Add(SubHead("State timeline"));
            var events = _orderDetail?.Events ?? new List<OmsOrderEvent>();
            if (events.Count == 0) stack.Children.Add(Line("No events.", 10.5, FundPalette.Faint));
            foreach (var e in events)
                stack.Children.Add(Line(TradingText.Ts(e.Ts) + "  " + (e.FromState ?? "∅") + " → " + (e.ToState ?? "?") + (string.IsNullOrWhiteSpace(e.Note) ? "" : "  · " + e.Note)
                                        + (e.Data is JsonElement dj && dj.ValueKind == JsonValueKind.Object ? "  · " + FundFormat.Plain(dj) : ""), 10.5, StateBrush(e.ToState), mono: true));

            stack.Children.Add(SubHead("Fills"));
            var acc = o.AccountId ?? "";
            if (_fillErrors.TryGetValue(acc, out var ferr)) stack.Children.Add(ErrorBlock("Fills: " + ferr));
            var fills = _fills.TryGetValue(acc, out var fl) ? fl.Where(x => x.ClientOrderId == o.ClientOrderId).ToList() : new List<OmsFill>();
            if (fills.Count == 0) stack.Children.Add(Line("No fills.", 10.5, FundPalette.Faint));
            foreach (var f in fills)
                stack.Children.Add(Line(TradingText.Ts(f.Ts) + "  " + (f.Side ?? "?").ToUpperInvariant() + " " + FundFormat.Qty(f.Qty) + " " + (f.Symbol ?? "?") + " @ " + FundFormat.Num(f.Price, 4)
                                        + " · notional " + FundFormat.Money(f.Notional) + " · fees " + FundFormat.Money(f.Fees) + " · fill #" + f.Id.ToString(Inv), 10.5, FundPalette.Dim, mono: true));

            if (!o.IsTerminal)
            {
                var cancel = SmallButton("Cancel order…", "Cancel the working order " + o.ClientOrderId);
                cancel.Click += async (_, _) => await CancelOrderAsync(o);
                stack.Children.Add(cancel);
            }
            var panel = FundUi.Panel(stack);
            AutomationProperties.SetName(panel, "Order details " + o.ClientOrderId);
            host.Children.Add(panel);
        }

        private async Task CancelOrderAsync(OmsOrder o)
        {
            if (!FundDialogs.Confirm(Owner, "Cancel order?", TradingText.OrderLine(o) + "\nClient order id: " + o.ClientOrderId
                                                             + "\n\nCancels the working simulated order (a partly filled order keeps its fills).", "Cancel order"))
                return;
            var res = await PlatformClient.CancelOmsOrderAsync(o.ClientOrderId ?? "");
            if (!res.Ok) FundDialogs.Info(Owner, "Cancel order", PlatformClient.DescribeFailure(res));
            _signatures.Remove("orders-detail");
            await LoadOrdersAsync();
        }

        // =====================================================================================
        // Incubation
        // =====================================================================================

        private async Task LoadIncubationAsync()
        {
            await LoadRunsAsync();
            if (_selectedRunId == null || !_runs.Any(r => r.Id == _selectedRunId))
                _selectedRunId = (_runs.FirstOrDefault(r => r.IsRunning) ?? _runs.FirstOrDefault())?.Id;
            if (_selectedRunId != null) await LoadRunDetailAsync(_selectedRunId);
            RenderIfChanged(SecIncubation, new object?[]
            {
                _runs, _runsError, _selectedRunId, _selectedRunId != null ? _runDetails.GetValueOrDefault(_selectedRunId) : null,
                _selectedRunId != null ? _runDetailErrors.GetValueOrDefault(_selectedRunId) : null, Accounts().Select(a => a.Id).ToList(),
            }, RenderIncubation);
        }

        private void RenderIncubation()
        {
            var p = _incubationPanel;
            p.Children.Clear();
            var intro = new StackPanel();
            intro.Children.Add(FundUi.Text("Incubation · validated strategies on paper", 12, FundPalette.TextBright, bold: true));
            intro.Children.Add(Line("Only you start or abort a run. Its length (sessions) is chosen before the start and is IMMUTABLE afterwards; the result compares the "
                                    + "realised sub-ledger with the band the holdout evidence expects (passed / failed / inconclusive). After completion the strategy keeps "
                                    + "trading on paper until you abort or retire it. Live trading is off — the live guard comes with stage 8 and a live step only on your "
                                    + "explicit go.", 10.5, FundPalette.Dim));
            var start = SmallButton("Start incubation…", "Start an incubation run (Ctrl+I)");
            start.Click += async (sender, _) => await DialogGuard.RunAsync("incubation-start", () => StartIncubationAsync(null), sender);   // dlgguard
            intro.Children.Add(start);
            p.Children.Add(FundUi.Panel(intro));
            if (_runsError != null) p.Children.Add(ErrorBlock(_runsError));

            if (_runs.Count == 0)
            {
                p.Children.Add(Line(_runsError == null ? "No incubation runs yet." : "", 11, FundPalette.Faint));
                return;
            }
            var list = TradingUi.List("Incubation runs, newest first — arrow keys show the details below", Math.Min(200, 30 + 26 * _runs.Count));
            _suppressRunSelection = true;
            try
            {
                foreach (var r in _runs)
                {
                    var status = r.IsRunning ? "RUNNING · " + DayText(r) : (r.Status ?? "?").ToUpperInvariant() + (r.Result != null ? " · " + r.Result.ToUpperInvariant() : "");
                    var widths = new double[] { 210, 0, 190 };
                    var row = Row(widths, new[]
                    {
                        status, (r.StrategyName ?? r.StrategyId ?? "?") + " · v" + (r.Version?.ToString(Inv) ?? "?"),
                        (r.StartDate ?? "?") + " → " + (r.EndDate ?? "?") + " · " + (r.LengthSessions?.ToString(Inv) ?? "?") + " fixed",
                    }, brushes: new Brush?[] { r.IsRunning ? FundPalette.Focus : ResultBrush(r.Result), FundPalette.TextBright, FundPalette.Dim });
                    var item = new ListBoxItem { Content = row, Tag = r.Id, Padding = new Thickness(4, 2, 4, 2) };
                    AutomationProperties.SetName(item, (r.StrategyName ?? r.StrategyId) + ", " + status + ", length " + (r.LengthSessions?.ToString(Inv) ?? "?") + " sessions, fixed");
                    list.Items.Add(item);
                    if (r.Id == _selectedRunId) list.SelectedItem = item;
                }
            }
            finally { _suppressRunSelection = false; }
            list.SelectionChanged += async (_, _) =>
            {
                if (_suppressRunSelection) return;
                var id = (list.SelectedItem as ListBoxItem)?.Tag as string;
                if (id == null || id == _selectedRunId) return;
                _selectedRunId = id;
                await LoadRunDetailAsync(id);
                _signatures.Remove(SecIncubation);
                RenderIfChanged(SecIncubation, new object?[] { _runs, id }, () => { RenderIncubation(); FocusRunList(); });
            };
            p.Children.Add(list);
            _runList = list;

            var run = _runs.FirstOrDefault(r => r.Id == _selectedRunId);
            if (run != null) p.Children.Add(RunDetail(run));
        }

        private ListBox? _runList;

        private void FocusRunList()
        {
            var list = _runList;
            if (list?.SelectedItem is ListBoxItem item) list.Dispatcher.BeginInvoke(new Action(() => item.Focus()));
        }

        private FrameworkElement RunDetail(IncubationRun r)
        {
            var stack = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text((r.StrategyName ?? r.StrategyId ?? "?") + " · v" + (r.Version?.ToString(Inv) ?? "?") + "  ", 13, FundPalette.TextBright, bold: true, wrap: false));
            head.Children.Add(Chip(r.IsRunning ? "INCUBATING · " + DayText(r) : (r.Status ?? "?").ToUpperInvariant(), r.IsRunning ? FundPalette.Focus : FundPalette.Faint));
            head.Children.Add(new Border { Width = 4 });
            head.Children.Add(Chip("LENGTH " + (r.LengthSessions?.ToString(Inv) ?? "?") + " · FIXED", FundPalette.Neutral));
            if (r.Result != null)
            {
                head.Children.Add(new Border { Width = 4 });
                head.Children.Add(Chip("RESULT · " + r.Result.ToUpperInvariant(), ResultBrush(r.Result)));
            }
            if (r.LiveEligible == true)
            {
                head.Children.Add(new Border { Width = 4 });
                head.Children.Add(Chip("LIVE-ELIGIBLE (LIVE IS OFF)", FundPalette.Long));
            }
            stack.Children.Add(head);
            if (FundUi.FixtureLine(r.Fixture, r.FixtureNote, 12, new Thickness(0, 4, 0, 2)) is TextBlock fixture) stack.Children.Add(fixture);
            stack.Children.Add(Line(DayHelp(r), 10.5, FundPalette.Dim));
            stack.Children.Add(Line("Run " + (r.Id ?? "?") + " · account " + (r.AccountId ?? "?") + " · started " + TradingText.Ts(r.CreatedAt) + " by " + (r.CreatedBy ?? "?")
                                    + (string.IsNullOrWhiteSpace(r.Reason) ? "" : " · reason: " + r.Reason), 10.5, FundPalette.Faint));
            if (r.EndedAt != null)
                stack.Children.Add(Line("Ended " + TradingText.Ts(r.EndedAt) + (string.IsNullOrWhiteSpace(r.EndReason) ? "" : " — " + r.EndReason), 10.5, FundPalette.Risk));

            var cols = new Grid { Margin = new Thickness(0, 6, 0, 0) };
            for (var i = 0; i < 3; i++)
            {
                cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                if (i < 2) cols.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(18) });
            }
            var c1 = new StackPanel();
            c1.Children.Add(FundUi.Text("Realised", 10.5, FundPalette.Faint, bold: true));
            c1.Children.Add(FundUi.KeyValue("Return (log)", FundFormat.Fraction(r.Realised?.Return, 2, signed: true) + " (" + FundFormat.Num(r.Realised?.LogReturn, 4) + ")", FundPalette.ForSigned(r.Realised?.Return)));
            c1.Children.Add(FundUi.KeyValue("Vol · Sharpe", FundFormat.Fraction(r.Realised?.Vol, 1) + " · " + FundFormat.Num(r.Realised?.Sharpe, 2)));
            c1.Children.Add(FundUi.KeyValue("Max drawdown", FundFormat.Fraction(r.Realised?.MaxDrawdown, 1)));
            c1.Children.Add(FundUi.KeyValue("Equity", FundFormat.Money(r.Realised?.Equity)));
            var c2 = new StackPanel();
            c2.Children.Add(FundUi.Text("Expected (holdout evidence)", 10.5, FundPalette.Faint, bold: true));
            c2.Children.Add(FundUi.KeyValue("Mean · std daily", FundFormat.Fraction(r.Expected?.MeanDaily, 3) + " · " + FundFormat.Fraction(r.Expected?.StdDaily, 2)));
            c2.Children.Add(FundUi.KeyValue("Vol · max DD", FundFormat.Fraction(r.Expected?.Vol, 1) + " · " + FundFormat.Fraction(r.Expected?.MaxDrawdown, 1)));
            c2.Children.Add(FundUi.KeyValue("Sessions · run", (r.Expected?.Sessions?.ToString(Inv) ?? "—") + " · " + (r.Expected?.SourceRunId ?? "—")));
            var c3 = new StackPanel();
            c3.Children.Add(FundUi.Text("Band and tracking", 10.5, FundPalette.Faint, bold: true));
            c3.Children.Add(FundUi.KeyValue("Band (log return)", FundFormat.Num(r.Band?.Lo, 4) + " … " + FundFormat.Num(r.Band?.Hi, 4) + " (n " + (r.Band?.N?.ToString(Inv) ?? "—") + ")"));
            c3.Children.Add(FundUi.KeyValue("Tracking error", FundFormat.Fraction(r.TrackingError, 1)));
            c3.Children.Add(FundUi.KeyValue("Traded · missed", (r.Progress?.Traded?.ToString(Inv) ?? "—") + " · " + (r.Progress?.Missed?.ToString(Inv) ?? "—")));
            c3.Children.Add(FundUi.KeyValue("Last session", r.Progress?.LastSession ?? "—"));
            cols.Children.Add(c1);
            Grid.SetColumn(c2, 2);
            cols.Children.Add(c2);
            Grid.SetColumn(c3, 4);
            cols.Children.Add(c3);
            stack.Children.Add(cols);
            foreach (var reason in r.ResultReasons ?? new List<JsonElement>())
                stack.Children.Add(Line("• " + FundFormat.Plain(reason), 10.5, ResultBrush(r.Result)));

            if (r.IsRunning)
            {
                var abort = SmallButton("Abort run…", "Abort the incubation run of " + (r.StrategyName ?? r.StrategyId));
                InFlightButton(abort, "abort:" + (r.Id ?? ""), () => AbortRunAsync(r));
                stack.Children.Add(abort);
            }

            if (r.Id != null && _runDetailErrors.TryGetValue(r.Id, out var err)) stack.Children.Add(ErrorBlock(err));
            if (r.Id != null && _runDetails.TryGetValue(r.Id, out var detail))
            {
                stack.Children.Add(SubHead("Decisions (newest first)"));
                var decisions = detail.Decisions ?? new List<IncubationDecision>();
                if (decisions.Count == 0) stack.Children.Add(Line("No decision yet — the first one is for the session before the start date.", 10.5, FundPalette.Faint));
                foreach (var d in decisions.Take(40))
                {
                    var targets = d.Targets == null ? "" : " · targets " + string.Join(", ", d.Targets.Select(kv => kv.Key + " " + FundFormat.Fraction(kv.Value, 1)));
                    var force = d.Force != null && d.Force.Count > 0 ? " · force 0: " + string.Join(", ", d.Force) : "";
                    // fix1023 round 3: a decision pending at the abort → "cancelled — run aborted; never executed" (+ when), never shown as pending
                    var statusText = d.Status == "cancelled"
                        ? "cancelled — run aborted; never executed" + (string.IsNullOrWhiteSpace(d.CancelledAt) ? "" : " (" + TradingText.Ts(d.CancelledAt) + ")")
                        : d.Status ?? "?";
                    var text = (d.IsTransient ? "latest attempt " + TradingText.TsShort(d.CreatedAt) : (d.BarDate ?? "?")) + " · " + statusText
                               + (string.IsNullOrWhiteSpace(d.Reason) ? "" : " · " + d.Reason) + targets + force + (string.IsNullOrWhiteSpace(d.Error) ? "" : " · error: " + d.Error);
                    var brush = d.Status switch { "executed" => FundPalette.Long, "failed" => KillText, "missed" or "not_executed" => FundPalette.Risk, "pending" => FundPalette.Focus, "cancelled" => FundPalette.Faint, _ => FundPalette.Dim };
                    stack.Children.Add(Line(text, 10.5, brush, mono: true));
                }
                // fix1023 review B3: the run's events merged chronologically with its decay decisions (paused / keep running /
                // demoted) — so the decision history stays reachable after a demote (the strategy then leaves Monitoring)
                var merged = new List<(string Ts, string Text, Brush Brush)>();
                foreach (var e in detail.Events ?? new List<IncubationEvent>())
                    merged.Add((e.Ts ?? "", TradingText.Ts(e.Ts) + "  " + (e.Event ?? "?") + EventDataText(e.Data), FundPalette.Dim));
                foreach (var de in detail.DecayEvents ?? new List<IncubationDecayEvent>())
                {
                    var label = de.Action switch { "paused" => "decay: paused", "keep_running" => "decay: keep running", "demoted" => "decay: demoted", _ => "decay: " + (de.Action ?? "?") };
                    var text = TradingText.Ts(de.Ts) + "  " + label + (de.Synthetic == true ? " (SYNTHETIC sandbox pause)" : "")
                               + (string.IsNullOrWhiteSpace(de.Rule) ? "" : " · rule " + MonUi.RuleName(de.Rule))
                               + (string.IsNullOrWhiteSpace(de.CheckDay) ? "" : " · check day " + de.CheckDay)
                               + " · by " + (string.IsNullOrWhiteSpace(de.Actor) ? "—" : de.Actor)
                               + (string.IsNullOrWhiteSpace(de.Reason) ? "" : " · " + de.Reason);
                    merged.Add((de.Ts ?? "", text, de.Action == "paused" || de.Action == "demoted" ? FundPalette.Risk : FundPalette.Text));
                }
                var evExp = new Expander
                {
                    Header = FundUi.Text("Events (" + merged.Count.ToString(Inv) + ")", 11, FundPalette.Text, wrap: false),
                    Foreground = FundPalette.Text,
                    Margin = new Thickness(0, 6, 0, 0),
                };
                AutomationProperties.SetName(evExp, "Incubation events incl. decay decisions");
                var evs = new StackPanel { Margin = new Thickness(16, 4, 0, 0) };
                foreach (var m in merged.Select((x, i) => (x, i)).OrderBy(t => FundFormat.ParseTs(t.x.Ts) ?? DateTimeOffset.MinValue).ThenBy(t => t.i).Select(t => t.x))
                    evs.Children.Add(Line(m.Text, 10.5, m.Brush, mono: true));
                evExp.Content = evs;
                stack.Children.Add(evExp);
            }
            var panel = FundUi.Panel(stack, margin: new Thickness(0, 8, 0, 8));
            AutomationProperties.SetName(panel, "Incubation run " + (r.StrategyName ?? r.StrategyId) + " details");
            return panel;
        }

        private async Task StartIncubationAsync(string? strategyId)
        {
            var strategies = await PlatformClient.GetStrategiesAsync();
            if (!strategies.Ok || strategies.Value?.Strategies == null)
            {
                FundDialogs.Info(Owner, "Start incubation", strategies.Ok ? "Unexpected response from the backend (/v2/strategies)." : PlatformClient.DescribeFailure(strategies));
                return;
            }
            if (_incDefaults == null) await LoadRunsAsync();
            var validated = strategies.Value.Strategies.Where(s => s.State == "validated" && s.Id.Length > 0)
                                      .OrderBy(s => s.Name, StringComparer.OrdinalIgnoreCase).ToList();
            var dialog = new IncubationStartDialog(Owner, validated, _incDefaults, strategyId);
            if (dialog.ShowDialog() != true) return;
            if (dialog.AlreadyRunning)
            {
                FundDialogs.Info(Owner, "Start incubation", "An incubation run of this strategy is already running.");
                SelectTab(SecIncubation);
                _signatures.Remove(SecIncubation);
                await RefreshAsync(force: true);
                return;
            }
            if (dialog.Run == null) return;
            var r = dialog.Run;
            FundDialogs.Info(Owner, "Incubation started",
                (r.StrategyName ?? r.StrategyId) + " · v" + (r.Version?.ToString(Inv) ?? "?") + "\nLength: " + (r.LengthSessions?.ToString(Inv) ?? "?")
                + " sessions (fixed) · " + (r.StartDate ?? "?") + " → " + (r.EndDate ?? "?")
                + "\n" + AllocationText(dialog.Allocation)
                + "\n\nThe runtime decides for it from now on; orders are only sent while auto trading is on.");
            _selectedRunId = r.Id;
            SelectTab(SecIncubation);
            _signatures.Remove(SecIncubation);
            await RefreshAsync(force: true);
        }

        /// <summary>fix1023 round 3: the start response's capital allocation (done / failed + error / pending + note), verbatim.</summary>
        private static string AllocationText(IncubationAllocation? a) => a?.Status switch
        {
            "done" => "Capital allocation: done.",
            "failed" => "Capital allocation FAILED: " + (string.IsNullOrWhiteSpace(a.Error) ? "no reason given" : a.Error)
                        + " — the run is started; the runtime retries the allocation at its next cycle (an alert was raised).",
            // click-test 2026-10-24: ONE sentence — the backend's own note when it sends one, else this text
            "pending" => string.IsNullOrWhiteSpace(a.Note) ? "Capital allocation is running in the background; a failure raises an alert."
                                                           : "Capital allocation: " + a.Note!.Trim(),
            null => "Capital allocation: not reported (the start was confirmed after a time-out) — see the strategy's capital in Strategies.",
            _ => "Capital allocation: " + a.Status,
        };

        /// <summary>fix1023 round 3: event data as "key=value" for the keys that are present and non-empty only (no "reason=").</summary>
        private static string EventDataText(JsonElement? data)
        {
            if (data is not JsonElement d || d.ValueKind != JsonValueKind.Object) return "";
            var parts = d.EnumerateObject()
                         .Where(p => p.Value.ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined)
                                     && !(p.Value.ValueKind == JsonValueKind.String && string.IsNullOrWhiteSpace(p.Value.GetString()))
                                     && !(p.Value.ValueKind == JsonValueKind.Array && p.Value.GetArrayLength() == 0)
                                     && !(p.Value.ValueKind == JsonValueKind.Object && !p.Value.EnumerateObject().Any()))
                         .Select(p => p.Name + "=" + FundFormat.Plain(p.Value))
                         .ToList();
            return parts.Count == 0 ? "" : "  · " + string.Join(", ", parts);
        }

        private async Task AbortRunAsync(IncubationRun r)
        {
            var reason = TradingReasonDialog.Ask(Owner, "Abort incubation",
                "Ends the incubation run of " + (r.StrategyName ?? r.StrategyId) + " now (" + DayText(r).ToLowerInvariant() + "). The strategy returns to validated; its positions "
                + "are closed to 0 at the next tradable moment and its cash returns to Unallocated. A new run chooses its length again and starts from zero.",
                "Abort run " + (r.Id ?? "?") + " and close its positions", danger: true);
            if (reason == null || r.Id == null) return;
            var res = await PlatformClient.AbortIncubationAsync(r.Id, reason);
            if (!res.Ok) FundDialogs.Info(Owner, "Abort incubation", PlatformClient.DescribeFailure(res));
            _signatures.Remove(SecIncubation);
            await RefreshAsync(force: true);
        }

        // =====================================================================================
        // Migration of the old paper account
        // =====================================================================================

        private async Task LoadMigrationAsync()
        {
            var res = await PlatformClient.GetMigrationAsync();
            if (_stopped) return;
            if (res.Ok && res.Value != null) { _migration = res.Value; _migrationError = null; }
            else _migrationError = res.Ok ? "Unexpected response from the backend (/v2/portfolio/migration)." : PlatformClient.DescribeFailure(res);
            RenderIfChanged(SecMigration, new object?[] { _migration, _migrationError, _dryRun, _dryRunAt, _dryRunError }, RenderMigration);
        }

        private bool DryRunFresh => _dryRun?.Ok == true && _dryRunAt is DateTime at && DateTime.UtcNow - at < TimeSpan.FromMinutes(30);

        private void RenderMigration()
        {
            var p = _migrationPanel;
            p.Children.Clear();
            if (_migrationError != null) p.Children.Add(ErrorBlock(_migrationError));
            var m = _migration;
            var box = new StackPanel();
            box.Children.Add(FundUi.Text("Migration of the old paper account", 12, FundPalette.TextBright, bold: true));
            box.Children.Add(Line("A hard switch of the owner, started only by you: dry run first, then one confirmation step. The old account is frozen, copied 1:1 into "
                                  + "paper-council and marked migrated; the old engine then only analyses. Before it, the old engine keeps its account exactly as today "
                                  + "and the platform never writes to it.", 10.5, FundPalette.Dim));
            var legacy = m?.LegacyAccount ?? _state?.LegacyAccount;
            box.Children.Add(FundUi.KeyValue("Old account status", (legacy?.Status ?? "—") + " · owner " + (legacy?.Owner ?? "—"),
                                             legacy?.Status == "migrated" ? FundPalette.Focus : legacy?.Status == "migrating" ? FundPalette.Risk : FundPalette.Long));
            if (legacy?.MigratedAt != null) box.Children.Add(FundUi.KeyValue("Migrated at", TradingText.Ts(legacy.MigratedAt)));
            if (m?.Migration is TradingMigrationRecord rec)
            {
                box.Children.Add(FundUi.KeyValue("Migration", "#" + rec.IdText + " · " + TradingText.Ts(rec.CreatedAt) + " → " + (rec.AccountId ?? "—")));
                if (rec.Summary is JsonElement sum && sum.ValueKind == JsonValueKind.Object)
                    foreach (var prop in sum.EnumerateObject())
                        box.Children.Add(FundUi.KeyValue(FundFormat.Label(prop.Name), FundFormat.Plain(prop.Value), size: 11));
            }
            box.Children.Add(FundUi.KeyValue("Can migrate now", m?.CanMigrate == true ? "yes (after a dry run without blockers)" : "no"));

            var row = new WrapPanel { Margin = new Thickness(0, 6, 0, 0) };
            var dry = SmallButton("Dry run", "Run the migration dry run (writes nothing except an audit row)");
            dry.ToolTip = "Report what would be copied and the blockers — writes nothing except an audit row";
            dry.Click += async (_, _) => await DryRunAsync(dry);
            row.Children.Add(dry);
            var migrate = TradingUi.Danger("Migrate…");
            migrate.Padding = new Thickness(10, 2, 10, 2);
            migrate.MinHeight = 22;
            migrate.FontSize = 11;
            migrate.Margin = new Thickness(0, 4, 8, 0);
            migrate.IsEnabled = m?.CanMigrate == true && DryRunFresh;
            migrate.ToolTip = m?.CanMigrate != true ? "Not possible: the account was already migrated or paper-council exists."
                : !DryRunFresh ? "Run a dry run without blockers first (valid for 30 minutes)." : "One confirmation step (note optional)";
            AutomationProperties.SetName(migrate, "Migrate the old paper account");
            AutomationProperties.SetHelpText(migrate, migrate.ToolTip as string ?? "");
            migrate.Click += async (_, _) => await MigrateAsync();
            row.Children.Add(migrate);
            box.Children.Add(row);
            p.Children.Add(FundUi.Panel(box));

            if (_dryRunError != null) p.Children.Add(ErrorBlock("Dry run failed: " + _dryRunError));
            if (_dryRun != null) p.Children.Add(DryRunReport(_dryRun));
        }

        private FrameworkElement DryRunReport(TradingDryRun d)
        {
            var s = new StackPanel();
            var head = new WrapPanel();
            head.Children.Add(FundUi.Text("Dry-run report  ", 12, FundPalette.TextBright, bold: true, wrap: false));
            head.Children.Add(Chip(d.Ok ? "NO BLOCKERS" : "BLOCKED", d.Ok ? FundPalette.Long : FundPalette.Kill));
            head.Children.Add(FundUi.Text("  " + (_dryRunAt is DateTime at ? at.ToLocalTime().ToString("HH:mm:ss", Inv) : ""), 10.5, FundPalette.Faint, wrap: false));
            s.Children.Add(head);
            foreach (var b in d.Blockers ?? new List<TradingBlocker>())
                s.Children.Add(Callout((b.Code ?? "?") + ": " + (b.Message ?? ""), FundPalette.Kill, FundPalette.KillSurface));
            s.Children.Add(FundUi.KeyValue("Target account · source", (d.TargetAccountId ?? "—") + " · " + (d.SourceId ?? "—")));
            if (d.Legacy is TradingMigrationLegacy l)
            {
                s.Children.Add(FundUi.KeyValue("Orders · ledger rows · equity points", (l.Orders?.ToString(Inv) ?? "—") + " · " + (l.LedgerRows?.ToString(Inv) ?? "—") + " · " + (l.EquityPoints?.ToString(Inv) ?? "—")));
                s.Children.Add(FundUi.KeyValue("Cash · starting capital · peak", FundFormat.Money(l.Cash) + " · " + FundFormat.Money(l.StartingCapital) + " · " + FundFormat.Money(l.PeakEquity)));
                s.Children.Add(FundUi.KeyValue("Halted · autopilot · pending orders", (l.Halted == true ? "yes" : "no") + " · " + (l.Autopilot == true ? "ON" : "off") + " · " + (l.PendingOrders?.ToString(Inv) ?? "—"),
                                               l.Autopilot == true || (l.PendingOrders ?? 0) > 0 ? FundPalette.Risk : null));
                if (l.ResetAt != null) s.Children.Add(FundUi.KeyValue("Last reset", TradingText.Ts(l.ResetAt)));
                s.Children.Add(SubHead("Positions to copy"));
                var ps = l.Positions ?? new List<TradingMigrationPosition>();
                if (ps.Count == 0) s.Children.Add(Line("None.", 10.5, FundPalette.Faint));
                foreach (var pos in ps) s.Children.Add(Line((pos.Symbol ?? "?") + " · " + FundFormat.Qty(pos.Qty) + " @ " + FundFormat.Num(pos.AvgPrice, 4), 10.5, FundPalette.Dim, mono: true));
            }
            if (d.Settings is JsonElement st && st.ValueKind == JsonValueKind.Object)
                s.Children.Add(FundUi.KeyValue("paper-council limits", string.Join(" · ", st.EnumerateObject().Select(x => FundFormat.Label(x.Name) + " " + FundFormat.Plain(x.Value)))));
            if (d.BrokerParams is JsonElement bp && bp.ValueKind == JsonValueKind.Object)
            {
                var exp = new Expander
                {
                    Header = FundUi.Text("Broker parameters that will be copied", 11, FundPalette.Text, wrap: false),
                    Foreground = FundPalette.Text,
                    Margin = new Thickness(0, 6, 0, 0),
                };
                AutomationProperties.SetName(exp, "Broker parameters that will be copied");
                var list = new StackPanel { Margin = new Thickness(16, 4, 0, 0) };
                foreach (var prop in bp.EnumerateObject()) list.Children.Add(FundUi.KeyValue(prop.Name, FundFormat.Plain(prop.Value), size: 10.5));
                exp.Content = list;
                s.Children.Add(exp);
            }
            if (!string.IsNullOrWhiteSpace(d.Note)) s.Children.Add(Line(d.Note!, 10.5, FundPalette.Faint));
            var panel = FundUi.Panel(s);
            AutomationProperties.SetName(panel, "Migration dry-run report, " + (d.Ok ? "no blockers" : "blocked"));
            return panel;
        }

        private async Task DryRunAsync(Button button)
        {
            button.IsEnabled = false;
            _busy = true;
            try
            {
                var res = await PlatformClient.MigrationDryRunAsync();
                if (res.Ok && res.Value != null) { _dryRun = res.Value; _dryRunAt = DateTime.UtcNow; _dryRunError = null; }
                else { _dryRun = null; _dryRunAt = null; _dryRunError = res.Ok ? "Unexpected response from the backend (dry run)." : PlatformClient.DescribeFailure(res); }
            }
            finally { _busy = false; }
            _signatures.Remove(SecMigration);
            await LoadMigrationAsync();
        }

        private async Task MigrateAsync()
        {
            if (_dryRun == null || !DryRunFresh) return;
            var dialog = new TradingMigrationDialog(Owner, _dryRun);
            if (dialog.ShowDialog() != true || dialog.Result == null)
            {
                if (dialog.FailureCode != null)
                {
                    // a refused / failed attempt (§17.14 M2): the dry run is stale now — reload the real state
                    _dryRun = null;
                    _dryRunAt = null;
                    _dryRunError = null;
                    _signatures.Remove(SecMigration);
                    await LoadMigrationAsync();
                }
                return;
            }
            var r = dialog.Result;
            FundDialogs.Info(Owner, "Migration complete",
                "Migration #" + (r.Migration?.IdText ?? "?") + " · " + TradingText.Ts(r.Migration?.CreatedAt) + " → " + (r.Migration?.AccountId ?? "paper-council")
                + "\n\nThe old engine is advisory now (analyses only). The account is traded here, in Portfolio & Trading. The kill switch covers both.");
            _dryRun = null;
            _dryRunAt = null;
            _signatures.Clear();
            await RefreshAsync(force: true);
        }

        // =====================================================================================
        // Risk limits (pre-trade, post-trade, allocator) — adjustable with confirmation + optional audit note (fix1023)
        // =====================================================================================

        private async Task LoadSettingsAsync()
        {
            if (NoAccount(_settingsPanel)) return;
            var id = _accountId!;
            var res = await PlatformClient.GetTradingSettingsAsync(id);
            if (_stopped || _accountId != id) return;
            if (res.Ok && res.Value != null) { _settings = res.Value; _settingsError = null; }
            else { _settings = null; _settingsError = res.Ok ? "Unexpected response from the backend (/v2/portfolio/settings)." : PlatformClient.DescribeFailure(res); }
            RenderSettings();
        }

        private static readonly (string Key, string Title)[] SettingGroups =
        {
            ("post_trade", "Post-trade hard limits"),
            ("pre_trade", "Pre-trade checks (every order)"),
            ("allocator", "Allocator"),
        };

        private void RenderSettings()
        {
            var p = _settingsPanel;
            p.Children.Clear();
            _settingInputs.Clear();
            if (_accountId == null) return;
            p.Children.Add(AccountSelector("PtAcctSettings"));
            if (_settingsError != null) { p.Children.Add(ErrorBlock(_settingsError)); return; }
            var st = _settings;
            if (st?.Bounds == null) { p.Children.Add(Line("Loading…", 11, FundPalette.Faint)); return; }
            p.Children.Add(Line("Settings version " + (st.Version?.ToString(Inv) ?? "—") + (st.UpdatedAt != null ? " · changed " + TradingText.Ts(st.UpdatedAt) + " by " + (st.UpdatedBy ?? "?") : " · defaults")
                                + ". Every change is stored as a new version with your reason (audit log). Bounds are enforced by the backend — never clamped.", 10.5, FundPalette.Dim));
            foreach (var (group, title) in SettingGroups)
            {
                var keys = st.Bounds.Where(kv => kv.Value.Group == group).Select(kv => kv.Key).ToList();
                if (keys.Count == 0) continue;
                var box = new StackPanel();
                box.Children.Add(FundUi.Text(title, 12, FundPalette.TextBright, bold: true));
                if (group == "post_trade")
                    box.Children.Add(Line("Daily loss: at the limit no new risk-increasing orders until the next New York day (equities) / UTC day (crypto). "
                                          + "Drawdown from peak: at the limit the account is KILLED (halt, cancel all, close all, auto trading off).", 10.5, FundPalette.Dim));
                foreach (var key in keys) box.Children.Add(SettingRow(st, key));
                p.Children.Add(FundUi.Panel(box));
            }
            // keys of an unknown group (newer backend) are still editable
            var other = st.Bounds.Where(kv => !SettingGroups.Any(g => g.Key == kv.Value.Group)).Select(kv => kv.Key).ToList();
            if (other.Count > 0)
            {
                var box = new StackPanel();
                box.Children.Add(FundUi.Text("Other", 12, FundPalette.TextBright, bold: true));
                foreach (var key in other) box.Children.Add(SettingRow(st, key));
                p.Children.Add(FundUi.Panel(box));
            }
            var row = new WrapPanel();
            _settingsSave = SmallButton("Review and save…", "Review the risk-limit changes and save them (optional audit note)");
            _settingsSave.IsEnabled = false;
            _settingsSave.Click += async (_, _) => await SaveSettingsAsync();
            row.Children.Add(_settingsSave);
            var discard = SmallButton("Discard changes", "Discard the typed risk-limit changes");
            discard.Click += (_, _) => RenderSettings();
            row.Children.Add(discard);
            p.Children.Add(row);
            _settingsHint = Line("", 10.5, FundPalette.Faint);
            AutomationProperties.SetLiveSetting(_settingsHint, AutomationLiveSetting.Polite);
            p.Children.Add(_settingsHint);
            UpdateSettingsState();
        }

        private FrameworkElement SettingRow(TradingSettings st, string key)
        {
            var b = st.Bounds![key];
            var current = st.Values != null && st.Values.TryGetValue(key, out var cv) ? cv : (JsonElement?)null;
            var def = st.Defaults != null && st.Defaults.TryGetValue(key, out var dv) ? dv : (JsonElement?)null;
            var grid = new Grid { Margin = new Thickness(0, 3, 0, 3) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(110) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(290) });
            var label = FundUi.Text(b.Label ?? key, 11.5, FundPalette.Text);
            label.VerticalAlignment = VerticalAlignment.Center;
            label.ToolTip = key;
            grid.Children.Add(label);
            var box = FundUi.Input(TradingText.Number(current));
            box.Margin = new Thickness(8, 0, 8, 0);
            box.Tag = key;
            box.MaxLength = 16;
            var unit = UnitText(b.Unit);
            AutomationProperties.SetName(box, (b.Label ?? key) + " in " + (unit.Length == 0 ? "units" : unit));
            AutomationProperties.SetHelpText(box, "Range " + TradingText.Number(Num(b.Min)) + " to " + TradingText.Number(Num(b.Max)) + ", default " + TradingText.Number(def));
            box.TextChanged += (_, _) => UpdateSettingsState();
            Grid.SetColumn(box, 1);
            grid.Children.Add(box);
            var hint = FundUi.Text(unit + " · default " + TradingText.Number(def) + " · range " + TradingText.Number(Num(b.Min)) + "–" + TradingText.Number(Num(b.Max))
                                   + (b.Step is double step ? " · step " + TradingUi.Inv(step, "0.###") : ""), 10.5, FundPalette.Faint, wrap: false);
            hint.VerticalAlignment = VerticalAlignment.Center;
            Grid.SetColumn(hint, 2);
            grid.Children.Add(hint);
            _settingInputs[key] = box;
            return grid;
        }

        private static JsonElement? Num(double? v)
        {
            if (v is not double d) return null;
            using var doc = JsonDocument.Parse(d.ToString("R", CultureInfo.InvariantCulture));
            return doc.RootElement.Clone();
        }

        private static string UnitText(string? unit) => unit switch
        {
            "%" => "%",
            "USD" => "USD",
            "s" => "seconds",
            "orders" => "orders",
            "fraction" => "fraction (0.40 = 40 %)",
            null or "" => "",
            _ => unit,
        };

        /// <summary>Typed changes: key → (old, new); invalid inputs → error text.</summary>
        private (Dictionary<string, (double Old, double New)> Changes, List<string> Errors) TypedSettingChanges()
        {
            var changes = new Dictionary<string, (double, double)>(StringComparer.Ordinal);
            var errors = new List<string>();
            var st = _settings;
            if (st?.Bounds == null) return (changes, errors);
            foreach (var (key, box) in _settingInputs)
            {
                var b = st.Bounds[key];
                var old = st.Values != null && st.Values.TryGetValue(key, out var cv) ? TradingText.Double(cv) : null;
                var text = box.Text.Trim();
                if (!TradingUi.TryParseNumber(text, out var v))
                {
                    errors.Add((b.Label ?? key) + ": not a number");
                    box.BorderBrush = FundPalette.Kill;
                    continue;
                }
                if ((b.Min is double lo && v < lo) || (b.Max is double hi && v > hi))
                {
                    errors.Add((b.Label ?? key) + ": must be between " + TradingText.Number(Num(b.Min)) + " and " + TradingText.Number(Num(b.Max)));
                    box.BorderBrush = FundPalette.Kill;
                    continue;
                }
                if ((key == "duplicate_window_seconds" || key == "max_orders_per_day") && Math.Abs(v - Math.Round(v)) > 1e-9)
                {
                    errors.Add((b.Label ?? key) + ": must be a whole number");
                    box.BorderBrush = FundPalette.Kill;
                    continue;
                }
                var changed = old is not double o || Math.Abs(o - v) > 1e-9;
                box.BorderBrush = changed ? FundPalette.Focus : FundPalette.Hairline;
                if (changed) changes[key] = (old ?? double.NaN, v);
            }
            return (changes, errors);
        }

        private void UpdateSettingsState()
        {
            if (_settingsSave == null || _settingsHint == null) return;
            var (changes, errors) = TypedSettingChanges();
            _settingsSave.IsEnabled = changes.Count > 0 && errors.Count == 0;
            _settingsHint.Text = errors.Count > 0 ? string.Join(" · ", errors)
                : changes.Count == 0 ? "No changes." : changes.Count.ToString(Inv) + (changes.Count == 1 ? " change" : " changes") + " — review and save with an audit note.";
            _settingsHint.Foreground = errors.Count > 0 ? FundPalette.Kill : changes.Count > 0 ? FundPalette.Focus : FundPalette.Faint;
        }

        private async Task SaveSettingsAsync()
        {
            var st = _settings;
            var id = _accountId;
            if (st?.Bounds == null || id == null) return;
            var (changes, errors) = TypedSettingChanges();
            if (changes.Count == 0 || errors.Count > 0) return;
            var rows = new List<(string Key, string Label, string Old, string New, string Default)>();
            var warnings = new List<string>();
            foreach (var (key, (old, value)) in changes)
            {
                var b = st.Bounds[key];
                var def = st.Defaults != null && st.Defaults.TryGetValue(key, out var dv) ? dv : (JsonElement?)null;
                rows.Add((key, (b.Label ?? key) + " (" + UnitText(b.Unit) + ")", double.IsNaN(old) ? "—" : TradingUi.Inv(old, "0.####"), TradingUi.Inv(value, "0.####"), TradingText.Number(def)));
                var loosens = key switch
                {
                    "daily_loss_pct" or "drawdown_kill_pct" or "max_order_notional_pct" or "max_order_notional_usd" or "max_order_notional_usdt" or "max_position_pct_equity"
                        or "max_position_pct_crypto" or "price_collar_pct_equity" or "price_collar_pct_crypto" or "max_orders_per_day" => value > old,
                    "duplicate_window_seconds" => value < old,
                    _ => false,
                };
                if (loosens && !double.IsNaN(old))
                    warnings.Add("Loosened limit: " + (b.Label ?? key) + " from " + TradingUi.Inv(old, "0.####") + " to " + TradingUi.Inv(value, "0.####")
                                 + (key == "daily_loss_pct" || key == "drawdown_kill_pct" ? " — a hard post-trade limit (default " + TradingText.Number(def) + ")." : "."));
            }
            var dialog = new TradingSettingsDialog(Owner, id, rows, changes.ToDictionary(kv => kv.Key, kv => kv.Value.New, StringComparer.Ordinal), warnings);
            if (dialog.ShowDialog() != true || dialog.Saved == null) return;
            _settings = dialog.Saved;
            RenderSettings();
            await RefreshAsync(force: false);
        }
    }
}

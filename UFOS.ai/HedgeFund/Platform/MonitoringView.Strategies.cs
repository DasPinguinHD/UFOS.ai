using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // Monitoring → Strategies (§19.1/§19.2) and Accounts & brokers (§19.11 overview accounts).
    internal sealed partial class MonitoringView
    {
        /// <summary>GET /v2/monitoring/decay/rules (§19.14 M5), loaded on activation / F5.</summary>
        private MonDecayRules? _decayRules;

        private string BasisText(string? basis)
        {
            if (string.IsNullOrWhiteSpace(basis)) return "—";
            var label = _decayRules?.BasisLabels != null && _decayRules.BasisLabels.TryGetValue(basis!, out var l) ? l : null;
            var cal = _decayRules?.ByBasis != null && _decayRules.ByBasis.TryGetValue(basis!, out var b) ? b : null;
            var z = cal != null && cal.TryGetValue("RETURN_Z", out var zv) ? FundFormat.Plain(zv) : null;
            var dd = cal != null && cal.TryGetValue("DD_FACTOR", out var dv) ? FundFormat.Plain(dv) : null;
            return basis + (label != null ? " (" + label + ")" : "") + (z != null || dd != null ? " · RETURN_Z " + (z ?? "—") + " · DD_FACTOR " + (dd ?? "—") : "");
        }

        // =====================================================================================
        // Strategies
        // =====================================================================================

        private void RenderStrategies()
        {
            var panel = _strategiesPanel;
            panel.Children.Clear();
            var list = _overview?.Strategies ?? new List<MonStrategy>();
            panel.Children.Add(MonUi.Line("Every incubating strategy is compared with the expected behaviour frozen at its incubation start (holdout card: daily log-return mean μ and σ, "
                                          + "max drawdown). The band k·μ ± 1.96·σ·√k cannot be moved later. A decay rule that triggers PAUSES the strategy (no new orders, positions "
                                          + "kept) until you decide — the backend never resumes or demotes on its own.", 10.5, FundPalette.Faint));
            if (list.Count == 0)
            {
                panel.Children.Add(MonUi.Line("No strategy is incubating — nothing is monitored. Start an incubation in Portfolio & Trading → Incubation.", 11.5, FundPalette.Dim));
                return;
            }
            foreach (var s in list.OrderByDescending(x => x.IsPaused).ThenBy(x => x.DisplayName, StringComparer.OrdinalIgnoreCase))
                panel.Children.Add(StrategyCard(s));
        }

        private Border StrategyCard(MonStrategy s)
        {
            var stack = new StackPanel();
            var id = s.StrategyId ?? "";
            var title = FundUi.Text(s.DisplayName, 12.5, FundPalette.TextBright, bold: true);
            stack.Children.Add(title);
            if (FundUi.FixtureLine(s.Fixture, s.FixtureNote, 11.5, new Thickness(0, 3, 0, 3)) is TextBlock fixture) stack.Children.Add(fixture);
            var (dText, dBrush) = MonUi.DecayChip(s.Decay?.State);
            var chips = ChipRow(MonUi.Chip(dText, dBrush),
                                MonUi.Chip(RunStatusLabel(s.RunStatus), s.RunStatus == "running" ? FundPalette.Focus : FundPalette.Dim),
                                MonUi.Chip("ACCOUNT " + (s.AccountId ?? "?").ToUpperInvariant(), FundPalette.Dim));
            if (s.Deviation?.InBand is bool inBand)
                chips.Children.Add(MonUi.Chip(inBand ? "INSIDE THE BAND" : "OUTSIDE THE BAND", inBand ? FundPalette.Long : FundPalette.Risk));
            stack.Children.Add(chips);
            stack.Children.Add(MonUi.Line("v" + (s.Version?.ToString(Inv) ?? "?") + " · " + id + " · run " + (s.RunId ?? "?") + " · " + MonUi.Int(s.Sessions) + " completed session(s)"
                                          + (s.LastSession != null ? " (last " + s.LastSession + ")" : "") + " · calendar " + (s.Calendar ?? "—")
                                          + " · last decay check " + MonUi.LastCheckText(s.Decay), 10.5, FundPalette.Faint, mono: true));

            if (!string.IsNullOrWhiteSpace(s.Error))
            {
                // §19.14: an entry the backend could not build is shown with its reason, never hidden
                stack.Children.Add(MonUi.Chip("NOT EVALUATED", FundPalette.Kill));
                stack.Children.Add(MonUi.Callout("The backend could not evaluate this strategy: " + s.Error + " — no band, statistics or decay state can be shown for it right now "
                                                 + "(it is still incubating; the monitor retries).", FundPalette.Kill, FundPalette.KillSurface));
                var errCard = FundUi.Panel(stack, new Thickness(14, 12, 14, 12), new Thickness(0, 0, 0, 10));
                errCard.BorderBrush = FundPalette.Kill;
                AutomationProperties.SetName(errCard, "Strategy " + s.DisplayName + ", not evaluated: " + s.Error);
                return errCard;
            }
            if (s.IsPaused) stack.Children.Add(PausedBox(s));
            if ((s.Decay?.ProtectiveExits ?? 0) > 0)
                stack.Children.Add(MonUi.Line("■ " + MonUi.Int(s.Decay!.ProtectiveExits) + " risk-reducing protective exit(s) executed while paused (stop loss / take profit / "
                                              + "drawdown stop to 0) — listed under \"Band chart, events and checks\".", 10.5, FundPalette.Risk));
            stack.Children.Add(MonUi.Line("Decay calibration: " + BasisText(s.Decay?.Basis)
                                          + ((s.Decay?.SinceIndex ?? 0) > 0
                                              ? " · evaluating returns since your keep-running decision (after session " + MonUi.Int(s.Decay!.SinceIndex) + ")"
                                              : ""), 10.5, (s.Decay?.SinceIndex ?? 0) > 0 ? FundPalette.Focus : FundPalette.Faint, mono: true));

            // realised vs expected
            stack.Children.Add(MonUi.Head("Realised vs expected"));
            var r = s.Realised;
            var x = s.Expected;
            var d = s.Deviation;
            if (x == null)
                stack.Children.Add(MonUi.Line("No expected basis — the run has no frozen holdout statistics (expected_json); the band and the decay rules cannot be evaluated.", 11, FundPalette.Risk));
            if (r == null || d == null)
                stack.Children.Add(MonUi.Line("Fewer than 2 completed sessions — no realised statistics yet.", 11, FundPalette.Dim));
            else
            {
                var bandText = d.Band != null ? MonUi.LogAsPct(d.Band.Lo) + " … " + MonUi.LogAsPct(d.Band.Hi) + " after " + MonUi.Int(d.Band.N) + " sessions" : FundFormat.Dash;
                stack.Children.Add(MonUi.Pairs(new[]
                {
                    ("Cumulative return", MonUi.LogAsPct(r.CumLogReturn) + " realised  ·  expected band " + bandText + (d.InBand == true ? "  ✓ inside" : d.InBand == false ? "  ✕ outside" : "")),
                    ("z (cumulative)", MonUi.Num(d.ZCum) + "   ·   rolling z (return rule window) " + MonUi.Num(d.RollingZ)),
                    ("Volatility (annualised)", MonUi.Frac(r.Vol) + " realised vs " + MonUi.Frac(x?.Vol) + " expected   ·   ratio " + MonUi.Num(d.VolRatio)),
                    ("Drawdown", MonUi.Frac(r.Drawdown, 2) + " now · " + MonUi.Frac(r.MaxDrawdown, 2) + " max vs " + MonUi.Frac(x?.MaxDrawdown, 2) + " holdout max   ·   ratio " + MonUi.Num(d.DdRatio)),
                    ("Sharpe", MonUi.Num(r.Sharpe) + " realised vs " + MonUi.Num(x?.Sharpe) + " expected"),
                    ("Tracking error (annualised)", MonUi.Frac(d.TrackingError, 2)),
                }));
            }
            if (x != null)
                stack.Children.Add(MonUi.Line("Expected basis (frozen): μ " + MonUi.Frac(x.MeanDaily, 3) + " / day · σ " + MonUi.Frac(x.StdDaily, 2) + " / day · from run " + (x.SourceRunId ?? "—"),
                                              10.5, FundPalette.Faint, mono: true));

            // rules
            var rules = s.Decay?.Rules ?? new List<MonRule>();
            if (rules.Count > 0)
            {
                stack.Children.Add(MonUi.Head("Decay rules — live (now): value vs threshold"));
                if (MonUi.IsSyntheticPause(s.Decay?.Paused))   // fix1023 review minor 5
                    stack.Children.Add(MonUi.Line("Live numbers — the pause used synthetic numbers (see above).", 10.5, FundPalette.Kill));
                stack.Children.Add(MonUi.Table(new[] { "Rule", "State", "Count", "Value", "Threshold", "Note" },
                    rules.Select(rule => (IReadOnlyList<string>)new[]
                    {
                        string.IsNullOrWhiteSpace(rule.Label) ? MonUi.RuleName(rule.Rule) : rule.Label!,
                        rule.Breached == true ? "BREACHED" : "ok",
                        (rule.Count?.ToString(Inv) ?? "—") + " of " + (rule.Needed?.ToString(Inv) ?? "—"),
                        MonUi.Frac(rule.Value, 2),
                        MonUi.Frac(rule.Threshold, 2),
                        rule.Message ?? "",
                    }), "Decay rules of " + s.DisplayName, new double[] { 210, 80, 70, 80, 80, 0 }));
                stack.Children.Add(MonUi.Line("Values are fractions shown as percent: return rule = sum of the last w daily log returns vs w·μ − Z·σ·√w; drawdown = current drawdown "
                                              + "vs the limit (× holdout max drawdown); volatility = realised 20-session vol vs the limit (× expected vol). A rule triggers after "
                                              + "\"count of\" consecutive breaches (the drawdown rule at once).", 10, FundPalette.Faint));
            }

            // detail (chart, events, checks)
            var expanded = _expanded.Contains(id);
            var toggle = MonUi.Small(expanded ? "▾ Hide band chart, events and checks" : "▸ Band chart, events and checks",
                                     (expanded ? "Hide" : "Show") + " the band chart, events and checks of " + s.DisplayName);
            toggle.Margin = new Thickness(0, 8, 0, 0);
            toggle.HorizontalAlignment = HorizontalAlignment.Left;
            AutomationProperties.SetItemStatus(toggle, expanded ? "expanded" : "collapsed");
            toggle.Click += async (_, _) => await ToggleDetailAsync(id);
            stack.Children.Add(toggle);
            if (expanded) stack.Children.Add(DetailBlock(id));

            var card = FundUi.Panel(stack, new Thickness(14, 12, 14, 12), new Thickness(0, 0, 0, 10));
            if (s.IsPaused) card.BorderBrush = FundPalette.Kill;
            AutomationProperties.SetName(card, "Strategy " + s.DisplayName + ", " + dText);
            return card;
        }

        private static string RunStatusLabel(string? status) => status switch
        {
            "running" => "INCUBATING",
            "completed" => "INCUBATION COMPLETE",
            "aborted" => "ABORTED",
            null or "" => "RUN —",
            _ => status!.ToUpperInvariant(),
        };

        private Border PausedBox(MonStrategy s)
        {
            var p = s.Decay?.Paused;
            var rule = (s.Decay?.Rules ?? new List<MonRule>()).FirstOrDefault(r => r.Rule == p?.Rule);
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text("■ PAUSED by the decay monitor — waiting for your decision", 12, FundPalette.Kill, bold: true));
            if (p?.Synthetic == true)   // sandbox force-decay (SPEC "Sandbox profile" S5)
                stack.Children.Add(FundUi.Text("SYNTHETIC PAUSE (sandbox force-decay) — not real performance"
                                               + (FundUi.FixtureText(p.Fixture, p.FixtureNote) is string ft ? " · " + ft : ""), 11.5, FundPalette.Kill, bold: true));
            stack.Children.Add(MonUi.Line("Since " + TradingText.Ts(p?.At) + " (check day " + (p?.CheckDay ?? "—") + ") · rule: " + MonUi.RuleName(p?.Rule), 11, FundPalette.Text));
            if (!string.IsNullOrWhiteSpace(p?.Reason)) stack.Children.Add(MonUi.Line(p!.Reason, 11, FundPalette.Text));
            // fix1023 B2: the numbers of THIS pause (labelled synthetic for a sandbox force-decay); live numbers only for a real pause
            if (MonUi.PauseNumbersText(p) is string pauseNumbers)
                stack.Children.Add(MonUi.Line(pauseNumbers, 10.5, MonUi.IsSyntheticPause(p) ? FundPalette.Kill : FundPalette.Text, mono: !MonUi.IsSyntheticPause(p)));
            if (rule != null && !MonUi.IsSyntheticPause(p))
                stack.Children.Add(MonUi.Line("Numbers now: value " + MonUi.Frac(rule.Value, 2) + " vs threshold " + MonUi.Frac(rule.Threshold, 2)
                                              + " · breached " + (rule.Count?.ToString(Inv) ?? "—") + " of " + (rule.Needed?.ToString(Inv) ?? "—") + " session(s)"
                                              + (string.IsNullOrWhiteSpace(rule.Message) ? "" : " · " + rule.Message), 10.5, FundPalette.Text, mono: true));
            stack.Children.Add(MonUi.Line("The strategy keeps its positions and its capital and sends NO order that opens or increases exposure (its other decisions are recorded "
                                          + "as \"not executed\"). Exception: its OWN risk-reducing protective exits (stop loss / take profit / drawdown stop to 0, reduce-only) still run while it is paused (§19.14 M3, default — user confirm) — each one raises an alert and is listed under \"Band chart, events and checks\". "
                                          + "The kill switch, drawdown kill, margin orders, manual orders and the account's other strategies are not affected. Incubation sessions "
                                          + "while paused count as missed. No automatic resume, no automatic demotion.", 10.5, FundPalette.Dim));
            var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 6, 0, 0) };
            var keep = TradingUi.Primary("Keep running…");
            AutomationProperties.SetName(keep, "Keep running " + s.DisplayName + " (one confirmation step, optional note)");
            keep.Click += async (_, _) => await DecideAsync(s, PlatformClient.DecisionKeepRunning);
            row.Children.Add(keep);
            var demote = TradingUi.Danger("Demote…");
            demote.Margin = new Thickness(8, 0, 0, 0);
            AutomationProperties.SetName(demote, "Demote " + s.DisplayName + " back to validated (one confirmation step, optional note)");
            demote.Click += async (_, _) => await DecideAsync(s, PlatformClient.DecisionDemote);
            row.Children.Add(demote);
            stack.Children.Add(row);
            var box = new Border
            {
                Background = FundPalette.KillSurface,
                BorderBrush = FundPalette.Kill,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 8, 10, 8),
                Margin = new Thickness(0, 6, 0, 4),
                Child = stack,
            };
            AutomationProperties.SetName(box, "Paused by the decay monitor: " + (p?.Reason ?? MonUi.RuleName(p?.Rule)));
            return box;
        }

        private async Task ToggleDetailAsync(string id)
        {
            if (string.IsNullOrEmpty(id)) return;
            if (!_expanded.Remove(id))
            {
                _expanded.Add(id);
                var res = await PlatformClient.GetMonitoredStrategyAsync(id);
                if (_stopped) return;
                if (res.Ok && res.Value != null) { _details[id] = res.Value; _detailErrors.Remove(id); }
                else _detailErrors[id] = res.Ok ? "Unexpected response from the backend." : PlatformClient.DescribeFailure(res);
            }
            _signatures.Remove(SecStrategies);
            RenderIfChanged(SecStrategies, new object?[] { _overview?.Strategies, _details, _detailErrors, _expanded.OrderBy(x => x).ToList() }, RenderStrategies);
        }

        private UIElement DetailBlock(string id)
        {
            var stack = new StackPanel { Margin = new Thickness(0, 6, 0, 0) };
            if (_detailErrors.TryGetValue(id, out var err))
            {
                stack.Children.Add(MonUi.ErrorBlock(err));
                return stack;
            }
            if (!_details.TryGetValue(id, out var det))
            {
                stack.Children.Add(MonUi.Line("Loading…", 11, FundPalette.Faint));
                return stack;
            }
            var series = det.Series ?? new List<MonPoint>();
            var cursor = MonUi.Line(series.Count > 0 ? MonitoringBandChart.Describe(series.Last()) : "", 10.5, FundPalette.Dim, mono: true);
            AutomationProperties.SetLiveSetting(cursor, AutomationLiveSetting.Polite);
            var chart = new MonitoringBandChart(series) { Margin = new Thickness(0, 2, 0, 2) };
            chart.CursorChanged += info => { cursor.Text = info; AutomationProperties.SetName(cursor, info); };
            stack.Children.Add(chart);
            stack.Children.Add(cursor);
            stack.Children.Add(MonUi.Line("— realised (bright) · - - expected mean (grey) · blue area = incubation band ±1.96·σ·√k · red dashed = decay line of the return rule "
                                          + "(appears once MIN_SESSIONS sessions are complete). Log returns, labelled as simple returns.", 10, FundPalette.Faint));

            var events = det.Events ?? new List<MonEvent>();
            stack.Children.Add(MonUi.Head("Decisions, pauses and protective exits"));
            stack.Children.Add(MonUi.Line("While paused the strategy opens or increases nothing; its OWN risk-reducing protective exits (stop loss / take profit / drawdown stop to 0, reduce-only) still run while it is paused (§19.14 M3, default — user confirm) and are listed below.",
                                          10, FundPalette.Faint));
            stack.Children.Add(MonUi.Table(new[] { "Time", "Action", "By", "Rule", "Reason / audit note" },
                events.OrderByDescending(e => e.Ts ?? "", StringComparer.Ordinal).Select(e => (IReadOnlyList<string>)new[]
                {
                    TradingText.Ts(e.Ts), EventLabel(e.Action), e.Actor ?? "—", MonUi.RuleName(e.Rule), e.Reason ?? "",
                }), "Decay events", new double[] { 130, 110, 60, 170, 0 }));

            var exits = det.ProtectiveExits ?? new List<MonProtectiveExit>();
            stack.Children.Add(MonUi.Head("Protective exits while paused (" + exits.Count.ToString(Inv) + ")"));
            stack.Children.Add(MonUi.Table(new[] { "Time", "Symbol", "Side · qty", "State", "Order", "Reason" },
                exits.Select(x => (IReadOnlyList<string>)new[]
                {
                    TradingText.Ts(x.Ts), x.Symbol ?? "—", (x.Side ?? "?").ToUpperInvariant() + " " + FundFormat.Qty(x.Qty), (x.State ?? "—").ToUpperInvariant(),
                    x.ClientOrderId ?? "—", x.Reason ?? "",
                }), "Protective exits while paused", new double[] { 130, 70, 90, 90, 150, 0 }));

            var checks = (det.Checks ?? new List<MonCheck>()).OrderByDescending(c => c.Day ?? "", StringComparer.Ordinal).Take(15).ToList();
            stack.Children.Add(MonUi.Head("Newest checks (one per completed session)"));
            stack.Children.Add(MonUi.Table(new[] { "Day", "State", "Triggered", "Return rule", "Drawdown", "Volatility" },
                checks.Select(c => (IReadOnlyList<string>)new[]
                {
                    c.Day ?? "—", (c.State ?? "—").ToUpperInvariant(), (c.Triggered?.Count ?? 0) > 0 ? string.Join(", ", c.Triggered!) : "—",
                    StatText(c.Stats, "return_band"), StatText(c.Stats, "drawdown"), StatText(c.Stats, "volatility"),
                }), "Newest decay checks", new double[] { 90, 90, 110, 0, 0, 0 }));

            var rulesInfo = det.Rules ?? _decayRules;
            var ruleText = rulesInfo?.Text ?? new List<string>();
            if (ruleText.Count > 0)
            {
                stack.Children.Add(MonUi.Head("The rule (decay.RULES_BY_BASIS — default, user confirm; text for the default basis "
                                              + (rulesInfo?.DefaultBasis ?? "—") + ")"));
                foreach (var sentence in ruleText) stack.Children.Add(MonUi.Line("• " + sentence, 10.5, FundPalette.Dim));
            }
            var byBasis = rulesInfo?.ByBasis ?? new Dictionary<string, Dictionary<string, JsonElement>>();
            if (byBasis.Count > 0)
            {
                var mine = det.Strategy?.Decay?.Basis;
                stack.Children.Add(MonUi.Head("Calibration per holdout basis"));
                stack.Children.Add(MonUi.Table(new[] { "Basis", "Label", "RETURN_Z", "DD_FACTOR" },
                    byBasis.Select(kv => (IReadOnlyList<string>)new[]
                    {
                        kv.Key + (kv.Key == mine ? "  ◂ this strategy" : "") + (kv.Key == rulesInfo?.DefaultBasis ? " (default)" : ""),
                        rulesInfo?.BasisLabels != null && rulesInfo.BasisLabels.TryGetValue(kv.Key, out var bl) ? bl : "",
                        kv.Value.TryGetValue("RETURN_Z", out var z) ? FundFormat.Plain(z) : "—",
                        kv.Value.TryGetValue("DD_FACTOR", out var dd) ? FundFormat.Plain(dd) : "—",
                    }), "Decay calibration per holdout basis", new double[] { 200, 0, 80, 80 }));
            }
            return stack;
        }

        private static string EventLabel(string? action) => action switch
        {
            "paused" => "PAUSED",
            "keep_running" => "KEEP RUNNING",
            "demoted" => "DEMOTED",
            "protective_exit" or "reduce_only_exit" => "PROTECTIVE EXIT (reduce-only)",
            null or "" => "—",
            _ => action!.ToUpperInvariant(),
        };

        /// <summary>"value vs threshold (count) ✕" of one rule in a check's stats.</summary>
        private static string StatText(JsonElement? stats, string rule)
        {
            var s = ShJson.Get(stats, rule);
            if (s == null) return "—";
            var breached = ShJson.Bool(s, "breached") == true;
            var count = ShJson.Num(s, "count");
            return MonUi.Frac(ShJson.Num(s, "value"), 2) + " vs " + MonUi.Frac(ShJson.Num(s, "threshold"), 2)
                   + (count is double c && c > 0 ? " (" + c.ToString("0", Inv) + ")" : "") + (breached ? " ✕" : "");
        }

        private async Task DecideAsync(MonStrategy s, string action)
        {
            if (_busy || string.IsNullOrEmpty(s.StrategyId)) return;
            var minSessions = _decayRules?.Rules != null && _decayRules.Rules.TryGetValue("MIN_SESSIONS", out var ms) && ms.ValueKind == JsonValueKind.Number
                              && ms.TryGetInt32(out var m) ? m : (int?)null;
            var dialog = new MonDecisionDialog(Owner, s, action, minSessions);
            _busy = true;
            bool? ok;
            try { ok = dialog.ShowDialog(); }
            finally { _busy = false; }
            if (ok != true || dialog.Result == null) return;
            SetAction(action == PlatformClient.DecisionDemote
                          ? "Demoted " + s.DisplayName + " — the incubation run was aborted and the strategy is back to validated; the runtime closes its positions in the next tradable window."
                          : "Keep running: " + s.DisplayName + " is no longer paused — only the returns after your decision are evaluated from now on.",
                      FundPalette.Long);
            await RefreshAsync(force: true);
        }

        // =====================================================================================
        // Accounts & brokers
        // =====================================================================================

        private void RenderAccounts()
        {
            var panel = _accountsPanel;
            panel.Children.Clear();
            var m = _overview?.Monitor;
            var mon = new StackPanel();
            mon.Children.Add(FundUi.Text("Monitor task (sp-monitor)", 12, FundPalette.TextBright, bold: true));
            mon.Children.Add(MonUi.Pairs(new[]
            {
                ("Running", (m?.Running == true ? "yes" : m?.Running == false ? "NO" : "—") + (m?.Stale == true ? " · MONITOR STALE" : "")),
                ("Last tick", m?.LastTickAt != null ? TradingText.Ts(m.LastTickAt) : "never"),
                ("Last error", string.IsNullOrWhiteSpace(m?.LastError) ? "none" : m!.LastError!),
            }, m?.LastError != null ? FundPalette.Risk : null));
            mon.Children.Add(MonUi.Line("Every 30 s: audit scanner (kill switch, reconciliation halts, daily loss / drawdown halts, dead-man switch, incubation finished) and the "
                                        + "broker link watcher (an outage of 10 min → one \"broker disconnected\" alert). Every 60 s: decay checks and the report scheduler. "
                                        + "A failure of the monitor never touches trading.", 10.5, FundPalette.Faint));
            panel.Children.Add(FundUi.Panel(mon, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10)));

            var accounts = _overview?.Accounts ?? new List<MonAccount>();
            if (accounts.Count == 0)
            {
                panel.Children.Add(MonUi.Line("No platform account is open — open paper-main in Portfolio & Trading.", 11.5, FundPalette.Dim));
                return;
            }
            foreach (var a in accounts) panel.Children.Add(AccountCard(a));
        }

        private Border AccountCard(MonAccount a)
        {
            var stack = new StackPanel();
            var isBroker = a.Link != null || (a.Broker != null && a.Broker != "internal_paper");
            stack.Children.Add(FundUi.Text((a.Label ?? a.AccountId ?? "?") + " · " + (a.AccountId ?? "?"), 12.5, FundPalette.TextBright, bold: true));
            var chips = ChipRow(MonUi.Chip(isBroker ? "PAPER / DEMO AT THE BROKER" : "PAPER · SIMULATED", FundPalette.Focus),
                                MonUi.Chip("BROKER " + (a.Broker ?? "?").ToUpperInvariant(), FundPalette.Dim));
            if (a.Halt?.Active == true) chips.Children.Add(MonUi.Chip("HALTED · " + BrokerText.HaltKind(a.Halt.Kind).ToUpperInvariant(), FundPalette.Kill));
            else chips.Children.Add(MonUi.Chip("NOT HALTED", FundPalette.Long));
            if (a.ValuationOk == false) chips.Children.Add(MonUi.Chip("NOT VALUED", FundPalette.Risk));
            if (isBroker)
            {
                var (lt, lb) = BrokerText.LinkChip(a.Link);
                chips.Children.Add(MonUi.Chip(lt, lb));
                var (rt, rb) = BrokerText.ReconcileChip(a.Reconciliation, a.Halt);
                chips.Children.Add(MonUi.Chip(rt, rb));
            }
            stack.Children.Add(chips);
            stack.Children.Add(MonUi.Pairs(new[]
            {
                ("Equity", a.ValuationOk == false ? "not valued" : FundFormat.Money(a.Equity)),
                ("Day P&L", a.ValuationOk == false ? "—" : FundFormat.Money(a.DayPnl, signed: true)),
                ("Drawdown from peak", FundFormat.Pct(a.DrawdownPct, 2)),
            }));
            if (a.ValuationOk == false && !string.IsNullOrWhiteSpace(a.ValuationMessage))
                stack.Children.Add(MonUi.Line(a.ValuationMessage, 10.5, FundPalette.Risk));
            if (a.Halt?.Active == true)
                stack.Children.Add(MonUi.Line("Halt since " + TradingText.Ts(a.Halt.At) + ": " + (a.Halt.Reason ?? "(no reason given)"), 10.5, FundPalette.Kill));
            if (isBroker)
            {
                if (!string.IsNullOrWhiteSpace(a.Link?.Message)) stack.Children.Add(MonUi.Line("Link: " + a.Link!.Message, 10.5, FundPalette.Dim));
                if (a.Link?.NextRetryAt != null) stack.Children.Add(MonUi.Line("Next automatic try " + TradingText.Ts(a.Link.NextRetryAt), 10.5, FundPalette.Faint));
                if (a.Link?.LastOkAt != null) stack.Children.Add(MonUi.Line("Last answer from the broker " + TradingText.Ts(a.Link.LastOkAt), 10.5, FundPalette.Faint));
                var rec = a.Reconciliation;
                stack.Children.Add(MonUi.Line("Reconciliation: " + (rec?.Status ?? "—") + (rec?.At != null ? " at " + TradingText.Ts(rec.At) : "")
                                              + (string.IsNullOrWhiteSpace(rec?.Reason) ? "" : " — " + rec!.Reason), 10.5,
                                              rec?.Status == "mismatch" ? FundPalette.Kill : FundPalette.Dim));
                var open = MonUi.Small("Keys & data → Brokers…", "Open Keys and data at the Brokers section for " + (a.AccountId ?? "this account"));
                open.Margin = new Thickness(0, 6, 0, 0);
                open.HorizontalAlignment = HorizontalAlignment.Left;
                open.Click += (_, _) => BrokersRequested?.Invoke();
                stack.Children.Add(open);
            }
            var card = FundUi.Panel(stack, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10));
            if (a.Halt?.Active == true) card.BorderBrush = FundPalette.Kill;
            AutomationProperties.SetName(card, "Account " + (a.AccountId ?? "?"));
            return card;
        }
    }
}

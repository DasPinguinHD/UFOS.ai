using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// "Attribution" section of the backtest and validation reports (Strategy Platform §11.1 layer 1, §11.3 WPF;
    /// payload §11.5): WHY the run performed as it did — computed deterministically by the backend when the run
    /// finished (no AI). Blocks: decomposition bars (regression: market / alpha / residual; P&amp;L sources: price,
    /// costs, dividends, other cash), long/short, by-symbol top/bottom table (long/short columns), by-year table
    /// (strategy vs benchmark, excess green/red), regime table, best/worst 63-session periods, drawdowns with their
    /// biggest losing symbols. Every block may be {unavailable: reason} — the reason is shown verbatim, nothing is
    /// recomputed or guessed. Numbers use the fact table's display strings (the same strings the AI explanation must
    /// cite) and are formatted from the raw value only when the backend lists no fact for them. A run without an
    /// attribution shows the backend's reason (§11.4: "Not available for runs created before engine … — run the
    /// backtest again."). Validation runs: the base (training) run's attribution; gate_facts are NOT shown here
    /// (they are the gate table's values — no duplication).
    /// </summary>
    internal sealed class AttributionView : StackPanel
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private readonly Dictionary<string, string> _display = new(StringComparer.Ordinal);

        /// <param name="attribution">The run card's `attribution` (null = none).</param>
        /// <param name="unavailableReason">The card's `attribution_unavailable` (the backend's reason when null).</param>
        /// <param name="scopeNote">What the attribution covers (e.g. "base (training) run of this validation").</param>
        /// <param name="headerExtra">Optional element right of the section title (the "Explain with AI…" button).</param>
        public AttributionView(JsonElement? attribution, string? unavailableReason, string scopeNote, UIElement? headerExtra)
        {
            AutomationProperties.SetName(this, "Attribution");
            var head = new DockPanel { Margin = new Thickness(0, 14, 0, 2), LastChildFill = true };
            if (headerExtra != null)
            {
                DockPanel.SetDock(headerExtra, Dock.Right);
                head.Children.Add(headerExtra);
            }
            var title = FundUi.Text("Attribution", 12, FundPalette.Text, bold: true);
            title.VerticalAlignment = VerticalAlignment.Center;
            head.Children.Add(title);
            Children.Add(head);
            Children.Add(Line("Computed from the run when it finished — deterministic, no AI · " + scopeNote, 10.5, FundPalette.Faint));

            if (attribution is not JsonElement a || a.ValueKind != JsonValueKind.Object)
            {
                Children.Add(Notice(string.IsNullOrWhiteSpace(unavailableReason)
                    ? "The backend sent no attribution and no reason — it may be outdated; restart the Hedge Fund backend."
                    : unavailableReason!));
                return;
            }

            foreach (var f in BtJson.Array(a, "facts"))
            {
                var id = BtJson.Str(f, "id");
                var display = BtJson.Str(f, "display");
                if (!string.IsNullOrEmpty(id) && display != null) _display[id!] = display;
            }

            if (BtJson.Str(a, "note") is { Length: > 0 } note) Children.Add(Notice(note));
            var meta = new List<string>();
            if (BtJson.Str(a, "range", "start") is string rs) meta.Add(rs + " … " + (BtJson.Str(a, "range", "end") ?? "?"));
            if (BtJson.Str(a, "benchmark") is string bm) meta.Add("benchmark " + bm);
            if (BtJson.Num(a, "capital") is double cap) meta.Add("capital " + D("run.capital", cap, v => FundFormat.Money(v, decimals: 0)));
            if (BtJson.Str(a, "version") is string ver) meta.Add(ver);
            if (BtJson.Str(a, "engine_version") is string ev) meta.Add("engine " + ev);
            if (meta.Count > 0) Children.Add(Line(string.Join(" · ", meta), 10.5, FundPalette.Faint, mono: true));

            RenderDecomposition(BtJson.Get(a, "decomposition"));
            RenderLongShort(BtJson.Get(a, "long_short"));
            RenderBySymbol(BtJson.Get(a, "by_symbol"));
            RenderByYear(BtJson.Get(a, "by_year"));
            RenderRegimes(BtJson.Get(a, "by_regime"));
            RenderPeriods(BtJson.Get(a, "periods"));
            RenderCosts(BtJson.Get(a, "costs"));
        }

        // =====================================================================================
        // Blocks
        // =====================================================================================

        private void RenderDecomposition(JsonElement? dec)
        {
            Sub("Decomposition");
            if (Unavailable(dec, "decomposition")) return;
            var total = BtJson.Num(dec, "total_pnl");
            Children.Add(FundUi.KeyValue("Total P&L", D("dec.total_pnl", total, Money), Signed(total), 11));

            // regression vs the benchmark: market + alpha + residual + outside = total P&L
            var reg = BtJson.Get(dec, "regression");
            var regTitle = "Regression vs " + (BtJson.Str(reg, "benchmark") ?? "the benchmark")
                           + (BtJson.Num(reg, "beta") is double beta ? " (beta " + D("dec.beta", beta, v => FundFormat.Num(v, 2)) + ")" : "");
            Children.Add(Line(regTitle, 11, FundPalette.Text));
            if (!Unavailable(reg, "regression"))
            {
                Children.Add(Bars("Return decomposition (regression)", new List<BarRow>
                {
                    Bar("Market (beta × benchmark)", "dec.market_pnl", BtJson.Num(reg, "market_pnl"),
                        "ann. " + D("dec.market_ann", BtJson.Num(reg, "market_ann"), Fraction)),
                    Bar("Alpha", "dec.alpha_pnl", BtJson.Num(reg, "alpha_pnl"),
                        "ann. " + D("dec.alpha_ann", BtJson.Num(reg, "alpha_ann"), Fraction)),
                    Bar("Residual", "dec.residual_pnl", BtJson.Num(reg, "residual_pnl"),
                        "ann. " + D("dec.residual_ann", BtJson.Num(reg, "residual_ann"), Fraction)),
                    Bar("Outside the regression days", "dec.outside_regression_pnl", BtJson.Num(reg, "outside_regression_pnl"), "first day, paused valuations"),
                }));
                if (BtJson.Num(reg, "pairs") is double pairs)
                    Children.Add(Line("Daily OLS of the strategy return on the benchmark return over " + pairs.ToString("0", Inv)
                                      + " day pairs (the same regression as the metrics' alpha/beta); the four parts add up to the total P&L.", 10.5, FundPalette.Faint));
            }

            // P&L sources: price − costs + dividends + other cash (+ float noise) = total P&L
            var src = BtJson.Get(dec, "sources");
            Children.Add(Line("P&L sources", 11, FundPalette.Text));
            if (!Unavailable(src, "sources"))
            {
                var costsTotal = BtJson.Num(src, "costs_total");
                var costParts = string.Join(" · ", new[]
                {
                    "slippage " + D("dec.slippage", BtJson.Num(src, "slippage"), Money),
                    "fees " + D("dec.fees", BtJson.Num(src, "fees"), Money),
                    "borrow " + D("dec.borrow", BtJson.Num(src, "borrow"), Money),
                    "funding " + D("dec.funding", BtJson.Num(src, "funding"), Money),
                });
                Children.Add(Bars("P&L sources", new List<BarRow>
                {
                    Bar("Price P&L (before slippage)", "dec.price_pnl", BtJson.Num(src, "price_pnl"), null),
                    // costs are reported as positive amounts; the bar shows them as the deduction they are
                    new BarRow("Costs", costsTotal is double ct ? (double?)(-ct) : null,
                               costsTotal is double ct2 ? "−" + D("dec.costs_total", ct2, Money) : FundFormat.Dash, costParts),
                    Bar("Dividends (net)", "dec.dividends_net", BtJson.Num(src, "dividends_net"), "received − paid on shorts"),
                    Bar("Other cash", "dec.cash_other", BtJson.Num(src, "cash_other"), null),
                }));
                Children.Add(Line("Gross P&L (before costs) " + D("dec.gross_pnl", BtJson.Num(src, "gross_pnl"), Money)
                                  + " · unattributed " + D("dec.unattributed", BtJson.Num(src, "unattributed"), Money) + " (rounding only)", 10.5, FundPalette.Faint, mono: true));
            }
        }

        private void RenderLongShort(JsonElement? ls)
        {
            Sub("Long / short");
            if (Unavailable(ls, "long_short")) return;
            var rows = new List<Cell[]>
            {
                new[] { C("Long side"), Pnl("ls.long.pnl", BtJson.Num(ls, "long_pnl")), C(D("ls.long.exposure", BtJson.Num(ls, "avg_long_exposure"), Fraction)) },
                new[] { C("Short side"), Pnl("ls.short.pnl", BtJson.Num(ls, "short_pnl")), C(D("ls.short.exposure", BtJson.Num(ls, "avg_short_exposure"), Fraction)) },
                new[] { C("No position at either close"), Pnl("ls.flat.pnl", BtJson.Num(ls, "flat_pnl")), C(FundFormat.Dash) },
            };
            Children.Add(Table(new[] { "Side", "P&L", "Avg. exposure" }, new[] { 200.0, 130, 120 }, rows, "Long and short side"));
        }

        private void RenderBySymbol(JsonElement? bs)
        {
            Sub("By symbol");
            if (Unavailable(bs, "by_symbol")) return;
            var headers = new[] { "Symbol", "P&L", "Share", "Long P&L", "Short P&L", "Days held", "Fills", "Costs" };
            var widths = new[] { 90.0, 110, 70, 105, 105, 70, 55, 95 };
            var top = BtJson.Array(bs, "top").Select(SymbolRow).ToList();
            var bottom = BtJson.Array(bs, "bottom").Select(SymbolRow).ToList();
            Children.Add(Line("Top contributors", 10.5, FundPalette.Dim));
            if (top.Count > 0) Children.Add(Table(headers, widths, top, "Top contributing symbols"));
            else Children.Add(Line("None.", 11, FundPalette.Faint));
            Children.Add(Line("Bottom contributors (worst first)", 10.5, FundPalette.Dim));
            if (bottom.Count > 0) Children.Add(Table(headers, widths, bottom, "Bottom contributing symbols"));
            else Children.Add(Line("None — every symbol is in the top list.", 11, FundPalette.Faint));

            var parts = new List<string>();
            if (BtJson.Num(bs, "symbols") is double n) parts.Add(D("sym.symbols", n, Count) + " symbols traded or held");
            if (BtJson.Get(bs, "others") is JsonElement oth && BtJson.Num(oth, "count") is double oc && oc > 0)
                parts.Add("others (" + D("sym._others.count", oc, Count) + "): " + D("sym._others.pnl", BtJson.Num(oth, "pnl"), Money)
                          + " · " + D("sym._others.share", BtJson.Num(oth, "share"), Fraction) + " of the total");
            if (BtJson.Get(bs, "reconciliation") is JsonElement rec)
                parts.Add("other cash " + Money(BtJson.Num(rec, "cash_other")) + " · unattributed " + D("sym.unattributed", BtJson.Num(rec, "unattributed"), Money));
            if (parts.Count > 0) Children.Add(Line(string.Join(" · ", parts), 10.5, FundPalette.Faint, mono: true));
            Children.Add(Line("Share = the symbol's P&L / total P&L. Long/short P&L by the side held at the previous close; costs = fees + slippage + borrow + funding.", 10.5, FundPalette.Faint));
        }

        private Cell[] SymbolRow(JsonElement r)
        {
            var sym = BtJson.Str(r, "symbol") ?? "?";
            var b = "sym." + sym + ".";
            return new[]
            {
                C(sym),
                Pnl(b + "pnl", BtJson.Num(r, "pnl")),
                C(D(b + "share", BtJson.Num(r, "share"), Fraction)),
                Pnl(b + "long_pnl", BtJson.Num(r, "long_pnl")),
                Pnl(b + "short_pnl", BtJson.Num(r, "short_pnl")),
                C(D(b + "days_held", BtJson.Num(r, "days_held"), Count)),
                C(D(b + "fills", BtJson.Num(r, "fills"), Count)),
                C(D(b + "costs", BtJson.Num(r, "costs"), Money)),
            };
        }

        private void RenderByYear(JsonElement? by)
        {
            Sub("By year");
            if (Unavailable(by, "by_year")) return;
            var rows = new List<Cell[]>();
            foreach (var y in BtJson.Array(by, "years"))
            {
                var year = BtJson.Str(y, "year") ?? "?";
                var p = "year." + year + ".";
                var excess = BtJson.Num(y, "excess");
                rows.Add(new[]
                {
                    C(year),
                    C(D(p + "return", BtJson.Num(y, "return"), Fraction)),
                    C(D(p + "benchmark", BtJson.Num(y, "benchmark_return"), Fraction)),
                    new Cell(D(p + "excess", excess, Fraction), Signed(excess)),
                    C(D(p + "max_drawdown", BtJson.Num(y, "max_drawdown"), Fraction)),
                    C(D(p + "net_exposure", BtJson.Num(y, "avg_net_exposure"), Fraction)),
                    Pnl(p + "pnl", BtJson.Num(y, "pnl")),
                    C(BtJson.Str(y, "days") ?? FundFormat.Dash),
                });
            }
            if (rows.Count == 0)
            {
                Children.Add(Line("No calendar year in this run.", 11, FundPalette.Faint));
                return;
            }
            Children.Add(Table(new[] { "Year", "Strategy", "Benchmark", "Excess", "Max DD", "Avg. net exp.", "P&L", "Days" },
                               new[] { 60.0, 90, 90, 90, 80, 95, 115, 50 }, rows, "Returns by calendar year: strategy against the benchmark"));
            Children.Add(Line("Excess = strategy − benchmark return (green above, red below the benchmark).", 10.5, FundPalette.Faint));
        }

        private void RenderRegimes(JsonElement? rg)
        {
            Sub("Market regimes");
            if (Unavailable(rg, "by_regime")) return;
            var rows = new List<Cell[]>();
            foreach (var r in BtJson.Array(rg, "regimes"))
            {
                var key = BtJson.Str(r, "regime") ?? "?";
                var p = "regime." + key + ".";
                rows.Add(new[]
                {
                    C(RegimeLabel(key)),
                    C(D(p + "days", BtJson.Num(r, "days"), Count)),
                    C(D(p + "share", BtJson.Num(r, "share"), Fraction)),
                    new Cell(D(p + "strategy_ann", BtJson.Num(r, "strategy_return_ann"), Fraction), Signed(BtJson.Num(r, "strategy_return_ann"))),
                    C(D(p + "benchmark_ann", BtJson.Num(r, "benchmark_return_ann"), Fraction)),
                    C(D(p + "hit_rate", BtJson.Num(r, "hit_rate"), Fraction)),
                });
            }
            if (rows.Count > 0)
                Children.Add(Table(new[] { "Regime", "Days", "Share", "Strategy (ann.)", "Benchmark (ann.)", "Hit rate" },
                                   new[] { 140.0, 60, 70, 110, 120, 80 }, rows, "Strategy and benchmark return by market regime"));
            else
                Children.Add(Line("No labelled days.", 11, FundPalette.Faint));
            Children.Add(Line("Regimes from " + (BtJson.Str(rg, "benchmark") ?? "the benchmark") + ", known at the previous close (no look-ahead): trend = trailing "
                              + (BtJson.Str(rg, "trend_window") ?? "?") + "-session return ≥ 0 → up; volatile = trailing " + (BtJson.Str(rg, "vol_window") ?? "?")
                              + "-session volatility ≥ its expanding median. Excluded: warm-up " + D("regime.warmup_days", BtJson.Num(rg, "warmup_days"), Count)
                              + " days, without a regime " + D("regime.unknown_days", BtJson.Num(rg, "unknown_days"), Count) + " days. Returns annualised, arithmetic.", 10.5, FundPalette.Faint));
        }

        private static string RegimeLabel(string key) => key switch
        {
            "up_calm" => "Up · calm",
            "up_volatile" => "Up · volatile",
            "down_calm" => "Down · calm",
            "down_volatile" => "Down · volatile",
            _ => key,
        };

        private void RenderPeriods(JsonElement? pd)
        {
            Sub("Best and worst periods");
            if (Unavailable(pd, "periods")) return;
            var window = BtJson.Str(pd, "window") ?? "?";
            var rows = new List<Cell[]>();
            foreach (var kind in new[] { "best", "worst" })
            {
                var k = 0;
                foreach (var w in BtJson.Array(pd, kind))
                {
                    k++;
                    var p = "period." + kind + k.ToString(Inv) + ".";
                    var excess = BtJson.Num(w, "excess");
                    rows.Add(new[]
                    {
                        C((kind == "best" ? "Best " : "Worst ") + k.ToString(Inv)),
                        C(D(p + "start", null, null, BtJson.Str(w, "start")) + " … " + D(p + "end", null, null, BtJson.Str(w, "end"))),
                        new Cell(D(p + "strategy", BtJson.Num(w, "strategy_return"), Fraction), Signed(BtJson.Num(w, "strategy_return"))),
                        C(D(p + "benchmark", BtJson.Num(w, "benchmark_return"), Fraction)),
                        new Cell(D(p + "excess", excess, Fraction), Signed(excess)),
                    });
                }
            }
            if (rows.Count > 0)
                Children.Add(Table(new[] { "Period", "Window", "Strategy", "Benchmark", "Excess" }, new[] { 80.0, 190, 90, 90, 90 }, rows,
                                   "Best and worst " + window + "-session periods"));
            else
                Children.Add(Line("The run is shorter than one " + window + "-session window.", 11, FundPalette.Faint));
            Children.Add(Line("Non-overlapping " + window + "-session windows.", 10.5, FundPalette.Faint));

            Children.Add(Line("Deepest drawdowns and their biggest losing symbols", 10.5, FundPalette.Dim));
            var dd = new List<Cell[]>();
            var n = 0;
            foreach (var d in BtJson.Array(pd, "drawdowns"))
            {
                n++;
                var p = "dd." + n.ToString(Inv) + ".";
                var recovered = BtJson.Bool(d, "recovered") == true;
                var recovery = recovered ? D(p + "recovery", null, null, BtJson.Str(d, "recovery")) : D(p + "recovery", null, null, "not recovered");
                var losers = string.Join("; ", BtJson.Array(d, "contributors").Select(c =>
                {
                    var sym = BtJson.Str(c, "symbol") ?? "?";
                    return sym + " " + D(p + "sym." + sym + ".pnl", BtJson.Num(c, "pnl"), Money);
                }));
                dd.Add(new[]
                {
                    C(n.ToString(Inv)),
                    C(D(p + "start", null, null, BtJson.Str(d, "start"))),
                    C(D(p + "trough", null, null, BtJson.Str(d, "trough"))),
                    new Cell(recovery, recovered ? FundPalette.Text : FundPalette.Risk),
                    new Cell(D(p + "depth", BtJson.Num(d, "depth"), Fraction), FundPalette.Short),
                    C(D(p + "days_to_trough", BtJson.Num(d, "days_to_trough"), Count)),
                    C(losers.Length > 0 ? losers : "none with a loss"),
                });
            }
            if (dd.Count > 0)
                Children.Add(Table(new[] { "#", "Peak", "Trough", "Recovery", "Depth", "Sessions", "Biggest losers (peak → trough)" },
                                   new[] { 30.0, 90, 90, 105, 75, 65, -1 }, dd, "Deepest drawdowns with their biggest losing symbols"));
            else
                Children.Add(Line("No drawdown.", 11, FundPalette.Faint));
        }

        private void RenderCosts(JsonElement? c)
        {
            Sub("Costs");
            if (Unavailable(c, "costs")) return;
            var pct = BtJson.Num(c, "pct_of_gross_pnl");
            var text = "Total " + D("cost.total", BtJson.Num(c, "total"), Money)
                       + " · " + (pct.HasValue ? D("cost.pct_of_gross", pct, v => FundFormat.Pct(v, 2)) + " of gross P&L" : "% of gross P&L: " + (BtJson.Str(c, "pct_of_gross_reason") ?? FundFormat.Dash))
                       + " · turnover " + D("cost.turnover", BtJson.Num(c, "turnover_annual"), Fraction) + " per year";
            Children.Add(Line(text, 11, FundPalette.Dim, mono: true));
        }

        // =====================================================================================
        // Helpers
        // =====================================================================================

        private sealed record Cell(string Text, Brush? Brush);
        private sealed record BarRow(string Label, double? Value, string Display, string? Note);

        private static Cell C(string text) => new(text, null);

        private Cell Pnl(string factId, double? value) => new(D(factId, value, Money), Signed(value));

        private BarRow Bar(string label, string factId, double? value, string? note) => new(label, value, D(factId, value, Money), note);

        private static string Money(double? v) => FundFormat.Money(v);
        private static string Fraction(double? v) => FundFormat.Fraction(v, 2);
        private static string Count(double? v) => FundFormat.Num(v, 0);

        /// <summary>The fact table's display string for <paramref name="factId"/>; without a fact the raw value formatted
        /// (or the raw text for dates/status); "—" when the backend sent no value.</summary>
        private string D(string factId, double? value, Func<double?, string>? format, string? rawText = null)
        {
            if (_display.TryGetValue(factId, out var shown)) return shown;
            if (rawText != null) return rawText;
            if (value is double && format != null) return format(value);
            return FundFormat.Dash;
        }

        private static Brush Signed(double? v) => v is double x && !double.IsNaN(x) ? (x > 0 ? FundPalette.Long : x < 0 ? FundPalette.Short : FundPalette.Dim) : FundPalette.Faint;

        /// <summary>True (and the backend's reason shown) when the block is missing or {unavailable: reason}.</summary>
        private bool Unavailable(JsonElement? block, string name)
        {
            if (block is not JsonElement b || b.ValueKind != JsonValueKind.Object)
            {
                Children.Add(Line("Not available: the attribution has no " + name + " block.", 11, FundPalette.Risk));
                return true;
            }
            if (b.TryGetProperty("unavailable", out var why))
            {
                var reason = why.ValueKind == JsonValueKind.String ? why.GetString() : FundFormat.Plain(why);
                Children.Add(Line("Not available: " + (string.IsNullOrWhiteSpace(reason) ? "no reason given" : reason), 11, FundPalette.Risk));
                return true;
            }
            return false;
        }

        private void Sub(string title)
        {
            var t = FundUi.Text(title, 11.5, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 10, 0, 4);
            Children.Add(t);
        }

        private static TextBlock Line(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
            return t;
        }

        private static Border Notice(string text) => new()
        {
            Background = FundPalette.Panel,
            BorderBrush = FundPalette.Risk,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(10, 6, 10, 6),
            Margin = new Thickness(0, 4, 0, 8),
            Child = FundUi.Text(text, 11.5, FundPalette.Risk),
        };

        /// <summary>Diverging bars around zero (green right = gain, red left = loss), scaled to the largest |value|.</summary>
        private static Border Bars(string name, List<BarRow> rows)
        {
            var max = rows.Select(r => r.Value is double v && !double.IsNaN(v) ? Math.Abs(v) : 0).DefaultIfEmpty(0.0).Max();
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(190) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(115) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1.2, GridUnitType.Star) });
            var names = new List<string>();
            for (var i = 0; i < rows.Count; i++)
            {
                var r = rows[i];
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var label = FundUi.Text(r.Label, 11, FundPalette.Text, wrap: false);
                label.VerticalAlignment = VerticalAlignment.Center;
                label.Margin = new Thickness(0, 3, 8, 3);
                Place(grid, label, i, 0);

                var frac = r.Value is double v && max > 0 && !double.IsNaN(v) ? Math.Abs(v) / max : 0;
                var negative = r.Value is double nv && nv < 0;
                var half = new Grid { Height = 12, VerticalAlignment = VerticalAlignment.Center };
                var fill = new Border { Background = negative ? FundPalette.Short : FundPalette.Long, CornerRadius = new CornerRadius(2) };
                if (negative)
                {
                    half.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1 - frac, GridUnitType.Star) });
                    half.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(frac, GridUnitType.Star) });
                    Grid.SetColumn(fill, 1);
                }
                else
                {
                    half.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(frac, GridUnitType.Star) });
                    half.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1 - frac, GridUnitType.Star) });
                    Grid.SetColumn(fill, 0);
                }
                if (frac > 0) half.Children.Add(fill);
                var halfHost = new Border
                {
                    BorderBrush = FundPalette.Faint,
                    BorderThickness = negative ? new Thickness(0, 0, 1, 0) : new Thickness(1, 0, 0, 0),
                    Child = half,
                };
                Place(grid, halfHost, i, negative ? 1 : 2);
                // the axis on the other half keeps the zero line continuous
                Place(grid, new Border { BorderBrush = FundPalette.Faint, BorderThickness = negative ? new Thickness(1, 0, 0, 0) : new Thickness(0, 0, 1, 0) }, i, negative ? 2 : 1);

                var value = FundUi.Text(r.Display, 11, r.Value is double sv ? Signed(sv) : FundPalette.Faint, mono: true, wrap: false);
                value.HorizontalAlignment = HorizontalAlignment.Right;
                value.VerticalAlignment = VerticalAlignment.Center;
                value.Margin = new Thickness(8, 3, 8, 3);
                Place(grid, value, i, 3);
                if (!string.IsNullOrWhiteSpace(r.Note))
                {
                    var note = FundUi.Text(r.Note, 10, FundPalette.Faint, mono: true);
                    note.VerticalAlignment = VerticalAlignment.Center;
                    Place(grid, note, i, 4);
                }
                names.Add(r.Label + " " + r.Display + (string.IsNullOrWhiteSpace(r.Note) ? "" : " (" + r.Note + ")"));
            }
            var panel = FundUi.Panel(grid, new Thickness(12, 8, 12, 8), new Thickness(0, 2, 0, 8));
            AutomationProperties.SetName(panel, name + ": " + string.Join("; ", names));
            return panel;
        }

        /// <summary>Plain table with per-cell colours; width -1 = star column. All cells are plain text.</summary>
        private static Border Table(string[] headers, double[] widths, List<Cell[]> rows, string name)
        {
            var grid = new Grid();
            foreach (var w in widths)
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = w < 0 ? new GridLength(1, GridUnitType.Star) : new GridLength(w) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var c = 0; c < headers.Length && c < widths.Length; c++)
            {
                var h = FundUi.Text(headers[c], 10.5, FundPalette.Faint, bold: true);
                h.Margin = new Thickness(0, 0, 8, 4);
                Place(grid, h, 0, c);
            }
            for (var r = 0; r < rows.Count; r++)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                for (var c = 0; c < rows[r].Length && c < widths.Length; c++)
                {
                    var cellData = rows[r][c];
                    var cell = FundUi.Text(cellData.Text, 11, cellData.Brush ?? (c == 0 ? FundPalette.TextBright : FundPalette.Text), mono: c > 0);
                    cell.Margin = new Thickness(0, 2, 8, 2);
                    Place(grid, cell, r + 1, c);
                }
            }
            var panel = FundUi.Panel(grid, new Thickness(12, 8, 12, 8), new Thickness(0, 2, 0, 8));
            AutomationProperties.SetName(panel, name);
            return panel;
        }

        private static void Place(Grid grid, UIElement element, int row, int column)
        {
            Grid.SetRow(element, row);
            Grid.SetColumn(element, column);
            grid.Children.Add(element);
        }
    }
}

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
    // Monitoring → Portfolio insights (Strategy Platform stage 9b, SPEC §20). ONLY the Shadow Account imports; per currency,
    // NO FX conversion; the payload's label and tax notice are shown verbatim (never a second copy); empty / incomplete states
    // and every unpriced position / unused transaction come from the payload with their reasons. No polling: loaded when the
    // section is opened, on F5 and when the year / depot filter changes (the computation may read daily closes).
    internal sealed partial class MonitoringView
    {
        private InsPortfolio? _insights;
        private string? _insightsError;
        private bool _insightsLoading;
        private int? _insightsCurrentYear;          // the backend's current year (first answer without year=)
        private bool _insightsPrevious;             // year picker: false = current, true = previous
        private string? _insightsAccount;           // depot filter (null = all)
        private List<ShAccount>? _insightsDepots;
        private string? _insightsDepotsError;
        /// <summary>§20.11: ISIN → mapped EODHD listing (GET /v2/shadow/symbol-mappings, no EODHD call) for "Refresh price…".</summary>
        private Dictionary<string, string> _insightsEodhdListings = new(StringComparer.Ordinal);

        private async Task LoadInsightsAsync()
        {
            if (_insightsLoading) return;
            _insightsLoading = true;
            RenderInsights();
            try
            {
                if (_insightsDepots == null)
                {
                    var depots = await PlatformClient.GetShadowAccountsAsync();
                    if (_stopped) return;
                    if (depots.Ok && depots.Value?.Accounts != null) { _insightsDepots = depots.Value.Accounts; _insightsDepotsError = null; }
                    else _insightsDepotsError = depots.Ok ? "the backend sent no depot list" : PlatformClient.DescribeFailure(depots);
                }
                int? year = _insightsPrevious && _insightsCurrentYear is int cy ? cy - 1 : null;
                if (_insightsPrevious && _insightsCurrentYear == null)
                {
                    // the current year is not known yet: ask for the default first (the backend decides the year)
                    var first = await PlatformClient.GetInsightsPortfolioAsync(null, _insightsAccount);
                    if (_stopped) return;
                    if (first.Ok && first.Value?.Year is int fy) { _insightsCurrentYear = fy; year = fy - 1; }
                }
                var res = await PlatformClient.GetInsightsPortfolioAsync(year, _insightsAccount);
                if (_stopped) return;
                var maps = await PlatformClient.GetShadowSymbolMappingsAsync();
                if (_stopped) return;
                // an older backend without the route simply offers no refresh action (nothing is assumed)
                _insightsEodhdListings = maps.Ok && maps.Value?.Mappings != null
                    ? maps.Value.Mappings.Where(m => m.PriceSource == "eodhd" && !string.IsNullOrWhiteSpace(m.Isin) && !string.IsNullOrWhiteSpace(m.Listing))
                          .GroupBy(m => m.Isin!.ToUpperInvariant()).ToDictionary(g => g.Key, g => g.Last().Listing!, StringComparer.Ordinal)
                    : new Dictionary<string, string>(StringComparer.Ordinal);
                if (res.Ok && res.Value != null)
                {
                    _insights = res.Value;
                    _insightsError = null;
                    if (year == null && res.Value.Year is int y) _insightsCurrentYear = y;
                }
                else
                {
                    _insights = null;
                    _insightsError = !res.Ok && res.HttpStatus == 404 && PlatformClient.ErrorCode(res) == null
                        ? "the running backend has no insights routes (older than stage 9b) — restart it from the Hedge Fund window"
                        : res.Ok ? "the backend sent no payload (/v2/insights/portfolio)" : PlatformClient.DescribeFailure(res);
                }
            }
            finally
            {
                _insightsLoading = false;
            }
            RenderInsights();
        }

        // =====================================================================================
        // Rendering
        // =====================================================================================

        private void RenderInsights()
        {
            var panel = _insightsPanel;
            panel.Children.Clear();
            var p = _insights;

            // header: label + tax notice (verbatim from the payload), privacy, filters
            var head = new StackPanel();
            var title = new WrapPanel();
            title.Children.Add(FundUi.Text("Portfolio insights", 13, FundPalette.TextBright, bold: true, wrap: false));
            var local = MonUi.Chip("DATA STAYS ON THIS PC", FundPalette.Long);
            local.Margin = new Thickness(12, 2, 0, 0);
            title.Children.Add(local);
            head.Children.Add(title);
            if (p?.Label is string label && label.Length > 0)
            {
                var l = MonUi.Callout(label, FundPalette.Risk, MonUi.RiskSurface);
                head.Children.Add(l);
            }
            if (!string.IsNullOrWhiteSpace(p?.TaxNotice)) head.Children.Add(MonUi.Line(p!.TaxNotice, 10.5, FundPalette.Dim));
            if (!string.IsNullOrWhiteSpace(p?.Privacy)) head.Children.Add(MonUi.Line(p!.Privacy, 10.5, FundPalette.Long));
            head.Children.Add(MonUi.Line("Only your imported broker statements (Shadow Account) — never platform, paper or broker accounts. Amounts per currency, no FX conversion.",
                                         10.5, FundPalette.Faint));
            head.Children.Add(Filters());
            panel.Children.Add(FundUi.Panel(head, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10)));

            if (_insightsLoading) { panel.Children.Add(MonUi.Line("Computing the insights (may read daily closes)…", 11, FundPalette.Faint)); return; }
            if (_insightsError != null) { panel.Children.Add(MonUi.ErrorBlock("Portfolio insights not available (" + _insightsError + ").")); return; }
            if (p == null) { panel.Children.Add(MonUi.Line("Not loaded yet — press F5.", 11, FundPalette.Faint)); return; }

            // state
            var stateRow = new WrapPanel { Margin = new Thickness(0, 0, 0, 4) };
            var (stText, stBrush) = p.State switch
            {
                "ok" => ("COMPLETE", FundPalette.Long),
                "incomplete" => ("INCOMPLETE — SEE THE REASONS", FundPalette.Risk),
                "no_imports" => ("NO IMPORTS", FundPalette.Faint),
                null or "" => ("STATE NOT REPORTED", FundPalette.Kill),
                _ => (p.State!.ToUpperInvariant(), FundPalette.Dim),
            };
            stateRow.Children.Add(MonUi.Chip(stText, stBrush));
            var asOf = "Year " + (p.Year?.ToString(MonUi.Inv) ?? "—") + " vs " + (p.PreviousYear?.ToString(MonUi.Inv) ?? "—") + " · computed " + TradingText.Ts(p.GeneratedAt);
            var newestValuation = (p.Accounts ?? new List<InsAccount>()).SelectMany(a => a.Positions ?? new List<InsPosition>())
                .Where(x => x.PriceSource == "valuation" && !string.IsNullOrEmpty(x.AsOf)).Select(x => x.AsOf!).DefaultIfEmpty().Max();
            var newestMarket = (p.Accounts ?? new List<InsAccount>()).SelectMany(a => a.Positions ?? new List<InsPosition>())
                .Where(x => x.PriceSource == "market" && !string.IsNullOrEmpty(x.AsOf)).Select(x => x.AsOf!).DefaultIfEmpty().Max();
            var valuationAsOf = p.AsOf?.Valuation ?? newestValuation;
            var marketAsOf = p.AsOf?.Market ?? newestMarket;
            asOf += " · prices as of: valuation " + (valuationAsOf ?? "none") + ", market close " + (marketAsOf ?? "none");
            var asOfText = FundUi.Text(asOf, 10.5, FundPalette.Faint, mono: true);
            asOfText.VerticalAlignment = VerticalAlignment.Center;
            stateRow.Children.Add(asOfText);
            panel.Children.Add(stateRow);
            foreach (var r in p.Reasons ?? new List<string>()) panel.Children.Add(MonUi.Line("• " + r, 10.5, FundPalette.Risk));
            if (p.State == "no_imports")
            {
                panel.Children.Add(MonUi.Line("No broker statement imported yet — import one in Hedge Fund → Shadow Account.", 11.5, FundPalette.Dim));
                return;
            }

            var yearKey = p.Year?.ToString(MonUi.Inv);
            var prevKey = p.PreviousYear?.ToString(MonUi.Inv);
            InsYearBlock? cur = null;
            InsYearBlock? prev = null;
            if (yearKey != null) p.Totals?.Years?.TryGetValue(yearKey, out cur);
            if (prevKey != null) p.Totals?.Years?.TryGetValue(prevKey, out prev);
            panel.Children.Add(YearSection("All depots" + (_insightsAccount != null ? " (filtered)" : ""), yearKey, cur, prevKey, prev));
            panel.Children.Add(YoySection(p));
            panel.Children.Add(AllocationSection("Unrealised P&L and allocation — all depots", p.Totals?.Allocation));

            foreach (var a in p.Accounts ?? new List<InsAccount>())
                panel.Children.Add(AccountSection(a, yearKey, prevKey));

            var limits = p.Limits ?? new List<string>();
            if (limits.Count > 0)
            {
                panel.Children.Add(MonUi.Head("Limits"));
                foreach (var lim in limits) panel.Children.Add(MonUi.Line("• " + lim, 10.5, FundPalette.Dim));
            }
        }

        private UIElement Filters()
        {
            var row = new WrapPanel { Margin = new Thickness(0, 8, 0, 0) };
            row.Children.Add(new TextBlock { Text = "Year  ", Foreground = FundPalette.Faint, FontSize = 11, VerticalAlignment = VerticalAlignment.Center });
            var group = "insy" + GetHashCode().ToString(MonUi.Inv);
            var cy = _insightsCurrentYear;
            var current = TradingUi.Radio(group, "Current" + (cy is int c ? " (" + c.ToString(MonUi.Inv) + ")" : ""), "Current year", !_insightsPrevious);
            var previous = TradingUi.Radio(group, "Previous" + (cy is int c2 ? " (" + (c2 - 1).ToString(MonUi.Inv) + ")" : ""), "Previous year", _insightsPrevious);
            current.Checked += async (_, _) => { if (!_insightsPrevious) return; _insightsPrevious = false; await LoadInsightsAsync(); };
            previous.Checked += async (_, _) => { if (_insightsPrevious) return; _insightsPrevious = true; await LoadInsightsAsync(); };
            row.Children.Add(current);
            row.Children.Add(previous);
            row.Children.Add(new TextBlock { Text = "   Depot  ", Foreground = FundPalette.Faint, FontSize = 11, VerticalAlignment = VerticalAlignment.Center });
            var box = new ComboBox { FontSize = 12, MinHeight = 24, MinWidth = 220 };
            AutomationProperties.SetName(box, "Depot filter (Shadow account)");
            var all = new ComboBoxItem { Content = "All depots", Tag = "" };
            box.Items.Add(all);
            if (_insightsAccount == null) box.SelectedItem = all;
            foreach (var d in _insightsDepots ?? new List<ShAccount>())
            {
                if (string.IsNullOrEmpty(d.Id)) continue;
                var item = new ComboBoxItem { Content = d.DisplayName, Tag = d.Id };
                box.Items.Add(item);
                if (d.Id == _insightsAccount) box.SelectedItem = item;
            }
            box.SelectionChanged += async (_, _) =>
            {
                if (box.SelectedItem is not ComboBoxItem { Tag: string id }) return;
                var wanted = id.Length == 0 ? null : id;
                if (wanted == _insightsAccount) return;
                _insightsAccount = wanted;
                await LoadInsightsAsync();
            };
            row.Children.Add(box);
            if (_insightsDepotsError != null)
                row.Children.Add(MonUi.Line("  Depot list not available (" + _insightsDepotsError + ")", 10.5, FundPalette.Kill));
            var reload = MonUi.Small("↻ Recompute", "Recompute the portfolio insights (F5)");
            reload.Margin = new Thickness(12, 0, 0, 0);
            reload.Click += async (_, _) => await LoadInsightsAsync();
            row.Children.Add(reload);
            return row;
        }

        private static string Signed(double? v, string? currency)
        {
            if (v is not double d || double.IsNaN(d) || double.IsInfinity(d)) return FundFormat.Dash;
            return (d > 0 && Math.Round(d, 2) != 0 ? "+" : "") + MonUi.Money(d, currency);
        }

        private static string Plain(double? v, string? currency) => MonUi.Money(v, currency);

        /// <summary>§20.9: the bucket label from the payload's `buckets`, else the fixed default.</summary>
        private string BucketLabel(string key, string fallback)
            => _insights?.Buckets?.FirstOrDefault(b => b.Key == key)?.Label is string l && l.Length > 0 ? l : fallback;

        private Border YearSection(string title, string? yearKey, InsYearBlock? cur, string? prevKey, InsYearBlock? prev)
        {
            var s = new StackPanel();
            s.Children.Add(FundUi.Text(title, 12, FundPalette.TextBright, bold: true));
            foreach (var (key, block) in new[] { (yearKey, cur), (prevKey, prev) })
            {
                s.Children.Add(MonUi.Head("Realised P&L " + (key ?? "—")));
                var currencies = block?.Currencies ?? new List<InsYearCurrency>();
                if (block == null) { s.Children.Add(MonUi.Line("not available (the backend sent no figures for " + (key ?? "this year") + ")", 10.5, FundPalette.Risk)); continue; }
                var rows = new List<IReadOnlyList<string>>();
                foreach (var c in currencies)
                {
                    var cc = c.Currency;
                    foreach (var (name, b) in new[]
                             {
                                 (BucketLabel("brokerage", "Brokerage (stocks + ETFs)"), c.Realised?.Brokerage),
                                 (BucketLabel("other", "Other"), c.Realised?.Other),
                                 (BucketLabel("unclassified", "Unclassified"), c.Realised?.Unclassified),
                             })
                        rows.Add(new[] { cc ?? "?", name, Plain(b?.Gains, cc), Plain(b?.Losses, cc), Signed(b?.Net, cc), b?.Parts?.ToString(MonUi.Inv) ?? FundFormat.Dash });
                    rows.Add(new[] { cc ?? "?", "Net", "", "", Signed(c.Realised?.Net, cc), "" });
                }
                s.Children.Add(MonUi.Table(new[] { "Currency", "Bucket", "Gains", "Losses", "Net", "Lot parts" }, rows, "Realised profit and loss " + (key ?? ""),
                                           new double[] { 70, 230, 0, 0, 0, 70 }));
                s.Children.Add(MonUi.Head("Dividends, withholding and fees " + (key ?? "—")));
                s.Children.Add(MonUi.Table(new[] { "Currency", "Dividends", "Interest", "Withholding", "Trading fees", "Other fees", "Fees", "Taxes on sales", "Tax refunds" },
                    currencies.Select(c => (IReadOnlyList<string>)new[]
                    {
                        c.Currency ?? "?", Plain(c.Dividends, c.Currency), Plain(c.Interest, c.Currency), Plain(c.Withholding, c.Currency),
                        Plain(c.TradingFees, c.Currency), Plain(c.OtherFees, c.Currency), Plain(c.Fees, c.Currency), Plain(c.TaxesOnSales, c.Currency),
                        Plain(c.TaxRefunds, c.Currency),
                    }), "Dividends, interest, withholding, fees, taxes on sales and tax refunds " + (key ?? ""), new double[] { 70, 0, 0, 0, 0, 0, 0, 0, 0 }));
                s.Children.Add(MonUi.Line("Taxes on sales / tax refunds = as reported in the import (not computed, not tax data).", 10, FundPalette.Faint));
            }
            s.Children.Add(MonUi.Line("As given in the imported statements (withholding as the broker reported it) — nothing recomputed. Unclassified = instrument type "
                                      + "unknown, never guessed (classify it in Shadow Account → Overview).", 10, FundPalette.Faint));
            return FundUi.Panel(s, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10));
        }

        private static Border YoySection(InsPortfolio p)
        {
            var s = new StackPanel();
            s.Children.Add(FundUi.Text("Year over year " + (p.Year?.ToString(MonUi.Inv) ?? "—") + " vs " + (p.PreviousYear?.ToString(MonUi.Inv) ?? "—"), 12, FundPalette.TextBright, bold: true));
            var rows = new List<IReadOnlyList<string>>();
            foreach (var y in p.Yoy ?? new List<InsYoy>())
                foreach (var (name, d) in new[] { ("Realised net", y.RealisedNet), ("Dividends", y.Dividends), ("Withholding", y.Withholding), ("Interest", y.Interest), ("Fees", y.Fees) })
                    rows.Add(new[] { y.Currency ?? "?", name, Plain(d?.Current, y.Currency), Plain(d?.Previous, y.Currency), Signed(d?.Delta, y.Currency) });
            s.Children.Add(MonUi.Table(new[] { "Currency", "Figure", (p.Year?.ToString(MonUi.Inv) ?? "Current"), (p.PreviousYear?.ToString(MonUi.Inv) ?? "Previous"), "Change" },
                                       rows, "Year over year comparison", new double[] { 70, 120, 0, 0, 0 }));
            return FundUi.Panel(s, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10));
        }

        private static Border AllocationSection(string title, List<InsAllocation>? allocation)
        {
            var s = new StackPanel();
            s.Children.Add(FundUi.Text(title, 12, FundPalette.TextBright, bold: true));
            var list = allocation ?? new List<InsAllocation>();
            if (allocation == null) s.Children.Add(MonUi.Line("not available (the backend sent no allocation)", 10.5, FundPalette.Risk));
            else if (list.Count == 0) s.Children.Add(MonUi.Line("No open positions.", 10.5, FundPalette.Dim));
            foreach (var a in list)
            {
                // the coverage % first and large: how much of the open cost basis is actually valued
                var cov = a.CoveragePct is double c ? c.ToString("0.#", MonUi.Inv) + " %" : FundFormat.Dash;
                var nothingValued = a.Valued == 0 && (a.Positions ?? 0) > 0;   // §20.7: an explicit "nothing valued (reason)"
                var bigText = nothingValued
                    ? (a.Currency ?? "?") + " · nothing valued (" + MonUi.Int(a.Positions) + " position(s) without a price source)"
                    : (a.Currency ?? "?") + " · coverage " + cov;
                var big = FundUi.Text(bigText, 15, !nothingValued && a.CoveragePct is double cv && cv >= 99.95 ? FundPalette.Long : FundPalette.Risk, bold: true, mono: true);
                big.TextWrapping = TextWrapping.Wrap;
                big.Margin = new Thickness(0, 8, 0, 2);
                AutomationProperties.SetName(big, nothingValued ? bigText : (a.Currency ?? "?") + ": coverage " + cov + " of the open cost basis is valued");
                s.Children.Add(big);
                var k = a.Concentration;
                s.Children.Add(MonUi.Pairs(new[]
                {
                    ("Market value (valued positions)", Plain(a.MarketValue, a.Currency)),
                    ("Unrealised P&L (valued positions)", Signed(a.Unrealised, a.Currency)),
                    ("Day change (market prices only)", a.DayChange is double dc ? Signed(dc, a.Currency) : "not available (no market prices)"),
                    ("Cost valued / total open cost", Plain(a.CostValued, a.Currency) + " / " + Plain(a.CostTotal, a.Currency)),
                    ("Positions valued", MonUi.Int(a.Valued) + " of " + MonUi.Int(a.Positions)),
                    ("Concentration", k == null ? "not available" : "HHI " + MonUi.Num(k.Hhi, 3) + " · effective N " + MonUi.Num(k.EffectiveN, 1)
                                                                    + " · largest " + FundFormat.Pct(k.Top1Pct, 1) + " · top 3 " + FundFormat.Pct(k.Top3Pct, 1)),
                }));
            }
            s.Children.Add(MonUi.Line("Weights and concentration over the valued positions only; never summed across currencies.", 10, FundPalette.Faint));
            return FundUi.Panel(s, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10));
        }

        private UIElement AccountSection(InsAccount a, string? yearKey, string? prevKey)
        {
            var s = new StackPanel();
            s.Children.Add(FundUi.Text((a.Label ?? "(no label)") + " · " + (a.AccountId ?? "?"), 12.5, FundPalette.TextBright, bold: true));
            var chips = new WrapPanel();
            chips.Children.Add(MonUi.Chip(a.State switch
                {
                    "ok" => "COMPLETE",
                    "incomplete" => "INCOMPLETE",
                    "no_imports" => "NO TRANSACTIONS IMPORTED",
                    null or "" => "STATE NOT REPORTED",
                    _ => a.State!.ToUpperInvariant(),
                }, a.State == "ok" ? FundPalette.Long : a.State == "no_imports" ? FundPalette.Faint : FundPalette.Risk));
            if ((a.Templates?.Count ?? 0) > 0) chips.Children.Add(MonUi.Chip("FROM " + string.Join(", ", a.Templates!).ToUpperInvariant(), FundPalette.Dim));
            s.Children.Add(chips);
            foreach (var r in a.Reasons ?? new List<string>()) s.Children.Add(MonUi.Line("• " + r, 10.5, FundPalette.Risk));

            InsYearBlock? cur = null;
            InsYearBlock? prev = null;
            if (yearKey != null) a.Years?.TryGetValue(yearKey, out cur);
            if (prevKey != null) a.Years?.TryGetValue(prevKey, out prev);
            s.Children.Add(YearSection("Figures of this depot", yearKey, cur, prevKey, prev));
            s.Children.Add(AllocationSection("Unrealised P&L and allocation — this depot", a.Allocation));

            var positions = a.Positions ?? new List<InsPosition>();
            s.Children.Add(MonUi.Head("Open positions (" + positions.Count.ToString(MonUi.Inv) + ")"));
            // live click-test 2026-10-18: the header counted every open position but the table listed only valued ones ("(none)" with
            // 13 positions). Every open position is listed now; an unpriced one shows "not valued" (reasons in the sections below).
            s.Children.Add(MonUi.Table(new[] { "Instrument", "Class", "Ccy", "Qty", "Cost", "Price (source, as of)", "Value", "Unrealised", "Weight" },
                positions.Select(x => x.PriceSource == "none"
                    ? (IReadOnlyList<string>)new[]
                    {
                        (x.Key ?? "?") + (string.IsNullOrWhiteSpace(x.Name) ? "" : " · " + x.Name), x.AssetClass ?? "unclassified", x.Currency ?? "—", FundFormat.Qty(x.Qty),
                        Plain(x.Cost, null), "not valued — see below", FundFormat.Dash, FundFormat.Dash, FundFormat.Dash,
                    }
                    : x.PriceSource == "market_other_currency"
                    // §20.11: valued only in the LISTING currency — value there, no P&L, never converted
                    ? new[]
                    {
                        (x.Key ?? "?") + (string.IsNullOrWhiteSpace(x.Name) ? "" : " · " + x.Name), x.AssetClass ?? "unclassified", x.Currency ?? "—", FundFormat.Qty(x.Qty),
                        Plain(x.Cost, null), MonUi.Num(x.Listing?.Price, 4) + " " + (x.Listing?.Currency ?? "") + " (" + PriceSourceText(x) + ", " + (x.Listing?.AsOf ?? x.AsOf ?? "—") + ")",
                        Plain(x.Listing?.Value, x.Listing?.Currency) + " (other currency)", "— (different currencies — not converted)", FundFormat.Dash,
                    }
                    : new[]
                    {
                        (x.Key ?? "?") + (string.IsNullOrWhiteSpace(x.Name) ? "" : " · " + x.Name), x.AssetClass ?? "unclassified", x.Currency ?? "—", FundFormat.Qty(x.Qty),
                        Plain(x.Cost, null), MonUi.Num(x.Price, 4) + " (" + PriceSourceText(x) + ", " + (x.AsOf ?? "—") + ")", Plain(x.Value, null),
                        Signed(x.Unrealised, null) + (x.UnrealisedPct is double up ? " (" + FundFormat.Pct(up, 1, signed: true) + ")" : ""),
                        x.Weight is double w ? FundFormat.Fraction(w, 1) : FundFormat.Dash,
                    }), "Open positions", new double[] { 0, 70, 40, 70, 90, 170, 90, 120, 60 }));
            // §20.7: positions without a price, split into broker positions without imported lots (cost null), positions whose
            // broker quantity differs from the imported lots, and the rest — every note verbatim
            var otherCurrency = positions.Where(x => x.PriceSource == "market_other_currency").ToList();
            if (otherCurrency.Count > 0)
            {
                s.Children.Add(MonUi.Head("Valued only in another currency (" + otherCurrency.Count.ToString(MonUi.Inv) + ") — no P&L, not converted"));
                foreach (var x in otherCurrency)
                    s.Children.Add(MonUi.Line("• " + (x.Key ?? "?") + (string.IsNullOrWhiteSpace(x.Name) ? "" : " · " + x.Name) + " — " + PriceSourceText(x)
                                              + (string.IsNullOrWhiteSpace(x.Note) ? "" : " · " + x.Note), 10.5, FundPalette.Dim));
            }
            var unpriced = positions.Where(x => x.PriceSource == "none").ToList();
            var noLots = unpriced.Where(x => x.Cost == null && x.BrokerQty != null).ToList();
            var mismatch = unpriced.Where(x => x.Cost != null && x.BrokerQty != null).ToList();
            var otherUnpriced = unpriced.Where(x => x.BrokerQty == null).ToList();
            if (mismatch.Count > 0)
            {
                s.Children.Add(MonUi.Head("Broker quantity differs from the imported lots (" + mismatch.Count.ToString(MonUi.Inv) + ") — not valued"));
                foreach (var x in mismatch)
                    s.Children.Add(MonUi.Line("• " + (x.Key ?? "?") + (string.IsNullOrWhiteSpace(x.Name) ? "" : " · " + x.Name) + " · lots " + FundFormat.Qty(x.Qty)
                                              + " · broker " + FundFormat.Qty(x.BrokerQty) + (x.AsOf != null ? " (" + x.AsOf + ")" : "") + " — "
                                              + (x.Note ?? "not available (no price source)"), 10.5, FundPalette.Risk));
            }
            if (noLots.Count > 0)
            {
                s.Children.Add(MonUi.Head("Broker positions without imported lots (" + noLots.Count.ToString(MonUi.Inv) + ") — not valued"));
                foreach (var x in noLots)
                    s.Children.Add(MonUi.Line("• " + (x.Key ?? "?") + (string.IsNullOrWhiteSpace(x.Name) ? "" : " · " + x.Name) + " · broker " + FundFormat.Qty(x.BrokerQty)
                                              + (x.AsOf != null ? " (" + x.AsOf + ")" : "") + " · no cost basis — " + (x.Note ?? "not available (no price source)"), 10.5, FundPalette.Risk));
                s.Children.Add(MonUi.Line("Import the older statements so these positions have purchases (lots).", 10, FundPalette.Faint));
            }
            if (otherUnpriced.Count > 0)
            {
                s.Children.Add(MonUi.Head("Not valued — reasons (" + otherUnpriced.Count.ToString(MonUi.Inv) + ")"));
                foreach (var x in otherUnpriced)
                {
                    var line = MonUi.Line("• " + (x.Key ?? "?") + (string.IsNullOrWhiteSpace(x.Name) ? "" : " · " + x.Name) + " · " + FundFormat.Qty(x.Qty) + " · cost "
                                          + Plain(x.Cost, x.Currency) + " — " + (x.Note ?? "not available (no price source)"), 10.5, FundPalette.Risk);
                    var isin = IsinOf(x);
                    // live retest 2026-10-18: issuer derivatives and bonds have NO price source (§20.11) — a mapping cannot help, so
                    // their row offers "Set valuation price…" instead of "Map to a listing…"
                    var manualOnly = IsManualOnly(x);
                    if (manualOnly && !string.IsNullOrWhiteSpace(a.AccountId))
                    {
                        var vrow = new Grid { Margin = new Thickness(0, 1, 0, 1) };
                        vrow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                        vrow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                        vrow.Children.Add(line);
                        var accountId = a.AccountId!;
                        var val = MonUi.Small("Set valuation price…", "Set your own valuation price for " + (x.Key ?? "this position") + " (no price source exists for it)");
                        val.Margin = new Thickness(8, 0, 0, 0);
                        val.VerticalAlignment = VerticalAlignment.Top;
                        val.Click += async (_, _) => await SetValuationFromInsightsAsync(accountId);
                        Grid.SetColumn(val, 1);
                        vrow.Children.Add(val);
                        s.Children.Add(vrow);
                        continue;
                    }
                    if (isin == null || manualOnly) { s.Children.Add(line); continue; }
                    // §20.10/§20.11: "Map to a listing…" — the user picks the listing (EODHD candidates or a US ticker); nothing is suggested
                    var row = new Grid { Margin = new Thickness(0, 1, 0, 1) };
                    row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                    row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                    row.Children.Add(line);
                    var target = x;
                    var map = MonUi.Small("Map to a listing…", "Map " + isin + " to a listing you pick (EODHD listing or US ticker)");
                    map.Margin = new Thickness(8, 0, 0, 0);
                    map.VerticalAlignment = VerticalAlignment.Top;
                    map.Click += async (_, _) => await MapPositionAsync(isin, target);
                    var actions = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Top };
                    // §20.11 review fixes: a mapped EODHD listing whose fetch failed today stays cached until tomorrow — refresh costs 1 call
                    if (_insightsEodhdListings.TryGetValue(isin, out var eodListing))
                    {
                        var rf = MonUi.Small(EodhdRefreshFlow.ActionText, "Refresh the EODHD price of " + eodListing + ", uses one of today's EODHD calls");
                        rf.Margin = new Thickness(8, 0, 0, 0);
                        rf.Click += async (_, _) => { if (await EodhdRefreshFlow.RunAsync(Owner, eodListing)) await LoadInsightsAsync(); };
                        actions.Children.Add(rf);
                    }
                    actions.Children.Add(map);
                    Grid.SetColumn(actions, 1);
                    row.Children.Add(actions);
                    s.Children.Add(row);
                }
                s.Children.Add(MonUi.Line("Map the ISIN to a listing (button, or Shadow Account → Overview → Symbol mapping…) or add a price in Set valuation prices… (never a stand-in).",
                                          10, FundPalette.Faint));
            }
            // §20.9: pending private-market orders (pre-payments, not positions) and corporate actions without their partner row
            var pending = a.Pending ?? new List<InsPending>();
            if (pending.Count > 0)
            {
                s.Children.Add(MonUi.Head("Pending private-market orders (" + pending.Count.ToString(MonUi.Inv) + ") — not positions"));
                foreach (var pd in pending)
                    s.Children.Add(MonUi.Line("• " + (pd.Date ?? "—") + " · " + (pd.Key ?? "?") + (string.IsNullOrWhiteSpace(pd.Name) ? "" : " · " + pd.Name) + " · "
                                              + Plain(pd.Amount, pd.Currency) + (pd.Fee is double pf && pf != 0 ? " (fee " + Plain(pf, pd.Currency) + ")" : "")
                                              + " — " + (pd.Note ?? "pending private-market order (pre-payment) — not a position"), 10.5, FundPalette.Dim));
            }
            var unpaired = a.UnpairedCorporateActions ?? new List<InsUnpaired>();
            if (unpaired.Count > 0)
            {
                s.Children.Add(MonUi.Head("Corporate actions without their partner row (" + unpaired.Count.ToString(MonUi.Inv) + ")"));
                foreach (var u in unpaired)
                    s.Children.Add(MonUi.Line("• " + (u.Date ?? "—") + " · " + (u.Key ?? "?") + " · " + FundFormat.Qty(u.Qty) + " — " + (u.Note ?? "reason not reported")
                                              + (string.IsNullOrWhiteSpace(u.ExtId) ? "" : " (id " + u.ExtId + ")"), 10.5, FundPalette.Risk));
                s.Children.Add(MonUi.Line("Never guessed: the position stays open until a later export brings the partner row.", 10, FundPalette.Faint));
            }
            var notUsed = a.NotUsed ?? new List<InsNotUsed>();
            var rejected = a.RejectedRows ?? new List<InsRejected>();
            if (notUsed.Count > 0 || rejected.Count > 0)
            {
                s.Children.Add(MonUi.Head("Transactions not used"));
                foreach (var n in notUsed)
                    s.Children.Add(MonUi.Line("• " + (n.Date ?? "—") + " · " + (n.Key ?? "?") + " · " + FundFormat.Qty(n.Qty) + " — " + (n.Reason ?? "reason not reported"), 10.5, FundPalette.Risk));
                foreach (var r in rejected)
                    s.Children.Add(MonUi.Line("• " + (r.FileName ?? r.ImportId ?? "?") + ": " + MonUi.Int(r.Rejected) + " row(s) rejected by the importer (see the import preview)", 10.5, FundPalette.Risk));
            }
            var card = FundUi.Panel(s, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10));
            AutomationProperties.SetName(card, "Depot " + (a.Label ?? a.AccountId ?? "?"));
            return card;
        }

        private static readonly System.Text.RegularExpressions.Regex IsinLike = new("^[A-Z]{2}[A-Z0-9]{9}[0-9]$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);

        private static string? IsinOf(InsPosition x)
        {
            var v = (x.Isin ?? x.Key ?? "").Trim().ToUpperInvariant();
            return IsinLike.IsMatch(v) ? v : null;
        }

        /// <summary>§20.11: issuer derivatives (turbos / calls) and bonds have no price source — only a valuation helps.</summary>
        private static bool IsManualOnly(InsPosition x)
            => string.Equals(x.AssetClass, "derivative", StringComparison.OrdinalIgnoreCase) || string.Equals(x.AssetClass, "bond", StringComparison.OrdinalIgnoreCase);

        /// <summary>Opens the account's valuation dialog (GET the account for its positions + current valuation; errors shown).</summary>
        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        private Task SetValuationFromInsightsAsync(string accountId) => DialogGuard.RunAsync("shadow-valuation:" + accountId, () => SetValuationFromInsightsCoreAsync(accountId));

        private async Task SetValuationFromInsightsCoreAsync(string accountId)
        {
            var res = await PlatformClient.GetShadowAccountAsync(accountId);
            if (_stopped) return;
            if (!res.Ok || res.Value == null)
            {
                FundDialogs.Info(Owner, "Set valuation price", "The account could not be loaded: " + (res.Ok ? "empty response." : PlatformClient.DescribeFailure(res)));
                return;
            }
            var dialog = new ShadowValuationDialog(Owner, accountId, res.Value.Positions ?? new List<ShPosition>(), res.Value.Valuation);
            if (dialog.ShowDialog() != true) return;
            await LoadInsightsAsync();
        }

        private async Task MapPositionAsync(string isin, InsPosition x)
        {
            var dialog = new ShadowSymbolMapDialog(Owner, isin, x.Name, x.Currency);
            if (dialog.ShowDialog() != true) return;
            await LoadInsightsAsync();
        }

        /// <summary>§20.10/§20.11: the backend's source text (price_detail) verbatim; the older wording only when it sends none.</summary>
        private static string PriceSourceText(InsPosition x) => !string.IsNullOrWhiteSpace(x.PriceDetail) ? x.PriceDetail! : x.PriceSource switch
        {
            "valuation" => "valuation" + (x.ValuationSource == "import" ? " from the import" : x.ValuationSource == "user" ? " by you" : ""),
            "market" => "market close",
            null or "" => "source not reported",
            _ => x.PriceSource!,
        };
    }
}

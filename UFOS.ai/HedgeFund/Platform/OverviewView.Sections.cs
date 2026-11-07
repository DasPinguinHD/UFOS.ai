using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Ui;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>Stage 11 Overview — rendering of each section from the loaded data (OverviewView.cs). Plain text only.</summary>
    internal sealed partial class OverviewView
    {
        private const string NotLoadedYet = "not loaded yet";

        private void RenderAll()
        {
            SafeRender(RenderAttention);
            SafeRender(RenderMarkets);
            SafeRender(RenderOptions);
            SafeRender(RenderAccount);
            SafeRender(RenderRisk);
            SafeRender(RenderEquityChart);
            SafeRender(RenderTiles);
            SafeRender(RenderActivity);
            if (!_activated)
                foreach (var r in new[] { _loopRow, _validationRow, _archiveRow, _aiRow, _connRow }) r.Set(NotLoadedYet);
        }

        // =====================================================================================
        // Attention strip
        // =====================================================================================

        private void RenderAttention()
        {
            if (_stopped) return;
            var items = new List<AttentionItem>();
            var unavailable = new List<string>();

            // old engine (only while it is not replaced by the Council switch-over): pending orders, halt
            if (_fundSeen && !_fundReplaced && IsLegacyMode() == true)
            {
                if (!_fundConnected || _fund == null) unavailable.Add("old engine (not reachable)");
                else
                {
                    var n = _fund.PendingOrders?.Count ?? 0;
                    if (n > 0)
                        items.Add(new AttentionItem(n == 1 ? "1 old-engine order waiting" : n + " old-engine orders waiting",
                            () => Navigate(OverviewTargets.Legacy), "Pending orders of the old engine: open Trading, Legacy"));
                    if (_fund.Halted?.Active == true)
                        items.Add(new AttentionItem("Old engine halted", () => Navigate(OverviewTargets.Legacy), "The old engine is halted: open Trading, Legacy"));
                }
            }

            // platform: working orders, halted accounts
            if (_trading != null)
            {
                var accounts = _trading.Accounts ?? new List<TradingAccountSummary>();
                var working = accounts.Sum(a => a.WorkingOrders ?? 0);
                if (working > 0)
                    items.Add(new AttentionItem(working == 1 ? "1 platform order working" : working + " platform orders working",
                        () => Navigate(OverviewTargets.Orders), "Working platform orders: open Portfolio and Trading"));
                var halted = accounts.Count(a => a.Halted);
                if (halted > 0)
                    items.Add(new AttentionItem(halted == 1 ? "1 platform account halted" : halted + " platform accounts halted",
                        () => Navigate(OverviewTargets.Trading), "Halted platform accounts: open Portfolio and Trading"));
            }
            else if (_tradingProblem != null) unavailable.Add("platform accounts (" + _tradingProblem + ")");

            // migration / Council switch-over still open
            var migrationOpen = _migration != null && _migration.LegacyAccount?.Status != "migrated";
            var switchoverOpen = _council != null && _council.Switchover != "done";
            if (migrationOpen || switchoverOpen)
                items.Add(new AttentionItem("Migration & Council switch-over pending", () => Navigate(OverviewTargets.Legacy),
                    "Migration and Council switch-over pending: open Trading, Legacy"));
            if (_migration == null && _migrationProblem != null) unavailable.Add("migration (" + _migrationProblem + ")");
            if (_council == null && _councilProblem != null) unavailable.Add("Council (" + _councilProblem + ")");

            // monitoring alerts not yet seen (app-wide notifier)
            var unacked = MonitoringNotifier.Unacked;
            if (unacked is int u && u > 0)
                items.Add(new AttentionItem(u == 1 ? "1 unseen alert" : u + " unseen alerts", () => Navigate(OverviewTargets.Alerts),
                    "Unseen alerts: open Monitoring, Alerts"));
            else if (unacked == null)
                unavailable.Add("alerts (" + (string.IsNullOrWhiteSpace(MonitoringNotifier.PollProblem) ? "count not read yet" : HfUi.Plain(MonitoringNotifier.PollProblem, 160)) + ")");

            _attention.SetItems(items, unavailable);
        }

        // =====================================================================================
        // Market band
        // =====================================================================================

        private void RenderMarkets()
        {
            var items = _markets?.Items;
            if (items == null || items.Count == 0)
            {
                _tickerRow.Visibility = Visibility.Collapsed;
                _marketProblem.Visibility = Visibility.Visible;
                HfUi.SetText(_marketProblem, "Market data: " + HfUi.NotAvailable(_markets == null ? (_marketsProblem ?? NotLoadedYet) : "the backend sent no items"));
                return;
            }
            _marketProblem.Visibility = Visibility.Collapsed;
            _tickerRow.Visibility = Visibility.Visible;
            while (_cells.Count < items.Count)
            {
                var cell = new TickerCell();
                _cells.Add(cell);
                _tickerRow.Children.Add(cell);
            }
            while (_cells.Count > items.Count)
            {
                var last = _cells[_cells.Count - 1];
                _cells.RemoveAt(_cells.Count - 1);
                _tickerRow.Children.Remove(last);
            }
            for (var i = 0; i < items.Count; i++) RenderTicker(_cells[i], items[i]);
            for (var i = 0; i < _cells.Count; i++)
                _cells[i].BorderThickness = new Thickness(0, 0, (i + 1) % _tickerRow.Columns == 0 ? 0 : 1, 0);
        }

        private static void RenderTicker(TickerCell cell, OverviewMarketItem it)
        {
            var label = HfUi.Plain(it.Label ?? it.Key ?? "?", 40);
            var source = HfUi.Plain(it.Source, 30);
            if (string.IsNullOrEmpty(source)) source = "source not stated";
            var provisional = it.Provisional == true ? " · provisional" : "";
            string footer;
            if (it.Session == "daily")
                footer = source + " · close " + DateOf(it.AsOf) + (it.SlotAgeMinutes is double sa ? " · slot " + HfUi.Age(sa) : "") + provisional;
            else footer = source + " · " + (it.Session == "closed" ? "closed · " : "") + HfUi.Age(it.AgeMinutes) + provisional;
            if (it.Available != true || it.Value is not double v || double.IsNaN(v) || double.IsInfinity(v))
            {
                cell.SetUnavailable(label, it.Available == true ? "no value in the answer" : it.Reason, source);
                return;
            }
            var key = (it.Key ?? "").ToUpperInvariant();
            var value = key switch
            {
                "US10Y" => v.ToString("0.00", CultureInfo.InvariantCulture) + " %",
                "EURUSD" => v.ToString("0.0000", CultureInfo.InvariantCulture),
                "VIX" => v.ToString("0.00", CultureInfo.InvariantCulture),
                _ => "$" + v.ToString("#,##0.00", CultureInfo.InvariantCulture),
            };
            string change;
            string? changeNote = null;
            if (it.Change is double c && !double.IsNaN(c) && !double.IsInfinity(c))
            {
                change = (it.ChangeUnit ?? "") switch
                {
                    "bp" => HfUi.Signed(c, 1, " bp"),
                    "pt" => HfUi.Signed(c, 2, " pt"),
                    "pct" => HfUi.Signed(c, 2, " %"),
                    _ => HfUi.Signed(c, 2, string.IsNullOrEmpty(it.ChangeUnit) ? "" : " " + HfUi.Plain(it.ChangeUnit, 8)),
                };
                if (key == "BTC") change += " 24h";   // contract §1.1: BTC change is over 24 h — said on the tile
            }
            else
            {
                change = "change not available";
                changeNote = "change " + HfUi.NotAvailable(string.IsNullOrWhiteSpace(it.Note) ? it.Reason : it.Note);
            }
            var series = it.Series?.Where(p => p.V is double pv && !double.IsNaN(pv) && !double.IsInfinity(pv)).Select(p => p.V!.Value).ToList();
            string? seriesNote = null;
            if (series == null || series.Count < 2)
                seriesNote = "chart " + HfUi.NotAvailable(string.IsNullOrWhiteSpace(it.SeriesReason) ? "no intraday series" : it.SeriesReason);
            var tip = new List<string> { label + " " + value + " (" + footer + ")" };
            if (!string.IsNullOrWhiteSpace(it.AsOf)) tip.Add("as of " + it.AsOf);
            if (changeNote != null) tip.Add(changeNote);
            if (seriesNote != null) tip.Add(seriesNote);
            if (!string.IsNullOrWhiteSpace(it.Note)) tip.Add(HfUi.Plain(it.Note, 300));
            cell.SetData(label, change, value, series, footer, string.Join("\n", tip), seriesNote);
        }

        /// <summary>The date part of an ISO timestamp ("2026-11-05"), or "date unknown".</summary>
        private static string DateOf(string? iso)
        {
            if (string.IsNullOrWhiteSpace(iso)) return "date unknown";
            var s = iso.Trim();
            return s.Length >= 10 && DateTime.TryParseExact(s.Substring(0, 10), "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out _)
                ? s.Substring(0, 10) : HfUi.Plain(s, 30);
        }

        // =====================================================================================
        // Options structure (OS2 archive; provisional shown)
        // =====================================================================================

        private void RenderOptions()
        {
            // header: provisional · slot age (SPY state)
            if (_spyState != null && !string.IsNullOrEmpty(_spyState.Slot))
                HfUi.SetText(_osMeta, (_spyState.Provisional == true ? "provisional · " : "") + "slot " + FundFormat.LocalTime(_spyState.Slot, "dd MMM HH:mm")
                                      + " · " + HfUi.Age(_spyState.AgeMinutes) + " old");
            else HfUi.SetText(_osMeta, _spyState?.Provisional == true ? "provisional" : "");
            RenderGex();
            RenderQqq();
            RenderExpectedMove();
            RenderVix();
        }

        /// <summary>Why the SPY parts cannot be shown (state missing / not available), or null.</summary>
        private string? SpyBlocked()
        {
            if (_spyState == null) return _spyStateProblem ?? NotLoadedYet;
            if (_spyState.Available != true) return _spyState.Reason ?? OsText.Reasons(_spyState.Reasons) ?? "the state is not available";
            return null;
        }

        private void RenderGex()
        {
            var blocked = SpyBlocked() ?? (_gex?.Gex == null ? (_gexProblem ?? "no GEX payload") : null);
            if (blocked != null)
            {
                HfUi.SetText(_gexRegime, "");
                HfUi.SetDot(_gexDot, DotKind.None);
                _gexChart.SetData(Array.Empty<(double, double)>(), null, null);
                _gexChart.Visibility = Visibility.Collapsed;
                _gexProblemText.Visibility = Visibility.Visible;
                HfUi.SetText(_gexProblemText, HfUi.NotAvailable(blocked));
                HfUi.SetText(_gexCaption, "");
                return;
            }
            var reg = _spyState!.Regime;
            var g = _gex!.Gex!;
            _gexChart.Visibility = Visibility.Visible;
            _gexProblemText.Visibility = Visibility.Collapsed;

            // regime label + dot from /state (variant A), blue / orange only here
            var word = OsText.SignWord(reg?.GexSign);
            HfUi.SetText(_gexRegime, word ?? "regime not available");
            HfUi.SetDot(_gexDot, reg?.GexSign is double s ? (s > 0 ? DotKind.GammaPositive : s < 0 ? DotKind.GammaNegative : DotKind.Neutral) : DotKind.None);

            // bars: variant A per strike; flip / spot lines from /state (one source per value)
            var spot = reg?.Spot;
            var flip = reg?.Flip;
            var bars = (g.PerStrike ?? new List<OsStrike>())
                .Where(p => p.Strike is double && p.GexA is double)
                .Select(p => (Strike: p.Strike!.Value, Value: p.GexA!.Value)).ToList();
            var windowNote = "";
            if (spot is double sp && sp > 0)
            {
                // display window: strikes within ±5 % of spot, widened to the flip when it lies within ±15 %
                var lo = sp * 0.95;
                var hi = sp * 1.05;
                if (flip is double f && f > sp * 0.85 && f < sp * 1.15) { lo = Math.Min(lo, f * 0.995); hi = Math.Max(hi, f * 1.005); }
                var shown = bars.Where(b => b.Strike >= lo && b.Strike <= hi).ToList();
                if (shown.Count < bars.Count) windowNote = " · strikes " + ChartText.Num(lo) + "–" + ChartText.Num(hi) + " shown";
                bars = shown;
            }
            _gexChart.SetData(bars, flip, spot, "no strike with GEX under variant A in the display window");
            AutomationProperties.SetName(_gexChart, "SPY gamma exposure by strike, variant A, " + bars.Count + " strikes");

            var parts = new List<string> { "variant A" };
            if (flip is double fl && reg?.FlipDistancePct is double dist)
                parts.Add("Flip " + ChartText.Num(Math.Round(fl, 2)) + " (" + Math.Abs(dist).ToString("0.0", CultureInfo.InvariantCulture) + " % " + (dist < 0 ? "below" : "above") + " spot)");
            else
            {
                var fr = g.Flip != null && g.Flip.TryGetValue("A", out var fa) ? fa?.Reason : null;
                parts.Add("Flip " + HfUi.NotAvailable(fr ?? "no flip in the state"));
            }
            parts.Add(AgreeText(reg));
            var status = _gex.Status ?? g.Status;
            if (status == "partial") parts.Add("partial: " + (OsText.Reasons(_gex.Reasons) ?? "see the OS2 quality view"));
            if (_gex.Provisional == true) parts.Add("provisional");
            HfUi.SetText(_gexCaption, string.Join(" · ", parts) + windowNote);
        }

        /// <summary>"variants A/B/C k/3 agree" from the state's signs (k = variants with variant A's sign).</summary>
        private static string AgreeText(OsRegime? reg)
        {
            var signs = reg?.Signs;
            if (signs == null || !signs.TryGetValue("A", out var a) || a is not double av) return "variant agreement " + HfUi.NotAvailable("no signs in the state");
            var k = new[] { "A", "B", "C" }.Count(v => signs.TryGetValue(v, out var x) && x is double xv && Math.Sign(xv) == Math.Sign(av));
            return "variants A/B/C " + k + "/3 agree";
        }

        private string? _qqqSignature;

        private void RenderQqq()
        {
            var st = _qqqState;
            var sig = (_qqqStateProblem ?? "") + "|" + st?.Available + "|" + st?.Reason + "|" + OsText.Reasons(st?.Reasons) + "|" + st?.Regime?.Agree + "|"
                      + string.Join(",", (st?.Regime?.Signs ?? new Dictionary<string, double?>()).Select(kv => kv.Key + "=" + kv.Value)) + "|" + st?.Provisional
                      + "|" + (st?.Provisional == true ? HfUi.Age(st.AgeMinutes) : "");   // the age is shown only with "provisional"
            if (sig == _qqqSignature) return;   // unchanged: keep the rows (and the keyboard focus)
            _qqqSignature = sig;
            _qqqRows.Children.Clear();
            string? blocked = _qqqState == null ? (_qqqStateProblem ?? NotLoadedYet)
                : _qqqState.Available != true ? (_qqqState.Reason ?? OsText.Reasons(_qqqState.Reasons) ?? "the state is not available") : null;
            var signs = _qqqState?.Regime?.Signs;
            if (blocked == null && signs == null) blocked = OsText.Reasons(_qqqState?.Reasons) ?? "no variant signs in the state";
            if (blocked != null)
            {
                HfUi.SetText(_qqqAgree, "");
                HfUi.SetDot(_qqqDot, DotKind.None);
                _qqqRows.Children.Add(HfUi.Text(HfUi.NotAvailable(blocked), 12, HfTokens.TextDim, wrap: true));
                return;
            }
            var agree = _qqqState!.Regime?.Agree;
            HfUi.SetText(_qqqAgree, agree == true ? "agree" : agree == false ? "disagree" : "agreement unknown", agree == false ? HfTokens.Attention : HfTokens.Text);
            HfUi.SetDot(_qqqDot, agree == false ? DotKind.Warning : DotKind.Neutral);
            // variant definitions: backend/strategy_platform/options_structure/gex.py
            foreach (var (key, text) in new[] { ("A", "Variant A · long calls / short puts"), ("B", "Variant B · short all options"), ("C", "Variant C · moneyness split") })
            {
                double? sign = signs!.TryGetValue(key, out var sv) ? sv : null;
                var word = OsText.SignWord(sign);
                var brush = sign is double x ? (x > 0 ? HfTokens.GammaPositive : x < 0 ? HfTokens.GammaNegative : HfTokens.Text) : HfTokens.TextDim;
                var row = new KeyValueRow(text, word ?? HfUi.NotAvailable("no sign"), divider: true);
                row.Set(word ?? HfUi.NotAvailable("no sign"), null, DotKind.None, brush);
                _qqqRows.Children.Add(row);
            }
            if (agree == false)
            {
                var note = HfUi.Text("no regime shown while variants disagree", 12, HfTokens.TextDim, wrap: true);
                note.Margin = new Thickness(0, 6, 0, 0);
                _qqqRows.Children.Add(note);
            }
            if (_qqqState.Provisional == true)
            {
                var prov = HfUi.Text("provisional · " + HfUi.Age(_qqqState.AgeMinutes) + " old", 11.5, HfTokens.TextDim);
                prov.Margin = new Thickness(0, 6, 0, 0);
                _qqqRows.Children.Add(prov);
            }
        }

        private void RenderExpectedMove()
        {
            var blocked = SpyBlocked() ?? (_surface?.Surface == null ? (_surfaceProblem ?? "no surface payload") : null);
            RenderNextExpiry();
            if (blocked != null)
            {
                HfUi.SetText(_emTitle, "Expected move · SPY");
                HfUi.SetText(_emValue, "");
                _emValue.Visibility = Visibility.Collapsed;
                _emBar.SetData(null, null, null);
                _emBar.Visibility = Visibility.Collapsed;
                HfUi.SetText(_emCaption, HfUi.NotAvailable(blocked));
                return;
            }
            var moves = _surface!.Surface!.ExpectedMove;
            var em = moves?.Today;
            var which = "today";
            if (em == null && moves?.Next != null) { em = moves.Next; which = "next expiry"; }
            var spot = _spyState!.Regime?.Spot;
            HfUi.SetText(_emTitle, "Expected move · SPY " + which);
            if (em == null || em.Straddle is not double straddle || spot is not double s || s <= 0)
            {
                var why = em == null ? "no expected move in the surface"
                    : em.Straddle == null ? (OsText.Reasons(em.Reasons) ?? "no ATM straddle")
                    : "no spot in the state";
                _emValue.Visibility = Visibility.Collapsed;
                _emBar.Visibility = Visibility.Collapsed;
                HfUi.SetText(_emCaption, HfUi.NotAvailable(why));
                return;
            }
            _emValue.Visibility = Visibility.Visible;
            _emBar.Visibility = Visibility.Visible;
            HfUi.SetText(_emValue, "±" + (straddle / s * 100).ToString("0.00", CultureInfo.InvariantCulture) + " %");
            _emBar.SetData(s - straddle, s, s + straddle, x => "$" + x.ToString("#,##0.00", CultureInfo.InvariantCulture));
            AutomationProperties.SetName(_emBar, "Expected move range " + ChartText.Num(Math.Round(s - straddle, 2)) + " to " + ChartText.Num(Math.Round(s + straddle, 2)) + ", spot " + ChartText.Num(Math.Round(s, 2)));
            HfUi.SetText(_emCaption, "ATM straddle $" + straddle.ToString("0.00", CultureInfo.InvariantCulture) + " · expiry " + HfUi.Plain(em.Expiration ?? "?", 12)
                                     + (_surface.Provisional == true ? " · provisional" : ""));
        }

        private void RenderNextExpiry()
        {
            var next = _calendar?.Features?.MonthlyExpiration;
            if (next?.Date != null && DateTime.TryParseExact(next.Date, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var d))
            {
                var days = next.Days is double n ? " · in " + n.ToString("0", CultureInfo.InvariantCulture) + (Math.Abs(n - 1) < 1e-9 ? " day" : " days") : "";
                HfUi.SetText(_emNext, "Next monthly expiry " + d.ToString("ddd MMM d", CultureInfo.InvariantCulture) + days);
            }
            else
            {
                var why = _calendar == null ? (_calendarProblem ?? NotLoadedYet) : (_calendar.Reason ?? OsText.Reasons(_calendar.Features?.Reasons) ?? "no monthly expiration in the calendar");
                HfUi.SetText(_emNext, "Next monthly expiry " + HfUi.NotAvailable(why));
            }
        }

        private void RenderVix()
        {
            var blocked = SpyBlocked() ?? (_surface?.Surface == null ? (_surfaceProblem ?? "no surface payload") : null);
            var vx = _surface?.Surface?.VixStructure;
            if (blocked == null && vx == null) blocked = "no VIX structure in the surface";
            if (blocked == null && !string.IsNullOrWhiteSpace(vx!.Reason)) blocked = vx.Reason;
            if (blocked != null)
            {
                HfUi.SetText(_vixLabel, "");
                _vixCurve.SetData(Array.Empty<TermPoint>(), HfUi.NotAvailable(blocked));
                HfUi.SetText(_vixCaption, "");
                return;
            }
            // only the points that exist (VIX 30 d, VIX3M) — no invented 9D / 6M points
            var pts = new List<TermPoint>();
            if (vx!.Vix?.Value is double v1) pts.Add(new TermPoint("VIX 30 d", v1, v1.ToString("0.00", CultureInfo.InvariantCulture)));
            if (vx.Vxv?.Value is double v3) pts.Add(new TermPoint("VIX3M", v3, v3.ToString("0.00", CultureInfo.InvariantCulture)));
            _vixCurve.SetData(pts, HfUi.NotAvailable("no VIX values"));
            AutomationProperties.SetName(_vixCurve, "VIX term structure: " + string.Join(", ", pts.Select(p => p.Label + " " + p.ValueText)));
            var label = string.IsNullOrWhiteSpace(vx.Label) ? "" : HfUi.Plain(vx.Label, 30);
            if (label.Length == 0) label = "label not available";
            HfUi.SetText(_vixLabel, char.ToUpperInvariant(label[0]) + label.Substring(1));
            var parts = new List<string>();
            foreach (var p in pts) parts.Add(p.Label.Split(' ')[0] + " " + p.ValueText);
            parts.Add(vx.Ratio is double r ? "ratio " + r.ToString("0.00", CultureInfo.InvariantCulture) : "ratio " + HfUi.NotAvailable("not computed"));
            parts.Add("FRED close " + DateOf(vx.Vix?.Date));
            if (_surface!.Provisional == true) parts.Add("provisional");
            HfUi.SetText(_vixCaption, string.Join(" · ", parts));
        }

        // =====================================================================================
        // Strategies card: active paper account, risk row, equity curve, tiles
        // =====================================================================================

        private void SetAccountUnavailable(string label, string reason)
        {
            HfUi.SetText(_accountLabel, label);
            HfUi.SetText(_equityValue, "not available", HfTokens.TextDim);
            _equityReason.Visibility = Visibility.Visible;
            HfUi.SetText(_equityReason, HfUi.NotAvailable(reason));
            HfUi.SetText(_todayValue, "—", HfTokens.TextDim);
            HfUi.SetText(_totalValue, "—", HfTokens.TextDim);
            AutomationProperties.SetName(_equityValue, "Total paper equity: " + HfUi.NotAvailable(reason));
        }

        private void SetAccountValues(string label, double equity, double? day, double? total, double? totalPct)
        {
            HfUi.SetText(_accountLabel, label);
            HfUi.SetText(_equityValue, FundFormat.Money(equity), HfTokens.TextValue);
            _equityReason.Visibility = Visibility.Collapsed;
            // red / green: the user's own P&L only (contract §0)
            HfUi.SetText(_todayValue, day is double d ? FundFormat.Money(d, signed: true) : "not available", day is double d2 ? FundPalette.ForSigned(d2) : HfTokens.TextDim);
            var totalText = total is double t ? FundFormat.Money(t, signed: true) + (totalPct is double p ? " (" + FundFormat.Pct(p, 2, signed: true) + ")" : "") : "not available";
            HfUi.SetText(_totalValue, totalText, total is double t2 ? FundPalette.ForSigned(t2) : HfTokens.TextDim);
            AutomationProperties.SetName(_equityValue, "Total paper equity " + FundFormat.Money(equity));
        }

        private void RenderAccount()
        {
            var legacy = IsLegacyMode();
            if (legacy == null)
            {
                SetAccountUnavailable("Active paper account: unknown",
                    "which account trades is unknown — the migration status " + (_migrationProblem != null ? "could not be read: " + _migrationProblem : "is not loaded yet"));
            }
            else if (legacy == true)
            {
                const string label = "Old-engine paper account (until the migration)";
                var status = _migration?.LegacyAccount?.Status ?? "?";
                if (_fundReplaced) SetAccountUnavailable(label, "the old engine was replaced by the Council switch-over (its /fund routes are retired) while the account status is \"" + HfUi.Plain(status, 30) + "\"");
                else if (!_fundSeen) SetAccountUnavailable(label, "waiting for the first /fund/state answer");
                else if (!_fundConnected || _fund?.Account == null) SetAccountUnavailable(label, "the old engine is not reachable — see Trading › Legacy");
                else
                {
                    var a = _fund.Account;
                    if (a.Unreadable == true) SetAccountUnavailable(label, a.Error ?? "account state unreadable");
                    else if (!string.IsNullOrWhiteSpace(a.ValuationPaused)) SetAccountUnavailable(label, a.ValuationPaused);
                    else if (a.Equity is not double eq) SetAccountUnavailable(label, "no equity in /fund/state");
                    else SetAccountValues(label, eq, a.DayPnl, a.TotalPnl, a.TotalPnlPct);
                }
            }
            else
            {
                // after the migration: the backend's ONE account-set helper (/v2/overview/risk → account_summary), never summed here
                var sum = _risk?.AccountSummary;
                var n = sum?.N is double nn ? (int)Math.Round(nn) : 0;
                var label = sum?.Summed == true && n > 1 ? "Platform paper accounts (" + n + " accounts, summed)" : "Platform paper account";
                if (_risk == null) SetAccountUnavailable(label, _riskProblem ?? NotLoadedYet);
                else if (sum == null) SetAccountUnavailable(label, "backend route outdated — restart the backend");
                else if (sum.Available != true || sum.Equity is not double eqp)
                    SetAccountUnavailable(label, string.IsNullOrWhiteSpace(sum.Reason) ? (n == 0 ? "no active paper account" : "no equity in the account summary") : HfUi.Plain(sum.Reason, 300));
                else SetAccountValues(label, eqp, sum.DayPnl, sum.TotalPnl, null);
            }
            SafeRender(RenderAges);
            SafeRender(RenderEquityChart);
        }

        /// <summary>The faint "source · age" lines: account figures and the System card (refreshed every tick).</summary>
        private void RenderAges()
        {
            var legacy = IsLegacyMode();
            string src;
            if (legacy == true)
                src = _fundSeen && _fundConnected && !_fundReplaced ? "/fund/state · " + HfUi.Age((DateTime.UtcNow - _fundAt).TotalMinutes) + " ago" : "";
            else if (legacy == false)
            {
                var asOf = FundFormat.ParseTs(_risk?.AccountSummary?.AsOf);
                src = asOf.HasValue ? "portfolio · as of " + HfUi.Age((DateTimeOffset.UtcNow - asOf.Value).TotalMinutes) + " ago" : "";
            }
            else src = "";
            HfUi.SetText(_accountSource, src);
            HfUi.SetText(_systemMeta, _systemLoadedAt == DateTime.MinValue ? "" : "updated " + HfUi.Age((DateTime.UtcNow - _systemLoadedAt).TotalMinutes) + " ago");
        }

        private void RenderRisk()
        {
            var chips = new[] { _chipVar, _chipEs, _chipExposure, _chipDd, _chipVol };
            if (_risk == null)
            {
                foreach (var c in chips) c.SetUnavailable(_riskProblem ?? NotLoadedYet);
                HfUi.SetText(_riskMeta, "");
                return;
            }
            var f = _risk.Figures;
            SetFigure(_chipVar, f?.Var99, "VaR 99 %, 1 day");
            SetFigure(_chipEs, f?.Es975, "ES 97.5 %, 1 day");
            SetFigure(_chipVol, f?.Vol20AnnPct, "volatility of the last 20 daily returns, annualised");
            // max drawdown shown as a loss (typographic minus), whatever sign the backend uses
            var dd = f?.MaxDrawdownPct;
            if (dd != null && dd.IsAvailable)
                _chipDd.SetValue(FundFormat.Pct(-Math.Abs(dd.Value!.Value), 1), Tip("maximum drawdown over the whole series", dd));
            else _chipDd.SetUnavailable(dd?.Reason ?? "not in the answer");
            var net = f?.NetExposurePct;
            var gross = f?.GrossExposurePct;
            if (net != null && net.IsAvailable && gross != null && gross.IsAvailable)
                _chipExposure.SetValue("net " + FundFormat.Pct(net.Value, 0, signed: true) + " · gross " + FundFormat.Pct(gross.Value, 0),
                    Tip("net = (long − short) / equity, gross = (long + short) / equity", net));
            else
                _chipExposure.SetUnavailable(net == null || !net.IsAvailable ? (net?.Reason ?? "net exposure not in the answer") : (gross?.Reason ?? "gross exposure not in the answer"));
            var account = _risk.Account == "legacy" ? "old-engine account" : _risk.Account == "platform" ? "platform account" : HfUi.Plain(_risk.Account ?? "account not stated", 30);
            var n = _risk.NDailyReturns is double nd ? nd.ToString("0", CultureInfo.InvariantCulture) + " daily returns" : "number of daily returns not stated";
            HfUi.SetText(_riskMeta, "Risk of the " + account + " · " + n + " · as of " + FundFormat.LocalTime(_risk.AsOf, "dd MMM HH:mm"));
        }

        private static string Tip(string what, OverviewFigure fig)
            => what + (string.IsNullOrWhiteSpace(fig.Method) ? "" : " — method: " + HfUi.Plain(fig.Method, 200));

        private static void SetFigure(StatChip chip, OverviewFigure? fig, string what)
        {
            if (fig == null) { chip.SetUnavailable("not in the answer"); return; }
            if (!fig.IsAvailable) { chip.SetUnavailable(fig.Reason); return; }
            var v = fig.Value!.Value;
            var text = (fig.Unit ?? "") switch
            {
                "usd" => FundFormat.Money(v),
                "pct" => FundFormat.Pct(v, 1),
                _ => FundFormat.Num(v) + (string.IsNullOrEmpty(fig.Unit) ? "" : " " + HfUi.Plain(fig.Unit, 10)),
            };
            chip.SetValue(text, Tip(what, fig));
        }

        private string? _chartKey;
        private FundEquity? _chartEquity;

        private void RenderEquityChart()
        {
            var legacy = IsLegacyMode();
            // redrawn only when the mode, the equity data (a new /fund/equity answer) or the range changed — not on every 5 s account update
            var key = legacy + "|" + _fundReplaced + "|" + _equityProblem + "|" + _rangeDays;
            if (key == _chartKey && ReferenceEquals(_equity, _chartEquity)) return;
            _chartKey = key;
            _chartEquity = _equity;
            var empty = new List<(DateTime X, double? Y)>();
            if (legacy != true)
            {
                var why = legacy == null ? "the active account is not known yet"
                    : "not shown here yet — the platform equity chart follows in a later step";
                _equityChart.SetData(empty, empty, null, legacy == null ? HfUi.NotAvailable(why) : "Not shown here yet (the platform equity chart follows in a later step).");
                HfUi.SetText(_chartCaption, "");
                return;
            }
            if (_fundReplaced)
            {
                _equityChart.SetData(empty, empty, null, HfUi.NotAvailable("the old engine's /fund routes are retired (Council switch-over)"));
                HfUi.SetText(_chartCaption, "");
                return;
            }
            if (_equity == null)
            {
                _equityChart.SetData(empty, empty, null, HfUi.NotAvailable(_equityProblem ?? NotLoadedYet));
                HfUi.SetText(_chartCaption, "");
                return;
            }
            var cutoff = _rangeDays > 0 ? DateTime.UtcNow.AddDays(-_rangeDays) : DateTime.MinValue;
            var points = (_equity.Points ?? new List<FundEquityPoint>())
                .Select(p => (Ts: FundFormat.ParseTs(p.Ts), P: p))
                .Where(x => x.Ts.HasValue && x.Ts.Value.UtcDateTime >= cutoff)
                .ToList();
            var eq = points.Select(x => (X: x.Ts!.Value.UtcDateTime, Y: x.P.Equity)).ToList();
            var bm = points.Select(x => (X: x.Ts!.Value.UtcDateTime, Y: x.P.BenchmarkEquity)).ToList();
            _equityChart.SetData(eq, bm, _equity.StartingCapital, "No equity snapshots in this range yet — the old engine records one every run.");
            HfUi.SetText(_chartCaption, "Line: your paper equity (old engine) · grey dashed: SPY scaled to the starting capital · hairline: starting capital · source /fund/equity");
        }

        /// <summary>The parts of one strategy tile that change between polls (texts are updated in place — keeps the keyboard focus).</summary>
        private sealed class TileParts
        {
            public Border Root = null!;
            public Button Button = null!;
            public TextBlock Name = null!;
            public TextBlock State = null!;
            public TextBlock Total = null!;
        }

        private string? _tileKeys;
        private readonly List<TileParts> _tileParts = new();
        private Border? _incubationTile;
        private TextBlock? _incubationCount;

        private void RenderTiles()
        {
            // sources the platform lists (strategy sub-ledgers; the Council only if the platform lists it as a source)
            var shown = _sources == null ? new List<TradingSource>()
                : _sources.Where(x => x.Kind == "strategy").Concat(_sources.Where(x => x.Kind == "council")).Take(3).ToList();
            var keys = (_sources == null ? "x" : "ok") + "|" + string.Join("\u001f", shown.Select(x => x.SourceId + "\u001e" + x.Kind));
            if (keys != _tileKeys || _incubationTile == null)
            {
                // the set of tiles changed: rebuild; otherwise only the texts below are updated (focus stays on its tile)
                _tileKeys = keys;
                _tiles.Children.Clear();
                _tileParts.Clear();
                foreach (var src in shown)
                {
                    var parts = CreateStrategyTile(src);
                    _tileParts.Add(parts);
                    _tiles.Children.Add(parts.Root);
                }
                _incubationTile ??= CreateIncubationTile();
                _tiles.Children.Add(_incubationTile);
                _tiles.Columns = Math.Max(1, _tiles.Children.Count);
            }
            var councilRetired = _council?.IsRetired == true;   // SP §22.15: the Council source tile reads "retired"
            for (var i = 0; i < shown.Count && i < _tileParts.Count; i++)
                UpdateStrategyTile(_tileParts[i], shown[i], councilRetired && shown[i].Kind == "council");

            if (_sources == null)
            {
                _tilesProblem.Visibility = Visibility.Visible;
                HfUi.SetText(_tilesProblem, "Strategies: " + HfUi.NotAvailable(_sourcesProblem ?? NotLoadedYet));
            }
            else if (shown.Count == 0)
            {
                _tilesProblem.Visibility = Visibility.Visible;
                HfUi.SetText(_tilesProblem, "No strategy runs in a platform paper account yet.");
            }
            else _tilesProblem.Visibility = Visibility.Collapsed;

            if (_incubationCount != null)
            {
                if (_incubation?.Runs != null)
                {
                    var n = _incubation.Runs.Count(r => r.IsRunning);
                    HfUi.SetText(_incubationCount, n.ToString(CultureInfo.InvariantCulture) + (n == 1 ? " run" : " runs"), HfTokens.TextValue);
                    _incubationCount.FontSize = 14;
                    _incubationCount.FontFamily = HfTokens.Mono;
                }
                else
                {
                    HfUi.SetText(_incubationCount, HfUi.NotAvailable(_incubationProblem ?? NotLoadedYet), HfTokens.TextDim);
                    _incubationCount.FontSize = 12;
                    _incubationCount.ClearValue(TextBlock.FontFamilyProperty);
                }
            }
        }

        private Border Tile(UIElement content)
            => new()
            {
                Background = HfTokens.InsetBg,
                BorderBrush = HfTokens.InsetBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(12, 10, 12, 10),
                Margin = new Thickness(0, 0, 10, 0),
                Child = content,
            };

        private TileParts CreateStrategyTile(TradingSource s)
        {
            var parts = new TileParts();
            var stack = new StackPanel();
            parts.Name = HfUi.Text("", 13, HfTokens.Text, bold: true);
            stack.Children.Add(parts.Name);
            parts.State = HfUi.Text("", 12, HfTokens.TextDim);
            stack.Children.Add(parts.State);
            var line = new DockPanel { Margin = new Thickness(0, 6, 0, 0) };
            var lbl = HfUi.Text("total ", 12, HfTokens.TextDim);
            DockPanel.SetDock(lbl, Dock.Left);
            line.Children.Add(lbl);
            parts.Total = HfUi.Text("", 14, HfTokens.TextValue, mono: true);
            line.Children.Add(parts.Total);
            stack.Children.Add(line);
            var target = s.Kind == "council" ? OverviewTargets.Legacy : OverviewTargets.Trading;   // the kind is part of the tile key
            parts.Button = HfUi.FlatButton(stack, () => Navigate(target), "strategy");
            parts.Root = Tile(parts.Button);
            return parts;
        }

        private static void UpdateStrategyTile(TileParts parts, TradingSource s, bool retired = false)
        {
            var name = HfUi.Plain(s.Name ?? s.StrategyId ?? s.SourceId ?? "strategy", 80);
            HfUi.SetText(parts.Name, name);
            HfUi.SetText(parts.State, retired ? "retired — managed by hand" : HfUi.Plain(s.State ?? "state not stated", 40));
            var total = s.Pnl?.Total;
            if (total is double t)
            {
                HfUi.SetText(parts.Total, FundFormat.Money(t, signed: true), FundPalette.ForSigned(t));   // own P&L: red / green
                parts.Total.FontSize = 14;
                parts.Total.FontFamily = HfTokens.Mono;
            }
            else
            {
                HfUi.SetText(parts.Total, HfUi.NotAvailable(s.Valued == false ? "not valued" : "no P&L in the answer"), HfTokens.TextDim);
                parts.Total.FontSize = 12;
                parts.Total.ClearValue(TextBlock.FontFamilyProperty);
            }
            AutomationProperties.SetName(parts.Button, name + ", " + (retired ? "retired" : HfUi.Plain(s.State ?? "", 40)) + ", total P&L "
                                                       + (total is double t2 ? FundFormat.Money(t2, signed: true) : "not available"));
        }

        private Border CreateIncubationTile()
        {
            var stack = new StackPanel();
            stack.Children.Add(HfUi.Text("In incubation", 13, HfTokens.Text, bold: true));
            _incubationCount = HfUi.Text("", 14, HfTokens.TextValue, mono: true, wrap: true);
            stack.Children.Add(_incubationCount);
            var link = HfUi.Link("Start incubation →", () => Navigate(OverviewTargets.Trading), "Start an incubation in Portfolio and Trading");
            link.HorizontalAlignment = HorizontalAlignment.Left;
            link.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(link);
            var tile = Tile(stack);
            tile.Margin = new Thickness(0);
            return tile;
        }

        // =====================================================================================
        // System + Recent activity
        // =====================================================================================

        private void RenderSystemRows(PlatformResult<LoopList> loops, PlatformResult<ValidationRunList> vals, PlatformResult<BacktestRunList> bts,
                                      PlatformResult<OaStatus> oa, PlatformResult<BrokerList> brokers, PlatformResult<DataSourceList> sources,
                                      (string Text, string? Sub, bool Ok) ai)
        {
            // Loop: the newest loop's status + finalists
            if (!loops.Ok) _loopRow.Set(HfUi.NotAvailable(Problem(loops)));
            else
            {
                var l = loops.Value?.Loops?.FirstOrDefault();
                if (l == null) _loopRow.Set("no loop yet", null, DotKind.Neutral);
                else
                {
                    var st = HfUi.Plain(l.Status ?? "status unknown", 30);
                    var dot = l.Status == "failed" ? DotKind.Error : DotKind.Neutral;
                    _loopRow.Set(st + " · " + (l.Finalists ?? 0) + ((l.Finalists ?? 0) == 1 ? " finalist" : " finalists"),
                        string.IsNullOrWhiteSpace(l.StatusReason) ? null : HfUi.Plain(l.StatusReason, 160), dot);
                }
            }

            // Validation & backtests: active (queued / running / waiting) runs among the newest 50 of each
            if (!vals.Ok || !bts.Ok) _validationRow.Set(HfUi.NotAvailable(!vals.Ok ? "validations: " + Problem(vals) : "backtests: " + Problem(bts)));
            else
            {
                var v = (vals.Value?.Runs ?? new List<ValidationRunSummary>()).Count(r => ValidationStatus.IsActive(r.Status));
                var b = (bts.Value?.Runs ?? new List<BacktestRunSummary>()).Count(r => BacktestStatus.IsActive(r.Status));
                _validationRow.Set(v + b == 0 ? "none running" : v + " validation" + (v == 1 ? "" : "s") + " · " + b + " backtest" + (b == 1 ? "" : "s") + " active",
                    null, DotKind.Neutral);
            }

            // Options archive: state + today collected / expected
            if (!oa.Ok || oa.Value == null) _archiveRow.Set(HfUi.NotAvailable(Problem(oa)));
            else
            {
                var s = oa.Value;
                var state = s.State ?? "unknown";
                var dot = state switch { "ok" => DotKind.Neutral, "degraded" => DotKind.Warning, "error" => DotKind.Error, _ => DotKind.None };
                var today = s.Today;
                var todayText = today?.ExpectedSlots is int exp && today.Collected is int col
                    ? " · today " + col + "/" + exp + " slots"
                    : today?.TradingDay == false ? " · no session today" : "";
                _archiveRow.Set((state == "not_set_up" ? "not set up" : HfUi.Plain(state, 20)) + todayText,
                    string.IsNullOrWhiteSpace(s.LastError) ? null : HfUi.Plain(s.LastError, 160), dot);
            }

            // AI budget today (violet = AI)
            if (ai.Ok) _aiRow.Set(ai.Text, ai.Sub, DotKind.Ai, HfTokens.Ai);
            else _aiRow.Set(ai.Text);
            _aiRow.ToolTip = "Sum of the Hedge Fund module's daily AI budgets (GET /ai/budgets, period \"day\"): spent / limit, UTC day.";

            // Connections: brokers (connected ones) + data sources — "all ok" or the first problem
            if (!brokers.Ok || !sources.Ok)
            {
                _connRow.Set(HfUi.NotAvailable(!brokers.Ok ? "brokers: " + Problem(brokers) : "data sources: " + Problem(sources)));
                return;
            }
            string? problem = null;
            var connected = 0;
            foreach (var br in brokers.Value?.Brokers ?? new List<BrokerStatus>())
            {
                if (br.IsRetired || br.Connection?.Status != "connected") continue;
                connected++;
                var link = br.Link?.State;
                if (problem == null && link is "auth_failed" or "keys_missing" or "not_installed" or "error")
                    problem = HfUi.Plain(br.Label ?? br.Broker ?? br.Id ?? "broker", 40) + ": " + link + (string.IsNullOrWhiteSpace(br.Link?.LastError) ? "" : " — " + HfUi.Plain(br.Link!.LastError, 120));
            }
            var ds = sources.Value?.Sources ?? new List<DataSourceStatus>();
            foreach (var d in ds)
            {
                if (problem != null) break;
                if (string.Equals((d.Status ?? "").Trim(), "error", StringComparison.OrdinalIgnoreCase))
                    problem = HfUi.Plain(d.Source ?? d.DataType ?? "data source", 40) + ": error" + (string.IsNullOrWhiteSpace(d.Message) ? "" : " — " + HfUi.Plain(d.Message, 120));
            }
            if (problem != null) _connRow.Set(problem, null, DotKind.Error);
            else _connRow.Set("all ok", connected + " broker" + (connected == 1 ? "" : "s") + " connected · " + ds.Count + " data sources", DotKind.Neutral);
        }

        private string? _activitySignature;

        private void RenderActivity()
        {
            // rebuilt only when what is shown changed (keeps the keyboard focus on a row across the 15 s polls)
            var sig = _activity == null ? "x|" + _activityProblem
                : string.Join("\u001f", (_activity.Items ?? new List<OverviewActivityItem>())
                      .Select(i => i.Ts + "\u001e" + i.Kind + "\u001e" + i.Title + "\u001e" + i.Detail + "\u001e" + i.Severity + "\u001e" + i.Link))
                  + "|" + string.Join(";", _activity.ProblemSources()) + "|" + string.Join(";", _activity.RetiredSources())
                  + "|" + DateTime.Now.Date.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
            if (sig == _activitySignature) return;
            _activitySignature = sig;
            _activityList.Children.Clear();
            if (_activity == null)
            {
                _activityProblemText.Visibility = Visibility.Visible;
                HfUi.SetText(_activityProblemText, HfUi.NotAvailable(_activityProblem ?? NotLoadedYet));
                return;
            }
            var problems = _activity.ProblemSources();
            // SP §22.15: the Council / old engine read "retired" — a state, said as such, never listed as "not available"
            var retired = _activity.RetiredSources().Select(n => n == "old_engine" ? "old engine" : n == "council" ? "Council" : n).ToList();
            var notes = new List<string>();
            if (problems.Count > 0) notes.Add("not available: " + HfUi.Plain(string.Join("; ", problems), 400));
            if (retired.Count > 0) notes.Add("retired: " + HfUi.Plain(string.Join(", ", retired), 120) + " (past Council runs stay listed)");
            _activityProblemText.Visibility = notes.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
            HfUi.SetText(_activityProblemText, string.Join(" · ", notes));
            var items = _activity.Items ?? new List<OverviewActivityItem>();
            if (items.Count == 0)
            {
                _activityList.Children.Add(HfUi.Text("No activity yet.", 12.5, HfTokens.TextDim));
                return;
            }
            var today = DateTime.Now.Date;
            foreach (var it in items)
            {
                var ts = FundFormat.ParseTs(it.Ts);
                var time = ts.HasValue ? ts.Value.ToLocalTime().ToString(ts.Value.ToLocalTime().Date == today ? "HH:mm" : "dd MMM HH:mm", CultureInfo.InvariantCulture) : "time ?";
                var kind = HfUi.Plain(it.Kind ?? "", 30);
                var title = HfUi.Plain(it.Title ?? "", 140);
                var detail = HfUi.Plain(it.Detail ?? "", 240);
                // severity: an amber dot for warnings / errors, a neutral one otherwise; the text stays neutral (red only for own P&L)
                var attention = it.Severity == "error" || it.Severity == "warning";

                var grid = new Grid { Margin = new Thickness(0, 6, 0, 6) };
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(14) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(78) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                var dot = HfUi.Dot(attention ? DotKind.Warning : DotKind.Neutral, 6);
                dot.VerticalAlignment = VerticalAlignment.Top;
                dot.Margin = new Thickness(0, 6, 0, 0);
                grid.Children.Add(dot);
                var t = HfUi.Text(time, 11.5, HfTokens.TextFaint, mono: true);
                t.VerticalAlignment = VerticalAlignment.Top;
                Grid.SetColumn(t, 1);
                grid.Children.Add(t);
                var body = new StackPanel();
                var head = new TextBlock { TextTrimming = TextTrimming.CharacterEllipsis, FontSize = 13 };
                head.Inlines.Add(new System.Windows.Documents.Run(kind) { Foreground = HfTokens.TextMuted });
                if (kind.Length > 0 && title.Length > 0) head.Inlines.Add(new System.Windows.Documents.Run(" · ") { Foreground = HfTokens.TextFaint });
                head.Inlines.Add(new System.Windows.Documents.Run(title) { Foreground = HfTokens.Text });
                head.ToolTip = kind + " · " + title;
                body.Children.Add(head);
                if (detail.Length > 0) body.Children.Add(HfUi.Text(detail, 12, HfTokens.TextDim));
                Grid.SetColumn(body, 2);
                grid.Children.Add(body);

                var target = OverviewTargets.Resolve(it.Link);
                var row = new Border { BorderBrush = HfTokens.Divider, BorderThickness = new Thickness(0, 0, 0, 1) };
                var name = time + ", " + (attention ? (it.Severity ?? "") + ", " : "") + kind + ", " + title + (detail.Length > 0 ? ", " + detail : "");
                if (target != null)
                {
                    row.Child = HfUi.FlatButton(grid, () => Navigate(target), name + " — open");
                }
                else
                {
                    row.Child = grid;
                    AutomationProperties.SetName(row, name);
                }
                _activityList.Children.Add(row);
            }
        }
    }
}

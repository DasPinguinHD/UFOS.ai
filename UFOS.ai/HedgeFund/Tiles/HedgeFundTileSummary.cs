using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using UFOS.ai.ModuleTiles;

namespace UFOS.ai.HedgeFund.Tiles
{
    /// <summary>
    /// Maps the answer of <c>GET /v2/portfolio/summary</c> (schema 1, SP §17.15, UFOS.ai/docs/tasks/PORTFOLIO_SUMMARY_API.md —
    /// stored values only) to the Hedge Fund tile's Live figures (TILES_API §10.4; user choice 2026-10-25):
    ///   1. Equity     — the total of the primary currency (USD when present); detail: positions · accounts, other currencies
    ///                   listed separately (amounts of different currencies are never added up).
    ///   2. Day P&amp;L — the primary currency's total (+ % of the day reference); tone green / red.
    ///   3. Trading    — KILLED / HALTED (red) / kill pending (amber), otherwise "auto ON|OFF" (+ "Council ON|OFF" after the
    ///                   switch-over); the old engine's autopilot in the detail line while it still applies.
    ///   Sparkline     — the stored equity snapshots of paper-main (else the first account of the primary currency).
    /// A value the route does not have is shown as "not available" WITH the route's reason (never a stand-in figure).
    /// No WPF types here: the mapping is checked offline.
    /// </summary>
    public static class HedgeFundTileSummary
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        public const string NotAvailable = "not available";

        /// <summary>Throws (JsonException / KeyNotFoundException / InvalidOperationException / FormatException) on an
        /// unexpected answer — the provider turns that into a visible "unexpected answer" state.</summary>
        public static TileLiveResult Parse(string json)
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var schema = root.GetProperty("schema").GetInt32();
            if (schema != 1) throw new FormatException("schema " + schema.ToString(Inv));
            if (root.GetProperty("paper_only").ValueKind != JsonValueKind.True) throw new FormatException("paper_only is not true");
            var asOf = DateTimeOffset.Parse(root.GetProperty("generated_at").GetString()!, Inv, DateTimeStyles.AssumeUniversal);
            var sandbox = root.TryGetProperty("mode", out var mode) && mode.ValueKind == JsonValueKind.String && mode.GetString() == "sandbox";

            var accounts = root.GetProperty("accounts").EnumerateArray().ToList();
            var totals = root.GetProperty("totals").EnumerateObject().ToList();
            if (accounts.Count == 0 || totals.Count == 0)
                return TileLiveResult.NoData("no active paper account yet");

            var primary = totals.FirstOrDefault(t => t.Name == "USD");
            if (primary.Value.ValueKind == JsonValueKind.Undefined) primary = totals[0];
            var cur = primary.Name;
            var total = primary.Value;
            var positions = root.GetProperty("positions_count").GetInt32();

            var figures = new List<TileFigure>
            {
                EquityFigure(cur, total, accounts, totals, positions),
                DayPnlFigure(cur, total, accounts),
                TradingFigure(root),
            };
            return TileLiveResult.Fresh(new TileLiveData(asOf, figures, Spark(accounts, cur)), sandbox);
        }

        private static TileFigure EquityFigure(string cur, JsonElement total, List<JsonElement> accounts, List<JsonProperty> totals, int positions)
        {
            var nAcc = total.GetProperty("accounts").GetInt32();
            var detail = positions.ToString(Inv) + (positions == 1 ? " position" : " positions") + " · "
                         + nAcc.ToString(Inv) + (nAcc == 1 ? " account" : " accounts");
            foreach (var other in totals.Where(t => t.Name != cur))
            {
                var eq = Num(other.Value.GetProperty("equity"));
                detail += " · " + (eq is double v ? Money(other.Name, v, signed: false) : other.Name + " " + NotAvailable);
            }
            var equity = Num(total.GetProperty("equity"));
            if (equity is double e) return new TileFigure("Equity", Money(cur, e, signed: false), TileTone.Neutral, detail);
            return new TileFigure("Equity", NotAvailable, TileTone.Caution, ReasonOf(accounts, total, a => Num(a.GetProperty("equity")) == null));
        }

        private static TileFigure DayPnlFigure(string cur, JsonElement total, List<JsonElement> accounts)
        {
            var pnl = Num(total.GetProperty("day_pnl"));
            if (pnl is not double p)
            {
                // without equity there is no day P&L either — the Equity row already carries the reason (the tile is small)
                var reason = Num(total.GetProperty("equity")) == null ? "see Equity"
                             : ReasonOf(accounts, total, a => Num(a.GetProperty("day_pnl")) == null);
                return new TileFigure("Day P&L", NotAvailable, TileTone.Caution, reason);
            }
            var text = Money(cur, p, signed: true);
            // % of the day reference (= equity − day P&L, i.e. the sum of the accounts' reference snapshots)
            if (Num(total.GetProperty("equity")) is double e && e - p > 0)
                text += " (" + (p >= 0 ? "+" : "-") + Math.Abs(p / (e - p) * 100).ToString("0.00", Inv) + "%)";
            var tone = p > 0 ? TileTone.Positive : p < 0 ? TileTone.Negative : TileTone.Neutral;
            return new TileFigure("Day P&L", text, tone);
        }

        private static TileFigure TradingFigure(JsonElement root)
        {
            var kill = root.GetProperty("kill");
            var halts = root.GetProperty("halts");
            var pending = Ids(kill.GetProperty("pending"));
            if (kill.GetProperty("active").ValueKind == JsonValueKind.True)
                return new TileFigure("Trading", "KILLED", TileTone.Negative, "kill switch: " + string.Join(", ", Ids(kill.GetProperty("accounts")))
                                      + (pending.Count > 0 ? " · still flattening: " + string.Join(", ", pending) : ""));
            if (halts.GetProperty("any").ValueKind == JsonValueKind.True)
            {
                var kinds = root.GetProperty("accounts").EnumerateArray()
                    .Where(a => a.GetProperty("halt").GetProperty("active").ValueKind == JsonValueKind.True)
                    .Select(a => a.GetProperty("id").GetString() + " (" + (Str(a.GetProperty("halt").GetProperty("kind")) ?? "halt") + ")");
                return new TileFigure("Trading", "HALTED", TileTone.Negative, string.Join(", ", kinds));
            }
            if (pending.Count > 0)
                return new TileFigure("Trading", "kill pending", TileTone.Caution, "still flattening: " + string.Join(", ", pending));

            var auto = root.GetProperty("autotrade").GetProperty("enabled").ValueKind == JsonValueKind.True;
            var value = "auto " + (auto ? "ON" : "OFF");
            var council = root.GetProperty("council");
            var details = new List<string>();
            var switchover = Str(council.GetProperty("switchover"));
            if (switchover == "done")
                value += " · Council " + (council.GetProperty("orders_enabled").ValueKind == JsonValueKind.True ? "ON" : "OFF");
            else if (switchover == null)
                details.Add("Council state " + NotAvailable + " (" + (Str(council.GetProperty("error")) ?? "no reason given") + ")");
            else if (switchover == "pending")
                details.Add("Council switch-over pending");

            var old = root.GetProperty("old_engine");
            if (old.GetProperty("applicable").ValueKind == JsonValueKind.True)
            {
                var ap = old.GetProperty("autopilot");
                if (ap.ValueKind == JsonValueKind.True || ap.ValueKind == JsonValueKind.False)
                    details.Add("old engine autopilot " + (ap.ValueKind == JsonValueKind.True ? "ON" : "OFF")
                                + (old.GetProperty("halted").ValueKind == JsonValueKind.True ? ", halted" : ""));
                else
                    details.Add("old engine: " + (Str(old.GetProperty("reason")) ?? NotAvailable));
            }
            return new TileFigure("Trading", value, TileTone.Neutral, details.Count == 0 ? null : string.Join(" · ", details));
        }

        private static IReadOnlyList<double>? Spark(List<JsonElement> accounts, string cur)
        {
            var acc = accounts.FirstOrDefault(a => a.GetProperty("id").GetString() == "paper-main");
            if (acc.ValueKind == JsonValueKind.Undefined) acc = accounts.FirstOrDefault(a => a.GetProperty("currency").GetString() == cur);
            if (acc.ValueKind == JsonValueKind.Undefined || !acc.TryGetProperty("spark", out var s) || s.ValueKind != JsonValueKind.Array) return null;
            var pts = s.EnumerateArray().Select(p => Num(p.GetProperty("equity"))).Where(v => v != null).Select(v => v!.Value).ToList();
            return pts.Count >= 2 ? pts : null;
        }

        /// <summary>The route's own reason for the first account (of this currency's "missing" list) that lacks the value.</summary>
        private static string ReasonOf(List<JsonElement> accounts, JsonElement total, Func<JsonElement, bool> lacks)
        {
            var missing = Ids(total.GetProperty("missing"));
            foreach (var a in accounts)
            {
                var id = a.GetProperty("id").GetString();
                if (id == null || !missing.Contains(id) || !lacks(a)) continue;
                return id + ": " + (Str(a.GetProperty("reason")) ?? "no reason given");
            }
            return "missing for: " + (missing.Count > 0 ? string.Join(", ", missing) : "no account named");
        }

        private static string Money(string cur, double v, bool signed)
        {
            var sign = v < 0 ? "-" : signed && v > 0 ? "+" : "";
            var abs = Math.Abs(v).ToString("N0", Inv);
            return cur == "USD" ? sign + "$" + abs : sign + abs + " " + cur;
        }

        private static double? Num(JsonElement e) => e.ValueKind == JsonValueKind.Number ? e.GetDouble() : null;
        private static string? Str(JsonElement e) => e.ValueKind == JsonValueKind.String ? e.GetString() : null;
        private static List<string> Ids(JsonElement arr) => arr.EnumerateArray().Select(x => x.GetString() ?? "?").ToList();
    }
}

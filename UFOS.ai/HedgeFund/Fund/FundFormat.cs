using System;
using System.Globalization;
using System.Linq;
using System.Text.Json;

namespace UFOS.ai.HedgeFund.Fund
{
    /// <summary>Display formatting for the Hedge Fund dashboard (invariant culture, USD).</summary>
    internal static class FundFormat
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        public const string Dash = "—";
        public const string Minus = "−"; // typographic minus, same as the original dashboard

        /// <summary>"$12,345.67", "−$12.30"; signed = "+$1.20" for positive values.</summary>
        public static string Money(double? value, bool signed = false, int decimals = 2)
        {
            if (value is not double v || double.IsNaN(v) || double.IsInfinity(v)) return Dash;
            var abs = Math.Abs(v).ToString("N" + decimals, Inv);
            if (v < 0 && Math.Round(v, decimals) != 0) return Minus + "$" + abs;
            return (signed && v > 0 ? "+" : "") + "$" + abs;
        }

        /// <summary>AI cost: "$0.0021" (4 decimals below one dollar).</summary>
        public static string Usd(double? value)
        {
            if (value is not double v || double.IsNaN(v)) return Dash;
            return "$" + v.ToString(Math.Abs(v) < 1 ? "0.0000" : "N2", Inv);
        }

        /// <summary>Budget amounts: "$0.04".</summary>
        public static string Usd2(double? value)
        {
            if (value is not double v || double.IsNaN(v)) return Dash;
            return "$" + v.ToString("N2", Inv);
        }

        /// <summary>Percent value that is already in percent units (0.51 = 0.51 %).</summary>
        public static string Pct(double? value, int decimals = 1, bool signed = false)
        {
            if (value is not double v || double.IsNaN(v) || double.IsInfinity(v)) return Dash;
            var text = Math.Abs(v).ToString("F" + decimals, Inv) + "%";
            if (v < 0 && Math.Round(v, decimals) != 0) return Minus + text;
            return (signed && v > 0 ? "+" : "") + text;
        }

        /// <summary>Fraction (0.039) shown as percent ("3.9%").</summary>
        public static string Fraction(double? value, int decimals = 1, bool signed = false)
            => value is double v ? Pct(v * 100.0, decimals, signed) : Dash;

        public static string Num(double? value, int decimals = 2)
        {
            if (value is not double v || double.IsNaN(v) || double.IsInfinity(v)) return Dash;
            var text = Math.Abs(v).ToString("N" + decimals, Inv);
            return v < 0 && Math.Round(v, decimals) != 0 ? Minus + text : text;
        }

        /// <summary>Whole shares ("1,250") or fractional crypto quantities with up to 8 decimals, trailing zeros trimmed ("0.01523").</summary>
        public static string Qty(double? value)
        {
            if (value is not double v || double.IsNaN(v) || double.IsInfinity(v)) return Dash;
            if (Math.Abs(v - Math.Round(v)) < 1e-9) return Math.Round(v).ToString("N0", Inv);
            var text = Math.Round(v, 8).ToString("#,##0.########", Inv);
            return text.StartsWith("-", StringComparison.Ordinal) ? Minus + text[1..] : text;
        }

        /// <summary>Signed funding total: "+$12.30 received", "−$4.00 paid", "$0.00".</summary>
        public static string Funding(double? value)
        {
            if (value is not double v || double.IsNaN(v) || double.IsInfinity(v)) return Dash;
            if (Math.Round(v, 2) == 0) return "$0.00";
            return Money(v, signed: true) + (v > 0 ? " received" : " paid");
        }

        public static DateTimeOffset? ParseTs(string? iso)
        {
            if (string.IsNullOrWhiteSpace(iso)) return null;
            return DateTimeOffset.TryParse(iso, Inv, DateTimeStyles.AssumeUniversal, out var t) ? t : null;
        }

        /// <summary>Local wall-clock time of a UTC ISO timestamp ("14:05"), or "—".</summary>
        public static string LocalTime(string? iso, string format = "HH:mm")
        {
            var t = ParseTs(iso);
            return t.HasValue ? t.Value.ToLocalTime().ToString(format, Inv) : Dash;
        }

        /// <summary>"3:12", "1:02:03"; negative spans count as 0:00.</summary>
        public static string Countdown(TimeSpan span)
        {
            if (span < TimeSpan.Zero) span = TimeSpan.Zero;
            return span.TotalHours >= 1
                ? ((int)span.TotalHours).ToString(Inv) + ":" + span.Minutes.ToString("00", Inv) + ":" + span.Seconds.ToString("00", Inv)
                : span.Minutes.ToString(Inv) + ":" + span.Seconds.ToString("00", Inv);
        }

        /// <summary>A JSON value as short plain text (strings unquoted, numbers rounded, arrays joined).</summary>
        public static string Plain(JsonElement e)
        {
            switch (e.ValueKind)
            {
                case JsonValueKind.String: return e.GetString() ?? "";
                case JsonValueKind.Number:
                    if (e.TryGetInt64(out var l)) return l.ToString(Inv);
                    return e.TryGetDouble(out var d) ? Math.Round(d, 4).ToString("0.####", Inv) : e.GetRawText();
                case JsonValueKind.True: return "yes";
                case JsonValueKind.False: return "no";
                case JsonValueKind.Null:
                case JsonValueKind.Undefined: return Dash;
                case JsonValueKind.Array:
                    var items = e.EnumerateArray().Select(Plain).ToList();
                    return items.Count == 0 ? "none" : string.Join(", ", items);
                case JsonValueKind.Object:
                    return string.Join(", ", e.EnumerateObject().Select(p => p.Name + "=" + Plain(p.Value)));
                default: return e.GetRawText();
            }
        }

        /// <summary>"technical" -> "Technical", "premium_on_flip" -> "Premium on flip".</summary>
        public static string Label(string? key)
        {
            if (string.IsNullOrWhiteSpace(key)) return Dash;
            var text = key.Replace('_', ' ').Trim();
            return char.ToUpperInvariant(text[0]) + text[1..];
        }
    }
}

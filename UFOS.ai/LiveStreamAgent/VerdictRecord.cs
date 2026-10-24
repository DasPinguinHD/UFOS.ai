using System;
using System.Windows.Media;

namespace UFOS.ai
{
    public class VerdictRecord
    {
        public DateTime Timestamp { get; set; }
        public string Verdict { get; set; } = string.Empty;
        public string Ticker { get; set; } = string.Empty;
        public int Confidence { get; set; }
        public string Reason { get; set; } = string.Empty;
        // Provenance of the AI verdict (EU AI Act Art. 50) - kept with the record so older
        // verdicts in the history navigation stay labelled with their original model/time.
        public string? Model { get; set; }
        public DateTimeOffset? GeneratedAt { get; set; }
        // Optional OpenRouter usage of the call that produced this verdict (shared AI cost ledger);
        // null when the backend sent none (older backend / provider without cost reporting).
        public double? CostUsd { get; set; }
        public int? PromptTokens { get; set; }
        public int? CompletionTokens { get; set; }

        public string? UsageText => FormatUsage(CostUsd, PromptTokens, CompletionTokens);

        // "Cost: $0.0021 · 812 in / 190 out tokens" (cost < 0.01 -> 4 decimals, else 3).
        // Returns null when nothing is known, so the UI can hide the line.
        public static string? FormatUsage(double? costUsd, int? promptTokens, int? completionTokens)
        {
            var inv = System.Globalization.CultureInfo.InvariantCulture;
            string? cost = null;
            if (costUsd.HasValue && !double.IsNaN(costUsd.Value) && !double.IsInfinity(costUsd.Value))
                cost = "Cost: $" + costUsd.Value.ToString(Math.Abs(costUsd.Value) < 0.01 ? "0.0000" : "0.000", inv);
            string? tokens = null;
            if (promptTokens.HasValue || completionTokens.HasValue)
                tokens = $"{(promptTokens.HasValue ? promptTokens.Value.ToString(inv) : "?")} in / {(completionTokens.HasValue ? completionTokens.Value.ToString(inv) : "?")} out tokens";
            if (cost == null && tokens == null) return null;
            if (cost == null) return tokens;
            if (tokens == null) return cost;
            return cost + " \u00b7 " + tokens;
        }

        public string HeaderText => $"Verdict at {Timestamp:yyyy-MM-dd HH:mm:ss}: {Verdict} {Ticker}".Trim();

        public bool IsLong => (Verdict ?? string.Empty).ToUpperInvariant().Contains("LONG") || (Verdict ?? string.Empty).ToUpperInvariant().Contains("BUY") || (Verdict ?? string.Empty).ToUpperInvariant().Contains("BULL");
        public bool IsShort => (Verdict ?? string.Empty).ToUpperInvariant().Contains("SHORT") || (Verdict ?? string.Empty).ToUpperInvariant().Contains("SELL") || (Verdict ?? string.Empty).ToUpperInvariant().Contains("BEAR");

        public SolidColorBrush StrokeBrush
        {
            get
            {
                if (IsLong) return Brushes.Green;
                if (IsShort) return Brushes.Red;
                return Brushes.Gray;
            }
        }
    }
}


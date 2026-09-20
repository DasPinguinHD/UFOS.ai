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


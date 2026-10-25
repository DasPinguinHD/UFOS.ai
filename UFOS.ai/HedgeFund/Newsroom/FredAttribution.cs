using System;
using System.Diagnostics;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// FRED API terms of use (https://fred.stlouisfed.org/docs/api/terms_of_use.html, licence item relayed by the lead
    /// 2026-10-25): an application that uses the FRED API must show the exact notice below and give users a link to the terms.
    /// Shown in three places — Financial Newsroom → Macro indicators, the Economic Calendar section, ⚙ Keys &amp; data → the FRED
    /// row of Data sources — in addition to the existing "from FRED" source attribution, which stays.
    /// The link opens ONLY this fixed https URL in the default browser; a failure is shown, never swallowed.
    /// </summary>
    internal static class FredAttribution
    {
        /// <summary>The exact sentence the FRED API terms require (do not reword).</summary>
        public const string Notice = "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.";
        public const string TermsUrl = "https://fred.stlouisfed.org/docs/api/terms_of_use.html";
        public const string TermsLinkText = "FRED API terms";

        /// <summary>Fills <paramref name="target"/> with the notice and the "FRED API terms" link (replaces its text).</summary>
        public static void Fill(TextBlock target, Brush linkBrush)
        {
            target.Inlines.Clear();
            target.Inlines.Add(new Run(Notice + " "));
            target.Inlines.Add(CreateLink(linkBrush));
            AutomationProperties.SetName(target, Notice + " Link: " + TermsLinkText + ".");
        }

        /// <summary>The "FRED API terms" hyperlink (opens <see cref="TermsUrl"/> in the default browser).</summary>
        public static Hyperlink CreateLink(Brush linkBrush)
        {
            var link = new Hyperlink(new Run(TermsLinkText))
            {
                NavigateUri = new Uri(TermsUrl, UriKind.Absolute),
                Foreground = linkBrush,
                ToolTip = TermsUrl + " (opens in your default browser)",
            };
            AutomationProperties.SetName(link, TermsLinkText + " (opens in your default browser)");
            link.RequestNavigate += (s, e) =>
            {
                e.Handled = true;
                OpenTerms(s as DependencyObject);
            };
            return link;
        }

        /// <summary>Opens the FRED API terms — https and the fixed host only; a failure is logged AND shown with the URL.</summary>
        public static void OpenTerms(DependencyObject? source)
        {
            var uri = new Uri(TermsUrl, UriKind.Absolute);
            if (uri.Scheme != Uri.UriSchemeHttps || !string.Equals(uri.Host, "fred.stlouisfed.org", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("FRED terms URL must be https://fred.stlouisfed.org/…");
            try
            {
                Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                AppLog.Warn("HedgeFund", "Could not open the FRED API terms in the default browser", ex);
                var owner = source != null ? Window.GetWindow(source) : null;
                var text = "The FRED API terms could not be opened in your default browser (" + ex.GetType().Name + ").\n\nAddress: " + TermsUrl;
                if (owner != null) MessageBox.Show(owner, text, TermsLinkText, MessageBoxButton.OK, MessageBoxImage.Warning);
                else MessageBox.Show(text, TermsLinkText, MessageBoxButton.OK, MessageBoxImage.Warning);
            }
        }
    }
}

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Windows.Controls;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>One section of the Financial Newsroom (menu entry + view).</summary>
    internal sealed record NewsroomSection(
        string Id,
        string Glyph,
        string Title,
        string Subtitle,
        Func<UserControl> CreateView);

    /// <summary>
    /// The Financial Newsroom's sections — the single extension point. The
    /// newsroom window's navigation is built from this list (the Hedge Fund
    /// window's "Financial Newsroom" button opens the FIRST entry), so a new section is:
    /// a UserControl view here in Newsroom/, a backend collector + endpoint
    /// (HedgeFund/backend/newsroom/), and one line below.
    /// </summary>
    internal static class NewsroomSections
    {
        public static readonly IReadOnlyList<NewsroomSection> All = new[]
        {
            new NewsroomSection("insider", "◆", "Insider Trades", "SEC Form 4 filings · AI summary",
                () => new InsiderTradesView()),
            new NewsroomSection("macro", "◈", "Macro Indicators", "FRED rates, inflation, jobs",
                () => new MacroIndicatorsView()),
            new NewsroomSection("calendar", "◇", "Economic Calendar", "Upcoming US releases & FOMC",
                () => new EconomicCalendarView()),
            new NewsroomSection("analysis", "▣", "Analysis & Reasoning", "Combine signals · AI synthesis",
                () => new AnalysisReasoningView()),
        };
    }

    /// <summary>
    /// Implemented by every section view: the newsroom window calls Shutdown()
    /// when it closes, so a view's auto-refresh timer never outlives the window.
    /// </summary>
    public interface INewsroomView
    {
        void Shutdown();
    }

    /// <summary>Opens http(s) links (SEC filings, FRED series) in the default browser.</summary>
    internal static class NewsroomLinks
    {
        public static void Open(string? url)
        {
            if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) ||
                (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
            {
                AppLog.Warn("HedgeFund", "Refused to open non-http(s) link: " + url);
                return;
            }
            try
            {
                Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                AppLog.Warn("HedgeFund", "Could not open link in the default browser: " + uri.AbsoluteUri, ex);
            }
        }
    }
}

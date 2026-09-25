using System;
using System.Threading.Tasks;

namespace UFOS.ai.Services
{
    // AiModel: set it (to the model id) whenever a language model wrote Headline/Detail -
    // MainWindow then shows the "✦ AI-GENERATED · model · time" label (EU AI Act Art. 50).
    public sealed record PortfolioInsight(string Headline, string Detail, DateTimeOffset GeneratedAt, string? AiModel = null);

    // Extension seam for the "Portfolio Insight" card: there is no real portfolio/broker
    // connection yet (HedgeFund is still a stub), so this stays illustrative until a real
    // portfolio microservice exists. Swapping PlaceholderPortfolioInsightProvider for a
    // real implementation is the only change MainWindow needs once one exists.
    public interface IPortfolioInsightProvider
    {
        Task<PortfolioInsight?> TryGetLatestAsync();
    }

    public sealed class PlaceholderPortfolioInsightProvider : IPortfolioInsightProvider
    {
        public Task<PortfolioInsight?> TryGetLatestAsync() => Task.FromResult<PortfolioInsight?>(null);
    }
}

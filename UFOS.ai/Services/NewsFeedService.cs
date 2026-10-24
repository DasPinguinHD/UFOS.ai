using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.ServiceModel.Syndication;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Xml;

namespace UFOS.ai.Services
{
    public sealed record NewsHeadline(string Title, string Source, DateTimeOffset PublishedAt, string Summary, string? Link);

    // Free, keyless RSS feeds - no signup, no rate-limit-sensitive API key to manage.
    public sealed class NewsFeedService
    {
        // Three dedicated MarketWatch feeds, all purely market/business news. Unlike
        // "Top Stories" (which has no <category> tags at all and mixes in the
        // "Moneyist" advice column - retirement/estate-planning reader questions,
        // celebrity real-estate items, etc.), none of these carry that content, so
        // merging them gives a reliably on-topic pool with no per-item filtering needed.
        private static readonly string[] MarketFeedUrls =
        {
            "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
            "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
            "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
        };

        // Last resort only, if every feed above is unreachable - mixes in off-topic
        // MarketWatch verticals, so it's never used while any market feed works.
        private static readonly string[] FallbackFeedUrls =
        {
            "https://feeds.content.dowjones.io/public/rss/mw_topstories",
            "https://finance.yahoo.com/news/rssindex",
        };

        private static readonly string[] MoneyFlowKeywords =
        {
            "flow", "inflow", "outflow", "fund", "sector", "rotation", "capital",
        };

        private readonly HttpClient _httpClient;

        public NewsFeedService(HttpClient httpClient)
        {
            _httpClient = httpClient;
        }

        public async Task<IReadOnlyList<NewsHeadline>> GetTopStoriesAsync()
        {
            var fetches = await Task.WhenAll(MarketFeedUrls.Select(FetchFeedSafeAsync)).ConfigureAwait(false);
            var merged = fetches
                .SelectMany(headlines => headlines)
                .GroupBy(h => h.Title, StringComparer.OrdinalIgnoreCase)
                .Select(g => g.First())
                .OrderByDescending(h => h.PublishedAt)
                .ToList();

            if (merged.Count > 0)
            {
                return merged;
            }

            foreach (var url in FallbackFeedUrls)
            {
                try
                {
                    var headlines = await FetchFeedAsync(url).ConfigureAwait(false);
                    if (headlines.Count > 0)
                    {
                        return headlines;
                    }
                }
                catch
                {
                    // try the next fallback feed
                }
            }

            return Array.Empty<NewsHeadline>();
        }

        private async Task<IReadOnlyList<NewsHeadline>> FetchFeedSafeAsync(string url)
        {
            try
            {
                return await FetchFeedAsync(url).ConfigureAwait(false);
            }
            catch
            {
                return Array.Empty<NewsHeadline>();
            }
        }

        // Best-effort: free RSS has no dedicated fund-flow feed, so this scans the top
        // headlines (excluding index 0, already shown on the Markets card - otherwise a
        // keyword match there would duplicate it) for flow-ish keywords and falls back
        // to the next general headline.
        public static NewsHeadline? PickMoneyFlowsHeadline(IReadOnlyList<NewsHeadline> headlines)
        {
            var candidate = headlines.Skip(1).Take(15)
                .FirstOrDefault(h => MoneyFlowKeywords.Any(k => h.Title.Contains(k, StringComparison.OrdinalIgnoreCase)));

            if (candidate is not null)
            {
                return candidate;
            }

            return headlines.Count > 1 ? headlines[1] : headlines.FirstOrDefault();
        }

        private async Task<IReadOnlyList<NewsHeadline>> FetchFeedAsync(string url)
        {
            using var stream = await _httpClient.GetStreamAsync(url).ConfigureAwait(false);
            using var reader = XmlReader.Create(stream);
            var feed = SyndicationFeed.Load(reader);

            return feed.Items
                .Select(item => new NewsHeadline(
                    item.Title?.Text?.Trim() ?? string.Empty,
                    ResolveSource(feed, item),
                    item.PublishDate != default ? item.PublishDate : DateTimeOffset.Now,
                    CleanSummary(item.Summary?.Text),
                    item.Links.FirstOrDefault()?.Uri?.ToString()))
                .Where(h => !string.IsNullOrWhiteSpace(h.Title))
                .ToList();
        }

        private static string ResolveSource(SyndicationFeed feed, SyndicationItem item)
        {
            if (!string.IsNullOrWhiteSpace(item.Authors.FirstOrDefault()?.Name))
            {
                return item.Authors[0].Name;
            }

            var feedTitle = feed.Title?.Text ?? "News";

            // The three merged feeds are each titled "MarketWatch.com - Bulletins" /
            // "- MarketPulse" / "- Real-time Headlines" - normalize so cards showing
            // headlines from different feeds still read consistently.
            return feedTitle.StartsWith("MarketWatch.com", StringComparison.OrdinalIgnoreCase)
                ? "MarketWatch.com"
                : feedTitle;
        }

        // RSS descriptions often embed raw HTML (tags, entities) - strip it down to
        // plain text for display in the article popup.
        private static string CleanSummary(string? rawSummary)
        {
            if (string.IsNullOrWhiteSpace(rawSummary))
            {
                return string.Empty;
            }

            var withoutTags = Regex.Replace(rawSummary, "<[^>]+>", " ");
            var decoded = WebUtility.HtmlDecode(withoutTags);
            return Regex.Replace(decoded, @"\s+", " ").Trim();
        }
    }
}

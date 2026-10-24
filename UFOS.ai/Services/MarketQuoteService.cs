using System;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;

namespace UFOS.ai.Services
{
    public sealed record MarketQuote(string Symbol, double Price, double? ChangePercent, DateTimeOffset FetchedAt);

    // Mirrors LiveStreamAgent/backend/market_data.py's _fetch_chart_quote: same
    // unauthenticated Yahoo Finance chart endpoint, same meta fields, no API key.
    public sealed class MarketQuoteService
    {
        private const string ChartUrlTemplate = "https://query1.finance.yahoo.com/v8/finance/chart/{0}?interval=1d&range=1d";

        private readonly HttpClient _httpClient;

        public MarketQuoteService(HttpClient httpClient)
        {
            _httpClient = httpClient;
        }

        public async Task<MarketQuote?> GetQuoteAsync(string symbol)
        {
            var url = string.Format(ChartUrlTemplate, Uri.EscapeDataString(symbol));
            using var response = await _httpClient.GetAsync(url).ConfigureAwait(false);
            response.EnsureSuccessStatusCode();

            await using var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false);
            using var doc = await JsonDocument.ParseAsync(stream).ConfigureAwait(false);

            if (!doc.RootElement.TryGetProperty("chart", out var chart) ||
                !chart.TryGetProperty("result", out var resultArray) ||
                resultArray.ValueKind != JsonValueKind.Array ||
                resultArray.GetArrayLength() == 0)
            {
                return null;
            }

            var meta = resultArray[0].GetProperty("meta");
            if (!meta.TryGetProperty("regularMarketPrice", out var priceEl) || !priceEl.TryGetDouble(out var price))
            {
                return null;
            }

            double? previousClose = null;
            if (meta.TryGetProperty("chartPreviousClose", out var prevEl) && prevEl.TryGetDouble(out var prev))
            {
                previousClose = prev;
            }
            else if (meta.TryGetProperty("previousClose", out var prevEl2) && prevEl2.TryGetDouble(out var prev2))
            {
                previousClose = prev2;
            }

            double? changePercent = previousClose is > 0
                ? (price - previousClose.Value) / previousClose.Value * 100
                : null;

            return new MarketQuote(symbol, price, changePercent, DateTimeOffset.Now);
        }
    }
}

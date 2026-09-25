using System;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Input;
using System.Windows.Media;
using Microsoft.Web.WebView2.Core;
using UFOS.ai.Logging;
using UFOS.ai.Services;
using UFOS.ai.Shared;

namespace UFOS.ai
{
    public partial class ArticleWindow : Window
    {
        // MarketWatch returns a DataDome bot-challenge (a CAPTCHA JS wall) to a plain
        // HTTP GET, even for its free, non-paywalled "Markets" section articles - a
        // real HttpClient request never sees the actual page. Embedding a real
        // Chromium engine (WebView2) instead loads the article exactly as the user's
        // own browser would, which gets past that challenge the same way.
        private const int MaxSummaryInputLength = 8000;

        private readonly OpenRouterClient _openRouterClient = new();
        private readonly NewsHeadline _headline;
        private bool _webViewReady;

        public ArticleWindow(NewsHeadline headline, Window owner)
        {
            InitializeComponent();
            Owner = owner;
            _headline = headline;

            ArticleTitleText.Text = headline.Title;

            // One-click AI analysis -> the trigger itself says it's AI (EU AI Act Art. 50,
            // project convention "AI content labelling").
            SummarizeButton.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetHelpText(SummarizeButton, AiContent.TriggerTooltip);
            ArticleMetaText.Text = $"{headline.Source} · {headline.PublishedAt:MMM d, HH:mm}";

            if (string.IsNullOrWhiteSpace(headline.Link))
            {
                OpenLinkText.Visibility = Visibility.Collapsed;
                LoadingOverlay.Visibility = Visibility.Collapsed;
                ShowFallbackText(string.IsNullOrWhiteSpace(headline.Summary)
                    ? "No article link or summary was provided by the source feed."
                    : headline.Summary);
                return;
            }

            _ = InitializeWebViewAsync(headline.Link);
        }

        private async System.Threading.Tasks.Task InitializeWebViewAsync(string link)
        {
            try
            {
                var userDataFolder = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "UFOS.ai", "WebView2");
                Directory.CreateDirectory(userDataFolder);

                var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: userDataFolder);
                await ArticleWebView.EnsureCoreWebView2Async(environment);

                ArticleWebView.CoreWebView2.NavigationCompleted += (_, e) =>
                {
                    LoadingOverlay.Visibility = Visibility.Collapsed;
                    _webViewReady = e.IsSuccess;
                    if (!e.IsSuccess)
                    {
                        ShowFallbackText(string.IsNullOrWhiteSpace(_headline.Summary)
                            ? "Could not load the article."
                            : _headline.Summary);
                    }
                };

                ArticleWebView.CoreWebView2.Navigate(link);
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("ArticleWindow", "Failed to initialize WebView2", ex); } catch { }
                LoadingOverlay.Visibility = Visibility.Collapsed;
                ShowFallbackText(string.IsNullOrWhiteSpace(_headline.Summary)
                    ? "Could not load the article (WebView2 unavailable)."
                    : _headline.Summary);
            }
        }

        // Plain (non-AI) text: the source feed's own teaser or a status message.
        private void ShowFallbackText(string text)
        {
            SetAiPresentation(badge: null, caveat: false);
            SummaryText.Text = text;
            AutomationProperties.SetName(SummaryText, string.Empty);
            SummaryPanel.Visibility = Visibility.Visible;
        }

        // ---- AI summary states: labelled from the first moment they're visible ----

        private void ShowAiLoading()
        {
            SetAiPresentation(AiContent.LoadingBadge, caveat: false);
            SummaryText.Text = "Reading the article and asking the language model…";
            AutomationProperties.SetName(SummaryText, "AI summary is being generated");
            SummaryPanel.Visibility = Visibility.Visible;
        }

        private void ShowAiSummary(AiTextResult result)
        {
            SetAiPresentation(AiContent.Badge(result.Model, result.GeneratedAt), caveat: true);
            SummaryText.Text = result.Text;
            AutomationProperties.SetName(SummaryText, AiContent.AutomationName(result.Model, result.GeneratedAt));
            SummaryCostText.Text = FormatCostLine(result);
            SummaryCostText.Visibility = Visibility.Visible;
            SummaryPanel.Visibility = Visibility.Visible;
        }

        // "Cost: $0.0123 · 1,512 in / 874 out tokens (charged by OpenRouter)"
        private static string FormatCostLine(AiTextResult result)
        {
            if (result.CostUsd is null)
            {
                return "Cost: not reported by OpenRouter";
            }

            var tokens = result.PromptTokens is null && result.CompletionTokens is null
                ? string.Empty
                : $" · {AiCostLedger.FormatTokens(result.PromptTokens)} in / {AiCostLedger.FormatTokens(result.CompletionTokens)} out tokens";
            return $"Cost: {AiCostLedger.FormatUsd(result.CostUsd)}{tokens} (charged by OpenRouter)";
        }

        private void ShowAiError(string message)
        {
            SetAiPresentation(AiContent.UnavailableBadge, caveat: false);
            SummaryText.Text = message;
            AutomationProperties.SetName(SummaryText, string.Empty);
            SummaryPanel.Visibility = Visibility.Visible;
        }

        // Violet left accent + badge line for anything the model wrote; the plain
        // panel look for everything else, so AI text never looks like source text.
        private void SetAiPresentation(string? badge, bool caveat)
        {
            bool isAi = badge is not null;
            SummaryAiBadge.Text = badge ?? string.Empty;
            SummaryAiBadge.Visibility = isAi ? Visibility.Visible : Visibility.Collapsed;
            SummaryAiCaveat.Text = AiContent.Caveat;
            SummaryAiCaveat.Visibility = caveat ? Visibility.Visible : Visibility.Collapsed;
            // Only ShowAiSummary shows the cost line; hidden while loading, on errors and for plain text.
            SummaryCostText.Visibility = Visibility.Collapsed;
            SummaryPanel.BorderThickness = isAi ? new Thickness(2, 0, 0, 0) : new Thickness(0);
            SummaryPanel.BorderBrush = isAi ? TryFindResource("AiAccentBrush") as Brush : null;
        }

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton == MouseButtonState.Pressed)
            {
                try { DragMove(); } catch { }
            }
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        private void OpenLinkText_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (string.IsNullOrWhiteSpace(_headline.Link))
            {
                return;
            }

            try
            {
                Process.Start(new ProcessStartInfo(_headline.Link) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                (Application.Current as IUiErrorReporter)?.ShowUiException(ex);
            }
        }

        private async void SummarizeButton_Click(object sender, RoutedEventArgs e)
        {
            if (!_openRouterClient.IsConfigured)
            {
                ShowAiError("AI summary unavailable: no OpenRouter API key configured. " +
                            "Copy UFOS.ai/.env.example to UFOS.ai/.env and set OPENROUTER_API_KEY.");
                return;
            }

            SummarizeButton.IsEnabled = false;
            SummarizeButton.Content = AiContent.Glyph + " Summarizing…";
            ShowAiLoading();

            try
            {
                // Grab the page text first: ShowAiLoading() covers the WebView visually
                // but the DOM is still there to read.
                var articleText = await ExtractArticleTextAsync();
                var summary = await _openRouterClient.SummarizeArticleAsync(_headline.Title, articleText);
                if (summary is null)
                {
                    ShowAiError("AI summary unavailable: the model returned an empty response.");
                }
                else
                {
                    ShowAiSummary(summary);
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("ArticleSummary", "Failed to summarize article via OpenRouter", ex); } catch { }
                ShowAiError($"AI summary failed: {ex.Message}");
            }
            finally
            {
                SummarizeButton.IsEnabled = true;
                SummarizeButton.Content = AiContent.Glyph + " Summarize with AI";
            }
        }

        // Pulls visible text out of the loaded page's DOM (once WebView2 has actually
        // rendered it, past MarketWatch's bot-challenge) rather than sending raw HTML
        // to the LLM. Tries known article-container selectors first since they exclude
        // navigation/ads/related-links boilerplate; falls back to the whole page's
        // text, and further back to the RSS teaser if the page never loaded at all.
        private async System.Threading.Tasks.Task<string> ExtractArticleTextAsync()
        {
            if (!_webViewReady || ArticleWebView.CoreWebView2 is null)
            {
                return _headline.Summary;
            }

            const string script = """
                (function() {
                    var selectors = ['.article__content', '.article-body', '[itemprop="articleBody"]', 'article'];
                    for (var i = 0; i < selectors.length; i++) {
                        var el = document.querySelector(selectors[i]);
                        if (el && el.innerText && el.innerText.trim().length > 200) {
                            return el.innerText;
                        }
                    }
                    return document.body.innerText;
                })();
                """;

            try
            {
                var resultJson = await ArticleWebView.CoreWebView2.ExecuteScriptAsync(script);
                var text = JsonSerializer.Deserialize<string>(resultJson) ?? string.Empty;
                text = text.Trim();
                return text.Length > MaxSummaryInputLength ? text[..MaxSummaryInputLength] : text;
            }
            catch
            {
                return _headline.Summary;
            }
        }
    }
}

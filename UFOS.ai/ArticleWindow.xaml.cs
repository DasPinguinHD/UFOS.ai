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
            // user request 2026-10-23: start maximized on the owner's monitor, within its work area (taskbar visible);
            // WPF raises no StateChanged for the initial state, so the maximized look is applied once here
            WindowMaximize.ConstrainToWorkArea(this);
            WindowMaximize.StartMaximized(this);
            Window_StateChanged(this, EventArgs.Empty);
            Owner = owner;
            _headline = headline;

            ArticleTitleText.Text = headline.Title;

            // One-click AI analysis -> the trigger itself says it's AI (EU AI Act Art. 50,
            // project convention "AI content labelling").
            SummarizeButton.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetHelpText(SummarizeButton, AiContent.TriggerTooltip);
            ArticleMetaText.Text = $"{headline.Source} · {headline.PublishedAt:MMM d, HH:mm}";

            // The link comes from a third-party RSS feed: only http(s) is ever loaded or
            // opened (a file:, javascript: or custom-protocol link could otherwise run local
            // programs via ShellExecute or script in the WebView). Security-by-default rule.
            var webLink = TryGetWebUri(headline.Link);
            if (webLink == null)
            {
                if (!string.IsNullOrWhiteSpace(headline.Link))
                    AppLog.Warn("ArticleWindow", "Ignored a non-http(s) article link from the feed: " + headline.Link);
                OpenLinkText.Visibility = Visibility.Collapsed;
                LoadingOverlay.Visibility = Visibility.Collapsed;
                ShowFallbackText(string.IsNullOrWhiteSpace(headline.Summary)
                    ? "No article link or summary was provided by the source feed."
                    : headline.Summary);
                return;
            }

            _ = InitializeWebViewAsync(webLink.AbsoluteUri);
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
                // Third-party article pages (S10-A2 M5): InPrivate profile — cookies, cache and storage of the sites
                // live only as long as this window (CoreWebView2ControllerOptions, WebView2 SDK 1.0.1661+; the app
                // references 1.0.4191.47). Side effect: a site's bot challenge (MarketWatch's DataDome) may run again
                // on every article instead of once per profile.
                var controllerOptions = environment.CreateCoreWebView2ControllerOptions();
                controllerOptions.IsInPrivateModeEnabled = true;
                await ArticleWebView.EnsureCoreWebView2Async(environment, controllerOptions);

                // No DevTools, no alert/confirm/prompt boxes from the page, no host objects, no web messages
                // (this window reads the page only through ExecuteScriptAsync), no autofill / password saving.
                var settings = ArticleWebView.CoreWebView2.Settings;
                settings.AreDevToolsEnabled = false;
                settings.AreDefaultScriptDialogsEnabled = false;
                settings.AreHostObjectsAllowed = false;
                settings.IsWebMessageEnabled = false;
                settings.IsPasswordAutosaveEnabled = false;
                settings.IsGeneralAutofillEnabled = false;
                settings.IsStatusBarEnabled = false;

                // A news page never downloads files or gets camera / microphone / location / notifications /
                // clipboard / … — every download is cancelled silently, every permission request denied.
                ArticleWebView.CoreWebView2.DownloadStarting += (_, args) =>
                {
                    args.Cancel = true;
                    args.Handled = true;
                    AppLog.Warn("ArticleWindow", "Cancelled a download started by the article page.");
                };
                ArticleWebView.CoreWebView2.PermissionRequested += (_, args) =>
                {
                    args.State = CoreWebView2PermissionState.Deny;
                    args.SavesInProfile = false;   // nothing is remembered
                    args.Handled = true;           // decided here — WebView2 never shows its own prompt
                };
                // Click-test 2026-10-24: MarketWatch still showed "We'd like to send you some notifications — Allow / Dismiss".
                // The browser prompt itself is denied above; that box is the site's OWN in-page "soft ask", shown while the
                // page sees Notification.permission == "default". Every document therefore sees notifications (and the
                // Permissions API answer for them) as already DENIED, so such soft asks are skipped.
                await ArticleWebView.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(NotificationsDeniedScript);

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

                // Redirects / clicks inside the article: stay on http(s) only; pop-ups
                // (target=_blank) open in the default browser instead of a bare WebView window.
                ArticleWebView.CoreWebView2.NavigationStarting += (_, args) =>
                {
                    if (TryGetWebUri(args.Uri) == null)
                    {
                        args.Cancel = true;
                        AppLog.Warn("ArticleWindow", "Blocked navigation to a non-http(s) URL: " + args.Uri);
                    }
                };
                ArticleWebView.CoreWebView2.NewWindowRequested += (_, args) =>
                {
                    args.Handled = true;
                    OpenInDefaultBrowser(args.Uri);
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

        /// <summary>Runs before any page script: notifications report "denied", requestPermission resolves "denied",
        /// navigator.permissions.query reports "denied" for notifications / push. Changes nothing else.</summary>
        private const string NotificationsDeniedScript = @"(function () {
  try {
    if (window.Notification) {
      Object.defineProperty(window.Notification, 'permission', { get: function () { return 'denied'; }, configurable: false });
      window.Notification.requestPermission = function (cb) { try { if (typeof cb === 'function') cb('denied'); } catch (e) {} return Promise.resolve('denied'); };
    }
    if (navigator.permissions && navigator.permissions.query) {
      var original = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = function (desc) {
        var name = desc && desc.name;
        if (name === 'notifications' || name === 'push') return Promise.resolve({ state: 'denied', status: 'denied', onchange: null });
        return original(desc);
      };
    }
  } catch (e) { }
})();";

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
            AutomationProperties.SetName(SummaryText, AiContent.LoadingAutomationName("summary"));
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

        // Title bar (borderless window, conventions "Borderless windows"): no maximize button;
        // dragging a MAXIMIZED window restores it only once the mouse actually moves
        // with the button held (a plain click does nothing), keeps the cursor at its
        // relative spot on the title bar, then drags. Same pattern as
        // HedgeFund/HedgeFundWindow.xaml.cs (TitleBar_MouseMove).
        private bool _restoreOnDrag;
        private System.Windows.Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (WindowState == WindowState.Maximized)
            {
                _restoreOnDrag = true;
                _restoreDragStart = e.GetPosition(this);
                ((UIElement)sender).CaptureMouse();
                return;
            }
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseMove(object sender, MouseEventArgs e)
        {
            if (!_restoreOnDrag || e.LeftButton != MouseButtonState.Pressed) return;
            var pos = e.GetPosition(this);
            if (Math.Abs(pos.X - _restoreDragStart.X) < SystemParameters.MinimumHorizontalDragDistance &&
                Math.Abs(pos.Y - _restoreDragStart.Y) < SystemParameters.MinimumVerticalDragDistance) return;

            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();

            var ratioX = ActualWidth > 0 ? pos.X / ActualWidth : 0.5;
            var restoreWidth = RestoreBounds.Width;
            // Cursor position in screen DIPs (PointToScreen returns device pixels).
            var toDip = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice
                        ?? System.Windows.Media.Matrix.Identity;
            var cursor = toDip.Transform(PointToScreen(pos));

            WindowState = WindowState.Normal;
            Left = cursor.X - restoreWidth * ratioX;
            Top = cursor.Y - 9; // middle of the 18 px title bar
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
        {
            if (!_restoreOnDrag) return;
            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();
        }

        // Maximized: square corners. A transparent (AllowsTransparency) window has no invisible resize frame
        // hanging off the screen, so no extra padding is needed.
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            var maximized = WindowState == WindowState.Maximized;
            RootBorder.CornerRadius = maximized ? new CornerRadius(0) : new CornerRadius(8);
            TitleBarBorder.CornerRadius = maximized ? new CornerRadius(0) : new CornerRadius(8, 8, 0, 0);
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        private void OpenLinkText_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            OpenInDefaultBrowser(_headline.Link);
        }

        /// <summary>The URL as an absolute http/https URI, or null for anything else (file:, javascript:, custom protocols, junk).</summary>
        private static Uri? TryGetWebUri(string? url)
        {
            if (string.IsNullOrWhiteSpace(url)) return null;
            if (!Uri.TryCreate(url.Trim(), UriKind.Absolute, out var uri)) return null;
            return uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps ? uri : null;
        }

        /// <summary>Opens an http(s) link in the default browser; refuses (and logs) anything else.</summary>
        private static void OpenInDefaultBrowser(string? url)
        {
            var uri = TryGetWebUri(url);
            if (uri == null)
            {
                if (!string.IsNullOrWhiteSpace(url)) AppLog.Warn("ArticleWindow", "Refused to open a non-http(s) link: " + url);
                return;
            }
            try
            {
                Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
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
                ShowAiError("AI summary unavailable: the OpenRouter API key is not set. " +
                            "Enter it in ⚙ Keys & data (Hedge Fund window) — one key serves all modules.");
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
            catch (AiBudgetBlockedException ex)
            {
                // App-side daily AI cap (Shared/AiBudgetAppCaps.cs): refused BEFORE the call, nothing was
                // sent or charged. The message says why and where to raise the cap.
                try { AppLog.Info("ArticleSummary", "Summarize with AI blocked by the AI budget: " + ex.Reason); } catch { }
                ShowAiError(ex.Message);
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

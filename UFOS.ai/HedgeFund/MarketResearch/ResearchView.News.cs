using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// MR6 (SPEC "MR6 backend notes", plan §1.2 G / §7): the news card of the active symbol, Research settings
    /// ("Show news thumbnails" + the backend's privacy note, logo plan state) and symbol logos (header, watchlist
    /// rows, palette rows).
    ///
    /// Rules: headline / summary / publisher are plain text (TextBlocks only); a link opens in the default browser
    /// only when it is http/https (<see cref="NewsroomLinks.Open"/>); thumbnails and logos are loaded ONLY from the
    /// HedgeFund backend (127.0.0.1:8867 + the route path, token header) — never from a third-party URL; images are
    /// size-checked (bytes, signature, pixel size) before decoding, decoded to a small size, frozen and cached for the
    /// session. A news failure shows "News unavailable: &lt;reason&gt;" verbatim — no older list as current. A missing
    /// logo (LogosNotOnPlan / NoLogo / any non-200) shows the ticker initials: the normal empty state, not a data
    /// fallback.
    /// </summary>
    public partial class ResearchView
    {
        private const int NewsPageSize = 20;
        private const int MaxNewsKept = 200;
        private const int MaxNewsShown = 60;
        private const int MaxTickerChips = 8;
        private const int MaxThumbBytes = 512 * 1024;   // backend cap (media.py)
        private const int MaxThumbSide = 4096;
        private const int MaxLogoBytes = 256 * 1024;    // SPEC §4.4 MAX_LOGO_BYTES
        private const int MaxLogoSide = 1024;
        private const int MaxImageCacheEntries = 300;
        private const int MaxLogoRequestsInFlight = 2;
        private static readonly TimeSpan ImageTimeout = TimeSpan.FromSeconds(30);
        private static readonly Regex NewsImagePath = new(@"^/research/news/images/[0-9a-f]{64}$", RegexOptions.CultureInvariant);
        private static readonly Regex CryptoTag = new(@"^([A-Z0-9]{2,10})USD$", RegexOptions.CultureInvariant);

        // ---------- news state (active symbol) ----------
        private string? _newsSymbol;
        private readonly List<NewsItem> _news = new();
        private string? _newsError;        // "News unavailable: …" (the list is empty then)
        private string? _newsPageError;    // older page failed (the list stays)
        private bool _newsLoading;
        private bool _newsHasMore;
        private string? _newsNextBefore;
        private int _newsGeneration;
        private string? _newsSourceLabel;
        private string? _newsNote;
        private DispatcherTimer? _newsAgeTimer;
        private bool _newsRenderedOnce;

        // ---------- settings ----------
        private NewsSettings? _newsSettings;
        private bool _newsSettingsLoading;
        private bool _newsSettingsSaving;

        // ---------- images (session caches; frozen bitmaps) ----------
        private sealed class LogoEntry
        {
            public ImageSource? Image;
            public string? Error;
        }

        private readonly Dictionary<string, LogoEntry> _logoCache = new(StringComparer.Ordinal);
        private readonly Dictionary<string, List<WeakReference<Image>>> _logoTargets = new(StringComparer.Ordinal);
        private readonly Queue<string> _logoQueue = new();
        private readonly HashSet<string> _logoQueued = new(StringComparer.Ordinal);
        private int _logoInFlight;
        private string? _logosOffReason;   // LogosNotOnPlan (account-wide): no further logo requests this session
        private readonly Dictionary<string, ImageSource?> _thumbCache = new(StringComparer.Ordinal);
        private readonly Queue<string> _thumbOrder = new();
        private readonly Dictionary<string, string> _thumbErrors = new(StringComparer.Ordinal);
        private readonly Dictionary<string, List<Action<ImageSource?>>> _thumbWaiters = new(StringComparer.Ordinal);
        // Review M2 (2026-10-17): thumbnails are requested only for cards in (or near) the visible part of the news list,
        // at most 2 at a time; queued requests of cards that scrolled out or of the previous symbol are dropped; a 503
        // "busy" / 429 from the proxy is retried later (bounded) — the card simply has no picture meanwhile.
        private const int MaxThumbsInFlight = 2;
        private static readonly int[] ThumbBusyRetrySeconds = { 5, 15, 30 };
        private sealed class ThumbSlot
        {
            public string Path = "";
            public FrameworkElement Card = null!;
            public Action<ImageSource?> Apply = null!;
            public bool Requested;
        }
        private readonly List<ThumbSlot> _thumbSlots = new();
        private readonly List<string> _thumbQueue = new();
        private readonly HashSet<string> _thumbInFlight = new(StringComparer.Ordinal);
        private readonly Dictionary<string, int> _thumbBusyRetries = new(StringComparer.Ordinal);
        private int _thumbGeneration;

        // =====================================================================================
        // Active symbol → news (called from RenderHeader, the socket re-sync and the news topic)
        // =====================================================================================

        /// <summary>Follows the active tab: a new symbol clears the card and loads its first page; refreshes the header logo.</summary>
        private void NewsTrackActive()
        {
            var symbol = _active?.Symbol;
            RenderHeaderLogo();
            EnsureNewsAgeTimer();
            if (_newsSettings == null && !_newsSettingsLoading && IsShown) _ = LoadNewsSettingsAsync(); // logo plan state + thumbnails switch
            if (symbol == _newsSymbol)
            {
                if (!_newsRenderedOnce) { _newsRenderedOnce = true; RenderNews(); } // initial "No symbol open." state
                return;
            }
            _newsRenderedOnce = true;
            _newsSymbol = symbol;
            _news.Clear();
            _newsError = null;
            _newsPageError = null;
            _newsHasMore = false;
            _newsNextBefore = null;
            _newsLoading = false;
            _newsGeneration++;
            RenderNews();
            if (symbol != null) _ = LoadNewsAsync(symbol, older: false);
        }

        /// <summary>After a reconnect: items missed while disconnected are not replayed → re-fetch the first page.</summary>
        private void ReloadNews()
        {
            var symbol = _newsSymbol;
            if (symbol == null || _shutDown) return;
            _newsGeneration++;
            _newsLoading = false;
            _ = LoadNewsAsync(symbol, older: false);
        }

        private async Task LoadNewsAsync(string symbol, bool older)
        {
            if (older && (_newsLoading || !_newsHasMore || _newsNextBefore == null)) return;
            var generation = older ? _newsGeneration : ++_newsGeneration;
            _newsLoading = true;
            if (!older) _newsPageError = null;
            RenderNews();
            PlatformResult<NewsList> result;
            try { result = await ResearchClient.GetNewsAsync(symbol, NewsPageSize, older ? _newsNextBefore : null); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading news failed.", ex);
                result = new PlatformResult<NewsList> { Status = PlatformCallStatus.Error, Message = "unexpected error — see the Backend Log." };
            }
            if (_shutDown || generation != _newsGeneration || symbol != _newsSymbol) return;
            _newsLoading = false;
            if (!result.Ok || result.Value == null)
            {
                var text = result.Ok ? "empty response from the backend." : result.Message;
                var message = text.StartsWith("News unavailable", StringComparison.Ordinal) ? text : "News unavailable: " + text;
                if (older)
                {
                    _newsPageError = "Older news not loaded: " + text;
                }
                else
                {
                    _news.Clear(); // no older list shown as current (no fallback)
                    _newsError = message;
                    _newsHasMore = false;
                    _newsNextBefore = null;
                }
                RenderNews();
                return;
            }
            var v = result.Value;
            _newsError = null;
            if (!older) _newsPageError = null;
            if (!string.IsNullOrWhiteSpace(v.SourceLabel)) _newsSourceLabel = v.SourceLabel;
            if (!string.IsNullOrWhiteSpace(v.Note)) _newsNote = v.Note;
            if (!older) _news.Clear(); // the first page replaces everything (REST is authoritative after a (re)load)
            foreach (var item in v.Items ?? new List<NewsItem>()) MergeNews(item, fromStream: false);
            _newsHasMore = v.HasMore == true && !string.IsNullOrWhiteSpace(v.NextBefore);
            _newsNextBefore = v.NextBefore;
            RenderNews();
        }

        /// <summary>Adds or replaces (same id) one item; newest first; bounded. Returns false when it was dropped.</summary>
        private bool MergeNews(NewsItem item, bool fromStream)
        {
            if (item.Id is not long id || id <= 0 || string.IsNullOrWhiteSpace(item.Headline) || ParseUtc(item.CreatedAt) == null) return false;
            var index = _news.FindIndex(n => n.Id == id);
            if (index >= 0)
            {
                // A stream repeat with the same updated_at changes nothing; REST and newer corrections replace.
                if (fromStream && _news[index].UpdatedAt == item.UpdatedAt) return false;
                _news.RemoveAt(index);
            }
            var created = ParseUtc(item.CreatedAt)!.Value;
            var at = _news.FindIndex(n => (ParseUtc(n.CreatedAt) ?? DateTimeOffset.MinValue) < created ||
                                          ((ParseUtc(n.CreatedAt) ?? DateTimeOffset.MinValue) == created && (n.Id ?? 0) < id));
            if (at < 0) _news.Add(item);
            else _news.Insert(at, item);
            while (_news.Count > MaxNewsKept) _news.RemoveAt(_news.Count - 1);
            return true;
        }

        /// <summary>WS <c>news</c> (topic news:&lt;SYM&gt; of the active tab): insert or replace by id.</summary>
        private void Socket_NewsReceived(object? sender, WsNews message)
        {
            if (_shutDown || message.Item == null || _newsSymbol == null || _newsError != null) return;
            if (!NewsMatchesSymbol(message.Item, _newsSymbol)) return;
            if (MergeNews(message.Item, fromStream: true)) RenderNews();
        }

        /// <summary>The item's Benzinga tags contain the symbol (crypto tags are written <c>BTCUSD</c>).</summary>
        private static bool NewsMatchesSymbol(NewsItem item, string symbol)
        {
            var tags = item.Symbols ?? new List<string>();
            var compact = symbol.Replace("-", "", StringComparison.Ordinal);
            return tags.Any(t => string.Equals(t, symbol, StringComparison.OrdinalIgnoreCase) ||
                                 string.Equals(t, compact, StringComparison.OrdinalIgnoreCase) ||
                                 string.Equals(t.Replace("/", ".", StringComparison.Ordinal), symbol, StringComparison.OrdinalIgnoreCase));
        }

        // =====================================================================================
        // Rendering
        // =====================================================================================

        private void RenderNews()
        {
            ResetThumbnailSlots();
            NewsItemsPanel.Children.Clear();
            var symbol = _newsSymbol;
            var label = _newsSourceLabel ?? "Benzinga via Alpaca";
            NewsCaptionText.Text = "NEWS · " + label.ToUpperInvariant() + (symbol != null ? " · " + symbol : "");
            NewsCaptionText.ToolTip = _newsNote ?? "News for the open symbol from Alpaca (content by Benzinga). Plain text; links open in your browser.";
            AutomationProperties.SetName(NewsCaptionText, "News for " + (symbol ?? "no symbol") + ", source " + label);
            RenderNewsLiveChip();
            NewsMoreButton.Visibility = Visibility.Collapsed;
            if (symbol == null)
            {
                SetNewsMessage("No symbol open.");
                return;
            }
            if (_newsError != null)
            {
                SetNewsMessage(_newsError, retry: true);
                return;
            }
            if (_news.Count == 0)
            {
                SetNewsMessage(_newsLoading ? "Loading news for " + symbol + "…" : "No news for " + symbol + " in the loaded range.");
                return;
            }
            SetNewsMessage(_newsPageError, retry: _newsPageError != null);
            foreach (var item in _news.Take(MaxNewsShown)) NewsItemsPanel.Children.Add(BuildNewsCard(item));
            Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(RequestVisibleThumbnails)); // after layout
            if (_newsHasMore)
            {
                NewsMoreButton.Content = _newsLoading ? "Loading…" : "Older news";
                NewsMoreButton.IsEnabled = !_newsLoading;
                NewsMoreButton.Visibility = Visibility.Visible;
            }
        }

        private void SetNewsMessage(string? text, bool retry = false)
        {
            NewsMessageText.Text = text ?? "";
            NewsMessageText.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
            if (!string.IsNullOrEmpty(text)) AutomationProperties.SetName(NewsMessageText, text);
            NewsRetryButton.Visibility = retry && _newsSymbol != null ? Visibility.Visible : Visibility.Collapsed;
        }

        /// <summary>The news STREAM state (feeds row "news", MR2b) — REST failures are shown on the card itself.</summary>
        private void RenderNewsLiveChip()
        {
            if (_newsSymbol == null) { NewsLiveChip.Visibility = Visibility.Collapsed; return; }
            var feed = _lastFeeds?.Feeds?.FirstOrDefault(f => f.Id == "news");
            string text, brush, tip;
            if (_socket.State != ResearchSocketState.Connected)
            {
                text = "NOT LIVE";
                brush = "MutedForegroundBrush";
                tip = "New items do not arrive while the live connection is down: " + _socket.DescribeState() + " The list is re-loaded after the reconnect.";
            }
            else if (feed?.State == "live")
            {
                text = "● LIVE";
                brush = "PositiveGreenBrush";
                tip = "New items arrive over the live news stream. " + feed.DescriptionText;
            }
            else
            {
                text = "NOT LIVE";
                brush = feed?.State == "unavailable" ? "AccentRedBrush" : "CautionBrush";
                tip = feed != null ? feed.DescriptionText : "The news stream state is not known yet.";
            }
            NewsLiveChipText.Text = text;
            NewsLiveChipText.Foreground = Token(brush);
            NewsLiveChip.ToolTip = tip;
            AutomationProperties.SetName(NewsLiveChip, "News " + text + ". " + tip);
            NewsLiveChip.Visibility = Visibility.Visible;
        }

        private UIElement BuildNewsCard(NewsItem item)
        {
            var grid = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var text = new StackPanel();
            var created = ParseUtc(item.CreatedAt);
            var source = item.SourceLabel ?? _newsSourceLabel ?? "Benzinga via Alpaca";
            var linkHost = SafeLinkHost(item.Url);

            // Headline: plain text; a button only when there is a safe http(s) link.
            var headline = new TextBlock
            {
                Text = item.Headline ?? "",
                FontSize = 12,
                FontWeight = FontWeights.SemiBold,
                Foreground = new SolidColorBrush(Color.FromRgb(0xE6, 0xE6, 0xE6)),
                TextWrapping = TextWrapping.Wrap,
                MaxHeight = 34,
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            var published = created is { } c ? LocalDateTime(c.ToString("o", CultureInfo.InvariantCulture)) : null;
            var updated = item.UpdatedAt != null && item.UpdatedAt != item.CreatedAt ? LocalDateTime(item.UpdatedAt) : null;
            var timeTip = (published != null ? "Published " + published + " (local time)" : "") + (updated != null ? ", updated " + updated : "");
            if (linkHost != null)
            {
                var url = item.Url!;
                var open = new Button
                {
                    Style = (Style)Resources["ListRowButtonStyle"],
                    Content = headline,
                    Padding = new Thickness(2, 1, 2, 1),
                    HorizontalContentAlignment = HorizontalAlignment.Left,
                    ToolTip = "Open the original article in your browser (" + linkHost + ")." + (timeTip.Length > 0 ? "\n" + timeTip : ""),
                };
                AutomationProperties.SetName(open, "News: " + item.Headline + ". Opens " + linkHost + " in the browser.");
                open.Click += (_, _) => NewsroomLinks.Open(url); // http/https only (checked again there)
                text.Children.Add(open);
            }
            else
            {
                headline.Margin = new Thickness(3, 1, 3, 1);
                headline.ToolTip = "No link available for this item." + (timeTip.Length > 0 ? "\n" + timeTip : "");
                AutomationProperties.SetName(headline, "News: " + item.Headline + " (no link)");
                text.Children.Add(headline);
            }

            // Meta line: source · age · ticker chips (each opens its symbol).
            var meta = new WrapPanel { Margin = new Thickness(3, 2, 0, 0) };
            var age = new TextBlock
            {
                Text = source + " · " + (created is { } c2 ? RelativeAge(c2) : "time unknown") +
                       (string.IsNullOrWhiteSpace(item.Publisher) ? "" : " · " + item.Publisher),
                FontFamily = new FontFamily("Consolas"),
                FontSize = 9.5,
                Foreground = Token("MutedForegroundBrush"),
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0, 0, 8, 0),
                ToolTip = timeTip.Length > 0 ? timeTip : null,
                Tag = created,
            };
            age.SetValue(FrameworkElement.NameProperty, "NewsAge");
            meta.Children.Add(age);
            var tags = (item.Symbols ?? new List<string>()).Where(t => !string.IsNullOrWhiteSpace(t)).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
            foreach (var tag in tags.Take(MaxTickerChips))
            {
                var t = tag.ToUpperInvariant();
                var chip = new Button
                {
                    Style = (Style)Resources["IconButtonStyle"],
                    Content = t,
                    FontFamily = new FontFamily("Consolas"),
                    FontSize = 9.5,
                    Padding = new Thickness(4, 0, 4, 0),
                    Margin = new Thickness(0, 0, 3, 0),
                    BorderBrush = Token("AccentBorderBrush"),
                    ToolTip = "Open " + t,
                };
                AutomationProperties.SetName(chip, "Open the symbol " + t);
                chip.Click += (_, _) => _ = OpenNewsTagAsync(t);
                meta.Children.Add(chip);
            }
            if (tags.Count > MaxTickerChips)
            {
                meta.Children.Add(new TextBlock
                {
                    Text = "+" + (tags.Count - MaxTickerChips),
                    FontFamily = new FontFamily("Consolas"),
                    FontSize = 9.5,
                    Foreground = Token("MutedForegroundBrush"),
                    VerticalAlignment = VerticalAlignment.Center,
                    ToolTip = string.Join(" ", tags.Skip(MaxTickerChips)),
                });
            }
            text.Children.Add(meta);

            if (!string.IsNullOrWhiteSpace(item.Summary))
            {
                text.Children.Add(new TextBlock
                {
                    Text = item.Summary,
                    FontSize = 10.5,
                    Foreground = Token("SecondaryForegroundBrush"),
                    TextWrapping = TextWrapping.Wrap,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                    MaxHeight = 30,
                    Margin = new Thickness(3, 2, 0, 0),
                });
            }
            grid.Children.Add(text);

            // Thumbnail: ONLY through the backend proxy (images[].path), never the original URL.
            var path = PickThumbnailPath(item);
            if (path != null)
            {
                var image = new Image
                {
                    Width = 96,
                    Height = 54,
                    Stretch = Stretch.UniformToFill,
                    Visibility = Visibility.Collapsed,
                };
                var frame = new Border
                {
                    Width = 96,
                    Height = 54,
                    CornerRadius = new CornerRadius(5),
                    Background = Token("WindowBackgroundBrush"),
                    BorderBrush = Token("AccentBorderBrush"),
                    BorderThickness = new Thickness(1),
                    Margin = new Thickness(10, 2, 0, 0),
                    VerticalAlignment = VerticalAlignment.Top,
                    ClipToBounds = true,
                    Child = image,
                    Visibility = Visibility.Collapsed,
                };
                AutomationProperties.SetName(frame, "Thumbnail of the news item");
                Grid.SetColumn(frame, 1);
                grid.Children.Add(frame);
                RegisterThumbnail(path, grid, source2 =>
                {
                    if (source2 == null)
                    {
                        frame.Visibility = Visibility.Collapsed; // a card without a picture (reason logged + in the cache)
                        return;
                    }
                    image.Source = source2;
                    image.Visibility = Visibility.Visible;
                    frame.Visibility = Visibility.Visible;
                    frame.ToolTip = "Thumbnail loaded by the HedgeFund backend (proxy); switch thumbnails off in ⚙ Research settings.";
                });
            }
            AutomationProperties.SetName(grid, item.Headline + ", " + source + ", " + (created is { } c3 ? RelativeAge(c3) : ""));
            return grid;
        }

        private static string? PickThumbnailPath(NewsItem item)
        {
            var images = item.Images ?? new List<NewsImage>();
            var pick = images.FirstOrDefault(i => i.Size == "thumb") ?? images.FirstOrDefault(i => i.Size == "small");
            return pick?.Path != null && NewsImagePath.IsMatch(pick.Path) ? pick.Path : null;
        }

        /// <summary>The host of an http/https link (shown as the link target), or null when the link is not safe to open.</summary>
        private static string? SafeLinkHost(string? url)
        {
            if (string.IsNullOrWhiteSpace(url) || url.Length > 1000) return null;
            if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)) return null;
            if (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps) return null;
            if (!string.IsNullOrEmpty(uri.UserInfo) || string.IsNullOrEmpty(uri.Host)) return null;
            return uri.Host;
        }

        private static DateTimeOffset? ParseUtc(string? iso)
        {
            if (string.IsNullOrWhiteSpace(iso)) return null;
            return DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto) ? dto : null;
        }

        private static string RelativeAge(DateTimeOffset created)
        {
            var span = DateTimeOffset.UtcNow - created;
            if (span < TimeSpan.FromMinutes(1)) return "just now";
            if (span < TimeSpan.FromHours(1)) return ((int)span.TotalMinutes).ToString(CultureInfo.InvariantCulture) + " min ago";
            if (span < TimeSpan.FromDays(1)) return ((int)span.TotalHours).ToString(CultureInfo.InvariantCulture) + " h ago";
            return ((int)span.TotalDays).ToString(CultureInfo.InvariantCulture) + " d ago";
        }

        /// <summary>Ages tick while shown (every 30 s) without rebuilding the cards; the live chip follows the feed state.</summary>
        private void EnsureNewsAgeTimer()
        {
            if (_newsAgeTimer != null) return;
            _newsAgeTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromSeconds(30) };
            _newsAgeTimer.Tick += (_, _) =>
            {
                if (!IsShown || _shutDown) return;
                RenderNewsLiveChip();
                foreach (var card in NewsItemsPanel.Children.OfType<Grid>())
                {
                    if (card.Children.OfType<StackPanel>().FirstOrDefault()?.Children.OfType<WrapPanel>().FirstOrDefault()?
                            .Children.OfType<TextBlock>().FirstOrDefault(t => t.Name == "NewsAge") is { Tag: DateTimeOffset created } age)
                    {
                        var text = age.Text;
                        var cut = text.IndexOf(" · ", StringComparison.Ordinal);
                        if (cut < 0) continue;
                        var rest = text.Substring(cut + 3);
                        var after = rest.IndexOf(" · ", StringComparison.Ordinal);
                        age.Text = text.Substring(0, cut) + " · " + RelativeAge(created) + (after >= 0 ? rest.Substring(after) : "");
                    }
                }
            };
            _newsAgeTimer.Start();
        }

        private void NewsMoreButton_Click(object sender, RoutedEventArgs e)
        {
            if (_newsSymbol != null) _ = LoadNewsAsync(_newsSymbol, older: true);
        }

        private void NewsRetryButton_Click(object sender, RoutedEventArgs e)
        {
            if (_newsSymbol == null) return;
            if (_newsError != null)
            {
                _newsError = null;
                ReloadNews();
            }
            else
            {
                _newsPageError = null;
                _ = LoadNewsAsync(_newsSymbol, older: true);
            }
        }

        /// <summary>
        /// A ticker chip: open the tag when Alpaca's asset list has it exactly (crypto tags are written BTCUSD → BTC-USD is
        /// looked up too). A lookup in the asset list — no guess; nothing found → a message.
        /// </summary>
        private async Task OpenNewsTagAsync(string tag)
        {
            var candidates = new List<string> { tag.Replace("/", ".", StringComparison.Ordinal) };
            if (CryptoTag.Match(tag) is { Success: true } m) candidates.Add(m.Groups[1].Value + "-USD");
            foreach (var candidate in candidates)
            {
                if (!TickerInput.IsMatch(candidate)) continue;
                PlatformResult<PaletteResult> result;
                try { result = await ResearchClient.GetPaletteAsync(candidate, 5); }
                catch (Exception ex)
                {
                    AppLog.Warn(LogModule, "Market Research: looking up a news ticker failed.", ex);
                    ShowWorkspaceMessage("Could not open " + tag + " — see the Backend Log.");
                    return;
                }
                if (_shutDown) return;
                if (!result.Ok)
                {
                    ShowWorkspaceMessage("Could not open " + tag + ": " + result.Message);
                    return;
                }
                var hit = result.Value?.Items?.FirstOrDefault(i => string.Equals(i.Symbol, candidate, StringComparison.OrdinalIgnoreCase));
                if (hit?.Symbol != null)
                {
                    OpenSymbol(hit.Symbol, hit.Name, hit.AssetClass, hit.Kind, hit.Exchange);
                    return;
                }
            }
            ShowWorkspaceMessage(tag + " (from the news tags) is not in Alpaca's asset list — it cannot be opened here.");
        }

        // =====================================================================================
        // Thumbnails (backend proxy only)
        // =====================================================================================

        private void LoadThumbnail(string path, Action<ImageSource?> apply)
        {
            if (_thumbCache.TryGetValue(path, out var cached))
            {
                apply(cached);
                return;
            }
            if (_thumbWaiters.TryGetValue(path, out var waiters))
            {
                waiters.Add(apply);
                return;
            }
            _thumbWaiters[path] = new List<Action<ImageSource?>> { apply };
            if (!_thumbInFlight.Contains(path) && !_thumbQueue.Contains(path)) _thumbQueue.Add(path);
            PumpThumbnails();
        }

        /// <summary>A card with a thumbnail: shown at once from the session cache, else requested when it is (nearly) visible.</summary>
        private void RegisterThumbnail(string path, FrameworkElement card, Action<ImageSource?> apply)
        {
            if (_thumbCache.TryGetValue(path, out var cached))
            {
                apply(cached);
                return;
            }
            _thumbSlots.Add(new ThumbSlot { Path = path, Card = card, Apply = apply });
        }

        /// <summary>Every re-render (new symbol, new items): queued requests and waiting cards of the old list are dropped.</summary>
        private void ResetThumbnailSlots()
        {
            _thumbGeneration++;
            _thumbSlots.Clear();
            _thumbQueue.Clear();
            _thumbWaiters.Clear(); // in-flight answers still fill the cache, they just have no card to update
        }

        private void NewsScroll_ScrollChanged(object sender, ScrollChangedEventArgs e) => RequestVisibleThumbnails();

        /// <summary>Requests thumbnails of cards in the visible part of the list (± 120 px); un-queues cards that scrolled out.</summary>
        private void RequestVisibleThumbnails()
        {
            if (_shutDown || _thumbSlots.Count == 0) return;
            var viewport = new Rect(0, -120, Math.Max(0, NewsScroll.ViewportWidth), Math.Max(0, NewsScroll.ViewportHeight) + 240);
            foreach (var slot in _thumbSlots)
            {
                var visible = false;
                try
                {
                    if (slot.Card.IsVisible && NewsScroll.IsAncestorOf(slot.Card))
                    {
                        var bounds = slot.Card.TransformToAncestor(NewsScroll).TransformBounds(new Rect(0, 0, slot.Card.ActualWidth, slot.Card.ActualHeight));
                        visible = bounds.IntersectsWith(viewport);
                    }
                }
                catch (InvalidOperationException) { visible = false; }
                if (visible && !slot.Requested)
                {
                    slot.Requested = true;
                    LoadThumbnail(slot.Path, slot.Apply);
                }
                else if (!visible && slot.Requested && _thumbQueue.Remove(slot.Path))
                {
                    // Scrolled out before its request started: drop it (asked again when it comes back).
                    slot.Requested = false;
                    _thumbWaiters.Remove(slot.Path);
                }
            }
        }

        private void PumpThumbnails()
        {
            while (_thumbInFlight.Count < MaxThumbsInFlight && _thumbQueue.Count > 0 && !_shutDown)
            {
                var path = _thumbQueue[0];
                _thumbQueue.RemoveAt(0);
                if (!_thumbWaiters.ContainsKey(path)) continue; // nobody waits any more
                _thumbInFlight.Add(path);
                _ = FetchThumbnailAsync(path);
            }
        }

        /// <summary>The proxy is busy (503 "busy" / 429): retry later, bounded; true = a retry was scheduled.</summary>
        private bool RetryThumbnailLater(string path)
        {
            var attempt = _thumbBusyRetries.TryGetValue(path, out var n) ? n : 0;
            if (attempt >= ThumbBusyRetrySeconds.Length) return false;
            _thumbBusyRetries[path] = attempt + 1;
            var generation = _thumbGeneration;
            var timer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromSeconds(ThumbBusyRetrySeconds[attempt]) };
            timer.Tick += (_, _) =>
            {
                timer.Stop();
                if (_shutDown || generation != _thumbGeneration || !_thumbWaiters.ContainsKey(path)) return; // card gone / list re-rendered
                if (!_thumbInFlight.Contains(path) && !_thumbQueue.Contains(path)) _thumbQueue.Add(path);
                PumpThumbnails();
            };
            timer.Start();
            return true;
        }

        private async Task FetchThumbnailAsync(string path)
        {
            ImageSource? image = null;
            string? error = null;
            var busy = false;
            try
            {
                var result = await ResearchClient.GetBinaryAsync(path, MaxThumbBytes, ImageTimeout);
                busy = !result.Ok && (result.Status == 429 ||
                                      (result.Status == 503 && (result.Kind == "ThumbnailBusy" ||
                                                                (result.Error ?? "").Contains("busy", StringComparison.OrdinalIgnoreCase))));
                if (!result.Ok || result.Bytes == null)
                {
                    // 409 ThumbnailsOff / 503 ThumbnailUnavailable / 404 UnknownImage: no picture (normal for a card).
                    error = result.Error ?? "no image";
                }
                else
                {
                    var bytes = result.Bytes;
                    var decoded = await Task.Run(() => DecodeImage(bytes, MaxThumbBytes, MaxThumbSide, 192));
                    image = decoded.Image;
                    error = decoded.Error;
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading a news thumbnail failed.", ex);
                error = "unexpected error";
            }
            finally
            {
                _thumbInFlight.Remove(path);
            }
            if (_shutDown) return;
            if (busy && RetryThumbnailLater(path))
            {
                PumpThumbnails(); // no picture meanwhile — not an error (the waiting card keeps its slot)
                return;
            }
            _thumbBusyRetries.Remove(path);
            if (error != null)
            {
                _thumbErrors[path] = error;
                AppLog.Warn(LogModule, "Market Research: news thumbnail not shown: " + error);
            }
            // Session cache (bounded); a "thumbnails off" answer is not cached (the setting may change).
            if (!(error != null && error.Contains("switched off", StringComparison.OrdinalIgnoreCase)))
            {
                _thumbCache[path] = image;
                _thumbOrder.Enqueue(path);
                while (_thumbOrder.Count > MaxImageCacheEntries) _thumbCache.Remove(_thumbOrder.Dequeue());
            }
            if (_thumbWaiters.Remove(path, out var waiters))
                foreach (var apply in waiters)
                {
                    try { apply(image); }
                    catch (Exception ex) { AppLog.Warn(LogModule, "Market Research: showing a thumbnail failed.", ex); }
                }
            PumpThumbnails();
        }

        /// <summary>
        /// Size-checked decoding: bytes ≤ cap, PNG/JPEG/GIF signature, pixel size ≤ cap read from the header BEFORE
        /// the full decode, decoded down to <paramref name="decodeWidth"/> px, frozen (usable on the UI thread).
        /// Runs on a worker thread.
        /// </summary>
        private static (ImageSource? Image, string? Error) DecodeImage(byte[] bytes, int maxBytes, int maxSide, int decodeWidth)
        {
            if (bytes.Length == 0 || bytes.Length > maxBytes) return (null, "image size outside the limit");
            if (!HasImageSignature(bytes)) return (null, "not a PNG, JPEG or GIF image");
            try
            {
                int width, height;
                using (var probe = new MemoryStream(bytes, writable: false))
                {
                    var decoder = BitmapDecoder.Create(probe, BitmapCreateOptions.IgnoreColorProfile | BitmapCreateOptions.DelayCreation,
                                                       BitmapCacheOption.None);
                    if (decoder.Frames.Count == 0) return (null, "the image has no frame");
                    width = decoder.Frames[0].PixelWidth;
                    height = decoder.Frames[0].PixelHeight;
                }
                if (width <= 0 || height <= 0 || width > maxSide || height > maxSide) return (null, "image dimensions outside the limit");
                var bitmap = new BitmapImage();
                using (var stream = new MemoryStream(bytes, writable: false))
                {
                    bitmap.BeginInit();
                    bitmap.CacheOption = BitmapCacheOption.OnLoad; // decoded now; the stream can be closed
                    bitmap.CreateOptions = BitmapCreateOptions.IgnoreColorProfile;
                    if (decodeWidth > 0 && width > decodeWidth) bitmap.DecodePixelWidth = decodeWidth;
                    bitmap.StreamSource = stream;
                    bitmap.EndInit();
                }
                bitmap.Freeze();
                return (bitmap, null);
            }
            catch (Exception ex) when (ex is NotSupportedException || ex is FileFormatException || ex is ArgumentException ||
                                       ex is InvalidOperationException || ex is IOException || ex is OverflowException ||
                                       ex is System.Runtime.InteropServices.COMException)
            {
                return (null, "the image could not be decoded (" + ex.GetType().Name + ")");
            }
        }

        private static bool HasImageSignature(byte[] b)
        {
            if (b.Length >= 8 && b[0] == 0x89 && b[1] == 0x50 && b[2] == 0x4E && b[3] == 0x47 && b[4] == 0x0D && b[5] == 0x0A && b[6] == 0x1A && b[7] == 0x0A)
                return true; // PNG
            if (b.Length >= 3 && b[0] == 0xFF && b[1] == 0xD8 && b[2] == 0xFF) return true; // JPEG
            if (b.Length >= 6 && b[0] == (byte)'G' && b[1] == (byte)'I' && b[2] == (byte)'F' && b[3] == (byte)'8' &&
                (b[4] == (byte)'7' || b[4] == (byte)'9') && b[5] == (byte)'a') return true; // GIF
            return false; // WebP is not accepted (review L6, 2026-10-17: the backend proxy no longer passes image/webp)
        }

        private static bool IsPng(byte[] b)
            => b.Length >= 8 && b[0] == 0x89 && b[1] == 0x50 && b[2] == 0x4E && b[3] == 0x47 && b[4] == 0x0D && b[5] == 0x0A && b[6] == 0x1A && b[7] == 0x0A;

        // =====================================================================================
        // Logos (GET /research/symbols/{s}/logo; initials = the normal empty state)
        // =====================================================================================

        /// <summary>A small logo badge: ticker initials, replaced by the logo once (and if) the backend has one.</summary>
        private FrameworkElement LogoBadge(string symbol, double size)
        {
            var initials = new TextBlock
            {
                Text = Initials(symbol),
                FontFamily = new FontFamily("Consolas"),
                FontSize = Math.Max(7, size * 0.45),
                Foreground = Token("MutedForegroundBrush"),
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
            var image = new Image { Width = size, Height = size, Stretch = Stretch.Uniform, Visibility = Visibility.Collapsed, Tag = initials };
            var grid = new Grid();
            grid.Children.Add(initials);
            grid.Children.Add(image);
            var badge = new Border
            {
                Width = size,
                Height = size,
                CornerRadius = new CornerRadius(3),
                Background = Token("WindowBackgroundBrush"),
                BorderBrush = Token("AccentBorderBrush"),
                BorderThickness = new Thickness(1),
                Margin = new Thickness(0, 0, 5, 0),
                VerticalAlignment = VerticalAlignment.Center,
                ClipToBounds = true,
                Child = grid,
            };
            AutomationProperties.SetName(badge, "Logo " + symbol);
            AttachLogo(symbol, image);
            return badge;
        }

        /// <summary>Wraps a ticker TextBlock (its grid column is kept) with a logo badge in front of it.</summary>
        private FrameworkElement WithLogo(string symbol, FrameworkElement ticker, double size)
        {
            var column = Grid.GetColumn(ticker);
            var dock = new DockPanel { LastChildFill = true, VerticalAlignment = VerticalAlignment.Center };
            Grid.SetColumn(dock, column);
            Grid.SetColumn(ticker, 0);
            var badge = LogoBadge(symbol, size);
            DockPanel.SetDock(badge, Dock.Left);
            dock.Children.Add(badge);
            dock.Children.Add(ticker);
            return dock;
        }

        /// <summary>The header's 40 px box: initials (XAML) or the logo.</summary>
        private void RenderHeaderLogo()
        {
            var tab = _active;
            LogoImage.Source = null;
            LogoImage.Visibility = Visibility.Collapsed;
            LogoText.Visibility = Visibility.Visible;
            LogoImage.Tag = LogoText;
            if (tab == null) return;
            LogoBox.ToolTip = "Logo placeholder (ticker initials).";
            AttachLogo(tab.Symbol, LogoImage);
        }

        private void AttachLogo(string symbol, Image target)
        {
            if (_logoCache.TryGetValue(symbol, out var entry))
            {
                ApplyLogo(target, symbol, entry);
                return;
            }
            if (_logosOffReason != null)
            {
                ApplyLogo(target, symbol, new LogoEntry { Error = _logosOffReason });
                return;
            }
            if (!_logoTargets.TryGetValue(symbol, out var list)) _logoTargets[symbol] = list = new List<WeakReference<Image>>();
            list.RemoveAll(w => !w.TryGetTarget(out _));
            list.Add(new WeakReference<Image>(target));
            if (_logoQueued.Add(symbol))
            {
                _logoQueue.Enqueue(symbol);
                PumpLogos();
            }
        }

        private void ApplyLogo(Image target, string symbol, LogoEntry entry)
        {
            var initials = target.Tag as TextBlock;
            if (entry.Image != null)
            {
                target.Source = entry.Image;
                target.Visibility = Visibility.Visible;
                if (initials != null) initials.Visibility = Visibility.Collapsed;
                if (ReferenceEquals(target, LogoImage))
                {
                    LogoBox.ToolTip = "Logo of " + symbol + " (Alpaca, via the HedgeFund backend).";
                    AutomationProperties.SetName(LogoBox, "Logo of " + symbol);
                }
            }
            else
            {
                target.Source = null;
                target.Visibility = Visibility.Collapsed;
                if (initials != null) initials.Visibility = Visibility.Visible;
                if (ReferenceEquals(target, LogoImage))
                {
                    LogoBox.ToolTip = "No logo — ticker initials shown." + (entry.Error != null ? "\n" + entry.Error : "");
                    AutomationProperties.SetName(LogoBox, "Logo placeholder: " + LogoText.Text);
                }
            }
        }

        private void PumpLogos()
        {
            while (_logoInFlight < MaxLogoRequestsInFlight && _logoQueue.Count > 0 && !_shutDown)
            {
                var symbol = _logoQueue.Dequeue();
                _logoInFlight++;
                _ = FetchLogoAsync(symbol);
            }
        }

        private async Task FetchLogoAsync(string symbol)
        {
            var entry = new LogoEntry();
            try
            {
                if (_logosOffReason != null)
                {
                    entry.Error = _logosOffReason;
                }
                else
                {
                    var result = await ResearchClient.GetBinaryAsync("/research/symbols/" + Uri.EscapeDataString(symbol) + "/logo", MaxLogoBytes, ImageTimeout);
                    if (!result.Ok || result.Bytes == null)
                    {
                        entry.Error = result.Error ?? "no logo";
                        // Account-wide refusal: no further logo requests this session (initials everywhere).
                        if (result.Kind == "LogosNotOnPlan") _logosOffReason = result.Error ?? "Logos are not available on this Alpaca plan.";
                    }
                    else if (result.ContentType != "image/png" || !IsPng(result.Bytes))
                    {
                        entry.Error = "the backend answered with something that is not a PNG logo";
                    }
                    else
                    {
                        var bytes = result.Bytes;
                        var decoded = await Task.Run(() => DecodeImage(bytes, MaxLogoBytes, MaxLogoSide, 96));
                        entry.Image = decoded.Image;
                        entry.Error = decoded.Error;
                    }
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading a logo failed.", ex);
                entry.Error = "unexpected error — see the Backend Log";
            }
            finally
            {
                _logoInFlight--;
            }
            if (_shutDown) return;
            _logoQueued.Remove(symbol);
            if (_logoCache.Count >= MaxImageCacheEntries) _logoCache.Clear(); // bounded session cache
            _logoCache[symbol] = entry;
            if (_logoTargets.Remove(symbol, out var targets))
                foreach (var weak in targets)
                    if (weak.TryGetTarget(out var image)) ApplyLogo(image, symbol, entry);
            if (_logosOffReason != null)
            {
                // Everything still queued gets the initials without a request.
                while (_logoQueue.Count > 0)
                {
                    var queued = _logoQueue.Dequeue();
                    _logoQueued.Remove(queued);
                    var off = new LogoEntry { Error = _logosOffReason };
                    _logoCache[queued] = off;
                    if (_logoTargets.Remove(queued, out var waiting))
                        foreach (var weak in waiting)
                            if (weak.TryGetTarget(out var image)) ApplyLogo(image, queued, off);
                }
            }
            PumpLogos();
        }

        // =====================================================================================
        // Research settings (⚙ in the status strip): "Show news thumbnails" + privacy note + logo state
        // =====================================================================================

        private async Task LoadNewsSettingsAsync()
        {
            if (_newsSettingsLoading) return;
            _newsSettingsLoading = true;
            try
            {
                var result = await ResearchClient.GetNewsSettingsAsync();
                if (_shutDown) return;
                if (result.Ok && result.Value != null)
                {
                    _newsSettings = result.Value;
                    if (result.Value.Logos?.State == "not_on_plan" && _logosOffReason == null)
                        _logosOffReason = "Logos are not available on this Alpaca plan: " + (result.Value.Logos.Reason ?? "no reason was reported.");
                    SetSettingsMessage(null);
                }
                else
                {
                    _newsSettings = null;
                    SetSettingsMessage("Settings not loaded: " + (result.Ok ? "empty response from the backend." : result.Message));
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading the Research settings failed.", ex);
                SetSettingsMessage("Settings not loaded — see the Backend Log.");
            }
            finally
            {
                _newsSettingsLoading = false;
            }
            RenderSettings();
        }

        private void SettingsButton_Click(object sender, RoutedEventArgs e)
        {
            RenderSettings();
            SettingsPopup.IsOpen = true;
            _ = LoadNewsSettingsAsync();
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() =>
            {
                if (SettingsPopup.IsOpen) ThumbnailsToggle.Focus();
            }));
        }

        private void SettingsPopup_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key != Key.Escape) return;
            e.Handled = true;
            SettingsPopup.IsOpen = false;
            SettingsButton.Focus();
        }

        private void RenderSettings()
        {
            var s = _newsSettings;
            var on = s?.Thumbnails;
            ThumbnailsToggle.IsEnabled = s != null && !_newsSettingsSaving;
            ThumbnailsToggleText.Text = (on == true ? "☑ " : "☐ ") + "Show news thumbnails";
            var state = on == null ? "unknown" : on.Value ? "on" : "off";
            AutomationProperties.SetName(ThumbnailsToggle, "Show news thumbnails, currently " + state + ". Press to switch.");
            ThumbnailsNoteText.Text = s?.Note ?? (_newsSettingsLoading ? "Loading…" : "");
            var logos = s?.Logos;
            LogosStateText.Text = logos == null
                ? ""
                : logos.State switch
                {
                    "available" => "Logos: available from Alpaca.",
                    "not_on_plan" => "Logos: not on this Alpaca plan — ticker initials are shown. " + (logos.Reason ?? ""),
                    "unknown" => "Logos: not checked yet (asked with the first logo).",
                    _ => "Logos: state not reported.",
                };
            LogosStateText.Visibility = string.IsNullOrEmpty(LogosStateText.Text) ? Visibility.Collapsed : Visibility.Visible;
        }

        private void SetSettingsMessage(string? text)
        {
            SettingsMessageText.Text = text ?? "";
            SettingsMessageText.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
        }

        private async void ThumbnailsToggle_Click(object sender, RoutedEventArgs e)
        {
            var s = _newsSettings;
            if (s?.Thumbnails is not bool current || _newsSettingsSaving) return;
            _newsSettingsSaving = true;
            RenderSettings();
            try
            {
                var result = await ResearchClient.PutNewsSettingsAsync(!current);
                if (_shutDown) return;
                if (result.Ok && result.Value != null)
                {
                    _newsSettings = result.Value;
                    SetSettingsMessage(result.Value.Thumbnails == true
                        ? "Thumbnails on — loaded by the backend (proxy)."
                        : "Thumbnails off — no image request is made.");
                    _thumbCache.Clear();
                    _thumbOrder.Clear();
                    _thumbErrors.Clear();
                    ReloadNews(); // image paths appear / disappear with the setting
                }
                else
                {
                    SetSettingsMessage("Not saved: " + (result.Ok ? "empty response from the backend." : result.Message));
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: saving the thumbnails setting failed.", ex);
                SetSettingsMessage("Not saved — see the Backend Log.");
            }
            finally
            {
                _newsSettingsSaving = false;
                RenderSettings();
                ThumbnailsToggle.Focus();
            }
        }
    }
}

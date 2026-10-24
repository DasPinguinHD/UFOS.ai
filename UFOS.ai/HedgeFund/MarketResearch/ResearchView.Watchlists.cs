using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// MR4 watchlists in the sidebar (SPEC "MR4 backend notes"): several lists (groups), collapsible, counts, add via
    /// ☆ / "+" (palette), remove, reorder (▲/▼ buttons in edit mode, Alt+↑/↓ and Delete on a focused row), rename,
    /// move and delete lists. Live price + % vs previous close with ▲/▼ from GET …/quotes (live state, the hub's last
    /// stream trade, previous close — never a REST snapshot) and the WebSocket (`watchlist:&lt;id&gt;` topic per shown,
    /// expanded list → `trade` / `symbol_status`). A symbol that is not live (stream limit, not covered, stream off)
    /// shows its reason; nothing is filled in from another source (Q10).
    /// Every change is confirmed by the backend (its answer carries all lists); changes run one after another.
    /// </summary>
    public partial class ResearchView
    {
        private const int DefaultMaxLists = 20;      // SPEC §4.4 MAX_WATCHLISTS (the backend's `limits` win)
        private const int DefaultMaxItems = 200;     // MAX_WATCHLIST_ITEMS
        private const int DefaultMaxNameChars = 40;  // MAX_NAME_CHARS

        private List<WatchlistDto> _lists = new();
        private WatchlistLimits? _listLimits;
        private LoadState _listsLoad = LoadState.NotLoaded;
        private DateTime _listsAttemptUtc = DateTime.MinValue;
        private string? _editingListId;
        private string? _confirmDeleteListId;
        private Task _listOps = Task.CompletedTask;
        private (string ListId, string Symbol)? _focusRowAfterRender;
        private string? _focusListHeaderAfterRender;

        /// <summary>Per symbol: live state, last stream trade, previous close (from …/quotes and the socket).</summary>
        private readonly Dictionary<string, WatchQuote> _quotes = new(StringComparer.Ordinal);
        private readonly Dictionary<string, List<WatchRowParts>> _rowParts = new(StringComparer.Ordinal);
        private readonly Dictionary<string, int> _quoteGeneration = new(StringComparer.Ordinal);
        private readonly HashSet<string> _quotesInFlight = new(StringComparer.Ordinal);
        private string? _quotesNote;

        private sealed class WatchQuote
        {
            public bool? Live;
            public string? Reason;
            public double? Last;
            public bool LastFromSocket;
            public string? LastTs;
            public NumValue? LastNum;
            public NumValue? PrevClose;
            public string? Name;
            public bool Covered = true;
        }

        private sealed class WatchRowParts
        {
            public string ListId = "";
            public string Symbol = "";
            public Button Row = null!;
            public TextBlock Arrow = null!;
            public TextBlock Ticker = null!;
            public TextBlock Price = null!;
            public TextBlock Pct = null!;
            public TextBlock Reason = null!;
        }

        private int MaxLists => _listLimits?.MaxLists ?? DefaultMaxLists;
        private int MaxItems => _listLimits?.MaxItems ?? DefaultMaxItems;
        private int MaxNameChars => _listLimits?.MaxNameChars ?? DefaultMaxNameChars;

        // =====================================================================================
        // Load / apply
        // =====================================================================================

        private async Task LoadWatchlistsAsync()
        {
            if (_listsLoad == LoadState.Loading) return;
            _listsLoad = LoadState.Loading;
            _listsAttemptUtc = DateTime.UtcNow;
            if (_lists.Count == 0) ShowWatchlistMessage("Loading watchlists…", retry: false);
            PlatformResult<WatchlistsResponse> result;
            try { result = await ResearchClient.GetWatchlistsAsync(); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading the watchlists failed.", ex);
                result = new PlatformResult<WatchlistsResponse> { Status = PlatformCallStatus.Error, Message = "unexpected error — see the Backend Log." };
            }
            if (_shutDown) return;
            if (!result.Ok || result.Value?.Lists == null)
            {
                _listsLoad = LoadState.Failed;
                _lists = new List<WatchlistDto>(); // nothing old shown as current
                RenderWatchlists();
                ShowWatchlistMessage("Watchlists not loaded: " + (result.Ok ? "empty response from the backend." : result.Message) +
                                     " (retried automatically once the backend answers).", retry: true);
                _socket.TopicsChanged();
                RenderStar();
                return;
            }
            _listsLoad = LoadState.Loaded;
            HideWatchlistMessage();
            ApplyLists(result.Value);
        }

        private void WatchlistRetryButton_Click(object sender, RoutedEventArgs e)
        {
            _listsAttemptUtc = DateTime.MinValue;
            _ = LoadWatchlistsAsync();
        }

        /// <summary>Every watchlist route answers with all lists: replace the local copy, re-render, re-subscribe, re-quote.</summary>
        private void ApplyLists(WatchlistsResponse response)
        {
            if (response.Limits != null) _listLimits = response.Limits;
            var lists = (response.Lists ?? new List<WatchlistDto>())
                .Where(l => !string.IsNullOrWhiteSpace(l.Id))
                .OrderBy(l => l.Position ?? int.MaxValue)
                .ToList();
            foreach (var l in lists)
                l.Items = (l.Items ?? new List<WatchlistItemDto>())
                    .Where(i => i.Symbol != null && PlatformSymbol.IsMatch(i.Symbol.ToUpperInvariant()))
                    .OrderBy(i => i.Position ?? int.MaxValue)
                    .ToList();
            _lists = lists;
            if (_editingListId != null && _lists.All(l => l.Id != _editingListId)) _editingListId = null;
            if (_confirmDeleteListId != null && _lists.All(l => l.Id != _confirmDeleteListId)) _confirmDeleteListId = null;
            foreach (var item in _lists.SelectMany(l => l.Items!))
            {
                var q = Quote(item.Symbol!.ToUpperInvariant());
                if (!string.IsNullOrWhiteSpace(item.Name)) q.Name = item.Name;
                q.Covered = item.Covered != false;
            }
            RenderWatchlists();
            _socket.TopicsChanged();
            RequestAllQuotes();
            RenderStar();
            if (StarPopup.IsOpen) BuildStarPopup();
        }

        private WatchQuote Quote(string symbol)
        {
            if (!_quotes.TryGetValue(symbol, out var q))
            {
                q = new WatchQuote();
                _quotes[symbol] = q;
            }
            return q;
        }

        private IEnumerable<WatchlistDto> ExpandedLists => _lists.Where(l => l.Collapsed != true);

        // =====================================================================================
        // Quotes (GET /research/watchlists/{id}/quotes) — after a load/change, a reconnect, an expand, while incomplete
        // =====================================================================================

        private void RequestAllQuotes()
        {
            if (!IsShown) return;
            foreach (var list in ExpandedLists) _ = LoadQuotesAsync(list.Id!);
        }

        private async Task LoadQuotesAsync(string listId)
        {
            if (_shutDown || !IsShown) return;
            if (!_quotesInFlight.Add(listId)) return;
            var generation = _quoteGeneration.TryGetValue(listId, out var g) ? g + 1 : 1;
            _quoteGeneration[listId] = generation;
            PlatformResult<WatchlistQuotes> result;
            try { result = await ResearchClient.GetWatchlistQuotesAsync(listId); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading watchlist quotes failed.", ex);
                result = new PlatformResult<WatchlistQuotes> { Status = PlatformCallStatus.Error, Message = "unexpected error — see the Backend Log." };
            }
            finally
            {
                _quotesInFlight.Remove(listId);
            }
            if (_shutDown) return;
            var list = _lists.FirstOrDefault(l => l.Id == listId);
            if (list == null) return;
            if (!result.Ok || result.Value == null)
            {
                // Values of this list are unknown now: shown as "—" with the reason (no older value as current).
                var reason = "Prices not loaded: " + (result.Ok ? "empty response from the backend." : result.Message);
                foreach (var item in list.Items ?? new List<WatchlistItemDto>())
                {
                    var q = Quote(item.Symbol!.ToUpperInvariant());
                    q.Live = false;
                    q.Reason = reason;
                    q.Last = null;
                    q.LastNum = null;
                    q.PrevClose = null;
                    q.LastFromSocket = false;
                }
                RefreshAllRows();
                return;
            }
            var v = result.Value;
            if (!string.IsNullOrWhiteSpace(v.Note)) _quotesNote = v.Note;
            foreach (var item in v.Items ?? new List<WatchlistQuoteItem>())
            {
                var sym = item.Symbol?.ToUpperInvariant();
                if (sym == null) continue;
                var q = Quote(sym);
                q.Live = item.Live;
                q.Reason = string.IsNullOrWhiteSpace(item.Reason) ? null : item.Reason;
                q.Covered = item.Covered != false;
                if (!string.IsNullOrWhiteSpace(item.Name)) q.Name = item.Name;
                q.PrevClose = item.PrevClose;
                if (item.Last?.Value != null)
                {
                    q.Last = item.Last.Value;
                    q.LastNum = item.Last;
                    q.LastFromSocket = false;
                    q.LastTs = item.Last.AsOf;
                }
                else if (item.Live != true || !q.LastFromSocket)
                {
                    q.Last = null; // not live → no value; live without a trade yet → "—" with the reason
                    q.LastNum = item.Last;
                    q.LastFromSocket = false;
                }
            }
            RefreshAllRows();
            if (v.Complete == false)
            {
                // "Previous close is still loading — ask again." → ask again after retry_after_seconds while the list is shown.
                var seconds = Math.Clamp(v.RetryAfterSeconds ?? 5, 1, 60);
                var timer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromSeconds(seconds) };
                timer.Tick += (_, _) =>
                {
                    timer.Stop();
                    if (_quoteGeneration.TryGetValue(listId, out var current) && current != generation) return; // newer request ran
                    var l = _lists.FirstOrDefault(x => x.Id == listId);
                    if (l != null && l.Collapsed != true && IsShown) _ = LoadQuotesAsync(listId);
                };
                timer.Start();
            }
        }

        // =====================================================================================
        // Live updates (called from the socket handlers in ResearchView.xaml.cs)
        // =====================================================================================

        private void WatchlistTrade(string symbol, double price, string? ts)
        {
            if (!_quotes.TryGetValue(symbol, out var q)) return;
            q.Last = price;
            q.LastFromSocket = true;
            q.LastTs = ts;
            UpdateRowsFor(symbol);
        }

        private void WatchlistSymbolStatus(string symbol, bool? live, string? reason)
        {
            if (!_quotes.TryGetValue(symbol, out var q)) return;
            q.Live = live;
            q.Reason = string.IsNullOrWhiteSpace(reason) ? null : reason;
            if (live == false)
            {
                q.Last = null; // not live: no price (Q10 — no delayed/snapshot value)
                q.LastFromSocket = false;
            }
            UpdateRowsFor(symbol);
        }

        /// <summary>The live connection went down: stream values are no longer current (re-requested after the re-sync).</summary>
        private void WatchlistConnectionLost()
        {
            foreach (var q in _quotes.Values) q.LastFromSocket = false;
            RefreshAllRows();
        }

        // =====================================================================================
        // Topics (§4.3.1 order: active chart, then watchlist:<id> in panel order, then feeds)
        // =====================================================================================

        private IReadOnlyList<string> BuildTopics()
        {
            var topics = new List<string>();
            var tab = _active;
            if (tab != null)
            {
                topics.Add("candle:" + tab.Symbol + ":" + tab.Tf + ":" + tab.TopicSession);
                topics.Add("trades:" + tab.Symbol);
                topics.Add("news:" + tab.Symbol); // MR6: live news of the active symbol
            }
            // One subscription per shown, expanded list: the hub expands it to trades:<SYM> of its items (MR4 notes).
            foreach (var list in ExpandedLists)
            {
                if (topics.Count >= ResearchSocket.MaxTopics - 1) break; // keep room for feeds (20 lists max → never reached)
                topics.Add("watchlist:" + list.Id);
            }
            topics.Add("alerts"); // MR7: fired alerts (toast + log), §4.3.1 order: charts, watchlists, alerts, feeds
            topics.Add("feeds");
            return topics;
        }

        // =====================================================================================
        // Rendering
        // =====================================================================================

        private void ShowWatchlistMessage(string text, bool retry)
        {
            WatchlistMessageText.Text = text;
            WatchlistMessageText.Visibility = Visibility.Visible;
            AutomationProperties.SetName(WatchlistMessageText, text);
            WatchlistRetryButton.Visibility = retry ? Visibility.Visible : Visibility.Collapsed;
        }

        private void HideWatchlistMessage()
        {
            WatchlistMessageText.Text = "";
            WatchlistMessageText.Visibility = Visibility.Collapsed;
            WatchlistRetryButton.Visibility = Visibility.Collapsed;
        }

        private void RenderWatchlists()
        {
            WatchlistPanel.Children.Clear();
            _rowParts.Clear();
            _listHeaders.Clear();
            NewListButton.IsEnabled = _listsLoad == LoadState.Loaded;
            if (_listsLoad != LoadState.Loaded)
            {
                UpdateWatchlistLiveNote();
                return; // the message line says why (loading / not loaded)
            }
            if (_lists.Count == 0)
            {
                WatchlistPanel.Children.Add(new TextBlock { Text = "No watchlists yet.", Style = (Style)Resources["EmptyTitleStyle"], FontSize = 11.5, Margin = new Thickness(2, 4, 2, 0) });
                WatchlistPanel.Children.Add(new TextBlock
                {
                    Text = "Create one with + above, or with ☆ in the tab bar for the open symbol.",
                    Style = (Style)Resources["EmptyHintStyle"],
                    FontSize = 10.5,
                    Margin = new Thickness(2, 4, 2, 0),
                });
                UpdateWatchlistLiveNote();
                return;
            }
            for (var i = 0; i < _lists.Count; i++) WatchlistPanel.Children.Add(BuildListBlock(_lists[i], i));
            UpdateWatchlistLiveNote();

            // Keyboard reorder / remove: keep the focus on the moved row (or the list header) after the re-render.
            if (_focusRowAfterRender is { } target)
            {
                _focusRowAfterRender = null;
                var parts = _rowParts.TryGetValue(target.Symbol, out var ps) ? ps.FirstOrDefault(p => p.ListId == target.ListId) : null;
                if (parts != null) Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => parts.Row.Focus()));
            }
            else if (_focusListHeaderAfterRender is { } headerId)
            {
                _focusListHeaderAfterRender = null;
                var header = FindListHeader(headerId);
                if (header != null) Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => header.Focus()));
            }
        }

        private readonly Dictionary<string, Button> _listHeaders = new(StringComparer.Ordinal);

        private Button? FindListHeader(string listId) => _listHeaders.TryGetValue(listId, out var b) ? b : null;

        private UIElement BuildListBlock(WatchlistDto list, int index)
        {
            var id = list.Id!;
            var name = list.Name ?? "";
            var items = list.Items ?? new List<WatchlistItemDto>();
            var collapsed = list.Collapsed == true;
            var editing = _editingListId == id;
            var block = new StackPanel { Margin = new Thickness(0, 0, 0, 8) };

            // ----- header: ▾ NAME · n   [+] [✎] -----
            var header = new Grid();
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var caption = new TextBlock
            {
                Text = (collapsed ? "▸ " : "▾ ") + name.ToUpperInvariant() + " · " + items.Count.ToString(CultureInfo.InvariantCulture),
                FontFamily = new FontFamily("Consolas"),
                FontSize = 10,
                FontWeight = FontWeights.SemiBold,
                Foreground = Token("SecondaryForegroundBrush"),
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            var toggle = new Button
            {
                Style = (Style)Resources["ListRowButtonStyle"],
                Content = caption,
                Padding = new Thickness(4, 2, 4, 2),
                ToolTip = name + " — " + items.Count + " symbol(s). Click to " + (collapsed ? "expand" : "collapse") +
                          ". Collapsed lists are not streamed (saves live-stream slots).",
            };
            AutomationProperties.SetName(toggle, "Watchlist " + name + ", " + items.Count + " symbols, " + (collapsed ? "collapsed" : "expanded") +
                                                 ". Press to " + (collapsed ? "expand" : "collapse") + ".");
            toggle.Click += (_, _) => ToggleCollapsed(id);
            _listHeaders[id] = toggle;
            var add = IconButton("+", "Add a symbol to " + name + " (search)", () => OpenPalette(PaletteMode.AddToList, id));
            Grid.SetColumn(add, 1);
            var edit = IconButton(editing ? "✓" : "✎", editing ? "Done editing " + name : "Edit " + name + " (rename, reorder, remove, delete)",
                                  () => ToggleEditing(id));
            Grid.SetColumn(edit, 2);
            header.Children.Add(toggle);
            header.Children.Add(add);
            header.Children.Add(edit);
            block.Children.Add(header);

            // ----- edit panel -----
            if (editing) block.Children.Add(BuildEditPanel(list, index));

            // ----- rows -----
            if (!collapsed)
            {
                if (items.Count == 0)
                {
                    block.Children.Add(new TextBlock
                    {
                        Text = "No symbols — add one with + or ☆.",
                        Style = (Style)Resources["EmptyHintStyle"],
                        FontSize = 10.5,
                        Margin = new Thickness(6, 2, 2, 0),
                    });
                }
                for (var i = 0; i < items.Count; i++) block.Children.Add(BuildRow(list, items[i].Symbol!.ToUpperInvariant(), i, items.Count, editing));
            }
            return block;
        }

        private Button IconButton(string content, string label, Action onClick)
        {
            var b = new Button { Style = (Style)Resources["IconButtonStyle"], Content = content, ToolTip = label, VerticalAlignment = VerticalAlignment.Center };
            AutomationProperties.SetName(b, label);
            b.Click += (_, _) => onClick();
            return b;
        }

        private UIElement BuildEditPanel(WatchlistDto list, int index)
        {
            var id = list.Id!;
            var name = list.Name ?? "";
            var panel = new StackPanel { Margin = new Thickness(4, 4, 2, 6) };

            var renameBorder = new Border { Style = (Style)Resources["InputBorderStyle"] };
            var renameBox = new TextBox { Style = (Style)Resources["InputBoxStyle"], Text = name, MaxLength = MaxNameChars, FontSize = 11.5 };
            AutomationProperties.SetName(renameBox, "Rename " + name + ": type the new name and press Enter (Escape stops editing)");
            renameBox.KeyDown += (_, e) =>
            {
                if (e.Key == Key.Enter)
                {
                    e.Handled = true;
                    var newName = renameBox.Text.Trim();
                    if (newName == name) return;
                    if (!CheckListName(newName, out var why)) { ShowWatchlistMessage(why, retry: false); return; }
                    EnqueueListOp(async () => ApplyMutation(await ResearchClient.PatchWatchlistAsync(id, newName, null, null), "Renamed."));
                }
                else if (e.Key == Key.Escape)
                {
                    e.Handled = true;
                    _focusListHeaderAfterRender = id;
                    ToggleEditing(id);
                }
            };
            renameBorder.Child = renameBox;
            panel.Children.Add(renameBorder);

            var buttons = new WrapPanel { Margin = new Thickness(0, 4, 0, 0) };
            var up = SmallButton("▲ Up", "Move " + name + " up", index > 0, () => MoveList(id, -1));
            var down = SmallButton("▼ Down", "Move " + name + " down", index < _lists.Count - 1, () => MoveList(id, +1));
            var delete = SmallButton("Delete…", "Delete the watchlist " + name, true, () =>
            {
                _confirmDeleteListId = id;
                RenderWatchlists();
            });
            var done = SmallButton("Done", "Stop editing " + name, true, () =>
            {
                _focusListHeaderAfterRender = id;
                ToggleEditing(id);
            });
            buttons.Children.Add(up);
            buttons.Children.Add(down);
            buttons.Children.Add(delete);
            buttons.Children.Add(done);
            // MR8: the list as Strategy Platform universe → a NEW strategy draft in the Library's editor (no strategy write here).
            buttons.Children.Add(SmallButton("Use as universe…", "Use " + name + " as the universe of a new strategy draft (Library editor)", true,
                                             () => _ = UseAsUniverseAsync(id)));
            panel.Children.Add(buttons);

            if (_confirmDeleteListId == id)
            {
                var count = list.Items?.Count ?? 0;
                var confirm = new Border
                {
                    Background = Tint("AccentRedBrush", 0x14),
                    BorderBrush = Tint("AccentRedBrush", 0x66),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(6),
                    Padding = new Thickness(8, 6, 8, 6),
                    Margin = new Thickness(0, 6, 0, 0),
                };
                var stack = new StackPanel();
                var question = new TextBlock
                {
                    Text = "Delete \"" + name + "\" and its " + count + " symbol(s)? This cannot be undone.",
                    FontSize = 11,
                    Foreground = new SolidColorBrush(Color.FromRgb(0xE6, 0xE6, 0xE6)),
                    TextWrapping = TextWrapping.Wrap,
                };
                stack.Children.Add(question);
                var row = new WrapPanel { Margin = new Thickness(0, 6, 0, 0) };
                var yes = SmallButton("Delete", "Delete " + name + " now", true, () =>
                {
                    _confirmDeleteListId = null;
                    EnqueueListOp(async () => ApplyMutation(await ResearchClient.DeleteWatchlistAsync(id), "Watchlist deleted."));
                });
                var no = SmallButton("Cancel", "Keep " + name, true, () =>
                {
                    _confirmDeleteListId = null;
                    RenderWatchlists();
                });
                row.Children.Add(yes);
                row.Children.Add(no);
                stack.Children.Add(row);
                confirm.Child = stack;
                AutomationProperties.SetName(confirm, question.Text);
                panel.Children.Add(confirm);
                Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => no.Focus()));
            }
            return panel;
        }

        private Button SmallButton(string text, string label, bool enabled, Action onClick)
        {
            var b = new Button
            {
                Style = (Style)Resources["RoundedDarkButtonStyle"],
                Content = text,
                Padding = new Thickness(8, 2, 8, 2),
                Margin = new Thickness(0, 0, 4, 4),
                FontSize = 10.5,
                IsEnabled = enabled,
                ToolTip = label,
            };
            AutomationProperties.SetName(b, label);
            b.Click += (_, _) => onClick();
            return b;
        }

        private UIElement BuildRow(WatchlistDto list, string symbol, int index, int count, bool editing)
        {
            var id = list.Id!;
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(12) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(editing ? 0 : 54) });
            var arrow = new TextBlock { FontFamily = new FontFamily("Consolas"), FontSize = 9.5, VerticalAlignment = VerticalAlignment.Center };
            var ticker = new TextBlock
            {
                Text = symbol,
                FontFamily = new FontFamily("Consolas"),
                FontSize = 11,
                VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            Grid.SetColumn(ticker, 1);
            var price = new TextBlock { FontFamily = new FontFamily("Consolas"), FontSize = 11, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(4, 0, 0, 0) };
            Grid.SetColumn(price, 2);
            var pct = new TextBlock
            {
                FontFamily = new FontFamily("Consolas"),
                FontSize = 10.5,
                VerticalAlignment = VerticalAlignment.Center,
                HorizontalAlignment = HorizontalAlignment.Right,
            };
            Grid.SetColumn(pct, 3);
            grid.Children.Add(arrow);
            grid.Children.Add(WithLogo(symbol, ticker, 14)); // MR6 logo badge (initials when none)
            grid.Children.Add(price);
            grid.Children.Add(pct);
            var reason = new TextBlock
            {
                FontSize = 9.5,
                Foreground = Token("MutedForegroundBrush"),
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(12, 1, 0, 0),
                Visibility = Visibility.Collapsed,
            };
            var content = new StackPanel();
            content.Children.Add(grid);
            content.Children.Add(reason);
            var row = new Button
            {
                Style = (Style)Resources["ListRowButtonStyle"],
                Content = content,
                Padding = new Thickness(4, 2, 4, 2),
                Margin = new Thickness(0, 0, 0, 1),
            };
            row.Click += (_, _) =>
            {
                var q = _quotes.TryGetValue(symbol, out var qq) ? qq : null;
                var item = list.Items?.FirstOrDefault(i => string.Equals(i.Symbol, symbol, StringComparison.OrdinalIgnoreCase));
                OpenSymbol(symbol, q?.Name ?? item?.Name, item?.AssetClass, item?.Kind, item?.Exchange);
            };
            row.PreviewKeyDown += (_, e) =>
            {
                var key = e.Key == Key.System ? e.SystemKey : e.Key;
                var alt = (Keyboard.Modifiers & ModifierKeys.Alt) == ModifierKeys.Alt;
                if (key == Key.Delete)
                {
                    e.Handled = true;
                    RemoveSymbol(id, symbol, index, count);
                }
                else if (alt && (key == Key.Up || key == Key.Down))
                {
                    e.Handled = true;
                    MoveSymbol(id, symbol, key == Key.Up ? -1 : +1);
                }
            };
            var parts = new WatchRowParts { ListId = id, Symbol = symbol, Row = row, Arrow = arrow, Ticker = ticker, Price = price, Pct = pct, Reason = reason };
            if (!_rowParts.TryGetValue(symbol, out var list2)) _rowParts[symbol] = list2 = new List<WatchRowParts>();
            list2.Add(parts);
            UpdateRow(parts);

            if (!editing) return row;
            // Edit mode: ▲ ▼ × next to the row (separate buttons, never nested in the row button).
            var line = new Grid();
            line.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            line.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var tools = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            var up = IconButton("▲", "Move " + symbol + " up", () => MoveSymbol(id, symbol, -1));
            up.IsEnabled = index > 0;
            var down = IconButton("▼", "Move " + symbol + " down", () => MoveSymbol(id, symbol, +1));
            down.IsEnabled = index < count - 1;
            var remove = IconButton("×", "Remove " + symbol + " from " + (list.Name ?? "the list"), () => RemoveSymbol(id, symbol, index, count));
            tools.Children.Add(up);
            tools.Children.Add(down);
            tools.Children.Add(remove);
            Grid.SetColumn(tools, 1);
            line.Children.Add(row);
            line.Children.Add(tools);
            return line;
        }

        private void RefreshAllRows()
        {
            foreach (var parts in _rowParts.Values.SelectMany(p => p)) UpdateRow(parts);
            UpdateWatchlistLiveNote();
        }

        private void UpdateRowsFor(string symbol)
        {
            if (!_rowParts.TryGetValue(symbol, out var list)) return;
            foreach (var parts in list) UpdateRow(parts);
        }

        /// <summary>Price, ▲/▼ + % vs previous close, live state, per-symbol reason (stream limit, not covered …).</summary>
        private void UpdateRow(WatchRowParts parts)
        {
            var symbol = parts.Symbol;
            var q = _quotes.TryGetValue(symbol, out var qq) ? qq : null;
            var connected = _socket.State == ResearchSocketState.Connected;
            var live = connected && q?.Live == true;
            var selected = _active != null && _active.Symbol == symbol;
            var bright = new SolidColorBrush(Color.FromRgb(0xE6, 0xE6, 0xE6));
            var muted = Token("MutedForegroundBrush");

            var last = q?.Last;
            var prev = q?.PrevClose?.Value;
            double? change = last is double l && prev is double p && p > 0 ? l - p : null;
            double? pct = change is double c && prev is double p2 && p2 > 0 ? c / p2 * 100.0 : null;

            parts.Ticker.Foreground = selected ? Token("AccentBlueBrush") : (q?.Covered == false ? muted : bright);
            parts.Price.Text = last is double lv ? FormatPrice(lv) : "—";
            parts.Price.Foreground = live && last != null ? bright : muted;
            if (pct is double pv)
            {
                parts.Arrow.Text = Arrow(pv);
                parts.Pct.Text = SignedPct(pv);
                var brush = live ? DirectionBrush(pv) : muted;
                parts.Arrow.Foreground = brush;
                parts.Pct.Foreground = brush;
            }
            else
            {
                parts.Arrow.Text = "·";
                parts.Arrow.Foreground = muted;
                parts.Pct.Text = last != null ? "—" : "";
                parts.Pct.Foreground = muted;
            }

            // Per-symbol reason line — only while connected (otherwise the one note above the lists explains it).
            string? reasonLine = null;
            if (q?.Covered == false) reasonLine = q.Reason ?? "No longer in Alpaca's asset list — remove it or keep it for later.";
            else if (connected && q?.Live == false) reasonLine = q.Reason ?? symbol + " is not live.";
            parts.Reason.Text = reasonLine ?? "";
            parts.Reason.Visibility = reasonLine != null ? Visibility.Visible : Visibility.Collapsed;

            if (selected)
            {
                parts.Row.Background = Tint("AccentBlueBrush", 0x1F);
                parts.Row.BorderBrush = Tint("AccentBlueBrush", 0x66);
            }
            else
            {
                parts.Row.ClearValue(Control.BackgroundProperty);
                parts.Row.ClearValue(Control.BorderBrushProperty);
            }

            // Tooltip + screen-reader name: name, price + feed, change, live state, keyboard hints.
            var lines = new List<string> { symbol + (string.IsNullOrWhiteSpace(q?.Name) ? "" : " — " + q!.Name) };
            if (last != null)
            {
                lines.Add("Last trade " + FormatPrice(last.Value) + " · " + (q!.LastNum?.FeedLabel ?? (AssetClassOf(symbol) == "crypto" ? "Alpaca crypto · real time" : "IEX · real time")) +
                          " (live stream)" + (LocalDateTime(q.LastTs) is { } at ? " at " + at : ""));
            }
            else
            {
                lines.Add("Last trade: — " + (q?.LastNum?.Reason ?? q?.Reason ?? (connected ? "waiting for the live quote." : "not live.")));
            }
            if (pct is double pv2 && change is double cv)
                lines.Add("Change vs previous close " + Signed(cv) + " (" + SignedPct(pv2) + ", " + DirectionWord(pv2) + ")");
            lines.Add(NumTip("Previous close", q?.PrevClose));
            if (!connected) lines.Add("Not live: " + _socket.DescribeState());
            else if (q?.Live == false && q.Reason != null) lines.Add("Not live: " + q.Reason);
            lines.Add("Enter opens · Delete removes · Alt+↑/↓ moves");
            parts.Row.ToolTip = string.Join("\n", lines);
            AutomationProperties.SetName(parts.Row, symbol + ", " + parts.Price.Text +
                                                    (pct is double pv3 ? ", " + DirectionWord(pv3) + " " + Math.Abs(pv3).ToString("0.00", CultureInfo.InvariantCulture) + " percent" : "") +
                                                    (live ? ", live" : ", not live" + (reasonLine != null ? ": " + reasonLine : "")) +
                                                    (selected ? ", selected" : ""));
        }

        /// <summary>One line above the lists while the live connection is down (prices greyed), plus the backend's quotes note.</summary>
        private void UpdateWatchlistLiveNote()
        {
            var hasItems = _lists.Any(l => l.Collapsed != true && (l.Items?.Count ?? 0) > 0);
            string? text = null;
            if (hasItems && _socket.State != ResearchSocketState.Connected)
                text = "Not live — prices are greyed: " + _socket.DescribeState();
            WatchlistLiveNoteText.Text = text ?? "";
            WatchlistLiveNoteText.Visibility = text != null ? Visibility.Visible : Visibility.Collapsed;
            WatchlistLiveNoteText.ToolTip = _quotesNote;
        }

        /// <summary>Selection highlight follows the active tab.</summary>
        private void RenderWatchlistSelection() => RefreshAllRows();

        // =====================================================================================
        // Changes (each confirmed by the backend; run one after another)
        // =====================================================================================

        private void EnqueueListOp(Func<Task> op)
        {
            _listOps = RunListOpAsync(_listOps, op);
        }

        private async Task RunListOpAsync(Task previous, Func<Task> op)
        {
            try { await previous; }
            catch { /* the previous op logged its own failure */ }
            if (_shutDown) return;
            try { await op(); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: a watchlist change failed.", ex);
                ShowWatchlistMessage("Watchlist change failed — see the Backend Log.", retry: false);
                _ = LoadWatchlistsAsync();
            }
        }

        /// <summary>A mutation answer: all lists on success; otherwise the backend's reason and a re-load (the server state wins).</summary>
        private void ApplyMutation(PlatformResult<WatchlistsResponse> result, string? done)
        {
            if (_shutDown) return;
            if (result.Ok && result.Value?.Lists != null)
            {
                ApplyLists(result.Value);
                if (done != null) ShowWatchlistMessage(done, retry: false);
                else HideWatchlistMessage();
                return;
            }
            ShowWatchlistMessage(result.Ok ? "Watchlist change: empty response from the backend." : result.Message, retry: false);
            if (!result.Ok && result.Status != PlatformCallStatus.Conflict && result.Status != PlatformCallStatus.BadRequest)
                _ = LoadWatchlistsAsync();
        }

        private bool CheckListName(string name, out string why)
        {
            if (name.Length == 0) { why = "Give the watchlist a name."; return false; }
            if (name.Length > MaxNameChars) { why = "A watchlist name has at most " + MaxNameChars + " characters."; return false; }
            if (name.Any(char.IsControl)) { why = "A watchlist name cannot contain control characters."; return false; }
            why = "";
            return true;
        }

        private void ToggleCollapsed(string id)
        {
            var list = _lists.FirstOrDefault(l => l.Id == id);
            if (list == null) return;
            var collapsed = list.Collapsed != true;
            list.Collapsed = collapsed; // applied at once (the backend confirms; on failure the server state is re-loaded)
            _focusListHeaderAfterRender = id;
            RenderWatchlists();
            _socket.TopicsChanged();
            if (!collapsed) _ = LoadQuotesAsync(id);
            EnqueueListOp(async () =>
            {
                var result = await ResearchClient.PatchWatchlistAsync(id, null, collapsed, null);
                if (result.Ok && result.Value?.Lists != null)
                {
                    _focusListHeaderAfterRender = id;
                    ApplyLists(result.Value);
                }
                else ApplyMutation(result, null);
            });
        }

        private void ToggleEditing(string id)
        {
            _editingListId = _editingListId == id ? null : id;
            _confirmDeleteListId = null;
            RenderWatchlists();
        }

        private void MoveList(string id, int delta)
        {
            EnqueueListOp(async () =>
            {
                var index = _lists.FindIndex(l => l.Id == id);
                var target = index + delta;
                if (index < 0 || target < 0 || target >= _lists.Count) return;
                ApplyMutation(await ResearchClient.PatchWatchlistAsync(id, null, null, target), null);
            });
        }

        private void MoveSymbol(string id, string symbol, int delta)
        {
            EnqueueListOp(async () =>
            {
                var list = _lists.FirstOrDefault(l => l.Id == id);
                if (list?.Items == null) return;
                var symbols = list.Items.Select(i => i.Symbol!.ToUpperInvariant()).ToList();
                var index = symbols.IndexOf(symbol);
                var target = index + delta;
                if (index < 0 || target < 0 || target >= symbols.Count) return;
                symbols.RemoveAt(index);
                symbols.Insert(target, symbol);
                _focusRowAfterRender = (id, symbol);
                ApplyMutation(await ResearchClient.PutWatchlistItemsAsync(id, symbols), null);
            });
        }

        private void RemoveSymbol(string id, string symbol, int index, int count, bool keepFocus = false)
        {
            EnqueueListOp(async () =>
            {
                var list = _lists.FirstOrDefault(l => l.Id == id);
                var items = list?.Items;
                if (items == null) return;
                // Keyboard: the focus moves to the next row (or the previous one, or the list header).
                var next = index + 1 < items.Count ? items[index + 1].Symbol : index - 1 >= 0 ? items[index - 1].Symbol : null;
                if (keepFocus) { /* ☆ menu: the focus stays in the menu */ }
                else if (next != null) _focusRowAfterRender = (id, next.ToUpperInvariant());
                else _focusListHeaderAfterRender = id;
                ApplyMutation(await ResearchClient.RemoveWatchlistItemAsync(id, symbol), symbol + " removed from " + (list!.Name ?? "the list") + ".");
            });
        }

        /// <summary>Adds <paramref name="symbol"/> to a list (palette "+" mode, ☆ menu).</summary>
        private void AddSymbolToList(string id, string symbol, bool keepFocus = false)
        {
            EnqueueListOp(async () =>
            {
                var list = _lists.FirstOrDefault(l => l.Id == id);
                if (list == null) { ShowWatchlistMessage("That watchlist no longer exists.", retry: false); return; }
                if ((list.Items?.Count ?? 0) >= MaxItems) { ShowWatchlistMessage("A watchlist holds at most " + MaxItems + " symbols.", retry: false); return; }
                if (list.Items?.Any(i => string.Equals(i.Symbol, symbol, StringComparison.OrdinalIgnoreCase)) == true)
                {
                    ShowWatchlistMessage(symbol + " is already in " + (list.Name ?? "this watchlist") + ".", retry: false);
                    return;
                }
                if (list.Collapsed == true)
                {
                    // Show what was added: expand the list first.
                    var expand = await ResearchClient.PatchWatchlistAsync(id, null, false, null);
                    if (!expand.Ok) { ApplyMutation(expand, null); return; }
                }
                if (!keepFocus) _focusRowAfterRender = (id, symbol.ToUpperInvariant());
                ApplyMutation(await ResearchClient.AddWatchlistItemAsync(id, symbol), symbol + " added to " + (list.Name ?? "the list") + ".");
            });
        }

        /// <summary>Creates a list (sidebar "+" or ☆ menu), optionally with a first symbol.</summary>
        private void CreateList(string name, string? withSymbol)
        {
            EnqueueListOp(async () =>
            {
                if (_lists.Count >= MaxLists) { ShowWatchlistMessage("At most " + MaxLists + " watchlists — delete one first.", retry: false); return; }
                var created = await ResearchClient.CreateWatchlistAsync(name);
                if (!created.Ok || created.Value?.Lists == null || string.IsNullOrWhiteSpace(created.Value.Id))
                {
                    ApplyMutation(created, null);
                    return;
                }
                var id = created.Value.Id!;
                if (withSymbol == null)
                {
                    _focusListHeaderAfterRender = id;
                    ApplyMutation(created, "Watchlist \"" + name + "\" created — add symbols with +.");
                    return;
                }
                ApplyLists(created.Value);
                _focusRowAfterRender = (id, withSymbol.ToUpperInvariant());
                ApplyMutation(await ResearchClient.AddWatchlistItemAsync(id, withSymbol), withSymbol + " added to the new list \"" + name + "\".");
            });
        }

        // ---------- "+" new list in the sidebar ----------

        private void NewListButton_Click(object sender, RoutedEventArgs e)
        {
            if (NewListPanel.Visibility == Visibility.Visible)
            {
                HideNewListPanel();
                return;
            }
            NewListNameBox.MaxLength = MaxNameChars;
            NewListNameBox.Text = "";
            NewListPanel.Visibility = Visibility.Visible;
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => NewListNameBox.Focus()));
        }

        private void HideNewListPanel()
        {
            NewListPanel.Visibility = Visibility.Collapsed;
            NewListNameBox.Text = "";
        }

        private void NewListNameBox_TextChanged(object sender, TextChangedEventArgs e)
        {
            NewListHint.Visibility = string.IsNullOrEmpty(NewListNameBox.Text) ? Visibility.Visible : Visibility.Collapsed;
        }

        private void NewListNameBox_KeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key == Key.Escape)
            {
                e.Handled = true;
                HideNewListPanel();
                NewListButton.Focus();
                return;
            }
            if (e.Key != Key.Enter) return;
            e.Handled = true;
            var name = NewListNameBox.Text.Trim();
            if (!CheckListName(name, out var why))
            {
                ShowWatchlistMessage(why, retry: false);
                return;
            }
            HideNewListPanel();
            CreateList(name, null);
        }

        private void NewListNameBox_LostKeyboardFocus(object sender, KeyboardFocusChangedEventArgs e)
        {
            if (string.IsNullOrWhiteSpace(NewListNameBox.Text)) HideNewListPanel();
        }

        // =====================================================================================
        // ☆ in the tab bar: membership of the open symbol in each list
        // =====================================================================================

        private void RenderStar()
        {
            var tab = _active;
            ToolTipService.SetShowOnDisabled(StarButton, true);
            var inLists = tab == null
                ? new List<WatchlistDto>()
                : _lists.Where(l => l.Items?.Any(i => string.Equals(i.Symbol, tab.Symbol, StringComparison.OrdinalIgnoreCase)) == true).ToList();
            StarButton.IsEnabled = tab != null && _listsLoad == LoadState.Loaded;
            StarButton.Content = inLists.Count > 0 ? "★" : "☆";
            if (inLists.Count > 0) StarButton.Foreground = Token("AccentBlueBrush");
            else StarButton.ClearValue(Control.ForegroundProperty);
            string tip;
            if (tab == null) tip = "Open a symbol to add it to a watchlist.";
            else if (_listsLoad != LoadState.Loaded) tip = "Watchlists are not loaded — see the message in the sidebar.";
            else if (inLists.Count > 0) tip = tab.Symbol + " is in: " + string.Join(", ", inLists.Select(l => l.Name)) + " — click to change.";
            else tip = "Add " + tab.Symbol + " to a watchlist.";
            StarButton.ToolTip = tip;
            AutomationProperties.SetName(StarButton, tip);
        }

        private void StarButton_Click(object sender, RoutedEventArgs e)
        {
            if (_active == null) return;
            BuildStarPopup();
            StarPopupMessage.Visibility = Visibility.Collapsed;
            StarNewListBox.Text = "";
            StarNewListBox.MaxLength = MaxNameChars;
            StarPopup.IsOpen = true;
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() =>
            {
                if (!StarPopup.IsOpen) return;
                if (StarPopupLists.Children.OfType<Button>().FirstOrDefault() is { } first) first.Focus();
                else StarNewListBox.Focus();
            }));
        }

        private void BuildStarPopup()
        {
            var tab = _active;
            StarPopupLists.Children.Clear();
            if (tab == null) { StarPopup.IsOpen = false; return; }
            StarPopupTitle.Text = "ADD " + tab.Symbol + " TO WATCHLIST";
            if (_lists.Count == 0)
            {
                StarPopupLists.Children.Add(new TextBlock
                {
                    Text = "No watchlists yet — type a name below to create one with " + tab.Symbol + ".",
                    Style = (Style)Resources["EmptyHintStyle"],
                    Margin = new Thickness(2, 0, 2, 2),
                });
                return;
            }
            foreach (var list in _lists)
            {
                var id = list.Id!;
                var contains = list.Items?.Any(i => string.Equals(i.Symbol, tab.Symbol, StringComparison.OrdinalIgnoreCase)) == true;
                var text = new TextBlock
                {
                    Text = (contains ? "☑ " : "☐ ") + (list.Name ?? "") + " · " + (list.Items?.Count ?? 0).ToString(CultureInfo.InvariantCulture),
                    FontSize = 11.5,
                    Foreground = contains ? Token("AccentBlueBrush") : Token("PrimaryForegroundBrush"),
                    TextTrimming = TextTrimming.CharacterEllipsis,
                };
                var row = new Button { Style = (Style)Resources["ListRowButtonStyle"], Content = text, Padding = new Thickness(6, 3, 6, 3) };
                var label = (contains ? "Remove " + tab.Symbol + " from " : "Add " + tab.Symbol + " to ") + (list.Name ?? "the list") +
                            (contains ? " (it is in this list)" : "");
                row.ToolTip = label;
                AutomationProperties.SetName(row, label);
                var symbol = tab.Symbol;
                row.Click += (_, _) =>
                {
                    if (contains)
                    {
                        var items = list.Items ?? new List<WatchlistItemDto>();
                        var index = items.FindIndex(i => string.Equals(i.Symbol, symbol, StringComparison.OrdinalIgnoreCase));
                        RemoveSymbol(id, symbol, Math.Max(0, index), items.Count, keepFocus: true);
                    }
                    else
                    {
                        AddSymbolToList(id, symbol, keepFocus: true);
                    }
                    _starFocusListId = id; // re-focused after the menu is rebuilt with the backend's answer
                };
                StarPopupLists.Children.Add(row);
                if (_starFocusListId == id && StarPopup.IsOpen)
                {
                    _starFocusListId = null;
                    Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => row.Focus()));
                }
            }
        }

        private string? _starFocusListId;

        private void StarPopup_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key != Key.Escape) return;
            e.Handled = true;
            StarPopup.IsOpen = false;
            StarButton.Focus();
        }

        private void StarPopup_Closed(object? sender, EventArgs e)
        {
            StarNewListBox.Text = "";
            StarPopupMessage.Visibility = Visibility.Collapsed;
        }

        private void StarNewListBox_TextChanged(object sender, TextChangedEventArgs e)
        {
            StarNewListHint.Visibility = string.IsNullOrEmpty(StarNewListBox.Text) ? Visibility.Visible : Visibility.Collapsed;
        }

        private void StarNewListBox_KeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key != Key.Enter || _active == null) return;
            e.Handled = true;
            var name = StarNewListBox.Text.Trim();
            if (!CheckListName(name, out var why))
            {
                StarPopupMessage.Text = why;
                StarPopupMessage.Visibility = Visibility.Visible;
                return;
            }
            StarPopup.IsOpen = false;
            CreateList(name, _active.Symbol);
        }
    }
}

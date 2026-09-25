using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Navigation;
using System.Windows.Threading;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Financial Newsroom → "Insider Trades". Moved from Global Monitoring's
    /// InsiderTradesWindow (2026-09-24) and unchanged in behaviour: lists recent
    /// SEC Form 4 filings with parsed transaction details, auto-refreshes every
    /// 10s from the local HedgeFund backend (GET /newsroom/insider — no extra SEC
    /// requests; the collector polls SEC on its own schedule), and offers the
    /// one-click AI summary "✦ Sum up most notable" (POST /newsroom/insider-summary),
    /// labelled per the project's AI content labelling convention.
    ///
    /// 2026-09-25: "10b5-1 plan" chip on pre-planned trades, a banner for insider
    /// clusters (several insiders of one company buying/selling within 14 days,
    /// from the endpoint's "clusters"), and a filter row (free text over title +
    /// summary, "open-market purchases only"). The full list is kept in
    /// _allTrades; the filter is applied on every rebuild and on every change.
    /// </summary>
    public partial class InsiderTradesView : UserControl, INewsroomView
    {
        private const string ItemsUrl = NewsroomBackend.BaseUrl + "/newsroom/insider";
        private const string SummaryUrl = NewsroomBackend.BaseUrl + "/newsroom/insider-summary";
        private static readonly TimeSpan AutoRefreshInterval = TimeSpan.FromSeconds(10);

        private readonly ObservableCollection<InsiderTradeViewModel> _trades = new();
        private List<InsiderTradeViewModel> _allTrades = new();
        private string _clusterSignature = "";
        private readonly DispatcherTimer _autoRefreshTimer;
        private string _lastSignature = "";
        private bool _loading;

        public InsiderTradesView()
        {
            InitializeComponent();
            TradesList.ItemsSource = _trades;
            ClustersList.ItemsSource = Array.Empty<ClusterViewModel>();
            // One-click AI analysis -> trigger labelled as AI (EU AI Act Art. 50).
            SummaryButton.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetHelpText(SummaryButton, AiContent.TriggerTooltip);
            SummaryCaveat.Text = AiContent.Caveat;
            _autoRefreshTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = AutoRefreshInterval };
            _autoRefreshTimer.Tick += async (s, e) =>
            {
                if (IsVisible) await LoadTradesAsync(silent: true);
            };
            IsVisibleChanged += async (s, e) =>
            {
                if (e.NewValue is true && !_shutDown) await LoadTradesAsync(silent: true);
            };
            _autoRefreshTimer.Start();
        }

        // The view instance is kept while the newsroom window is open; it only
        // polls while it's actually shown.
        // Auto-refresh (reworked 2026-09-25): the timer no longer depends on the
        // view's Loaded/Unloaded events (cached views that are swapped in and out
        // of the newsroom window's ContentControl don't reliably get them in every
        // order). It runs for the view's lifetime at Normal priority, skips ticks
        // while the view isn't visible, refreshes right away when it becomes
        // visible again, and is stopped by Shutdown() when the window closes.
        private bool _shutDown;

        private async void View_Loaded(object sender, RoutedEventArgs e)
        {
            await LoadTradesAsync(silent: _allTrades.Count > 0);
        }

        private void View_Unloaded(object sender, RoutedEventArgs e)
        {
        }

        /// <summary>Called by the newsroom window when it closes.</summary>
        public void Shutdown()
        {
            _shutDown = true;
            _autoRefreshTimer.Stop();
        }

        private async void RefreshButton_Click(object sender, RoutedEventArgs e)
        {
            await LoadTradesAsync(silent: false);
        }

        private void Filing_RequestNavigate(object sender, RequestNavigateEventArgs e)
        {
            NewsroomLinks.Open(e.Uri?.AbsoluteUri);
            e.Handled = true;
        }

        /// <param name="silent">Auto-refresh: no "Loading…" flash, keeps the scroll
        /// position, keeps the current list if the backend is briefly unreachable.</param>
        private async Task LoadTradesAsync(bool silent)
        {
            if (_loading) return;
            _loading = true;
            if (!silent) ShowStatus("Loading insider trades…");

            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
                using var doc = JsonDocument.Parse(await client.GetStringAsync(ItemsUrl));
                var root = doc.RootElement;
                var items = root.TryGetProperty("items", out var arr) && arr.ValueKind == JsonValueKind.Array
                    ? arr.EnumerateArray().Select(InsiderTradeViewModel.FromJson).ToList()
                    : new System.Collections.Generic.List<InsiderTradeViewModel>();
                var ordered = items.OrderByDescending(t => t.SortKey).ToList();

                var signature = string.Join("|", ordered.Select(t => t.Id + ":" + t.Summary));
                LastUpdatedText.Text = $"Auto-refresh every {AutoRefreshInterval.TotalSeconds:0}s · updated {DateTime.Now:HH:mm:ss}";
                UpdateClusters(root);

                if (ordered.Count == 0)
                {
                    _allTrades = new List<InsiderTradeViewModel>();
                    _trades.Clear();
                    _lastSignature = "";
                    ShowStatus(EmptyMessage(root));
                    return;
                }
                if (silent && signature == _lastSignature)
                {
                    ShowListOrNoMatch();
                    return; // nothing changed — no rebuild, no flicker
                }
                _lastSignature = signature;
                _allTrades = ordered;
                ApplyFilter(keepScrollPosition: silent);
            }
            catch (Exception ex)
            {
                if (silent && _allTrades.Count > 0)
                {
                    LastUpdatedText.Text = $"Auto-refresh failed at {DateTime.Now:HH:mm:ss} ({ex.Message}) — showing last list";
                }
                else
                {
                    ShowStatus("The HedgeFund backend isn't answering yet (status: " + NewsroomBackend.Status + "). " +
                               "It is started automatically with the Hedge Fund window — the first start installs " +
                               "Python packages and can take a few minutes. This view retries on its own.");
                }
            }
            finally
            {
                _loading = false;
            }
        }

        // ---- Filter (2026-09-25) ----

        private const string NoMatchMessage = "No filings match the filter.";

        private bool Matches(InsiderTradeViewModel t)
        {
            if (PurchasesOnlyCheckBox.IsChecked == true && !t.HasOpenMarketPurchase) return false;
            var text = FilterTextBox.Text?.Trim() ?? "";
            return text.Length == 0
                || t.Title.Contains(text, StringComparison.OrdinalIgnoreCase)
                || t.Summary.Contains(text, StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>Rebuilds the visible list from _allTrades. keepScrollPosition:
        /// auto-refresh keeps the reader's place; a filter change starts at the top.</summary>
        private void ApplyFilter(bool keepScrollPosition)
        {
            if (_allTrades.Count == 0) return; // the load path shows the empty/status message
            var offset = ListScrollViewer.VerticalOffset;
            _trades.Clear();
            foreach (var t in _allTrades.Where(Matches)) _trades.Add(t);
            ShowListOrNoMatch();
            if (keepScrollPosition) ListScrollViewer.ScrollToVerticalOffset(offset);
            else ListScrollViewer.ScrollToTop();
        }

        private void ShowListOrNoMatch()
        {
            if (_trades.Count == 0 && _allTrades.Count > 0) ShowStatus(NoMatchMessage);
            else ShowList();
        }

        private void FilterTextBox_TextChanged(object sender, TextChangedEventArgs e)
        {
            FilterHintText.Visibility = string.IsNullOrEmpty(FilterTextBox.Text) ? Visibility.Visible : Visibility.Collapsed;
            ApplyFilter(keepScrollPosition: false);
        }

        private void PurchasesOnlyCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            ApplyFilter(keepScrollPosition: false);
        }

        // ---- Insider clusters banner (2026-09-25) ----

        private void UpdateClusters(JsonElement root)
        {
            var clusters = root.TryGetProperty("clusters", out var arr) && arr.ValueKind == JsonValueKind.Array
                ? arr.EnumerateArray().Select(ClusterViewModel.FromJson).ToList()
                : new List<ClusterViewModel>();
            var signature = string.Join("|", clusters.Select(c => c.Text));
            if (signature == _clusterSignature) return; // no rebuild every 10s
            _clusterSignature = signature;
            ClustersList.ItemsSource = clusters;
            ClustersBanner.Visibility = clusters.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        }

        private static string EmptyMessage(JsonElement root)
        {
            var message = "No insider filings loaded yet.";
            if (root.TryGetProperty("status", out var st) && st.ValueKind == JsonValueKind.Object)
            {
                var ok = st.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.True;
                var failed = st.TryGetProperty("ok", out okEl) && okEl.ValueKind == JsonValueKind.False;
                var text = st.TryGetProperty("message", out var m) ? m.GetString() : null;
                if (failed && !string.IsNullOrWhiteSpace(text))
                    return "SEC EDGAR collector: " + text + "\n\nIt retries on its own. If it never loads, check " +
                           "SEC_EDGAR_USER_AGENT in HedgeFund/backend/.env.";
                if (!ok)
                    message += " The SEC EDGAR collector needs 1–2 minutes for its first fetch after the backend starts.";
            }
            return message + " This list refreshes automatically.";
        }

        // "Sum up most notable": the backend builds a compact, pre-ranked table of
        // the filings it has in memory (see backend/newsroom/insider_summary.py)
        // and asks OpenRouter to summarize it.
        private async void SummaryButton_Click(object sender, RoutedEventArgs e)
        {
            SummaryButton.IsEnabled = false;
            SummaryCard.Visibility = Visibility.Visible;
            SummaryCaveat.Visibility = Visibility.Collapsed;
            SummaryCost.Visibility = Visibility.Collapsed;
            SummaryHeader.Text = AiContent.LoadingBadge;
            SummaryText.Text = "Asking the language model (OpenRouter)…";
            AutomationProperties.SetName(SummaryText, "AI summary is being generated");

            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(75) };
                using var response = await client.PostAsync(SummaryUrl, new StringContent("{}", System.Text.Encoding.UTF8, "application/json"));
                using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                var root = doc.RootElement;

                if (response.IsSuccessStatusCode && root.TryGetProperty("summary", out var summary))
                {
                    var count = root.TryGetProperty("filings", out var f) && f.ValueKind == JsonValueKind.Number ? f.GetInt32() : 0;
                    // Machine-readable marking from the backend (newsroom/ai.py provenance()).
                    AiContent.TryReadProvenance(root, out var model, out var generatedAt);
                    generatedAt ??= DateTimeOffset.Now;
                    var badge = AiContent.Badge(model, generatedAt).ToUpperInvariant();
                    SummaryHeader.Text = count > 0
                        ? $"{badge} · MOST NOTABLE OF {count} FILINGS"
                        : $"{badge} · MOST NOTABLE TRADES";
                    SummaryText.Text = summary.GetString() ?? "";
                    SummaryCaveat.Visibility = Visibility.Visible;
                    // Cost of this request as reported by OpenRouter (usage.cost; credits = USD).
                    double? costUsd = null;
                    long? promptTokens = null, completionTokens = null;
                    if (root.TryGetProperty("usage", out var us) && us.ValueKind == JsonValueKind.Object)
                    {
                        if (us.TryGetProperty("cost_usd", out var c) && c.ValueKind == JsonValueKind.Number) costUsd = c.GetDouble();
                        if (us.TryGetProperty("prompt_tokens", out var pt) && pt.ValueKind == JsonValueKind.Number) promptTokens = pt.GetInt64();
                        if (us.TryGetProperty("completion_tokens", out var ct) && ct.ValueKind == JsonValueKind.Number) completionTokens = ct.GetInt64();
                    }
                    SummaryCost.Text = CostLine(costUsd, promptTokens, completionTokens);
                    SummaryCost.Visibility = Visibility.Visible;
                    AutomationProperties.SetName(SummaryText, AiContent.AutomationName(model, generatedAt));
                }
                else
                {
                    SummaryHeader.Text = AiContent.UnavailableBadge;
                    SummaryCost.Visibility = Visibility.Collapsed;
                    AutomationProperties.SetName(SummaryText, string.Empty);
                    SummaryText.Text = root.TryGetProperty("error", out var err)
                        ? err.GetString() ?? "Unknown error."
                        : $"Backend answered HTTP {(int)response.StatusCode}.";
                }
            }
            catch (Exception ex)
            {
                SummaryHeader.Text = AiContent.UnavailableBadge;
                SummaryCost.Visibility = Visibility.Collapsed;
                AutomationProperties.SetName(SummaryText, string.Empty);
                SummaryText.Text = "Could not reach the HedgeFund backend (" + ex.Message + "). Status: " + NewsroomBackend.Status;
            }
            finally
            {
                SummaryButton.IsEnabled = true;
            }
        }

        /// <summary>"Cost: $0.0123 · 1,512 in / 874 out tokens (charged by OpenRouter)" — same format as AnalysisReasoningView.</summary>
        private static string CostLine(double? costUsd, long? promptTokens, long? completionTokens)
        {
            var cost = costUsd == null
                ? "Cost: not reported by OpenRouter"
                : "Cost: $" + costUsd.Value.ToString(costUsd.Value < 0.01 ? "0.0000" : "0.000", CultureInfo.InvariantCulture);
            var tokens = promptTokens != null && completionTokens != null
                ? $" · {promptTokens.Value.ToString("N0", CultureInfo.InvariantCulture)} in / " +
                  $"{completionTokens.Value.ToString("N0", CultureInfo.InvariantCulture)} out tokens"
                : "";
            return cost + tokens + (costUsd == null ? "" : " (charged by OpenRouter)");
        }

        private void ShowStatus(string message)
        {
            StatusText.Text = message;
            StatusPanel.Visibility = Visibility.Visible;
            ListScrollViewer.Visibility = Visibility.Collapsed;
        }

        private void ShowList()
        {
            StatusPanel.Visibility = Visibility.Collapsed;
            ListScrollViewer.Visibility = Visibility.Visible;
        }

        private sealed class InsiderTradeViewModel
        {
            public string Id { get; init; } = "";
            public string Title { get; init; } = "";
            public string Summary { get; init; } = "";
            public string Source { get; init; } = "";
            public Uri? UrlUri { get; init; }
            public bool HasUrl => UrlUri != null;
            public string TimestampDisplay { get; init; } = "";
            public DateTimeOffset SortKey { get; init; }
            /// <summary>details.plan_10b5_1 — trade under a Rule 10b5-1 plan (pre-planned).</summary>
            public bool IsPlan10b5One { get; init; }
            /// <summary>Any details.transactions[].code == "P".</summary>
            public bool HasOpenMarketPurchase { get; init; }

            private static string Str(JsonElement e, string name) =>
                e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

            public static InsiderTradeViewModel FromJson(JsonElement e)
            {
                // filed_at = when the Form 4 was filed (EDGAR feed); timestamp = when we saw it.
                var when = Str(e, "filed_at");
                if (!DateTimeOffset.TryParse(when, out var parsed))
                {
                    DateTimeOffset.TryParse(Str(e, "timestamp"), out parsed);
                }
                var url = Str(e, "url");
                var plan = false;
                var purchase = false;
                if (e.TryGetProperty("details", out var details) && details.ValueKind == JsonValueKind.Object)
                {
                    plan = details.TryGetProperty("plan_10b5_1", out var p) && p.ValueKind == JsonValueKind.True;
                    if (details.TryGetProperty("transactions", out var txs) && txs.ValueKind == JsonValueKind.Array)
                        purchase = txs.EnumerateArray().Any(tx => tx.ValueKind == JsonValueKind.Object && Str(tx, "code") == "P");
                }
                return new InsiderTradeViewModel
                {
                    Id = Str(e, "id"),
                    Title = string.IsNullOrWhiteSpace(Str(e, "title")) ? "(untitled filing)" : Str(e, "title"),
                    Summary = Str(e, "summary"),
                    Source = string.IsNullOrWhiteSpace(Str(e, "source")) ? "SEC EDGAR" : Str(e, "source"),
                    UrlUri = Uri.TryCreate(url, UriKind.Absolute, out var uri) ? uri : null,
                    TimestampDisplay = parsed == default ? "" : parsed.ToLocalTime().ToString("MMM d, HH:mm"),
                    SortKey = parsed,
                    IsPlan10b5One = plan,
                    HasOpenMarketPurchase = purchase,
                };
            }
        }

        /// <summary>One line of the clusters banner, e.g.
        /// "◆ Cluster buy: 3 insiders bought Acme Corp (ACME) · $1.2M · Sep 10–Sep 18".</summary>
        private sealed class ClusterViewModel
        {
            public string Text { get; init; } = "";
            public string Tooltip { get; init; } = "";
            public bool IsBuy { get; init; }

            private static string Str(JsonElement e, string name) =>
                e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

            private static string Money(double v) =>
                v >= 1_000_000 ? $"${v / 1_000_000:0.0}M"
                : v >= 1_000 ? $"${v / 1_000:0}K"
                : $"${v:0}";

            private static string ShortDate(string iso) =>
                DateTime.TryParseExact(iso, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var d)
                    ? d.ToString("MMM d", CultureInfo.InvariantCulture)
                    : iso;

            public static ClusterViewModel FromJson(JsonElement e)
            {
                var isBuy = Str(e, "kind") == "buy";
                var count = e.TryGetProperty("count", out var c) && c.ValueKind == JsonValueKind.Number ? c.GetInt32() : 0;
                var total = e.TryGetProperty("total_usd", out var t) && t.ValueKind == JsonValueKind.Number ? t.GetDouble() : 0;
                var names = e.TryGetProperty("insiders", out var n) && n.ValueKind == JsonValueKind.Array
                    ? n.EnumerateArray().Where(x => x.ValueKind == JsonValueKind.String).Select(x => x.GetString() ?? "").ToList()
                    : new List<string>();
                var company = Str(e, "issuer_name");
                var ticker = Str(e, "issuer_ticker");
                var who = string.IsNullOrWhiteSpace(company) ? (string.IsNullOrWhiteSpace(ticker) ? "unknown issuer" : ticker)
                    : string.IsNullOrWhiteSpace(ticker) ? company : $"{company} ({ticker})";
                var first = ShortDate(Str(e, "first_date"));
                var last = ShortDate(Str(e, "last_date"));
                var dates = first == last ? first : $"{first}–{last}";
                var verb = isBuy ? "bought" : "sold";
                var parts = new List<string> { $"◆ Cluster {(isBuy ? "buy" : "sell")}: {count} insiders {verb} {who}" };
                if (total > 0) parts.Add(Money(total));
                if (!string.IsNullOrWhiteSpace(dates)) parts.Add(dates);
                return new ClusterViewModel
                {
                    Text = string.Join(" · ", parts),
                    Tooltip = (isBuy ? "Open-market purchases" : "Open-market sales (Rule 10b5-1 plans excluded)") +
                              " by different insiders within 14 days: " + string.Join(", ", names),
                    IsBuy = isBuy,
                };
            }
        }
    }
}

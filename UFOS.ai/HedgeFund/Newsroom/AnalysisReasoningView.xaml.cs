using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Financial Newsroom → "Analysis &amp; Reasoning" (2026-09-25). Shows everything the
    /// other three sections know (insider trades, macro indicators, economic calendar)
    /// as square tiles grouped by source (GET /newsroom/overview). The user picks two
    /// or more tiles — by hand or via a template such as "Recession check" — and asks
    /// the AI to connect them ("✦ Let AI analyse this", POST /newsroom/analysis) with
    /// a Standard or Premium model.
    ///
    /// - Selection is kept by tile id across auto-refreshes (ids that disappear are dropped).
    /// - Auto-refresh every 60s (3s while the backend is unreachable or returns no tiles),
    ///   same DispatcherTimer pattern as MacroIndicatorsView: runs for the view's lifetime,
    ///   skips ticks while hidden, refreshes when shown again, stopped by Shutdown().
    /// - The AI output follows the AI content labelling rules (CLAUDE.md, Shared/AiContent.cs):
    ///   ✦ trigger + tooltip, loading/result badge before the text, caveat after it, violet
    ///   container, AutomationProperties, errors never look like analysis, and Copy / Save
    ///   keep the label line ("✦ AI-generated analysis (model, time) — not investment advice").
    /// </summary>
    public partial class AnalysisReasoningView : UserControl, INewsroomView
    {
        private const string OverviewUrl = NewsroomBackend.BaseUrl + "/newsroom/overview";
        private const string AnalysisUrl = NewsroomBackend.BaseUrl + "/newsroom/analysis";
        private const string LogModule = "HedgeFund";
        private static readonly TimeSpan AutoRefreshInterval = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan WaitingRefreshInterval = TimeSpan.FromSeconds(3);
        private static readonly TimeSpan AnalysisTimeout = TimeSpan.FromSeconds(150);

        /// <summary>Display order of the groups; unknown groups follow in order of appearance.</summary>
        private static readonly string[] GroupOrder =
            { "Macro Regime", "US Macro", "Euro Area", "Derived", "Insider Trades", "Calendar" };

        /// <summary>Selected model tier ("standard" / "premium"), remembered while the app runs.</summary>
        private static string s_tier = "standard";

        private readonly ObservableCollection<TileGroupViewModel> _groups = new();
        private readonly ObservableCollection<AnalysisTemplateViewModel> _templates = new();
        private readonly HashSet<string> _selectedIds = new(StringComparer.Ordinal);
        private List<TileViewModel> _allTiles = new(); // display order
        private readonly DispatcherTimer _autoRefreshTimer;
        private string _lastSignature = "";
        private string _templateSignature = "";
        private string _standardModel = "";
        private string _premiumModel = "";
        private bool _loading;
        private bool _waiting;
        private bool _shutDown;

        private bool _analysisRunning;
        private CancellationTokenSource? _analysisCts;
        private AnalysisResult? _result;

        public AnalysisReasoningView()
        {
            InitializeComponent();
            GroupsList.ItemsSource = _groups;
            TemplatesList.ItemsSource = _templates;

            // One-click AI analysis -> trigger labelled as AI (EU AI Act Art. 50).
            AnalyseButton.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetHelpText(AnalyseButton, AiContent.TriggerTooltip);
            AnalysisCaveat.Text = AiContent.Caveat;
            // Set after InitializeComponent so TierRadio_Checked never runs half-initialised.
            (s_tier == "premium" ? PremiumTierRadio : StandardTierRadio).IsChecked = true;
            UpdateTierTooltips();
            UpdateSelectionUi();

            _autoRefreshTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = AutoRefreshInterval };
            _autoRefreshTimer.Tick += async (s, e) =>
            {
                if (IsVisible) await LoadAsync(silent: true);
            };
            IsVisibleChanged += async (s, e) =>
            {
                if (e.NewValue is true && !_shutDown) await LoadAsync(silent: true);
            };
            _autoRefreshTimer.Start();
        }

        private async void View_Loaded(object sender, RoutedEventArgs e)
        {
            await LoadAsync(silent: _allTiles.Count > 0);
        }

        /// <summary>Called by the newsroom window when it closes.</summary>
        public void Shutdown()
        {
            _shutDown = true;
            _autoRefreshTimer.Stop();
            try { _analysisCts?.Cancel(); } catch (ObjectDisposedException) { }
        }

        private async void RefreshButton_Click(object sender, RoutedEventArgs e)
        {
            await LoadAsync(silent: _allTiles.Count > 0);
        }

        // ---------------------------------------------------------------- loading

        private async Task LoadAsync(bool silent)
        {
            if (_loading || _shutDown) return;
            _loading = true;
            if (!silent) ShowStatus("Loading the overview…");

            try
            {
                string json;
                using (var client = new HttpClient { Timeout = TimeSpan.FromSeconds(15) })
                {
                    json = await client.GetStringAsync(OverviewUrl);
                }
                if (_shutDown) return;
                Render(json);
            }
            catch (Exception ex)
            {
                SetWaiting(true); // backend not reachable yet (e.g. still starting) -> retry soon
                if (_allTiles.Count > 0)
                {
                    LastUpdatedText.Text = $"Auto-refresh failed at {DateTime.Now:HH:mm:ss} ({ex.Message}) — showing last overview";
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

        private void Render(string json)
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) throw new JsonException("Unexpected /newsroom/overview response.");

            // Models per tier (tooltips of the tier switch).
            if (root.TryGetProperty("models", out var models) && models.ValueKind == JsonValueKind.Object)
            {
                _standardModel = Str(models, "standard");
                _premiumModel = Str(models, "premium");
                UpdateTierTooltips();
            }

            RenderTemplates(root);

            // Tiles (unique non-empty ids).
            var tiles = new List<TileViewModel>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            if (root.TryGetProperty("tiles", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var el in arr.EnumerateArray())
                {
                    var tile = TileViewModel.FromJson(el);
                    if (tile != null && seen.Add(tile.Id)) tiles.Add(tile);
                }
            }

            SetWaiting(tiles.Count == 0);
            var generated = DateTimeOffset.TryParse(Str(root, "generated_at"), CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal, out var g) ? g.ToLocalTime().ToString("HH:mm:ss") : null;
            LastUpdatedText.Text = _waiting
                ? $"Waiting for the backend's first data — re-reading every {WaitingRefreshInterval.TotalSeconds:0}s · updated {DateTime.Now:HH:mm:ss}"
                : $"{tiles.Count} items{(generated != null ? " · overview built " + generated : "")} · re-read every {AutoRefreshInterval.TotalSeconds:0}s · updated {DateTime.Now:HH:mm:ss}";

            if (tiles.Count == 0)
            {
                if (_allTiles.Count > 0)
                {
                    LastUpdatedText.Text = $"The backend returned no items at {DateTime.Now:HH:mm:ss} — showing last overview, retrying every {WaitingRefreshInterval.TotalSeconds:0}s";
                    return;
                }
                ShowStatus("No items yet — the Insider Trades, Macro Indicators and Economic Calendar collectors need " +
                           "a minute or two for their first fetch after the backend starts. This view refreshes automatically.");
                return;
            }

            // Selection persists by id; ids that disappeared are dropped.
            var removed = _selectedIds.RemoveWhere(id => !seen.Contains(id));

            var signature = string.Join("\u001e", tiles.Select(t => t.Signature));
            if (signature == _lastSignature)
            {
                ShowList();
                if (removed > 0) UpdateSelectionUi();
                return; // nothing changed — no rebuild, no flicker, focus stays
            }
            _lastSignature = signature;

            var offset = ListScrollViewer.VerticalOffset;
            var orderedGroups = tiles.Select(t => t.Group).Distinct(StringComparer.Ordinal)
                .OrderBy(grp => { var i = Array.IndexOf(GroupOrder, grp); return i < 0 ? GroupOrder.Length : i; })
                .ToList(); // OrderBy is stable: unknown groups keep their order of appearance

            var display = new List<TileViewModel>();
            _groups.Clear();
            foreach (var grp in orderedGroups)
            {
                var groupTiles = tiles.Where(t => t.Group == grp).ToList();
                foreach (var t in groupTiles) t.IsSelected = _selectedIds.Contains(t.Id);
                display.AddRange(groupTiles);
                _groups.Add(new TileGroupViewModel(grp, groupTiles));
            }
            _allTiles = display;
            ShowList();
            ListScrollViewer.ScrollToVerticalOffset(offset); // keep the reader's place
            UpdateSelectionUi();
        }

        private void RenderTemplates(JsonElement root)
        {
            var templates = new List<AnalysisTemplateViewModel>();
            if (root.TryGetProperty("templates", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var el in arr.EnumerateArray())
                {
                    var t = AnalysisTemplateViewModel.FromJson(el);
                    if (t != null) templates.Add(t);
                }
            }
            var signature = string.Join("\u001e", templates.Select(t => t.Signature));
            if (signature == _templateSignature) return;
            _templateSignature = signature;
            _templates.Clear();
            foreach (var t in templates) _templates.Add(t);
            TemplatesLabel.Visibility = templates.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        }

        private void SetWaiting(bool waiting)
        {
            _waiting = waiting;
            var interval = waiting ? WaitingRefreshInterval : AutoRefreshInterval;
            if (_autoRefreshTimer.Interval != interval) _autoRefreshTimer.Interval = interval;
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

        // ---------------------------------------------------------------- selection

        private void Tile_Click(object sender, RoutedEventArgs e)
        {
            if (sender is not ToggleButton { DataContext: TileViewModel tile } button) return;
            var selected = button.IsChecked == true;
            tile.IsSelected = selected;
            if (selected) _selectedIds.Add(tile.Id);
            else _selectedIds.Remove(tile.Id);
            UpdateSelectionUi();
        }

        /// <summary>A template REPLACES the selection with its tile ids (those that exist).</summary>
        private void Template_Click(object sender, RoutedEventArgs e)
        {
            if (sender is not FrameworkElement { DataContext: AnalysisTemplateViewModel template }) return;
            var existing = new HashSet<string>(_allTiles.Select(t => t.Id), StringComparer.Ordinal);
            _selectedIds.Clear();
            foreach (var id in template.TileIds)
            {
                if (existing.Contains(id)) _selectedIds.Add(id);
            }
            ApplySelectionToTiles();
            UpdateSelectionUi();
        }

        private void ClearSelectionButton_Click(object sender, RoutedEventArgs e)
        {
            _selectedIds.Clear();
            ApplySelectionToTiles();
            UpdateSelectionUi();
        }

        private void ApplySelectionToTiles()
        {
            foreach (var t in _allTiles) t.IsSelected = _selectedIds.Contains(t.Id);
        }

        private List<TileViewModel> SelectedTilesInOrder() =>
            _allTiles.Where(t => _selectedIds.Contains(t.Id)).ToList();

        private string SelectionKey() =>
            string.Join(",", _selectedIds.OrderBy(id => id, StringComparer.Ordinal));

        private void UpdateSelectionUi()
        {
            var n = _selectedIds.Count;
            SelectionCountText.Text = n switch
            {
                0 => "Nothing selected — pick 2+ items",
                1 => "1 selected — pick at least one more",
                _ => $"{n} selected",
            };
            ClearSelectionButton.Visibility = n > 0 ? Visibility.Visible : Visibility.Collapsed;
            ActionBar.Visibility = n >= 2 ? Visibility.Visible : Visibility.Collapsed;
            ActionHintText.Text = _analysisRunning
                ? "Analysis running — this can take up to two minutes."
                : $"Combines the {n} selected items in one AI analysis.";
            UpdateStaleHint();
        }

        private void UpdateStaleHint()
        {
            SelectionChangedHint.Visibility = _result != null && !_analysisRunning && SelectionKey() != _result.SelectionKey
                ? Visibility.Visible
                : Visibility.Collapsed;
        }

        // ---------------------------------------------------------------- model tier

        private void TierRadio_Checked(object sender, RoutedEventArgs e)
        {
            s_tier = ReferenceEquals(sender, PremiumTierRadio) ? "premium" : "standard";
        }

        private void UpdateTierTooltips()
        {
            var standard = string.IsNullOrWhiteSpace(_standardModel) ? "chosen by the backend" : _standardModel;
            var premium = string.IsNullOrWhiteSpace(_premiumModel) ? "chosen by the backend" : _premiumModel;
            StandardTierRadio.ToolTip = "Standard model: " + standard;
            PremiumTierRadio.ToolTip = "Premium model: " + premium + " — stronger model, higher cost per analysis";
            AutomationProperties.SetHelpText(StandardTierRadio, (string)StandardTierRadio.ToolTip);
            AutomationProperties.SetHelpText(PremiumTierRadio, (string)PremiumTierRadio.ToolTip);
        }

        // ---------------------------------------------------------------- AI analysis

        private async void AnalyseButton_Click(object sender, RoutedEventArgs e)
        {
            if (_analysisRunning) return;
            var selected = SelectedTilesInOrder();
            if (selected.Count < 2) return;

            var tier = s_tier;
            var key = SelectionKey();
            var basedOn = selected.Select(t => new BasedOnItem(t.Title, t.Value, t.TrendText, t.AsOfDisplay)).ToList();

            _analysisRunning = true;
            AnalyseButton.IsEnabled = false;
            _analysisCts?.Dispose();
            _analysisCts = new CancellationTokenSource();
            var token = _analysisCts.Token;

            _result = null; // a new run replaces the previous analysis
            AnalysisCard.Visibility = Visibility.Visible;
            AnalysisActions.Visibility = Visibility.Collapsed;
            AnalysisCost.Visibility = Visibility.Collapsed;
            AnalysisCaveat.Visibility = Visibility.Collapsed;
            ActionStatusText.Text = "";
            AnalysisHeader.Text = $"{AiContent.LoadingBadge} · {selected.Count} ITEMS · {tier.ToUpperInvariant()}";
            AnalysisText.Text = "Asking the language model (OpenRouter)… this can take up to two minutes.";
            AnalysisBasedOn.Text = "Based on: " + string.Join(", ", basedOn.Select(b => b.Title));
            AutomationProperties.SetName(AnalysisText, "AI analysis is being generated");
            UpdateSelectionUi();

            try
            {
                var body = JsonSerializer.Serialize(new { tile_ids = selected.Select(t => t.Id).ToArray(), tier });
                using var client = new HttpClient { Timeout = AnalysisTimeout };
                using var content = new StringContent(body, Encoding.UTF8, "application/json");
                using var response = await client.PostAsync(AnalysisUrl, content, token);
                var text = await response.Content.ReadAsStringAsync(token);
                if (_shutDown) return;

                string? analysis = null;
                string? error = null;
                string? model = null;
                DateTimeOffset? generatedAt = null;
                string generatedAtRaw = "";
                var used = selected.Count;
                var usedTier = tier;
                double? costUsd = null;
                long? promptTokens = null, completionTokens = null;
                try
                {
                    using var doc = JsonDocument.Parse(text);
                    var root = doc.RootElement;
                    if (root.ValueKind == JsonValueKind.Object)
                    {
                        if (root.TryGetProperty("analysis", out var a) && a.ValueKind == JsonValueKind.String) analysis = a.GetString();
                        if (root.TryGetProperty("error", out var er) && er.ValueKind == JsonValueKind.String) error = er.GetString();
                        if (root.TryGetProperty("tiles_used", out var tu) && tu.ValueKind == JsonValueKind.Number && tu.TryGetInt32(out var n) && n > 0) used = n;
                        // Cost of this request as reported by OpenRouter (usage.cost; credits = USD).
                        if (root.TryGetProperty("usage", out var us) && us.ValueKind == JsonValueKind.Object)
                        {
                            if (us.TryGetProperty("cost_usd", out var c) && c.ValueKind == JsonValueKind.Number) costUsd = c.GetDouble();
                            if (us.TryGetProperty("prompt_tokens", out var pt) && pt.ValueKind == JsonValueKind.Number) promptTokens = pt.GetInt64();
                            if (us.TryGetProperty("completion_tokens", out var ct) && ct.ValueKind == JsonValueKind.Number) completionTokens = ct.GetInt64();
                        }
                        var tierText = Str(root, "tier");
                        if (tierText.Length > 0) usedTier = tierText;
                        // Machine-readable marking from the backend (newsroom/ai.py provenance()).
                        AiContent.TryReadProvenance(root, out model, out generatedAt);
                        if (root.TryGetProperty("provenance", out var prov) && prov.ValueKind == JsonValueKind.Object)
                        {
                            generatedAtRaw = Str(prov, "generated_at");
                            if (string.IsNullOrWhiteSpace(model)) model = NullIfEmpty(Str(prov, "model"));
                        }
                    }
                }
                catch (JsonException)
                {
                    // Not JSON (e.g. a proxy error page) -> handled as an error below.
                }

                if (response.IsSuccessStatusCode && !string.IsNullOrWhiteSpace(analysis))
                {
                    var at = generatedAt ?? DateTimeOffset.Now;
                    _result = new AnalysisResult(analysis, model, at,
                        generatedAtRaw.Length > 0 ? generatedAtRaw : at.ToString("o", CultureInfo.InvariantCulture),
                        usedTier, used, key, basedOn, CostLine(costUsd, promptTokens, completionTokens));
                    ShowResult(_result);
                }
                else
                {
                    ShowError(!string.IsNullOrWhiteSpace(error)
                        ? error
                        : $"The backend answered HTTP {(int)response.StatusCode} without an analysis.");
                }
            }
            catch (OperationCanceledException) when (_shutDown)
            {
                // Window closed while the analysis was running.
            }
            catch (Exception ex)
            {
                if (_shutDown) return;
                var message = ex is TaskCanceledException
                    ? $"The analysis took longer than {AnalysisTimeout.TotalSeconds:0} s and was aborted."
                    : "Could not reach the HedgeFund backend (" + ex.Message + ").";
                ShowError(message + " Status: " + NewsroomBackend.Status);
            }
            finally
            {
                _analysisRunning = false;
                if (!_shutDown)
                {
                    AnalyseButton.IsEnabled = true;
                    UpdateSelectionUi();
                }
            }
        }

        private void ShowResult(AnalysisResult r)
        {
            AnalysisHeader.Text = $"{AiContent.Badge(r.Model, r.GeneratedAt).ToUpperInvariant()} · {r.ItemCount} ITEMS · {r.Tier.ToUpperInvariant()}";
            AnalysisText.Text = r.Text;
            AnalysisCaveat.Visibility = Visibility.Visible;
            AnalysisBasedOn.Text = "Based on: " + string.Join(", ", r.BasedOn.Select(b => b.Title));
            AnalysisCost.Text = r.CostText;
            AnalysisCost.Visibility = Visibility.Visible;
            AnalysisActions.Visibility = Visibility.Visible;
            AutomationProperties.SetName(AnalysisText, AiContent.AutomationName(r.Model, r.GeneratedAt));
        }

        private void ShowError(string message)
        {
            _result = null;
            AnalysisHeader.Text = AiContent.UnavailableBadge;
            AnalysisText.Text = message;
            AnalysisCaveat.Visibility = Visibility.Collapsed;
            AnalysisCost.Visibility = Visibility.Collapsed;
            AnalysisActions.Visibility = Visibility.Collapsed;
            AutomationProperties.SetName(AnalysisText, string.Empty);
        }

        // ---------------------------------------------------------------- Copy / Save

        /// <summary>"✦ AI-generated analysis (model, 2026-09-25 18:42) — not investment advice" (AI Act rule 8).</summary>
        private static string LabelLine(AnalysisResult r) =>
            $"{AiContent.Glyph} AI-generated analysis ({(string.IsNullOrWhiteSpace(r.Model) ? "unknown model" : r.Model)}, " +
            $"{r.GeneratedAt.ToLocalTime():yyyy-MM-dd HH:mm}) — not investment advice";

        /// <summary>"Cost: $0.0123 · 1,512 in / 874 out tokens (charged by OpenRouter)".</summary>
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

        private static string BasedOnLine(AnalysisResult r) =>
            "Based on: " + string.Join(", ", r.BasedOn.Select(b => b.Title));

        private void CopyButton_Click(object sender, RoutedEventArgs e)
        {
            if (_result is not { } r) return;
            var sb = new StringBuilder();
            sb.AppendLine(LabelLine(r));
            sb.AppendLine();
            sb.AppendLine(r.Text.Trim());
            sb.AppendLine();
            sb.AppendLine(AiContent.Caveat);
            sb.AppendLine(BasedOnLine(r));
            sb.Append(r.CostText);
            try
            {
                Clipboard.SetText(sb.ToString());
                ActionStatusText.Text = "Copied";
            }
            catch (Exception ex)
            {
                ActionStatusText.Text = "Copy failed";
                AppLog.Warn(LogModule, "Could not copy the AI analysis to the clipboard.", ex);
            }
        }

        private void SaveButton_Click(object sender, RoutedEventArgs e)
        {
            if (_result is not { } r) return;
            var dialog = new SaveFileDialog
            {
                Title = "Save AI analysis as Markdown",
                FileName = $"ufos-analysis-{r.GeneratedAt.ToLocalTime():yyyyMMdd-HHmm}.md",
                DefaultExt = ".md",
                AddExtension = true,
                Filter = "Markdown (*.md)|*.md|All files (*.*)|*.*",
            };
            var owner = Window.GetWindow(this);
            var ok = owner != null ? dialog.ShowDialog(owner) : dialog.ShowDialog();
            if (ok != true) return;
            try
            {
                File.WriteAllText(dialog.FileName, BuildMarkdown(r), new UTF8Encoding(false));
                ActionStatusText.Text = "Saved";
            }
            catch (Exception ex)
            {
                ActionStatusText.Text = "Save failed";
                AppLog.Warn(LogModule, "Could not save the AI analysis to " + dialog.FileName, ex);
                var text = "Could not save the file:\n" + ex.Message;
                if (owner != null) MessageBox.Show(owner, text, "Save as Markdown", MessageBoxButton.OK, MessageBoxImage.Warning);
                else MessageBox.Show(text, "Save as Markdown", MessageBoxButton.OK, MessageBoxImage.Warning);
            }
        }

        private static string BuildMarkdown(AnalysisResult r)
        {
            var sb = new StringBuilder();
            sb.AppendLine(LabelLine(r));
            sb.AppendLine();
            sb.AppendLine("# Analysis & Reasoning — UFOS.ai Financial Newsroom");
            sb.AppendLine();
            sb.AppendLine($"Model tier: {Capitalize(r.Tier)} · {r.ItemCount} items · generated {r.GeneratedAt.ToLocalTime():yyyy-MM-dd HH:mm}");
            sb.AppendLine();
            sb.AppendLine("## AI-generated analysis");
            sb.AppendLine();
            sb.AppendLine(r.Text.Trim());
            sb.AppendLine();
            sb.AppendLine("## Selected items");
            sb.AppendLine();
            foreach (var b in r.BasedOn)
            {
                var line = $"- **{b.Title}**: {b.Value}";
                if (b.Trend.Length > 0) line += $" ({b.Trend})";
                if (b.AsOf.Length > 0) line += $" — {b.AsOf}";
                sb.AppendLine(line);
            }
            sb.AppendLine();
            sb.AppendLine("## Caveat");
            sb.AppendLine();
            sb.AppendLine("_" + AiContent.Caveat + "_");
            sb.AppendLine();
            sb.AppendLine(BasedOnLine(r));
            sb.AppendLine();
            sb.AppendLine("## Provenance");
            sb.AppendLine();
            sb.AppendLine("```yaml");
            sb.AppendLine("ai_generated: true");
            sb.AppendLine("human_reviewed: false");
            sb.AppendLine("model: " + (string.IsNullOrWhiteSpace(r.Model) ? "unknown" : r.Model));
            sb.AppendLine("generated_at: " + r.GeneratedAtRaw);
            sb.AppendLine("tier: " + r.Tier);
            sb.AppendLine("usage: \"" + r.CostText.Replace("\"", "'") + "\"");
            sb.AppendLine("provider: OpenRouter");
            sb.AppendLine("generator: UFOS.ai Financial Newsroom — Analysis & Reasoning");
            sb.AppendLine("```");
            return sb.ToString();
        }

        private static string Capitalize(string s) =>
            string.IsNullOrEmpty(s) ? s : char.ToUpperInvariant(s[0]) + s.Substring(1);

        // ---------------------------------------------------------------- JSON helpers

        private static string? NullIfEmpty(string s) => string.IsNullOrWhiteSpace(s) ? null : s;

        /// <summary>String or number property as text; anything else / missing -> "".</summary>
        private static string Str(JsonElement e, string name) =>
            e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) ? ValueText(v) : "";

        private static string ValueText(JsonElement v) => v.ValueKind switch
        {
            JsonValueKind.String => v.GetString() ?? "",
            JsonValueKind.Number => v.GetRawText(),
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            JsonValueKind.Null => "",
            JsonValueKind.Undefined => "",
            _ => v.GetRawText(),
        };

        // ---------------------------------------------------------------- view models

        private sealed record BasedOnItem(string Title, string Value, string Trend, string AsOf);

        private sealed record AnalysisResult(
            string Text,
            string? Model,
            DateTimeOffset GeneratedAt,
            string GeneratedAtRaw,
            string Tier,
            int ItemCount,
            string SelectionKey,
            IReadOnlyList<BasedOnItem> BasedOn,
            string CostText);

        private sealed class TileGroupViewModel
        {
            public TileGroupViewModel(string title, List<TileViewModel> tiles)
            {
                Title = title;
                Tiles = tiles;
            }

            public string Title { get; }
            public string Header => $"{Title.ToUpperInvariant()} · {Tiles.Count}";
            public List<TileViewModel> Tiles { get; }
        }

        private sealed class AnalysisTemplateViewModel
        {
            public string Id { get; init; } = "";
            public string Label { get; init; } = "";
            public string Description { get; init; } = "";
            public IReadOnlyList<string> TileIds { get; init; } = Array.Empty<string>();
            public string Signature => string.Join("\u001f", Id, Label, Description, string.Join(",", TileIds));

            public static AnalysisTemplateViewModel? FromJson(JsonElement e)
            {
                if (e.ValueKind != JsonValueKind.Object) return null;
                var label = Str(e, "label");
                if (label.Length == 0) label = Str(e, "id");
                if (label.Length == 0) return null;
                var ids = e.TryGetProperty("tile_ids", out var arr) && arr.ValueKind == JsonValueKind.Array
                    ? arr.EnumerateArray().Select(ValueText).Where(s => s.Length > 0).Distinct(StringComparer.Ordinal).ToList()
                    : new List<string>();
                var description = Str(e, "description");
                return new AnalysisTemplateViewModel
                {
                    Id = Str(e, "id"),
                    Label = label,
                    Description = description.Length > 0 ? description : "Selects " + ids.Count + " items",
                    TileIds = ids,
                };
            }
        }

        private sealed class TileViewModel : INotifyPropertyChanged
        {
            private static readonly SolidColorBrush OkBrush = Frozen(0x3E, 0xCF, 0x8E);
            private static readonly SolidColorBrush WatchBrush = Frozen(0xF3, 0x9C, 0x4A);
            private static readonly SolidColorBrush AlertBrush = Frozen(0xED, 0x6A, 0x5A);
            private static readonly SolidColorBrush InfoBrush = Frozen(0x49, 0xA7, 0xFF);
            private static readonly SolidColorBrush NoLevelBrush = Frozen(0x55, 0x55, 0x55);

            private static SolidColorBrush Frozen(byte r, byte g, byte b)
            {
                var brush = new SolidColorBrush(Color.FromRgb(r, g, b));
                brush.Freeze();
                return brush;
            }

            public string Id { get; init; } = "";
            public string Group { get; init; } = "";
            public string Title { get; init; } = "";
            public string Value { get; init; } = "—";
            public string Trend { get; init; } = "none";
            public string Change { get; init; } = "";
            public string Level { get; init; } = "";
            public string Subtitle { get; init; } = "";
            public string AsOf { get; init; } = "";
            public string Facts { get; init; } = "";

            public string TrendArrow => Trend switch
            {
                "up" => "▲",
                "down" => "▼",
                "flat" => "▶",
                _ => "",
            };

            /// <summary>"▲ +0.25 pp" — neutral grey in the tile (no good/bad colouring of direction).</summary>
            public string TrendText => string.Join(" ", new[] { TrendArrow, Change }.Where(s => s.Length > 0));

            public Brush LevelBrush => Level switch
            {
                "ok" => OkBrush,
                "watch" => WatchBrush,
                "alert" => AlertBrush,
                "info" => InfoBrush,
                _ => NoLevelBrush,
            };

            private string LevelWord => Level switch
            {
                "ok" => "level ok",
                "watch" => "level watch",
                "alert" => "level alert",
                "info" => "info",
                _ => "",
            };

            private string TrendWord => Trend switch
            {
                "up" => "up",
                "down" => "down",
                "flat" => "flat",
                _ => "",
            };

            public string AsOfDisplay
            {
                get
                {
                    if (AsOf.Length == 0) return "";
                    if (DateTimeOffset.TryParse(AsOf, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var d))
                    {
                        return AsOf.Length > 10
                            ? "as of " + d.ToLocalTime().ToString("MMM d, HH:mm", CultureInfo.InvariantCulture)
                            : "as of " + d.ToString("MMM d, yyyy", CultureInfo.InvariantCulture);
                    }
                    return "as of " + AsOf;
                }
            }

            public string TooltipText
            {
                get
                {
                    var head = Subtitle.Length > 0 ? $"{Title}\n{Subtitle}" : Title;
                    return Facts.Length > 0 ? head + "\n\n" + Facts : head;
                }
            }

            private bool _isSelected;

            public bool IsSelected
            {
                get => _isSelected;
                set
                {
                    if (_isSelected == value) return;
                    _isSelected = value;
                    OnPropertyChanged(nameof(IsSelected));
                    OnPropertyChanged(nameof(AutomationName));
                }
            }

            /// <summary>Screen reader: "10Y Treasury, 4.12 %, up +0.05, level watch, selected".</summary>
            public string AutomationName
            {
                get
                {
                    var parts = new List<string> { Title, Value };
                    if (TrendWord.Length > 0 || Change.Length > 0) parts.Add((TrendWord + " " + Change).Trim());
                    if (LevelWord.Length > 0) parts.Add(LevelWord);
                    parts.Add(IsSelected ? "selected" : "not selected");
                    return string.Join(", ", parts);
                }
            }

            public string Signature =>
                string.Join("\u001f", Id, Group, Title, Value, Trend, Change, Level, Subtitle, AsOf, Facts);

            public event PropertyChangedEventHandler? PropertyChanged;

            private void OnPropertyChanged(string name) =>
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));

            public static TileViewModel? FromJson(JsonElement e)
            {
                if (e.ValueKind != JsonValueKind.Object) return null;
                var id = Str(e, "id");
                if (id.Length == 0) return null;
                var title = Str(e, "title");
                var value = Str(e, "value");
                var group = Str(e, "group");
                return new TileViewModel
                {
                    Id = id,
                    Group = group.Length > 0 ? group : "Other",
                    Title = title.Length > 0 ? title : id,
                    Value = value.Length > 0 ? value : "—",
                    Trend = Str(e, "trend").ToLowerInvariant(),
                    Change = Str(e, "change"),
                    Level = Str(e, "level").ToLowerInvariant(),
                    Subtitle = Str(e, "subtitle"),
                    AsOf = Str(e, "as_of"),
                    Facts = FactsText(e),
                };
            }

            /// <summary>"facts" may be a string, a list of strings or an object — all become lines of text.</summary>
            private static string FactsText(JsonElement e)
            {
                if (!e.TryGetProperty("facts", out var f)) return "";
                return f.ValueKind switch
                {
                    JsonValueKind.Array => string.Join("\n", f.EnumerateArray().Select(ValueText)
                        .Where(s => s.Length > 0).Select(s => "• " + s)),
                    JsonValueKind.Object => string.Join("\n", f.EnumerateObject()
                        .Select(p => p.Name + ": " + ValueText(p.Value))),
                    _ => ValueText(f),
                };
            }
        }
    }
}

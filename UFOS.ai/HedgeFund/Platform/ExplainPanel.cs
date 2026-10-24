using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// "Explain with AI…" for the backtest and validation reports (Strategy Platform §11.2 layer 2, §11.3 WPF; API
    /// §11.6). <see cref="TriggerButton"/> sits in the Attribution header; the panel itself (this element) holds the
    /// options (Standard / Premium with the backend's pre-call estimate, English / German, "Generate"), the waiting
    /// state ("Writing explanation…" — the synchronous POST may take up to 90 s; the UI stays responsive), the
    /// stored explanation and "Earlier versions (n)" (newest first). Explanations are stored with the run; a
    /// regenerate costs again (new row). Labelling (EU AI Act Art. 50): "✦ AI-generated · model · time — explains the
    /// computed attribution; not verified — information, not a recommendation", violet AI container, caveat, screen-reader name
    /// starting with "AI-generated". The text is shown as plain text runs only (no markup, no links); numbers the
    /// backend's number check could not find in the fact table (check.unverified, offsets per field) are highlighted
    /// red with the tooltip "not in the computed data" — shown, never removed. Hypotheses get their own labelled
    /// box. Backend errors (budget reached, AI off, no key, bad_json, timeout, busy, …) are shown verbatim.
    /// </summary>
    internal sealed class ExplainPanel : StackPanel
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        public const string UnverifiedTooltip = "not in the computed data";
        private const string WaitingText = "Writing explanation…";
        // [fact.id] / [a.b, c.d] citations (SPEC §11.2: every number cited with its fact id) — shown dimmed, never removed.
        private static readonly Regex Citation = new(@"\[[A-Za-z0-9_.<>/\-]+(?:\s*,\s*[A-Za-z0-9_.<>/\-]+)*\]", RegexOptions.Compiled);

        private readonly bool _validation;
        private readonly string? _runId;
        private readonly string _groupId = Guid.NewGuid().ToString("N");
        private readonly Border _options;
        private readonly RadioButton _standard;
        private readonly RadioButton _premium;
        private readonly RadioButton _english;
        private readonly RadioButton _german;
        private readonly Button _generate;
        private readonly TextBlock _budgetText;
        private readonly TextBlock _status;
        private readonly StackPanel _errorHost;
        private readonly StackPanel _resultHost;
        private readonly StackPanel _versionsBar;
        private readonly Button _regenerate;
        private readonly Button _versionsButton;
        private readonly StackPanel _versionsList;
        private readonly DispatcherTimer _waitTimer;
        private DateTime _waitStarted;
        private List<RunExplanation> _items = new();
        private RunExplanation? _shown;
        private bool _busy;
        private bool _loaded;
        private bool _explainable;
        private ExplainEstimate? _estimate;

        /// <summary>"✦ Explain with AI…" — enabled only for done runs with an attribution (tooltip with the reason otherwise).</summary>
        public Button TriggerButton { get; }

        /// <param name="validation">true: /v2/validations/{id}/explanations (validation + holdout runs); false: /v2/backtests/{id}/explanations.</param>
        /// <param name="runId">The run card's id.</param>
        /// <param name="status">The run card's status.</param>
        /// <param name="hasAttribution">The card carries an attribution.</param>
        /// <param name="unavailableReason">The card's attribution_unavailable (the backend's reason when there is none).</param>
        public ExplainPanel(bool validation, string? runId, string? status, bool hasAttribution, string? unavailableReason)
        {
            _validation = validation;
            _runId = string.IsNullOrWhiteSpace(runId) ? null : runId;
            AutomationProperties.SetName(this, "AI explanation of the attribution");

            TriggerButton = FundUi.MakeButton(AiContent.Glyph + " Explain with AI…", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            TriggerButton.Padding = new Thickness(10, 2, 10, 2);
            TriggerButton.VerticalAlignment = VerticalAlignment.Center;
            ToolTipService.SetShowOnDisabled(TriggerButton, true);
            AutomationProperties.SetName(TriggerButton, "Explain with AI");
            TriggerButton.Click += (_, _) => ToggleOptions();

            // ----- options -----
            var opt = new StackPanel();
            var optTitle = FundUi.Text(AiContent.Glyph + " Explain with AI", 11.5, FundPalette.Ai, bold: true);
            optTitle.Margin = new Thickness(0, 0, 0, 4);
            opt.Children.Add(optTitle);
            opt.Children.Add(Line("A language model (via OpenRouter) puts the computed attribution above into words — it sees the numbers and the strategy logic. "
                                  + "The backend checks every number in the answer against the computed data. Each explanation is stored with the run and costs money.", 10.5, FundPalette.Faint));
            var modelRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 6, 0, 2) };
            modelRow.Children.Add(RowLabel("Model"));
            _standard = Radio("model" + _groupId, "Standard", "Standard model (cheaper)", true);
            _premium = Radio("model" + _groupId, "Premium", "Premium model (more expensive)", false);
            modelRow.Children.Add(_standard);
            modelRow.Children.Add(_premium);
            opt.Children.Add(modelRow);
            var langRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 2, 0, 6) };
            langRow.Children.Add(RowLabel("Language"));
            _english = Radio("lang" + _groupId, "EN", "Answer in English", true);
            _german = Radio("lang" + _groupId, "DE", "Answer in German", false);
            langRow.Children.Add(_english);
            langRow.Children.Add(_german);
            opt.Children.Add(langRow);
            var genRow = new StackPanel { Orientation = Orientation.Horizontal };
            _generate = FundUi.MakeButton("Generate", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _generate.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetName(_generate, "Generate the AI explanation");
            _generate.Click += async (_, _) => await GenerateAsync();
            genRow.Children.Add(_generate);
            var cancel = FundUi.MakeButton("Close");
            cancel.Margin = new Thickness(8, 0, 0, 0);
            cancel.Click += (_, _) => { if (!_busy) _options!.Visibility = Visibility.Collapsed; };
            genRow.Children.Add(cancel);
            opt.Children.Add(genRow);
            _budgetText = Line("", 10.5, FundPalette.Faint, mono: true);
            _budgetText.Margin = new Thickness(0, 6, 0, 0);
            opt.Children.Add(_budgetText);
            _options = new Border
            {
                Background = FundPalette.AiSurface,
                BorderBrush = FundPalette.Ai,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(12, 10, 12, 10),
                Margin = new Thickness(0, 4, 0, 8),
                Child = opt,
                Visibility = Visibility.Collapsed,
            };
            AutomationProperties.SetName(_options, "Explain with AI: options");
            Children.Add(_options);

            _status = Line("", 11, FundPalette.Ai);
            _status.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(_status, AutomationLiveSetting.Polite);
            Children.Add(_status);
            _errorHost = new StackPanel();
            Children.Add(_errorHost);
            _resultHost = new StackPanel();
            Children.Add(_resultHost);

            _versionsBar = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 4), Visibility = Visibility.Collapsed };
            _regenerate = FundUi.MakeButton("Regenerate (costs again)", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _regenerate.ToolTip = "Opens the options for a new explanation — a new call to the language model, charged again. The current one is kept.";
            _regenerate.Click += (_, _) => ShowOptions(_shown);
            _versionsBar.Children.Add(_regenerate);
            _versionsButton = FundUi.MakeButton("Earlier versions (0)");
            _versionsButton.Margin = new Thickness(8, 0, 0, 0);
            _versionsButton.Click += (_, _) => _versionsList!.Visibility = _versionsList.Visibility == Visibility.Visible ? Visibility.Collapsed : Visibility.Visible;
            _versionsBar.Children.Add(_versionsButton);
            Children.Add(_versionsBar);
            _versionsList = new StackPanel { Visibility = Visibility.Collapsed, Margin = new Thickness(0, 0, 0, 8) };
            AutomationProperties.SetName(_versionsList, "Earlier AI explanations of this run, newest first");
            Children.Add(_versionsList);

            _waitTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _waitTimer.Tick += (_, _) => ShowWaiting();

            // ----- enabled state from the card; the backend's GET …/explanations has the final word -----
            string? reason = null;
            if (_runId == null) reason = "The run card has no id.";
            else if (status != "done")
                reason = !string.IsNullOrWhiteSpace(unavailableReason) ? unavailableReason : "Only finished runs (status done) can be explained — this run is " + (status ?? "in an unknown state") + ".";
            else if (!hasAttribution)
                reason = !string.IsNullOrWhiteSpace(unavailableReason) ? unavailableReason
                       : "The backend sent no attribution and no reason — it may be outdated; restart the Hedge Fund backend.";
            SetExplainable(reason == null, reason);
            if (reason == null) Loaded += async (_, _) => await LoadOnceAsync();
        }

        // =====================================================================================
        // Loading, generating
        // =====================================================================================

        private async Task LoadOnceAsync()
        {
            if (_loaded || _runId == null) return;
            _loaded = true;
            SetStatus("Loading stored explanations…", FundPalette.Faint);
            var res = await PlatformClient.ListExplanationsAsync(_validation, _runId);
            if (_busy) return;   // a Generate started meanwhile: its result decides what is shown
            SetStatus(null, null);
            if (!res.Ok || res.Value == null)
            {
                ShowError(res.Ok ? "Unexpected response from the backend (explanations list) — backend and app versions may not match." : res.Message);
                return;
            }
            var list = res.Value;
            _items = (list.Explanations ?? new List<RunExplanation>()).ToList();
            _estimate = list.EstimateUsd;
            ApplyEstimate();
            ApplyBudget(list.Budget);
            if (list.Explainable == false)
                SetExplainable(false, string.IsNullOrWhiteSpace(list.UnavailableReason) ? "The backend refused explanations for this run (no reason given)." : list.UnavailableReason);
            if (_items.Count > 0) Show(_items[0]);
        }

        private async Task GenerateAsync()
        {
            if (_busy || _runId == null || !_explainable) return;
            var model = _premium.IsChecked == true ? PlatformClient.ExplainModelPremium : PlatformClient.ExplainModelStandard;
            var language = _german.IsChecked == true ? PlatformClient.ExplainLanguageGerman : PlatformClient.ExplainLanguageEnglish;
            _busy = true;
            _errorHost.Children.Clear();
            SetControlsEnabled(false);
            _waitStarted = DateTime.UtcNow;
            ShowWaiting();
            _waitTimer.Start();
            try
            {
                var res = await PlatformClient.CreateExplanationAsync(_validation, _runId, model, language);
                if (!res.Ok || res.Value == null)
                {
                    // budget reached / AI off / no key / bad_json / timeout / busy … — the backend's text, verbatim
                    ShowError(res.Ok ? "Unexpected response from the backend (explanation) — backend and app versions may not match." : res.Message);
                }
                else
                {
                    _items.Insert(0, res.Value);
                    _options.Visibility = Visibility.Collapsed;
                    Show(res.Value);
                }
            }
            finally
            {
                _waitTimer.Stop();
                SetStatus(null, null);
                _busy = false;
                SetControlsEnabled(true);
            }
            var budget = await PlatformClient.GetExplainSettingsAsync();
            if (budget.Ok && budget.Value != null) ApplyBudget(budget.Value);
        }

        // =====================================================================================
        // State
        // =====================================================================================

        private void SetExplainable(bool explainable, string? reason)
        {
            _explainable = explainable;
            TriggerButton.IsEnabled = explainable;
            TriggerButton.ToolTip = explainable
                ? "Puts the computed attribution into words with a language model (OpenRouter) — costs money, stored with the run. May contain errors — information, not a recommendation."
                : reason;
            AutomationProperties.SetHelpText(TriggerButton, explainable ? "" : reason ?? "");
            if (!explainable) _options.Visibility = Visibility.Collapsed;
            if (!explainable && !string.IsNullOrWhiteSpace(reason) && _loaded)
            {
                _errorHost.Children.Clear();
                _errorHost.Children.Add(Line("AI explanation not available: " + reason, 11, FundPalette.Risk));
            }
        }

        private void ToggleOptions()
        {
            if (!_explainable) return;
            if (_options.Visibility == Visibility.Visible && !_busy) _options.Visibility = Visibility.Collapsed;
            else ShowOptions(null);
        }

        private void ShowOptions(RunExplanation? like)
        {
            if (!_explainable) return;
            if (like != null)
            {
                if (like.ModelTier == PlatformClient.ExplainModelPremium) _premium.IsChecked = true;
                else if (like.ModelTier == PlatformClient.ExplainModelStandard) _standard.IsChecked = true;
                if (like.Language == PlatformClient.ExplainLanguageGerman) _german.IsChecked = true;
                else if (like.Language == PlatformClient.ExplainLanguageEnglish) _english.IsChecked = true;
            }
            _generate.Content = _items.Count > 0 ? "Generate (costs again)" : "Generate";
            _options.Visibility = Visibility.Visible;
            _options.BringIntoView();
            _generate.Focus();
        }

        private void SetControlsEnabled(bool enabled)
        {
            _generate.IsEnabled = enabled && _explainable;
            _standard.IsEnabled = enabled;
            _premium.IsEnabled = enabled;
            _english.IsEnabled = enabled;
            _german.IsEnabled = enabled;
            _regenerate.IsEnabled = enabled && _explainable;
            foreach (var b in _versionsList.Children.OfType<Button>()) b.IsEnabled = enabled;
        }

        private void ShowWaiting()
        {
            var secs = (int)Math.Max(0, (DateTime.UtcNow - _waitStarted).TotalSeconds);
            SetStatus(AiContent.Glyph + " " + WaitingText + " " + secs.ToString(Inv) + " s (the language model may take up to 90 s)", FundPalette.Ai);
        }

        private void SetStatus(string? text, Brush? brush)
        {
            _status.Text = text ?? "";
            if (brush != null) _status.Foreground = brush;
            _status.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
        }

        private void ShowError(string message)
        {
            _errorHost.Children.Clear();
            var t = FundUi.Text(message, 11.5, FundPalette.Make(0xFFF4A197));
            AutomationProperties.SetName(t, "AI explanation failed: " + message);
            _errorHost.Children.Add(new Border
            {
                Background = FundPalette.KillSurface,
                BorderBrush = FundPalette.Kill,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 4, 0, 8),
                Child = t,
            });
        }

        /// <summary>Estimated cost per tier from GET …/explanations (estimate_usd); without one, no number is shown.</summary>
        private void ApplyEstimate()
        {
            _standard.Content = "Standard" + (_estimate?.Standard is double s ? " · est. " + FundFormat.Usd(s) : "");
            _premium.Content = "Premium" + (_estimate?.Premium is double p ? " · est. " + FundFormat.Usd(p) : "");
        }

        private void ApplyBudget(ExplainSettings? b)
        {
            if (b == null)
            {
                _budgetText.Text = "";
                return;
            }
            var parts = new List<string>
            {
                "Explanation budget today: " + FundFormat.Usd(b.SpentTodayUsd) + " of " + (b.DailyBudgetUsd is double d ? FundFormat.Usd2(d) : "(not set)") + " spent",
            };
            if (b.RemainingUsd is double r) parts.Add(FundFormat.Usd(r) + " left");
            if (b.Enabled == false) parts.Add("explanations are off (budget 0) — raise it in the Hedge Fund settings");
            _budgetText.Text = string.Join(" · ", parts);
        }

        // =====================================================================================
        // Result
        // =====================================================================================

        private void Show(RunExplanation e)
        {
            _shown = e;
            _errorHost.Children.Clear();
            _resultHost.Children.Clear();
            _resultHost.Children.Add(BuildResult(e));
            _versionsBar.Visibility = Visibility.Visible;
            _regenerate.IsEnabled = _explainable && !_busy;
            _regenerate.ToolTip = _explainable
                ? (object)"Opens the options for a new explanation — a new call to the language model, charged again. The current one is kept."
                : TriggerButton.ToolTip;
            ToolTipService.SetShowOnDisabled(_regenerate, true);
            RenderVersions();
        }

        private void RenderVersions()
        {
            var earlier = Math.Max(0, _items.Count - 1);
            _versionsButton.Content = "Earlier versions (" + earlier.ToString(Inv) + ")";
            _versionsButton.IsEnabled = earlier > 0;
            _versionsList.Children.Clear();
            foreach (var item in _items)   // newest first (backend order; a new one is inserted at 0)
            {
                var current = ReferenceEquals(item, _shown);
                var b = FundUi.MakeButton(VersionText(item) + (current ? "  (shown)" : ""));
                b.HorizontalAlignment = HorizontalAlignment.Left;
                b.HorizontalContentAlignment = HorizontalAlignment.Left;
                b.Margin = new Thickness(0, 0, 0, 4);
                b.FontSize = 11;
                b.BorderBrush = current ? FundPalette.Ai : FundPalette.ButtonBorder;
                b.IsEnabled = !_busy;
                var target = item;
                b.Click += (_, _) => Show(target);
                AutomationProperties.SetName(b, "AI explanation " + VersionText(item) + (current ? ", shown" : ""));
                _versionsList.Children.Add(b);
            }
        }

        private static string VersionText(RunExplanation e)
            => FundFormat.LocalTime(e.CreatedAt, "yyyy-MM-dd HH:mm") + " · " + TierLabel(e.ModelTier) + " · " + LanguageLabel(e.Language)
               + " · " + (string.IsNullOrWhiteSpace(e.Model) ? "model not reported" : e.Model) + " · " + FundFormat.Usd(e.CostUsd)
               + (e.Check?.Ok == false ? " · " + UnverifiedCount(e).ToString(Inv) + " unverified" : "");

        private static int UnverifiedCount(RunExplanation e) => e.Check?.Unverified?.Count ?? 0;

        private static string TierLabel(string? tier) => tier switch
        {
            "standard" => "Standard",
            "premium" => "Premium",
            null or "" => "tier not reported",
            _ => tier!,
        };

        private static string LanguageLabel(string? lang) => lang switch
        {
            "en" => "English",
            "de" => "German",
            null or "" => "language not reported",
            _ => lang!,
        };

        private static Border BuildResult(RunExplanation e)
        {
            FundAi.TryRead(e.Provenance, e.Model, e.CreatedAt, out var model, out var at);
            var modelText = string.IsNullOrWhiteSpace(model) ? "model not reported" : model!;
            var timeText = at.HasValue ? at.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm", Inv) : FundFormat.LocalTime(e.CreatedAt, "yyyy-MM-dd HH:mm");
            var output = e.Output ?? new ExplanationOutput();
            var unverified = e.Check?.Unverified ?? new List<UnverifiedNumber>();
            var unlocated = new List<UnverifiedNumber>();
            var stack = new StackPanel();

            var header = FundUi.Text(AiContent.Glyph + " AI-generated · " + modelText + " · " + timeText
                                     + " — explains the computed attribution; not verified — information, not a recommendation", 10.5, FundPalette.Ai, bold: true, mono: true);
            header.Margin = new Thickness(0, 0, 0, 2);
            stack.Children.Add(header);
            var meta = TierLabel(e.ModelTier) + " · " + LanguageLabel(e.Language) + " · cost " + FundFormat.Usd(e.CostUsd)
                       + (BtJson.Bool(e.Usage, "cost_estimated") == true ? " (estimated)" : "")
                       + (BtJson.Num(e.Usage, "facts_omitted") is double om && om > 0 ? " · " + om.ToString("0", Inv) + " facts left out of the prompt (size cap)" : "");
            stack.Children.Add(Line(meta, 10.5, FundPalette.Faint, mono: true));

            // the text blocks (unverified numbers marked inside them; ones that cannot be located are listed below)
            var body = new StackPanel { Margin = new Thickness(0, 6, 0, 0) };
            var summary = Rich(output.Summary ?? "", "summary", unverified, unlocated, 12, FundPalette.TextBright, bold: false);
            summary.Margin = new Thickness(0, 0, 0, 6);
            body.Children.Add(summary);
            var sections = output.Sections ?? new List<ExplanationSection>();
            for (var i = 0; i < sections.Count; i++)
            {
                var key = "sections." + i.ToString(Inv);
                var t = Rich(sections[i].Title ?? "", key + ".title", unverified, unlocated, 11.5, FundPalette.Text, bold: true);
                t.Margin = new Thickness(0, 6, 0, 2);
                body.Children.Add(t);
                var x = Rich(sections[i].Text ?? "", key + ".text", unverified, unlocated, 11.5, FundPalette.Dim, bold: false);
                x.LineHeight = 17;
                body.Children.Add(x);
            }
            stack.Children.Add(body);

            var hypotheses = output.Hypotheses ?? new List<string>();
            if (hypotheses.Count > 0)
            {
                var hs = new StackPanel();
                var ht = FundUi.Text("Hypotheses — possible causes from outside the computed data (news, macro, sector); not checked against the data", 10.5, FundPalette.Risk, bold: true);
                ht.Margin = new Thickness(0, 0, 0, 4);
                hs.Children.Add(ht);
                for (var i = 0; i < hypotheses.Count; i++)
                    hs.Children.Add(Bullet(Rich(hypotheses[i], "hypotheses." + i.ToString(Inv), unverified, unlocated, 11.5, FundPalette.Dim, bold: false)));
                var hb = new Border
                {
                    BorderBrush = FundPalette.Risk,
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(6),
                    Padding = new Thickness(10, 6, 10, 6),
                    Margin = new Thickness(0, 8, 0, 4),
                    Child = hs,
                };
                AutomationProperties.SetName(hb, "AI-generated hypotheses, not checked against the data");
                stack.Children.Add(hb);
            }

            var limits = output.Limits ?? new List<string>();
            if (limits.Count > 0)
            {
                var lt = FundUi.Text("Limits — what the data cannot answer", 10.5, FundPalette.Text, bold: true);
                lt.Margin = new Thickness(0, 8, 0, 2);
                stack.Children.Add(lt);
                for (var i = 0; i < limits.Count; i++)
                    stack.Children.Add(Bullet(Rich(limits[i], "limits." + i.ToString(Inv), unverified, unlocated, 11, FundPalette.Dim, bold: false)));
            }

            stack.Children.Add(CheckBlock(e.Check, unverified, unlocated));

            var caveat = FundUi.Text(AiContent.Caveat, 10, FundPalette.Faint);
            caveat.FontStyle = FontStyles.Italic;
            caveat.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(caveat);

            var border = new Border
            {
                Background = FundPalette.AiSurface,
                BorderBrush = FundPalette.Ai,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(12, 10, 12, 10),
                Margin = new Thickness(0, 4, 0, 8),
                Child = stack,
            };
            AutomationProperties.SetName(border, AiContent.AutomationName(model, at) + " Explanation of the computed attribution; not verified.");
            return border;
        }

        /// <summary>Number-check summary: all found, or the unverified numbers (and unknown fact ids) listed.</summary>
        private static FrameworkElement CheckBlock(ExplanationCheck? check, List<UnverifiedNumber> unverified, List<UnverifiedNumber> unlocated)
        {
            var stack = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
            if (check == null)
            {
                stack.Children.Add(Line("Number check: the backend sent no check result.", 10.5, FundPalette.Risk));
                return stack;
            }
            var unknown = check.UnknownIds ?? new List<string>();
            if (unverified.Count == 0 && unknown.Count == 0)
            {
                stack.Children.Add(Line("Number check: every number in the text was found in the computed data" + (check.Ok == true ? "." : " (check status not ok — see the backend)."), 10.5,
                                        check.Ok == true ? FundPalette.Faint : FundPalette.Risk));
                return stack;
            }
            if (unverified.Count > 0)
            {
                var t = Line("Number check: " + unverified.Count.ToString(Inv) + (unverified.Count == 1 ? " number is" : " numbers are")
                             + " not in the computed data (marked red): "
                             + string.Join(", ", unverified.Select(u => "\"" + (u.Text ?? "?") + "\" (" + SectionLabel(u.Section) + ")")), 10.5, FundPalette.Kill);
                stack.Children.Add(t);
            }
            if (unlocated.Count > 0)
                stack.Children.Add(Line("Could not be marked in the text (position did not match): "
                                        + string.Join(", ", unlocated.Select(u => "\"" + (u.Text ?? "?") + "\" (" + SectionLabel(u.Section) + ")")), 10.5, FundPalette.Kill));
            if (unknown.Count > 0)
                stack.Children.Add(Line("Cited fact ids that do not exist: " + string.Join(", ", unknown), 10.5, FundPalette.Kill));
            return stack;
        }

        private static string SectionLabel(string? key)
        {
            if (string.IsNullOrWhiteSpace(key)) return "unknown place";
            var parts = key!.Split('.');
            if (parts.Length == 1) return parts[0];
            if (!int.TryParse(parts[1], NumberStyles.Integer, Inv, out var n)) return key;
            var idx = (n + 1).ToString(Inv);
            return parts[0] switch
            {
                "sections" => "section " + idx + (parts.Length > 2 && parts[2] == "title" ? " title" : ""),
                "hypotheses" => "hypothesis " + idx,
                "limits" => "limit " + idx,
                _ => key,
            };
        }

        /// <summary>
        /// Plain-text TextBlock of one output field built from Runs only (no markup is interpreted, no hyperlinks):
        /// [id] citations dimmed, check.unverified numbers of this field red with the tooltip "not in the computed data".
        /// Offsets are Python string indices (code points) — converted to UTF-16; an entry whose text does not stand at its
        /// offset is not guessed elsewhere but added to <paramref name="unlocated"/> (listed under the explanation).
        /// </summary>
        internal static TextBlock Rich(string text, string field, List<UnverifiedNumber> unverified, List<UnverifiedNumber> unlocated,
                                      double size, Brush brush, bool bold)
        {
            var tb = FundUi.Text("", size, brush, bold: bold);
            var marks = new byte[text.Length];   // 0 text, 1 citation, 2 unverified
            var flagged = new List<string>();
            foreach (var u in unverified.Where(x => x.Section == field))
            {
                var needle = u.Text ?? "";
                var start = u.Offset is int cp ? Utf16Index(text, cp) : null;
                if (needle.Length == 0 || start is not int s || s + needle.Length > text.Length
                    || string.CompareOrdinal(text, s, needle, 0, needle.Length) != 0)
                {
                    unlocated.Add(u);
                    continue;
                }
                for (var i = s; i < s + needle.Length; i++) marks[i] = 2;
                flagged.Add(needle);
            }
            foreach (Match m in Citation.Matches(text))
                for (var i = m.Index; i < m.Index + m.Length; i++)
                    if (marks[i] == 0) marks[i] = 1;

            var pos = 0;
            while (pos < text.Length)
            {
                var kind = marks[pos];
                var end = pos;
                while (end < text.Length && marks[end] == kind) end++;
                var run = new Run(text.Substring(pos, end - pos));
                if (kind == 1)
                {
                    run.Foreground = FundPalette.Faint;
                    run.FontSize = Math.Max(9, size - 1.5);
                }
                else if (kind == 2)
                {
                    run.Foreground = FundPalette.Kill;
                    run.Background = FundPalette.KillSurface;
                    run.TextDecorations = TextDecorations.Underline;
                    run.ToolTip = UnverifiedTooltip;
                }
                tb.Inlines.Add(run);
                pos = end;
            }
            if (text.Length == 0) tb.Inlines.Add(new Run("(empty)") { Foreground = FundPalette.Faint });
            AutomationProperties.SetName(tb, text + (flagged.Count > 0 ? " — not in the computed data: " + string.Join(", ", flagged) : ""));
            return tb;
        }

        /// <summary>UTF-16 index of the code point at <paramref name="codePoints"/>, or null when the text is shorter.</summary>
        private static int? Utf16Index(string s, int codePoints)
        {
            if (codePoints < 0) return null;
            var i = 0;
            var cp = 0;
            while (i < s.Length && cp < codePoints)
            {
                i += char.IsHighSurrogate(s[i]) && i + 1 < s.Length && char.IsLowSurrogate(s[i + 1]) ? 2 : 1;
                cp++;
            }
            return cp == codePoints ? i : null;
        }

        // =====================================================================================
        // Small builders
        // =====================================================================================

        private static FrameworkElement Bullet(TextBlock text)
        {
            var grid = new Grid { Margin = new Thickness(0, 1, 0, 2) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(14) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.Children.Add(FundUi.Text("•", 11.5, FundPalette.Faint));
            Grid.SetColumn(text, 1);
            grid.Children.Add(text);
            return grid;
        }

        private static TextBlock RowLabel(string text)
        {
            var t = FundUi.Text(text, 10.5, FundPalette.Faint, wrap: false);
            t.Width = 70;
            t.VerticalAlignment = VerticalAlignment.Center;
            return t;
        }

        private static RadioButton Radio(string group, string content, string name, bool isChecked)
        {
            var rb = new RadioButton
            {
                GroupName = group,
                Content = content,
                IsChecked = isChecked,
                Foreground = FundPalette.Text,
                FontSize = 12,
                Margin = new Thickness(0, 0, 18, 0),
                VerticalAlignment = VerticalAlignment.Center,
            };
            AutomationProperties.SetName(rb, name);
            return rb;
        }

        private static TextBlock Line(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
            return t;
        }
    }
}

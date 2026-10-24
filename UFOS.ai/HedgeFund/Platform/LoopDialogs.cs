using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// "✦ New loop…" (Strategy Platform stage 6, SPEC §16.1/§16.12/§16.13): seed = a Library strategy (+ optional version) |
    /// an idea in words (1..4,000 characters) | a confirmed paper; variants N × generations G (the backend's defaults, 6 × 4,
    /// max 8 × 5) and search points P; Standard / Premium; data from 2016 or 2000; auto-resume. Below: the backend's
    /// pre-start estimate (GET /v2/loops/estimate — planned backtests = trials, AI calls, USD), the loop AI budget incl. the
    /// per-run cap, the projected G4 impact as a warning, and the "fully autonomous" switch state from Settings (snapshot
    /// at the start; the holdout always stays manual). The request is sent from the dialog: a refusal (400 / 402 budget /
    /// 404 / 409 paper_not_confirmed / 503 …) keeps the dialog open and shows the backend's text verbatim; a 409 busy with
    /// a loop id closes it so the caller can show the running loop. Inputs are format-checked only; the backend validates.
    /// </summary>
    internal sealed class LoopStartDialog : FundPopupWindow
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        // SPEC §16.1 contract values — used only while GET /v2/settings/loop did not answer (said so in the dialog)
        private static readonly (int Lo, int Hi, int Def) ContractVariants = (2, 8, 6);
        private static readonly (int Lo, int Hi, int Def) ContractGenerations = (1, 5, 4);
        private static readonly (int Lo, int Hi, int Def) ContractSearch = (0, 3, 1);

        private readonly RadioButton _seedStrategy;
        private readonly RadioButton _seedIdea;
        private readonly RadioButton _seedPaper;
        private readonly StackPanel _strategyPanel;
        private readonly StackPanel _ideaPanel;
        private readonly StackPanel _paperPanel;
        private readonly TextBox _strategyFilter;
        private readonly ListBox _strategyList;
        private readonly TextBox _version;
        private readonly TextBox _idea;
        private readonly TextBlock _ideaCount;
        private readonly ListBox _paperList;
        private readonly NumberChoice _variants;
        private readonly NumberChoice _generations;
        private readonly NumberChoice _search;
        private readonly RadioButton _standard;
        private readonly RadioButton _premium;
        private readonly CheckBox _from2000;
        private readonly CheckBox _autoResume;
        private readonly StackPanel _estimateHost;
        private readonly TextBlock _error;
        private readonly Button _start;
        private readonly Button _cancel;
        private readonly DispatcherTimer _estimateTimer;
        private readonly List<StrategySummary> _strategies;
        private readonly bool? _autonomous;
        private int _estimateSeq;
        private bool _submitting;

        /// <summary>Result of POST /v2/loops (success, or a 409 busy with the running loop's id).</summary>
        public PlatformResult<LoopDetail>? StartResult { get; private set; }

        public LoopStartDialog(Window? owner, PlatformResult<StrategyList> strategies, PlatformResult<PaperList> papers,
                               PlatformResult<LoopSettings> settings, string? preselectStrategyId)
            : base(owner, "Start discovery loop", 660, 800, resizable: true)
        {
            _strategies = (strategies.Ok ? strategies.Value?.Strategies : null)?.Where(s => s.Id.Length > 0)
                              .OrderBy(s => s.Name ?? s.Id, StringComparer.OrdinalIgnoreCase).ToList() ?? new List<StrategySummary>();
            var cfg = settings.Ok ? settings.Value : null;
            _autonomous = cfg?.Autonomous;

            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 8) };
            stack.Children.Add(FundUi.Text("Start discovery loop", 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text("A population of N variants × G generations: every variant is backtested and scored deterministically, an AI diagnoses "
                                    + "the results and refines the spec (Studio validator, topic check), a bounded parameter search adds a few versions, "
                                    + "the best variants are kept and the worst culled. The loop runs in the backend — you can leave this view; "
                                    + "one loop runs at a time and shares the single backtest slot.", 11.5, FundPalette.Dim);
            intro.Margin = new Thickness(0, 6, 0, 10);
            intro.LineHeight = 17;
            stack.Children.Add(intro);

            // ----- seed -----
            stack.Children.Add(Head("Seed"));
            var seedRow = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            AutomationProperties.SetName(seedRow, "Seed kind");
            _seedStrategy = Radio("LoopSeed", "Library strategy", "Seed: a strategy from the Library", true);
            _seedIdea = Radio("LoopSeed", "Idea in words", "Seed: an idea in words (the AI drafts the first strategy)", false);
            _seedPaper = Radio("LoopSeed", "Confirmed paper", "Seed: the strategy of a confirmed research paper", false);
            seedRow.Children.Add(_seedStrategy);
            seedRow.Children.Add(_seedIdea);
            seedRow.Children.Add(_seedPaper);
            stack.Children.Add(seedRow);

            _strategyPanel = new StackPanel();
            if (!strategies.Ok || strategies.Value == null)
                _strategyPanel.Children.Add(Note("Strategies could not be loaded: " + (strategies.Ok ? "unexpected response from the backend (/v2/strategies)." : strategies.Message), FundPalette.Kill));
            _strategyFilter = FundUi.Input("", mono: false);
            _strategyFilter.Margin = new Thickness(0, 0, 0, 4);
            AutomationProperties.SetName(_strategyFilter, "Filter strategies by name or id");
            _strategyFilter.ToolTip = "Filter by name or id";
            _strategyFilter.TextChanged += (_, _) => FillStrategies(SelectedStrategyId());
            _strategyPanel.Children.Add(Label("Strategy (any strategy, incl. alpha-library entries — the loop's variants are new strategies in its family; it is never changed)"));
            _strategyPanel.Children.Add(_strategyFilter);
            _strategyList = MakeList("Strategies", 150);
            _strategyList.SelectionChanged += (_, _) => { UpdateVersionHint(); ScheduleEstimate(); };
            _strategyPanel.Children.Add(_strategyList);
            _version = FundUi.Input("");
            _version.Margin = new Thickness(0, 4, 0, 2);
            _strategyPanel.Children.Add(Label("Version (optional — empty = the strategy's current version)"));
            _strategyPanel.Children.Add(_version);
            AutomationProperties.SetName(_version, "Seed strategy version, optional");
            stack.Children.Add(_strategyPanel);

            _ideaPanel = new StackPanel { Visibility = Visibility.Collapsed };
            _ideaPanel.Children.Add(Label("Idea (1–4,000 characters). One AI call drafts the root strategy (Studio prompt, validator, topic check); "
                                          + "an invalid or off-topic draft ends the loop as failed — no stand-in idea."));
            _idea = FundUi.Input("", mono: false);
            _idea.AcceptsReturn = true;
            _idea.TextWrapping = TextWrapping.Wrap;
            _idea.MinHeight = 80;
            _idea.MaxHeight = 160;
            _idea.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
            _idea.MaxLength = PlatformClient.LoopIdeaMaxChars;
            AutomationProperties.SetName(_idea, "Strategy idea in words");
            _ideaCount = FundUi.Text("0 / 4,000", 10, FundPalette.Faint, mono: true);
            _idea.TextChanged += (_, _) => _ideaCount.Text = (_idea.Text ?? "").Length.ToString("N0", Inv) + " / " + PlatformClient.LoopIdeaMaxChars.ToString("N0", Inv);
            _ideaPanel.Children.Add(_idea);
            _ideaPanel.Children.Add(_ideaCount);
            stack.Children.Add(_ideaPanel);

            _paperPanel = new StackPanel { Visibility = Visibility.Collapsed };
            _paperPanel.Children.Add(Label("Confirmed paper (its confirmed strategy, current version, is the seed)"));
            _paperList = MakeList("Confirmed papers", 120);
            _paperList.SelectionChanged += (_, _) => ScheduleEstimate();
            var confirmed = (papers.Ok ? papers.Value?.Papers : null)?.Where(p => p.Status == "confirmed" && !string.IsNullOrWhiteSpace(p.StrategyId) && !string.IsNullOrWhiteSpace(p.Id)).ToList()
                            ?? new List<PaperInfo>();
            if (!papers.Ok || papers.Value == null)
                _paperPanel.Children.Add(Note("Papers could not be loaded: " + (papers.Ok ? "unexpected response from the backend (/v2/papers)." : papers.Message), FundPalette.Kill));
            else if (confirmed.Count == 0)
                _paperPanel.Children.Add(Note("No confirmed paper with a strategy yet — confirm one in Studio → Paper first.", FundPalette.Risk));
            foreach (var p in confirmed)
            {
                var text = (string.IsNullOrWhiteSpace(p.Title) ? "(untitled paper)" : p.Title!) + (p.Year is int y ? " (" + y.ToString(Inv) + ")" : "")
                           + " · " + p.Id + " · strategy " + p.StrategyId;
                var item = new ListBoxItem { Content = FundUi.Text(text, 11, FundPalette.Text, wrap: false), Tag = p, Padding = new Thickness(6, 2, 6, 2) };
                AutomationProperties.SetName(item, "Paper " + text);
                _paperList.Items.Add(item);
            }
            _paperPanel.Children.Add(_paperList);
            stack.Children.Add(_paperPanel);

            // ----- population -----
            stack.Children.Add(Head("Population"));
            var limits = cfg?.Limits;
            var vr = Range(limits, "variants", ContractVariants, cfg?.Defaults?.Variants);
            var gr = Range(limits, "generations", ContractGenerations, cfg?.Defaults?.Generations);
            var sr = Range(limits, "search_points", ContractSearch, cfg?.Defaults?.SearchPoints);
            _variants = new NumberChoice("LoopVariants", "Variants per generation", vr.Lo, vr.Hi, vr.Def, ScheduleEstimate);
            _generations = new NumberChoice("LoopGenerations", "Generations", gr.Lo, gr.Hi, gr.Def, ScheduleEstimate);
            _search = new NumberChoice("LoopSearch", "Parameter-search points per new variant", sr.Lo, sr.Hi, sr.Def, ScheduleEstimate);
            stack.Children.Add(Label("Variants per generation (" + vr.Lo.ToString(Inv) + "–" + vr.Hi.ToString(Inv) + ", default " + vr.Def.ToString(Inv) + ")"));
            stack.Children.Add(_variants.Panel);
            stack.Children.Add(Label("Generations (" + gr.Lo.ToString(Inv) + "–" + gr.Hi.ToString(Inv) + ", default " + gr.Def.ToString(Inv) + ")"));
            stack.Children.Add(_generations.Panel);
            stack.Children.Add(Label("Parameter-search points per new variant (" + sr.Lo.ToString(Inv) + "–" + sr.Hi.ToString(Inv) + ", default " + sr.Def.ToString(Inv)
                                     + ") — each is one more version + backtest (= trial)"));
            stack.Children.Add(_search.Panel);
            if (cfg == null)
                stack.Children.Add(Note("Loop settings could not be loaded (" + (settings.Ok ? "unexpected response from the backend (/v2/settings/loop)" : settings.Message)
                                        + ") — the choices above use the contract's ranges and defaults (SPEC §16.1).", FundPalette.Risk));

            var modelRow = new WrapPanel { Margin = new Thickness(0, 6, 0, 6) };
            AutomationProperties.SetName(modelRow, "Model");
            var ml = FundUi.Text("Model", 10.5, FundPalette.Faint, wrap: false);
            ml.VerticalAlignment = VerticalAlignment.Center;
            ml.Margin = new Thickness(0, 0, 10, 0);
            modelRow.Children.Add(ml);
            _standard = Radio("LoopModel", "Standard", "Standard model (cheaper)", true);
            _premium = Radio("LoopModel", "Premium", "Premium model (more expensive)", false);
            _standard.Checked += (_, _) => ScheduleEstimate();
            _premium.Checked += (_, _) => ScheduleEstimate();
            modelRow.Children.Add(_standard);
            modelRow.Children.Add(_premium);
            stack.Children.Add(modelRow);

            _from2000 = new CheckBox
            {
                Content = "Load data from 2000 (incl. 2001/2008 crises — needs the Tiingo key for data before 2016)",
                Foreground = FundPalette.Text,
                FontSize = 11.5,
                Margin = new Thickness(0, 0, 0, 6),
                ToolTip = "history_from = \"2000\" for every loop backtest; without it the backtests load data from 2016-01-04. Without the Tiingo key the backtests fail with the backend's message.",
            };
            AutomationProperties.SetName(_from2000, "Load backtest data from 2000; needs the Tiingo key");
            stack.Children.Add(_from2000);
            _autoResume = new CheckBox
            {
                Content = "Continue automatically after a backend restart or a lost connection",
                Foreground = FundPalette.Text,
                FontSize = 11.5,
                IsChecked = true,
                Margin = new Thickness(0, 0, 0, 10),
                ToolTip = "auto_resume (default on): an interrupted loop continues after the backend started again; a loop backtest that lost the connection "
                          + "waits and resumes. Completed steps are never recomputed. Off: use Resume yourself.",
            };
            AutomationProperties.SetName(_autoResume, "Continue the loop automatically after a backend restart or a lost connection");
            stack.Children.Add(_autoResume);

            // ----- autonomous switch state -----
            stack.Children.Add(Head("Finalists"));
            var autoText = _autonomous switch
            {
                true => "Fully autonomous: ON (from Settings). The loop validates its finalists itself (gates G1–G8) — you are not asked to approve them.",
                false => "Fully autonomous: OFF (from Settings). The finalists are proposed for your approval; an approved finalist runs through the validation gates G1–G8 (pass → candidate proposal; \"Validated\" needs the holdout, opened only by you).",
                _ => "Fully autonomous: not known — the loop settings could not be loaded. The backend uses its stored setting.",
            };
            stack.Children.Add(Note(autoText + " The switch is snapshot at the start (change it under Loop → Settings…).", _autonomous == true ? FundPalette.Risk : FundPalette.Text));
            stack.Children.Add(Note(cfg?.HoldoutNote ?? "The holdout always stays manual: the loop never opens it.", FundPalette.Dim));

            // ----- estimate (live click-test 2026-10-22: pinned above "✦ Start loop" — at the end of the long form it was out of view) -----
            _estimateHost = new StackPanel();
            AutomationProperties.SetName(_estimateHost, "Estimate before the start");
            AutomationProperties.SetLiveSetting(_estimateHost, AutomationLiveSetting.Polite);

            _error = FundUi.Text("", 11, FundPalette.Kill);
            _error.Margin = new Thickness(0, 8, 0, 4);
            _error.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(_error, AutomationLiveSetting.Assertive);
            stack.Children.Add(_error);
            var disclaimer = FundUi.Text("Hypothetical, simulated results — they do not indicate future results. Paper trading only.", 10.5, FundPalette.Faint);
            disclaimer.FontStyle = FontStyles.Italic;
            disclaimer.Margin = new Thickness(0, 4, 0, 4);
            stack.Children.Add(disclaimer);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(20, 6, 20, 14) };
            _cancel = FundUi.MakeButton("Cancel");
            _cancel.IsCancel = true;
            _cancel.Click += (_, _) => { if (!_submitting) DialogResult = false; };
            _start = FundUi.MakeButton(AiContent.Glyph + " Start loop", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _start.Margin = new Thickness(8, 0, 0, 0);
            _start.ToolTip = "Starts the loop (Ctrl+Enter). " + AiContent.TriggerTooltip + " Costs money (loop AI budget); every backtest is a trial of the family.";
            AutomationProperties.SetName(_start, "Start the discovery loop (AI)");
            AutomationProperties.SetHelpText(_start, AiContent.TriggerTooltip);
            _start.Click += async (_, _) => await SubmitAsync();
            buttons.Children.Add(_cancel);
            buttons.Children.Add(_start);

            var layout = new Grid();
            layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack, Focusable = false };
            layout.Children.Add(scroll);
            // estimate + budget + amber G4 box, always visible next to the start button (updates on every seed / variants /
            // generations / search points / tier change, debounced 400 ms)
            var estimateStack = new StackPanel();
            estimateStack.Children.Add(Head("Estimate, budget and G4 impact"));
            estimateStack.Children.Add(_estimateHost);
            var estimatePanel = new Border
            {
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(0, 1, 0, 0),
                Padding = new Thickness(20, 4, 20, 0),
                Child = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = estimateStack, Focusable = false, MaxHeight = 260 },
            };
            Grid.SetRow(estimatePanel, 1);
            layout.Children.Add(estimatePanel);
            Grid.SetRow(buttons, 2);
            layout.Children.Add(buttons);
            SetBody(layout);

            _estimateTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(400) };
            _estimateTimer.Tick += async (_, _) => await RunEstimateAsync();
            _seedStrategy.Checked += (_, _) => SeedChanged();
            _seedIdea.Checked += (_, _) => SeedChanged();
            _seedPaper.Checked += (_, _) => SeedChanged();
            FillStrategies(preselectStrategyId);
            PreviewKeyDown += async (_, e) =>
            {
                if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control)
                {
                    e.Handled = true;
                    await SubmitAsync();
                }
            };
            Loaded += (_, _) =>
            {
                _seedStrategy.Focus();
                ScheduleEstimate();
            };
            // no close while the request is in flight (its result decides what happens next)
            Closing += (_, e) => { if (_submitting) e.Cancel = true; else _estimateTimer.Stop(); };
        }

        // ----- seed -----

        private string SeedKind() => _seedIdea.IsChecked == true ? "idea" : _seedPaper.IsChecked == true ? "paper" : "strategy";

        private void SeedChanged()
        {
            var kind = SeedKind();
            _strategyPanel.Visibility = kind == "strategy" ? Visibility.Visible : Visibility.Collapsed;
            _ideaPanel.Visibility = kind == "idea" ? Visibility.Visible : Visibility.Collapsed;
            _paperPanel.Visibility = kind == "paper" ? Visibility.Visible : Visibility.Collapsed;
            ScheduleEstimate();
        }

        private void FillStrategies(string? selectId)
        {
            var filter = (_strategyFilter.Text ?? "").Trim();
            _strategyList.Items.Clear();
            ListBoxItem? select = null;
            foreach (var s in _strategies)
            {
                var name = string.IsNullOrWhiteSpace(s.Name) ? "(unnamed)" : s.Name!;
                if (filter.Length > 0 && name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0 && s.Id.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                var text = name + " · " + StrategyStates.Label(s.State) + " · v" + (s.CurrentVersion?.ToString(Inv) ?? "?") + " · "
                           + (s.IsLibrary ? "alpha library" : s.Origin ?? "?") + (s.TrialCount is int t ? " · " + t.ToString(Inv) + " trials" : "");
                var item = new ListBoxItem { Content = FundUi.Text(text, 11, FundPalette.Text, wrap: false), Tag = s.Id, Padding = new Thickness(6, 2, 6, 2) };
                TextSearch.SetText(item, name);
                AutomationProperties.SetName(item, "Strategy " + text);
                _strategyList.Items.Add(item);
                if (selectId != null && s.Id == selectId) select = item;
            }
            if (_strategyList.Items.Count == 0)
                _strategyList.Items.Add(new ListBoxItem { Content = FundUi.Text(_strategies.Count == 0 ? "No strategies." : "No strategy matches the filter.", 11, FundPalette.Faint), IsEnabled = false });
            if (select != null)
            {
                _strategyList.SelectedItem = select;
                _strategyList.ScrollIntoView(select);
            }
        }

        private string? SelectedStrategyId() => (_strategyList.SelectedItem as ListBoxItem)?.Tag as string;

        private StrategySummary? SelectedStrategy()
        {
            var id = SelectedStrategyId();
            return id == null ? null : _strategies.FirstOrDefault(s => s.Id == id);
        }

        private PaperInfo? SelectedPaper() => (_paperList.SelectedItem as ListBoxItem)?.Tag as PaperInfo;

        private void UpdateVersionHint()
        {
            var s = SelectedStrategy();
            _version.ToolTip = s?.CurrentVersion is int cv ? "Empty = current version v" + cv.ToString(Inv) : "Empty = the strategy's current version";
        }

        // ----- estimate -----

        private void ScheduleEstimate()
        {
            if (_estimateTimer == null) return;   // raised while the controls are being built
            _estimateTimer.Stop();
            _estimateTimer.Start();
        }

        private string Model() => _premium.IsChecked == true ? PlatformClient.ExplainModelPremium : PlatformClient.ExplainModelStandard;

        private async Task RunEstimateAsync()
        {
            _estimateTimer.Stop();
            var seq = ++_estimateSeq;
            string? strategyId = null;
            var idea = false;
            switch (SeedKind())
            {
                case "strategy":
                    strategyId = SelectedStrategyId();
                    if (strategyId == null)
                    {
                        ShowEstimate(Note("Select a strategy to see the estimate (planned backtests, AI calls, cost and the G4 impact).", FundPalette.Faint));
                        return;
                    }
                    break;
                case "paper":
                    strategyId = SelectedPaper()?.StrategyId;
                    if (strategyId == null)
                    {
                        ShowEstimate(Note("Select a confirmed paper to see the estimate.", FundPalette.Faint));
                        return;
                    }
                    break;
                default:
                    idea = true;   // a new family: no trials yet
                    break;
            }
            ShowEstimate(Note("Estimating…", FundPalette.Faint));
            var res = await PlatformClient.EstimateLoopAsync(strategyId, idea, _variants.Value, _generations.Value, _search.Value, Model());
            if (seq != _estimateSeq || !IsLoaded) return;
            if (!res.Ok || res.Value == null)
            {
                ShowEstimate(Note("Estimate unavailable: " + (res.Ok ? "unexpected response from the backend (/v2/loops/estimate)." : res.Message), FundPalette.Kill));
                return;
            }
            var e = res.Value;
            var list = new List<UIElement>();
            var tier = LoopText.TierLabel(Model());
            list.Add(Note("Planned: " + Int(e.BacktestsPlanned) + " backtests (each one a trial of the seed's family) · " + Int(e.AiCallsPlanned) + " AI calls · estimated "
                          + FundFormat.Usd(e.EstimateUsd) + " (≈ " + FundFormat.Usd(e.EstimatePerCallUsd) + " per call, " + tier + ")"
                          + (_autonomous == true && e.ValidationsIfAutonomous is int vi ? " · then up to " + vi.ToString(Inv) + " validation runs of the finalists (no AI)" : ""),
                          FundPalette.Text, mono: true));
            list.Add(Note(LoopText.DescriptorLine(e.Budget), FundPalette.Faint, mono: true));
            var cap = LoopText.DescriptorLimit(e.Budget, "per_run_usd");
            var daily = LoopText.DescriptorLimit(e.Budget, "daily_usd");
            var spentToday = BtJson.Num(e.Budget, "spent", "today_usd");
            if (BtJson.Bool(e.Budget, "enabled") == false)
                list.Add(Note("The loop AI is off (a limit is 0) — the start will be refused. Set the limits in Settings… or the central AI budget window.", FundPalette.Kill));
            else if (cap is double c && e.EstimateUsd is double est && est > c)
                list.Add(Note("The estimate exceeds the per-run AI cap (" + FundFormat.Usd2(c) + "): the loop stops creating variants when the cap is reached "
                              + "(\"stopped early: per-run AI cap reached\"), finishes the pending backtests and the selection. Raise the cap in Settings… for the full run.", FundPalette.Risk));
            if (daily is double dc && spentToday is double st && e.EstimateUsd is double est2 && st + est2 > dc && BtJson.Bool(e.Budget, "enabled") != false)
                list.Add(Note("Today's remaining loop budget (" + FundFormat.Usd(Math.Max(0, dc - st)) + ") is below the estimate — the loop may pause (\"paused — loop AI budget reached\") "
                              + "and continue after you raise it or on the next UTC day.", FundPalette.Risk));
            var g4 = new StackPanel();
            g4.Children.Add(FundUi.Text("⚠ " + LoopText.TrialsLine(e.Trials), 11, FundPalette.Risk, bold: true));
            g4.Children.Add(FundUi.Text("More trials raise the bar of the deflated-Sharpe gate (G4) for every strategy of this family — the loop's finalists and your other "
                                        + "variants of it. Duplicates (identical logic) are refused before their backtest and cost no trial.", 10.5, FundPalette.Dim));
            if (BtJson.Str(e.Trials, "note") is { Length: > 0 } tn) g4.Children.Add(FundUi.Text(tn, 10.5, FundPalette.Faint));
            var g4Box = new Border
            {
                BorderBrush = FundPalette.Risk,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 4, 0, 4),
                Child = g4,
            };
            AutomationProperties.SetName(g4Box, "Warning: " + LoopText.TrialsLine(e.Trials));
            list.Add(g4Box);
            if (!string.IsNullOrWhiteSpace(e.Note)) list.Add(Note(e.Note!, FundPalette.Faint));
            ShowEstimate(list.ToArray());
        }

        private void ShowEstimate(params UIElement[] elements)
        {
            _estimateHost.Children.Clear();
            foreach (var el in elements) _estimateHost.Children.Add(el);
        }

        // ----- submit -----

        private async Task SubmitAsync()
        {
            if (_submitting) return;
            var problem = Build(out var request);
            if (problem != null || request == null)
            {
                ShowError(problem ?? "The request could not be built.");
                return;
            }
            _submitting = true;
            _start.IsEnabled = false;
            _cancel.IsEnabled = false;
            _error.Visibility = Visibility.Collapsed;
            PlatformResult<LoopDetail> res;
            try
            {
                res = await PlatformClient.StartLoopAsync(request);
            }
            finally
            {
                _submitting = false;
                _start.IsEnabled = true;
                _cancel.IsEnabled = true;
            }
            if (res.Ok || (res.Status == PlatformCallStatus.Conflict && PlatformClient.ReadConflictLoopId(res) != null))
            {
                StartResult = res;
                DialogResult = true;
                return;
            }
            // 400 / 402 budget / 404 / 409 paper_not_confirmed / 503 … — verbatim, the dialog stays open
            ShowError(res.Message);
        }

        private string? Build(out LoopStartRequest? request)
        {
            request = null;
            var kind = SeedKind();
            string? strategyId = null;
            int? version = null;
            string? idea = null;
            string? paperId = null;
            switch (kind)
            {
                case "strategy":
                {
                    strategyId = SelectedStrategyId();
                    if (strategyId == null) return "Select the seed strategy.";
                    var vt = (_version.Text ?? "").Trim();
                    if (vt.Length > 0)
                    {
                        if (!int.TryParse(vt, NumberStyles.None, Inv, out var v) || v < 1) return "Version must be empty or a whole number ≥ 1.";
                        version = v;
                    }
                    break;
                }
                case "idea":
                    idea = (_idea.Text ?? "").Trim();
                    if (idea.Length == 0) return "Describe the idea first.";
                    if (idea.Length > PlatformClient.LoopIdeaMaxChars) return "The idea is longer than 4,000 characters.";
                    break;
                default:
                {
                    var p = SelectedPaper();
                    if (p?.Id == null) return "Select a confirmed paper.";
                    paperId = p.Id;
                    break;
                }
            }
            request = new LoopStartRequest
            {
                SeedKind = kind,
                StrategyId = strategyId,
                StrategyVersion = version,
                IdeaText = idea,
                PaperId = paperId,
                Variants = _variants.Value,
                Generations = _generations.Value,
                SearchPoints = _search.Value,
                Model = Model(),
                HistoryFrom2000 = _from2000.IsChecked == true,
                AutoResume = _autoResume.IsChecked == true,
            };
            return null;
        }

        private void ShowError(string text)
        {
            _error.Text = text;
            _error.Visibility = Visibility.Visible;
        }

        // ----- builders -----

        /// <summary>[lo, hi] from the settings' `limits` ([lo, hi] arrays) and the default; the contract values when missing.</summary>
        private static (int Lo, int Hi, int Def) Range(JsonElement? limits, string key, (int Lo, int Hi, int Def) contract, int? def)
        {
            var arr = BtJson.Array(limits, key).Select(x => x.ValueKind == JsonValueKind.Number && x.TryGetInt32(out var n) ? n : (int?)null).ToList();
            var lo = arr.Count == 2 && arr[0] is int a ? a : contract.Lo;
            var hi = arr.Count == 2 && arr[1] is int b && b >= lo ? b : Math.Max(lo, contract.Hi);
            var d = def is int dv && dv >= lo && dv <= hi ? dv : Math.Min(hi, Math.Max(lo, contract.Def));
            return (lo, hi, d);
        }

        private static string Int(int? v) => v?.ToString(Inv) ?? "—";

        private static TextBlock Head(string text)
        {
            var t = FundUi.Text(text.ToUpperInvariant(), 10.5, FundPalette.Faint, bold: true);
            t.Margin = new Thickness(0, 8, 0, 4);
            return t;
        }

        private static TextBlock Label(string text)
        {
            var t = FundUi.Text(text, 10.5, FundPalette.Faint);
            t.Margin = new Thickness(0, 2, 0, 3);
            return t;
        }

        private static TextBlock Note(string text, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, 10.5, brush, mono: mono);
            t.Margin = new Thickness(0, 2, 0, 3);
            return t;
        }

        private static ListBox MakeList(string name, double height)
        {
            var list = new ListBox
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                Foreground = FundPalette.Text,
                Height = height,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
            };
            ScrollViewer.SetHorizontalScrollBarVisibility(list, ScrollBarVisibility.Disabled);
            AutomationProperties.SetName(list, name);
            return list;
        }

        internal static RadioButton Radio(string group, string content, string name, bool isChecked)
        {
            var rb = new RadioButton
            {
                GroupName = group,
                Content = content,
                IsChecked = isChecked,
                Foreground = FundPalette.Text,
                FontSize = 12,
                Margin = new Thickness(0, 0, 18, 4),
                VerticalAlignment = VerticalAlignment.Center,
            };
            AutomationProperties.SetName(rb, name);
            return rb;
        }

        /// <summary>A row of radio buttons lo..hi (keyboard: Tab to the row, arrow keys / Space to choose).</summary>
        private sealed class NumberChoice
        {
            private readonly List<(RadioButton Button, int Value)> _items = new();
            public WrapPanel Panel { get; } = new() { Margin = new Thickness(0, 0, 0, 6) };

            public NumberChoice(string group, string name, int lo, int hi, int def, Action changed)
            {
                AutomationProperties.SetName(Panel, name);
                for (var n = lo; n <= hi; n++)
                {
                    var rb = Radio(group, n.ToString(Inv), name + " " + n.ToString(Inv), n == def);
                    rb.Margin = new Thickness(0, 0, 14, 2);
                    rb.Checked += (_, _) => changed();
                    _items.Add((rb, n));
                    Panel.Children.Add(rb);
                }
            }

            public int Value
            {
                get
                {
                    foreach (var (button, value) in _items)
                        if (button.IsChecked == true) return value;
                    return _items[0].Value;
                }
            }
        }
    }

    /// <summary>
    /// Loop → "Settings…" (SPEC §16.12 GET/PUT /v2/settings/loop): the "fully autonomous" switch — turning it ON needs an explicit
    /// confirmation (the loop then validates its finalists G1–G8 itself; the holdout stays manual), turning it off is immediate —
    /// the three loop AI limits (day / month / per run, 0 = off, saved with "Save limits"; also in the central AI budget window),
    /// the spend of the UTC day / month and the backend's defaults and ranges for new loops (read-only). Inputs stay disabled
    /// until the backend answered; every refusal is shown verbatim and the switch returns to the stored state.
    /// </summary>
    internal sealed class LoopSettingsDialog : FundPopupWindow
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private const double SpecMax = 200.0;   // SPEC §16.6: 0..200

        private readonly CheckBox _autonomous;
        private readonly TextBlock _autonomousNote;
        private readonly TextBox _daily;
        private readonly TextBox _monthly;
        private readonly TextBox _perRun;
        private readonly TextBlock _limitsLabelDaily;
        private readonly TextBlock _limitsLabelMonthly;
        private readonly TextBlock _limitsLabelRun;
        private readonly TextBlock _spent;
        private readonly TextBlock _defaults;
        private readonly TextBlock _status;
        private readonly Button _saveLimits;
        private LoopSettings? _settings;
        private bool _saving;

        public LoopSettingsDialog(Window? owner) : base(owner, "Discovery loop settings", 560, 620, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Discovery loop settings", 14, FundPalette.TextBright, bold: true));

            _autonomous = new CheckBox
            {
                Content = "Discovery loop: fully autonomous (validates its finalists G1–G8 itself; never opens the holdout)",
                Foreground = FundPalette.Text,
                FontSize = 12,
                IsEnabled = false,
                Margin = new Thickness(0, 12, 0, 4),
            };
            AutomationProperties.SetName(_autonomous, "Discovery loop fully autonomous");
            AutomationProperties.SetHelpText(_autonomous, "When on, new loops validate their finalists (gates G1–G8) themselves. Turning it on asks for confirmation. The holdout is always opened only by you.");
            _autonomous.Click += async (_, _) => await AutonomousClickedAsync();
            stack.Children.Add(_autonomous);
            _autonomousNote = FundUi.Text("Not loaded yet.", 10.5, FundPalette.Faint);
            _autonomousNote.Margin = new Thickness(22, 0, 0, 10);
            stack.Children.Add(_autonomousNote);

            stack.Children.Add(Head("Loop AI budget (0 = off)"));
            (_limitsLabelDaily, _daily) = LimitRow(stack, "Daily cap (USD)", "Loop AI daily cap in US dollars");
            (_limitsLabelMonthly, _monthly) = LimitRow(stack, "Monthly budget (USD)", "Loop AI monthly budget in US dollars");
            (_limitsLabelRun, _perRun) = LimitRow(stack, "Per loop run (USD)", "Loop AI cap per loop run in US dollars");
            _spent = FundUi.Text("", 10.5, FundPalette.Faint, mono: true);
            _spent.Margin = new Thickness(0, 2, 0, 4);
            stack.Children.Add(_spent);
            _saveLimits = FundUi.MakeButton("Save limits");
            _saveLimits.HorizontalAlignment = HorizontalAlignment.Left;
            _saveLimits.IsEnabled = false;
            _saveLimits.ToolTip = "PUT /v2/settings/loop — applies immediately (also to a running loop: limits are re-read before every AI call). Audited.";
            AutomationProperties.SetName(_saveLimits, "Save the loop AI limits");
            _saveLimits.Click += async (_, _) => await SaveLimitsAsync();
            stack.Children.Add(_saveLimits);
            var central = FundUi.Text("The same limits appear in the central AI budget window of the UFOS.ai main window (\"Discovery loop\", incl. \"per loop run\").", 10.5, FundPalette.Faint);
            central.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(central);

            stack.Children.Add(Head("Defaults for new loops"));
            _defaults = FundUi.Text("Not loaded yet.", 10.5, FundPalette.Dim);
            stack.Children.Add(_defaults);

            _status = FundUi.Text("Loading…", 11, FundPalette.Faint);
            _status.Margin = new Thickness(0, 10, 0, 10);
            AutomationProperties.SetLiveSetting(_status, AutomationLiveSetting.Polite);
            stack.Children.Add(_status);

            var close = FundUi.MakeButton("Close");
            close.IsCancel = true;
            close.HorizontalAlignment = HorizontalAlignment.Right;
            close.Click += (_, _) => { if (!_saving) Close(); };
            stack.Children.Add(close);
            SetBody(stack);

            Loaded += async (_, _) => await LoadAsync();
            Closing += (_, e) => { if (_saving) e.Cancel = true; };
        }

        private async Task LoadAsync()
        {
            var res = await PlatformClient.GetLoopSettingsAsync();
            if (!IsLoaded) return;
            if (!res.Ok || res.Value == null)
            {
                SetStatus(res.Ok ? "Unexpected response from the backend (/v2/settings/loop) — backend and app versions may not match." : res.Message, FundPalette.Kill);
                // 503 settings_invalid: saving the limits is the repair path
                if (res.Status == PlatformCallStatus.Unavailable)
                {
                    _daily.IsEnabled = _monthly.IsEnabled = _perRun.IsEnabled = _saveLimits.IsEnabled = true;
                    _autonomous.IsEnabled = true;
                }
                return;
            }
            Apply(res.Value);
            SetStatus("", FundPalette.Faint);
            _autonomous.Focus();
        }

        private void Apply(LoopSettings s)
        {
            _settings = s;
            _autonomous.IsChecked = s.Autonomous == true;
            _autonomous.IsEnabled = s.Autonomous != null;
            _autonomousNote.Text = (s.Autonomous == null ? "The backend reported no value for the switch. " : "")
                                   + (s.AutonomousNote ?? "") + (string.IsNullOrWhiteSpace(s.HoldoutNote) ? "" : " " + s.HoldoutNote)
                                   + " Off: finalists are proposed for your approval. Running loops keep the value they started with.";
            var b = s.Budget;
            foreach (var (label, box, key) in new[] { (_limitsLabelDaily, _daily, "daily_usd"), (_limitsLabelMonthly, _monthly, "monthly_usd"), (_limitsLabelRun, _perRun, "per_run_usd") })
            {
                var entry = BtJson.Array(b, "limits").FirstOrDefault(l => BtJson.Str(l, "key") == key);
                var hasEntry = entry.ValueKind == JsonValueKind.Object;
                if (hasEntry && BtJson.Str(entry, "label") is { Length: > 0 } lt) label.Text = lt;
                var value = hasEntry ? BtJson.Num(entry, "value") : null;
                if (!box.IsKeyboardFocusWithin) box.Text = value is double v ? v.ToString("0.00", Inv) : "";
                box.Tag = hasEntry ? BtJson.Num(entry, "max") : null;
                box.ToolTip = hasEntry ? "0–" + (BtJson.Num(entry, "max") ?? SpecMax).ToString("0.##", Inv) + " (0 = off) · default " + LoopText.Usd2OrUnset(BtJson.Num(entry, "default")) : null;
                box.IsEnabled = true;
            }
            _saveLimits.IsEnabled = true;
            _spent.Text = LoopText.DescriptorLine(b);
            var d = s.Defaults;
            var lim = s.Limits;
            _defaults.Text = "Defaults: " + (d?.Variants?.ToString(Inv) ?? "?") + " variants × " + (d?.Generations?.ToString(Inv) ?? "?") + " generations, "
                             + (d?.SearchPoints?.ToString(Inv) ?? "?") + " search point(s) per new variant. Ranges: variants " + Pair(lim, "variants")
                             + ", generations " + Pair(lim, "generations") + ", search points " + Pair(lim, "search_points")
                             + ", at most " + (BtJson.Num(lim, "max_backtests") is double mb ? mb.ToString("0", Inv) : "?") + " backtests per loop. "
                             + "Set by the backend; choose per loop in the start dialog.";
        }

        private static string Pair(JsonElement? limits, string key)
        {
            var arr = BtJson.Array(limits, key).Select(x => FundFormat.Plain(x)).ToList();
            return arr.Count == 2 ? arr[0] + "–" + arr[1] : "?";
        }

        private async Task AutonomousClickedAsync()
        {
            var want = _autonomous.IsChecked == true;
            var stored = _settings?.Autonomous == true;
            if (_saving)
            {
                _autonomous.IsChecked = stored;
                return;
            }
            if (want && !FundDialogs.Confirm(this, "Fully autonomous discovery loop",
                    "Turn on \"fully autonomous\"?\n\nNew loops then validate their finalists themselves: each finalist gets a validation run "
                    + "(walk-forward, gates G1–G8) on the shared job slot without asking you first. You no longer approve or discard finalists.\n\n"
                    + "Unchanged: the holdout is ALWAYS opened only by you, from the strategy's validation report — the loop never opens it and never "
                    + "changes a lifecycle state. Every backtest still counts as a trial. Loops already running keep the setting they started with.",
                    "Turn on", danger: true))
            {
                _autonomous.IsChecked = stored;
                return;
            }
            _saving = true;
            _autonomous.IsEnabled = false;
            SetStatus("Saving…", FundPalette.Faint);
            try
            {
                var res = await PlatformClient.PutLoopSettingsAsync(want, null, null, null);
                if (!res.Ok || res.Value == null)
                {
                    _autonomous.IsChecked = stored;
                    SetStatus("Not saved: " + (res.Ok ? "unexpected response from the backend (/v2/settings/loop)." : res.Message), FundPalette.Kill);
                    return;
                }
                Apply(res.Value);
                SetStatus(want ? "Fully autonomous is ON for new loops." : "Fully autonomous is OFF — new loops propose their finalists for your approval.", FundPalette.Long);
            }
            finally
            {
                _saving = false;
                _autonomous.IsEnabled = true;
            }
        }

        private async Task SaveLimitsAsync()
        {
            if (_saving) return;
            var values = new List<(string Name, TextBox Box)> { ("Daily cap", _daily), ("Monthly budget", _monthly), ("Per loop run", _perRun) };
            var parsed = new double?[3];
            for (var i = 0; i < values.Count; i++)
            {
                var (name, box) = values[i];
                var max = box.Tag is double m && m > 0 ? m : SpecMax;
                var text = (box.Text ?? "").Trim().Replace(',', '.');
                if (text.Length == 0) continue;   // not sent
                if (!double.TryParse(text, NumberStyles.Float, Inv, out var v) || double.IsNaN(v) || double.IsInfinity(v) || v < 0 || v > max)
                {
                    SetStatus(name + ": enter 0–" + max.ToString("0.##", Inv) + " (0 = off).", FundPalette.Kill);
                    box.Focus();
                    return;
                }
                parsed[i] = Math.Round(v, 2);
            }
            if (parsed.All(p => p == null))
            {
                SetStatus("Enter at least one limit.", FundPalette.Kill);
                return;
            }
            _saving = true;
            _saveLimits.IsEnabled = false;
            SetStatus("Saving…", FundPalette.Faint);
            try
            {
                var res = await PlatformClient.PutLoopSettingsAsync(null, parsed[1], parsed[0], parsed[2]);
                if (!res.Ok || res.Value == null)
                {
                    SetStatus("Not saved: " + (res.Ok ? "unexpected response from the backend (/v2/settings/loop)." : res.Message), FundPalette.Kill);
                    return;
                }
                Apply(res.Value);
                SetStatus("Limits saved — they apply immediately.", FundPalette.Long);
            }
            finally
            {
                _saving = false;
                _saveLimits.IsEnabled = true;
            }
        }

        private void SetStatus(string text, Brush brush)
        {
            _status.Text = text;
            _status.Foreground = brush;
        }

        private static (TextBlock Label, TextBox Box) LimitRow(StackPanel stack, string label, string name)
        {
            var grid = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(100) });
            var l = FundUi.Text(label, 11.5, FundPalette.Dim);
            l.VerticalAlignment = VerticalAlignment.Center;
            grid.Children.Add(l);
            var box = FundUi.Input("");
            box.IsEnabled = false;
            AutomationProperties.SetName(box, name);
            Grid.SetColumn(box, 1);
            grid.Children.Add(box);
            stack.Children.Add(grid);
            return (l, box);
        }

        private static TextBlock Head(string text)
        {
            var t = FundUi.Text(text.ToUpperInvariant(), 10.5, FundPalette.Faint, bold: true);
            t.Margin = new Thickness(0, 8, 0, 6);
            return t;
        }
    }
}

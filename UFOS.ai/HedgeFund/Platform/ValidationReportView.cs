using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Validation report (Strategy Platform stage 4, SPEC §9.3/§9.4/§9.7) — same layout and look as
    /// <see cref="BacktestReportView"/>. Left: validation and holdout runs of the strategy
    /// (GET /v2/validations?strategy_id=). Right: progress panel while the selected run is queued /
    /// running (GET /v2/validations/{id} every 1.5 s, phase/percent/message verbatim), then the
    /// report: gate table (status icon, value, threshold, the backend's plain-language explanation,
    /// "Add exception…" for a failed G1–G8 — never for H), walk-forward fold chart, parameter-grid
    /// heat table, PBO + deflated Sharpe, stress table, exceptions, holdout section.
    /// Backend texts are shown verbatim; a value the backend could not compute shows "—" with its
    /// reason. Hypothetical, simulated results.
    /// §11 (explain): section "Attribution" of the base run (<see cref="AttributionView"/>, no AI) and "Explain with AI…"
    /// (<see cref="ExplainPanel"/>: AI-generated and labelled as such, EU AI Act Art. 50).
    /// §10 (robustness): status "waiting" shows the backend's waiting message verbatim with Cancel; a failed
    /// validation run with resumable = true gets "↻ Resume" (history + report header, POST …/resume, a 409 reason
    /// verbatim). Holdout runs NEVER show Resume — a failed holdout run is final (SPEC §10.3).
    /// </summary>
    internal sealed class ValidationReportView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(1.5);
        private static readonly JsonSerializerOptions ReadOptions = new() { PropertyNameCaseInsensitive = true };

        public const int MinExceptionReasonLength = 30;
        public const string ExceptionWarning = "Exceptions are permanent and shown in every report.";

        private string? _strategyId;
        private string? _strategyName;
        private string? _selectedRunId;
        private ValidationCard? _card;
        private List<ValidationRunSummary> _runs = new();
        private int _cardSeq;
        private int _historySeq;
        private bool _polling;
        private bool _stopped;
        private bool _exceptionBusy;

        private readonly DispatcherTimer _timer;
        private readonly StackPanel _historyPanel;
        private readonly TextBlock _historyStatus;
        private readonly StackPanel _report;
        private readonly Border _progressPanel;
        private readonly TextBlock _progressPhase;
        private readonly TextBlock _progressMessage;
        private readonly Border _progressFill;
        private readonly Grid _progressTrack;
        private readonly TextBlock _progressError;
        private readonly Button _cancelButton;

        /// <summary>Raised after a run finished or an exception was added (the library reloads the strategy state).</summary>
        public event Action<string?>? StateMayHaveChanged;

        public ValidationReportView(string? strategyId, string? strategyName)
        {
            _strategyId = strategyId;
            _strategyName = strategyName;
            AutomationProperties.SetName(this, "Validation report");

            var root = new Grid { Margin = new Thickness(14, 12, 14, 14) };
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(250) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            // ----- run history -----
            var historyStack = new StackPanel();
            var hHead = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            hHead.Children.Add(FundUi.Text("Validation runs", 12, FundPalette.Text, bold: true));
            var refresh = FundUi.MakeButton("↻");
            refresh.Padding = new Thickness(8, 1, 8, 1);
            refresh.MinHeight = 20;
            refresh.FontSize = 11;
            refresh.HorizontalAlignment = HorizontalAlignment.Right;
            refresh.ToolTip = "Reload the validation runs";
            AutomationProperties.SetName(refresh, "Reload the validation runs");
            refresh.Click += async (_, _) => await LoadHistoryAsync();
            hHead.Children.Add(refresh);
            historyStack.Children.Add(hHead);
            _historyStatus = FundUi.Text("", 10.5, FundPalette.Faint);
            _historyStatus.Margin = new Thickness(0, 0, 0, 6);
            historyStack.Children.Add(_historyStatus);
            _historyPanel = new StackPanel();
            AutomationProperties.SetName(_historyPanel, "Validation and holdout runs of this strategy");
            historyStack.Children.Add(_historyPanel);
            var historyScroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = historyStack };
            root.Children.Add(new Border
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(10, 10, 6, 10),
                Child = historyScroll,
            });

            // ----- right side: progress panel (same as the backtest report) + report -----
            var right = new StackPanel();
            _progressPhase = FundUi.Text("", 12, FundPalette.TextBright, bold: true);
            _progressTrack = new Grid { Height = 6, Margin = new Thickness(0, 8, 0, 6) };
            _progressTrack.Children.Add(new Border { Background = FundPalette.Hairline, CornerRadius = new CornerRadius(3) });
            _progressFill = new Border { Background = FundPalette.Focus, CornerRadius = new CornerRadius(3), HorizontalAlignment = HorizontalAlignment.Left, Width = 0 };
            _progressTrack.Children.Add(_progressFill);
            _progressTrack.SizeChanged += (_, _) => UpdateProgressBar(_card?.Progress?.Percent);
            _progressMessage = FundUi.Text("", 11.5, FundPalette.Dim, mono: true);
            AutomationProperties.SetLiveSetting(_progressMessage, AutomationLiveSetting.Polite);
            _progressError = FundUi.Text("", 11, FundPalette.Kill);
            _progressError.Visibility = Visibility.Collapsed;
            _progressError.Margin = new Thickness(0, 6, 0, 0);
            _cancelButton = FundUi.MakeButton("Cancel run", FundPalette.KillSurface, FundPalette.Kill, FundPalette.Kill);
            _cancelButton.HorizontalAlignment = HorizontalAlignment.Left;
            _cancelButton.Margin = new Thickness(0, 10, 0, 0);
            AutomationProperties.SetName(_cancelButton, "Cancel the running validation or holdout run");
            _cancelButton.Click += async (_, _) => await CancelAsync();
            var progressStack = new StackPanel();
            progressStack.Children.Add(_progressPhase);
            progressStack.Children.Add(_progressTrack);
            progressStack.Children.Add(_progressMessage);
            progressStack.Children.Add(_progressError);
            progressStack.Children.Add(_cancelButton);
            _progressPanel = FundUi.Panel(progressStack);
            _progressPanel.Visibility = Visibility.Collapsed;
            AutomationProperties.SetName(_progressPanel, "Validation progress");
            right.Children.Add(_progressPanel);

            _report = new StackPanel();
            right.Children.Add(_report);
            var rightScroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = right, Margin = new Thickness(12, 0, 0, 0) };
            ChartTokens.AttachPageWheel(rightScroll);   // retest 2026-10-23: one notch = the normal step everywhere on the report
            Grid.SetColumn(rightScroll, 1);
            root.Children.Add(rightScroll);
            Content = root;

            _timer = new DispatcherTimer { Interval = PollInterval };
            _timer.Tick += async (_, _) => await PollAsync();
        }

        public string? StrategyId => _strategyId;

        public void Stop()
        {
            _stopped = true;
            _timer.Stop();
        }

        /// <summary>async void entry point: an exception reaches the dispatcher's handler instead of a discarded task.</summary>
        public async void Begin(string? runId) => await ShowRunAsync(runId);

        public async Task ShowRunAsync(string? runId)
        {
            if (runId != null)
            {
                await SelectRunAsync(runId);
                await LoadHistoryAsync();
            }
            else
            {
                await LoadHistoryAsync();
                var first = _runs.FirstOrDefault()?.Id;
                if (first != null) await SelectRunAsync(first);
                else ShowReportPlaceholder("No validation runs for this strategy yet — use \"Validate…\" in the library.");
            }
        }

        // =====================================================================================
        // History
        // =====================================================================================

        private async Task LoadHistoryAsync()
        {
            if (_strategyId == null)
            {
                _historyStatus.Text = "Loading…";
                return;
            }
            var seq = ++_historySeq;
            _historyStatus.Text = "Loading…";
            var list = await PlatformClient.ListValidationsAsync(_strategyId, 50);
            if (seq != _historySeq || _stopped) return;
            if (!list.Ok)
            {
                _historyStatus.Text = "";
                _historyPanel.Children.Clear();
                _historyPanel.Children.Add(ErrorBlock(list.Message));
                return;
            }
            _runs = list.Value?.Runs ?? new List<ValidationRunSummary>();
            if (string.IsNullOrWhiteSpace(_strategyName)) _strategyName = _runs.FirstOrDefault(r => !string.IsNullOrWhiteSpace(r.StrategyName))?.StrategyName;
            _historyStatus.Text = (_strategyName ?? _strategyId) + " · " + _runs.Count.ToString(Inv) + (_runs.Count == 1 ? " run" : " runs");
            RenderHistory();
        }

        private void RenderHistory()
        {
            _historyPanel.Children.Clear();
            if (_runs.Count == 0)
            {
                _historyPanel.Children.Add(FundUi.Text("No runs yet.", 11, FundPalette.Faint));
                return;
            }
            foreach (var run in _runs)
            {
                var id = run.Id ?? "";
                var stack = new StackPanel();
                var head = new StackPanel { Orientation = Orientation.Horizontal };
                head.Children.Add(StatusChip(run.Status));
                var kind = KindLabel(run.Kind);
                var when = FundUi.Text(kind + " · " + FundFormat.LocalTime(run.CreatedAt, "yyyy-MM-dd HH:mm") + " · v" + (run.Version?.ToString(Inv) ?? "?"), 10.5, FundPalette.Dim, mono: true, wrap: false);
                when.Margin = new Thickness(6, 0, 0, 0);
                when.VerticalAlignment = VerticalAlignment.Center;
                head.Children.Add(when);
                stack.Children.Add(head);
                if (FundUi.FixtureLine(run.Fixture, run.FixtureNote, 10.5, new Thickness(0, 1, 0, 1)) is TextBlock fixtureRow) stack.Children.Add(fixtureRow);
                if (run.Range is JsonElement rg)
                    stack.Children.Add(Line((BtJson.Str(rg, "start") ?? "?") + " … " + (BtJson.Str(rg, "end") ?? "?"), 10.5, FundPalette.Faint, mono: true));
                if (run.Passed is bool passed)
                    stack.Children.Add(Line((passed ? (run.Kind == "holdout" ? "holdout passed" : "all gates passed") : "not passed")
                                            + (run.Exceptions is int ex && ex > 0 ? " · " + ex.ToString(Inv) + (ex == 1 ? " exception" : " exceptions") : ""),
                                            10.5, passed ? FundPalette.Long : FundPalette.Risk));
                if (!string.IsNullOrWhiteSpace(run.Error))
                    stack.Children.Add(Line(run.Error! + RunResume.ErrorKindSuffix(run.ErrorKind), 10.5, FundPalette.Kill));
                var resumed = RunResume.ResumedText(run.ResumedFrom, run.ResumeCount);
                if (resumed != null) stack.Children.Add(Line(resumed, 10.5, FundPalette.Dim, mono: true));
                var button = RowButton(stack, id == _selectedRunId);
                AutomationProperties.SetName(button, kind + " run " + id + ", " + (run.Status ?? "unknown status") + ", started " + FundFormat.LocalTime(run.CreatedAt, "yyyy-MM-dd HH:mm")
                                                     + (run.Passed is bool p ? (p ? ", all gates passed" : ", not passed") : "")
                                                     + (resumed != null ? ", " + resumed : ""));
                button.Click += async (_, _) => await SelectRunAsync(id);
                _historyPanel.Children.Add(button);
                // never for a holdout run (CanResume checks the kind), whatever `resumable` says
                if (id.Length > 0 && RunResume.CanResume(run.Status, run.Resumable, run.Kind))
                {
                    var resume = RunResume.MakeResumeButton(id);
                    resume.HorizontalAlignment = HorizontalAlignment.Left;
                    resume.Margin = new Thickness(0, -2, 4, 8);
                    resume.Click += async (_, _) => await ResumeRunAsync(id, resume);
                    _historyPanel.Children.Add(resume);
                }
            }
        }

        private static string KindLabel(string? kind) => kind switch
        {
            "holdout" => "Holdout",
            "validation" or null or "" => "Validation",
            _ => kind!,
        };

        private static Button RowButton(UIElement content, bool selected)
        {
            var b = FundUi.MakeButton("");
            b.Content = content;
            b.HorizontalContentAlignment = HorizontalAlignment.Stretch;
            b.Padding = new Thickness(8, 6, 8, 6);
            b.Margin = new Thickness(0, 0, 4, 6);
            b.Background = selected ? FundPalette.Make(0x267C8CF8) : FundPalette.Make(0xFF1E1E1E);
            b.BorderBrush = selected ? FundPalette.Focus : FundPalette.PanelBorder;
            if (content is FrameworkElement fe) fe.HorizontalAlignment = HorizontalAlignment.Left;
            return b;
        }

        // =====================================================================================
        // Selected run: load, poll
        // =====================================================================================

        private async Task SelectRunAsync(string runId)
        {
            var seq = ++_cardSeq;
            _selectedRunId = runId;
            _timer.Stop();
            RenderHistory();
            ShowReportPlaceholder("Loading…");
            _progressPanel.Visibility = Visibility.Collapsed;
            var res = await PlatformClient.GetValidationAsync(runId);
            if (seq != _cardSeq || _stopped) return;
            if (!res.Ok || res.Value == null)
            {
                _report.Children.Clear();
                _report.Children.Add(ErrorBlock(res.Ok ? "Unexpected response from the backend (validation detail)." : res.Message));
                return;
            }
            var historyMissing = _strategyId == null;
            ApplyCard(res.Value);
            if (historyMissing && _strategyId != null) await LoadHistoryAsync();
        }

        private void ApplyCard(ValidationCard card)
        {
            _card = card;
            if (_strategyId == null && !string.IsNullOrWhiteSpace(card.StrategyId)) _strategyId = card.StrategyId;
            if (string.IsNullOrWhiteSpace(_strategyName) && !string.IsNullOrWhiteSpace(card.StrategyName)) _strategyName = card.StrategyName;
            if (ValidationStatus.IsActive(card.Status))
            {
                ShowProgress(card);
                ShowReportPlaceholder("The report appears here when the run has finished.");
                if (!_stopped) _timer.Start();
            }
            else
            {
                _timer.Stop();
                _progressPanel.Visibility = Visibility.Collapsed;
                RenderReport(card);
            }
        }

        private async Task PollAsync()
        {
            if (_polling || _stopped || _selectedRunId == null) return;
            _polling = true;
            try
            {
                var seq = _cardSeq;
                var res = await PlatformClient.GetValidationAsync(_selectedRunId);
                if (seq != _cardSeq || _stopped) return;
                if (!res.Ok || res.Value == null)
                {
                    // keep polling: the last progress stays visible, the problem is shown verbatim
                    _progressError.Text = res.Ok ? "Unexpected response from the backend (validation detail)." : res.Message;
                    _progressError.Visibility = Visibility.Visible;
                    return;
                }
                _progressError.Visibility = Visibility.Collapsed;
                ApplyCard(res.Value);
                if (!ValidationStatus.IsActive(res.Value.Status))
                {
                    await LoadHistoryAsync();
                    StateMayHaveChanged?.Invoke(_strategyId);
                }
            }
            finally
            {
                _polling = false;
            }
        }

        private void ShowProgress(ValidationCard card)
        {
            var p = card.Progress;
            _progressPanel.Visibility = Visibility.Visible;
            var phase = p?.Phase ?? card.Status ?? "running";
            // SPEC §10.3: "waiting" = connection lost, auto-resume probing; the backend's message
            // ("Connection lost — waiting for Alpaca (attempt n/5)…") is shown verbatim, Cancel stays available.
            var waiting = RunResume.IsWaiting(card.Status, p?.Phase);
            _progressPhase.Text = waiting
                ? KindLabel(card.Kind) + " waiting — connection lost, resumes automatically when it is back"
                : KindLabel(card.Kind) + " " + (card.Status ?? "running") + " — phase: " + PhaseLabel(phase)
                  + (p?.Percent is double pct ? " · " + pct.ToString("0", Inv) + " %" : "");
            _progressPhase.Foreground = waiting ? FundPalette.Risk : FundPalette.TextBright;
            _progressMessage.Text = string.IsNullOrWhiteSpace(p?.Message)
                ? (waiting ? "Waiting for the connection (the backend sent no progress message)." : "—")
                : p!.Message!;
            _progressMessage.Foreground = waiting ? FundPalette.Risk : FundPalette.Dim;
            AutomationProperties.SetName(_progressPanel, KindLabel(card.Kind) + " progress: " + _progressPhase.Text + ". " + _progressMessage.Text);
            UpdateProgressBar(p?.Percent);
        }

        private static string PhaseLabel(string phase) => phase switch
        {
            "queued" => "queued",
            "data" => "loading data",
            "simulate" => "simulating",
            "metrics" => "computing metrics",
            "folds" => "walk-forward folds",
            "grid" => "parameter grid",
            "stress" => "stress tests",
            "gates" => "evaluating the gates",
            "holdout" => "holdout test",
            "saving" => "saving the report",
            _ => phase,
        };

        private async Task CancelAsync()
        {
            var runId = _selectedRunId;
            if (runId == null) return;
            var holdout = _card?.Kind == "holdout";
            var text = holdout
                ? "Cancel the running holdout run? A run cancelled before its result exists records no holdout event."
                : "Cancel the running validation? The run card is kept with status \"cancelled\".";
            if (!FundDialogs.Confirm(Window.GetWindow(this), "Cancel run", text, "Cancel run", danger: true)) return;
            _cancelButton.IsEnabled = false;
            var res = await PlatformClient.CancelValidationAsync(runId);
            _cancelButton.IsEnabled = true;
            if (!res.Ok)
            {
                _progressError.Text = res.Message;
                _progressError.Visibility = Visibility.Visible;
                return;
            }
            _progressMessage.Text = "cancelling…";
            await PollAsync();
        }

        /// <summary>POST /v2/validations/{id}/resume → shows the NEW run (resumed_from = id). A refusal (409) is shown verbatim.</summary>
        private async Task ResumeRunAsync(string runId, Button button)
        {
            button.IsEnabled = false;
            var newId = await RunResume.ResumeAsync(Window.GetWindow(this), runId, validation: true);
            if (_stopped) return;
            button.IsEnabled = true;
            if (newId == null) return;
            await SelectRunAsync(newId);
            await LoadHistoryAsync();
            StateMayHaveChanged?.Invoke(_strategyId);
        }

        private void UpdateProgressBar(double? percent)
        {
            var w = _progressTrack.ActualWidth;
            var p = percent is double v && !double.IsNaN(v) ? Math.Max(0, Math.Min(100, v)) : 0;
            _progressFill.Width = w > 0 ? w * p / 100.0 : 0;
        }

        // =====================================================================================
        // Report
        // =====================================================================================

        private void ShowReportPlaceholder(string text)
        {
            _report.Children.Clear();
            if (_card != null && ValidationStatus.IsActive(_card.Status)) _report.Children.Add(Header(_card));
            _report.Children.Add(FundUi.Text(text, 11.5, FundPalette.Faint));
        }

        /// <summary>A block of the report (card.report — merged with the exceptions for validation runs).</summary>
        private static JsonElement? Rep(ValidationCard card, params string[] path)
            => BtJson.Get(card.Report, path);

        private static List<GateResult> GatesOf(ValidationCard card)
        {
            if (Rep(card, "gates") is JsonElement g && g.ValueKind == JsonValueKind.Array)
            {
                try { return g.Deserialize<List<GateResult>>(ReadOptions) ?? new List<GateResult>(); }
                catch (JsonException ex)
                {
                    // shown, never hidden: the report says the gate block could not be read
                    return new List<GateResult> { new() { Gate = "?", Status = "fail", Explanation = "The gate block of this report could not be read: " + ex.Message } };
                }
            }
            return new List<GateResult>();
        }

        private void RenderReport(ValidationCard card)
        {
            _report.Children.Clear();
            _report.Children.Add(Header(card));

            if (card.Status != "done")
            {
                var text = card.Status switch
                {
                    "failed" => "The run failed: " + (string.IsNullOrWhiteSpace(card.Error) ? "(no message)" : card.Error) + RunResume.ErrorKindSuffix(card.ErrorKind)
                                + (card.Kind == "holdout" ? "\nA failed holdout run is final — it cannot be resumed (the window could otherwise be re-run with knowledge of a partial result)." : ""),
                    "cancelled" => string.IsNullOrWhiteSpace(card.Error) ? "The run was cancelled." : card.Error!,
                    _ => "Status: " + (card.Status ?? "unknown") + (string.IsNullOrWhiteSpace(card.Error) ? "" : " — " + card.Error),
                };
                _report.Children.Add(ErrorBlock(text));
                return;
            }

            var gates = GatesOf(card);
            var isHoldout = card.Kind == "holdout";
            RenderVerdict(card, gates, isHoldout);
            RenderGates(card, gates);
            if (isHoldout) RenderHoldout(card, gates);
            else if (Rep(card, "holdout") is JsonElement snapshot) RenderHoldoutSnapshot(snapshot);
            if (!isHoldout)
            {
                RenderFolds(card);
                RenderGrid(card);
                RenderOverfitting(card, gates);
                RenderStress(card, gates);
            }
            RenderExceptions(card, gates);
            RenderAttribution(card, isHoldout);
            // Stage 5b (SPEC §14.3/§14.5): investment committee of this validation (advisory; "Convene committee…" only when the
            // automatic run is off; a skipped automatic run shows its note) — not for holdout runs
            if (!isHoldout && !string.IsNullOrWhiteSpace(card.StrategyId))
                _report.Children.Add(new CommitteePanel(card.StrategyId!, card.Version, card.Id));

            var note = FundUi.Text("Hypothetical, simulated results on historical data — they do not indicate future results. Static universe: survivorship bias applies.", 10.5, FundPalette.Faint);
            note.FontStyle = FontStyles.Italic;
            note.Margin = new Thickness(0, 14, 0, 0);
            _report.Children.Add(note);
            // sandbox fixture (SPEC "Sandbox profile" S4): repeated under the long report, next to the disclaimer
            if (FundUi.FixtureLine(card.Fixture, card.FixtureNote, 11.5, new Thickness(0, 6, 0, 0)) is TextBlock fixtureEnd) _report.Children.Add(fixtureEnd);
        }

        /// <summary>SPEC §11.3: the Attribution of the base (training) run — holdout runs: of the holdout window only — with
        /// "Explain with AI…" (POST /v2/validations/{id}/explanations). The report's gate_facts are not repeated here (the
        /// gate table above shows them); the AI explanation receives them via the backend's fact table.</summary>
        private void RenderAttribution(ValidationCard card, bool isHoldout)
        {
            var explain = new ExplainPanel(validation: true, card.Id, card.Status, card.Attribution != null, card.AttributionUnavailable);
            var scope = isHoldout
                ? "the locked holdout window only"
                : "the base (training) backtest of this validation — the gate values are in the gate table above";
            _report.Children.Add(new AttributionView(card.Attribution, card.AttributionUnavailable, scope, explain.TriggerButton));
            _report.Children.Add(explain);
        }

        private FrameworkElement Header(ValidationCard card)
        {
            var stack = new StackPanel { Margin = new Thickness(0, 0, 0, 6) };
            var title = new StackPanel { Orientation = Orientation.Horizontal };
            title.Children.Add(FundUi.Text(KindLabel(card.Kind) + " — " + (card.StrategyName ?? _strategyName ?? card.StrategyId ?? "Strategy") + " · v" + (card.Version?.ToString(Inv) ?? "?"),
                                           15, FundPalette.TextBright, bold: true, wrap: false));
            var chip = StatusChip(card.Status);
            chip.Margin = new Thickness(10, 2, 0, 0);
            title.Children.Add(chip);
            if (!string.IsNullOrWhiteSpace(card.Id) && RunResume.CanResume(card.Status, card.Resumable, card.Kind))
            {
                var runId = card.Id!;
                var resume = RunResume.MakeResumeButton(runId);
                resume.Margin = new Thickness(12, 0, 0, 0);
                resume.VerticalAlignment = VerticalAlignment.Center;
                resume.Click += async (_, _) => await ResumeRunAsync(runId, resume);
                title.Children.Add(resume);
            }
            stack.Children.Add(title);
            if (FundUi.FixtureLine(card.Fixture, card.FixtureNote, 12.5, new Thickness(0, 4, 0, 4)) is TextBlock fixture) stack.Children.Add(fixture);
            var req = card.Request;
            var range = card.Range;
            var parts = new List<string>();
            if (range != null) parts.Add("Simulated: " + (BtJson.Str(range, "start") ?? "?") + " … " + (BtJson.Str(range, "end") ?? "?"));
            if (BtJson.Str(req, "history_from") is string hf) parts.Add("history from " + hf);
            if (BtJson.Str(card.Report, "thresholds", "preset") is string preset) parts.Add("gate preset " + preset);
            if (BtJson.Num(card.Report, "trial_count") is double trials) parts.Add("family trials " + trials.ToString("0", Inv));
            if (card.TrialNumber is int tn) parts.Add("trial #" + tn.ToString(Inv));
            if (card.AutoResume == true) parts.Add("auto-resume on (max " + RunResume.MaxAutoResumes.ToString(Inv) + ")");
            if (parts.Count > 0) stack.Children.Add(Line(string.Join(" · ", parts), 11, FundPalette.Dim, mono: true));
            stack.Children.Add(Line("Run " + (card.Id ?? "?") + " · created " + FundFormat.LocalTime(card.CreatedAt, "yyyy-MM-dd HH:mm")
                                    + (card.FinishedAt != null ? " · finished " + FundFormat.LocalTime(card.FinishedAt, "yyyy-MM-dd HH:mm") : "")
                                    + " · content " + Short(card.ContentHash), 10.5, FundPalette.Faint, mono: true));
            if (RunResume.ResumedText(card.ResumedFrom, card.ResumeCount) is string resumed)
                stack.Children.Add(Line(char.ToUpperInvariant(resumed[0]) + resumed[1..], 10.5, FundPalette.Dim, mono: true));
            return stack;
        }

        private void RenderVerdict(ValidationCard card, List<GateResult> gates, bool isHoldout)
        {
            var passed = card.Passed ?? BtJson.Bool(card.Report, "passed");
            var failed = gates.Count(g => g.Status == "fail");
            var exceptions = gates.Count(g => g.Status == "exception");
            string text;
            Brush brush;
            if (gates.Count == 0)
            {
                text = "This report contains no gate results.";
                brush = FundPalette.Risk;
            }
            else if (passed == true || (passed == null && failed == 0))
            {
                text = isHoldout
                    ? "Holdout passed — the strategy version can move to validated."
                    : "All gates passed" + (exceptions > 0 ? " (" + exceptions.ToString(Inv) + " with a documented exception)" : "") + " — the strategy version can move to candidate.";
                brush = FundPalette.Long;
            }
            else
            {
                text = isHoldout
                    ? "Holdout failed — the family is locked until enough new unseen data exists (no exception possible)."
                    : failed.ToString(Inv) + (failed == 1 ? " gate failed" : " gates failed") + " — the strategy stays where it is. A failed G1–G8 may be overridden with a written, permanent exception.";
                brush = FundPalette.Kill;
            }
            if (BtJson.Str(card.Report, "verdict") is string verdict && verdict.Length > 0) text += "\n" + verdict;
            var t = FundUi.Text(text, 12, brush, bold: true);
            t.Margin = new Thickness(0, 6, 0, 4);
            AutomationProperties.SetName(t, "Result: " + text);
            _report.Children.Add(t);
        }

        // ----- gate table -----

        private void RenderGates(ValidationCard card, List<GateResult> gates)
        {
            Section("Gates");
            if (gates.Count == 0)
            {
                _report.Children.Add(Line("No gate results.", 11, FundPalette.Faint));
                return;
            }
            var grid = new Grid();
            var widths = new[] { new GridLength(28), new GridLength(170), new GridLength(110), new GridLength(110), new GridLength(1, GridUnitType.Star), new GridLength(120) };
            foreach (var w in widths) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = w });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var headers = new[] { "", "Gate", "Value", "Threshold", "What it means", "" };
            for (var c = 0; c < headers.Length; c++)
            {
                var h = FundUi.Text(headers[c], 10.5, FundPalette.Faint, bold: true);
                h.Margin = new Thickness(0, 0, 8, 4);
                Grid.SetColumn(h, c);
                grid.Children.Add(h);
            }
            var row = 1;
            foreach (var g in gates)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var (icon, brush, statusText) = StatusVisual(g.Status);
                var iconText = FundUi.Text(icon, 13, brush, bold: true, wrap: false);
                iconText.Margin = new Thickness(0, 3, 8, 3);
                AutomationProperties.SetName(iconText, statusText);
                iconText.ToolTip = statusText;
                Place(grid, iconText, row, 0);

                var gateName = (g.Gate ?? "?") + (string.IsNullOrWhiteSpace(g.Name) ? GateNames.TryGetValue(g.Gate ?? "", out var n) ? " " + n : "" : " " + g.Name);
                Place(grid, Cell(gateName, FundPalette.TextBright, mono: false), row, 1);
                Place(grid, Cell(ValueText(g.Value), g.Status == "fail" ? FundPalette.Kill : FundPalette.Text), row, 2);
                Place(grid, Cell(ValueText(g.Threshold), FundPalette.Dim), row, 3);
                var explanation = string.IsNullOrWhiteSpace(g.Explanation) ? "—" : g.Explanation!;
                var expStack = new StackPanel();
                expStack.Children.Add(Cell(explanation, FundPalette.Text, mono: false));
                if (g.Status == "exception" && ExceptionReason(g, card) is string why)
                {
                    var ex = FundUi.Text("Exception: " + why, 10.5, FundPalette.Risk);
                    ex.Margin = new Thickness(0, 0, 8, 2);
                    expStack.Children.Add(ex);
                }
                Place(grid, expStack, row, 4);

                if (g.Status == "fail" && IsExceptionable(g.Gate) && card.Kind != "holdout")
                {
                    var add = FundUi.MakeButton("Add exception…");
                    add.FontSize = 11;
                    add.Padding = new Thickness(8, 2, 8, 2);
                    add.VerticalAlignment = VerticalAlignment.Top;
                    add.HorizontalAlignment = HorizontalAlignment.Left;
                    add.Margin = new Thickness(0, 2, 0, 2);
                    add.ToolTip = "Override this failed gate with a written reason (at least " + MinExceptionReasonLength + " characters). " + ExceptionWarning;
                    AutomationProperties.SetName(add, "Add exception for gate " + gateName);
                    var gate = g;
                    add.Click += async (_, _) => await AddExceptionAsync(card, gate, gateName);
                    Place(grid, add, row, 5);
                }
                else if (g.Status == "fail" && string.Equals(g.Gate, "H", StringComparison.OrdinalIgnoreCase))
                {
                    var none = FundUi.Text("no exception possible", 10, FundPalette.Faint);
                    none.Margin = new Thickness(0, 3, 0, 3);
                    Place(grid, none, row, 5);
                }
                AutomationProperties.SetName(iconText, gateName + ": " + statusText + ", value " + ValueText(g.Value) + ", threshold " + ValueText(g.Threshold) + ". " + explanation);
                row++;
            }
            var panel = FundUi.Panel(grid, new Thickness(12, 8, 12, 8), new Thickness(0, 2, 0, 8));
            AutomationProperties.SetName(panel, "Gate table");
            _report.Children.Add(panel);
            _report.Children.Add(Line("✓ pass · ✗ fail · ! passed by a documented exception. Values and thresholds as computed by the backend; the threshold set is the gate preset of this run.", 10.5, FundPalette.Faint));
        }

        private static readonly Dictionary<string, string> GateNames = new(StringComparer.OrdinalIgnoreCase)
        {
            ["G1"] = "Consistency",
            ["G2"] = "Alpha",
            ["G3"] = "Signal decay",
            ["G4"] = "Deflated Sharpe",
            ["G5"] = "Overfitting (PBO)",
            ["G6"] = "Robustness",
            ["G7"] = "Sample size",
            ["G8"] = "Capacity",
            ["H"] = "Holdout",
        };

        /// <summary>G1–G8 may get an exception; the holdout gate H never (SPEC §9.4).</summary>
        public static bool IsExceptionable(string? gate)
            => gate is { Length: 2 } g && (g[0] == 'G' || g[0] == 'g') && g[1] >= '1' && g[1] <= '8';

        private static (string Icon, Brush Brush, string Text) StatusVisual(string? status) => status switch
        {
            "pass" => ("✓", FundPalette.Long, "passed"),
            "fail" => ("✗", FundPalette.Kill, "failed"),
            "exception" => ("!", FundPalette.Risk, "passed by exception"),
            _ => ("?", FundPalette.Dim, "status " + (status ?? "unknown")),
        };

        private static string? ExceptionReason(GateResult g, ValidationCard card)
        {
            if (g.Exception is JsonElement e)
            {
                if (e.ValueKind == JsonValueKind.String) return e.GetString();
                if (BtJson.Str(e, "reason") is string r) return r + ((BtJson.Str(e, "created_at") ?? BtJson.Str(e, "ts")) is string ts ? " (" + FundFormat.LocalTime(ts, "yyyy-MM-dd HH:mm") + ")" : "");
            }
            foreach (var x in BtJson.Array(card.Report, "exceptions"))
                if (string.Equals(BtJson.Str(x, "gate"), g.Gate, StringComparison.OrdinalIgnoreCase)) return BtJson.Str(x, "reason");
            return null;
        }

        private async Task AddExceptionAsync(ValidationCard card, GateResult gate, string gateName)
        {
            if (_exceptionBusy || card.Id == null || gate.Gate == null) return;
            var owner = Window.GetWindow(this);
            var dialog = new GateExceptionDialog(owner, gateName, ValueText(gate.Value), ValueText(gate.Threshold), gate.Explanation);
            if (dialog.ShowDialog() != true) return;
            _exceptionBusy = true;
            try
            {
                var res = await PlatformClient.AddGateExceptionAsync(card.Id, gate.Gate, dialog.Reason);
                if (_stopped) return;
                if (!res.Ok)
                {
                    FundDialogs.Info(owner, "Exception not added", res.Message);
                    return;
                }
                // The route answers with the updated report; reload it from GET so the view shows exactly what is stored.
                await SelectRunAsync(card.Id);
                await LoadHistoryAsync();
                StateMayHaveChanged?.Invoke(_strategyId);
            }
            finally
            {
                _exceptionBusy = false;
            }
        }

        // ----- holdout -----

        private void RenderHoldout(ValidationCard card, List<GateResult> gates)
        {
            // kind "holdout": report = {passed, mode, gates [H], window, range, validation_run_id, training_sharpe,
            // eligibility, metrics {total_return, cagr, sharpe, max_drawdown, volatility}} (validation/jobs.run_holdout).
            Section("Holdout");
            var rep = card.Kind == "holdout" ? card.Report : Rep(card, "holdout");
            var hGate = gates.FirstOrDefault(g => string.Equals(g.Gate, "H", StringComparison.OrdinalIgnoreCase));
            var det = hGate?.Details;
            var rows = new List<(string, string)>
            {
                ("Window tested", (BtJson.Str(rep, "window", "start") ?? BtJson.Str(det, "window", "start") ?? BtJson.Str(card.Range, "start") ?? "?") + " … "
                                  + (BtJson.Str(rep, "window", "end") ?? BtJson.Str(det, "window", "end") ?? BtJson.Str(card.Range, "end") ?? "?")),
            };
            if (BtJson.Str(rep, "eligibility", "mode") == "unseen") rows.Add(("Mode", "unseen months only (the family had seen part of the window)"));
            rows.Add(("Net Sharpe (holdout)", FundFormat.Num(BtJson.Num(rep, "metrics", "sharpe") ?? BtJson.Num(det, "sharpe"))));
            rows.Add(("Training Sharpe", FundFormat.Num(BtJson.Num(rep, "training_sharpe") ?? BtJson.Num(det, "training_sharpe"))));
            if (BtJson.Num(det, "required_ratio") is double rr) rows.Add(("Required share of the training Sharpe", FundFormat.Fraction(rr, 0)));
            if (BtJson.Str(det, "mode") == "cross_sectional" || BtJson.Num(det, "mean_ic") != null)
                rows.Add(("Mean IC (horizon " + (BtJson.Str(det, "horizon") ?? "?") + " d)", BtJson.Num(det, "mean_ic") is double ic ? ic.ToString("0.0000", Inv) : "— " + (BtJson.Str(det, "ic_reason") ?? "")));
            rows.Add(("Total return", FundFormat.Fraction(BtJson.Num(rep, "metrics", "total_return"), 1, signed: true)));
            rows.Add(("Max drawdown", FundFormat.Fraction(BtJson.Num(rep, "metrics", "max_drawdown"))));
            if (BtJson.Str(rep, "validation_run_id") is string vid) rows.Add(("Validation run used", vid));
            foreach (var (k, v) in rows) _report.Children.Add(FundUi.KeyValue(k, v, FundPalette.Text, 11));
            if (hGate != null && !string.IsNullOrWhiteSpace(hGate.Explanation)) _report.Children.Add(Line(hGate.Explanation!, 11, FundPalette.Text));
            _report.Children.Add(Line("The holdout is the final test on data no family member has seen: net Sharpe > 0, at least half of the training Sharpe and (cross-sectional) a positive mean IC. "
                                      + "It is opened once per strategy family and can never be overridden by an exception.", 10.5, FundPalette.Faint));
        }

        /// <summary>Validation runs store the holdout eligibility at run time (families.holdout_eligibility payload).</summary>
        private void RenderHoldoutSnapshot(JsonElement h)
        {
            Section("Holdout (status when this validation ran)");
            var eligible = BtJson.Bool(h, "eligible");
            _report.Children.Add(FundUi.KeyValue("Eligible", eligible == true ? "yes" : eligible == false ? "no" : "—",
                                                 eligible == true ? FundPalette.Long : FundPalette.Risk, 11));
            _report.Children.Add(FundUi.KeyValue("Holdout window", (BtJson.Str(h, "holdout_start") ?? BtJson.Str(h, "window", "start") ?? "?") + " … " + (BtJson.Str(h, "window", "end") ?? "?"), FundPalette.Text, 11));
            if (BtJson.Str(h, "mode") == "unseen") _report.Children.Add(FundUi.KeyValue("Tested window (unseen months only)", (BtJson.Str(h, "window", "start") ?? "?") + " … " + (BtJson.Str(h, "window", "end") ?? "?"), FundPalette.Risk, 11));
            if (BtJson.Str(h, "eligible_from") is string ef) _report.Children.Add(FundUi.KeyValue("Eligible from", ef, FundPalette.Risk, 11));
            if (BtJson.Num(h, "universe_events") is double u)
                _report.Children.Add(FundUi.KeyValue("Other families that opened this window", u.ToString("0", Inv), u > 0 ? FundPalette.Risk : FundPalette.Text, 11));
            if (BtJson.Num(h, "family_events") is double fe)
                _report.Children.Add(FundUi.KeyValue("Holdout events of this family", fe.ToString("0", Inv), FundPalette.Text, 11));
            if (BtJson.Str(h, "reason") is string why) _report.Children.Add(Line(why, 11, FundPalette.Risk));
            _report.Children.Add(Line("Use \"Open holdout (once)…\" in the library after a passing validation. The current status is shown there.", 10.5, FundPalette.Faint));
        }

        // ----- folds -----

        private void RenderFolds(ValidationCard card)
        {
            Section("Walk-forward folds");
            var folds = BtJson.Array(card.Report, "folds").ToList();
            if (folds.Count == 0)
            {
                _report.Children.Add(Line(BtJson.Str(card.Report, "folds_reason") ?? "No fold results in this report.", 11, FundPalette.Faint));
                return;
            }
            var labels = folds.Select((f, i) => "Fold " + (BtJson.Str(f, "fold") ?? (i + 1).ToString(Inv))).ToList();
            var sharpe = folds.Select(f => BtJson.Num(f, "sharpe")).ToList();
            var ic = folds.Select(f => BtJson.Num(f, "mean_ic")).ToList();

            var chart = new IcDecayChart { Height = 140, ValueFormat = "0.00" };
            chart.SetData(labels.Zip(sharpe, (l, v) => (l, v)));
            AutomationProperties.SetName(chart, "Fold chart: net Sharpe per walk-forward fold — "
                                                + string.Join(", ", labels.Zip(sharpe, (l, v) => l + " = " + (v is double x ? x.ToString("0.00", Inv) : "not available"))));
            _report.Children.Add(Line("Net Sharpe per fold", 11, FundPalette.Dim));
            _report.Children.Add(FundUi.Panel(chart, new Thickness(8, 8, 8, 6), new Thickness(0, 0, 0, 4)));
            if (ic.Any(v => v.HasValue))
            {
                var icChart = new IcDecayChart { Height = 120 };
                icChart.SetData(labels.Zip(ic, (l, v) => (l, v)));
                AutomationProperties.SetName(icChart, "Fold chart: mean rank IC per walk-forward fold — "
                                                      + string.Join(", ", labels.Zip(ic, (l, v) => l + " = " + (v is double x ? x.ToString("0.000", Inv) : "not available"))));
                _report.Children.Add(Line("Mean rank IC per fold", 11, FundPalette.Dim));
                _report.Children.Add(FundUi.Panel(icChart, new Thickness(8, 8, 8, 6), new Thickness(0, 0, 0, 4)));
            }
            var rows = folds.Select((f, i) => new[]
            {
                labels[i],
                (BtJson.Str(f, "start") ?? "?") + " … " + (BtJson.Str(f, "end") ?? "?"),
                FundFormat.Num(BtJson.Num(f, "sharpe")),
                BtJson.Num(f, "mean_ic") is double x ? x.ToString("0.0000", Inv) : "—",
                FundFormat.Fraction(BtJson.Num(f, "total_return"), 1, signed: true),
                FundFormat.Fraction(BtJson.Num(f, "max_drawdown")),
                FundFormat.Num(BtJson.Num(f, "days"), 0),
                BtJson.Str(f, "reason") ?? "",
            }).ToList();
            var horizon = folds.Select(f => BtJson.Str(f, "horizon")).FirstOrDefault(h => !string.IsNullOrEmpty(h));
            _report.Children.Add(Table(new[] { "Fold", "Out-of-sample segment", "Sharpe", "Mean IC" + (horizon != null ? " (" + horizon + " d)" : ""), "Return", "Max DD", "Days", "Note" },
                                       new[] { 60.0, 190, 64, 96, 74, 66, 50, -1 }, rows, "Walk-forward fold table"));
            _report.Children.Add(Line("The training range is split into consecutive, equal-length segments; the strategy is not refitted per fold. Consistent results across folds speak against luck in one period.", 10.5, FundPalette.Faint));
        }

        // ----- parameter grid heat table -----

        private void RenderGrid(ValidationCard card)
        {
            Section("Parameter grid");
            var grid = Rep(card, "grid");
            var configs = BtJson.Array(grid, "configs").ToList();
            if (configs.Count == 0)
            {
                _report.Children.Add(Line(BtJson.Str(grid, "reason") ?? BtJson.Str(card.Report, "grid_reason") ?? "No parameter grid in this report.", 11, FundPalette.Faint));
                return;
            }
            // grid = {axes {name: [values]}, fixed {name: {value, reason}}, configs [{index, params, sharpe, reason, base}],
            //         base_index, total, subsampled, neighbours, base_sharpe} (validation/jobs.run_validation)
            var names = BtJson.Props(grid, "axes").Select(p => p.Name).ToList();
            if (names.Count == 0)
                names = configs.SelectMany(c => BtJson.Props(c, "params").Select(p => p.Name)).Distinct().ToList();
            var values = configs.Select(c => BtJson.Num(c, "sharpe")).ToList();
            var finite = values.Where(v => v.HasValue).Select(v => v!.Value).ToList();
            var maxAbs = finite.Count == 0 ? 1 : Math.Max(0.01, finite.Max(x => Math.Abs(x)));
            var baseIndex = BtJson.Num(grid, "base_index") is double bi ? (int)bi : -1;

            FrameworkElement table;
            if (names.Count == 2)
            {
                // pivot: first parameter = rows, second = columns
                var rowVals = configs.Select(c => BtJson.Str(c, "params", names[0]) ?? "?").Distinct().ToList();
                var colVals = configs.Select(c => BtJson.Str(c, "params", names[1]) ?? "?").Distinct().ToList();
                var g = new Grid();
                g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                foreach (var _ in colVals) g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(62) });
                g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var corner = FundUi.Text(names[0] + " \\ " + names[1], 10.5, FundPalette.Faint, bold: true, wrap: false);
                corner.Margin = new Thickness(0, 0, 10, 4);
                g.Children.Add(corner);
                for (var c = 0; c < colVals.Count; c++)
                {
                    var h = FundUi.Text(colVals[c], 10.5, FundPalette.Faint, bold: true, mono: true, wrap: false);
                    h.TextAlignment = TextAlignment.Center;
                    h.Margin = new Thickness(0, 0, 0, 4);
                    Place(g, h, 0, c + 1);
                }
                for (var r = 0; r < rowVals.Count; r++)
                {
                    g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                    var rh = FundUi.Text(rowVals[r], 10.5, FundPalette.Faint, bold: true, mono: true, wrap: false);
                    rh.Margin = new Thickness(0, 0, 10, 0);
                    rh.VerticalAlignment = VerticalAlignment.Center;
                    Place(g, rh, r + 1, 0);
                    for (var c = 0; c < colVals.Count; c++)
                    {
                        var idx = configs.FindIndex(x => (BtJson.Str(x, "params", names[0]) ?? "?") == rowVals[r] && (BtJson.Str(x, "params", names[1]) ?? "?") == colVals[c]);
                        var cell = HeatCell(idx >= 0 ? values[idx] : null, maxAbs, idx >= 0 && (idx == baseIndex || BtJson.Bool(configs[idx], "base") == true),
                                            names[0] + " " + rowVals[r] + ", " + names[1] + " " + colVals[c], idx < 0);
                        Place(g, cell, r + 1, c + 1);
                    }
                }
                table = g;
            }
            else
            {
                var g = new Grid();
                foreach (var _ in names) g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(90) });
                g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(70) });
                g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                for (var c = 0; c < names.Count; c++)
                {
                    var h = FundUi.Text(names[c], 10.5, FundPalette.Faint, bold: true, wrap: false);
                    h.Margin = new Thickness(0, 0, 8, 4);
                    Place(g, h, 0, c);
                }
                var sh = FundUi.Text("Sharpe", 10.5, FundPalette.Faint, bold: true, wrap: false);
                sh.Margin = new Thickness(0, 0, 0, 4);
                Place(g, sh, 0, names.Count);
                for (var i = 0; i < configs.Count; i++)
                {
                    g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                    var label = new List<string>();
                    for (var c = 0; c < names.Count; c++)
                    {
                        var v = BtJson.Str(configs[i], "params", names[c]) ?? "?";
                        label.Add(names[c] + " " + v);
                        var t = FundUi.Text(v, 11, FundPalette.Text, mono: true, wrap: false);
                        t.Margin = new Thickness(0, 1, 8, 1);
                        t.VerticalAlignment = VerticalAlignment.Center;
                        Place(g, t, i + 1, c);
                    }
                    Place(g, HeatCell(values[i], maxAbs, i == baseIndex || BtJson.Bool(configs[i], "base") == true, string.Join(", ", label), false), i + 1, names.Count);
                }
                table = g;
            }
            // live click-test 2026-10-22: this horizontal scroller swallowed the mouse wheel (a ScrollViewer marks every wheel event
            // handled) — the wheel now scrolls the report page
            var heatScroll = new ScrollViewer { Content = table, HorizontalScrollBarVisibility = ScrollBarVisibility.Auto, VerticalScrollBarVisibility = ScrollBarVisibility.Disabled, Focusable = false };
            heatScroll.PreviewMouseWheel += (sender, e) => { if (sender is UIElement hs) ChartTokens.ForwardWheel(hs, e); };
            var panel = FundUi.Panel(heatScroll, new Thickness(12, 8, 12, 8), new Thickness(0, 2, 0, 4));
            AutomationProperties.SetName(panel, "Parameter grid heat table: net Sharpe per parameter configuration, " + configs.Count.ToString(Inv) + " configurations");
            _report.Children.Add(panel);
            var sub = BtJson.Bool(grid, "subsampled") == true
                ? " Evenly subsampled: " + configs.Count.ToString(Inv) + " of " + (BtJson.Str(grid, "total") ?? "?") + " configurations."
                : "";
            var fixedParams = BtJson.Props(grid, "fixed").Select(p => p.Name + " = " + (BtJson.Str(p.Value, "value") ?? "?") + " (" + (BtJson.Str(p.Value, "reason") ?? "fixed") + ")").ToList();
            if (fixedParams.Count > 0) _report.Children.Add(Line("Not varied: " + string.Join("; ", fixedParams), 10.5, FundPalette.Dim));
            var failedCfg = configs.Where(c => BtJson.Str(c, "reason") is { Length: > 0 }).Select(c => "#" + (BtJson.Str(c, "index") ?? "?") + ": " + BtJson.Str(c, "reason")).ToList();
            if (failedCfg.Count > 0) _report.Children.Add(Line("Configurations without a Sharpe — " + string.Join("; ", failedCfg.Take(6)) + (failedCfg.Count > 6 ? " …" : ""), 10.5, FundPalette.Risk));
            _report.Children.Add(Line("Net Sharpe of each parameter configuration (green positive, red negative, stronger colour = larger value; framed = the strategy's own parameters)." + sub
                                      + " A good strategy keeps working when its parameters move a little.", 10.5, FundPalette.Faint));
        }

        private static Border HeatCell(double? value, double maxAbs, bool isBase, string name, bool missing)
        {
            var text = missing ? "" : value is double v ? v.ToString("0.00", Inv) : "n/a";
            Brush bg = Brushes.Transparent;
            if (value is double x)
            {
                var a = (byte)Math.Round(40 + 150 * Math.Min(1, Math.Abs(x) / maxAbs));
                bg = x >= 0 ? FundPalette.Make(((uint)a << 24) | 0x6FA98Au) : FundPalette.Make(((uint)a << 24) | 0xC1604Bu);
            }
            var t = FundUi.Text(text, 10.5, FundPalette.TextBright, mono: true, wrap: false);
            t.TextAlignment = TextAlignment.Center;
            var cell = new Border
            {
                Background = bg,
                BorderBrush = isBase ? FundPalette.Focus : FundPalette.Hairline,
                BorderThickness = new Thickness(isBase ? 2 : 1),
                Margin = new Thickness(1),
                Padding = new Thickness(4, 3, 4, 3),
                MinWidth = 56,
                Child = t,
                ToolTip = missing ? name + ": not in the grid" : name + ": Sharpe " + text + (isBase ? " (strategy's own parameters)" : ""),
            };
            AutomationProperties.SetName(cell, missing ? name + ": not in the grid" : name + ": Sharpe " + text + (isBase ? ", strategy's own parameters" : ""));
            return cell;
        }

        // ----- PBO + deflated Sharpe -----

        private void RenderOverfitting(ValidationCard card, List<GateResult> gates)
        {
            Section("Overfitting checks");
            var pbo = Rep(card, "pbo");
            var dsr = Rep(card, "dsr");
            var panel = new WrapPanel();
            panel.Children.Add(InfoBox("Probability of backtest overfitting (PBO)",
                BtJson.Num(pbo, "value"),
                v => v.ToString("0.00", Inv),
                "How often the parameter set that looked best in one half of the history ranked below the median in the other half — lower is better.",
                pbo, gates.FirstOrDefault(g => g.Gate == "G5")));
            panel.Children.Add(InfoBox("Deflated Sharpe ratio",
                BtJson.Num(dsr, "value"),
                v => v.ToString("0.000", Inv),
                "The probability that the true Sharpe ratio is above zero after accounting for how many backtests this strategy family has tried — higher is better.",
                dsr, gates.FirstOrDefault(g => g.Gate == "G4")));
            _report.Children.Add(panel);
        }

        private static Border InfoBox(string title, double? value, Func<double, string> fmt, string explanation, JsonElement? block, GateResult? gate)
        {
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text(title, 11, FundPalette.Text, bold: true));
            var reason = BtJson.Str(block, "reason");
            var v = FundUi.Text(value is double d ? fmt(d) : "— " + (reason ?? ""), 18, value is null ? FundPalette.Faint : FundPalette.TextBright, bold: true, mono: true);
            v.Margin = new Thickness(0, 4, 0, 4);
            stack.Children.Add(v);
            var exp = FundUi.Text(explanation, 10.5, FundPalette.Dim);
            exp.LineHeight = 15;
            stack.Children.Add(exp);
            foreach (var p in BtJson.Props(block))
            {
                if (p.Name is "value" or "reason" || p.Value.ValueKind is JsonValueKind.Object or JsonValueKind.Array) continue;
                var text = p.Value.ValueKind == JsonValueKind.Number ? ValueText(p.Value) : FundFormat.Plain(p.Value);
                stack.Children.Add(FundUi.KeyValue(FundFormat.Label(p.Name), text, FundPalette.Text, 10.5));
            }
            if (gate != null)
            {
                var (icon, brush, text) = StatusVisual(gate.Status);
                var g = FundUi.Text(icon + " " + (gate.Gate ?? "") + " " + text + " · threshold " + ValueText(gate.Threshold), 10.5, brush);
                g.Margin = new Thickness(0, 4, 0, 0);
                stack.Children.Add(g);
            }
            var panel = FundUi.Panel(stack, new Thickness(12, 10, 12, 10), new Thickness(0, 0, 10, 10));
            panel.Width = 330;
            panel.VerticalAlignment = VerticalAlignment.Top;
            AutomationProperties.SetName(panel, title + ": " + (value is double dd ? fmt(dd) : "not available") + ". " + explanation);
            return panel;
        }

        // ----- stress -----

        private void RenderStress(ValidationCard card, List<GateResult> gates)
        {
            // report.stress = the stress block {scenarios [{kind, key, label, status, reason, window, metrics {strategy, benchmark}}]};
            // the episode check (drawdown ≤ ratio × benchmark's) and the costs check come from G6's details.
            Section("Stress tests");
            var stress = Rep(card, "stress");
            var items = BtJson.Array(stress, "scenarios").ToList();
            if (items.Count == 0)
            {
                _report.Children.Add(Line(BtJson.Str(stress, "reason") ?? "No stress results in this report.", 11, FundPalette.Faint));
                return;
            }
            var g6 = gates.FirstOrDefault(g => g.Gate == "G6")?.Details;
            var checks = new Dictionary<string, JsonElement>();
            foreach (var e in BtJson.Array(g6, "episodes"))
                if (BtJson.Str(e, "key") is string k) checks[k] = e;
            var costsKey = BtJson.Str(g6, "costs", "key");
            var rows = new List<string[]>();
            var brushes = new List<Brush>();
            foreach (var sc in items)
            {
                var key = BtJson.Str(sc, "key") ?? "";
                var kind = BtJson.Str(sc, "kind");
                if (kind == "monte_carlo" || key == "monte_carlo") continue;
                var status = BtJson.Str(sc, "status") ?? "—";
                var win = BtJson.Str(sc, "window", "start") is string ws ? ws + " … " + (BtJson.Str(sc, "window", "end") ?? "?") : "—";
                var m = BtJson.Get(sc, "metrics");
                string result;
                Brush brush;
                if (status != "done")
                {
                    result = BtJson.Str(sc, "reason") ?? status;
                    brush = status == "failed" ? FundPalette.Kill : FundPalette.Faint;
                }
                else if (checks.TryGetValue(key, out var chk))
                {
                    var c = BtJson.Str(chk, "check");
                    result = c == "pass" ? "✓ within limit" : c == "fail" ? "✗ over limit" : c ?? "done";
                    if (BtJson.Num(chk, "limit") is double lim) result += " (limit " + FundFormat.Fraction(lim, 1) + ")";
                    brush = c == "fail" ? FundPalette.Kill : FundPalette.Text;
                }
                else if (key == costsKey)
                {
                    var sh = BtJson.Num(g6, "costs", "sharpe");
                    result = sh is double x ? (x > 0 ? "✓ net Sharpe > 0" : "✗ net Sharpe ≤ 0") : "✗ " + (BtJson.Str(g6, "costs", "reason") ?? "not computed");
                    brush = sh is double y && y > 0 ? FundPalette.Text : FundPalette.Kill;
                }
                else
                {
                    result = "done";
                    brush = FundPalette.Text;
                }
                rows.Add(new[]
                {
                    BtJson.Str(sc, "label") ?? key,
                    win,
                    result,
                    FundFormat.Num(BtJson.Num(m, "strategy", "sharpe")),
                    FundFormat.Fraction(BtJson.Num(m, "strategy", "max_drawdown")),
                    FundFormat.Fraction(BtJson.Num(m, "benchmark", "max_drawdown")),
                });
                brushes.Add(brush);
            }
            _report.Children.Add(Table(new[] { "Scenario", "Window", "G6 check", "Sharpe", "Max DD", "Bench. max DD" },
                                       new[] { 160.0, 180, -1, 64, 74, 96 }, rows, "Stress table", brushes));
            foreach (var n in BtJson.Array(g6, "notes")) _report.Children.Add(Line(StrategyJson.Scalar(n), 10.5, FundPalette.Dim));
            var ratio = BtJson.Str(card.Report, "thresholds", "effective", "g6_episode_dd_ratio") ?? "1.5";
            _report.Children.Add(Line("Higher costs test how sensitive the result is to trading costs; in each historical crisis episode the drawdown must stay within "
                                      + ratio + "× the benchmark's. Episodes inside the locked holdout window are skipped.", 10.5, FundPalette.Faint));
        }

        // ----- exceptions -----

        private void RenderExceptions(ValidationCard card, List<GateResult> gates)
        {
            Section("Exceptions");
            var list = BtJson.Array(card.Report, "exceptions").ToList();
            if (list.Count == 0)
            {
                var failed = gates.Where(g => g.Status == "fail" && IsExceptionable(g.Gate)).Select(g => g.Gate).ToList();
                _report.Children.Add(Line(failed.Count > 0
                    ? "None. Use \"Add exception…\" in the gate table to override a failed gate (" + string.Join(", ", failed) + ") with a written reason. " + ExceptionWarning
                    : "None.", 11, FundPalette.Faint));
                return;
            }
            foreach (var x in list)
            {
                var head = (BtJson.Str(x, "gate") ?? "?") + " · " + FundFormat.LocalTime(BtJson.Str(x, "created_at") ?? BtJson.Str(x, "ts"), "yyyy-MM-dd HH:mm")
                           + " · " + (BtJson.Str(x, "actor") ?? "user");
                _report.Children.Add(Line(head, 11, FundPalette.Risk, mono: true));
                var reason = Line(BtJson.Str(x, "reason") ?? "—", 11.5, FundPalette.Text);
                reason.Margin = new Thickness(12, 0, 0, 6);
                _report.Children.Add(reason);
            }
            _report.Children.Add(Line(ExceptionWarning + " They belong to this validation run — a new run needs new exceptions.", 10.5, FundPalette.Faint));
        }

        // =====================================================================================
        // Small builders
        // =====================================================================================

        /// <summary>A gate value / threshold as text: numbers compact, strings verbatim, objects as key=value pairs.</summary>
        public static string ValueText(JsonElement? e)
        {
            if (e is not JsonElement v) return "—";
            switch (v.ValueKind)
            {
                case JsonValueKind.Number:
                    var d = v.GetDouble();
                    return Math.Abs(d) >= 1000 ? d.ToString("N0", Inv) : d.ToString("0.###", Inv);
                case JsonValueKind.String:
                    return string.IsNullOrWhiteSpace(v.GetString()) ? "—" : v.GetString()!;
                case JsonValueKind.Null:
                case JsonValueKind.Undefined:
                    return "—";
                case JsonValueKind.Object:
                    return string.Join(" · ", v.EnumerateObject().Select(p => p.Name + " " + ValueText(p.Value)));
                case JsonValueKind.Array:
                    return string.Join(", ", v.EnumerateArray().Select(x => ValueText(x)));
                default:
                    return FundFormat.Plain(v);
            }
        }

        private static void Place(Grid grid, UIElement element, int row, int column)
        {
            Grid.SetRow(element, row);
            Grid.SetColumn(element, column);
            grid.Children.Add(element);
        }

        private static TextBlock Cell(string text, Brush brush, bool mono = true)
        {
            var t = FundUi.Text(text, 11, brush, mono: mono);
            t.Margin = new Thickness(0, 3, 8, 3);
            return t;
        }

        private static Border Table(string[] headers, double[] widths, List<string[]> rows, string name, List<Brush>? rowBrushes = null)
        {
            var grid = new Grid();
            foreach (var w in widths)
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = w < 0 ? new GridLength(1, GridUnitType.Star) : new GridLength(w) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var c = 0; c < headers.Length && c < widths.Length; c++)
            {
                var h = FundUi.Text(headers[c], 10.5, FundPalette.Faint, bold: true);
                h.Margin = new Thickness(0, 0, 8, 4);
                Place(grid, h, 0, c);
            }
            for (var r = 0; r < rows.Count; r++)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var brush = rowBrushes != null && r < rowBrushes.Count ? rowBrushes[r] : FundPalette.Text;
                for (var c = 0; c < rows[r].Length && c < widths.Length; c++)
                {
                    var cell = FundUi.Text(rows[r][c], 11, c == 0 ? FundPalette.TextBright : brush, mono: c > 0);
                    cell.Margin = new Thickness(0, 2, 8, 2);
                    Place(grid, cell, r + 1, c);
                }
            }
            var panel = FundUi.Panel(grid, new Thickness(12, 8, 12, 8), new Thickness(0, 2, 0, 8));
            AutomationProperties.SetName(panel, name);
            return panel;
        }

        private void Section(string title)
        {
            var t = FundUi.Text(title, 12, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 14, 0, 6);
            _report.Children.Add(t);
        }

        private static TextBlock Line(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
            return t;
        }

        private static Border StatusChip(string? status)
        {
            var brush = status switch
            {
                "done" => FundPalette.Long,
                "failed" => FundPalette.Kill,
                "cancelled" => FundPalette.Neutral,
                "running" or "queued" => FundPalette.Focus,
                RunResume.WaitingStatus => FundPalette.Risk,
                _ => FundPalette.Dim,
            };
            return new Border
            {
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(999),
                Padding = new Thickness(6, 0, 6, 1),
                VerticalAlignment = VerticalAlignment.Center,
                Child = FundUi.Text(status ?? "unknown", 9.5, brush, bold: true, wrap: false),
            };
        }

        private static Border ErrorBlock(string message) => new()
        {
            Background = FundPalette.KillSurface,
            BorderBrush = FundPalette.Kill,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(10, 6, 10, 6),
            Margin = new Thickness(0, 4, 0, 8),
            Child = FundUi.Text(message, 11.5, FundPalette.Make(0xFFF4A197)),
        };

        private static string Short(string? hash) => string.IsNullOrEmpty(hash) ? "—" : hash!.Length > 12 ? hash[..12] : hash;
    }

    /// <summary>
    /// Window hosting <see cref="ValidationReportView"/> (FundPopupWindow chrome: borderless, resizable,
    /// drag-to-restore, square corners when maximised). One window per strategy.
    /// </summary>
    internal sealed class ValidationReportWindow : FundPopupWindow
    {
        private static readonly List<ValidationReportWindow> OpenWindows = new();
        private readonly ValidationReportView _view;

        /// <summary>Raised when a run of any open report finished or got an exception (argument: strategy id).</summary>
        public static event Action<string?>? StrategyStateMayHaveChanged;

        private ValidationReportWindow(Window? owner, string? strategyId, string? strategyName)
            : base(owner, "Validation report" + (string.IsNullOrWhiteSpace(strategyName) ? "" : " — " + strategyName), 1180, 820, resizable: true)
        {
            _view = new ValidationReportView(strategyId, strategyName);
            _view.StateMayHaveChanged += id => StrategyStateMayHaveChanged?.Invoke(id);
            SetBody(_view);
            StartMaximized();   // user request 2026-10-23: report windows open maximized
            Closed += (_, _) =>
            {
                _view.Stop();
                OpenWindows.Remove(this);
            };
        }

        public static void Open(Window? owner, string? strategyId, string? strategyName, string? runId)
        {
            var existing = strategyId == null ? null : OpenWindows.FirstOrDefault(w => w._view.StrategyId == strategyId);
            if (existing != null)
            {
                if (existing.WindowState == WindowState.Minimized) existing.WindowState = WindowState.Normal;
                existing.Activate();
                existing._view.Begin(runId);
                return;
            }
            var window = new ValidationReportWindow(owner, strategyId, strategyName);
            OpenWindows.Add(window);
            window.Show();
            window._view.Begin(runId);
        }
    }
}

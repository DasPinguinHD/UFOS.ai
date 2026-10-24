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
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Investment committee (Strategy Platform stage 5b, SPEC §14.3/§14.5; routes/JSON §14.7) — used on the validation
    /// report (with the validation run id) and in the Library's strategy detail. "Convene committee…" is shown ONLY when
    /// the setting "run automatically after each validation" is off (GET /v2/settings/committee); with the backend's
    /// pre-call estimate per model. Progress per role (Researcher → Quant → Risk Manager → Reviewer → Chair) with Cancel
    /// (stops before the next role, charged so far), polled every 2 s while a run is running. Memo view: the pinned
    /// AI-interaction notice, every role output in its own violet AI container with "✦ AI-generated · model · time ·
    /// role" and a cost line, numbers the §11.2 check could not find red with the tooltip "not in the computed data",
    /// plain text only; the memo is advisory and never changes the strategy's state. History = the committee runs of
    /// this strategy (or validation run), newest first; a skipped auto run shows its note ("committee skipped: …")
    /// verbatim. Backend errors (busy, no_validation, budget …) are shown verbatim.
    /// </summary>
    internal sealed class CommitteePanel : StackPanel
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(2);
        private static readonly string[] DefaultRoleOrder = { "researcher", "quant", "risk_manager", "reviewer", "chair" };

        private readonly string _strategyId;
        private readonly int? _version;
        private readonly string? _validationRunId;
        private readonly string _groupId = Guid.NewGuid().ToString("N");
        private readonly Button _convene;
        private readonly TextBlock _modeText;
        private readonly StackPanel _skippedHost;
        private readonly Border _options;
        private readonly RadioButton _standard;
        private readonly RadioButton _premium;
        private readonly Button _start;
        private readonly StackPanel _errorHost;
        private readonly StackPanel _progressHost;
        private readonly StackPanel _memoHost;
        private readonly StackPanel _historyHost;
        private readonly DispatcherTimer _poll;
        private List<CommitteeRun> _runs = new();
        private string? _shownRunId;
        private CommitteeDetail? _shown;
        private bool _loaded;
        private bool _busy;
        private bool _polling;

        public CommitteePanel(string strategyId, int? version, string? validationRunId)
        {
            _strategyId = strategyId;
            _version = version;
            _validationRunId = string.IsNullOrWhiteSpace(validationRunId) ? null : validationRunId;
            AutomationProperties.SetName(this, "Investment committee");
            Margin = new Thickness(0, 6, 0, 6);

            var head = new DockPanel { Margin = new Thickness(0, 8, 0, 2) };
            _convene = FundUi.MakeButton(AiContent.Glyph + " Convene committee…", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _convene.Padding = new Thickness(10, 2, 10, 2);
            _convene.Visibility = Visibility.Collapsed;
            _convene.ToolTip = "Five AI roles review the strategy and the validation report and write an advisory memo — costs money (ideation budget).";
            AutomationProperties.SetName(_convene, "Convene the investment committee (AI)");
            _convene.Click += async (_, _) => await ToggleOptionsAsync();
            DockPanel.SetDock(_convene, Dock.Right);
            head.Children.Add(_convene);
            head.Children.Add(FundUi.Text("Investment committee", 12, FundPalette.Text, bold: true));
            Children.Add(head);
            _modeText = Line("Loading the committee settings…", 10.5, FundPalette.Faint);
            Children.Add(_modeText);
            _skippedHost = new StackPanel();
            Children.Add(_skippedHost);

            var opt = new StackPanel();
            opt.Children.Add(Line("Researcher → Quant → Risk Manager → Reviewer / Devil's Advocate → Chair (memo). They see the strategy logic, the gate facts of "
                                  + "the latest validation, the attribution and memory facts — never holdout results unless the holdout of this version was opened.", 10.5, FundPalette.Faint));
            var row = new WrapPanel { Margin = new Thickness(0, 6, 0, 0) };
            _standard = Radio("committee" + _groupId, "Standard", "Committee with the standard model", true);
            _premium = Radio("committee" + _groupId, "Premium", "Committee with the premium model", false);
            row.Children.Add(_standard);
            row.Children.Add(_premium);
            _start = FundUi.MakeButton("Convene (5 AI calls)", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _start.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetName(_start, "Start the committee run");
            _start.Click += async (_, _) => await StartAsync();
            row.Children.Add(_start);
            var close = FundUi.MakeButton("Close");
            close.Margin = new Thickness(8, 0, 0, 0);
            close.Click += (_, _) => _options!.Visibility = Visibility.Collapsed;
            row.Children.Add(close);
            opt.Children.Add(row);
            _options = new Border
            {
                Background = FundPalette.AiSurface,
                BorderBrush = FundPalette.Ai,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(12, 8, 12, 10),
                Margin = new Thickness(0, 4, 0, 6),
                Child = opt,
                Visibility = Visibility.Collapsed,
            };
            AutomationProperties.SetName(_options, "Convene committee: options");
            Children.Add(_options);
            _errorHost = new StackPanel();
            Children.Add(_errorHost);
            _progressHost = new StackPanel();
            AutomationProperties.SetLiveSetting(_progressHost, AutomationLiveSetting.Polite);
            Children.Add(_progressHost);
            _memoHost = new StackPanel();
            Children.Add(_memoHost);
            _historyHost = new StackPanel();
            Children.Add(_historyHost);

            _poll = new DispatcherTimer { Interval = PollInterval };
            _poll.Tick += async (_, _) => await PollAsync();
            Loaded += async (_, _) =>
            {
                if (!_loaded)
                {
                    _loaded = true;
                    await LoadAsync();
                }
                else UpdatePolling();
            };
            Unloaded += (_, _) => _poll.Stop();
        }

        // =====================================================================================
        // Loading
        // =====================================================================================

        private async Task LoadAsync()
        {
            var settings = await PlatformClient.GetCommitteeSettingsAsync();
            if (!settings.Ok || settings.Value == null)
            {
                _modeText.Text = "Committee settings not loaded: " + (settings.Ok ? "unexpected response from the backend (/v2/settings/committee)." : settings.Message)
                                 + " — \"Convene committee…\" is only offered when the automatic run is known to be off.";
                _modeText.Foreground = FundPalette.Risk;
            }
            else if (settings.Value.AutoAfterValidation == true)
            {
                _modeText.Text = "The committee runs automatically after each validation (" + TierLabel(settings.Value.Model) + " model) — change it in the Hedge Fund settings.";
                _modeText.Foreground = FundPalette.Faint;
            }
            else if (settings.Value.AutoAfterValidation == false)
            {
                _modeText.Text = "Advisory only — the memo never changes the strategy's state. The automatic run is off (Hedge Fund settings).";
                _modeText.Foreground = FundPalette.Faint;
                _convene.Visibility = Visibility.Visible;
            }
            else
            {
                _modeText.Text = "The backend reported no value for the automatic committee setting — \"Convene committee…\" is not offered.";
                _modeText.Foreground = FundPalette.Risk;
            }
            await LoadRunsAsync(selectNewest: true);
            await StudioView.ApplyBackendNoticeAsync(_noticeForMemo);
        }

        private readonly Border _noticeForMemo = StudioView.NoticeBlock();

        private async Task LoadRunsAsync(bool selectNewest)
        {
            var res = await PlatformClient.ListCommitteeRunsAsync(_strategyId, _validationRunId, 20);
            if (!res.Ok || res.Value == null)
            {
                ShowError(res.Ok ? "Unexpected response from the backend (/v2/committee)." : res.Message);
                return;
            }
            _runs = res.Value.Runs ?? new List<CommitteeRun>();
            _skippedHost.Children.Clear();
            foreach (var r in _runs.Where(r => r.Status == "skipped"))
            {
                var t = Line((string.IsNullOrWhiteSpace(r.Note) ? "committee skipped (no reason given)" : r.Note!) + " · " + FundFormat.LocalTime(r.CreatedAt, "yyyy-MM-dd HH:mm"),
                             10.5, FundPalette.Risk);
                AutomationProperties.SetName(t, "Committee note: " + t.Text);
                _skippedHost.Children.Add(t);
            }
            RenderHistory();
            if (selectNewest)
            {
                var target = _runs.FirstOrDefault(r => r.Status == "running") ?? _runs.FirstOrDefault(r => r.Status != "skipped");
                if (target?.Id != null) await ShowRunAsync(target.Id);
            }
        }

        private void RenderHistory()
        {
            _historyHost.Children.Clear();
            var shown = _runs.Where(r => r.Status != "skipped").ToList();
            if (shown.Count == 0)
            {
                _historyHost.Children.Add(Line(_runs.Count == 0 ? "No committee runs yet." : "No committee memo yet.", 10.5, FundPalette.Faint));
                return;
            }
            var exp = new Expander
            {
                Header = FundUi.Text("Committee history (" + shown.Count.ToString(Inv) + ")", 10.5, FundPalette.Text, bold: true),
                Margin = new Thickness(0, 4, 0, 2),
            };
            AutomationProperties.SetName(exp, "Committee history, newest first");
            var list = new StackPanel();
            foreach (var r in shown)
            {
                var id = r.Id;
                var current = id != null && id == _shownRunId;
                var b = FundUi.MakeButton(RunText(r) + (current ? "  (shown)" : ""));
                b.HorizontalAlignment = HorizontalAlignment.Left;
                b.FontSize = 11;
                b.Margin = new Thickness(0, 0, 0, 4);
                b.BorderBrush = current ? FundPalette.Ai : FundPalette.ButtonBorder;
                AutomationProperties.SetName(b, "Committee run " + RunText(r) + (current ? ", shown" : ""));
                if (id != null) b.Click += async (_, _) => await ShowRunAsync(id);
                list.Children.Add(b);
            }
            exp.Content = list;
            _historyHost.Children.Add(exp);
        }

        private static string RunText(CommitteeRun r)
            => FundFormat.LocalTime(r.CreatedAt, "yyyy-MM-dd HH:mm") + " · " + (r.Status ?? "?") + " · " + (r.Trigger ?? "?")
               + " · v" + (r.Version?.ToString(Inv) ?? "?") + " · " + TierLabel(r.ModelTier) + " · " + FundFormat.Usd(r.CostUsd);

        // =====================================================================================
        // Convene, progress, cancel
        // =====================================================================================

        private async Task ToggleOptionsAsync()
        {
            if (_options.Visibility == Visibility.Visible)
            {
                _options.Visibility = Visibility.Collapsed;
                return;
            }
            _options.Visibility = Visibility.Visible;
            _standard.Content = "Standard · estimating…";
            _premium.Content = "Premium · estimating…";
            var s = await PlatformClient.GetCommitteeEstimateAsync(_strategyId, _version, _validationRunId, PlatformClient.ExplainModelStandard);
            var p = await PlatformClient.GetCommitteeEstimateAsync(_strategyId, _version, _validationRunId, PlatformClient.ExplainModelPremium);
            _standard.Content = "Standard" + EstimateText(s);
            _premium.Content = "Premium" + EstimateText(p);
            if (!s.Ok) ShowError(s.Message);
        }

        private static string EstimateText(PlatformResult<CommitteeEstimate> r)
            => r.Ok && r.Value?.EstimateUsd is double e ? " · est. " + FundFormat.Usd(e) + " for " + (r.Value.Roles?.ToString(Inv) ?? "5") + " roles" : "";

        private async Task StartAsync()
        {
            if (_busy) return;
            var model = _premium.IsChecked == true ? PlatformClient.ExplainModelPremium : PlatformClient.ExplainModelStandard;
            _busy = true;
            _start.IsEnabled = false;
            _errorHost.Children.Clear();
            try
            {
                var res = await PlatformClient.StartCommitteeAsync(_strategyId, _version, _validationRunId, model);
                if (!res.Ok || res.Value?.Run?.Id == null)
                {
                    ShowError(res.Ok ? "Unexpected response from the backend (/v2/committee)." : res.Message);
                    return;
                }
                _options.Visibility = Visibility.Collapsed;
                var id = res.Value.Run.Id!;
                await LoadRunsAsync(selectNewest: false);
                await ShowRunAsync(id);
            }
            finally
            {
                _busy = false;
                _start.IsEnabled = true;
            }
        }

        private async Task ShowRunAsync(string runId)
        {
            _shownRunId = runId;
            var res = await PlatformClient.GetCommitteeAsync(runId);
            if (runId != _shownRunId) return;
            if (!res.Ok || res.Value == null)
            {
                ShowError(res.Ok ? "Unexpected response from the backend (committee run)." : res.Message);
                return;
            }
            _shown = res.Value;
            RenderRun();
            RenderHistory();
            UpdatePolling();
        }

        private async Task PollAsync()
        {
            if (_polling || _shownRunId == null || !IsVisible) return;
            _polling = true;
            try
            {
                var id = _shownRunId;
                var res = await PlatformClient.GetCommitteeAsync(id);
                if (id != _shownRunId || !res.Ok || res.Value == null) return;   // the last state stays; the next tick tries again
                var wasRunning = _shown?.Run?.Status == "running";
                _shown = res.Value;
                RenderRun();
                if (wasRunning && _shown.Run?.Status != "running") await LoadRunsAsync(selectNewest: false);
            }
            finally
            {
                _polling = false;
                UpdatePolling();
            }
        }

        private void UpdatePolling()
        {
            if (IsLoaded && _shown?.Run?.Status == "running") { if (!_poll.IsEnabled) _poll.Start(); }
            else _poll.Stop();
        }

        private async Task CancelAsync(string runId, Button button)
        {
            if (!FundDialogs.Confirm(Window.GetWindow(this), "Cancel committee", "Stop the committee before the next role? The roles so far are charged and kept.", "Cancel committee", danger: true))
                return;
            button.IsEnabled = false;
            var res = await PlatformClient.CancelCommitteeAsync(runId);
            if (!res.Ok) ShowError(res.Message);
            button.IsEnabled = true;
            await ShowRunAsync(runId);
        }

        // =====================================================================================
        // Rendering one run
        // =====================================================================================

        private void RenderRun()
        {
            _progressHost.Children.Clear();
            _memoHost.Children.Clear();
            var d = _shown;
            var run = d?.Run;
            if (d == null || run == null) return;

            var roles = run.Roles is { Count: > 0 } rl ? rl : DefaultRoleOrder.ToList();
            var done = run.Progress?.Done ?? 0;
            var progress = new StackPanel();
            progress.Children.Add(Line("Committee run " + (run.Id ?? "?") + " · " + RunText(run)
                                       + (run.HoldoutIncluded == true ? " · includes the opened holdout of this version" : ""), 10.5, FundPalette.Dim, mono: true));
            if (!string.IsNullOrWhiteSpace(run.Note)) progress.Children.Add(Line(run.Note!, 10.5, FundPalette.Risk));
            var rolesRow = new WrapPanel();
            for (var i = 0; i < roles.Count; i++)
            {
                var role = roles[i];
                var isDone = i < done;
                var isCurrent = run.Status == "running" && role == run.Progress?.CurrentRole;
                var mark = isDone ? "✓ " : isCurrent ? "▶ " : "○ ";
                var t = FundUi.Text(mark + RoleLabel(role), 10.5, isDone ? FundPalette.Long : isCurrent ? FundPalette.Ai : FundPalette.Faint, mono: true, wrap: false);
                t.Margin = new Thickness(0, 2, 14, 2);
                AutomationProperties.SetName(t, RoleLabel(role) + (isDone ? ": done" : isCurrent ? ": working" : ": pending"));
                rolesRow.Children.Add(t);
            }
            progress.Children.Add(rolesRow);
            if (run.Status == "running" && run.Id != null)
            {
                string runId = run.Id;
                var cancel = FundUi.MakeButton("Cancel");
                cancel.HorizontalAlignment = HorizontalAlignment.Left;
                cancel.Margin = new Thickness(0, 4, 0, 0);
                AutomationProperties.SetName(cancel, "Cancel the committee run before the next role");
                cancel.Click += async (_, _) => await CancelAsync(runId, cancel);
                progress.Children.Add(cancel);
            }
            _progressHost.Children.Add(progress);

            var messages = d.Messages ?? new List<CommitteeMessage>();
            if (messages.Count == 0) return;
            if (_noticeForMemo.Parent is Panel old) old.Children.Remove(_noticeForMemo);
            _memoHost.Children.Add(_noticeForMemo);
            foreach (var m in messages.OrderBy(x => x.Seq ?? 0))
                _memoHost.Children.Add(RoleElement(m, m.Role == "chair" ? d.Memo : null, m.Role == "chair" ? d.MemoCheck ?? m.Check : m.Check));
            var adv = Line("Advisory only — the memo never changes the strategy's state, sizing or gates.", 10.5, FundPalette.Faint);
            adv.FontStyle = FontStyles.Italic;
            _memoHost.Children.Add(adv);
        }

        private static FrameworkElement RoleElement(CommitteeMessage m, CommitteeMemo? memo, ExplanationCheck? check)
        {
            if (m.Discarded == true)
            {
                // review: a role text that failed the topic check is replaced by the backend's fixed note — neutral, no AI label
                var ns = new StackPanel();
                ns.Children.Add(FundUi.Text("System note · " + RoleLabel(m.Role) + " · cost " + FundFormat.Usd(m.CostUsd ?? BtJson.Num(m.Usage, "cost_usd"))
                                            + (string.IsNullOrWhiteSpace(m.DiscardedReason) ? "" : " · " + m.DiscardedReason!.Replace('_', ' ')), 10, FundPalette.Faint, mono: true));
                var fixedNote = BtJson.Str(m.Output, "summary");
                ns.Children.Add(FundUi.Text(string.IsNullOrWhiteSpace(fixedNote) ? "This committee contribution was not shown." : fixedNote, 11, FundPalette.Text));
                var nb = new Border
                {
                    Background = FundPalette.WindowBg,
                    BorderBrush = FundPalette.Hairline,
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(6),
                    Padding = new Thickness(10, 5, 10, 6),
                    Margin = new Thickness(0, 4, 0, 6),
                    Child = ns,
                };
                AutomationProperties.SetName(nb, "System note: " + RoleLabel(m.Role) + " — " + (fixedNote ?? "not shown"));
                return nb;
            }
            FundAi.TryRead(m.Provenance, null, m.CreatedAt, out var model, out var at);
            var unverified = check?.Unverified ?? new List<UnverifiedNumber>();
            var unlocated = new List<UnverifiedNumber>();
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text(AiContent.Glyph + " AI-generated · " + (string.IsNullOrWhiteSpace(model) ? "model not reported" : model)
                                           + " · " + (at.HasValue ? at.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm", Inv) : FundFormat.LocalTime(m.CreatedAt, "yyyy-MM-dd HH:mm"))
                                           + " · " + RoleLabel(m.Role), 10.5, FundPalette.Ai, bold: true, mono: true));
            stack.Children.Add(Line("cost " + FundFormat.Usd(m.CostUsd ?? BtJson.Num(m.Usage, "cost_usd"))
                                    + (BtJson.Num(m.Usage, "estimate_usd") is double est ? " · estimate was " + FundFormat.Usd(est) : ""), 10, FundPalette.Faint, mono: true));

            if (m.Role == "chair")
            {
                if (memo == null)
                {
                    stack.Children.Add(Line("The backend sent no memo for the Chair.", 10.5, FundPalette.Risk));
                }
                else
                {
                    stack.Children.Add(Para(memo.Summary, "summary", unverified, unlocated, 12, FundPalette.TextBright));
                    AddList(stack, "Arguments for", memo.ArgumentsFor, "arguments_for", unverified, unlocated);
                    AddList(stack, "Arguments against", memo.ArgumentsAgainst, "arguments_against", unverified, unlocated);
                    if (memo.Confidence != null)
                    {
                        stack.Children.Add(Sub("Confidence " + (memo.Confidence.Value is double cv ? cv.ToString("0.00", Inv) + " (0–1)" : "not given")));
                        stack.Children.Add(Para(memo.Confidence.Reason, "confidence.reason", unverified, unlocated, 11, FundPalette.Dim));
                    }
                    if (memo.Sizing != null)
                    {
                        stack.Children.Add(Sub("Conditional sizing " + (memo.Sizing.PercentOfCapital is double pc ? pc.ToString("0.##", Inv) + " % of capital" : "not given")));
                        AddList(stack, null, memo.Sizing.Rules, "sizing.rules", unverified, unlocated);
                    }
                    AddList(stack, "Stop rules", memo.StopRules, "stop_rules", unverified, unlocated);
                    AddList(stack, "Open questions", memo.OpenQuestions, "open_questions", unverified, unlocated);
                }
            }
            else
            {
                stack.Children.Add(Para(BtJson.Str(m.Output, "summary"), "summary", unverified, unlocated, 11.5, FundPalette.Text));
                var points = BtJson.Array(m.Output, "points").Select(p => p.ValueKind == JsonValueKind.String ? p.GetString() ?? "" : p.GetRawText()).ToList();
                AddList(stack, null, points, "points", unverified, unlocated);
            }

            // number check summary (shown, never removed)
            if (check == null) stack.Children.Add(Line("Number check: the backend sent no check result.", 10, FundPalette.Risk));
            else if (unverified.Count == 0 && (check.UnknownIds?.Count ?? 0) == 0)
                stack.Children.Add(Line("Number check: every number was found in the computed data.", 10, FundPalette.Faint));
            else
            {
                if (unverified.Count > 0)
                    stack.Children.Add(Line(unverified.Count.ToString(Inv) + (unverified.Count == 1 ? " number is" : " numbers are") + " not in the computed data (marked red): "
                                            + string.Join(", ", unverified.Select(u => "\"" + (u.Text ?? "?") + "\"")), 10, FundPalette.Kill));
                if (unlocated.Count > 0)
                    stack.Children.Add(Line("Could not be marked in the text: " + string.Join(", ", unlocated.Select(u => "\"" + (u.Text ?? "?") + "\" (" + (u.Section ?? "?") + ")")), 10, FundPalette.Kill));
                if (check.UnknownIds is { Count: > 0 } ids)
                    stack.Children.Add(Line("Cited fact ids that do not exist: " + string.Join(", ", ids), 10, FundPalette.Kill));
            }
            var caveat = FundUi.Text(AiContent.ResearchNotice, 10, FundPalette.Faint);   // Stage 10: research output notice (b)
            caveat.FontStyle = FontStyles.Italic;
            caveat.Margin = new Thickness(0, 4, 0, 0);
            stack.Children.Add(caveat);
            var border = new Border
            {
                Background = FundPalette.AiSurface,
                BorderBrush = FundPalette.Ai,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(12, 8, 12, 10),
                Margin = new Thickness(0, 4, 0, 6),
                Child = stack,
            };
            AutomationProperties.SetName(border, AiContent.AutomationName(model, at) + " Committee role " + RoleLabel(m.Role) + ".");
            return border;
        }

        /// <summary>One plain-text field; unverified numbers of <paramref name="field"/> marked (ExplainPanel.Rich).</summary>
        private static TextBlock Para(string? text, string field, List<UnverifiedNumber> unverified, List<UnverifiedNumber> unlocated, double size, Brush brush)
        {
            var t = ExplainPanel.Rich(text ?? "", field, unverified, unlocated, size, brush, bold: false);
            t.Margin = new Thickness(0, 3, 0, 3);
            return t;
        }

        private static void AddList(StackPanel stack, string? title, List<string>? items, string field, List<UnverifiedNumber> unverified, List<UnverifiedNumber> unlocated)
        {
            if (items == null || items.Count == 0) return;
            if (title != null) stack.Children.Add(Sub(title));
            for (var i = 0; i < items.Count; i++)
            {
                // the offsets refer to the item text itself (field "<list>.<i>"), so the bullet is a separate element
                var t = ExplainPanel.Rich(items[i], field + "." + i.ToString(Inv), unverified, unlocated, 11, FundPalette.Dim, bold: false);
                var grid = new Grid { Margin = new Thickness(0, 1, 0, 2) };
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(14) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                grid.Children.Add(FundUi.Text("•", 11, FundPalette.Faint));
                Grid.SetColumn(t, 1);
                grid.Children.Add(t);
                stack.Children.Add(grid);
            }
        }

        private static TextBlock Sub(string text)
        {
            var t = FundUi.Text(text, 10.5, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 6, 0, 1);
            return t;
        }

        private static string RoleLabel(string? role) => role switch
        {
            "researcher" => "Researcher (thesis)",
            "quant" => "Quant (implementation, data)",
            "risk_manager" => "Risk Manager (stress, drawdowns, sizing)",
            "reviewer" => "Reviewer / Devil's Advocate",
            "chair" => "Chair (memo)",
            null or "" => "role not reported",
            _ => role!,
        };

        private static string TierLabel(string? tier) => tier switch
        {
            "standard" => "Standard",
            "premium" => "Premium",
            null or "" => "tier not reported",
            _ => tier!,
        };

        private void ShowError(string message)
        {
            _errorHost.Children.Clear();
            _errorHost.Children.Add(new Border
            {
                Background = FundPalette.KillSurface,
                BorderBrush = FundPalette.Kill,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 4, 0, 6),
                Child = FundUi.Text(message, 11.5, FundPalette.Make(0xFFF4A197)),
            });
        }

        private static TextBlock Line(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
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
                Margin = new Thickness(0, 0, 14, 0),
                VerticalAlignment = VerticalAlignment.Center,
            };
            AutomationProperties.SetName(rb, name);
            return rb;
        }
    }
}

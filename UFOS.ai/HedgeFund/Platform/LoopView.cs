using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Hedge Fund → "Loop" (Strategy Platform stage 6, discovery loop — SPEC §16, exact JSON §16.12, WPF notes §16.13).
    /// Left: the loop runs (newest first), "✦ New loop…" (start dialog: seed = Library strategy | idea text | confirmed
    /// paper, variants × generations, model, the backend's pre-start estimate with the per-run AI cap and the projected
    /// G4 impact, the "fully autonomous" switch state) and "Settings…" (autonomous switch with an explicit confirmation
    /// when turning it on, the three loop AI limits, the backend's defaults). Right: the selected run — pinned
    /// AI-interaction notice, status (incl. waiting for the backtest slot / the connection, paused, paused by the budget,
    /// interrupted with its resume state), Pause / Resume / Cancel as the backend's `can` allows, progress of the running
    /// step and job, counts, loop AI spend, projected trials / G4 bar; the finalists panel (approval mode: Approve /
    /// Discard each; autonomous: the loop's own validation results; "Open report", "Open validation report", "Open in
    /// Library"; never a holdout button); a generation × slot grid (state, score, kept / refined / culled / duplicate
    /// markers) and per generation the variants with versions + search points, the deterministic score card, the
    /// pre-checks, the AI diagnosis and refinement in the violet AI container (EU AI Act label before, caveat after, cost
    /// line, unverified numbers marked red, operator requests as "unverified AI suggestion"), the spec diff against the
    /// parent and the activity log. All texts are plain text (AI texts, strategy names and seed ideas are untrusted).
    /// Backend errors are shown verbatim; nothing is retried with other inputs or replaced. Polls every 2 s only while
    /// the view is visible and the loop is not finished (or a finalist validation is pending); a lost connection keeps
    /// the last known state with a "connection lost (attempt n)" banner and keeps polling.
    /// </summary>
    internal sealed class LoopView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(2);
        private const int ListEveryNthPoll = 10;      // the run list is refreshed every 20 s while polling
        private const int MaxEventsKept = 500;
        private const int MaxEventsShown = 200;
        private const int MaxDiffLines = 60;

        private readonly StackPanel _listPanel;
        private readonly TextBlock _listStatus;
        private readonly Button _newLoop;
        private readonly Button _settings;
        private readonly Border _notice;
        private readonly StackPanel _statusHost;
        private readonly StackPanel _body;
        private readonly ScrollViewer _bodyScroll;
        private readonly StackPanel _errorHost;
        private readonly DispatcherTimer _poll;

        private List<LoopSummary> _loops = new();
        private readonly Dictionary<string, string> _strategyNames = new(StringComparer.Ordinal);
        private string? _loopId;
        private LoopDetail? _detail;
        private readonly List<LoopEvent> _events = new();
        private long? _lastEventId;
        private string? _bodySignature;
        // CHART_FULLSCREEN: the generation × slot grid sits in this persistent host, so its full screen (Shared/FullScreenHost) survives
        // the 2-s poll re-renders — while it is full screen only the grid is refreshed in place, the rest of the body on exit.
        private readonly Border _gridHost = new();
        private bool _bodyRenderPending;
        private string? _connectionProblem;
        private int _connectionAttempts;
        private int _pollCount;
        private int _detailSeq;
        private int _listSeq;
        private bool _polling;
        private bool _stopped;
        private bool _busy;
        private string? _lastStatus;

        // expander states that survive a re-render (key → expanded)
        private readonly Dictionary<string, bool> _expanded = new(StringComparer.Ordinal);
        private readonly Dictionary<string, LoopVariant> _variantsById = new(StringComparer.Ordinal);
        private readonly Dictionary<string, FrameworkElement> _variantElements = new(StringComparer.Ordinal);
        // stored specs for the diff (strategy@version) — versions are immutable, so the cache never goes stale
        private readonly Dictionary<string, Task<PlatformResult<StrategyVersionDetail>>> _versionCache = new(StringComparer.Ordinal);

        /// <summary>"Open in Library" (argument: strategy id) — HedgeFundWindow switches to the Library.</summary>
        public event Action<string>? OpenInLibraryRequested;

        public LoopView()
        {
            AutomationProperties.SetName(this, "Discovery loop");
            Focusable = false;
            var root = new Grid();
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(270) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            // ----- runs -----
            var left = new DockPanel();
            var lHead = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            lHead.Children.Add(FundUi.Text("Discovery loops", 12, FundPalette.Text, bold: true));
            var refresh = FundUi.MakeButton("↻");
            refresh.Padding = new Thickness(8, 1, 8, 1);
            refresh.MinHeight = 20;
            refresh.FontSize = 11;
            refresh.HorizontalAlignment = HorizontalAlignment.Right;
            refresh.ToolTip = "Reload the loop runs (F5)";
            AutomationProperties.SetName(refresh, "Reload the discovery loop runs");
            refresh.Click += async (_, _) => await ActivateAsync();
            lHead.Children.Add(refresh);
            DockPanel.SetDock(lHead, Dock.Top);
            left.Children.Add(lHead);

            _newLoop = FundUi.MakeButton(AiContent.Glyph + " New loop…", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _newLoop.HorizontalAlignment = HorizontalAlignment.Stretch;
            _newLoop.Margin = new Thickness(0, 0, 4, 6);
            _newLoop.ToolTip = "Start a discovery loop (Ctrl+N): variants × generations of AI diagnosis + refinement, every variant backtested. "
                               + AiContent.TriggerTooltip;
            AutomationProperties.SetName(_newLoop, "Start a new discovery loop (AI)");
            AutomationProperties.SetHelpText(_newLoop, AiContent.TriggerTooltip);
            _newLoop.Click += async (_, _) => await NewLoopAsync();
            DockPanel.SetDock(_newLoop, Dock.Top);
            left.Children.Add(_newLoop);

            _settings = FundUi.MakeButton("Settings…");
            _settings.HorizontalAlignment = HorizontalAlignment.Stretch;
            _settings.Margin = new Thickness(0, 0, 4, 8);
            _settings.ToolTip = "\"Fully autonomous\" switch, the loop AI budget (day / month / per run) and the defaults";
            AutomationProperties.SetName(_settings, "Discovery loop settings");
            _settings.Click += async (_, _) => await OpenSettingsAsync();
            DockPanel.SetDock(_settings, Dock.Top);
            left.Children.Add(_settings);

            _listStatus = FundUi.Text("", 10.5, FundPalette.Faint);
            _listStatus.Margin = new Thickness(0, 0, 0, 6);
            DockPanel.SetDock(_listStatus, Dock.Top);
            left.Children.Add(_listStatus);
            _listPanel = new StackPanel();
            AutomationProperties.SetName(_listPanel, "Discovery loop runs, newest first");
            left.Children.Add(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = _listPanel });
            root.Children.Add(new Border
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(10, 10, 6, 10),
                Child = left,
            });

            // ----- selected run -----
            var right = new Grid { Margin = new Thickness(12, 0, 0, 0) };
            right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            right.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Grid.SetColumn(right, 1);
            root.Children.Add(right);

            _notice = StudioView.NoticeBlock();   // the backend's fixed AI-interaction notice, pinned, not dismissible
            Place(right, _notice, 0);
            _statusHost = new StackPanel { Margin = new Thickness(0, 0, 10, 6) };
            AutomationProperties.SetName(_statusHost, "Loop status");
            Place(right, _statusHost, 1);
            _body = new StackPanel { Margin = new Thickness(0, 0, 10, 0) };
            AutomationProperties.SetName(_body, "Loop finalists and generations");
            _bodyScroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = _body, Focusable = false };
            Place(right, _bodyScroll, 2);
            _errorHost = new StackPanel { Margin = new Thickness(0, 0, 10, 0) };
            Place(right, _errorHost, 3);

            Content = root;
            ShowBodyPlaceholder("Select a loop run or start one with \"✦ New loop…\".");

            _poll = new DispatcherTimer { Interval = PollInterval };
            _poll.Tick += async (_, _) => await PollAsync();
            FullScreenHost.RegisterF11(_gridHost, () => _gridHost, "Loop population", PopulationHeader);
            // after the grid's full screen ended (Esc / button / F11) the body re-renders once with what changed meanwhile
            _gridHost.SizeChanged += (_, _) =>
            {
                if (!_bodyRenderPending || FullScreenHost.IsActive(_gridHost)) return;
                Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() =>
                {
                    if (_bodyRenderPending && !FullScreenHost.IsActive(_gridHost)) RenderBody();
                }));
            };
            IsVisibleChanged += (_, _) => UpdatePolling();
            PreviewKeyDown += async (_, e) =>
            {
                if (e.Key == Key.F5)
                {
                    e.Handled = true;
                    await ActivateAsync();
                }
                else if (e.Key == Key.N && (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control)
                {
                    e.Handled = true;
                    await NewLoopAsync();
                }
            };
        }

        /// <summary>Called whenever the rail's "Loop" entry is selected: reloads the runs and the selected one.</summary>
        public async Task ActivateAsync()
        {
            if (_stopped) return;
            _ = LoadStrategyNamesAsync();
            await LoadListAsync();
            var target = _loopId != null && _loops.Any(l => l.Id == _loopId) ? _loopId : _loops.FirstOrDefault()?.Id;
            if (target != null) await SelectLoopAsync(target, force: true);
            else if (_loopId == null)
            {
                _detail = null;
                RenderStatus();
                ShowBodyPlaceholder(_loops.Count == 0 && _listStatus.Foreground != FundPalette.Kill
                    ? "No discovery loop yet — start one with \"✦ New loop…\"."
                    : "Select a loop run.");
            }
        }

        /// <summary>Window closed: stop polling for good.</summary>
        public void Shutdown()
        {
            _stopped = true;
            _poll.Stop();
        }

        // =====================================================================================
        // Run list
        // =====================================================================================

        private async Task LoadStrategyNamesAsync()
        {
            var res = await PlatformClient.GetStrategiesAsync();
            if (_stopped || !res.Ok || res.Value?.Strategies == null) return;   // names are labels only; ids are shown otherwise
            foreach (var s in res.Value.Strategies)
                if (s.Id.Length > 0 && !string.IsNullOrWhiteSpace(s.Name)) _strategyNames[s.Id] = s.Name!;
            RenderList();
        }

        private async Task LoadListAsync()
        {
            var seq = ++_listSeq;
            if (_loops.Count == 0) _listStatus.Text = "Loading…";
            var res = await PlatformClient.ListLoopsAsync(50);
            if (seq != _listSeq || _stopped) return;
            if (!res.Ok || res.Value == null)
            {
                _listStatus.Text = res.Ok ? "Unexpected response from the backend (/v2/loops) — backend and app versions may not match." : res.Message;
                _listStatus.Foreground = FundPalette.Kill;
                RenderList();
                return;
            }
            _loops = res.Value.Loops ?? new List<LoopSummary>();
            ApplyNotice(res.Value.AiInteractionNotice);
            _listStatus.Text = _loops.Count.ToString(Inv) + (_loops.Count == 1 ? " run" : " runs") + " · one loop at a time";
            _listStatus.Foreground = FundPalette.Faint;
            RenderList();
        }

        private void RenderList()
        {
            _listPanel.Children.Clear();
            if (_loops.Count == 0)
            {
                _listPanel.Children.Add(FundUi.Text("No loop runs yet.", 11, FundPalette.Faint));
                return;
            }
            foreach (var l in _loops)
            {
                var id = l.Id ?? "";
                var selected = id == _loopId;
                // the selected run shows its live status (the list entry is refreshed less often)
                var status = selected && _detail != null && _detail.Id == id ? _detail.Status ?? l.Status : l.Status;
                var stack = new StackPanel();
                var title = FundUi.Text(LoopText.SeedLabel(l.Seed, l.SeedText, _strategyNames, 60), 11.5,
                                        selected ? FundPalette.TextBright : FundPalette.Text, bold: selected);
                stack.Children.Add(title);
                var st = FundUi.Text(LoopText.StatusLabel(status), 10.5, LoopText.StatusBrush(status), bold: true);
                stack.Children.Add(st);
                var c = l.Config;
                var line = FundFormat.LocalTime(l.CreatedAt, "yyyy-MM-dd HH:mm")
                           + " · " + (c?.Variants?.ToString(Inv) ?? "?") + "×" + (c?.Generations?.ToString(Inv) ?? "?")
                           + " · gen " + (l.GenerationsDone?.ToString(Inv) ?? "?") + "/" + (c?.Generations?.ToString(Inv) ?? "?")
                           + " · best " + LoopText.Score(l.BestScore)
                           + " · " + (l.Finalists?.ToString(Inv) ?? "?") + " finalist" + (l.Finalists == 1 ? "" : "s")
                           + (c?.Autonomous == true ? " · autonomous" : "");
                stack.Children.Add(FundUi.Text(line, 10, FundPalette.Faint, mono: true));
                var b = FundUi.MakeButton("");
                b.Content = stack;
                stack.HorizontalAlignment = HorizontalAlignment.Left;
                b.HorizontalContentAlignment = HorizontalAlignment.Stretch;
                b.Padding = new Thickness(8, 6, 8, 6);
                b.Margin = new Thickness(0, 0, 4, 6);
                b.Background = selected ? FundPalette.Make(0x267C8CF8) : FundPalette.Make(0xFF1E1E1E);
                b.BorderBrush = selected ? FundPalette.Focus : FundPalette.PanelBorder;
                AutomationProperties.SetName(b, "Loop run " + LoopText.SeedLabel(l.Seed, l.SeedText, _strategyNames, 60) + ", " + LoopText.StatusLabel(status)
                                                + (selected ? ", selected" : "") + ", " + line);
                b.Click += async (_, _) => { if (id.Length > 0) await SelectLoopAsync(id, force: false); };
                _listPanel.Children.Add(b);
            }
        }

        // =====================================================================================
        // One run: load, poll
        // =====================================================================================

        private async Task SelectLoopAsync(string loopId, bool force)
        {
            if (_loopId != loopId)
            {
                _detail = null;
                _events.Clear();
                _lastEventId = null;
                _bodySignature = null;
                _expanded.Clear();
                _connectionProblem = null;
                _connectionAttempts = 0;
                _errorHost.Children.Clear();
                ShowBodyPlaceholder("Loading…");
            }
            else if (force)
            {
                _bodySignature = null;   // re-render once (e.g. after coming back to the view)
            }
            _loopId = loopId;
            RenderList();
            await LoadDetailAsync(loopId, quiet: false);
        }

        private async Task LoadDetailAsync(string loopId, bool quiet)
        {
            var seq = ++_detailSeq;
            var res = await PlatformClient.GetLoopAsync(loopId, _detail != null ? _lastEventId : null);
            if (seq != _detailSeq || _stopped || loopId != _loopId) return;
            if (!res.Ok || res.Value == null)
            {
                if (res.Status == PlatformCallStatus.NotFound)
                {
                    _detail = null;
                    _connectionProblem = null;
                    RenderStatus();
                    ShowBodyPlaceholder(res.Message);
                    UpdatePolling();
                    return;
                }
                // connection lost / backend restarting: keep the last known state, say so, keep polling
                _connectionAttempts++;
                _connectionProblem = res.Ok ? "Unexpected response from the backend (/v2/loops/" + loopId + ") — backend and app versions may not match." : res.Message;
                if (_detail == null && !quiet) ShowBodyPlaceholder(_connectionProblem);
                RenderStatus();
                UpdatePolling();
                return;
            }
            var reconnected = _connectionProblem != null;
            _connectionProblem = null;
            _connectionAttempts = 0;
            var d = res.Value;
            if (d.Events != null)
            {
                foreach (var e in d.Events)
                    if (e.Id is long eid && (_lastEventId == null || eid > _lastEventId)) _events.Add(e);
                if (_events.Count > MaxEventsKept) _events.RemoveRange(0, _events.Count - MaxEventsKept);
            }
            if (d.LastEventId is long last) _lastEventId = last;
            _detail = d;
            ApplyNotice(d.AiInteractionNotice);
            RenderStatus();
            var sig = Signature(d);
            if (sig != _bodySignature || reconnected)
            {
                _bodySignature = sig;
                RenderBody();
            }
            if (_lastStatus != d.Status)
            {
                var first = _lastStatus == null;
                _lastStatus = d.Status;
                if (!first) await LoadListAsync();   // status changed: the list entry follows
                RenderList();
            }
            UpdatePolling();
        }

        private async Task PollAsync()
        {
            if (_polling || _stopped || _busy || _loopId == null || !IsVisible) return;
            _polling = true;
            try
            {
                await LoadDetailAsync(_loopId, quiet: true);
                if (++_pollCount % ListEveryNthPoll == 0) await LoadListAsync();
            }
            finally
            {
                _polling = false;
            }
        }

        private void UpdatePolling()
        {
            if (!_stopped && IsVisible && _loopId != null && NeedsPolling()) { if (!_poll.IsEnabled) _poll.Start(); }
            else _poll.Stop();
        }

        private bool NeedsPolling()
        {
            if (_connectionProblem != null) return true;
            if (_detail == null) return false;
            if (!LoopText.IsTerminal(_detail.Status) || _detail.Active == true) return true;
            // finished loop: a finalist validation may still be submitted / running (approval mode)
            return (_detail.Finalists ?? new List<LoopFinalist>()).Any(f => f.State == "approved" || f.State == "validating");
        }

        /// <summary>Changes that need the finalists / generations re-rendered (status, progress and budget re-render every poll).</summary>
        private static string Signature(LoopDetail d)
        {
            var parts = new List<string> { d.Id ?? "", (d.LastEventId ?? -1).ToString(Inv), d.FinalistsNote ?? "", d.DoneNote ?? "",
                                           d.Config?.Autonomous == true ? "A" : "a" };
            foreach (var g in d.Generations ?? new List<LoopGeneration>())
            {
                parts.Add("g" + (g.Generation ?? 0).ToString(Inv) + (g.Selection is JsonElement s && s.ValueKind == JsonValueKind.Object ? "s" : ""));
                foreach (var v in g.Variants ?? new List<LoopVariant>())
                {
                    parts.Add((v.Id ?? "") + ":" + (v.State ?? "") + ":" + (v.TopicCheck ?? "") + ":" + (v.ChosenVersion ?? 0).ToString(Inv)
                              + ":" + (v.Diagnosis?.Pending == true ? "p" : "") + (v.Refinement?.Pending == true ? "p" : "")
                              + ":" + string.Join(",", (v.Versions ?? new List<LoopVariantVersion>()).Select(x => (x.RunId ?? "") + "=" + (x.RunStatus ?? ""))));
                }
            }
            foreach (var f in d.Finalists ?? new List<LoopFinalist>())
                parts.Add((f.VariantId ?? "") + ":" + (f.State ?? "") + ":" + (f.ValidationStatus ?? "") + ":" + (f.Passed?.ToString() ?? "") + ":" + (f.Version ?? 0).ToString(Inv));
            return string.Join("|", parts);
        }

        // =====================================================================================
        // Status area (re-rendered on every poll)
        // =====================================================================================

        private void RenderStatus()
        {
            _statusHost.Children.Clear();
            if (_connectionProblem != null)
            {
                var banner = ErrorBlock("Connection to the backend lost (attempt " + _connectionAttempts.ToString(Inv) + "): " + _connectionProblem
                                        + "\nShowing the last known state; checking again every 2 s. A loop that was running when the backend stopped is "
                                        + "shown as \"Interrupted\" once the backend is back — it resumes automatically when auto-resume is on.");
                AutomationProperties.SetLiveSetting(banner, AutomationLiveSetting.Polite);
                _statusHost.Children.Add(banner);
            }
            var d = _detail;
            if (d == null)
            {
                _statusHost.Children.Add(FundUi.Text("Discovery loop", 15, FundPalette.TextBright, bold: true));
                return;
            }

            // title + actions
            var title = new DockPanel { LastChildFill = true };
            var actions = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
            var can = d.Can;
            var loopId = d.Id ?? "";
            if (can?.Pause == true)
            {
                var pause = SmallButton("Pause", "Pause the loop after the current step");
                pause.ToolTip = "The running backtest finishes, then the loop stops and frees the job slot. Resume continues from the stored steps.";
                pause.Click += async (_, _) => await LoopActionAsync("pause", loopId, pause);
                actions.Children.Add(pause);
            }
            if (can?.Resume == true)
            {
                var resume = SmallButton(AiContent.Glyph + " Resume", "Resume the loop from its stored steps (AI)");
                resume.ToolTip = "Continues from the stored steps — completed steps are never recomputed; further AI calls cost money (loop budget). "
                                 + AiContent.TriggerTooltip;
                resume.Click += async (_, _) => await LoopActionAsync("resume", loopId, resume);
                actions.Children.Add(resume);
            }
            if (can?.Cancel == true)
            {
                var cancel = SmallButton("Cancel loop", "Cancel the loop");
                cancel.ToolTip = "Stops after the current step; the loop's running backtest or validation is cancelled. Stored variants and runs are kept.";
                cancel.Click += async (_, _) => await LoopActionAsync("cancel", loopId, cancel);
                actions.Children.Add(cancel);
            }
            DockPanel.SetDock(actions, Dock.Right);
            title.Children.Add(actions);
            var head = new WrapPanel();
            var t = FundUi.Text(LoopText.SeedLabel(d.Seed, null, _strategyNames, 120), 15, FundPalette.TextBright, bold: true);
            t.Margin = new Thickness(0, 0, 8, 0);
            head.Children.Add(t);
            var chip = Chip(LoopText.StatusLabel(d.Status), LoopText.StatusBrush(d.Status));
            AutomationProperties.SetName(chip, "Status: " + LoopText.StatusLabel(d.Status));
            AutomationProperties.SetLiveSetting(chip, AutomationLiveSetting.Polite);
            head.Children.Add(chip);
            title.Children.Add(head);
            _statusHost.Children.Add(title);

            _statusHost.Children.Add(Line("Loop " + (d.Id ?? "?") + " · created " + FundFormat.LocalTime(d.CreatedAt, "yyyy-MM-dd HH:mm")
                                          + " · root strategy " + (d.RootStrategyId ?? "not created yet")
                                          + " · family " + (d.FamilyId ?? "—"), 10.5, FundPalette.Faint, mono: true));
            _statusHost.Children.Add(Line(ConfigLine(d.Config), 10.5, FundPalette.Dim, mono: true));

            // status explanation (waiting / paused / interrupted / failed …)
            var explain = StatusExplanation(d);
            if (explain != null)
            {
                var e = Line(explain, 11, LoopText.StatusBrush(d.Status));
                AutomationProperties.SetLiveSetting(e, AutomationLiveSetting.Polite);
                _statusHost.Children.Add(e);
            }

            // progress of the current step and of its job
            if (d.Progress is JsonElement p && p.ValueKind == JsonValueKind.Object && !LoopText.IsTerminal(d.Status))
            {
                var phase = BtJson.Str(p, "phase");
                var where = BtJson.Str(p, "variant_id") is { Length: > 0 } vid ? " · " + VariantLabel(vid)
                          : BtJson.Num(p, "generation") is double g ? " · generation " + g.ToString("0", Inv) : "";
                var pct = BtJson.Num(p, "percent") is double pc ? " · " + pc.ToString("0", Inv) + " %" : "";
                var msg = BtJson.Str(p, "message") is { Length: > 0 } m ? " — " + m : "";
                var line = Line("Step: " + PhaseLabel(phase) + where + pct + msg + (BtJson.Str(p, "run_id") is { Length: > 0 } rid ? " · run " + rid : ""),
                                10.5, FundPalette.Focus, mono: true);
                AutomationProperties.SetLiveSetting(line, AutomationLiveSetting.Polite);
                _statusHost.Children.Add(line);
            }
            if (d.JobProgress is JsonElement jp && jp.ValueKind == JsonValueKind.Object && !LoopText.IsTerminal(d.Status))
            {
                var waiting = BtJson.Str(jp, "phase") == RunResume.WaitingStatus;
                _statusHost.Children.Add(Line("Job: " + (BtJson.Str(jp, "phase") ?? "?")
                                              + (BtJson.Num(jp, "percent") is double jpc ? " · " + jpc.ToString("0", Inv) + " %" : "")
                                              + (BtJson.Str(jp, "message") is { Length: > 0 } jm ? " — " + jm : "")
                                              + (waiting ? " (connection lost — the run resumes automatically)" : ""),
                                              10.5, waiting ? FundPalette.Risk : FundPalette.Dim, mono: true));
            }

            // counts, AI spend, trials / G4
            var c = d.Counts;
            _statusHost.Children.Add(Line("Backtests " + Int(BtJson.Num(c, "backtests_started")) + " of " + Int(BtJson.Num(c, "backtests_planned")) + " planned"
                                          + " · " + Int(BtJson.Num(c, "variants")) + " variants · " + Int(BtJson.Num(c, "failed")) + " failed · "
                                          + Int(BtJson.Num(c, "duplicates")) + " duplicates · " + Int(BtJson.Num(c, "ai_messages")) + " AI messages",
                                          10.5, FundPalette.Dim, mono: true));
            _statusHost.Children.Add(BudgetLine(d));
            var trials = Line(LoopText.TrialsLine(d.Trials) + " — every loop backtest counts as a trial of the seed's family.", 10.5, FundPalette.Risk);
            trials.ToolTip = BtJson.Str(d.Trials, "note");
            _statusHost.Children.Add(trials);
            if (d.RunCapReached == true)
                _statusHost.Children.Add(Line("Per-run AI cap reached — no new variants are created; the pending backtests and the selection finish.", 10.5, FundPalette.Risk));
            if (!string.IsNullOrWhiteSpace(d.DoneNote)) _statusHost.Children.Add(Line(d.DoneNote!, 11, FundPalette.Risk));
            foreach (var n in d.Notes ?? new List<string>())
                if (!string.IsNullOrWhiteSpace(n)) _statusHost.Children.Add(Line("Note: " + n, 10.5, FundPalette.Dim));
        }

        private static string ConfigLine(LoopConfig? c)
        {
            if (c == null) return "Config not reported by the backend.";
            return (c.Variants?.ToString(Inv) ?? "?") + " variants × " + (c.Generations?.ToString(Inv) ?? "?") + " generations · "
                   + (c.SearchPoints?.ToString(Inv) ?? "?") + " search point" + (c.SearchPoints == 1 ? "" : "s") + " · "
                   + LoopText.TierLabel(c.Model) + " model · data from " + (c.HistoryFrom ?? "?")
                   + " · auto-resume " + (c.AutoResume == true ? "on" : c.AutoResume == false ? "off" : "?")
                   + " · fully autonomous " + (c.Autonomous == true ? "ON" : c.Autonomous == false ? "OFF" : "?") + " (snapshot at start)";
        }

        private static string? StatusExplanation(LoopDetail d)
        {
            var reason = string.IsNullOrWhiteSpace(d.StatusReason) ? "" : " " + d.StatusReason;
            switch (d.Status)
            {
                case "running":
                    return "Working: AI diagnosis and refinement, then one backtest at a time on the shared job slot." + reason;
                case "waiting_slot":
                    return "Waiting for the backtest slot — another job (a manual backtest or validation, a Studio draft or a paper replication) "
                           + "is running. The loop continues automatically when the slot is free." + reason;
                case "waiting_connection":
                    return "The data connection was lost during a loop backtest. The loop checks it at most every 30 s (no time limit) and resumes "
                           + "the run automatically; a resumed run is still one trial. Cancel stops the loop." + reason;
                case "paused":
                    return "Paused by you. Resume continues from the stored steps." + reason;
                case "paused_budget":
                    return "Paused: the loop's daily or monthly AI budget is reached. Raise it (Settings… or the central AI budget window) or wait "
                           + "for the next UTC day, then Resume." + reason;
                case "interrupted":
                    return (d.Config?.AutoResume == true
                               ? "The backend stopped while the loop ran. With auto-resume on, it continues automatically after the backend has started; "
                                 + "if it still shows \"Interrupted\", use Resume."
                               : "The backend stopped while the loop ran. Resume continues from the stored steps.")
                           + " Completed steps are never recomputed; an AI call cut off by the stop is repeated and costs again." + reason;
                case "failed":
                    return "The loop failed:" + (reason.Length > 0 ? reason : " the backend gave no reason.");
                case "cancelled":
                    return "Cancelled." + reason;
                case "done":
                    return "Finished." + reason;
                default:
                    return reason.Length > 0 ? reason.Trim() : null;
            }
        }

        private static string PhaseLabel(string? phase) => phase switch
        {
            "seed" => "seed (AI draft of the idea / seed backtest)",
            "plan" => "planning the generation",
            "ai" => AiContent.Glyph + " AI diagnosis / refinement",
            "backtest" => "backtest",
            "validation" => "validation (G1–G8, no holdout)",
            "waiting_slot" => "waiting for the backtest slot",
            "waiting_connection" => "waiting for the connection",
            "selection" => "selection",
            null or "" => "—",
            _ => phase!,
        };

        private TextBlock BudgetLine(LoopDetail d)
        {
            var b = d.Budget;
            var limits = BtJson.Get(b, "limits");
            var unavailable = BtJson.Str(limits, "unavailable");
            var text = "Loop AI this run: " + FundFormat.Usd(BtJson.Num(b, "spent_run_usd"))
                       + (unavailable == null ? " of per-run cap " + LoopText.Usd2OrUnset(BtJson.Num(limits, "per_run_usd")) : "")
                       + " · AI messages " + FundFormat.Usd(BtJson.Num(b, "ai_messages_cost_usd"))
                       + (unavailable == null
                           ? " · daily cap " + LoopText.Usd2OrUnset(BtJson.Num(limits, "daily_usd")) + " · monthly budget " + LoopText.Usd2OrUnset(BtJson.Num(limits, "monthly_usd"))
                           : " · limits unavailable: " + unavailable)
                       + (BtJson.Str(b, "paused_reason") is { Length: > 0 } pr ? " · paused: " + pr : "");
            var line = Line(text, 10.5, unavailable != null || BtJson.Str(b, "paused_reason") != null ? FundPalette.Risk : FundPalette.Faint, mono: true);
            AutomationProperties.SetName(line, text);
            return line;
        }

        // =====================================================================================
        // Body: finalists, grid, generations, activity (re-rendered when something changed)
        // =====================================================================================

        private void RenderBody()
        {
            if (FullScreenHost.IsActive(_gridHost) && _detail is LoopDetail fd
                && (fd.Generations ?? new List<LoopGeneration>()).Count > 0)
            {
                var fgens = fd.Generations!.OrderBy(g => g.Generation ?? 0).ToList();
                foreach (var g in fgens)
                    foreach (var v in g.Variants ?? new List<LoopVariant>())
                        if (v.Id != null) _variantsById[v.Id] = v;
                _gridHost.Child = GridPanel(fd, fgens);
                _bodyRenderPending = true;
                return;
            }
            if (FullScreenHost.IsActive(_gridHost)) FullScreenHost.ActiveFor(_gridHost)?.Exit();   // the grid is gone (no generation)
            _bodyRenderPending = false;
            var offset = _bodyScroll.VerticalOffset;
            _body.Children.Clear();
            _variantsById.Clear();
            _variantElements.Clear();
            var d = _detail;
            if (d == null) return;
            var gens = (d.Generations ?? new List<LoopGeneration>()).OrderBy(g => g.Generation ?? 0).ToList();
            foreach (var g in gens)
                foreach (var v in g.Variants ?? new List<LoopVariant>())
                    if (v.Id != null) _variantsById[v.Id] = v;

            _body.Children.Add(FinalistsPanel(d));
            if (gens.Count == 0)
            {
                _body.Children.Add(Line(LoopText.IsTerminal(d.Status) ? "No generation was planned." : "No generation planned yet — the seed is prepared first.",
                                        11, FundPalette.Faint));
            }
            else
            {
                _gridHost.Child = GridPanel(d, gens);
                _body.Children.Add(_gridHost);
                var latest = gens.Last().Generation ?? 0;
                foreach (var g in gens) _body.Children.Add(GenerationElement(g, (g.Generation ?? 0) == latest));
            }
            _body.Children.Add(EventsExpander());
            if (!string.IsNullOrWhiteSpace(d.HoldoutNote))
                _body.Children.Add(Line(d.HoldoutNote!, 10.5, FundPalette.Faint));
            var disclaimer = Line("Hypothetical, simulated results — they do not indicate future results.", 10, FundPalette.Faint);
            disclaimer.FontStyle = FontStyles.Italic;
            _body.Children.Add(disclaimer);
            Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(() => _bodyScroll.ScrollToVerticalOffset(offset)));
        }

        private void ShowBodyPlaceholder(string text)
        {
            _body.Children.Clear();
            _variantElements.Clear();
            _body.Children.Add(FundUi.Text(text, 11.5, FundPalette.Faint));
        }

        // ----- finalists -----

        private FrameworkElement FinalistsPanel(LoopDetail d)
        {
            var stack = new StackPanel();
            var autonomous = d.Config?.Autonomous == true;
            stack.Children.Add(FundUi.Text("Finalists", 12.5, FundPalette.TextBright, bold: true));
            stack.Children.Add(Line(autonomous
                ? "Fully autonomous run: the loop validates its finalists itself (gates G1–G8, no holdout) and shows the results here."
                : "Approval mode: approve a finalist to run it through the validation gates (G1–G8, no holdout), or discard it. "
                  + "The loop never changes a lifecycle state — a finalist that passes is your candidate proposal: promote it to Candidate yourself in the Library.",
                10.5, FundPalette.Dim));
            var fins = d.Finalists ?? new List<LoopFinalist>();
            if (fins.Count == 0)
            {
                var text = !string.IsNullOrWhiteSpace(d.FinalistsNote) ? d.FinalistsNote!
                         : LoopText.IsTerminal(d.Status) ? "No finalists."
                         : "Finalists are chosen after the last generation's selection (best non-duplicate variants with a score > 0 that pass all pre-checks).";
                stack.Children.Add(Line(text, 11, !string.IsNullOrWhiteSpace(d.FinalistsNote) ? FundPalette.Risk : FundPalette.Faint));
            }
            foreach (var f in fins) stack.Children.Add(FinalistCard(d, f, autonomous));
            var panel = FundUi.Panel(stack, new Thickness(12, 10, 12, 10), new Thickness(0, 0, 0, 10));
            AutomationProperties.SetName(panel, "Finalists, " + fins.Count.ToString(Inv) + (autonomous ? ", gate validation run by the loop" : ", awaiting your approval"));
            return panel;
        }

        private FrameworkElement FinalistCard(LoopDetail d, LoopFinalist f, bool autonomous)
        {
            var stack = new StackPanel();
            var v = f.VariantId != null && _variantsById.TryGetValue(f.VariantId, out var fv) ? fv : null;
            var name = v?.StrategyName ?? f.StrategyId ?? "?";
            var head = new WrapPanel();
            var nt = FundUi.Text((v != null ? VariantLabel(v.Id) + " · " : "") + name, 12, FundPalette.TextBright, bold: true);
            nt.Margin = new Thickness(0, 0, 8, 0);
            head.Children.Add(nt);
            head.Children.Add(Chip(FinalistStateLabel(f.State), FinalistStateBrush(f.State)));
            stack.Children.Add(head);
            stack.Children.Add(Line("Strategy " + (f.StrategyId ?? "?") + " · v" + (f.Version?.ToString(Inv) ?? "?")
                                    + (f.EvaluatedVersion is int ev && ev != f.Version ? " (evaluated v" + ev.ToString(Inv) + ")" : "")
                                    + " · score " + LoopText.Score(f.Score) + " · Sharpe " + FundFormat.Num(f.Sharpe)
                                    + " · run " + (f.RunId ?? "?"), 10.5, FundPalette.Dim, mono: true));
            if (f.Promoted == true)
                stack.Children.Add(Line("Promoted: the evaluated version was re-saved as the strategy's current version (same trading logic, no new backtest).", 10.5, FundPalette.Faint));
            switch (f.State)
            {
                case "proposed":
                    stack.Children.Add(Line(autonomous ? "Waiting for the loop's own validation." : "Waiting for your decision.", 10.5, FundPalette.Risk));
                    break;
                case "approved":
                    stack.Children.Add(Line("Approved — the validation (G1–G8, no holdout) starts when the backtest slot is free.", 10.5, FundPalette.Focus));
                    break;
                case "validating":
                    stack.Children.Add(Line("Validation running (G1–G8, no holdout) · run " + (f.ValidationRunId ?? "?")
                                            + (string.IsNullOrWhiteSpace(f.ValidationStatus) ? "" : " · " + f.ValidationStatus), 10.5, FundPalette.Focus, mono: true));
                    break;
                case "passed_gates":
                    stack.Children.Add(Line("Passed the gates G1–G8 (validation run, no holdout). The loop did not change the strategy's lifecycle state"
                                            + (f.EligibleState is { Length: > 0 } es
                                                ? " — it is now eligible for \"" + StrategyStates.Label(es) + "\": move it there yourself in the Library."
                                                : ".")
                                            + " This is NOT \"Validated\": that needs the holdout, which only you open in the Library.", 10.5, FundPalette.Long));
                    break;
                case "failed_gates":
                    stack.Children.Add(Line("Validation done — did not pass the gates.", 10.5, FundPalette.Short));
                    break;
                case "validation_failed":
                case "validation_cancelled":
                case "validation_start_failed":
                    stack.Children.Add(Line(FinalistStateLabel(f.State) + (f.ValidationRunId != null ? " · run " + f.ValidationRunId : "")
                                            + (string.IsNullOrWhiteSpace(f.ValidationError) ? "." : ": " + f.ValidationError), 10.5, FundPalette.Kill));
                    break;
                case "rejected":
                    stack.Children.Add(Line("Discarded by you" + (string.IsNullOrWhiteSpace(f.RejectReason) ? "." : ": " + f.RejectReason), 10.5, FundPalette.Dim));
                    break;
            }
            if (f.Passed is bool passed && f.State != "passed_gates" && f.State != "failed_gates")
                stack.Children.Add(Line("Gates passed: " + (passed ? "yes" : "no"), 10.5, passed ? FundPalette.Long : FundPalette.Short));
            if (!string.IsNullOrWhiteSpace(f.DecidedAt))
                stack.Children.Add(Line("Decided " + FundFormat.LocalTime(f.DecidedAt, "yyyy-MM-dd HH:mm"), 10, FundPalette.Faint, mono: true));

            var row = new WrapPanel { Margin = new Thickness(0, 2, 0, 0) };
            var loopId = d.Id ?? "";
            var variantId = f.VariantId ?? "";
            if (f.State == "proposed" && !autonomous && loopId.Length > 0 && variantId.Length > 0)
            {
                var approve = SmallButton("Approve → validate…", "Approve finalist " + name + " and validate it");
                approve.Background = FundPalette.Make(0x267C8CF8);
                approve.BorderBrush = FundPalette.Focus;
                approve.Foreground = FundPalette.TextBright;
                approve.ToolTip = "Submits a validation run (gates G1–G8, no holdout) for this finalist when the backtest slot is free. No AI is used.";
                approve.Click += async (_, _) => await ApproveAsync(loopId, variantId, name, approve);
                row.Children.Add(approve);
                var reject = SmallButton("Discard…", "Discard finalist " + name);
                reject.ToolTip = "Discards this finalist (reason optional). The strategy stays a draft in the Library.";
                reject.Click += async (_, _) => await RejectAsync(loopId, variantId, name, reject);
                row.Children.Add(reject);
            }
            AddReportButtons(row, f.StrategyId, name, f.RunId, f.ValidationRunId);
            stack.Children.Add(row);
            var card = new Border
            {
                Background = FundPalette.WindowBg,
                BorderBrush = FinalistStateBrush(f.State),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 7, 10, 8),
                Margin = new Thickness(0, 6, 0, 2),
                Child = stack,
            };
            AutomationProperties.SetName(card, "Finalist " + name + ", " + FinalistStateLabel(f.State) + ", score " + LoopText.Score(f.Score));
            return card;
        }

        private void AddReportButtons(Panel row, string? strategyId, string? name, string? runId, string? validationRunId)
        {
            if (!string.IsNullOrWhiteSpace(runId))
            {
                string rid = runId!;
                var open = SmallButton("Open report", "Open the backtest report of run " + rid);
                open.Click += (_, _) => BacktestReportWindow.Open(Window.GetWindow(this), strategyId, name, rid);
                row.Children.Add(open);
            }
            if (!string.IsNullOrWhiteSpace(validationRunId))
            {
                string vrid = validationRunId!;
                var openV = SmallButton("Open validation report", "Open the validation report of run " + vrid);
                openV.Click += (_, _) => ValidationReportWindow.Open(Window.GetWindow(this), strategyId, name, vrid);
                row.Children.Add(openV);
            }
            if (!string.IsNullOrWhiteSpace(strategyId))
            {
                string sid = strategyId!;
                var lib = SmallButton("Open in Library", "Open strategy " + (name ?? sid) + " in the Library");
                lib.Click += (_, _) => OpenInLibraryRequested?.Invoke(sid);
                row.Children.Add(lib);
            }
        }

        private static string FinalistStateLabel(string? state) => state switch
        {
            "proposed" => "Proposed",
            "approved" => "Approved",
            "rejected" => "Discarded",
            "validating" => "Validating",
            "passed_gates" => "Passed G1–G8 → eligible for candidate",
            "failed_gates" => "Failed the gates",
            "validation_failed" => "Validation failed",
            "validation_cancelled" => "Validation cancelled",
            "validation_start_failed" => "Validation could not start",
            null or "" => "State not reported",
            _ => state!,
        };

        private static Brush FinalistStateBrush(string? state) => state switch
        {
            "proposed" => FundPalette.Risk,
            "approved" or "validating" => FundPalette.Focus,
            "passed_gates" => FundPalette.Long,
            "failed_gates" => FundPalette.Short,
            "validation_failed" or "validation_cancelled" or "validation_start_failed" => FundPalette.Kill,
            _ => FundPalette.PanelBorder,
        };

        // ----- generation × slot grid -----

        private FrameworkElement GridPanel(LoopDetail d, List<LoopGeneration> gens)
        {
            // DockPanel + ScrollViewer: in full screen the panel has a finite height and the grid scrolls; in the body it sizes to content
            var stack = new DockPanel { LastChildFill = true };
            var heading = FundUi.Text("Generations × variants", 12.5, FundPalette.TextBright, bold: true);
            heading.VerticalAlignment = VerticalAlignment.Center;
            var headRow = FundUi.HeadingWithFullScreen(heading, FundUi.FullScreenButton(_gridHost, "Loop population", PopulationHeader, registerF11: false));
            DockPanel.SetDock(headRow, Dock.Top);
            stack.Children.Add(headRow);
            var slots = Math.Max(d.Config?.Variants ?? 0, gens.SelectMany(g => g.Variants ?? new List<LoopVariant>()).Select(v => v.Slot ?? 0).DefaultIfEmpty(0).Max());
            var grid = new Grid { Margin = new Thickness(0, 6, 0, 4) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(40) });
            for (var s = 1; s <= slots; s++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star), MinWidth = 64 });
            var header = new RowDefinition { Height = GridLength.Auto };
            grid.RowDefinitions.Add(header);
            for (var s = 1; s <= slots; s++)
            {
                var h = FundUi.Text("Slot " + s.ToString(Inv), 10, FundPalette.Faint, wrap: false);
                h.HorizontalAlignment = HorizontalAlignment.Center;
                Grid.SetColumn(h, s);
                grid.Children.Add(h);
            }
            var row = 1;
            foreach (var g in gens)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var gl = FundUi.Text("G" + (g.Generation ?? 0).ToString(Inv), 11, FundPalette.Text, bold: true, wrap: false);
                gl.VerticalAlignment = VerticalAlignment.Center;
                Grid.SetRow(gl, row);
                grid.Children.Add(gl);
                foreach (var v in g.Variants ?? new List<LoopVariant>())
                {
                    if (v.Slot is not int slot || slot < 1 || slot > slots) continue;
                    var cell = GridCell(g, v);
                    Grid.SetRow(cell, row);
                    Grid.SetColumn(cell, slot);
                    grid.Children.Add(cell);
                }
                row++;
            }
            var legend = Line("★ kept (elite, carried over) · ↻ refined in the next generation · ✕ culled · ≡ duplicate (not eligible) · ! failed · … in progress · "
                                    + "score = Sharpe − penalties (deterministic, no AI). Enter / click a cell to open the variant.", 10, FundPalette.Faint);
            DockPanel.SetDock(legend, Dock.Bottom);
            stack.Children.Add(legend);
            stack.Children.Add(new ScrollViewer { Content = grid, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false });
            var panel = FundUi.Panel(stack, new Thickness(12, 10, 12, 10), new Thickness(0, 0, 0, 10));
            AutomationProperties.SetName(panel, "Grid of generations and variant slots");
            return panel;
        }

        /// <summary>Full-screen bar text of the population grid: seed, status, generations.</summary>
        private string PopulationHeader()
        {
            var d = _detail;
            if (d == null) return "no loop selected";
            return LoopText.SeedLabel(d.Seed, null, _strategyNames, 80) + " · " + LoopText.StatusLabel(d.Status)
                   + " · " + (d.Generations?.Count ?? 0).ToString(Inv) + " generation(s)";
        }

        private Button GridCell(LoopGeneration g, LoopVariant v)
        {
            var (glyph, brush, marker) = Marker(g, v);
            var score = BtJson.Num(v.Score, "score");
            var text = "#" + (v.Slot ?? 0).ToString(Inv) + " " + glyph + " " + (score is double sc ? LoopText.Score(sc) : StateShort(v.State));
            var b = FundUi.MakeButton(text, FundPalette.Make(0xFF1E1E1E), brush, brush);
            b.FontSize = 10.5;
            b.FontFamily = FundUi.Mono;
            b.Padding = new Thickness(4, 2, 4, 2);
            b.MinHeight = 22;
            b.Margin = new Thickness(2);
            var summary = VariantLabel(v.Id) + " · " + OriginShort(v) + " · " + StateLabel(v.State) + (score is double s2 ? " · score " + LoopText.Score(s2) : "")
                          + (marker.Length > 0 ? " · " + marker : "");
            b.ToolTip = summary;
            AutomationProperties.SetName(b, "Variant " + summary);
            var id = v.Id;
            var gen = g.Generation ?? 0;
            b.Click += (_, _) => FocusVariant(id, gen);
            return b;
        }

        /// <summary>Opens the variant's expander (and its generation) and scrolls it into view.</summary>
        private void FocusVariant(string? variantId, int generation)
        {
            if (variantId == null) return;
            if (FullScreenHost.IsActive(_gridHost)) FullScreenHost.ActiveFor(_gridHost)?.Exit();   // the variant opens in the body below
            _expanded["g:" + generation.ToString(Inv)] = true;
            _expanded["v:" + variantId] = true;
            RenderBody();
            Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(() =>
            {
                if (_variantElements.TryGetValue(variantId, out var el))
                {
                    el.BringIntoView();
                    el.Focus();
                }
            }));
        }

        /// <summary>Glyph + colour + text of a variant's selection marker in its generation (text always carries the meaning).</summary>
        private static (string Glyph, Brush Brush, string Text) Marker(LoopGeneration g, LoopVariant v)
        {
            var id = v.Id ?? "";
            var sel = g.Selection;
            if (sel is JsonElement s && s.ValueKind == JsonValueKind.Object)
            {
                if (BtJson.Get(s, "duplicates", id) != null) return ("≡", FundPalette.Risk, "duplicate (not eligible)");
                if (BtJson.Array(s, "elites").Any(e => StrategyJson.Scalar(e) == id)) return ("★", FundPalette.Long, "kept (elite)");
                if (BtJson.Array(s, "refine").Any(e => StrategyJson.Scalar(e) == id)) return ("↻", FundPalette.Focus, "refined in the next generation");
                if (BtJson.Array(s, "culled").Any(e => StrategyJson.Scalar(e) == id)) return ("✕", FundPalette.Short, "culled");
            }
            return v.State switch
            {
                "failed" => ("!", FundPalette.Kill, "failed"),
                "duplicate" => ("≡", FundPalette.Risk, "duplicate"),
                "scored" => ("•", FundPalette.Text, "scored, selection pending"),
                _ => ("…", FundPalette.Faint, ""),
            };
        }

        // ----- generations and variants -----

        private FrameworkElement GenerationElement(LoopGeneration g, bool latest)
        {
            var gen = g.Generation ?? 0;
            var variants = (g.Variants ?? new List<LoopVariant>()).OrderBy(v => v.Slot ?? 0).ToList();
            var sel = g.Selection;
            var hasSel = sel is JsonElement s0 && s0.ValueKind == JsonValueKind.Object;
            var header = "Generation " + gen.ToString(Inv) + " · " + variants.Count.ToString(Inv) + " variants · "
                         + (hasSel
                             ? BtJson.Array(sel, "elites").Count().ToString(Inv) + " kept · " + BtJson.Array(sel, "refine").Count().ToString(Inv) + " refined · "
                               + BtJson.Array(sel, "culled").Count().ToString(Inv) + " culled"
                             : "selection pending");
            var exp = MakeExpander(header, "Generation " + gen.ToString(Inv), "g:" + gen.ToString(Inv), latest, 12);
            var stack = new StackPanel { Margin = new Thickness(6, 4, 0, 4) };
            if (hasSel)
            {
                stack.Children.Add(Line("Selection: k = " + Int(BtJson.Num(sel, "k")) + " elites kept unchanged, c = " + Int(BtJson.Num(sel, "c"))
                                        + " culled (failed / unscorable / duplicates first, then the worst) and refilled with children of the elites; the rest are refined.",
                                        10.5, FundPalette.Faint));
            }
            foreach (var v in variants) stack.Children.Add(VariantElement(g, v));
            exp.Content = stack;
            return exp;
        }

        private FrameworkElement VariantElement(LoopGeneration g, LoopVariant v)
        {
            var (glyph, brush, marker) = Marker(g, v);
            var score = BtJson.Num(v.Score, "score");
            var headerPanel = new WrapPanel();
            var ht = FundUi.Text(VariantLabel(v.Id) + " · " + OriginShort(v) + " · " + (score is double sc ? "score " + LoopText.Score(sc) : "no score")
                                 + (BtJson.Num(v.Score, "sharpe") is double sh ? " · Sharpe " + FundFormat.Num(sh) : ""), 11, FundPalette.Text, mono: true, wrap: false);
            ht.Margin = new Thickness(0, 0, 8, 0);
            headerPanel.Children.Add(ht);
            headerPanel.Children.Add(Chip(StateLabel(v.State), StateBrush(v.State)));
            if (marker.Length > 0)
            {
                var mc = Chip(glyph + " " + marker, brush);
                mc.Margin = new Thickness(6, 0, 0, 0);
                headerPanel.Children.Add(mc);
            }
            var key = "v:" + (v.Id ?? "");
            var exp = new Expander
            {
                Header = headerPanel,
                Foreground = FundPalette.Text,
                Margin = new Thickness(0, 3, 0, 3),
                IsExpanded = _expanded.TryGetValue(key, out var open) && open,
            };
            exp.Expanded += (_, _) => _expanded[key] = true;
            exp.Collapsed += (_, _) => _expanded[key] = false;
            AutomationProperties.SetName(exp, "Variant " + VariantLabel(v.Id) + ", " + OriginShort(v) + ", " + StateLabel(v.State)
                                              + (score is double s3 ? ", score " + LoopText.Score(s3) : "") + (marker.Length > 0 ? ", " + marker : ""));
            if (v.Id != null) _variantElements[v.Id] = exp;

            var body = new StackPanel { Margin = new Thickness(18, 2, 0, 6) };
            body.Children.Add(Line((v.StrategyName ?? "(no strategy yet)") + (v.StrategyId != null ? " · " + v.StrategyId : ""), 11, FundPalette.TextBright));
            body.Children.Add(Line("Origin: " + OriginLong(v), 10.5, FundPalette.Dim));
            if (!string.IsNullOrWhiteSpace(v.Reason))
                body.Children.Add(Line(v.Reason!, 10.5, v.State == "failed" ? FundPalette.Kill : FundPalette.Risk));
            if (BtJson.Str(g.Selection, "reasons", v.Id ?? "") is { Length: > 0 } selReason)
                body.Children.Add(Line("Selection: " + selReason, 10.5, FundPalette.Dim));
            if (BtJson.Get(g.Selection, "duplicates", v.Id ?? "") is JsonElement dup)
                body.Children.Add(Line("Duplicate of " + VariantLabel(BtJson.Str(dup, "of")) + (BtJson.Num(dup, "correlation") is double corr ? " (daily-return correlation " + corr.ToString("0.00", Inv) + ")" : "")
                                       + " — not eligible as elite or finalist.", 10.5, FundPalette.Risk));
            if (v.KeyMetrics is JsonElement km && km.ValueKind == JsonValueKind.Object) body.Children.Add(MiniMetrics(km));
            body.Children.Add(VersionsBlock(v));
            body.Children.Add(ScoreBlock(v.Score));
            if (v.TopicCheck == "failed" && v.Diagnosis == null && v.Refinement == null)
                body.Children.Add(SystemNote("The AI texts of this variant did not pass the topic check — they are not shown."));
            if (v.Diagnosis != null) body.Children.Add(AiMessageElement(v, v.Diagnosis));
            if (v.Refinement != null) body.Children.Add(AiMessageElement(v, v.Refinement));
            var row = new WrapPanel { Margin = new Thickness(0, 4, 0, 0) };
            AddReportButtons(row, v.StrategyId, v.StrategyName, v.RunId, null);
            if (row.Children.Count > 0) body.Children.Add(row);
            exp.Content = body;
            return exp;
        }

        private static string StateShort(string? state) => state switch
        {
            "planned" => "planned",
            "ai" => "AI…",
            "saved" => "saved",
            "backtesting" => "testing",
            "scored" => "scored",
            "failed" => "failed",
            "duplicate" => "dup.",
            _ => state ?? "?",
        };

        private static string StateLabel(string? state) => state switch
        {
            "planned" => "planned",
            "ai" => AiContent.Glyph + " AI working",
            "saved" => "saved, waiting for its backtest",
            "backtesting" => "backtesting",
            "scored" => "scored",
            "failed" => "failed",
            "duplicate" => "duplicate",
            null or "" => "state not reported",
            _ => state!,
        };

        private static Brush StateBrush(string? state) => state switch
        {
            "ai" => FundPalette.Ai,
            "backtesting" => FundPalette.Focus,
            "scored" => FundPalette.Long,
            "failed" => FundPalette.Kill,
            "duplicate" => FundPalette.Risk,
            _ => FundPalette.Dim,
        };

        private string OriginShort(LoopVariant v) => v.Origin switch
        {
            "seed" => "seed",
            "mutation" => "mutation",
            "refined" => "refined",
            "child" => "child",
            "elite" => "elite (carried)",
            null or "" => "origin ?",
            _ => v.Origin!,
        };

        private string OriginLong(LoopVariant v)
        {
            var parent = VariantLabel(v.ParentVariantId);
            return v.Origin switch
            {
                "seed" => "the seed itself (an existing done backtest of it is reused when it matches — no new trial).",
                "mutation" => "mutation of the seed — an AI refinement told which aspect to vary" + (AspectOf(v) is { Length: > 0 } a ? " (" + a + ")" : "") + "; a new strategy in the seed's family.",
                "refined" => "refined from " + parent + " — AI diagnosis + refinement, a new version of the same loop strategy.",
                "child" => "child of the elite " + parent + " — AI diagnosis + refinement into a new strategy (same family).",
                "elite" => "elite carried over unchanged from " + VariantLabel(v.CarriedFrom) + " — no new backtest, same score.",
                _ => (v.Origin ?? "not reported") + (v.ParentVariantId != null ? " (parent " + parent + ")" : ""),
            };
        }

        private static string? AspectOf(LoopVariant v)
            => v.Refinement?.Output is JsonElement o && o.ValueKind == JsonValueKind.Object ? BtJson.Str(o, "aspect") : null;

        /// <summary>"g2·3" for a variant id (the id itself when unknown).</summary>
        private string VariantLabel(string? id)
        {
            if (string.IsNullOrEmpty(id)) return "—";
            return _variantsById.TryGetValue(id, out var v) ? "g" + (v.Generation ?? 0).ToString(Inv) + "·" + (v.Slot ?? 0).ToString(Inv) : id!;
        }

        private static FrameworkElement MiniMetrics(JsonElement km)
        {
            var text = "Sharpe " + FundFormat.Num(BtJson.Num(km, "sharpe"))
                       + " · Return " + FundFormat.Fraction(BtJson.Num(km, "total_return"), 1, signed: true)
                       + " · CAGR " + FundFormat.Fraction(BtJson.Num(km, "cagr"), 1, signed: true)
                       + " · Max DD " + FundFormat.Fraction(BtJson.Num(km, "max_drawdown"));
            var t = Line(text, 11, FundPalette.Text, mono: true);
            AutomationProperties.SetName(t, "Key metrics: " + text);
            return t;
        }

        private FrameworkElement VersionsBlock(LoopVariant v)
        {
            var stack = new StackPanel { Margin = new Thickness(0, 4, 0, 2) };
            var versions = v.Versions ?? new List<LoopVariantVersion>();
            stack.Children.Add(FundUi.Text("Versions and runs" + (versions.Count > 1 ? " (base + bounded parameter search; the best score is chosen)" : ""), 10.5, FundPalette.Text, bold: true));
            if (versions.Count == 0) stack.Children.Add(Line("No version saved yet.", 10.5, FundPalette.Faint));
            foreach (var x in versions)
            {
                var chosen = x.Version != null && x.Version == v.ChosenVersion;
                var what = x.Kind == "search"
                    ? "search " + (x.Parameter ?? "?") + " = " + (x.Value is JsonElement val ? FundFormat.Plain(val) : "?")
                    : (x.Kind ?? "?");
                var text = "v" + (x.Version?.ToString(Inv) ?? "?") + " · " + what
                           + " · run " + (x.RunId ?? "—") + (x.RunStatus != null ? " " + x.RunStatus : "")
                           + " · score " + LoopText.Score(x.Score)
                           + (chosen ? " · chosen" : "")
                           + (x.Reused == true ? " · reused existing run (no new trial)" : "");
                var line = new DockPanel { Margin = new Thickness(0, 1, 0, 1) };
                if (!string.IsNullOrWhiteSpace(x.RunId))
                {
                    string rid = x.RunId!;
                    var open = SmallButton("Report", "Open the backtest report of run " + rid);
                    open.Margin = new Thickness(8, 0, 0, 0);
                    open.MinHeight = 18;
                    open.Padding = new Thickness(6, 0, 6, 0);
                    open.FontSize = 10;
                    var sid = v.StrategyId;
                    var sname = v.StrategyName;
                    open.Click += (_, _) => BacktestReportWindow.Open(Window.GetWindow(this), sid, sname, rid);
                    DockPanel.SetDock(open, Dock.Right);
                    line.Children.Add(open);
                }
                var tb = FundUi.Text(text, 10.5, chosen ? FundPalette.TextBright : FundPalette.Dim, mono: true);
                line.Children.Add(tb);
                stack.Children.Add(line);
                if (!string.IsNullOrWhiteSpace(x.Error)) stack.Children.Add(Line("  " + x.Error, 10.5, FundPalette.Kill));
            }
            return stack;
        }

        private static FrameworkElement ScoreBlock(JsonElement? score)
        {
            var stack = new StackPanel { Margin = new Thickness(0, 4, 0, 2) };
            stack.Children.Add(FundUi.Text("Score card (deterministic, from the run card — no AI)", 10.5, FundPalette.Text, bold: true));
            if (score is not JsonElement s || s.ValueKind != JsonValueKind.Object)
            {
                stack.Children.Add(Line("Not scored yet.", 10.5, FundPalette.Faint));
                return stack;
            }
            var value = BtJson.Num(s, "score");
            if (value == null)
                stack.Children.Add(Line("No score: " + (BtJson.Str(s, "reason") ?? "the backend gave no reason."), 10.5, FundPalette.Risk));
            else
                stack.Children.Add(Line("Score " + LoopText.Score(value) + " = Sharpe " + LoopText.Score(BtJson.Num(s, "sharpe"))
                                        + " − turnover " + LoopText.Score(BtJson.Num(s, "penalties", "turnover"))
                                        + " − drawdown " + LoopText.Score(BtJson.Num(s, "penalties", "drawdown"))
                                        + " − costs " + LoopText.Score(BtJson.Num(s, "penalties", "costs"))
                                        + " − checks " + LoopText.Score(BtJson.Num(s, "penalties", "checks")), 10.5, FundPalette.Text, mono: true));
            var inputs = BtJson.Get(s, "inputs");
            if (inputs != null)
                stack.Children.Add(Line("Inputs: turnover " + FundFormat.Num(BtJson.Num(inputs, "turnover_annual"), 1) + "×/yr · max DD "
                                        + FundFormat.Fraction(BtJson.Num(inputs, "max_drawdown")) + " · costs " + FundFormat.Pct(BtJson.Num(inputs, "cost_share_pct"))
                                        + " of gross P&L · return " + FundFormat.Fraction(BtJson.Num(inputs, "total_return"), 1, signed: true)
                                        + " · CAGR " + FundFormat.Fraction(BtJson.Num(inputs, "cagr"), 1, signed: true)
                                        + " · " + Int(BtJson.Num(inputs, "trades_closed")) + " closed trades", 10.5, FundPalette.Dim, mono: true));
            foreach (var c in BtJson.Array(s, "checks"))
            {
                var st = BtJson.Str(c, "status") ?? "?";
                var label = st == "pass" ? "✓ pass" : st == "fail" ? "✕ fail" : st == "n/a" ? "– n/a" : st;
                stack.Children.Add(Line("Pre-check " + (BtJson.Str(c, "key") ?? "?") + ": " + label + " — " + (BtJson.Str(c, "explanation") ?? ""), 10.5,
                                        st == "pass" ? FundPalette.Long : st == "fail" ? FundPalette.Short : FundPalette.Dim));
            }
            if (BtJson.Bool(s, "checks_passed") is bool ok)
                stack.Children.Add(Line(ok ? "All pre-checks passed (needed for a finalist)." : "Not all pre-checks passed — cannot become a finalist.", 10.5, ok ? FundPalette.Long : FundPalette.Dim));
            if (BtJson.Get(s, "range") is JsonElement r)
                stack.Children.Add(Line("Evaluated range " + (BtJson.Str(r, "start") ?? "?") + " … " + (BtJson.Str(r, "end") ?? "?") + " (training data, before the holdout window)", 10, FundPalette.Faint, mono: true));
            foreach (var n in BtJson.Array(s, "notes"))
                if (StrategyJson.Scalar(n) is { Length: > 0 } nt) stack.Children.Add(Line("Note: " + nt, 10, FundPalette.Faint));
            return stack;
        }

        // ----- AI texts (diagnosis / refinement) -----

        private FrameworkElement AiMessageElement(LoopVariant v, LoopAiMessage m)
        {
            var role = m.Role == "diagnosis" ? "diagnosis" : m.Role == "refinement" ? "refinement" : (m.Role ?? "message");
            var output = m.Output is JsonElement o && o.ValueKind == JsonValueKind.Object ? o : (JsonElement?)null;
            if (output == null)
            {
                if (m.Pending == true && m.Discarded != true)
                {
                    // written but not yet topic-checked: labelled as AI from the first exposure, text not shown yet
                    var stack = new StackPanel();
                    stack.Children.Add(FundUi.Text(AiContent.Glyph + " AI-GENERATED · " + role + " · checking… (independent topic check pending)", 10.5, FundPalette.Ai, bold: true, mono: true));
                    stack.Children.Add(Line(CostLine(m), 10, FundPalette.Faint, mono: true));
                    var b = AiBorder(stack);
                    AutomationProperties.SetName(b, AiContent.LoadingAutomationName(role));
                    return b;
                }
                if (m.Discarded == true || m.TopicCheck == "failed")
                {
                    var text = m.DiscardedReason == "topic_check_failed" || m.TopicCheck == "failed"
                        ? "The AI texts of this variant did not pass the topic check — they are not shown."
                        : "AI " + role + " discarded (" + DiscardedLabel(m.DiscardedReason) + ") — not shown.";
                    return SystemNote(text + "\n" + CostLine(m));
                }
                return SystemNote("AI " + role + ": nothing to show (topic check " + (m.TopicCheck ?? "not reported") + ").\n" + CostLine(m));
            }
            if (m.Discarded == true)
            {
                // draft_invalid: ONLY the validator card — never the AI's text
                var stack = new StackPanel();
                stack.Children.Add(FundUi.Text("AI " + role + " discarded (" + DiscardedLabel(m.DiscardedReason) + ") — the AI's text is not shown; nothing was saved or backtested.", 10.5, FundPalette.Dim));
                stack.Children.Add(Line(CostLine(m), 10, FundPalette.Faint, mono: true));
                AddValidation(stack, BtJson.Get(output, "validation"));
                return NeutralBorder(stack, "Discarded AI " + role);
            }
            return m.Role == "diagnosis" ? DiagnosisElement(m, output.Value) : RefinementElement(v, m, output.Value);
        }

        private FrameworkElement DiagnosisElement(LoopAiMessage m, JsonElement o)
        {
            FundAi.TryRead(m.Provenance, null, m.CreatedAt, out var model, out var at);
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text(AiContent.Badge(model, at) + " · diagnosis", 10.5, FundPalette.Ai, bold: true, mono: true));
            stack.Children.Add(Line(CostLine(m), 10, FundPalette.Faint, mono: true));
            var marks = Unverified(m.Check);
            stack.Children.Add(Marked(BtJson.Str(o, "summary") ?? "(no summary)", marks, "summary", 11.5, FundPalette.Text, ""));
            var i = 0;
            var weak = BtJson.Array(o, "weaknesses").ToList();
            if (weak.Count > 0) stack.Children.Add(SubHead("Weaknesses"));
            foreach (var w in weak) stack.Children.Add(Marked(StrategyJson.Scalar(w), marks, "weaknesses." + (i++).ToString(Inv), 11, FundPalette.Dim, "• "));
            i = 0;
            var ideas = BtJson.Array(o, "ideas").ToList();
            if (ideas.Count > 0) stack.Children.Add(SubHead("Ideas"));
            foreach (var idea in ideas) stack.Children.Add(Marked(StrategyJson.Scalar(idea), marks, "ideas." + (i++).ToString(Inv), 11, FundPalette.Dim, "• "));
            if (marks.Count > 0)
                stack.Children.Add(Line(marks.Count.ToString(Inv) + " number" + (marks.Count == 1 ? "" : "s") + " could not be verified against the computed facts — marked red "
                                        + "(shown, not removed): " + string.Join(", ", marks.Select(x => x.Text)), 10.5, FundPalette.Kill));
            var unknownIds = BtJson.Array(m.Check, "unknown_ids").Select(x => StrategyJson.Scalar(x)).Where(x => x.Length > 0).ToList();
            if (unknownIds.Count > 0) stack.Children.Add(Line("Fact ids the AI cited that do not exist: " + string.Join(", ", unknownIds), 10.5, FundPalette.Kill));
            AddCaveat(stack);
            var b = AiBorder(stack);
            AutomationProperties.SetName(b, AiContent.AutomationName(model, at) + " Loop diagnosis: " + (BtJson.Str(o, "summary") ?? ""));
            return b;
        }

        private FrameworkElement RefinementElement(LoopVariant v, LoopAiMessage m, JsonElement o)
        {
            FundAi.TryRead(m.Provenance, null, m.CreatedAt, out var model, out var at);
            var stack = new StackPanel();
            var task = BtJson.Str(o, "task");
            stack.Children.Add(FundUi.Text(AiContent.Badge(model, at) + " · refinement" + (task != null ? " (" + task + ")" : ""), 10.5, FundPalette.Ai, bold: true, mono: true));
            stack.Children.Add(Line(CostLine(m), 10, FundPalette.Faint, mono: true));
            if (BtJson.Str(o, "aspect") is { Length: > 0 } aspect) stack.Children.Add(Line("Aspect to vary (fixed loop text): " + aspect, 10.5, FundPalette.Dim));
            var reply = FundUi.Text(BtJson.Str(o, "reply") is { Length: > 0 } r ? r : "(no reply text)", 11.5, FundPalette.Text);
            reply.LineHeight = 17;
            reply.Margin = new Thickness(0, 4, 0, 4);
            stack.Children.Add(reply);
            if (BtJson.Bool(o, "repaired") == true)
                stack.Children.Add(Line("The first answer was invalid; the validator's errors were sent back once (repair round, charged).", 10.5, FundPalette.Dim));
            AddValidation(stack, BtJson.Get(o, "validation"));
            var repairErrors = BtJson.Array(o, "repair_errors").ToList();
            if (repairErrors.Count > 0)
            {
                var exp = MakeExpander("Errors of the first answer (" + repairErrors.Count.ToString(Inv) + ")", "Validator errors of the first answer, before the repair round",
                                       "re:" + (m.Id ?? ""), false, 10.5);
                var list = new StackPanel();
                foreach (var e in repairErrors) list.Children.Add(IssueLine("error", e, FundPalette.Dim));
                exp.Content = list;
                stack.Children.Add(exp);
            }
            AddOperatorRequests(stack, o);
            var assumptions = BtJson.Array(o, "assumptions").Select(x => StrategyJson.Scalar(x)).Where(x => x.Length > 0).ToList();
            if (assumptions.Count > 0)
            {
                stack.Children.Add(SubHead("Assumptions of the AI"));
                foreach (var a in assumptions) stack.Children.Add(Line("• " + a, 10.5, FundPalette.Dim));
            }
            var proposed = BtJson.Get(o, "spec");
            stack.Children.Add(DiffElement(v, m, proposed));
            if (proposed is JsonElement spec && spec.ValueKind == JsonValueKind.Object)
            {
                var full = MakeExpander("Proposed spec (JSON, as returned by the AI)", "The full proposed strategy spec as JSON, as returned by the AI", "spec:" + (m.Id ?? ""), false, 10.5);
                full.Content = FundUi.Text(JsonSerializer.Serialize(spec, new JsonSerializerOptions { WriteIndented = true }), 10.5, FundPalette.Dim, mono: true);
                stack.Children.Add(full);
            }
            AddCaveat(stack);
            var b = AiBorder(stack);
            AutomationProperties.SetName(b, AiContent.AutomationName(model, at) + " Loop refinement: " + (BtJson.Str(o, "reply") ?? ""));
            return b;
        }

        /// <summary>The spec diff of a refined/mutated/child variant: parent (stored, chosen version) vs the variant's stored base
        /// version — or vs the AI's proposed spec when the variant has no stored version. Loaded when the expander opens.</summary>
        private FrameworkElement DiffElement(LoopVariant v, LoopAiMessage m, JsonElement? proposed)
        {
            var parent = v.ParentVariantId != null && _variantsById.TryGetValue(v.ParentVariantId, out var pv) ? pv : null;
            if (parent?.StrategyId == null || parent.ChosenVersion is not int pver)
                return Line(v.ParentVariantId == null ? "No parent variant — nothing to compare." : "The parent variant has no chosen stored version to compare with.", 10.5, FundPalette.Faint);
            var baseVersion = (v.Versions ?? new List<LoopVariantVersion>()).FirstOrDefault(x => x.Kind == "base")?.Version;
            var storedStrategy = baseVersion != null ? v.StrategyId : null;
            var storedVersion = storedStrategy != null ? baseVersion : null;
            if (storedStrategy == null && proposed is not JsonElement { ValueKind: JsonValueKind.Object })
                return Line("No stored version and no proposed spec to compare with the parent.", 10.5, FundPalette.Faint);
            var title = "Spec changes: " + (storedVersion is int sv ? "v" + sv.ToString(Inv) : "proposed spec (not saved)") + " vs parent "
                        + VariantLabel(parent.Id) + " v" + pver.ToString(Inv);
            var key = "diff:" + (m.Id ?? v.Id ?? "");
            var exp = MakeExpander(title, "Changes of the spec against the parent variant " + VariantLabel(parent.Id), key, false, 10.5);
            var list = new StackPanel();
            list.Children.Add(Line("Open to load the stored versions.", 10.5, FundPalette.Faint));
            exp.Content = list;
            var loading = false;
            var loaded = false;
            var parentStrategy = parent.StrategyId!;
            async Task LoadAsync()
            {
                if (loading || loaded) return;
                loading = true;
                list.Children.Clear();
                list.Children.Add(Line("Loading…", 10.5, FundPalette.Faint));
                try
                {
                    var before = await StoredSpecAsync(parentStrategy, pver);
                    JsonElement? after = proposed;
                    var error = before.Error;
                    if (error == null && storedStrategy != null && storedVersion is int ver)
                    {
                        var stored = await StoredSpecAsync(storedStrategy, ver);
                        after = stored.Spec;
                        error = stored.Error;
                    }
                    list.Children.Clear();
                    if (error != null || before.Spec is not JsonElement b || after is not JsonElement a)
                    {
                        list.Children.Add(Line(error ?? "The stored version has no spec.", 10.5, FundPalette.Kill));
                        return;
                    }
                    loaded = true;
                    var diff = StudioView.SpecDiff(b, a);
                    if (exp.Header is TextBlock h) h.Text = title + " (" + diff.Count.ToString(Inv) + ")";
                    if (diff.Count == 0) list.Children.Add(Line("No differences.", 10.5, FundPalette.Faint));
                    foreach (var (sign, text) in diff.Take(MaxDiffLines))
                        list.Children.Add(Line(sign + " " + text, 10.5, sign == "+" ? FundPalette.Long : sign == "−" ? FundPalette.Short : FundPalette.Risk, mono: true));
                    if (diff.Count > MaxDiffLines) list.Children.Add(Line("… " + (diff.Count - MaxDiffLines).ToString(Inv) + " more", 10.5, FundPalette.Faint));
                }
                finally
                {
                    loading = false;
                }
            }
            exp.Expanded += async (_, _) => await LoadAsync();
            if (exp.IsExpanded) _ = LoadAsync();   // re-rendered while open: load again (cached)
            return exp;
        }

        private async Task<(JsonElement? Spec, string? Error)> StoredSpecAsync(string strategyId, int version)
        {
            var key = strategyId + "@" + version.ToString(Inv);
            if (!_versionCache.TryGetValue(key, out var task))
            {
                task = PlatformClient.GetStrategyVersionAsync(strategyId, version);
                _versionCache[key] = task;
            }
            var res = await task;
            if (!res.Ok || res.Value == null)
            {
                _versionCache.Remove(key);
                return (null, res.Ok ? "Unexpected response from the backend (strategy version " + version.ToString(Inv) + ")." : res.Message);
            }
            return (res.Value.Spec, null);
        }

        private static void AddValidation(StackPanel stack, JsonElement? validation)
        {
            if (validation is not JsonElement val || val.ValueKind != JsonValueKind.Object)
            {
                stack.Children.Add(Line("The backend sent no validator result.", 10.5, FundPalette.Risk));
                return;
            }
            var errors = BtJson.Array(val, "errors").ToList();
            var warnings = BtJson.Array(val, "warnings").ToList();
            if (errors.Count == 0)
                stack.Children.Add(Line("Validator: ok" + (warnings.Count > 0 ? " · " + warnings.Count.ToString(Inv) + " warning(s)" : ""), 10.5, FundPalette.Long));
            foreach (var e in errors) stack.Children.Add(IssueLine("error", e, FundPalette.Kill));
            foreach (var w in warnings) stack.Children.Add(IssueLine("warning", w, FundPalette.Risk));
        }

        private static FrameworkElement IssueLine(string kind, JsonElement e, Brush brush)
        {
            string text;
            if (e.ValueKind == JsonValueKind.Object)
            {
                var path = BtJson.Str(e, "path");
                var code = BtJson.Str(e, "code");
                text = kind + (string.IsNullOrWhiteSpace(path) ? "" : " at " + path) + (string.IsNullOrWhiteSpace(code) ? "" : " [" + code + "]") + ": " + (BtJson.Str(e, "message") ?? "");
            }
            else text = kind + ": " + StrategyJson.Scalar(e);
            var t = Line(text, 10.5, brush, mono: true);
            AutomationProperties.SetName(t, "Validator " + text);
            return t;
        }

        private static void AddOperatorRequests(StackPanel stack, JsonElement o)
        {
            var requests = BtJson.Array(o, "operator_requests").ToList();
            if (requests.Count == 0) return;
            var label = BtJson.Str(o, "operator_requests_label") is { Length: > 0 } l ? l : "unverified AI suggestion";
            var inner = new StackPanel();
            inner.Children.Add(FundUi.Text("Operator requests — " + label + " (text only, never executed)", 10.5, FundPalette.Risk, bold: true));
            foreach (var r in requests)
            {
                inner.Children.Add(Line((BtJson.Str(r, "name") ?? "(no name)") + (BtJson.Str(r, "signature") is { Length: > 0 } sig ? " — " + sig : ""), 10.5, FundPalette.Text, mono: true));
                if (BtJson.Str(r, "purpose") is { Length: > 0 } purpose)
                {
                    var p = Line(purpose, 10.5, FundPalette.Dim);
                    p.Margin = new Thickness(12, 0, 0, 3);
                    inner.Children.Add(p);
                }
            }
            var box = new Border
            {
                BorderBrush = FundPalette.Risk,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 6, 0, 2),
                Child = inner,
            };
            AutomationProperties.SetName(box, "Operator requests, " + label + ", never executed: " + string.Join("; ", requests.Select(r => BtJson.Str(r, "name") ?? "")));
            stack.Children.Add(box);
        }

        /// <summary>{text, offset, section} entries of the §11.2 number check (diagnosis only).</summary>
        private static List<(string Text, int Offset, string Section)> Unverified(JsonElement? check)
        {
            var list = new List<(string, int, string)>();
            foreach (var u in BtJson.Array(check, "unverified"))
            {
                var text = BtJson.Str(u, "text");
                if (string.IsNullOrEmpty(text)) continue;
                var off = BtJson.Num(u, "offset") is double d ? (int)d : -1;
                list.Add((text!, off, BtJson.Str(u, "section") ?? ""));
            }
            return list;
        }

        /// <summary>Plain text with the unverified numbers of <paramref name="section"/> as red runs (only where the text at the offset
        /// matches; others stay listed below the diagnosis). Runs are plain text — nothing is parsed or executed.</summary>
        private static TextBlock Marked(string text, List<(string Text, int Offset, string Section)> marks, string section, double size, Brush brush, string prefix)
        {
            var tb = FundUi.Text("", size, brush);
            tb.Margin = new Thickness(0, 2, 0, 2);
            tb.LineHeight = size + 5.5;
            tb.Inlines.Clear();
            if (prefix.Length > 0) tb.Inlines.Add(new Run(prefix));
            var spans = marks.Where(x => x.Section == section && x.Offset >= 0 && x.Offset + x.Text.Length <= text.Length
                                         && string.CompareOrdinal(text, x.Offset, x.Text, 0, x.Text.Length) == 0)
                             .OrderBy(x => x.Offset).ToList();
            var pos = 0;
            foreach (var s in spans)
            {
                if (s.Offset < pos) continue;   // overlapping entry
                if (s.Offset > pos) tb.Inlines.Add(new Run(text.Substring(pos, s.Offset - pos)));
                tb.Inlines.Add(new Run(s.Text) { Foreground = FundPalette.Kill, TextDecorations = TextDecorations.Underline, ToolTip = "Unverified number — not found in the computed facts" });
                pos = s.Offset + s.Text.Length;
            }
            if (pos < text.Length) tb.Inlines.Add(new Run(text.Substring(pos)));
            AutomationProperties.SetName(tb, prefix + text + (spans.Count > 0 ? " (" + spans.Count.ToString(Inv) + " unverified numbers)" : ""));
            return tb;
        }

        private static string CostLine(LoopAiMessage m)
        {
            var parts = new List<string> { LoopText.TierLabel(BtJson.Str(m.Provenance, "model_tier")), "cost " + FundFormat.Usd(m.CostUsd ?? BtJson.Num(m.Usage, "cost_usd")) };
            if (BtJson.Num(m.Usage, "prompt_tokens") is double pt && BtJson.Num(m.Usage, "completion_tokens") is double ct)
                parts.Add(pt.ToString("N0", Inv) + " in / " + ct.ToString("N0", Inv) + " out tokens");
            if (BtJson.Num(m.Usage, "estimate_usd") is double est) parts.Add("estimate was " + FundFormat.Usd(est));
            if (!string.IsNullOrWhiteSpace(m.TopicCheck)) parts.Add("topic check " + m.TopicCheck);
            parts.Add("loop AI budget");
            return string.Join(" · ", parts);
        }

        private static string DiscardedLabel(string? reason) => reason switch
        {
            "topic_check_failed" => "it did not pass the topic check",
            "draft_invalid" => "the draft did not pass the validator",
            "bad_json" => "the AI's answer was not valid JSON",
            "off_topic" => "outside the loop's scope",
            "not_a_draft" => "the AI did not return a draft",
            "not_topic_checked" => "the variant failed before its topic check",
            null or "" => "no reason given",
            _ => reason!.Replace('_', ' '),
        };

        private static Border AiBorder(UIElement child) => new()
        {
            Background = FundPalette.AiSurface,
            BorderBrush = FundPalette.Ai,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(12, 8, 12, 10),
            Margin = new Thickness(0, 6, 0, 4),
            Child = child,
        };

        private static Border NeutralBorder(UIElement child, string name)
        {
            var b = new Border
            {
                Background = FundPalette.WindowBg,
                BorderBrush = FundPalette.Hairline,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 5, 10, 6),
                Margin = new Thickness(0, 6, 0, 4),
                Child = child,
            };
            AutomationProperties.SetName(b, name);
            return b;
        }

        /// <summary>Neutral system note (no AI label, no AI colours, fixed backend/app texts only).</summary>
        private static FrameworkElement SystemNote(string text)
        {
            var t = FundUi.Text(text, 10.5, FundPalette.Dim);
            return NeutralBorder(t, "System note: " + text);
        }

        private static void AddCaveat(StackPanel stack)
        {
            var caveat = FundUi.Text(AiContent.ResearchNotice, 10, FundPalette.Faint);   // Stage 10: research output notice (b)
            caveat.FontStyle = FontStyles.Italic;
            caveat.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(caveat);
        }

        // ----- activity log -----

        private FrameworkElement EventsExpander()
        {
            var exp = MakeExpander("Activity (" + _events.Count.ToString(Inv) + " events)", "Activity log of the loop, newest first", "events", false, 12);
            var list = new StackPanel { Margin = new Thickness(6, 4, 0, 4) };
            if (_events.Count == 0) list.Children.Add(Line("No events yet.", 10.5, FundPalette.Faint));
            foreach (var e in Enumerable.Reverse(_events).Take(MaxEventsShown))
            {
                var where = (e.Generation is int g ? " · g" + g.ToString(Inv) : "") + (e.VariantId != null ? " · " + VariantLabel(e.VariantId) : "");
                list.Children.Add(Line(FundFormat.LocalTime(e.CreatedAt, "HH:mm:ss") + " · " + (e.Event ?? "?") + where + EventData(e.Data), 10, FundPalette.Dim, mono: true));
            }
            if (_events.Count > MaxEventsShown) list.Children.Add(Line("… " + (_events.Count - MaxEventsShown).ToString(Inv) + " older events not shown", 10, FundPalette.Faint));
            exp.Content = list;
            return exp;
        }

        /// <summary>A few computed fields of an event's data (backend texts and ids only — events carry no AI text).</summary>
        private static string EventData(JsonElement? data)
        {
            var parts = new List<string>();
            foreach (var k in new[] { "message", "reason", "code", "note", "text", "role", "kind", "version", "run_id", "status", "parameter", "value", "cost_usd", "passed" })
            {
                var v = BtJson.Get(data, k);
                if (v is not JsonElement x || x.ValueKind == JsonValueKind.Object || x.ValueKind == JsonValueKind.Array) continue;
                var s = FundFormat.Plain(x);
                if (s.Length > 160) s = s[..160] + "…";
                parts.Add(k + " " + s);
            }
            if (BtJson.Num(data, "score", "score") is double sc) parts.Add("score " + LoopText.Score(sc));
            return parts.Count == 0 ? "" : " · " + string.Join(" · ", parts);
        }

        // =====================================================================================
        // Actions
        // =====================================================================================

        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        private Task NewLoopAsync() => DialogGuard.RunAsync("loop-new", () => NewLoopCoreAsync());

        private async Task NewLoopCoreAsync()
        {
            if (_busy || _stopped) return;
            _busy = true;
            _newLoop.IsEnabled = false;
            var old = _listStatus.Text;
            _listStatus.Text = "Loading strategies, papers and settings…";
            try
            {
                var strategiesTask = PlatformClient.GetStrategiesAsync();
                var papersTask = PlatformClient.ListPapersAsync(50);
                var settingsTask = PlatformClient.GetLoopSettingsAsync();
                await Task.WhenAll(strategiesTask, papersTask, settingsTask);
                if (_stopped) return;
                _listStatus.Text = old;
                var dialog = new LoopStartDialog(Window.GetWindow(this), strategiesTask.Result, papersTask.Result, settingsTask.Result,
                                                 _loopId != null && _detail?.Seed?.Kind == "strategy" ? _detail.Seed.StrategyId : null);
                if (dialog.ShowDialog() != true || dialog.StartResult == null) return;
                var res = dialog.StartResult;
                if (res.Ok && res.Value?.Id != null)
                {
                    await LoadListAsync();
                    await SelectLoopAsync(res.Value.Id!, force: true);
                    return;
                }
                if (res.Status == PlatformCallStatus.Conflict && PlatformClient.ReadConflictLoopId(res) is string running)
                {
                    if (FundDialogs.Confirm(Window.GetWindow(this), "Discovery loop", res.Message + "\n\nOne loop runs at a time. Show the running loop?", "Show running loop"))
                    {
                        await LoadListAsync();
                        await SelectLoopAsync(running, force: true);
                    }
                    return;
                }
                ShowError(res.Ok ? "Unexpected response from the backend (POST /v2/loops without an id) — backend and app versions may not match." : res.Message);
            }
            finally
            {
                if (_listStatus.Text == "Loading strategies, papers and settings…") _listStatus.Text = old;
                _busy = false;
                _newLoop.IsEnabled = true;
            }
        }

        private async Task OpenSettingsAsync()
        {
            if (_stopped) return;
            var dialog = new LoopSettingsDialog(Window.GetWindow(this));
            dialog.ShowDialog();
            await Task.CompletedTask;
        }

        private async Task LoopActionAsync(string action, string loopId, Button button)
        {
            if (loopId.Length == 0 || _busy) return;
            if (action == "cancel" && !FundDialogs.Confirm(Window.GetWindow(this), "Cancel discovery loop",
                    "Cancel this loop? It stops after the current step; its running backtest or validation is cancelled. "
                    + "An AI call in flight finishes (and is charged). Stored variants, runs and trials are kept; a cancelled loop cannot be resumed.",
                    "Cancel loop", danger: true))
                return;
            _busy = true;
            button.IsEnabled = false;
            _errorHost.Children.Clear();
            try
            {
                var res = action switch
                {
                    "pause" => await PlatformClient.PauseLoopAsync(loopId),
                    "resume" => await PlatformClient.ResumeLoopAsync(loopId),
                    _ => await PlatformClient.CancelLoopAsync(loopId),
                };
                if (_stopped) return;
                if (!res.Ok) ShowError(res.Message);
            }
            finally
            {
                _busy = false;
                button.IsEnabled = true;
            }
            if (_loopId == loopId) await LoadDetailAsync(loopId, quiet: true);
            await LoadListAsync();
        }

        private async Task ApproveAsync(string loopId, string variantId, string name, Button button)
        {
            if (_busy) return;
            if (!FundDialogs.Confirm(Window.GetWindow(this), "Approve finalist",
                    "Approve \"" + name + "\" and validate it?\n\nThe loop submits a validation run (walk-forward, gates G1–G8 — NOT the holdout) when the "
                    + "backtest slot is free. No AI is used. The strategy's lifecycle state does not change: if it passes, it is your candidate proposal — "
                    + "move it to Candidate yourself in the Library. The holdout is opened only by you, from the strategy's validation report.",
                    "Approve and validate"))
                return;
            _busy = true;
            button.IsEnabled = false;
            _errorHost.Children.Clear();
            try
            {
                var res = await PlatformClient.ApproveLoopFinalistAsync(loopId, variantId);
                if (_stopped) return;
                if (!res.Ok) ShowError(res.Message);
            }
            finally
            {
                _busy = false;
                button.IsEnabled = true;
            }
            if (_loopId == loopId) await LoadDetailAsync(loopId, quiet: true);
        }

        private async Task RejectAsync(string loopId, string variantId, string name, Button button)
        {
            if (_busy) return;
            var values = FundDialogs.Form(Window.GetWindow(this), "Discard finalist",
                "Discard \"" + name + "\"? The loop does not run its gate validation; the strategy stays a draft in the Library.",
                new[] { ("Reason (optional, up to " + PlatformClient.LoopRejectReasonMaxChars.ToString(Inv) + " characters)", "") }, "Discard finalist", danger: true);
            if (values == null) return;
            var reason = values.Length > 0 ? values[0].Trim() : "";
            if (reason.Length > PlatformClient.LoopRejectReasonMaxChars)
            {
                ShowError("The reason is longer than " + PlatformClient.LoopRejectReasonMaxChars.ToString(Inv) + " characters — nothing was changed.");
                return;
            }
            _busy = true;
            button.IsEnabled = false;
            _errorHost.Children.Clear();
            try
            {
                var res = await PlatformClient.RejectLoopFinalistAsync(loopId, variantId, reason.Length > 0 ? reason : null);
                if (_stopped) return;
                if (!res.Ok) ShowError(res.Message);
            }
            finally
            {
                _busy = false;
                button.IsEnabled = true;
            }
            if (_loopId == loopId) await LoadDetailAsync(loopId, quiet: true);
        }

        // =====================================================================================
        // Small builders
        // =====================================================================================

        private void ApplyNotice(string? text)
        {
            if (text == null) return;   // keep the last notice the backend sent (fixed text)
            StudioView.SetNotice(_notice, text);
        }

        private void ShowError(string message)
        {
            _errorHost.Children.Clear();
            var block = ErrorBlock(message);
            block.Margin = new Thickness(0, 6, 0, 0);
            AutomationProperties.SetLiveSetting(block, AutomationLiveSetting.Assertive);
            _errorHost.Children.Add(block);
        }

        private Expander MakeExpander(string header, string name, string key, bool defaultExpanded, double size)
        {
            var h = FundUi.Text(header, size, FundPalette.Text, bold: true, wrap: false);
            var exp = new Expander
            {
                Header = h,
                Foreground = FundPalette.Text,
                Margin = new Thickness(0, 4, 0, 2),
                IsExpanded = _expanded.TryGetValue(key, out var open) ? open : defaultExpanded,
            };
            exp.Expanded += (_, _) => _expanded[key] = true;
            exp.Collapsed += (_, _) => _expanded[key] = false;
            AutomationProperties.SetName(exp, name);
            return exp;
        }

        private static TextBlock SubHead(string text)
        {
            var t = FundUi.Text(text, 10.5, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 6, 0, 2);
            return t;
        }

        private static void Place(Grid grid, UIElement element, int row)
        {
            Grid.SetRow(element, row);
            grid.Children.Add(element);
        }

        private static TextBlock Line(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
            return t;
        }

        private static string Int(double? v) => v is double x ? x.ToString("0", Inv) : "—";

        private static Border Chip(string text, Brush brush) => new()
        {
            BorderBrush = brush,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(6, 0, 6, 1),
            VerticalAlignment = VerticalAlignment.Center,
            Child = FundUi.Text(text, 9.5, brush, bold: true, wrap: false),
        };

        private static Button SmallButton(string text, string name)
        {
            var b = FundUi.MakeButton(text);
            b.Padding = new Thickness(10, 2, 10, 2);
            b.MinHeight = 22;
            b.FontSize = 11;
            b.Margin = new Thickness(0, 4, 8, 0);
            b.HorizontalAlignment = HorizontalAlignment.Left;
            AutomationProperties.SetName(b, name);
            return b;
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
    }
}

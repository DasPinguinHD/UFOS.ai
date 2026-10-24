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
    /// Hedge Fund → "Studio" (Strategy Platform stage 5a, SPEC §13.3; routes/JSON §13.5). Left: Studio sessions
    /// (one session = one strategy family) and "+ New session". Right: the chat of the selected session — user
    /// bubbles, assistant replies in the violet AI container with the EU AI Act header "✦ AI-generated · model ·
    /// time" and a cost line, backend notes — plus the draft card of every assistant message that carried a spec:
    /// validator result (errors/warnings, repair round), spec changes vs the previous version (computed here from the
    /// two specs), "Operator requests — unverified AI suggestion" (text only, never executed), assumptions, the
    /// family's trial count, the automatic backtest (waiting for the backtest slot + Cancel / running + Cancel /
    /// mini metrics + "Open report") and "Open in Library"; "Memory used for this draft" lists the computed facts the
    /// prompt contained (no hidden context). Composer: Standard / Premium with the backend's pre-call estimate, Send
    /// (Ctrl+Enter), the ideation budget line (month/day). All texts are plain text (AI replies, strategy names and
    /// memory facts are untrusted data); backend errors (budget reached, AI off, no key, bad_json, busy, …) are shown
    /// verbatim; nothing is retried or replaced. The run status is polled every 3 s only while a draft backtest is
    /// waiting or running and the view is visible.
    /// </summary>
    internal sealed class StudioView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(3);
        private const int MaxDiffLines = 60;
        public const string ComposerPlaceholder = "Describe a trading idea — market, signal, rebalancing…";

        private readonly StackPanel _sessionsPanel;
        private readonly TextBlock _sessionsStatus;
        private readonly Button _newSession;
        private readonly Border _sessionsBorder;
        private readonly StackPanel _header;
        private readonly TextBlock _budgetText;
        private readonly ScrollViewer _chatScroll;
        private readonly StackPanel _chat;
        private readonly StackPanel _errorHost;
        private readonly TextBox _input;
        private readonly TextBlock _charCount;
        private readonly RadioButton _standard;
        private readonly RadioButton _premium;
        private readonly Button _send;
        private readonly TextBlock _sendStatus;
        private readonly DispatcherTimer _poll;
        private readonly DispatcherTimer _waitTimer;
        private readonly Dictionary<string, StackPanel> _statusHosts = new(StringComparer.Ordinal);
        private readonly Border _composerNotice;
        private readonly TextBlock _placeholder;
        private readonly Border _sessionNotice;
        private readonly Button _chatTab;
        private readonly Button _paperTab;
        private readonly Grid _chatRoot;
        private readonly Border _paperHost;
        private StudioPaperView? _paperView;
        // stored specs for the diff (strategy@version) — versions are immutable, so the cache never goes stale
        private readonly Dictionary<string, Task<PlatformResult<StrategyVersionDetail>>> _versionCache = new(StringComparer.Ordinal);

        private List<StudioSession> _sessions = new();
        private string? _sessionId;
        private StudioSessionDetail? _detail;
        private int _sessionSeq;
        private int _listSeq;
        private bool _busy;
        private bool _polling;
        private bool _stopped;
        private DateTime _waitStarted;

        /// <summary>"Open in Library" on a draft card (argument: strategy id) — HedgeFundWindow switches to the Library.</summary>
        public event Action<string>? OpenInLibraryRequested;

        public StudioView()
        {
            AutomationProperties.SetName(this, "Strategy Studio");
            var root = new Grid();
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(250) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            // ----- sessions -----
            var left = new DockPanel();
            var lHead = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            lHead.Children.Add(FundUi.Text("Studio sessions", 12, FundPalette.Text, bold: true));
            var refresh = FundUi.MakeButton("↻");
            refresh.Padding = new Thickness(8, 1, 8, 1);
            refresh.MinHeight = 20;
            refresh.FontSize = 11;
            refresh.HorizontalAlignment = HorizontalAlignment.Right;
            refresh.ToolTip = "Reload the sessions";
            AutomationProperties.SetName(refresh, "Reload the Studio sessions");
            refresh.Click += async (_, _) => await ActivateAsync();
            lHead.Children.Add(refresh);
            DockPanel.SetDock(lHead, Dock.Top);
            left.Children.Add(lHead);
            _newSession = FundUi.MakeButton("+ New session", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _newSession.HorizontalAlignment = HorizontalAlignment.Stretch;
            _newSession.Margin = new Thickness(0, 0, 4, 8);
            _newSession.ToolTip = "Start a new conversation — one session = one strategy family";
            AutomationProperties.SetName(_newSession, "New Studio session");
            _newSession.Click += async (_, _) => await NewSessionAsync();
            DockPanel.SetDock(_newSession, Dock.Top);
            left.Children.Add(_newSession);
            _sessionsStatus = FundUi.Text("", 10.5, FundPalette.Faint);
            _sessionsStatus.Margin = new Thickness(0, 0, 0, 6);
            DockPanel.SetDock(_sessionsStatus, Dock.Top);
            left.Children.Add(_sessionsStatus);
            _sessionsPanel = new StackPanel();
            AutomationProperties.SetName(_sessionsPanel, "Studio sessions, newest first");
            left.Children.Add(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = _sessionsPanel });
            _sessionsBorder = new Border
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(10, 10, 6, 10),
                Child = left,
            };
            root.Children.Add(_sessionsBorder);

            // ----- chat -----
            var right = new Grid { Margin = new Thickness(12, 0, 0, 0) };
            right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            right.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            right.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Grid.SetColumn(right, 1);
            root.Children.Add(right);

            var top = new StackPanel();
            _sessionNotice = NoticeBlock();   // PRIORITY fix 2026-10-17: pinned, not dismissible
            top.Children.Add(_sessionNotice);
            _header = new StackPanel { Margin = new Thickness(0, 0, 0, 4) };
            top.Children.Add(_header);
            Place(right, top, 0);
            _budgetText = FundUi.Text("Ideation AI budget: not loaded yet", 10.5, FundPalette.Faint, mono: true);
            _budgetText.Margin = new Thickness(0, 0, 0, 6);
            AutomationProperties.SetName(_budgetText, "Ideation AI budget");
            Place(right, _budgetText, 1);
            _chat = new StackPanel { Margin = new Thickness(0, 0, 10, 0) };
            AutomationProperties.SetName(_chat, "Studio conversation");
            _chatScroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = _chat, Focusable = false };
            Place(right, _chatScroll, 2);
            _errorHost = new StackPanel();
            Place(right, _errorHost, 3);

            var composer = new StackPanel();
            _input = new TextBox
            {
                AcceptsReturn = true,
                TextWrapping = TextWrapping.Wrap,
                MinHeight = 64,
                MaxHeight = 170,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                MaxLength = PlatformClient.StudioMaxMessageChars,
                FontSize = 12,
                Foreground = FundPalette.Text,
                Background = FundPalette.WindowBg,
                BorderBrush = FundPalette.Hairline,
                BorderThickness = new Thickness(1),
                Padding = new Thickness(8, 5, 8, 5),
                CaretBrush = FundPalette.Text,
                IsEnabled = false,
            };
            AutomationProperties.SetName(_input, "Message to the Studio researcher");
            AutomationProperties.SetHelpText(_input, "Describe a strategy idea or a change. Ctrl+Enter sends. Up to 4,000 characters.");
            _input.TextChanged += (_, _) =>
            {
                UpdateCharCount();
                _placeholder!.Visibility = string.IsNullOrEmpty(_input.Text) ? Visibility.Visible : Visibility.Collapsed;
            };
            _input.PreviewKeyDown += async (_, e) =>
            {
                if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control)
                {
                    e.Handled = true;
                    await SendAsync();
                }
            };
            // PRIORITY fix (user, 2026-10-17): the pinned AI-interaction notice above the composer + a placeholder
            _composerNotice = NoticeBlock();
            _composerNotice.Margin = new Thickness(0, 0, 0, 6);
            composer.Children.Add(_composerNotice);
            _placeholder = FundUi.Text(ComposerPlaceholder, 12, FundPalette.Faint);
            _placeholder.IsHitTestVisible = false;
            _placeholder.Margin = new Thickness(11, 6, 11, 0);
            _placeholder.VerticalAlignment = VerticalAlignment.Top;
            var inputGrid = new Grid();
            inputGrid.Children.Add(_input);
            inputGrid.Children.Add(_placeholder);
            composer.Children.Add(inputGrid);
            var row = new DockPanel { Margin = new Thickness(0, 6, 0, 0), LastChildFill = false };
            var modelLabel = FundUi.Text("Model", 10.5, FundPalette.Faint, wrap: false);
            modelLabel.VerticalAlignment = VerticalAlignment.Center;
            modelLabel.Margin = new Thickness(0, 0, 10, 0);
            row.Children.Add(modelLabel);
            _standard = Radio("StudioModel", "Standard", "Standard model (cheaper)", true);
            _premium = Radio("StudioModel", "Premium", "Premium model (more expensive)", false);
            row.Children.Add(_standard);
            row.Children.Add(_premium);
            _send = FundUi.MakeButton(AiContent.Glyph + " Send", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _send.IsEnabled = false;
            _send.ToolTip = "Sends the message to the language model (OpenRouter) — costs money (ideation budget). Ctrl+Enter.";
            ToolTipService.SetShowOnDisabled(_send, true);
            AutomationProperties.SetName(_send, "Send the message to the Studio researcher (AI)");
            _send.Click += async (_, _) => await SendAsync();
            DockPanel.SetDock(_send, Dock.Right);
            row.Children.Add(_send);
            _charCount = FundUi.Text("0 / 4,000", 10.5, FundPalette.Faint, mono: true, wrap: false);
            _charCount.VerticalAlignment = VerticalAlignment.Center;
            _charCount.Margin = new Thickness(0, 0, 10, 0);
            DockPanel.SetDock(_charCount, Dock.Right);
            row.Children.Add(_charCount);
            composer.Children.Add(row);
            _sendStatus = FundUi.Text("", 11, FundPalette.Ai);
            _sendStatus.Visibility = Visibility.Collapsed;
            _sendStatus.Margin = new Thickness(0, 6, 0, 0);
            AutomationProperties.SetLiveSetting(_sendStatus, AutomationLiveSetting.Polite);
            composer.Children.Add(_sendStatus);
            var note = FundUi.Text("The AI sees your text, the DSL reference, the current spec, the last run's computed facts and the memory listed with each draft. "
                                   + "It may only use the DSL; every valid draft is backtested automatically and counts as a trial of the family.", 10, FundPalette.Faint);
            note.Margin = new Thickness(0, 6, 0, 0);
            composer.Children.Add(note);
            Place(right, FundUi.Panel(composer, new Thickness(12, 10, 12, 10), new Thickness(0, 6, 10, 0)), 4);

            // Stage 5b (SPEC §14.5): tabs "Chat" | "Paper" — the paper view is created on first use
            var shell = new Grid();
            shell.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            shell.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            var tabs = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 8) };
            AutomationProperties.SetName(tabs, "Studio tabs");
            _chatTab = TabButton("Chat", "Chat: AI-assisted strategy drafts");
            _paperTab = TabButton("Paper", "Paper: build a strategy from a research paper and replicate it");
            _chatTab.Click += (_, _) => ShowTab(paper: false);
            _paperTab.Click += (_, _) => ShowTab(paper: true);
            tabs.Children.Add(_chatTab);
            tabs.Children.Add(_paperTab);
            shell.Children.Add(tabs);
            _chatRoot = root;
            _paperHost = new Border { Visibility = Visibility.Collapsed };
            Grid.SetRow(_paperHost, 1);
            Grid.SetRow(_chatRoot, 1);
            shell.Children.Add(_paperHost);
            shell.Children.Add(_chatRoot);
            Content = shell;
            ApplyTabLook(paper: false);
            ShowChatPlaceholder("Select a session or start one with \"+ New session\".");

            _poll = new DispatcherTimer { Interval = PollInterval };
            _poll.Tick += async (_, _) => await PollAsync();
            _waitTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _waitTimer.Tick += (_, _) => ShowWaiting();
            IsVisibleChanged += (_, _) => UpdatePolling();
        }

        private void ShowTab(bool paper)
        {
            if (paper)
            {
                if (_paperView == null)
                {
                    _paperView = new StudioPaperView();
                    _paperView.OpenInLibraryRequested += id => OpenInLibraryRequested?.Invoke(id);
                    _paperHost.Child = _paperView;
                }
                _chatRoot.Visibility = Visibility.Collapsed;
                _paperHost.Visibility = Visibility.Visible;
                _paperView.Activate();
            }
            else
            {
                _paperHost.Visibility = Visibility.Collapsed;
                _chatRoot.Visibility = Visibility.Visible;
            }
            ApplyTabLook(paper);
        }

        private void ApplyTabLook(bool paper)
        {
            foreach (var (b, on) in new[] { (_chatTab, !paper), (_paperTab, paper) })
            {
                b.Background = on ? FundPalette.Make(0x267C8CF8) : FundPalette.ButtonBg;
                b.BorderBrush = on ? FundPalette.Focus : FundPalette.ButtonBorder;
                b.Foreground = on ? FundPalette.TextBright : FundPalette.Text;
                AutomationProperties.SetItemStatus(b, on ? "selected" : "");
            }
        }

        private static Button TabButton(string text, string name)
        {
            var b = FundUi.MakeButton(text);
            b.Padding = new Thickness(14, 3, 14, 3);
            b.Margin = new Thickness(0, 0, 6, 0);
            AutomationProperties.SetName(b, name);
            return b;
        }

        /// <summary>Called whenever the rail's "Studio" entry is selected: reloads the sessions and the selected one.</summary>
        public async Task ActivateAsync()
        {
            if (_stopped) return;
            if (_paperHost.Visibility == Visibility.Visible) _paperView?.Activate();
            await LoadSessionsAsync();
            var target = _sessionId != null && _sessions.Any(s => s.Id == _sessionId) ? _sessionId : _sessions.FirstOrDefault()?.Id;
            if (target != null) await SelectSessionAsync(target);
            else
            {
                _sessionId = null;
                _detail = null;
                RenderHeader();
                ShowChatPlaceholder("No Studio session yet — start one with \"+ New session\".");
                UpdateComposer();
                await LoadBudgetAsync();
            }
        }

        /// <summary>Window closed: stop polling for good.</summary>
        public void Shutdown()
        {
            _stopped = true;
            _paperView?.Shutdown();
            _poll.Stop();
            _waitTimer.Stop();
        }

        // =====================================================================================
        // Sessions
        // =====================================================================================

        private async Task LoadSessionsAsync()
        {
            var seq = ++_listSeq;
            _sessionsStatus.Text = "Loading…";
            var res = await PlatformClient.ListStudioSessionsAsync(50);
            if (seq != _listSeq || _stopped) return;
            if (!res.Ok || res.Value == null)
            {
                _sessions = new List<StudioSession>();
                _sessionsStatus.Text = res.Ok ? "Unexpected response from the backend (/v2/studio/sessions)." : res.Message;
                _sessionsStatus.Foreground = FundPalette.Kill;
                RenderSessions();
                return;
            }
            _sessions = res.Value.Sessions ?? new List<StudioSession>();
            ApplyNotice(res.Value.AiInteractionNotice);
            _sessionsStatus.Text = _sessions.Count.ToString(Inv) + (_sessions.Count == 1 ? " session" : " sessions");
            _sessionsStatus.Foreground = FundPalette.Faint;
            RenderSessions();
        }

        private void RenderSessions()
        {
            _sessionsPanel.Children.Clear();
            if (_sessions.Count == 0)
            {
                _sessionsPanel.Children.Add(FundUi.Text("No sessions yet.", 11, FundPalette.Faint));
                return;
            }
            foreach (var s in _sessions)
            {
                var id = s.Id ?? "";
                var selected = id == _sessionId;
                var stack = new StackPanel();
                stack.Children.Add(FundUi.Text(SessionTitle(s), 11.5, selected ? FundPalette.TextBright : FundPalette.Text, bold: selected));
                stack.Children.Add(FundUi.Text(FundFormat.LocalTime(s.LastMessageAt ?? s.CreatedAt, "yyyy-MM-dd HH:mm")
                                               + " · " + (s.MessageCount?.ToString(Inv) ?? "?") + " messages · " + (s.TrialCount?.ToString(Inv) ?? "?") + " trials",
                                               10, FundPalette.Faint, mono: true));
                var b = FundUi.MakeButton("");
                b.Content = stack;
                stack.HorizontalAlignment = HorizontalAlignment.Left;
                b.HorizontalContentAlignment = HorizontalAlignment.Stretch;
                b.Padding = new Thickness(8, 6, 8, 6);
                b.Margin = new Thickness(0, 0, 4, 6);
                b.Background = selected ? FundPalette.Make(0x267C8CF8) : FundPalette.Make(0xFF1E1E1E);
                b.BorderBrush = selected ? FundPalette.Focus : FundPalette.PanelBorder;
                AutomationProperties.SetName(b, "Studio session " + SessionTitle(s) + (selected ? ", selected" : "") + ", " + (s.MessageCount?.ToString(Inv) ?? "unknown") + " messages");
                b.Click += async (_, _) => { if (id.Length > 0) await SelectSessionAsync(id); };
                _sessionsPanel.Children.Add(b);
            }
        }

        private static string SessionTitle(StudioSession s)
            => string.IsNullOrWhiteSpace(s.Title) ? "Untitled session (" + (s.Id ?? "?") + ")" : s.Title!;

        private async Task NewSessionAsync()
        {
            if (_busy) return;
            var values = FundDialogs.Form(Window.GetWindow(this), "New Studio session",
                "One session = one strategy family: the first valid draft creates a strategy (origin studio, state draft), follow-ups create new versions.",
                new[] { ("Title (optional, up to " + PlatformClient.StudioMaxTitleChars.ToString(Inv) + " characters)", "") }, "Create session");
            if (values == null) return;
            _newSession.IsEnabled = false;
            var res = await PlatformClient.CreateStudioSessionAsync(values.Length > 0 ? values[0] : null);
            _newSession.IsEnabled = !_busy;
            if (_stopped) return;
            if (!res.Ok || res.Value?.Id == null)
            {
                ShowError(res.Ok ? "Unexpected response from the backend (new Studio session)." : res.Message);
                return;
            }
            _sessionId = res.Value.Id;
            await LoadSessionsAsync();
            await SelectSessionAsync(res.Value.Id!);
        }

        private async Task SelectSessionAsync(string sessionId)
        {
            if (_sessionId != sessionId) _detail = null;
            _sessionId = sessionId;
            RenderSessions();
            await LoadSessionAsync(sessionId, scrollToEnd: true, quiet: false);
        }

        // =====================================================================================
        // One session: load, poll
        // =====================================================================================

        private async Task LoadSessionAsync(string sessionId, bool scrollToEnd, bool quiet)
        {
            var seq = ++_sessionSeq;
            if (!quiet && _detail == null) ShowChatPlaceholder("Loading…");
            var res = await PlatformClient.GetStudioSessionAsync(sessionId);
            if (seq != _sessionSeq || _stopped || sessionId != _sessionId) return;
            if (!res.Ok || res.Value == null)
            {
                var msg = res.Ok ? "Unexpected response from the backend (Studio session) — backend and app versions may not match." : res.Message;
                if (quiet) ShowError(msg);
                else
                {
                    _detail = null;
                    RenderHeader();
                    _chat.Children.Clear();
                    _statusHosts.Clear();
                    _chat.Children.Add(ErrorBlock(msg));
                    UpdateComposer();
                }
                return;
            }
            var oldCount = _detail?.Messages?.Count ?? -1;
            _detail = res.Value;
            if (!quiet || oldCount != (_detail.Messages?.Count ?? 0))
            {
                RenderDetail();
                if (scrollToEnd || quiet) ScrollChatToEnd();
            }
            else
            {
                RenderHeader();
                RenderBudget(_detail.Budget, null);
                RefreshBacktestStatuses();
            }
            UpdatePolling();
        }

        private async Task PollAsync()
        {
            if (_polling || _stopped || _busy || _sessionId == null || !IsVisible) return;
            _polling = true;
            try
            {
                await LoadSessionAsync(_sessionId, scrollToEnd: false, quiet: true);
            }
            finally
            {
                _polling = false;
            }
        }

        private void UpdatePolling()
        {
            if (!_stopped && IsVisible && _detail != null && AnyActiveBacktest()) { if (!_poll.IsEnabled) _poll.Start(); }
            else _poll.Stop();
        }

        private bool AnyActiveBacktest()
        {
            if (_detail?.Messages == null) return false;
            if (_detail.Queue is { Count: > 0 }) return true;
            foreach (var m in _detail.Messages)
                if (m.Draft != null && IsActive(BacktestOf(m))) return true;
            return false;
        }

        private bool IsActive(DraftBacktest? bt)
        {
            if (bt == null) return false;
            if (bt.State == "waiting") return true;
            if (bt.State != "started") return false;
            var status = RunOf(bt.RunId)?.Status ?? bt.RunStatus;
            return status == null || BacktestStatus.IsActive(status);
        }

        /// <summary>The draft's backtest: the queue entry (current position) while waiting, else the message's draft.backtest.</summary>
        private DraftBacktest? BacktestOf(StudioMessage m)
        {
            var id = m.Id;
            var queued = id == null ? null : _detail?.Queue?.FirstOrDefault(q => q.MessageId == id);
            return queued ?? m.Draft?.Backtest;
        }

        private StudioRun? RunOf(string? runId)
            => runId == null ? null : _detail?.Runs?.FirstOrDefault(r => r.RunId == runId);

        private async Task LoadBudgetAsync()
        {
            var res = await PlatformClient.GetIdeationSettingsAsync();
            if (_stopped) return;
            if (!res.Ok || res.Value == null) RenderBudget(null, res.Ok ? "Unexpected response from the backend (/v2/settings/ideation)." : res.Message);
            else RenderBudget(res.Value, null);
        }

        private void RenderBudget(IdeationSettings? b, string? error)
        {
            if (b == null)
            {
                _budgetText.Text = "Ideation AI budget: " + (error ?? "not reported by the backend");
                _budgetText.Foreground = error != null ? FundPalette.Kill : FundPalette.Faint;
                return;
            }
            _budgetText.Text = StudioJson.BudgetLine(b) + " — set it in the Hedge Fund settings";
            _budgetText.Foreground = b.Enabled == false || !string.IsNullOrWhiteSpace(b.PausedReason) ? FundPalette.Risk : FundPalette.Faint;
        }

        // =====================================================================================
        // Rendering
        // =====================================================================================

        private void RenderHeader()
        {
            _header.Children.Clear();
            var s = _detail?.Session;
            if (s == null)
            {
                _header.Children.Add(FundUi.Text("Strategy Studio", 15, FundPalette.TextBright, bold: true));
                return;
            }
            var title = new DockPanel();
            var strategyId = s.StrategyId;
            if (strategyId != null)
            {
                var lib = FundUi.MakeButton("Open in Library");
                lib.VerticalAlignment = VerticalAlignment.Center;
                lib.ToolTip = "Show this session's strategy (all versions) in the Library";
                AutomationProperties.SetName(lib, "Open the session's strategy in the Library");
                string sid = strategyId;
                lib.Click += (_, _) => OpenInLibraryRequested?.Invoke(sid);
                DockPanel.SetDock(lib, Dock.Right);
                title.Children.Add(lib);
            }
            title.Children.Add(FundUi.Text(SessionTitle(s), 15, FundPalette.TextBright, bold: true));
            _header.Children.Add(title);
            var trials = _detail?.TrialCount ?? s.TrialCount;
            _header.Children.Add(Line("Session " + (s.Id ?? "?") + " · created " + FundFormat.LocalTime(s.CreatedAt, "yyyy-MM-dd HH:mm")
                                      + " · strategy " + (strategyId ?? "none yet (created by the first valid draft)")
                                      + (s.FamilyId != null ? " · family " + s.FamilyId : "")
                                      + " · family trials " + (trials?.ToString(Inv) ?? "—"), 10.5, FundPalette.Faint, mono: true));
        }

        private void RenderDetail()
        {
            ApplyNotice(_detail?.AiInteractionNotice);
            RenderHeader();
            RenderBudget(_detail?.Budget, null);
            ApplyEstimate(_detail?.EstimateUsd);
            _chat.Children.Clear();
            _statusHosts.Clear();
            var messages = _detail?.Messages ?? new List<StudioMessage>();
            if (messages.Count == 0)
                _chat.Children.Add(FundUi.Text("No messages yet — describe a strategy idea below (e.g. universe, signal, holding period).", 11.5, FundPalette.Faint));
            for (var i = 0; i < messages.Count; i++)
            {
                var m = messages[i];
                // Stage 10 polish (todo 2026-10-17): a discarded AI answer (off topic, topic check …) followed by the backend's fixed note
                // renders as ONE small system note with the cost — no empty AI block, no second note
                if (m.Role == "assistant" && IsEmptyDiscard(m) && i + 1 < messages.Count && messages[i + 1].Role == "system_note")
                {
                    _chat.Children.Add(SystemNote((messages[i + 1].Text ?? "") + "\nAI reply not shown"
                                                  + (string.IsNullOrWhiteSpace(m.DiscardedReason) ? "" : " (" + DiscardedLabel(m.DiscardedReason!) + ")")
                                                  + " · " + CostLine(m), m.CreatedAt, FundPalette.Text));
                    i++;
                    continue;
                }
                _chat.Children.Add(MessageElement(m));
            }
            UpdateComposer();
        }

        private void ApplyEstimate(ExplainEstimate? e)
        {
            _standard.Content = "Standard" + (e?.Standard is double s ? " · est. " + FundFormat.Usd(s) : "");
            _premium.Content = "Premium" + (e?.Premium is double p ? " · est. " + FundFormat.Usd(p) : "");
        }

        /// <summary>An assistant message with nothing to show: discarded, or off topic with no text — and no draft / validator card.</summary>
        private static bool IsEmptyDiscard(StudioMessage m)
            => m.Draft == null && (m.Discarded == true || (m.Kind == "off_topic" && string.IsNullOrWhiteSpace(m.Text)));

        private FrameworkElement MessageElement(StudioMessage m)
        {
            switch (m.Role)
            {
                case "user":
                {
                    var stack = new StackPanel();
                    stack.Children.Add(FundUi.Text("You · " + FundFormat.LocalTime(m.CreatedAt, "yyyy-MM-dd HH:mm"), 10, FundPalette.Faint, mono: true));
                    var t = FundUi.Text(m.Text ?? "", 11.5, FundPalette.TextBright);
                    t.Margin = new Thickness(0, 2, 0, 0);
                    stack.Children.Add(t);
                    var b = new Border
                    {
                        Background = FundPalette.ButtonBg,
                        BorderBrush = FundPalette.ButtonBorder,
                        BorderThickness = new Thickness(1),
                        CornerRadius = new CornerRadius(8),
                        Padding = new Thickness(10, 6, 10, 8),
                        Margin = new Thickness(80, 4, 0, 6),
                        HorizontalAlignment = HorizontalAlignment.Right,
                        MaxWidth = 760,
                        Child = stack,
                    };
                    AutomationProperties.SetName(b, "Your message: " + (m.Text ?? ""));
                    return b;
                }
                case "assistant" when m.Discarded == true:
                {
                    // PRIORITY fix 2026-10-17: a discarded AI answer is never shown — only that it happened and what it cost
                    // (the backend's fixed note follows as a system_note). draft_invalid keeps ONLY the validator card.
                    var note = SystemNote("AI reply discarded" + (string.IsNullOrWhiteSpace(m.DiscardedReason) ? "" : " (" + DiscardedLabel(m.DiscardedReason!) + ")")
                                          + " — not shown · " + CostLine(m)
                                          + (string.IsNullOrWhiteSpace(m.Text) ? "" : "\n" + m.Text), m.CreatedAt, FundPalette.Dim);
                    if (m.Draft == null) return note;
                    var both = new StackPanel();
                    both.Children.Add(note);
                    var label = Line("Validator findings on the spec the AI proposed (the AI's text is not shown):", 10.5, FundPalette.Faint);
                    label.Margin = new Thickness(0, 0, 40, 2);
                    both.Children.Add(label);
                    var card = DraftCard(m, m.Draft);
                    card.Margin = new Thickness(0, 0, 40, 8);
                    both.Children.Add(card);
                    return both;
                }
                case "assistant" when IsEmptyDiscard(m):
                    // off topic with no text and no backend note after it: one small note with the cost, never an empty AI block
                    return SystemNote("AI reply not shown (outside the Studio's scope) · " + CostLine(m), m.CreatedAt, FundPalette.Dim);
                case "assistant":
                    return AssistantElement(m);
                default:
                    // system_note: fixed backend notes (scope note, queue full, backtest could not start, …) — no AI
                    return SystemNote(m.Text ?? "", m.CreatedAt, m.Role == "system_note" ? FundPalette.Text : FundPalette.Risk);
            }
        }

        /// <summary>Neutral system note (no AI label, no AI colours).</summary>
        private static FrameworkElement SystemNote(string text, string? createdAt, Brush brush)
        {
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text("System note · " + FundFormat.LocalTime(createdAt, "yyyy-MM-dd HH:mm"), 10, FundPalette.Faint, mono: true));
            var t = FundUi.Text(text, 11, brush);
            t.Margin = new Thickness(0, 2, 0, 0);
            stack.Children.Add(t);
            var b = new Border
            {
                Background = FundPalette.WindowBg,
                BorderBrush = FundPalette.Hairline,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 5, 10, 6),
                Margin = new Thickness(0, 2, 40, 6),
                Child = stack,
            };
            AutomationProperties.SetName(b, "System note: " + text);
            return b;
        }

        private static string DiscardedLabel(string reason) => reason switch
        {
            "off_topic" => "outside the Studio's scope",
            "clarify_limit" => "too many questions in a row",
            "draft_invalid" => "the draft did not pass the validator",
            "topic_check_failed" => "it did not pass the topic check",
            _ => reason.Replace('_', ' '),
        };

        private FrameworkElement AssistantElement(StudioMessage m)
        {
            FundAi.TryRead(m.Provenance, m.Model, m.CreatedAt, out var model, out var at);
            var modelText = string.IsNullOrWhiteSpace(model) ? "model not reported" : model!;
            var timeText = at.HasValue ? at.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm", Inv) : FundFormat.LocalTime(m.CreatedAt, "yyyy-MM-dd HH:mm");
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text(AiContent.Glyph + " AI-generated · " + modelText + " · " + timeText
                                           + (m.Kind == "clarify" ? " · clarifying question" : ""), 10.5, FundPalette.Ai, bold: true, mono: true));
            stack.Children.Add(Line(CostLine(m), 10, FundPalette.Faint, mono: true));
            var reply = FundUi.Text(string.IsNullOrEmpty(m.Text) ? "(no reply text)" : m.Text, 11.5, FundPalette.Text);
            reply.LineHeight = 17;
            reply.Margin = new Thickness(0, 4, 0, 4);
            stack.Children.Add(reply);

            if (m.Draft != null) stack.Children.Add(DraftCard(m, m.Draft));
            AddOperatorRequests(stack, m.OperatorRequests);
            AddAssumptions(stack, m.Assumptions);
            AddMemory(stack, m.MemoryUsed);

            var caveat = FundUi.Text(AiContent.ResearchNotice, 10, FundPalette.Faint);   // Stage 10: research output notice (b)
            caveat.FontStyle = FontStyles.Italic;
            caveat.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(caveat);
            var border = new Border
            {
                Background = FundPalette.AiSurface,
                BorderBrush = FundPalette.Ai,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(12, 8, 12, 10),
                Margin = new Thickness(0, 4, 40, 8),
                Child = stack,
            };
            AutomationProperties.SetName(border, AiContent.AutomationName(model, at) + " Studio reply: " + (m.Text ?? ""));
            return border;
        }

        private static string CostLine(StudioMessage m)
        {
            var parts = new List<string> { TierLabel(m.ModelTier), "cost " + FundFormat.Usd(m.CostUsd ?? BtJson.Num(m.Usage, "cost_usd")) };
            if (!string.IsNullOrWhiteSpace(m.TopicCheck)) parts.Add("topic check " + m.TopicCheck);
            if (BtJson.Num(m.Usage, "calls") is double calls && calls > 1)
                parts.Add(calls.ToString("0", Inv) + " calls (incl. one repair round)");
            if (BtJson.Num(m.Usage, "estimate_usd") is double est) parts.Add("estimate was " + FundFormat.Usd(est));
            return string.Join(" · ", parts);
        }

        private static string TierLabel(string? tier) => tier switch
        {
            "standard" => "Standard",
            "premium" => "Premium",
            null or "" => "tier not reported",
            _ => tier!,
        };

        private FrameworkElement DraftCard(StudioMessage m, StudioDraft d)
        {
            var stack = new StackPanel();
            var head = new StackPanel { Orientation = Orientation.Horizontal };
            head.Children.Add(FundUi.Text("Draft", 11.5, FundPalette.TextBright, bold: true, wrap: false));
            var valid = d.Valid == true;
            var needsConfirmation = valid && d.Version == null && !string.IsNullOrWhiteSpace(d.RequiresConfirmation);
            var notSaved = valid && d.NotSaved == true;
            var chip = notSaved ? Chip("not saved", FundPalette.Risk)
                     : needsConfirmation ? Chip("unsaved — needs your confirmation", FundPalette.Risk)
                     : Chip(valid ? "valid" : "invalid", valid ? FundPalette.Long : FundPalette.Kill);
            chip.Margin = new Thickness(8, 0, 0, 0);
            head.Children.Add(chip);
            if (notSaved || needsConfirmation)
            {
                var vchip = Chip("valid", FundPalette.Long);
                vchip.Margin = new Thickness(6, 0, 0, 0);
                head.Children.Add(vchip);
            }
            var versionText = d.Version is int v ? " · v" + v.ToString(Inv)
                            : !valid ? " · no version, not backtested"
                            : notSaved || needsConfirmation ? " · not saved, not backtested" : "";
            var vt = FundUi.Text(versionText + (d.StrategyId != null ? " · " + d.StrategyId : ""), 10.5, FundPalette.Dim, mono: true, wrap: false);
            vt.VerticalAlignment = VerticalAlignment.Center;
            vt.Margin = new Thickness(6, 0, 0, 0);
            head.Children.Add(vt);
            stack.Children.Add(head);

            // validator result + repair round
            if (d.Repaired == true)
                stack.Children.Add(Line("The first answer was invalid; the validator's errors were sent back once (repair round, charged).", 10.5, FundPalette.Dim));
            if (!string.IsNullOrWhiteSpace(d.RepairSkipped))
                stack.Children.Add(Line("No repair round: " + d.RepairSkipped, 10.5, FundPalette.Risk));
            var errors = d.Validation?.Errors ?? new List<StrategyIssue>();
            var warnings = d.Validation?.Warnings ?? new List<StrategyIssue>();
            if (d.Validation == null)
                stack.Children.Add(Line("The backend sent no validator result.", 10.5, FundPalette.Risk));
            else if (errors.Count == 0)
                stack.Children.Add(Line("Validator: ok" + (warnings.Count > 0 ? " · " + warnings.Count.ToString(Inv) + " warning(s)" : ""), 10.5, FundPalette.Long));
            foreach (var e in errors) stack.Children.Add(Issue("error", e, FundPalette.Kill));
            foreach (var w in warnings) stack.Children.Add(Issue("warning", w, FundPalette.Risk));
            if (d.RepairErrors is { Count: > 0 } re)
            {
                var exp = MakeExpander("Errors of the first answer (" + re.Count.ToString(Inv) + ")", "Validator errors of the first answer, before the repair round");
                var list = new StackPanel();
                foreach (var e in re) list.Children.Add(Issue("error", e, FundPalette.Dim));
                exp.Content = list;
                stack.Children.Add(exp);
            }
            if (d.Unchanged == true)
                stack.Children.Add(Line("Same spec as the previous version — no new version was stored.", 10.5, FundPalette.Dim));
            if (notSaved)
                stack.Children.Add(Line("Not saved: " + (string.IsNullOrWhiteSpace(d.NotSavedReason) ? "the backend gave no reason." : d.NotSavedReason), 10.5, FundPalette.Risk));
            if (needsConfirmation)
            {
                stack.Children.Add(Line("Not saved yet — saving it as a new version " + d.RequiresConfirmation
                                        + ". Nothing is saved or backtested until you confirm.", 10.5, FundPalette.Risk));
                var messageId = m.Id;
                if (messageId != null && _sessionId != null)
                {
                    string sid = _sessionId;
                    string mid = messageId;
                    string effect = d.RequiresConfirmation!;
                    var save = SmallButton("Save as new version…", "Save this draft as a new version — " + effect);
                    save.ToolTip = "Saves the draft as a new version (" + effect + ") and backtests it — counts as another trial";
                    save.Click += async (_, _) => await SaveDraftAsync(sid, mid, effect, save);
                    stack.Children.Add(save);
                }
            }
            if (d.SavedByConfirmation == true)
                stack.Children.Add(Line("Saved after your confirmation — the strategy went back to the draft state.", 10.5, FundPalette.Dim));

            // spec changes vs the STORED version the label names (GET /v2/strategies/{id}/versions/{n}, cached) + the proposed spec
            if (d.Spec is JsonElement spec && spec.ValueKind == JsonValueKind.Object)
            {
                var diffStrategy = d.StrategyId ?? _detail?.Session?.StrategyId;
                var baseVersion = BaseVersionFor(m, d);
                if (diffStrategy != null && baseVersion is int bv)
                    stack.Children.Add(DiffExpander(diffStrategy, bv, d.Unchanged == true ? null : d.Version, spec));
                else
                    stack.Children.Add(Line(d.Version == 1 ? "First version of this strategy — no previous version to compare."
                                                           : "No stored earlier version of this strategy to compare with.", 10.5, FundPalette.Faint));
                var full = MakeExpander("Proposed spec (JSON, as returned by the AI)", "The full proposed strategy spec as JSON, as returned by the AI");
                var specText = FundUi.Text(JsonSerializer.Serialize(spec, new JsonSerializerOptions { WriteIndented = true }), 10.5, FundPalette.Dim, mono: true);
                full.Content = specText;
                stack.Children.Add(full);
            }
            else if (d.Spec != null)
            {
                stack.Children.Add(Line("The proposed spec is not a JSON object.", 10.5, FundPalette.Risk));
            }

            // family trial count + automatic backtest
            stack.Children.Add(Line("Family trials: " + ((_detail?.TrialCount ?? _detail?.Session?.TrialCount)?.ToString(Inv) ?? "—")
                                    + " — every finished backtest counts (stricter deflated-Sharpe gate G4 later).", 10.5, FundPalette.Dim));
            var host = new StackPanel();
            if (m.Id != null) _statusHosts[m.Id] = host;
            BuildBacktestStatus(host, m, d);
            stack.Children.Add(host);

            var strategyId = d.StrategyId;
            if (strategyId != null)
            {
                var lib = FundUi.MakeButton("Open in Library");
                lib.HorizontalAlignment = HorizontalAlignment.Left;
                lib.Margin = new Thickness(0, 6, 0, 0);
                AutomationProperties.SetName(lib, "Open the draft's strategy in the Library");
                string sid = strategyId;
                lib.Click += (_, _) => OpenInLibraryRequested?.Invoke(sid);
                stack.Children.Add(lib);
            }

            var card = new Border
            {
                Background = FundPalette.Panel,
                BorderBrush = valid ? FundPalette.PanelBorder : FundPalette.Kill,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 8, 10, 8),
                Margin = new Thickness(0, 4, 0, 4),
                Child = stack,
            };
            AutomationProperties.SetName(card, "Draft " + (valid ? "valid" : "invalid") + versionText);
            return card;
        }

        /// <summary>The stored version a draft is compared with: the version itself for an unchanged draft; else the
        /// backend's base_version; a stored v without one: v−1 (null: nothing stored to compare with).</summary>
        private int? BaseVersionFor(StudioMessage m, StudioDraft d)
        {
            if (d.Version is int v && d.Unchanged == true) return v;
            if (d.BaseVersion is int b) return b;                       // the backend's base of the draft
            return d.Version is int sv && sv > 1 ? sv - 1 : null;      // stored v without a reported base: its predecessor
        }

        /// <summary>"Spec changes: v{new} vs v{base}" (or "proposed spec vs v{base}" when the draft has no stored version of
        /// its own); both stored specs are fetched when the expander is opened the first time (cached per strategy@version).
        /// A failed fetch is shown verbatim and can be retried by collapsing and opening again.</summary>
        private Expander DiffExpander(string strategyId, int baseVersion, int? newVersion, JsonElement proposed)
        {
            var title = "Spec changes: " + (newVersion is int nv ? "v" + nv.ToString(Inv) : "proposed spec") + " vs stored v" + baseVersion.ToString(Inv);
            var exp = MakeExpander(title, "Changes of the spec against the stored version " + baseVersion.ToString(Inv));
            var list = new StackPanel();
            list.Children.Add(Line("Open to load the stored version" + (newVersion != null ? "s" : "") + ".", 10.5, FundPalette.Faint));
            exp.Content = list;
            var loading = false;
            var loaded = false;
            exp.Expanded += async (_, _) =>
            {
                if (loading || loaded) return;
                loading = true;
                list.Children.Clear();
                list.Children.Add(Line("Loading…", 10.5, FundPalette.Faint));
                try
                {
                    var before = await StoredSpecAsync(strategyId, baseVersion);
                    JsonElement? after = proposed;
                    string? error = before.Error;
                    if (error == null && newVersion is int n)
                    {
                        var stored = await StoredSpecAsync(strategyId, n);
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
                    var diff = SpecDiff(b, a);
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
            };
            return exp;
        }

        /// <summary>The stored spec of strategy@version (cached; failures are not cached) or the backend's error text.</summary>
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

        private void RefreshBacktestStatuses()
        {
            if (_detail?.Messages == null) return;
            foreach (var m in _detail.Messages)
            {
                if (m.Draft == null || m.Id == null || !_statusHosts.TryGetValue(m.Id, out var host)) continue;
                BuildBacktestStatus(host, m, m.Draft);
            }
        }

        private void BuildBacktestStatus(StackPanel host, StudioMessage m, StudioDraft d)
        {
            host.Children.Clear();
            host.Margin = new Thickness(0, 4, 0, 0);
            if (d.Valid != true)
            {
                host.Children.Add(Line("Backtest: none — invalid drafts are never backtested.", 10.5, FundPalette.Faint));
                return;
            }
            var bt = BacktestOf(m);
            if (bt == null && d.Version == null && !string.IsNullOrWhiteSpace(d.RequiresConfirmation))
            {
                host.Children.Add(Line("Backtest: starts after you save the draft (\"Save as new version…\").", 10.5, FundPalette.Faint));
                return;
            }
            if (bt == null && d.NotSaved == true)
            {
                host.Children.Add(Line("Backtest: none — the draft was not saved.", 10.5, FundPalette.Faint));
                return;
            }
            if (bt == null)
            {
                host.Children.Add(Line(d.Unchanged == true ? "Backtest: none — same spec as the previous version." : "Backtest: the backend reported no backtest for this draft.",
                                       10.5, d.Unchanged == true ? FundPalette.Faint : FundPalette.Risk));
                return;
            }
            var strategyId = d.StrategyId ?? _detail?.Session?.StrategyId;
            var msg = string.IsNullOrWhiteSpace(bt.Message) ? "" : " — " + bt.Message;
            switch (bt.State)
            {
                case "waiting":
                {
                    host.Children.Add(Line("Waiting for the backtest slot" + (bt.Position is int pos ? " (position " + pos.ToString(Inv) + ")" : "")
                                           + " — another job is running; it starts automatically" + msg, 10.5, FundPalette.Risk));
                    var messageId = bt.MessageId ?? m.Id;
                    if (messageId != null && _sessionId != null)
                    {
                        var cancel = SmallButton("Cancel", "Remove this draft from the backtest queue");
                        string sid = _sessionId;
                        string mid = messageId;
                        cancel.Click += async (_, _) => await CancelDraftAsync(sid, mid, cancel);
                        host.Children.Add(cancel);
                    }
                    return;
                }
                case "started":
                {
                    var run = RunOf(bt.RunId);
                    var status = run?.Status ?? bt.RunStatus;
                    var metrics = run?.KeyMetrics ?? bt.KeyMetrics;
                    var row = new WrapPanel { Margin = new Thickness(0, 2, 0, 0) };
                    if (status == null || BacktestStatus.IsActive(status))
                    {
                        host.Children.Add(Line("Backtest " + (status ?? "started") + (status == RunResume.WaitingStatus ? " (connection lost — resumes automatically)" : "…")
                                               + " · run " + (bt.RunId ?? "?") + msg, 10.5, FundPalette.Focus, mono: true));
                        if (bt.RunId != null)
                        {
                            string runId = bt.RunId;
                            var cancel = SmallButton("Cancel backtest", "Cancel the running backtest of this draft");
                            cancel.Click += async (_, _) => await CancelRunAsync(runId, cancel);
                            row.Children.Add(cancel);
                        }
                    }
                    else if (status == "done")
                    {
                        host.Children.Add(Line("Backtest done · run " + (bt.RunId ?? "?"), 10.5, FundPalette.Long, mono: true));
                        host.Children.Add(MiniMetrics(metrics));
                    }
                    else
                    {
                        host.Children.Add(Line("Backtest " + status + " · run " + (bt.RunId ?? "?") + msg, 10.5, status == "failed" ? FundPalette.Kill : FundPalette.Dim, mono: true));
                    }
                    if (bt.RunId != null)
                    {
                        string runId = bt.RunId;
                        var open = SmallButton("Open report", "Open the backtest report of this run");
                        open.Click += (_, _) => BacktestReportWindow.Open(Window.GetWindow(this), strategyId, null, runId);
                        row.Children.Add(open);
                    }
                    if (row.Children.Count > 0) host.Children.Add(row);
                    return;
                }
                case "cancelled":
                    host.Children.Add(Line("Backtest cancelled" + msg, 10.5, FundPalette.Dim));
                    return;
                case "start_failed":
                    host.Children.Add(Line("The backtest could not start" + msg, 10.5, FundPalette.Kill));
                    return;
                case "not_queued":
                    host.Children.Add(Line(string.IsNullOrWhiteSpace(bt.Message) ? "Backtest not queued." : bt.Message!, 10.5, FundPalette.Risk));
                    return;
                default:
                    host.Children.Add(Line("Backtest state: " + (bt.State ?? "not reported") + msg, 10.5, FundPalette.Risk));
                    return;
            }
        }

        private static FrameworkElement MiniMetrics(JsonElement? km)
        {
            if (km == null) return Line("No key metrics in this run.", 10.5, FundPalette.Faint);
            var text = "Return " + FundFormat.Fraction(BtJson.Num(km, "total_return"), 1, signed: true)
                       + " · CAGR " + FundFormat.Fraction(BtJson.Num(km, "cagr"), 1, signed: true)
                       + " · Sharpe " + FundFormat.Num(BtJson.Num(km, "sharpe"))
                       + " · Max DD " + FundFormat.Fraction(BtJson.Num(km, "max_drawdown"))
                       + " · Vol " + FundFormat.Fraction(BtJson.Num(km, "volatility"));
            var t = Line(text, 11, FundPalette.Text, mono: true);
            AutomationProperties.SetName(t, "Backtest key metrics: " + text);
            return t;
        }

        private static void AddOperatorRequests(StackPanel stack, List<StudioOperatorRequest>? requests)
        {
            if (requests == null || requests.Count == 0) return;
            var inner = new StackPanel();
            inner.Children.Add(FundUi.Text("Operator requests — unverified AI suggestion (text only, never executed)", 10.5, FundPalette.Risk, bold: true));
            foreach (var r in requests)
            {
                var t = Line((r.Name ?? "(no name)") + (string.IsNullOrWhiteSpace(r.Signature) ? "" : " — " + r.Signature), 10.5, FundPalette.Text, mono: true);
                inner.Children.Add(t);
                if (!string.IsNullOrWhiteSpace(r.Purpose))
                {
                    var p = Line(r.Purpose!, 10.5, FundPalette.Dim);
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
            AutomationProperties.SetName(box, "Operator requests, unverified AI suggestion, never executed: " + string.Join("; ", requests.Select(r => r.Name ?? "")));
            stack.Children.Add(box);
        }

        private static void AddAssumptions(StackPanel stack, List<string>? assumptions)
        {
            if (assumptions == null || assumptions.Count == 0) return;
            var head = FundUi.Text("Assumptions of the AI", 10.5, FundPalette.Text, bold: true);
            head.Margin = new Thickness(0, 6, 0, 2);
            stack.Children.Add(head);
            foreach (var a in assumptions) stack.Children.Add(Line("• " + a, 10.5, FundPalette.Dim));
        }

        private static void AddMemory(StackPanel stack, List<StudioMemoryFact>? memory)
        {
            var count = memory?.Count ?? 0;
            var exp = MakeExpander("Memory used for this draft (" + count.ToString(Inv) + ")",
                               "Computed facts from earlier runs that the prompt contained — no AI-written lessons, no holdout results");
            var list = new StackPanel();
            if (count == 0) list.Children.Add(Line("No memory facts were used.", 10.5, FundPalette.Faint));
            foreach (var f in memory ?? new List<StudioMemoryFact>())
            {
                list.Children.Add(Line((f.Kind ?? "fact") + (f.FamilyId != null ? " · family " + f.FamilyId : "") + (f.RunId != null ? " · run " + f.RunId : ""), 10, FundPalette.Faint, mono: true));
                var t = Line(f.Text ?? "", 10.5, FundPalette.Dim);
                t.Margin = new Thickness(0, 0, 0, 4);
                list.Children.Add(t);
            }
            list.Children.Add(Line("Computed facts only (gates, metrics, attribution of earlier runs) — never holdout results.", 10, FundPalette.Faint));
            exp.Content = list;
            stack.Children.Add(exp);
        }

        // =====================================================================================
        // Actions
        // =====================================================================================

        private async Task SendAsync()
        {
            if (_busy || _stopped || _sessionId == null) return;
            var text = (_input.Text ?? "").Trim();
            if (text.Length == 0)
            {
                SetSendStatus("Enter a message first.", FundPalette.Risk);
                return;
            }
            if (text.Length > PlatformClient.StudioMaxMessageChars)
            {
                SetSendStatus("The message is longer than 4,000 characters.", FundPalette.Risk);
                return;
            }
            var model = _premium.IsChecked == true ? PlatformClient.ExplainModelPremium : PlatformClient.ExplainModelStandard;
            var sessionId = _sessionId;
            _busy = true;
            _errorHost.Children.Clear();
            UpdateComposer();
            _sessionsBorder.IsEnabled = false;
            _waitStarted = DateTime.UtcNow;
            ShowWaiting();
            _waitTimer.Start();
            try
            {
                var res = await PlatformClient.PostStudioMessageAsync(sessionId, text, model);
                if (_stopped) return;
                if (!res.Ok || res.Value == null)
                {
                    // budget reached / AI off / no key / bad_json / timeout / busy … — the backend's text, verbatim; nothing was stored
                    ShowError(res.Ok ? "Unexpected response from the backend (Studio message) — backend and app versions may not match." : res.Message);
                    return;
                }
                _input.Text = "";
            }
            finally
            {
                _waitTimer.Stop();
                SetSendStatus(null, null);
                _busy = false;
                _sessionsBorder.IsEnabled = true;
                UpdateComposer();
            }
            if (_sessionId == sessionId) await LoadSessionAsync(sessionId, scrollToEnd: true, quiet: false);
            await LoadSessionsAsync();
            _input.Focus();
        }

        /// <summary>Review fix M1: "Save as new version…" → confirmation (demotes the strategy to draft, another trial) → POST …/save.</summary>
        private async Task SaveDraftAsync(string sessionId, string messageId, string effect, Button button)
        {
            if (_busy) return;
            if (!FundDialogs.Confirm(Window.GetWindow(this), "Save as new version",
                    "Save this AI draft as a new version of the strategy? This " + effect
                    + " — the strategy's earlier validation no longer applies to the new version and it has to go through the gates again.\n\n"
                    + "The new version is backtested automatically; that backtest counts as another trial of the family (stricter deflated-Sharpe gate G4).",
                    "Save as new version", danger: true))
                return;
            button.IsEnabled = false;
            var res = await PlatformClient.SaveStudioDraftAsync(sessionId, messageId);
            if (_stopped) return;
            if (!res.Ok) ShowError(res.Message);
            if (_sessionId == sessionId) await LoadSessionAsync(sessionId, scrollToEnd: false, quiet: false);
            button.IsEnabled = true;
        }

        private async Task CancelDraftAsync(string sessionId, string messageId, Button button)
        {
            button.IsEnabled = false;
            var res = await PlatformClient.CancelStudioDraftAsync(sessionId, messageId);
            if (_stopped) return;
            if (!res.Ok) ShowError(res.Message);
            if (_sessionId == sessionId) await LoadSessionAsync(sessionId, scrollToEnd: false, quiet: true);
            button.IsEnabled = true;
        }

        private async Task CancelRunAsync(string runId, Button button)
        {
            if (!FundDialogs.Confirm(Window.GetWindow(this), "Cancel backtest", "Cancel the running backtest of this draft? The run card is kept with status \"cancelled\".", "Cancel backtest", danger: true))
                return;
            button.IsEnabled = false;
            var res = await PlatformClient.CancelBacktestAsync(runId);
            if (_stopped) return;
            if (!res.Ok) ShowError(res.Message);
            if (_sessionId != null) await LoadSessionAsync(_sessionId, scrollToEnd: false, quiet: true);
            button.IsEnabled = true;
        }

        // =====================================================================================
        // Composer state
        // =====================================================================================

        private void UpdateComposer()
        {
            var hasSession = _sessionId != null && _detail != null;
            _input.IsEnabled = hasSession && !_busy;
            _send.IsEnabled = hasSession && !_busy;
            _standard.IsEnabled = !_busy;
            _premium.IsEnabled = !_busy;
            _newSession.IsEnabled = !_busy;
            _send.ToolTip = hasSession
                ? "Sends the message to the language model (OpenRouter) — costs money (ideation budget). Ctrl+Enter."
                : "Select a session or create one with \"+ New session\" first.";
            UpdateCharCount();
        }

        private void UpdateCharCount()
        {
            var n = (_input.Text ?? "").Length;
            _charCount.Text = n.ToString("N0", Inv) + " / " + PlatformClient.StudioMaxMessageChars.ToString("N0", Inv);
        }

        private void ShowWaiting()
        {
            var secs = (int)Math.Max(0, (DateTime.UtcNow - _waitStarted).TotalSeconds);
            SetSendStatus(AiContent.Glyph + " Writing a draft… " + secs.ToString(Inv) + " s (up to 90 s per call, a repair round adds one more call)", FundPalette.Ai);
        }

        private void SetSendStatus(string? text, Brush? brush)
        {
            _sendStatus.Text = text ?? "";
            if (brush != null) _sendStatus.Foreground = brush;
            _sendStatus.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
        }

        private void ShowError(string message)
        {
            _errorHost.Children.Clear();
            var block = ErrorBlock(message);
            block.Margin = new Thickness(0, 6, 10, 0);
            _errorHost.Children.Add(block);
        }

        private void ShowChatPlaceholder(string text)
        {
            _chat.Children.Clear();
            _statusHosts.Clear();
            _chat.Children.Add(FundUi.Text(text, 11.5, FundPalette.Faint));
        }

        private void ScrollChatToEnd()
            => Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => _chatScroll.ScrollToEnd()));

        // =====================================================================================
        // Spec diff (client-side, of the two JSON specs; plain text)
        // =====================================================================================

        /// <summary>Flattened path diff: "+" added, "−" removed, "~" changed (old → new). Arrays of scalars compare as one value.
        /// Also used by the discovery loop view (LoopView, SPEC §16.13) for the refinement diffs.</summary>
        internal static List<(string Sign, string Text)> SpecDiff(JsonElement before, JsonElement after)
        {
            var a = new SortedDictionary<string, string>(StringComparer.Ordinal);
            var b = new SortedDictionary<string, string>(StringComparer.Ordinal);
            Flatten(before, "", a);
            Flatten(after, "", b);
            var result = new List<(string, string)>();
            foreach (var key in a.Keys.Union(b.Keys).OrderBy(k => k, StringComparer.Ordinal))
            {
                var inA = a.TryGetValue(key, out var va);
                var inB = b.TryGetValue(key, out var vb);
                if (inA && inB)
                {
                    if (va != vb) result.Add(("~", key + ": " + va + " → " + vb));
                }
                else if (inB) result.Add(("+", key + ": " + vb));
                else result.Add(("−", key + ": " + va));
            }
            return result;
        }

        private static void Flatten(JsonElement e, string path, IDictionary<string, string> into)
        {
            switch (e.ValueKind)
            {
                case JsonValueKind.Object:
                {
                    var any = false;
                    foreach (var p in e.EnumerateObject())
                    {
                        any = true;
                        Flatten(p.Value, path.Length == 0 ? p.Name : path + "." + p.Name, into);
                    }
                    if (!any && path.Length > 0) into[path] = "{}";
                    break;
                }
                case JsonValueKind.Array:
                {
                    var items = e.EnumerateArray().ToList();
                    if (items.All(i => i.ValueKind != JsonValueKind.Object && i.ValueKind != JsonValueKind.Array))
                    {
                        into[path] = e.GetRawText();
                        break;
                    }
                    for (var i = 0; i < items.Count; i++) Flatten(items[i], path + "[" + i.ToString(Inv) + "]", into);
                    if (items.Count == 0) into[path] = "[]";
                    break;
                }
                default:
                    into[path.Length == 0 ? "(root)" : path] = e.GetRawText();
                    break;
            }
        }

        // =====================================================================================
        // Small builders
        // =====================================================================================

        /// <summary>The backend's fixed AI-interaction notice (GET session `ai_interaction_notice`): a neutral, always visible,
        /// non-dismissible block — collapsed only while the backend has not sent one (no stand-in text).</summary>
        private void ApplyNotice(string? text)
        {
            if (text == null) return;   // keep the last notice the backend sent (it is fixed text)
            _backendNotice = text;
            SetNotice(_sessionNotice, text);
            SetNotice(_composerNotice, text);
        }

        public static Border NoticeBlock()
        {
            var t = FundUi.Text("", 10.5, FundPalette.Dim);
            var b = new Border
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 5, 10, 6),
                Margin = new Thickness(0, 0, 0, 8),
                Child = t,
                Visibility = Visibility.Collapsed,
            };
            AutomationProperties.SetName(b, "Notice");
            return b;
        }

        /// <summary>The backend's fixed AI-interaction notice for views whose payload does not carry it (Paper tab,
        /// committee memo): read once from GET /v2/studio/sessions?limit=1 (`ai_interaction_notice`) — never a local text.</summary>
        public static async Task ApplyBackendNoticeAsync(Border block)
        {
            if (_backendNotice == null)
            {
                var res = await PlatformClient.ListStudioSessionsAsync(1);
                if (res.Ok && !string.IsNullOrWhiteSpace(res.Value?.AiInteractionNotice)) _backendNotice = res.Value!.AiInteractionNotice;
            }
            SetNotice(block, _backendNotice);
        }

        private static string? _backendNotice;

        public static void SetNotice(Border block, string? text)
        {
            if (block.Child is TextBlock t) t.Text = text ?? "";
            block.Visibility = string.IsNullOrWhiteSpace(text) ? Visibility.Collapsed : Visibility.Visible;
            AutomationProperties.SetName(block, "Notice: " + (text ?? ""));
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

        private static FrameworkElement Issue(string kind, StrategyIssue e, Brush brush)
        {
            var text = kind + (string.IsNullOrWhiteSpace(e.Path) ? "" : " at " + e.Path) + (e.Position.Length > 0 ? " (" + e.Position + ")" : "")
                       + (string.IsNullOrWhiteSpace(e.Code) ? "" : " [" + e.Code + "]") + ": " + (e.Message ?? "");
            var t = Line(text, 10.5, brush, mono: true);
            AutomationProperties.SetName(t, "Validator " + text);
            return t;
        }

        private static Border Chip(string text, Brush brush) => new()
        {
            BorderBrush = brush,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(6, 0, 6, 1),
            VerticalAlignment = VerticalAlignment.Center,
            Child = FundUi.Text(text, 9.5, brush, bold: true, wrap: false),
        };

        private static Expander MakeExpander(string header, string name)
        {
            var h = FundUi.Text(header, 10.5, FundPalette.Text, bold: true, wrap: false);
            var exp = new Expander { Header = h, Foreground = FundPalette.Text, Margin = new Thickness(0, 4, 0, 2), IsExpanded = false };
            AutomationProperties.SetName(exp, name);
            return exp;
        }

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

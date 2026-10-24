using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// JSON editor for one strategy (Strategy Platform stage 2, backend/strategy_platform/SPEC.md §7.12).
    /// - Monospace editor (AcceptsReturn, AcceptsTab, no word wrap). The text is DATA: nothing in it is
    ///   executed or rendered — the backend parses and validates it (§7.2/§7.5).
    /// - Live validation, debounced 600 ms → POST /v2/strategies/validate; error list with path, code,
    ///   message and line/column of parse errors (double-click / Enter moves the caret there).
    /// - Operator help from GET /v2/dsl/operators (operators, fields, external features).
    /// - Save: POST /v2/strategies (new) or PUT /v2/strategies/{id} with base_version (new version;
    ///   409 → the backend's message + "reload the latest version?"). Ctrl+S saves.
    /// Errors are shown verbatim; nothing is retried. Chrome: FundPopupWindow (borderless,
    /// full-screen safe, drag-to-restore — CLAUDE.md).
    /// </summary>
    internal sealed class StrategyEditorWindow : FundPopupWindow
    {
        /// <summary>Starting point of "New strategy" (a valid StrategySpec v1, SPEC §7.3).</summary>
        public const string NewStrategyTemplate =
@"{
  ""schema"": ""ufos.strategy/1"",
  ""name"": ""New strategy"",
  ""description"": ""Describe the idea, why it should work and when it should fail."",
  ""tags"": [""momentum""],
  ""category"": ""momentum"",
  ""universe"": {""preset"": ""liquid_etfs_crypto"", ""asset_classes"": [""equity""]},
  ""timeframe"": ""1Day"",
  ""parameters"": {
    ""lookback"": {""value"": 126, ""min"": 63, ""max"": 252, ""step"": 21, ""integer"": true, ""description"": ""Momentum lookback in trading days""}
  },
  ""features"": {
    ""mom"": {""op"": ""pct_change"", ""args"": [{""field"": ""close""}, {""param"": ""lookback""}]}
  },
  ""signal"": {""op"": ""cs_rank"", ""args"": [{""ref"": ""mom""}]},
  ""portfolio"": {
    ""mode"": ""long_only"",
    ""selection"": {""type"": ""top_n"", ""n"": 5},
    ""weighting"": ""equal"",
    ""max_weight"": 0.25,
    ""gross_exposure"": 1.0
  },
  ""rebalance"": {""frequency"": ""monthly""},
  ""risk"": {""stop_loss_pct"": null, ""take_profit_pct"": null, ""vol_target_annual"": null, ""max_drawdown_stop_pct"": null},
  ""execution"": {""order_type"": ""market"", ""timing"": ""next_open""},
  ""costs"": {""model"": ""paper_broker""}
}
";

        /// <summary>Raised after a successful save: (strategy id, new version).</summary>
        public event Action<string, int>? Saved;

        /// <summary>Id of the edited strategy; null while a new strategy has not been saved yet.</summary>
        public string? StrategyId { get; private set; }

        private int? _baseVersion;
        private string? _name;
        private bool _dirty;
        private bool _suppressDirty;
        private bool _forceClose;
        private bool _saving;
        private int _validateSeq;
        private DslCatalogue? _catalogue;

        private readonly DispatcherTimer _debounce;
        private readonly TextBox _editor;
        private readonly TextBlock _heading;
        private readonly TextBlock _status;
        private readonly ListBox _issues;
        private readonly TextBlock _issuesCaption;
        private readonly TextBox _helpFilter;
        private readonly StackPanel _helpList;
        private readonly Button _save;

        public StrategyEditorWindow(Window? owner, string? strategyId, string? name, int? baseVersion, string text)
            : base(owner, strategyId == null ? "New strategy" : "Strategy editor", 1120, 740, resizable: true)
        {
            StrategyId = strategyId;
            _name = name;
            _baseVersion = baseVersion;
            MinWidth = 820;
            MinHeight = 520;
            ShowInTaskbar = true;

            var root = new Grid { Margin = new Thickness(16, 12, 16, 14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });                       // heading
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });  // editor + help
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });                       // issues caption
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(150) });                   // issues
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });                       // buttons
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(300) });
            // scroll bars: FundPopupWindow merges the shared design (Styles/ScrollBars.xaml, both orientations)

            // ----- heading -----
            var head = new StackPanel { Margin = new Thickness(0, 0, 0, 8) };
            _heading = FundUi.Text("", 14, FundPalette.TextBright, bold: true, wrap: false);
            head.Children.Add(_heading);
            var hint = FundUi.Text("StrategySpec v1 (JSON). Validated live by the backend; nothing in the text runs code. "
                                   + "Saving an existing strategy creates a new version and returns it to draft.", 10.5, FundPalette.Faint);
            hint.Margin = new Thickness(0, 2, 0, 0);
            head.Children.Add(hint);
            Grid.SetColumnSpan(head, 2);
            root.Children.Add(head);

            // ----- editor -----
            _editor = new TextBox
            {
                Text = text,
                FontFamily = FundUi.Mono,
                FontSize = 12.5,
                AcceptsReturn = true,
                AcceptsTab = true,
                TextWrapping = TextWrapping.NoWrap,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                Foreground = FundPalette.Text,
                Background = FundPalette.WindowBg,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CaretBrush = FundPalette.TextBright,
                Padding = new Thickness(8, 6, 8, 6),
                IsUndoEnabled = true,
            };
            SpellCheck.SetIsEnabled(_editor, false);
            AutomationProperties.SetName(_editor, "Strategy JSON");
            _editor.TextChanged += Editor_TextChanged;
            Grid.SetRow(_editor, 1);
            root.Children.Add(_editor);

            // ----- operator help -----
            var help = new Grid();
            help.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            help.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            help.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            help.Children.Add(FundUi.Text("Operators", 10.5, FundPalette.Faint, bold: true));
            _helpFilter = FundUi.Input("", mono: true);
            _helpFilter.Margin = new Thickness(0, 4, 0, 6);
            _helpFilter.ToolTip = "Filter operators by name, category or description";
            AutomationProperties.SetName(_helpFilter, "Filter operators");
            _helpFilter.TextChanged += (_, _) => RenderHelp();
            Grid.SetRow(_helpFilter, 1);
            help.Children.Add(_helpFilter);
            _helpList = new StackPanel();
            var helpScroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = _helpList, Focusable = false };
            Grid.SetRow(helpScroll, 2);
            help.Children.Add(helpScroll);
            var helpBorder = new Border
            {
                Margin = new Thickness(10, 0, 0, 0),
                Padding = new Thickness(10, 8, 6, 8),
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Child = help,
            };
            AutomationProperties.SetName(helpBorder, "Operator help");
            Grid.SetRow(helpBorder, 1);
            Grid.SetColumn(helpBorder, 1);
            root.Children.Add(helpBorder);

            // ----- issues -----
            _issuesCaption = FundUi.Text("Validation", 10.5, FundPalette.Faint, bold: true);
            _issuesCaption.Margin = new Thickness(0, 10, 0, 4);
            Grid.SetRow(_issuesCaption, 2);
            Grid.SetColumnSpan(_issuesCaption, 2);
            root.Children.Add(_issuesCaption);

            _issues = new ListBox
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                Foreground = FundPalette.Text,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
            };
            ScrollViewer.SetHorizontalScrollBarVisibility(_issues, ScrollBarVisibility.Disabled);
            AutomationProperties.SetName(_issues, "Validation errors and warnings. Press Enter on a parse error to move the cursor to it.");
            _issues.MouseDoubleClick += (_, _) => GoToSelectedIssue();
            _issues.KeyDown += (_, e) =>
            {
                if (e.Key == Key.Enter) { e.Handled = true; GoToSelectedIssue(); }
            };
            Grid.SetRow(_issues, 3);
            Grid.SetColumnSpan(_issues, 2);
            root.Children.Add(_issues);

            // ----- buttons -----
            var bottom = new Grid { Margin = new Thickness(0, 10, 0, 0) };
            _status = FundUi.Text("", 11, FundPalette.Dim);
            _status.VerticalAlignment = VerticalAlignment.Center;
            _status.Margin = new Thickness(0, 0, 12, 0);
            AutomationProperties.SetLiveSetting(_status, AutomationLiveSetting.Polite);
            bottom.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            bottom.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            bottom.Children.Add(_status);
            var buttons = new StackPanel { Orientation = Orientation.Horizontal };
            var close = FundUi.MakeButton("Close");
            close.Click += (_, _) => Close();
            _save = FundUi.MakeButton(strategyId == null ? "Save new strategy" : "Save new version", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            _save.Margin = new Thickness(8, 0, 0, 0);
            _save.ToolTip = "Ctrl+S";
            _save.Click += async (_, _) => await SaveAsync();
            buttons.Children.Add(close);
            buttons.Children.Add(_save);
            Grid.SetColumn(buttons, 1);
            bottom.Children.Add(buttons);
            Grid.SetRow(bottom, 4);
            Grid.SetColumnSpan(bottom, 2);
            root.Children.Add(bottom);

            SetBody(root);
            UpdateHeading();

            _debounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(600) };
            _debounce.Tick += async (_, _) =>
            {
                _debounce.Stop();
                await ValidateAsync();
            };

            PreviewKeyDown += async (_, e) =>
            {
                if (e.Key == Key.S && Keyboard.Modifiers == ModifierKeys.Control)
                {
                    e.Handled = true;
                    await SaveAsync();
                }
            };
            Loaded += async (_, _) =>
            {
                _editor.Focus();
                _editor.CaretIndex = 0;
                _ = ValidateAsync();
                await LoadOperatorsAsync();
            };
            Closing += (_, e) =>
            {
                if (_forceClose || !_dirty) return;
                if (!FundDialogs.Confirm(this, "Discard changes?", "The editor has unsaved changes. Close it and discard them?", "Discard and close", danger: true))
                    e.Cancel = true;
            };
            Closed += (_, _) => _debounce.Stop();
        }

        /// <summary>Closes without the unsaved-changes question (the Hedge Fund window is closing).</summary>
        public void CloseWithoutPrompt()
        {
            _forceClose = true;
            Close();
        }

        private void UpdateHeading()
        {
            _heading.Text = StrategyId == null
                ? "New strategy" + (_dirty ? " •" : "")
                : (_name ?? "Strategy " + StrategyId) + " · editing on top of v" + (_baseVersion?.ToString() ?? "?") + (_dirty ? " •" : "");
            _save.Content = StrategyId == null ? "Save new strategy" : "Save new version";
        }

        private void Editor_TextChanged(object sender, TextChangedEventArgs e)
        {
            if (_suppressDirty) return;
            if (!_dirty)
            {
                _dirty = true;
                UpdateHeading();
            }
            _debounce.Stop();
            _debounce.Start();
        }

        // =====================================================================================
        // Validation
        // =====================================================================================

        private async Task ValidateAsync()
        {
            var seq = ++_validateSeq;
            var text = _editor.Text ?? "";
            var bytes = Encoding.UTF8.GetByteCount(text);
            if (bytes > PlatformClient.MaxStrategyTextBytes)
            {
                ShowIssues(null, null);
                SetStatus("The text is " + bytes.ToString("N0") + " bytes — strategies may be at most 256 KB (262,144 bytes). Not validated.", FundPalette.Kill);
                return;
            }
            SetStatus("Validating…", FundPalette.Dim);
            var result = await PlatformClient.ValidateStrategyAsync(text);
            if (seq != _validateSeq) return; // a newer text is being validated
            if (!result.Ok || result.Value == null)
            {
                ShowIssues(null, null);
                SetStatus(result.Ok ? "Unexpected response from the backend (validate)." : result.Message, FundPalette.Kill);
                return;
            }
            var v = result.Value;
            ShowIssues(v.Errors, v.Warnings);
            var errors = v.Errors?.Count ?? 0;
            var warnings = v.Warnings?.Count ?? 0;
            if (v.Ok && errors == 0)
                SetStatus("Valid" + (warnings > 0 ? " · " + warnings + " warning" + (warnings == 1 ? "" : "s") : "") + (_dirty ? " · unsaved" : ""), FundPalette.Long);
            else
                SetStatus(errors + " error" + (errors == 1 ? "" : "s") + (warnings > 0 ? " · " + warnings + " warning" + (warnings == 1 ? "" : "s") : "") + " — only valid strategies can be saved.", FundPalette.Kill);
        }

        private void ShowIssues(List<StrategyIssue>? errors, List<StrategyIssue>? warnings)
        {
            _issues.Items.Clear();
            var all = (errors ?? new List<StrategyIssue>()).Select(e => (Issue: e, Error: true))
                .Concat((warnings ?? new List<StrategyIssue>()).Select(w => (Issue: w, Error: false))).ToList();
            _issuesCaption.Text = all.Count == 0 ? "Validation" : "Validation · " + all.Count + " item" + (all.Count == 1 ? "" : "s");
            foreach (var (issue, isError) in all)
            {
                var grid = new Grid();
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(64) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(220) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                var kind = FundUi.Text(isError ? "error" : "warning", 10.5, isError ? FundPalette.Kill : FundPalette.Risk, bold: true, wrap: false);
                var code = FundUi.Text(issue.Code ?? "—", 11, FundPalette.Text, mono: true, wrap: false);
                var where = FundUi.Text(issue.Position.Length > 0 ? issue.Position : (issue.Path ?? "—"), 11, FundPalette.Dim, mono: true, wrap: false);
                where.ToolTip = issue.Path;
                var message = FundUi.Text(issue.Message ?? "", 11, FundPalette.Text);
                Grid.SetColumn(code, 1);
                Grid.SetColumn(where, 2);
                Grid.SetColumn(message, 3);
                grid.Children.Add(kind);
                grid.Children.Add(code);
                grid.Children.Add(where);
                grid.Children.Add(message);
                var item = new ListBoxItem { Content = grid, Tag = issue, Padding = new Thickness(6, 2, 6, 2) };
                if (issue.Line is int) item.ToolTip = "Double-click or press Enter to move the cursor to " + issue.Position;
                AutomationProperties.SetName(item, (isError ? "Error" : "Warning") + " " + (issue.Code ?? "") + " at " +
                    (issue.Position.Length > 0 ? issue.Position : issue.Path ?? "unknown position") + ": " + (issue.Message ?? ""));
                _issues.Items.Add(item);
            }
        }

        /// <summary>Moves the caret to the line/column of a parse error (1-based, as the backend reports them).</summary>
        private void GoToSelectedIssue()
        {
            if (_issues.SelectedItem is not ListBoxItem { Tag: StrategyIssue issue } || issue.Line is not int line) return;
            if (_editor.LineCount <= 0) return;
            var lineIndex = Math.Clamp(line - 1, 0, _editor.LineCount - 1);
            var start = _editor.GetCharacterIndexFromLineIndex(lineIndex);
            if (start < 0) return;
            var length = _editor.GetLineLength(lineIndex);
            var column = Math.Clamp((issue.Column ?? 1) - 1, 0, Math.Max(0, length));
            _editor.Focus();
            _editor.Select(Math.Min(start + column, _editor.Text.Length), 0);
            _editor.ScrollToLine(lineIndex);
        }

        private void SetStatus(string text, Brush brush)
        {
            _status.Text = text;
            _status.Foreground = brush;
        }

        // =====================================================================================
        // Operator help
        // =====================================================================================

        private async Task LoadOperatorsAsync()
        {
            _helpList.Children.Clear();
            _helpList.Children.Add(FundUi.Text("Loading operators…", 11, FundPalette.Faint));
            var result = await PlatformClient.GetOperatorsAsync();
            if (!result.Ok || result.Value == null)
            {
                _helpList.Children.Clear();
                var err = FundUi.Text(result.Ok ? "Unexpected response from the backend (/v2/dsl/operators)." : result.Message, 11, FundPalette.Kill);
                _helpList.Children.Add(err);
                return;
            }
            _catalogue = result.Value;
            RenderHelp();
        }

        private void RenderHelp()
        {
            if (_catalogue == null) return;
            _helpList.Children.Clear();
            var filter = (_helpFilter.Text ?? "").Trim();
            bool Match(params string?[] values) => filter.Length == 0 ||
                values.Any(v => v != null && v.Contains(filter, StringComparison.OrdinalIgnoreCase));

            var ops = (_catalogue.Operators ?? new List<DslOperator>()).Where(o => Match(o.Name, o.Category, o.Signature, o.Description)).ToList();
            foreach (var group in ops.GroupBy(o => o.Category ?? "other"))
            {
                var caption = FundUi.Text(FundFormat.Label(group.Key), 10.5, FundPalette.Faint, bold: true);
                caption.Margin = new Thickness(0, 8, 0, 2);
                _helpList.Children.Add(caption);
                foreach (var op in group)
                {
                    var sig = FundUi.Text(string.IsNullOrWhiteSpace(op.Signature) ? op.Name : op.Signature, 11.5, FundPalette.TextBright, mono: true);
                    sig.Margin = new Thickness(0, 3, 0, 0);
                    _helpList.Children.Add(sig);
                    if (!string.IsNullOrWhiteSpace(op.Description))
                        _helpList.Children.Add(FundUi.Text(op.Description, 10.5, FundPalette.Dim));
                }
            }

            var fields = HelpItems(_catalogue.Fields).Where(i => Match(i.Name, i.Description)).ToList();
            if (fields.Count > 0)
            {
                var caption = FundUi.Text("Fields · {\"field\": name}", 10.5, FundPalette.Faint, bold: true);
                caption.Margin = new Thickness(0, 10, 0, 2);
                _helpList.Children.Add(caption);
                foreach (var (name, description) in fields)
                {
                    _helpList.Children.Add(FundUi.Text(name, 11.5, FundPalette.TextBright, mono: true));
                    if (!string.IsNullOrWhiteSpace(description)) _helpList.Children.Add(FundUi.Text(description, 10.5, FundPalette.Dim));
                }
            }

            var ext = HelpItems(_catalogue.Ext).Where(i => Match(i.Name, i.Description)).ToList();
            if (ext.Count > 0)
            {
                var caption = FundUi.Text("External features · {\"ext\": name}", 10.5, FundPalette.Faint, bold: true);
                caption.Margin = new Thickness(0, 10, 0, 2);
                _helpList.Children.Add(caption);
                foreach (var (name, description) in ext)
                {
                    var n = FundUi.Text(name, 11.5, FundPalette.TextBright, mono: true);
                    n.Margin = new Thickness(0, 3, 0, 0);
                    _helpList.Children.Add(n);
                    if (!string.IsNullOrWhiteSpace(description)) _helpList.Children.Add(FundUi.Text(description, 10.5, FundPalette.Dim));
                }
            }

            if (_helpList.Children.Count == 0)
                _helpList.Children.Add(FundUi.Text(filter.Length > 0 ? "Nothing matches \"" + filter + "\"." : "The backend lists no operators.", 11, FundPalette.Faint));
        }

        /// <summary>Fields / ext entries: strings, or objects with name (+ description/doc).</summary>
        private static List<(string Name, string? Description)> HelpItems(JsonElement? element)
        {
            var list = new List<(string, string?)>();
            if (element is not JsonElement e) return list;
            if (e.ValueKind == JsonValueKind.Object)
            {
                // {name: description | {...}}
                foreach (var p in e.EnumerateObject())
                {
                    var desc = p.Value.ValueKind == JsonValueKind.String ? p.Value.GetString()
                             : p.Value.ValueKind == JsonValueKind.Object ? (StrategyJson.Str(p.Value, "description") ?? StrategyJson.Str(p.Value, "doc")) : null;
                    list.Add((p.Name, desc));
                }
                return list;
            }
            if (e.ValueKind != JsonValueKind.Array) return list;
            foreach (var item in e.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.String) list.Add((item.GetString() ?? "", null));
                else if (item.ValueKind == JsonValueKind.Object)
                {
                    var name = StrategyJson.Str(item, "name") ?? "?";
                    var desc = StrategyJson.Str(item, "description") ?? StrategyJson.Str(item, "doc");
                    if (item.TryGetProperty("point_in_time", out var pit) && pit.ValueKind == JsonValueKind.False)
                        desc = (desc ?? "") + " (not point-in-time — cannot be used)";
                    list.Add((name, desc));
                }
            }
            return list;
        }

        // =====================================================================================
        // Save / reload
        // =====================================================================================

        private async Task SaveAsync()
        {
            if (_saving) return;
            var text = _editor.Text ?? "";
            if (Encoding.UTF8.GetByteCount(text) > PlatformClient.MaxStrategyTextBytes)
            {
                SetStatus("The text is larger than 256 KB — not saved.", FundPalette.Kill);
                return;
            }
            _saving = true;
            _save.IsEnabled = false;
            SetStatus("Saving…", FundPalette.Dim);
            try
            {
                var isNew = StrategyId == null;
                PlatformResult<StrategyCreated> result;
                if (isNew) result = await PlatformClient.CreateStrategyAsync(text, "user");
                else if (_baseVersion is int baseVersion) result = await PlatformClient.UpdateStrategyAsync(StrategyId!, text, baseVersion);
                else
                {
                    SetStatus("The version this text is based on is unknown — reload the strategy before saving.", FundPalette.Kill);
                    return;
                }

                if (result.Ok && result.Value != null && result.Value.Version is int version)
                {
                    var id = string.IsNullOrEmpty(result.Value.Id) ? StrategyId : result.Value.Id;
                    if (string.IsNullOrEmpty(id))
                    {
                        SetStatus("Saved, but the backend returned no strategy id — refresh the library.", FundPalette.Risk);
                        return;
                    }
                    StrategyId = id;
                    _baseVersion = version;
                    string? nameProblem = null;
                    if (isNew) _name = ReadName(text, out nameProblem);
                    _dirty = false;
                    UpdateHeading();
                    SetStatus((result.Value.Unchanged
                        ? "No changes to the content — still version " + version + "."
                        : "Saved as version " + version + ".") + (nameProblem == null ? "" : " " + nameProblem),
                        nameProblem == null ? FundPalette.Long : FundPalette.Risk);
                    Saved?.Invoke(id, version);
                    return;
                }
                if (result.Ok)
                {
                    SetStatus("Unexpected response from the backend (save).", FundPalette.Kill);
                    return;
                }

                var validation = PlatformClient.ReadValidation(result);
                if (validation != null) ShowIssues(validation.Errors, validation.Warnings);
                SetStatus(result.Message, FundPalette.Kill);

                if (result.Status == PlatformCallStatus.Conflict && !isNew)
                {
                    var reload = FundDialogs.Confirm(this, "Not saved", result.Message
                        + "\n\nReload the latest version into the editor? The text in this editor will be replaced (copy it first if you need it).",
                        "Reload latest version", danger: true);
                    if (reload) await ReloadAsync();
                }
            }
            finally
            {
                _saving = false;
                _save.IsEnabled = true;
            }
        }

        private async Task ReloadAsync()
        {
            if (StrategyId == null) return;
            SetStatus("Loading the latest version…", FundPalette.Dim);
            var detail = await PlatformClient.GetStrategyAsync(StrategyId);
            if (!detail.Ok || detail.Value?.Strategy?.CurrentVersion is not int current)
            {
                SetStatus(detail.Ok ? "The backend returned this strategy without a version." : detail.Message, FundPalette.Kill);
                return;
            }
            var version = await PlatformClient.GetStrategyVersionAsync(StrategyId, current);
            if (!version.Ok || version.Value == null)
            {
                SetStatus(version.Ok ? "Unexpected response from the backend (strategy version)." : version.Message, FundPalette.Kill);
                return;
            }
            _suppressDirty = true;
            try
            {
                _editor.Text = version.Value.SpecText ?? (version.Value.Spec is JsonElement spec ? JsonSerializer.Serialize(spec, new JsonSerializerOptions { WriteIndented = true }) : "");
            }
            finally
            {
                _suppressDirty = false;
            }
            _baseVersion = current;
            _name = detail.Value.Strategy.Name ?? _name;
            _dirty = false;
            UpdateHeading();
            SetStatus("Reloaded version " + current + ".", FundPalette.Dim);
            await ValidateAsync();
        }

        /// <summary>The "name" of the saved text, for the heading. A text the backend accepted is valid JSON;
        /// if it still cannot be read, <paramref name="problem"/> says so (shown in the status line).</summary>
        private static string? ReadName(string text, out string? problem)
        {
            problem = null;
            try
            {
                using var doc = JsonDocument.Parse(text);
                return doc.RootElement.ValueKind == JsonValueKind.Object ? StrategyJson.Str(doc.RootElement, "name") : null;
            }
            catch (JsonException ex)
            {
                problem = "(The name could not be read from the text for the heading: " + ex.Message + ")";
                return null;
            }
        }
    }
}

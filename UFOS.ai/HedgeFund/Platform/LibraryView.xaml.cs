using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Hedge Fund → "Library" (Strategy Platform stage 2, backend/strategy_platform/SPEC.md §7.12).
    /// - Board: one column per lifecycle state (empty states collapse to a narrow labelled strip),
    ///   one card per strategy with origin/version and the chips "external text · unverified" /
    ///   "AI involvement unknown" taken from the system-owned envelope flags (§7.6).
    /// - Alpha library: table of the built-in strategies (name, family, reference, holding days).
    /// - Detail pane: summary texts, signal expression, parameters, versions, transitions,
    ///   validation warnings, claims of imported files (unverified) and the actions Edit /
    ///   Duplicate as draft / Move to… / Preview signal / Run backtest… / Backtest runs / Export….
    /// Strategy texts are untrusted data: plain TextBlocks only, never markup or links.
    /// Backend errors are shown verbatim (the `error` field); nothing is retried or replaced.
    /// No timers: the view loads when it is shown and on Refresh / after an action.
    /// </summary>
    public partial class LibraryView : UserControl
    {
        private const string LogModule = "HedgeFund";
        private const double CardColumnMinWidth = 190;
        private const double EmptyColumnWidth = 34;
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        private const string ImportConfirmText =
            "Imported strategies are untrusted: they start as draft, their claims are shown as unverified, and nothing in them runs code.";

        private List<StrategySummary> _strategies = new();
        private readonly Dictionary<string, Button> _cards = new();
        private readonly Dictionary<string, LibraryMeta> _libraryMeta = new();   // key: id + "@" + version
        private readonly List<StrategyEditorWindow> _editors = new();
        private string? _selectedId;
        private StrategyDetail? _detail;
        private StrategyVersionDetail? _version;
        private int _listSeq;
        private int _detailSeq;
        private bool _busy;
        private bool _alphaLoading;
        private int _nonEmptyColumns;
        private int _emptyColumns;

        public LibraryView()
        {
            InitializeComponent();
            BuildAlphaHeader();
            ShowDetailPlaceholder("Select a strategy on the board or in the alpha library.");
            // The board's width is computed from the viewport; while the board is collapsed (Alpha library tab,
            // or the Library page hidden) the viewport is not measured, so recompute once it is visible again.
            BoardScroll.IsVisibleChanged += (_, _) => { if (BoardScroll.IsVisible) ScheduleBoardLayout(); };
            // Stage 4: a finished validation/holdout run or a new gate exception can change state, chips and trial count.
            Loaded += (_, _) => ValidationReportWindow.StrategyStateMayHaveChanged += OnValidationStateChanged;
            Unloaded += (_, _) => ValidationReportWindow.StrategyStateMayHaveChanged -= OnValidationStateChanged;
        }

        private async void OnValidationStateChanged(string? strategyId)
        {
            if (!IsLoaded) return;
            await RefreshAsync(_selectedId ?? strategyId);
        }

        private Window? OwnerWindow => Window.GetWindow(this);

        /// <summary>Called by HedgeFundWindow whenever the Library entry of the navigation rail is selected.</summary>
        public Task ActivateAsync() => RefreshAsync(_selectedId);

        /// <summary>Strategy Studio "Open in Library" (SPEC §13.3): reloads the board and selects <paramref name="strategyId"/>.</summary>
        public Task ShowStrategyAsync(string strategyId) => RefreshAsync(strategyId);

        /// <summary>Closes the editors opened from this view without asking (the Hedge Fund window is closing).</summary>
        public void CloseEditors()
        {
            foreach (var editor in _editors.ToList())
            {
                editor.CloseWithoutPrompt();
            }
            _editors.Clear();
        }

        // =====================================================================================
        // List / board
        // =====================================================================================

        private async Task RefreshAsync(string? selectId)
        {
            var seq = ++_listSeq;
            StatusText.Text = "Loading strategies…";
            RefreshButton.IsEnabled = false;
            try
            {
                var result = await PlatformClient.GetStrategiesAsync();
                if (seq != _listSeq) return;
                if (!result.Ok || result.Value == null)
                {
                    // No stale list shown as if it were current: clear and say what happened.
                    _strategies = new List<StrategySummary>();
                    ShowError(result.Ok ? "Unexpected response from the backend (/v2/strategies)." : result.Message);
                    StatusText.Text = "Strategy list not loaded.";
                    RenderBoard();
                    RenderAlphaRows();
                    return;
                }
                HideError();
                _strategies = result.Value.Strategies ?? new List<StrategySummary>();
                if (result.Value.LibraryStatus is { Ok: false } seed)
                {
                    // The library could not be (fully) seeded at backend start — say so, verbatim.
                    var detail = seed.Errors is { Count: > 0 } errs ? " — " + string.Join("; ", errs.Select(FundFormat.Plain)) : "";
                    ShowError("Alpha library: " + (seed.Message ?? "seeding failed") + detail);
                }
                foreach (var s in _strategies)
                {
                    if (s.Library != null && s.CurrentVersion is int v) _libraryMeta[s.Id + "@" + v] = s.Library;
                }
                StatusText.Text = _strategies.Count + " strategies · loaded " + DateTime.Now.ToString("HH:mm:ss", Inv);
                RenderBoard();
                RenderAlphaRows();
                if (AlphaTab.IsChecked == true) _ = LoadLibraryMetaAsync();

                var target = selectId != null && _strategies.Any(s => s.Id == selectId) ? selectId : null;
                if (target != null) await SelectAsync(target);
                else if (_selectedId != null)
                {
                    _selectedId = null;
                    _detail = null;
                    _version = null;
                    ShowDetailPlaceholder("Select a strategy on the board or in the alpha library.");
                }
            }
            finally
            {
                if (seq == _listSeq) RefreshButton.IsEnabled = true;
            }
        }

        private void RenderBoard()
        {
            BoardGrid.Children.Clear();
            BoardGrid.ColumnDefinitions.Clear();
            _cards.Clear();
            _nonEmptyColumns = 0;
            _emptyColumns = 0;

            var known = new HashSet<string>(StrategyStates.All);
            var states = StrategyStates.All.ToList();
            // A state the backend knows but this app does not is still shown (at the end), never hidden.
            foreach (var extra in _strategies.Select(s => s.State ?? "").Where(s => s.Length > 0 && !known.Contains(s)).Distinct())
                states.Add(extra);

            for (var i = 0; i < states.Count; i++)
            {
                var state = states[i];
                var items = _strategies.Where(s => string.Equals(s.State, state, StringComparison.OrdinalIgnoreCase))
                                       .OrderBy(s => s.Name ?? "", StringComparer.OrdinalIgnoreCase).ToList();
                var column = new ColumnDefinition
                {
                    Width = items.Count == 0 ? new GridLength(EmptyColumnWidth) : new GridLength(1, GridUnitType.Star),
                    MinWidth = items.Count == 0 ? EmptyColumnWidth : CardColumnMinWidth,
                };
                BoardGrid.ColumnDefinitions.Add(column);
                FrameworkElement content = items.Count == 0 ? BuildEmptyColumn(state) : BuildColumn(state, items);
                Grid.SetColumn(content, i);
                BoardGrid.Children.Add(content);
                if (items.Count == 0) _emptyColumns++; else _nonEmptyColumns++;
            }
            UpdateBoardWidth();
            HighlightSelectedCard();
        }

        private FrameworkElement BuildEmptyColumn(string state)
        {
            var label = FundUi.Text(StrategyStates.Label(state) + " · 0", 10.5, FundPalette.Faint, wrap: false);
            label.LayoutTransform = new RotateTransform(90);
            label.HorizontalAlignment = HorizontalAlignment.Center;
            label.Margin = new Thickness(0, 6, 0, 0);
            var border = new Border
            {
                Margin = new Thickness(0, 0, 6, 0),
                BorderBrush = FundPalette.Hairline,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Child = label,
                ToolTip = "No strategy in state \"" + state + "\"",
            };
            AutomationProperties.SetName(border, StrategyStates.Label(state) + ": no strategies");
            return border;
        }

        private FrameworkElement BuildColumn(string state, List<StrategySummary> items)
        {
            var stack = new StackPanel { Margin = new Thickness(0, 0, 0, 4) };
            foreach (var s in items) stack.Children.Add(BuildCard(s));

            var header = FundUi.Text(StrategyStates.Label(state) + " · " + items.Count, 11, FundPalette.Text, bold: true, wrap: false);
            header.Margin = new Thickness(2, 0, 0, 8);
            var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack, Focusable = false };
            var layout = new Grid();
            layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            Grid.SetRow(scroll, 1);
            layout.Children.Add(header);
            layout.Children.Add(scroll);
            var border = new Border
            {
                Margin = new Thickness(0, 0, 6, 0),
                Padding = new Thickness(8, 8, 6, 6),
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Child = layout,
            };
            AutomationProperties.SetName(border, StrategyStates.Label(state) + ": " + items.Count + " strategies");
            return border;
        }

        private Button BuildCard(StrategySummary s)
        {
            var stack = new StackPanel();
            var name = FundUi.Text(DisplayName(s), 12, FundPalette.TextBright, bold: true);
            name.MaxHeight = 34;
            name.TextTrimming = TextTrimming.CharacterEllipsis;
            stack.Children.Add(name);
            var meta = FundUi.Text((s.Category ?? "—") + " · v" + (s.CurrentVersion?.ToString(Inv) ?? "?"), 10.5, FundPalette.Dim, mono: true, wrap: false);
            meta.Margin = new Thickness(0, 2, 0, 4);
            stack.Children.Add(meta);
            var chips = new WrapPanel();
            foreach (var chip in ChipsFor(s.Origin, s.ExternalText, s.AiGenerated, s.Valid)) chips.Children.Add(chip);
            foreach (var chip in FamilyChipsFor(s)) chips.Children.Add(chip);
            stack.Children.Add(chips);
            if (FundUi.FixtureLine(s.Fixture, s.FixtureNote, 10.5, new Thickness(0, 4, 0, 0)) is TextBlock fixture) stack.Children.Add(fixture);

            var button = CardButton(stack);
            button.Margin = new Thickness(0, 0, 0, 6);
            button.Tag = s.Id;
            button.ToolTip = DisplayName(s);
            AutomationProperties.SetName(button, CardAutomationName(s));
            button.Click += async (_, _) => await SelectAsync(s.Id);
            _cards[s.Id] = button;
            return button;
        }

        private static string CardAutomationName(StrategySummary s)
        {
            var parts = new List<string> { DisplayName(s), "state " + (s.State ?? "unknown"), "origin " + (s.Origin ?? "unknown"), "version " + (s.CurrentVersion?.ToString(Inv) ?? "unknown") };
            if (s.ExternalText) parts.Add("external text, unverified");
            if (StrategyJson.IsUnknown(s.AiGenerated)) parts.Add("AI involvement unknown");
            if (s.Valid == false) parts.Add("invalid");
            if (s.Contaminated == true) parts.Add("contaminated");
            if (!string.IsNullOrWhiteSpace(s.HoldoutFailedUntil)) parts.Add("holdout failed, locked until " + s.HoldoutFailedUntil);
            if (s.HasException == true) parts.Add("passed a gate by exception");
            return string.Join(", ", parts);
        }

        private static string DisplayName(StrategySummary s) => string.IsNullOrWhiteSpace(s.Name) ? "(unnamed strategy " + s.Id + ")" : s.Name!;

        /// <summary>Origin chip + envelope chips (§7.6/§7.12).</summary>
        private static IEnumerable<Border> ChipsFor(string? origin, bool externalText, JsonElement? aiGenerated, bool? valid)
        {
            if (!string.IsNullOrWhiteSpace(origin))
            {
                var brush = origin switch
                {
                    "library" => FundPalette.Focus,
                    "import" => FundPalette.Risk,
                    _ => FundPalette.Neutral,
                };
                yield return Chip(origin, brush, "Origin: " + origin);
            }
            if (externalText) yield return Chip("external text · unverified", FundPalette.Risk,
                "The texts of this strategy come from an external file — shown as plain text, not verified.");
            if (StrategyJson.IsUnknown(aiGenerated)) yield return Chip("AI involvement unknown", FundPalette.Risk,
                "Nobody has confirmed whether this strategy was written with AI help.");
            else if (StrategyJson.IsTrue(aiGenerated)) yield return Chip("AI-assisted", FundPalette.Ai,
                "Marked as created with AI involvement.");
            if (valid == false) yield return Chip("invalid", FundPalette.Kill, "The current version does not validate.");
        }

        /// <summary>Stage 4 family chips (SPEC §9.7): contaminated, holdout failed (until …), exception.</summary>
        private static IEnumerable<Border> FamilyChipsFor(StrategySummary s)
        {
            if (!string.IsNullOrWhiteSpace(s.HoldoutFailedUntil))
                yield return Chip("holdout failed (until " + s.HoldoutFailedUntil + ")", FundPalette.Kill,
                    "This strategy family failed the holdout. It is locked until " + s.HoldoutFailedUntil
                    + ", when 12 new unseen months exist; then it may be re-tested once on exactly those months.");
            else if (s.Contaminated == true)
                yield return Chip("contaminated", FundPalette.Risk,
                    "A member of this strategy family has already seen data inside the locked holdout window. "
                    + "The holdout can only be opened once 12 unseen months exist, and then uses only those months.");
            if (s.HasException == true)
                yield return Chip("exception", FundPalette.Risk,
                    "The latest validation passed at least one gate by a documented, permanent exception (shown in the report).");
        }

        private static Border Chip(string text, Brush brush, string tooltip)
        {
            var label = FundUi.Text(text, 9.5, brush, wrap: false);
            var chip = new Border
            {
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(999),
                Padding = new Thickness(6, 0, 6, 1),
                Margin = new Thickness(0, 2, 4, 0),
                Child = label,
                ToolTip = tooltip,
            };
            AutomationProperties.SetName(chip, text);
            return chip;
        }

        /// <summary>A focusable, keyboard-activatable card/row (Enter/Space), drawn as a panel.</summary>
        private static Button CardButton(UIElement content, bool row = false)
        {
            var bd = new FrameworkElementFactory(typeof(Border), "bd");
            bd.SetValue(Border.CornerRadiusProperty, new CornerRadius(row ? 4 : 6));
            bd.SetValue(Border.BackgroundProperty, new TemplateBindingExtension(Control.BackgroundProperty));
            bd.SetValue(Border.BorderBrushProperty, new TemplateBindingExtension(Control.BorderBrushProperty));
            bd.SetValue(Border.BorderThicknessProperty, new TemplateBindingExtension(Control.BorderThicknessProperty));
            bd.SetValue(Border.PaddingProperty, new TemplateBindingExtension(Control.PaddingProperty));
            bd.SetValue(UIElement.SnapsToDevicePixelsProperty, true);
            var cp = new FrameworkElementFactory(typeof(ContentPresenter));
            cp.SetValue(FrameworkElement.HorizontalAlignmentProperty, HorizontalAlignment.Stretch);
            bd.AppendChild(cp);
            var template = new ControlTemplate(typeof(Button)) { VisualTree = bd };
            var hover = new Trigger { Property = UIElement.IsMouseOverProperty, Value = true };
            hover.Setters.Add(new Setter(Border.BackgroundProperty, FundPalette.ButtonBg, "bd"));
            template.Triggers.Add(hover);
            var focus = new Trigger { Property = UIElement.IsKeyboardFocusedProperty, Value = true };
            focus.Setters.Add(new Setter(Border.BorderBrushProperty, FundPalette.Focus, "bd"));
            template.Triggers.Add(focus);

            return new Button
            {
                Template = template,
                Content = content,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Background = row ? Brushes.Transparent : FundPalette.WindowBg,
                BorderBrush = row ? Brushes.Transparent : FundPalette.Hairline,
                BorderThickness = new Thickness(1),
                Padding = row ? new Thickness(12, 4, 12, 4) : new Thickness(9, 7, 9, 7),
                Cursor = Cursors.Hand,
            };
        }

        private void HighlightSelectedCard()
        {
            foreach (var (id, card) in _cards)
            {
                card.BorderBrush = id == _selectedId ? FundPalette.Focus : FundPalette.Hairline;
            }
            foreach (var child in AlphaRows.Children.OfType<Button>())
            {
                child.Background = (child.Tag as string) == _selectedId ? FundPalette.ButtonBg : Brushes.Transparent;
            }
        }

        private void BoardScroll_SizeChanged(object sender, SizeChangedEventArgs e) => UpdateBoardWidth();

        /// <summary>
        /// Star columns fill the visible width; below the minimum the board scrolls horizontally.
        /// Root cause of the "board starts far right" bug (2026-10-06): BoardGrid.Width was computed while the board
        /// was collapsed (Alpha library tab) or from a stale viewport, and nothing recomputed it when the board was
        /// shown again; a Grid with an explicit Width narrower than its slot and the default Stretch alignment is
        /// CENTRED, leaving an empty band on the left. Now: BoardGrid is left-aligned (XAML), the width is only
        /// computed from a measured viewport, and it is recomputed (scroll reset to the left) whenever the board
        /// becomes visible.
        /// </summary>
        private void UpdateBoardWidth()
        {
            var needed = _nonEmptyColumns * (CardColumnMinWidth + 6) + _emptyColumns * (EmptyColumnWidth + 6);
            if (!BoardScroll.IsVisible) return; // no valid viewport while collapsed — recomputed on IsVisibleChanged
            var viewport = BoardScroll.ViewportWidth > 0 ? BoardScroll.ViewportWidth : BoardScroll.ActualWidth;
            BoardGrid.Width = Math.Max(needed, viewport > 0 ? viewport : needed);
        }

        /// <summary>Recomputes the board width after the pending layout pass and scrolls the board back to its left edge.</summary>
        private void ScheduleBoardLayout()
        {
            Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(() =>
            {
                UpdateBoardWidth();
                BoardScroll.ScrollToHorizontalOffset(0);
            }));
        }

        // =====================================================================================
        // Alpha library table
        // =====================================================================================

        private static readonly (string Header, GridLength Width)[] AlphaColumns =
        {
            ("Name", new GridLength(1.4, GridUnitType.Star)),
            ("Family", new GridLength(120)),
            ("Reference", new GridLength(1.2, GridUnitType.Star)),
            ("Holding days", new GridLength(90)),
        };

        private Grid AlphaGrid()
        {
            var grid = new Grid();
            foreach (var (_, width) in AlphaColumns) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = width });
            return grid;
        }

        private void BuildAlphaHeader()
        {
            var header = AlphaGrid();
            for (var i = 0; i < AlphaColumns.Length; i++)
            {
                var t = FundUi.Text(AlphaColumns[i].Header, 10.5, FundPalette.Faint, bold: true, wrap: false);
                t.Margin = new Thickness(0, 0, 8, 0);
                Grid.SetColumn(t, i);
                header.Children.Add(t);
            }
            AlphaHeader.Children.Clear();
            AlphaHeader.Children.Add(header);
        }

        private void RenderAlphaRows()
        {
            AlphaRows.Children.Clear();
            var library = _strategies.Where(s => s.IsLibrary).OrderBy(s => s.Name ?? "", StringComparer.OrdinalIgnoreCase).ToList();
            if (library.Count == 0)
            {
                var none = FundUi.Text(_strategies.Count == 0 ? "No strategies loaded." : "No library strategies.", 11.5, FundPalette.Faint);
                none.Margin = new Thickness(12, 6, 12, 6);
                AlphaRows.Children.Add(none);
                return;
            }
            foreach (var s in library)
            {
                var meta = s.CurrentVersion is int v && _libraryMeta.TryGetValue(s.Id + "@" + v, out var m) ? m : null;
                var grid = AlphaGrid();
                var cells = new[]
                {
                    DisplayName(s),
                    meta?.Family ?? (meta == null ? "…" : "—"),
                    meta?.Reference ?? (meta == null ? "…" : "—"),
                    meta?.HoldingDaysText ?? "…",
                };
                for (var i = 0; i < cells.Length; i++)
                {
                    var t = FundUi.Text(cells[i], 11.5, i == 0 ? FundPalette.TextBright : FundPalette.Dim, wrap: false);
                    t.Margin = new Thickness(0, 0, 8, 0);
                    t.ToolTip = cells[i];
                    Grid.SetColumn(t, i);
                    grid.Children.Add(t);
                }
                var row = CardButton(grid, row: true);
                row.Tag = s.Id;
                AutomationProperties.SetName(row, DisplayName(s) + ", family " + cells[1] + ", reference " + cells[2] + ", holding days " + cells[3]);
                row.Click += async (_, _) => await SelectAsync(s.Id);
                AlphaRows.Children.Add(row);
            }
            HighlightSelectedCard();
        }

        /// <summary>
        /// Library metadata (family, reference, holding days) lives on the version route
        /// (GET /v2/strategies/{id}/versions/{v} → library). Loaded once per version, one call at a
        /// time; a failure stops the loading and is shown verbatim.
        /// </summary>
        private async Task LoadLibraryMetaAsync()
        {
            if (_alphaLoading) return;
            _alphaLoading = true;
            var seq = _listSeq;
            try
            {
                var missing = _strategies.Where(s => s.IsLibrary && s.CurrentVersion is int v && !_libraryMeta.ContainsKey(s.Id + "@" + v)).ToList();
                foreach (var s in missing)
                {
                    var v = s.CurrentVersion!.Value;
                    var result = await PlatformClient.GetStrategyVersionAsync(s.Id, v);
                    if (seq != _listSeq) return;
                    if (!result.Ok || result.Value == null)
                    {
                        ShowError("Alpha library details could not be loaded: " + (result.Ok ? "unexpected response." : result.Message));
                        return;
                    }
                    _libraryMeta[s.Id + "@" + v] = result.Value.Library ?? new LibraryMeta();
                }
                if (missing.Count > 0) RenderAlphaRows();
            }
            finally
            {
                _alphaLoading = false;
            }
        }

        private void Tab_Checked(object sender, RoutedEventArgs e)
        {
            if (BoardScroll == null || AlphaPanel == null) return; // during InitializeComponent
            var alpha = AlphaTab.IsChecked == true;
            BoardScroll.Visibility = alpha ? Visibility.Collapsed : Visibility.Visible;
            AlphaPanel.Visibility = alpha ? Visibility.Visible : Visibility.Collapsed;
            if (alpha) _ = LoadLibraryMetaAsync();
        }

        private async void RefreshButton_Click(object sender, RoutedEventArgs e) => await RefreshAsync(_selectedId);

        // =====================================================================================
        // Detail pane
        // =====================================================================================

        private async Task SelectAsync(string id)
        {
            var seq = ++_detailSeq;
            _selectedId = id;
            _detail = null;
            _version = null;
            HighlightSelectedCard();
            ShowDetailPlaceholder("Loading…");

            var detail = await PlatformClient.GetStrategyAsync(id);
            if (seq != _detailSeq) return;
            if (!detail.Ok || detail.Value?.Strategy == null)
            {
                ShowDetailError(detail.Ok ? "Unexpected response from the backend (strategy detail)." : detail.Message);
                return;
            }
            var current = detail.Value.Strategy.CurrentVersion
                          ?? detail.Value.Versions?.Select(v => (int?)v.Version).Max();
            if (current is not int version)
            {
                ShowDetailError("The backend returned this strategy without a version.");
                return;
            }
            var ver = await PlatformClient.GetStrategyVersionAsync(id, version);
            if (seq != _detailSeq) return;
            if (!ver.Ok || ver.Value == null)
            {
                ShowDetailError(ver.Ok ? "Unexpected response from the backend (strategy version)." : ver.Message);
                return;
            }
            _detail = detail.Value;
            _version = ver.Value;
            _holdout = null;
            _holdoutError = null;
            if (_version.Library != null) _libraryMeta[id + "@" + version] = _version.Library;
            RenderDetail();

            var holdout = await PlatformClient.GetHoldoutAsync(id);
            if (seq != _detailSeq) return;
            if (holdout.Ok && holdout.Value != null) _holdout = holdout.Value;
            else _holdoutError = holdout.Ok ? "Unexpected response from the backend (holdout status)." : holdout.Message;
            ApplyHoldoutState();
        }

        /// <summary>"Open holdout (once)…": enabled only when GET /v2/strategies/{id}/holdout says eligible; otherwise
        /// disabled with the backend's reason + eligibility date (tooltip AND inline text for keyboard/screen-reader users).</summary>
        private void ApplyHoldoutState()
        {
            if (_holdoutButton == null || _holdoutNote == null) return;
            string? note = null;
            if (_holdout != null)
            {
                var canOpen = HoldoutText.CanOpen(_holdout);
                _holdoutButton.IsEnabled = canOpen;
                if (canOpen)
                {
                    _holdoutButton.ToolTip = "The final test on data no family member has seen: window " + (HoldoutText.WindowStart(_holdout) ?? "?") + " … "
                                             + (HoldoutText.WindowEnd(_holdout) ?? "?") + ". Once per strategy family; no exception possible.";
                    AutomationProperties.SetName(_holdoutButton, "Open holdout, once per strategy family");
                }
                else
                {
                    note = HoldoutText.NotEligibleText(_holdout);
                    _holdoutButton.ToolTip = note;
                    AutomationProperties.SetName(_holdoutButton, "Open holdout (not available: " + note.Replace("\n", " ") + ")");
                }
            }
            else if (_holdoutError != null)
            {
                _holdoutButton.IsEnabled = false;
                note = "Holdout status not loaded: " + _holdoutError;
                _holdoutButton.ToolTip = note;
                AutomationProperties.SetName(_holdoutButton, "Open holdout (not available: " + _holdoutError + ")");
            }
            else
            {
                _holdoutButton.IsEnabled = false;
                _holdoutButton.ToolTip = "Checking holdout eligibility…";
            }
            _holdoutNote.Text = note ?? "";
            _holdoutNote.Foreground = _holdoutError != null ? FundPalette.Kill : FundPalette.Faint;
            _holdoutNote.Visibility = note == null ? Visibility.Collapsed : Visibility.Visible;
        }

        private void ShowDetailPlaceholder(string text)
        {
            DetailPanel.Children.Clear();
            DetailPanel.Children.Add(FundUi.Text(text, 11.5, FundPalette.Faint));
        }

        private void ShowDetailError(string message)
        {
            DetailPanel.Children.Clear();
            DetailPanel.Children.Add(ErrorBlock(message));
        }

        private static Border ErrorBlock(string message)
        {
            var text = FundUi.Text(message, 11.5, FundPalette.Make(0xFFF4A197));
            return new Border
            {
                Background = FundPalette.KillSurface,
                BorderBrush = FundPalette.Kill,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 4, 0, 8),
                Child = text,
            };
        }

        private StackPanel? _previewHost;
        private Button? _previewButton;
        private Button? _holdoutButton;
        private TextBlock? _holdoutNote;
        private HoldoutStatus? _holdout;
        private string? _holdoutError;

        private void RenderDetail()
        {
            DetailPanel.Children.Clear();
            var s = _detail?.Strategy;
            var v = _version;
            if (s == null || v == null) return;
            var envelope = v.Envelope;

            var title = FundUi.Text(DisplayName(s), 15, FundPalette.TextBright, bold: true);
            DetailPanel.Children.Add(title);
            if (FundUi.FixtureLine(s.Fixture, s.FixtureNote, 12.5, new Thickness(0, 4, 0, 2)) is TextBlock fixture) DetailPanel.Children.Add(fixture);
            var line = FundUi.Text(StrategyStates.Label(s.State) + " · " + (s.Category ?? "—") + " · v" + (s.CurrentVersion?.ToString(Inv) ?? "?")
                                   + " · updated " + FundFormat.LocalTime(s.UpdatedAt, "yyyy-MM-dd HH:mm"), 11, FundPalette.Dim, mono: true);
            line.Margin = new Thickness(0, 3, 0, 4);
            DetailPanel.Children.Add(line);
            var chips = new WrapPanel { Margin = new Thickness(0, 0, 0, 10) };
            // Envelope flags win over the list flags (the envelope is the system-owned source, §7.6).
            var external = envelope?.ExternalText ?? s.ExternalText;
            var ai = envelope?.AiGenerated ?? s.AiGenerated;
            foreach (var chip in ChipsFor(envelope?.Origin ?? s.Origin, external, ai, v.Validation?.Ok ?? s.Valid)) chips.Children.Add(chip);
            foreach (var chip in FamilyChipsFor(s)) chips.Children.Add(chip);
            DetailPanel.Children.Add(chips);
            var trials = FundUi.Text("Trials in this family: " + (s.TrialCount?.ToString(Inv) ?? "—") + " — each backtest counts", 11, FundPalette.Dim);
            trials.Margin = new Thickness(0, -4, 0, 8);
            trials.ToolTip = "Every finished backtest, validation and holdout run of this strategy and all its copies and versions is a trial. "
                             + "More trials make a good-looking result more likely to be luck — the deflated Sharpe gate (G4) accounts for that.";
            AutomationProperties.SetName(trials, trials.Text);
            DetailPanel.Children.Add(trials);

            DetailPanel.Children.Add(BuildActions(s));
            _holdoutNote = FundUi.Text("", 10.5, FundPalette.Faint);
            _holdoutNote.Margin = new Thickness(0, -2, 0, 6);
            _holdoutNote.Visibility = Visibility.Collapsed;
            DetailPanel.Children.Add(_holdoutNote);
            ApplyHoldoutState();

            _previewHost = new StackPanel();
            DetailPanel.Children.Add(_previewHost);
            // Stage 5b (SPEC §14.5): investment committee of the current version (advisory; history + "Convene committee…" when auto is off)
            if (!string.IsNullOrWhiteSpace(s.Id)) DetailPanel.Children.Add(new CommitteePanel(s.Id, s.CurrentVersion, null));

            // ----- Summary -----
            var sum = v.Summary;
            Section("Summary");
            KeyText("Universe", sum?.UniverseText);
            KeyText("Signal", sum?.SignalText, mono: true);
            if (sum?.FeaturesText is { Count: > 0 } features)
            {
                foreach (var (name, text) in features) KeyText("Feature " + name, text, mono: true);
            }
            KeyText("Portfolio", sum?.PortfolioText);
            KeyText("Rebalance", sum?.RebalanceText);
            KeyText("Risk", sum?.RiskText);

            var spec = v.Spec;
            var description = spec is JsonElement sp ? StrategyJson.Str(sp, "description") : null;
            if (!string.IsNullOrWhiteSpace(description))
            {
                Section("Description");
                DetailPanel.Children.Add(PlainText(description!));
            }
            if (s.Tags is { Count: > 0 })
            {
                KeyText("Tags", string.Join(", ", s.Tags));
            }

            // ----- Parameters -----
            if (spec is JsonElement spec2 && spec2.ValueKind == JsonValueKind.Object &&
                spec2.TryGetProperty("parameters", out var parameters) && parameters.ValueKind == JsonValueKind.Object &&
                parameters.EnumerateObject().Any())
            {
                Section("Parameters");
                DetailPanel.Children.Add(ParametersTable(parameters));
            }

            // ----- Library metadata -----
            if (v.Library != null)
            {
                Section("Alpha library");
                KeyText("Family", v.Library.Family);
                KeyText("Reference", v.Library.Reference);
                KeyText("Holding days", v.Library.HoldingDaysText);
                KeyText("Notes", v.Library.Notes);
            }

            // ----- Validation -----
            var errors = v.Validation?.Errors ?? new List<StrategyIssue>();
            var warnings = v.Validation?.Warnings ?? new List<StrategyIssue>();
            if (errors.Count > 0 || warnings.Count > 0)
            {
                Section("Validation");
                foreach (var e in errors) DetailPanel.Children.Add(IssueLine(e, FundPalette.Kill, "error"));
                foreach (var w in warnings) DetailPanel.Children.Add(IssueLine(w, FundPalette.Risk, "warning"));
            }

            // ----- Provenance (system-owned) + claims (unverified) -----
            if (envelope != null)
            {
                Section("Provenance");
                KeyText("Origin", envelope.Origin);
                KeyText("Created", FundFormat.LocalTime(envelope.CreatedAt, "yyyy-MM-dd HH:mm") + " by " + (envelope.CreatedBy ?? "—"));
                if (envelope.Parent is JsonElement parent && parent.ValueKind == JsonValueKind.Object)
                    KeyText("Parent", (StrategyJson.Str(parent, "strategy_id") ?? "?") + " v" + (StrategyJson.Str(parent, "version") ?? "?"));
                KeyText("AI involvement", StrategyJson.IsUnknown(ai) ? "unknown" : StrategyJson.IsTrue(ai) ? "yes" : "no");
                if (envelope.SourceFile != null)
                {
                    KeyText("Source file", (envelope.SourceFile.Name ?? "—") + (envelope.SourceFile.Bytes is long b ? " · " + b.ToString(Inv) + " bytes" : ""));
                    KeyText("File SHA-256", envelope.SourceFile.Sha256, mono: true);
                }
                KeyText("Content hash", envelope.ContentHash ?? s.ContentHash, mono: true);
                if (envelope.Claimed is JsonElement claimed && claimed.ValueKind != JsonValueKind.Null && claimed.ValueKind != JsonValueKind.Undefined)
                {
                    Section("Claimed by the file — unverified");
                    var note = FundUi.Text("These statements came with the imported file. Nothing here was checked by UFOS.ai.", 10.5, FundPalette.Risk);
                    note.Margin = new Thickness(0, 0, 0, 4);
                    DetailPanel.Children.Add(note);
                    DetailPanel.Children.Add(PlainText(Pretty(claimed), mono: true));
                }
            }

            // ----- Versions / transitions -----
            if (_detail?.Versions is { Count: > 0 } versions)
            {
                Section("Versions");
                foreach (var ver in versions.OrderByDescending(x => x.Version))
                {
                    DetailPanel.Children.Add(PlainText("v" + ver.Version.ToString(Inv) + " · " + FundFormat.LocalTime(ver.CreatedAt, "yyyy-MM-dd HH:mm")
                        + " · " + (ver.Origin ?? "—") + " · " + Short(ver.ContentHash), mono: true));
                }
            }
            if (_detail?.Transitions is { Count: > 0 } transitions)
            {
                Section("Transitions");
                foreach (var t in transitions.OrderByDescending(x => x.Ts ?? ""))
                {
                    var head = FundFormat.LocalTime(t.Ts, "yyyy-MM-dd HH:mm") + " · " + (t.FromState ?? "—") + " → " + (t.ToState ?? "—")
                               + " · " + (t.Actor ?? "—") + (t.Version is int tv ? " · v" + tv.ToString(Inv) : "");
                    DetailPanel.Children.Add(PlainText(head, mono: true));
                    if (!string.IsNullOrWhiteSpace(t.Reason))
                    {
                        var reason = PlainText(t.Reason!);
                        reason.Margin = new Thickness(12, 0, 0, 4);
                        DetailPanel.Children.Add(reason);
                    }
                }
            }
        }

        private WrapPanel BuildActions(StrategySummary s)
        {
            var panel = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };

            var edit = FundUi.MakeButton("Edit");
            if (s.IsLibrary)
            {
                edit.IsEnabled = false;
                edit.ToolTip = "Library strategies are read-only — duplicate them first.";
                ToolTipService.SetShowOnDisabled(edit, true);
            }
            else edit.ToolTip = "Opens the JSON editor. Saving creates a new version; the strategy returns to draft.";
            AutomationProperties.SetName(edit, s.IsLibrary ? "Edit (not available: library strategies are read-only)" : "Edit strategy");
            edit.Click += (_, _) => OpenEditor(s);
            panel.Children.Add(WithGap(edit));

            var duplicate = FundUi.MakeButton("Duplicate as draft");
            duplicate.ToolTip = "Creates an editable copy in state draft.";
            AutomationProperties.SetName(duplicate, "Duplicate as draft");
            duplicate.Click += async (_, _) => await DuplicateAsync(s);
            panel.Children.Add(WithGap(duplicate));

            var move = FundUi.MakeButton("Move to…");
            var anyAllowed = _detail?.AllowedTransitions?.Any(t => t.Allowed) == true;
            move.ToolTip = anyAllowed ? "Change the lifecycle state (a note is optional)." : "No state change is allowed right now.";
            AutomationProperties.SetName(move, "Move to another lifecycle state");
            move.Click += async (_, _) =>
            {
                // fix1023 review minor 6: the dialog closes before the request — keep the button disabled until it is answered
                if (TradingUi.IsBusy(move)) return;
                TradingUi.SetBusy(move, true, "Moving…");
                try { await MoveAsync(s); }
                finally { TradingUi.SetBusy(move, false); move.IsEnabled = true; }
            };
            panel.Children.Add(WithGap(move));

            _previewButton = FundUi.MakeButton("Preview signal");
            _previewButton.ToolTip = "Evaluates the current version on recent daily bars: signal, rank and target weight per symbol.";
            AutomationProperties.SetName(_previewButton, "Preview signal");
            _previewButton.Click += async (_, _) => await PreviewAsync(s);
            panel.Children.Add(WithGap(_previewButton));

            // Stage 3 (SPEC §8.6): backtest engine v2 — dialog → progress panel → report window.
            var backtest = FundUi.MakeButton("Run backtest…");
            backtest.ToolTip = "Backtests the current version on daily bars through the paper broker (fees, slippage, dividends); optional stress tests. No AI.";
            AutomationProperties.SetName(backtest, "Run backtest");
            backtest.Click += async (_, _) => await RunBacktestAsync(s, backtest);
            panel.Children.Add(WithGap(backtest));

            // Stage 4 (SPEC §9.7): Validate… → progress → validation report; Open holdout (once)… with typed confirmation.
            var validate = FundUi.MakeButton("Validate…");
            validate.ToolTip = "Runs all gates G1–G8 on the training range (backtest, walk-forward folds, parameter grid, stress). Repeatable; every run counts as a trial.";
            AutomationProperties.SetName(validate, "Validate: run the validation gates");
            validate.Click += async (_, _) => await ValidateAsync(s);
            panel.Children.Add(WithGap(validate));

            _holdoutButton = FundUi.MakeButton("Open holdout (once)…");
            _holdoutButton.IsEnabled = false;
            _holdoutButton.ToolTip = "Checking holdout eligibility…";
            ToolTipService.SetShowOnDisabled(_holdoutButton, true);
            AutomationProperties.SetName(_holdoutButton, "Open holdout, once per strategy family");
            _holdoutButton.Click += async (_, _) => await OpenHoldoutAsync(s);
            panel.Children.Add(WithGap(_holdoutButton));

            var validations = FundUi.MakeButton("Validation runs");
            validations.ToolTip = "Opens the validation report with the validation and holdout runs of this strategy.";
            AutomationProperties.SetName(validations, "Open the validation runs of this strategy");
            validations.Click += (_, _) => ValidationReportWindow.Open(OwnerWindow, s.Id, DisplayName(s), null);
            panel.Children.Add(WithGap(validations));

            var reports = FundUi.MakeButton("Backtest runs");
            reports.ToolTip = "Opens the backtest report with the run history of this strategy.";
            AutomationProperties.SetName(reports, "Open the backtest runs of this strategy");
            reports.Click += (_, _) => BacktestReportWindow.Open(OwnerWindow, s.Id, DisplayName(s), null);
            panel.Children.Add(WithGap(reports));

            var export = FundUi.MakeButton("Export…");
            export.ToolTip = "Saves the current version as a JSON file.";
            AutomationProperties.SetName(export, "Export strategy to a JSON file");
            export.Click += async (_, _) => await ExportAsync(s);
            panel.Children.Add(WithGap(export));
            return panel;
        }

        private static Button WithGap(Button b)
        {
            b.Margin = new Thickness(0, 0, 6, 6);
            b.FontSize = 11.5;
            return b;
        }

        private void Section(string title)
        {
            var t = FundUi.Text(title, 10.5, FundPalette.Faint, bold: true);
            t.Margin = new Thickness(0, 12, 0, 4);
            DetailPanel.Children.Add(t);
        }

        private void KeyText(string label, string? value, bool mono = false)
        {
            var grid = new Grid { Margin = new Thickness(0, 1, 0, 3) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(96) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var l = FundUi.Text(label, 11, FundPalette.Faint);
            var v = PlainText(string.IsNullOrWhiteSpace(value) ? "—" : value!, mono);
            v.Margin = new Thickness(6, 0, 0, 0);
            Grid.SetColumn(v, 1);
            grid.Children.Add(l);
            grid.Children.Add(v);
            DetailPanel.Children.Add(grid);
        }

        /// <summary>Strategy text as a plain, selectable-free TextBlock (never markup, never a link).</summary>
        private static TextBlock PlainText(string text, bool mono = false)
        {
            var t = FundUi.Text(text.Length > 8000 ? text[..8000] + " …(truncated for display)" : text, 11.5, FundPalette.Text, mono: mono);
            t.LineHeight = 17;
            return t;
        }

        private static FrameworkElement IssueLine(StrategyIssue issue, Brush brush, string kind)
        {
            var text = (issue.Code ?? kind) + " — " + (issue.Message ?? "") +
                       (string.IsNullOrWhiteSpace(issue.Path) ? "" : "  (" + issue.Path + ")") +
                       (issue.Position.Length > 0 ? "  [" + issue.Position + "]" : "");
            var t = FundUi.Text(text, 11, brush);
            t.Margin = new Thickness(0, 0, 0, 3);
            AutomationProperties.SetName(t, kind + ": " + text);
            return t;
        }

        private static Grid ParametersTable(JsonElement parameters)
        {
            var grid = new Grid();
            var widths = new[] { new GridLength(1.2, GridUnitType.Star), new GridLength(54), new GridLength(54), new GridLength(54), new GridLength(44) };
            foreach (var w in widths) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = w });
            var headers = new[] { "Name", "Value", "Min", "Max", "Step" };
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var i = 0; i < headers.Length; i++)
            {
                var h = FundUi.Text(headers[i], 10, FundPalette.Faint, wrap: false);
                Grid.SetColumn(h, i);
                grid.Children.Add(h);
            }
            var row = 1;
            foreach (var p in parameters.EnumerateObject())
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var cells = new[]
                {
                    p.Name,
                    ParamValue(p.Value, "value"),
                    ParamValue(p.Value, "min"),
                    ParamValue(p.Value, "max"),
                    ParamValue(p.Value, "step"),
                };
                for (var i = 0; i < cells.Length; i++)
                {
                    var c = FundUi.Text(cells[i], 11, i == 0 ? FundPalette.Text : FundPalette.Dim, mono: true, wrap: false);
                    c.Margin = new Thickness(0, 2, 4, 0);
                    c.ToolTip = cells[i];
                    Grid.SetRow(c, row);
                    Grid.SetColumn(c, i);
                    grid.Children.Add(c);
                }
                row++;
                var desc = p.Value.ValueKind == JsonValueKind.Object ? StrategyJson.Str(p.Value, "description") : null;
                if (!string.IsNullOrWhiteSpace(desc))
                {
                    grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                    var d = FundUi.Text(desc, 10.5, FundPalette.Faint);
                    d.Margin = new Thickness(10, 0, 0, 3);
                    Grid.SetRow(d, row);
                    Grid.SetColumnSpan(d, 5);
                    grid.Children.Add(d);
                    row++;
                }
            }
            return grid;
        }

        private static string ParamValue(JsonElement param, string key)
            => param.ValueKind == JsonValueKind.Object && param.TryGetProperty(key, out var v) ? FundFormat.Plain(v) : "—";

        private static string Pretty(JsonElement e)
        {
            return JsonSerializer.Serialize(e, new JsonSerializerOptions { WriteIndented = true });
        }

        private static string Short(string? hash) => string.IsNullOrEmpty(hash) ? "—" : hash.Length > 12 ? hash[..12] : hash;

        // =====================================================================================
        // Actions
        // =====================================================================================

        private void OpenEditor(StrategySummary s)
        {
            if (s.IsLibrary || _version == null || s.CurrentVersion is not int version) return;
            var existing = _editors.FirstOrDefault(e => e.StrategyId == s.Id);
            if (existing != null)
            {
                if (existing.WindowState == WindowState.Minimized) existing.WindowState = WindowState.Normal;
                existing.Activate();
                return;
            }
            var text = _version.SpecText ?? (_version.Spec is JsonElement spec ? Pretty(spec) : "");
            ShowEditor(new StrategyEditorWindow(OwnerWindow, s.Id, DisplayName(s), version, text));
        }

        private void NewStrategyButton_Click(object sender, RoutedEventArgs e)
        {
            ShowEditor(new StrategyEditorWindow(OwnerWindow, null, null, null, StrategyEditorWindow.NewStrategyTemplate));
        }

        private void ShowEditor(StrategyEditorWindow editor)
        {
            _editors.Add(editor);
            editor.Saved += async (id, _) => await RefreshAsync(id);
            editor.Closed += (_, _) => _editors.Remove(editor);
            editor.Show();
        }

        private async Task DuplicateAsync(StrategySummary s)
        {
            if (_busy) return;
            var values = FundDialogs.Form(OwnerWindow, "Duplicate as draft",
                "Creates an editable copy of \"" + DisplayName(s) + "\" in state draft. The original stays unchanged.",
                new[] { ("Name of the copy", DisplayName(s) + " (draft)") }, "Duplicate");
            if (values == null) return;
            _busy = true;
            try
            {
                var result = await PlatformClient.DuplicateStrategyAsync(s.Id, values[0].Trim());
                if (!result.Ok || result.Value == null)
                {
                    FundDialogs.Info(OwnerWindow, "Duplicate failed", result.Ok ? "Unexpected response from the backend." : result.Message);
                    return;
                }
                await RefreshAsync(result.Value.Id);
            }
            finally
            {
                _busy = false;
            }
        }

        private async Task MoveAsync(StrategySummary s)
        {
            if (_busy || _detail == null) return;
            var options = _detail.AllowedTransitions ?? new List<AllowedTransition>();
            var dialog = new StrategyMoveDialog(OwnerWindow, DisplayName(s), s.State, options);
            if (dialog.ShowDialog() != true || dialog.Target == null) return;
            _busy = true;
            try
            {
                var result = await PlatformClient.TransitionStrategyAsync(s.Id, dialog.Target, dialog.Reason);
                if (!result.Ok)
                {
                    FundDialogs.Info(OwnerWindow, "State not changed", result.Message);
                    return;
                }
                await RefreshAsync(s.Id);
            }
            finally
            {
                _busy = false;
            }
        }

        private async Task ValidateAsync(StrategySummary s)
        {
            string? training = null;
            if (_holdout != null)
                training = "Training range ends " + (HoldoutText.TrainingEnd(_holdout) ?? "the session before " + (HoldoutText.HoldoutStart(_holdout) ?? "?"))
                           + " · locked holdout window from " + (HoldoutText.HoldoutStart(_holdout) ?? "?");
            await ValidationLauncher.RunAsync(OwnerWindow, s.Id, DisplayName(s), s.CurrentVersion, training);
        }

        private async Task OpenHoldoutAsync(StrategySummary s)
        {
            if (_busy) return;
            _busy = true;
            try
            {
                var started = await HoldoutLauncher.RunAsync(OwnerWindow, s.Id, DisplayName(s));
                if (started) await RefreshAsync(s.Id);   // seen_until / chips change as soon as the holdout is opened
            }
            finally
            {
                _busy = false;
            }
        }

        private void GateSettingsButton_Click(object sender, RoutedEventArgs e)
        {
            var window = new GateSettingsWindow(OwnerWindow);
            window.ShowDialog();
        }

        /// <summary>Live click-test fix 2026-10-18: the button is disabled while the dialog flow runs (no double start) and any
        /// failure is shown and logged — never a silent no-op.</summary>
        private async Task RunBacktestAsync(StrategySummary s, Button button)
        {
            if (!button.IsEnabled) return;
            button.IsEnabled = false;
            try
            {
                var spec = _version?.Spec;
                var benchmark = spec is JsonElement sp && sp.ValueKind == JsonValueKind.Object ? StrategyJson.Str(sp, "benchmark") : null;
                await BacktestLauncher.RunAsync(OwnerWindow, s.Id, DisplayName(s), s.CurrentVersion, benchmark);
            }
            catch (Exception ex)
            {
                AppLog.Error(LogModule, "Library: Run backtest failed.", ex);
                FundDialogs.Info(OwnerWindow, "Run backtest", "The backtest dialog could not be opened: " + ex.Message);
            }
            finally
            {
                button.IsEnabled = true;
            }
        }

        private async Task PreviewAsync(StrategySummary s)
        {
            if (_previewHost == null || _previewButton == null) return;
            var host = _previewHost;
            var button = _previewButton;
            var seq = _detailSeq;
            button.IsEnabled = false;
            host.Children.Clear();
            var loading = FundUi.Text("Computing the signal preview (loads daily bars, up to 60 s)…", 11, FundPalette.Dim);
            loading.Margin = new Thickness(0, 6, 0, 6);
            host.Children.Add(loading);
            try
            {
                var result = await PlatformClient.PreviewStrategyAsync(s.Id, s.CurrentVersion);
                if (seq != _detailSeq) return;
                host.Children.Clear();
                var heading = FundUi.Text("Signal preview", 10.5, FundPalette.Faint, bold: true);
                heading.Margin = new Thickness(0, 10, 0, 4);
                host.Children.Add(heading);
                if (!result.Ok || result.Value == null)
                {
                    host.Children.Add(ErrorBlock(result.Ok ? "Unexpected response from the backend (preview)." : result.Message));
                    return;
                }
                var p = result.Value;
                var info = "As of " + (p.AsOf ?? "—");
                if (p.Data != null)
                    info += " · data " + (p.Data.Source ?? "—") + " " + (p.Data.From ?? "?") + " → " + (p.Data.To ?? "?");
                host.Children.Add(FundUi.Text(info, 10.5, FundPalette.Dim, mono: true));
                if (p.Data?.SymbolsMissing is { Count: > 0 } missing)
                {
                    var m = FundUi.Text("Excluded (no history): " + string.Join(", ", missing), 10.5, FundPalette.Risk);
                    m.Margin = new Thickness(0, 2, 0, 2);
                    host.Children.Add(m);
                }
                foreach (var w in p.Warnings ?? new List<JsonElement>())
                {
                    var text = w.ValueKind == JsonValueKind.Object
                        ? (StrategyJson.Str(w, "code") ?? "warning") + " — " + (StrategyJson.Str(w, "message") ?? FundFormat.Plain(w))
                        : FundFormat.Plain(w);
                    var t = FundUi.Text(text, 10.5, FundPalette.Risk);
                    t.Margin = new Thickness(0, 2, 0, 2);
                    host.Children.Add(t);
                }
                host.Children.Add(PreviewTable(p.Rows ?? new List<StrategyPreviewRow>()));
                var note = FundUi.Text("Research preview on paper · simulated — for your own decision.", 10, FundPalette.Faint);
                note.FontStyle = FontStyles.Italic;
                note.Margin = new Thickness(0, 4, 0, 6);
                host.Children.Add(note);
            }
            finally
            {
                button.IsEnabled = true;
            }
        }

        private static FrameworkElement PreviewTable(List<StrategyPreviewRow> rows)
        {
            var grid = new Grid { Margin = new Thickness(0, 6, 0, 0) };
            var widths = new[] { new GridLength(1, GridUnitType.Star), new GridLength(78), new GridLength(52), new GridLength(70) };
            foreach (var w in widths) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = w });
            var headers = new[] { "Symbol", "Signal", "Rank", "Target wt" };
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var i = 0; i < headers.Length; i++)
            {
                var h = FundUi.Text(headers[i], 10, FundPalette.Faint, wrap: false);
                if (i > 0) h.TextAlignment = TextAlignment.Right;
                Grid.SetColumn(h, i);
                grid.Children.Add(h);
            }
            if (rows.Count == 0)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var none = FundUi.Text("No symbols with a signal.", 11, FundPalette.Faint);
                Grid.SetRow(none, 1);
                Grid.SetColumnSpan(none, 4);
                grid.Children.Add(none);
                return grid;
            }
            var r = 1;
            foreach (var row in rows)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var cells = new[]
                {
                    row.Symbol ?? "—",
                    row.Signal is double sig ? sig.ToString("0.####", Inv) : "—",
                    row.Rank is double rank ? rank.ToString("0.##", Inv) : "—",
                    row.TargetWeight is double tw ? (tw * 100).ToString("0.0", Inv) + "%" : "—",
                };
                for (var i = 0; i < cells.Length; i++)
                {
                    var brush = i == 3 && row.TargetWeight is double w2 ? FundPalette.ForSigned(w2) : FundPalette.Text;
                    var c = FundUi.Text(cells[i], 11, brush, mono: true, wrap: false);
                    if (i > 0) c.TextAlignment = TextAlignment.Right;
                    c.Margin = new Thickness(0, 1, 0, 1);
                    Grid.SetRow(c, r);
                    Grid.SetColumn(c, i);
                    grid.Children.Add(c);
                }
                r++;
            }
            AutomationProperties.SetName(grid, "Signal preview table, " + rows.Count + " symbols");
            return grid;
        }

        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        private Task ExportAsync(StrategySummary s) => DialogGuard.RunAsync("library-export:" + s.Id, () => ExportCoreAsync(s));

        private async Task ExportCoreAsync(StrategySummary s)
        {
            if (_busy || s.CurrentVersion is not int version) return;
            _busy = true;
            try
            {
                var result = await PlatformClient.ExportStrategyAsync(s.Id, version);
                if (!result.Ok || result.Value?.Text == null)
                {
                    FundDialogs.Info(OwnerWindow, "Export failed", result.Ok ? "The backend returned no text to export." : result.Message);
                    return;
                }
                var dialog = new Microsoft.Win32.SaveFileDialog
                {
                    Title = "Export strategy",
                    Filter = "Strategy JSON (*.json)|*.json",
                    DefaultExt = ".json",
                    AddExtension = true,
                    OverwritePrompt = true,
                    FileName = SafeFileName(result.Value.FileName, DisplayName(s)),
                };
                var owner = OwnerWindow;
                var ok = owner != null ? dialog.ShowDialog(owner) : dialog.ShowDialog();
                if (ok != true) return;
                try
                {
                    File.WriteAllText(dialog.FileName, result.Value.Text, new UTF8Encoding(false));
                    StatusText.Text = "Exported to " + Path.GetFileName(dialog.FileName);
                }
                catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is System.Security.SecurityException)
                {
                    FundDialogs.Info(OwnerWindow, "Export failed", "The file could not be written: " + ex.Message);
                }
            }
            finally
            {
                _busy = false;
            }
        }

        /// <summary>A file name proposal without directory parts or invalid characters.</summary>
        private static string SafeFileName(string? suggested, string fallbackName)
        {
            var name = string.IsNullOrWhiteSpace(suggested) ? fallbackName + ".json" : Path.GetFileName(suggested.Replace('/', '\\'));
            foreach (var c in Path.GetInvalidFileNameChars()) name = name.Replace(c, '_');
            name = name.Trim().Trim('.');
            if (name.Length == 0) name = "strategy.json";
            if (name.Length > 120) name = name[..120];
            if (!name.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) name += ".json";
            return name;
        }

        private async void ImportButton_Click(object sender, RoutedEventArgs e)
        {
            if (_busy) return;
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Title = "Import strategy",
                Filter = "Strategy JSON (*.json)|*.json",
                CheckFileExists = true,
                Multiselect = false,
            };
            var owner = OwnerWindow;
            var picked = owner != null ? dialog.ShowDialog(owner) : dialog.ShowDialog();
            if (picked != true) return;

            var path = dialog.FileName;
            var fileName = Path.GetFileName(path); // the NAME only — no directory goes to the backend
            byte[] bytes;
            try
            {
                // Size check BEFORE reading (SPEC §7.12), and the read itself is bounded too.
                var info = new FileInfo(path);
                if (info.Length > PlatformClient.MaxStrategyTextBytes)
                {
                    FundDialogs.Info(owner, "Import refused",
                        "\"" + fileName + "\" is " + info.Length.ToString("N0", Inv) + " bytes — strategy files may be at most 256 KB (262,144 bytes). The file was not read.");
                    return;
                }
                using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
                var buffer = new byte[PlatformClient.MaxStrategyTextBytes + 1];
                var total = 0;
                int read;
                while (total < buffer.Length && (read = stream.Read(buffer, total, buffer.Length - total)) > 0) total += read;
                if (total > PlatformClient.MaxStrategyTextBytes)
                {
                    FundDialogs.Info(owner, "Import refused", "\"" + fileName + "\" grew beyond 256 KB while it was read — import cancelled.");
                    return;
                }
                bytes = buffer[..total];
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is System.Security.SecurityException)
            {
                FundDialogs.Info(owner, "Import failed", "\"" + fileName + "\" could not be read: " + ex.Message);
                return;
            }

            string text;
            try
            {
                // Strict UTF-8: invalid byte sequences are an error, not replaced.
                text = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true).GetString(bytes);
            }
            catch (DecoderFallbackException)
            {
                FundDialogs.Info(owner, "Import refused", "\"" + fileName + "\" is not valid UTF-8 text — strategy files must be UTF-8 encoded JSON.");
                return;
            }
            // A UTF-8 byte order mark (added by some Windows editors) is part of the encoding, not of the JSON.
            if (text.Length > 0 && text[0] == '﻿') text = text[1..];
            var sha256 = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();

            if (!FundDialogs.Confirm(owner, "Import strategy", ImportConfirmText, "Import",
                    footnote: "File: " + fileName + " · " + bytes.Length.ToString("N0", Inv) + " bytes · SHA-256 " + sha256[..16] + "…"))
                return;

            _busy = true;
            try
            {
                var result = await PlatformClient.CreateStrategyAsync(text, "import", fileName, sha256);
                if (!result.Ok || result.Value == null)
                {
                    var message = result.Ok ? "Unexpected response from the backend." : result.Message;
                    var validation = PlatformClient.ReadValidation(result);
                    if (validation?.Errors is { Count: > 0 } errs)
                    {
                        message += "\n\n" + string.Join("\n", errs.Take(12).Select(x =>
                            (x.Code ?? "error") + " — " + (x.Message ?? "") + (string.IsNullOrWhiteSpace(x.Path) ? "" : " (" + x.Path + ")")
                            + (x.Position.Length > 0 ? " [" + x.Position + "]" : "")));
                        if (errs.Count > 12) message += "\n… and " + (errs.Count - 12) + " more.";
                    }
                    FundDialogs.Info(owner, "Import failed", message);
                    return;
                }
                AppLog.Info(LogModule, "Strategy imported from a file (" + bytes.Length + " bytes).");
                await RefreshAsync(result.Value.Id);
            }
            finally
            {
                _busy = false;
            }
        }

        // =====================================================================================
        // Error banner
        // =====================================================================================

        private void ShowError(string message)
        {
            ErrorText.Text = message;
            ErrorBanner.Visibility = Visibility.Visible;
        }

        private void HideError()
        {
            ErrorText.Text = "";
            ErrorBanner.Visibility = Visibility.Collapsed;
        }
    }

    /// <summary>
    /// "Move to…" — every target the backend lists; the not-allowed ones are disabled and show
    /// <c>reason_if_not</c> (tooltip AND inline text, so keyboard / screen-reader users get it too).
    /// An optional note (≤ 500 characters) goes into the audit log (fix1023; SPEC §7.10).
    /// </summary>
    internal sealed class StrategyMoveDialog : FundPopupWindow
    {
        public const int MaxReasonLength = 500;
        public string? Target { get; private set; }
        public string Reason => _reason.Text.Trim();

        private readonly TextBox _reason;
        private readonly Button _ok;
        private readonly List<RadioButton> _options = new();

        public StrategyMoveDialog(Window? owner, string strategyName, string? currentState, IReadOnlyList<AllowedTransition> transitions)
            : base(owner, "Move to…", 480, 420, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Move to another state", 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text("\"" + strategyName + "\" is in state " + (currentState ?? "—") + ". Choose the new state; a note is optional (it goes into the audit log).", 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 10);
            msg.LineHeight = 18;
            stack.Children.Add(msg);

            if (transitions.Count == 0)
            {
                stack.Children.Add(FundUi.Text("The backend lists no transitions for this strategy.", 11.5, FundPalette.Faint));
            }
            foreach (var t in transitions)
            {
                var label = StrategyStates.Label(t.To);
                var option = new RadioButton
                {
                    GroupName = "MoveTarget",
                    Content = label,
                    Foreground = t.Allowed ? FundPalette.Text : FundPalette.Faint,
                    IsEnabled = t.Allowed,
                    Tag = t.To,
                    Margin = new Thickness(0, 3, 0, 0),
                    FontSize = 12,
                };
                if (!t.Allowed)
                {
                    option.ToolTip = string.IsNullOrWhiteSpace(t.ReasonIfNot) ? "Not allowed." : t.ReasonIfNot;
                    ToolTipService.SetShowOnDisabled(option, true);
                }
                AutomationProperties.SetName(option, t.Allowed ? "Move to " + label : label + " (not allowed: " + (t.ReasonIfNot ?? "no reason given") + ")");
                option.Checked += (_, _) => UpdateOk();
                _options.Add(option);
                stack.Children.Add(option);
                if (!t.Allowed && !string.IsNullOrWhiteSpace(t.ReasonIfNot))
                {
                    var why = FundUi.Text(t.ReasonIfNot, 10.5, FundPalette.Faint);
                    why.Margin = new Thickness(20, 0, 0, 2);
                    stack.Children.Add(why);
                }
            }

            var reasonLabel = FundUi.Text("Note (optional, max. " + MaxReasonLength + " characters, stored in the audit log)", 10.5, FundPalette.Faint);
            reasonLabel.Margin = new Thickness(0, 12, 0, 4);
            stack.Children.Add(reasonLabel);
            _reason = FundUi.Input("", mono: false);
            _reason.MaxLength = MaxReasonLength;
            _reason.TextWrapping = TextWrapping.Wrap;
            _reason.AcceptsReturn = false;
            _reason.MinHeight = 52;
            AutomationProperties.SetName(_reason, "Optional note for the state change");
            _reason.TextChanged += (_, _) => UpdateOk();
            stack.Children.Add(_reason);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 14, 0, 0) };
            var cancel = FundUi.MakeButton("Cancel");
            cancel.IsCancel = true;
            cancel.Click += (_, _) => { DialogResult = false; };
            _ok = FundUi.MakeButton("Move", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            _ok.Margin = new Thickness(8, 0, 0, 0);
            _ok.IsDefault = true;
            _ok.IsEnabled = false;
            _ok.Click += (_, _) =>
            {
                var chosen = _options.FirstOrDefault(o => o.IsChecked == true);
                if (chosen?.Tag is not string to) return;
                Target = to;
                DialogResult = true;
            };
            buttons.Children.Add(cancel);
            buttons.Children.Add(_ok);
            stack.Children.Add(buttons);
            SetBody(stack);
            Loaded += (_, _) =>
            {
                var first = _options.FirstOrDefault(o => o.IsEnabled);
                if (first != null) first.Focus(); else cancel.Focus();
            };
        }

        private void UpdateOk()
        {
            if (_ok == null) return;
            var chosen = _options.FirstOrDefault(o => o.IsChecked == true);
            _ok.Content = chosen?.Tag is string to ? "Move to " + StrategyStates.Label(to) : "Move";
            AutomationProperties.SetName(_ok, (string)_ok.Content);
            _ok.IsEnabled = chosen != null && _reason.Text.Trim().Length <= MaxReasonLength;
        }
    }
}

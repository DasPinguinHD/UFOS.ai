using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;
using UFOS.ai.Logging;
using UFOS.ai.Services;
using UFOS.ai.Shared;

namespace UFOS.ai
{
    // Central "AI budgets" window (task B, contract UFOS.ai/docs/ai-budgets-spec.md §6), opened from the
    // MainWindow Settings popup. One card per registered provider (Shared/AiBudgetProviders.cs — a new
    // module plugs in there, nothing to change here), rendered from GET /ai/budgets — or, for the
    // app-local provider "UFOS.ai app" (Summarize with AI, Shared/AiBudgetAppCaps.cs), from the same
    // payload produced in-process. Rules:
    //  - backend values are authoritative: no backend → "<Label> backend not running — start the module
    //    to edit its budgets", no inputs, never a stand-in value;
    //  - a limit edit is saved with ONE PUT per budget, 600 ms after the last valid edit (or on
    //    Enter / leaving the field), re-rendered from the returned payload (never from the typed value);
    //    errors are shown verbatim; only one PUT is in flight at a time;
    //  - cost-log totals (AiCostLedger, %LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl) are informational
    //    and always captioned "from the cost log";
    //  - refresh on open, on Refresh, and every 30 s while visible.
    // No AI output is shown here, so no AI labelling applies.
    public partial class AiBudgetsWindow : Window
    {
        private static readonly TimeSpan AutoRefreshInterval = TimeSpan.FromSeconds(30);
        internal static readonly TimeSpan SaveDelay = TimeSpan.FromMilliseconds(600);
        internal static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        internal const string CostLogCaption = "from the cost log";
        internal const string CostLogToolTip =
            "Informational — the backend's own spend counter is authoritative (it also counts estimates for failed/unpriced calls).";
        private const string LogModule = "AI budgets";

        private readonly List<ModuleCard> _cards = new();
        private readonly DispatcherTimer _autoRefresh;
        private readonly CancellationTokenSource _closing = new();
        private readonly SemaphoreSlim _putGate = new(1, 1);
        private CostLogSnapshot _costLog = CostLogSnapshot.Empty;
        private bool _closed;
        private bool _refreshingAll;

        public AiBudgetsWindow()
        {
            InitializeComponent();
            _autoRefresh = new DispatcherTimer(DispatcherPriority.Background, Dispatcher) { Interval = AutoRefreshInterval };
            _autoRefresh.Tick += (_, __) =>
            {
                if (!_closed && IsVisible && WindowState != WindowState.Minimized)
                {
                    _ = RefreshAllAsync();
                }
            };
        }

        // ===================== Lifecycle =====================

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            try
            {
                BuildCards();
            }
            catch (Exception ex)
            {
                // AiBudgetRegistry.Providers rejects duplicate module ids (programming error).
                try { AppLog.Error(LogModule, "Could not build the AI budget cards", ex); } catch { }
                WindowErrorText.Text = "The module list could not be built: " + AiBudgetText.Clean(ex.Message);
                WindowErrorText.Visibility = Visibility.Visible;
            }
            _ = RefreshAllAsync();
            _autoRefresh.Start();
        }

        private void Window_IsVisibleChanged(object sender, DependencyPropertyChangedEventArgs e)
        {
            if (_closed) return;
            if (IsVisible) _autoRefresh.Start();
            else _autoRefresh.Stop();
        }

        private void Window_Closed(object? sender, EventArgs e)
        {
            _closed = true;
            _autoRefresh.Stop();
            foreach (var card in _cards) card.StopTimers();
            try { _closing.Cancel(); } catch { }
        }

        private void Window_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key == Key.Escape)
            {
                // Esc in a field with an unsaved edit restores the stored value; otherwise it closes the window.
                if (Keyboard.FocusedElement is TextBox { Tag: BudgetRow row } && row.Dirty)
                {
                    row.Revert();
                }
                else
                {
                    Close();
                }
                e.Handled = true;
            }
            else if (e.Key == Key.F5)
            {
                _ = RefreshAllAsync();
                e.Handled = true;
            }
        }

        // ===================== Cards =====================

        private void BuildCards()
        {
            CardsPanel.Children.Clear();
            _cards.Clear();
            foreach (var provider in AiBudgetRegistry.Providers)
            {
                var card = new ModuleCard(this, provider);
                _cards.Add(card);
                CardsPanel.Children.Add(card.Root);
            }
        }

        // ===================== Loading =====================

        private async Task RefreshAllAsync()
        {
            if (_closed || _refreshingAll) return;
            _refreshingAll = true;
            RefreshAllButton.IsEnabled = false;
            try
            {
                _costLog = await CostLogSnapshot.ReadAsync();
                if (_closed) return;
                await Task.WhenAll(_cards.Select(card => RefreshCardAsync(card, reloadCostLog: false)).ToList());
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, "Refreshing the AI budgets failed", ex); } catch { }
            }
            finally
            {
                _refreshingAll = false;
                if (!_closed) RefreshAllButton.IsEnabled = true;
            }
        }

        internal async Task RefreshCardAsync(ModuleCard card, bool reloadCostLog)
        {
            if (_closed || card.Loading) return;
            card.BeginLoading();
            var epoch = card.Epoch;
            try
            {
                if (reloadCostLog)
                {
                    _costLog = await CostLogSnapshot.ReadAsync();
                    if (_closed) return;
                }
                var state = await AiBudgetRegistry.LoadAsync(card.Provider, _closing.Token);
                if (_closed) return;
                // A PUT answer was rendered meanwhile: it is newer than this GET -> drop the GET result.
                if (card.Epoch != epoch) return;
                card.ApplyState(state, _costLog, forceKey: null);
            }
            catch (OperationCanceledException)
            {
                // window closed
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, $"Refreshing the AI budgets of {card.Provider.ModuleId} failed", ex); } catch { }
                if (!_closed)
                {
                    card.ApplyState(new AiBudgetModuleState(card.Provider, AiBudgetAvailability.Error,
                        $"{card.Provider.Label}: the AI budgets could not be shown ({ex.GetType().Name}) — see the Backend Log", null),
                        _costLog, forceKey: null);
                }
            }
            finally
            {
                if (!_closed) card.EndLoading();
            }
        }

        // ===================== Saving (one PUT per budget, one at a time) =====================

        internal async Task SaveRowAsync(ModuleCard card, BudgetRow row)
        {
            if (_closed) return;
            if (row.Saving)
            {
                row.SaveAgainWhenDone = true;   // a newer edit arrived while the PUT was running
                return;
            }
            if (!row.TryGetValidValue(out var value)) return;
            if (row.Entry.LimitUsd == value)
            {
                row.MarkClean();
                return;
            }

            var version = row.EditVersion;
            row.Saving = true;
            row.SaveAgainWhenDone = false;
            row.ShowSaveStatus("Saving…", B("MutedForegroundBrush"), announce: false);
            AiBudgetPutResult result;
            try
            {
                await _putGate.WaitAsync(_closing.Token);
                try
                {
                    result = await AiBudgetRegistry.PutAsync(card.Provider, row.Key, value, _closing.Token);
                }
                finally
                {
                    _putGate.Release();
                }
            }
            catch (OperationCanceledException)
            {
                return;   // window closed
            }
            finally
            {
                row.Saving = false;
            }
            if (_closed) return;

            if (result.Ok && result.Payload is not null)
            {
                var unchangedSinceSend = row.EditVersion == version;
                // Re-render from the backend's answer; the saved row's text is forced to the stored
                // value unless the user typed again meanwhile.
                card.ApplyPayload(result.Payload, _costLog, unchangedSinceSend ? row.Key : null);
                var changed = result.Payload.Changed;
                var text = changed is not null && changed.New is decimal stored
                    ? $"Saved: {BudgetRow.LimitDisplay(changed.Old)} → {BudgetRow.LimitDisplay(stored)}"
                    : "Saved.";
                if (row.Entry.Applies == "next_cycle") text += " — applies from the next fund cycle";
                row.ShowSaveStatus(text, B("PositiveGreenBrush"), announce: true);
                if (!unchangedSinceSend || row.SaveAgainWhenDone)
                {
                    row.SaveAgainWhenDone = false;
                    row.RestartDebounce();
                }
            }
            else
            {
                row.FailedVersion = version;
                row.ShowSaveStatus("Not saved: " + (result.Error ?? "unknown error") +
                                   " (edit the value or press Enter to retry; Esc restores the stored value)",
                                   B("AccentRedBrush"), announce: true);
                if (row.EditVersion != version || row.SaveAgainWhenDone)
                {
                    // The user typed again while the refused PUT was running: save that newer value.
                    row.SaveAgainWhenDone = false;
                    row.RestartDebounce();
                }
                // No answer / token refused / outdated: the module state probably changed -> re-check it.
                if (result.StatusCode is null or 401 or 404)
                {
                    _ = RefreshCardAsync(card, reloadCostLog: false);
                }
            }
        }

        // ===================== Helpers =====================

        internal Brush B(string key) => (Brush)FindResource(key);

        /// <summary>"<figures> — from the cost log" (caption always present) + informational tooltip.</summary>
        internal void SetCostLogText(TextBlock target, string figures)
        {
            target.Inlines.Clear();
            target.Inlines.Add(new Run(figures));
            target.Inlines.Add(new Run(" — " + CostLogCaption) { FontStyle = FontStyles.Italic, Foreground = B("QuietForegroundBrush") });
            target.ToolTip = CostLogToolTip;
            AutomationProperties.SetName(target, figures + ", " + CostLogCaption + ". " + CostLogToolTip);
        }

        internal static void Announce(UIElement element)
        {
            try
            {
                var peer = UIElementAutomationPeer.FromElement(element) ?? UIElementAutomationPeer.CreatePeerForElement(element);
                peer?.RaiseAutomationEvent(AutomationEvents.LiveRegionChanged);
            }
            catch
            {
                // accessibility notification only
            }
        }

        private void RefreshAllButton_Click(object sender, RoutedEventArgs e)
        {
            _ = RefreshAllAsync();
        }

        // ===================== Window chrome (full screen + drag-restore, see CLAUDE.md) =====================

        // Dragging the title bar of a MAXIMIZED window: DragMove() does nothing there, so the window is
        // restored as soon as the mouse actually moves with the button held (a plain click does nothing),
        // placed so the cursor keeps its relative spot on the title bar, then dragged
        // (same as HedgeFund/HedgeFundWindow.xaml.cs / TransactionLogWindow.xaml.cs).
        private bool _restoreOnDrag;
        private Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (e.ClickCount == 2)
            {
                WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                return;
            }
            if (WindowState == WindowState.Maximized)
            {
                _restoreOnDrag = true;
                _restoreDragStart = e.GetPosition(this);
                ((UIElement)sender).CaptureMouse();
                return;
            }
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseMove(object sender, MouseEventArgs e)
        {
            if (!_restoreOnDrag || e.LeftButton != MouseButtonState.Pressed) return;
            var pos = e.GetPosition(this);
            if (Math.Abs(pos.X - _restoreDragStart.X) < SystemParameters.MinimumHorizontalDragDistance &&
                Math.Abs(pos.Y - _restoreDragStart.Y) < SystemParameters.MinimumVerticalDragDistance) return;

            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();

            var ratioX = ActualWidth > 0 ? pos.X / ActualWidth : 0.5;
            var restoreWidth = RestoreBounds.Width;
            // Cursor position in screen DIPs (PointToScreen returns device pixels).
            var toDip = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice ?? Matrix.Identity;
            var cursor = toDip.Transform(PointToScreen(pos));

            WindowState = WindowState.Normal;
            Left = cursor.X - restoreWidth * ratioX;
            Top = cursor.Y - 9; // middle of the 18 px title bar
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
        {
            if (!_restoreOnDrag) return;
            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();
        }

        // A maximized borderless window is placed by Windows so that its invisible resize frame hangs off
        // the screen edges, cutting off the title bar and the top corners. Pad the content by that frame
        // while maximized and drop the rounded corners there (same fix as HedgeFund/HedgeFundWindow.xaml.cs).
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            if (WindowState == WindowState.Maximized)
            {
                var frame = SystemParameters.WindowResizeBorderThickness;
                const double paddedBorder = 4; // SM_CXPADDEDBORDER, not exposed by SystemParameters
                RootBorder.Margin = new Thickness(frame.Left + paddedBorder, frame.Top + paddedBorder,
                                                  frame.Right + paddedBorder, frame.Bottom + paddedBorder);
                RootBorder.CornerRadius = new CornerRadius(0);
                TitleBarBorder.CornerRadius = new CornerRadius(0);
            }
            else
            {
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(8);
                TitleBarBorder.CornerRadius = new CornerRadius(8, 8, 0, 0);
            }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        // =====================================================================================
        // One module card
        // =====================================================================================

        internal sealed class ModuleCard
        {
            private readonly AiBudgetsWindow _owner;
            private readonly Ellipse _stateDot;
            private readonly TextBlock _stateText;
            private readonly TextBlock _updatedText;
            private readonly Button _refreshButton;
            private readonly TextBlock _messageText;
            private readonly TextBlock _hintText;
            private readonly TextBlock _moduleCostText;
            private readonly StackPanel _featureCostPanel;
            private readonly StackPanel _rowsPanel;
            private readonly Dictionary<string, BudgetRow> _rows = new(StringComparer.Ordinal);
            private string _rowSignature = string.Empty;
            private DateTimeOffset? _lastUpdatedUtc;

            public ModuleCard(AiBudgetsWindow owner, IAiBudgetProvider provider)
            {
                _owner = owner;
                Provider = provider;

                _stateDot = new Ellipse { Width = 7, Height = 7, Fill = owner.B("MutedForegroundBrush"), VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 8, 0) };
                var title = new TextBlock
                {
                    Text = provider.Label,
                    FontSize = 14,
                    FontWeight = FontWeights.SemiBold,
                    Foreground = owner.B("PrimaryForegroundBrush"),
                    VerticalAlignment = VerticalAlignment.Center,
                };
                _stateText = new TextBlock
                {
                    Text = "checking…",
                    FontSize = 11,
                    FontFamily = new FontFamily("Consolas"),
                    Foreground = owner.B("MutedForegroundBrush"),
                    VerticalAlignment = VerticalAlignment.Center,
                    Margin = new Thickness(10, 1, 0, 0),
                };
                AutomationProperties.SetLiveSetting(_stateText, AutomationLiveSetting.Polite);
                _updatedText = new TextBlock
                {
                    FontSize = 10.5,
                    FontFamily = new FontFamily("Consolas"),
                    Foreground = owner.B("QuietForegroundBrush"),
                    VerticalAlignment = VerticalAlignment.Center,
                    Margin = new Thickness(0, 0, 10, 0),
                };
                _refreshButton = new Button
                {
                    Style = (Style)owner.FindResource("BudgetButtonStyle"),
                    Content = new TextBlock { Text = "↻ Refresh" },
                    ToolTip = $"Reload the {provider.Label} budgets",
                };
                AutomationProperties.SetName(_refreshButton, $"Refresh {provider.Label} budgets");
                _refreshButton.Click += (_, __) => _ = owner.RefreshCardAsync(this, reloadCostLog: true);

                var right = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
                right.Children.Add(_updatedText);
                right.Children.Add(_refreshButton);
                DockPanel.SetDock(right, Dock.Right);

                var left = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
                left.Children.Add(_stateDot);
                left.Children.Add(title);
                left.Children.Add(_stateText);

                var header = new DockPanel { LastChildFill = true };
                header.Children.Add(right);
                header.Children.Add(left);

                _messageText = new TextBlock
                {
                    FontSize = 12,
                    TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 10, 0, 0),
                    Visibility = Visibility.Collapsed,
                };
                AutomationProperties.SetLiveSetting(_messageText, AutomationLiveSetting.Polite);
                _hintText = new TextBlock
                {
                    FontSize = 11,
                    TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 3, 0, 0),
                    Foreground = owner.B("QuietForegroundBrush"),
                    Visibility = Visibility.Collapsed,
                };
                _moduleCostText = new TextBlock
                {
                    FontSize = 10.5,
                    TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 8, 0, 0),
                    Foreground = owner.B("MutedForegroundBrush"),
                };
                _featureCostPanel = new StackPanel { Margin = new Thickness(0, 4, 0, 0), Visibility = Visibility.Collapsed };
                _rowsPanel = new StackPanel { Margin = new Thickness(0, 6, 0, 0) };

                var body = new StackPanel();
                body.Children.Add(header);
                body.Children.Add(_messageText);
                body.Children.Add(_hintText);
                body.Children.Add(_moduleCostText);
                body.Children.Add(_featureCostPanel);
                body.Children.Add(_rowsPanel);

                Root = new Border
                {
                    Background = owner.B("ModulePanelBackgroundBrush"),
                    BorderBrush = owner.B("AccentBorderBrush"),
                    BorderThickness = new Thickness(1),
                    CornerRadius = (CornerRadius)owner.FindResource("LargeCornerRadius"),
                    Padding = new Thickness(16, 12, 16, 12),
                    Margin = new Thickness(0, 0, 0, 12),
                    Child = body,
                };
                AutomationProperties.SetName(Root, $"{provider.Label} AI budgets");
                owner.SetCostLogText(_moduleCostText, owner._costLog.ModuleLine(provider.CostLogModules));
            }

            public IAiBudgetProvider Provider { get; }
            public Border Root { get; }
            public bool Loading { get; private set; }
            /// <summary>Incremented whenever a PUT answer is rendered (newer than any GET in flight).</summary>
            public int Epoch { get; private set; }

            public void BeginLoading()
            {
                Loading = true;
                _refreshButton.IsEnabled = false;
                _updatedText.Text = "refreshing…";
            }

            public void EndLoading()
            {
                Loading = false;
                _refreshButton.IsEnabled = true;
                _updatedText.Text = _lastUpdatedUtc is DateTimeOffset t ? "updated " + t.ToLocalTime().ToString("HH:mm:ss", Inv) : string.Empty;
            }

            public void StopTimers()
            {
                foreach (var row in _rows.Values) row.StopTimer();
            }

            public void ApplyPayload(AiBudgetPayload payload, CostLogSnapshot costLog, string? forceKey)
            {
                Epoch++;
                ApplyState(new AiBudgetModuleState(Provider, AiBudgetAvailability.Ok, string.Empty, payload), costLog, forceKey);
                if (!Loading) EndLoading();   // refresh the "updated" time
            }

            public void ApplyState(AiBudgetModuleState state, CostLogSnapshot costLog, string? forceKey)
            {
                _lastUpdatedUtc = state.LoadedAtUtc;
                _owner.SetCostLogText(_moduleCostText, costLog.ModuleLine(Provider.CostLogModules));

                var (dot, text) = state.Availability switch
                {
                    AiBudgetAvailability.Ok => ("PositiveGreenBrush", Provider is IAiBudgetLocalSource ? "in the app" : "running"),
                    AiBudgetAvailability.NotRunning => ("MutedForegroundBrush", "backend not running"),
                    AiBudgetAvailability.NotInstalled => ("MutedForegroundBrush", "not installed"),
                    AiBudgetAvailability.Outdated => ("AccentAmberBrush", "backend outdated"),
                    AiBudgetAvailability.Invalid => ("AccentRedBrush", "unusable answer"),
                    _ => ("AccentRedBrush", "error"),
                };
                _stateDot.Fill = _owner.B(dot);
                _stateText.Text = text;
                _stateText.Foreground = _owner.B(dot);
                AutomationProperties.SetName(Root, $"{Provider.Label} AI budgets, {text}");

                if (state.Availability == AiBudgetAvailability.Ok && state.Payload is { Budgets: { } budgets })
                {
                    _messageText.Visibility = Visibility.Collapsed;
                    _hintText.Visibility = Visibility.Collapsed;
                    _featureCostPanel.Visibility = Visibility.Collapsed;
                    _featureCostPanel.Children.Clear();
                    RenderRows(budgets, costLog, forceKey);
                    return;
                }

                // Not usable: one line, no inputs, no values from an earlier answer (no stand-ins).
                ClearRows();
                var previous = _messageText.Text;
                _messageText.Text = state.Message;
                _messageText.Foreground = state.Availability switch
                {
                    AiBudgetAvailability.NotRunning or AiBudgetAvailability.NotInstalled => _owner.B("SecondaryForegroundBrush"),
                    AiBudgetAvailability.Outdated => _owner.B("AccentAmberBrush"),
                    _ => _owner.B("AccentRedBrush"),
                };
                _messageText.Visibility = Visibility.Visible;
                if (previous != state.Message) Announce(_messageText);

                _hintText.Text = Provider.StartHint;
                _hintText.Visibility = state.Availability == AiBudgetAvailability.NotRunning ? Visibility.Visible : Visibility.Collapsed;

                RenderFeatureCosts(costLog);
            }

            // Read-only per-feature totals while the backend can't be asked (cost log only, labelled).
            private void RenderFeatureCosts(CostLogSnapshot costLog)
            {
                _featureCostPanel.Children.Clear();
                var lines = costLog.FeatureLines(Provider.CostLogModules, 8);
                if (lines.Count == 0)
                {
                    _featureCostPanel.Visibility = Visibility.Collapsed;
                    return;
                }
                _featureCostPanel.Children.Add(new TextBlock
                {
                    Text = "By feature this month (UTC), read-only:",
                    FontSize = 10.5,
                    Foreground = _owner.B("QuietForegroundBrush"),
                    Margin = new Thickness(0, 2, 0, 2),
                });
                foreach (var line in lines)
                {
                    var tb = new TextBlock
                    {
                        FontSize = 10.5,
                        TextWrapping = TextWrapping.Wrap,
                        Foreground = _owner.B("MutedForegroundBrush"),
                        Margin = new Thickness(10, 1, 0, 0),
                    };
                    _owner.SetCostLogText(tb, line);
                    _featureCostPanel.Children.Add(tb);
                }
                _featureCostPanel.Visibility = Visibility.Visible;
            }

            private void ClearRows()
            {
                foreach (var row in _rows.Values) row.StopTimer();
                _rows.Clear();
                _rowsPanel.Children.Clear();
                _rowSignature = string.Empty;
            }

            private void RenderRows(List<AiBudgetEntry> entries, CostLogSnapshot costLog, string? forceKey)
            {
                var signature = string.Join("\n", entries.Select(e => e.Key + "\t" + e.Group));
                if (signature != _rowSignature || _rowsPanel.Children.Count == 0)
                {
                    // Rebuild the layout; rows of keys that are still present are reused (keeps unsaved edits).
                    var old = new Dictionary<string, BudgetRow>(_rows, StringComparer.Ordinal);
                    _rows.Clear();
                    _rowsPanel.Children.Clear();
                    if (entries.Count == 0)
                    {
                        _rowsPanel.Children.Add(new TextBlock
                        {
                            Text = "This backend reports no AI budgets.",
                            FontSize = 11.5,
                            Foreground = _owner.B("SecondaryForegroundBrush"),
                            Margin = new Thickness(0, 8, 0, 0),
                        });
                    }
                    string? group = null;
                    var first = true;
                    foreach (var entry in entries)
                    {
                        var key = entry.Key!;
                        if (first || !string.Equals(group, entry.Group, StringComparison.Ordinal))
                        {
                            group = entry.Group;
                            first = false;
                            if (!string.IsNullOrEmpty(group))
                            {
                                _rowsPanel.Children.Add(new TextBlock
                                {
                                    Text = group.ToUpperInvariant(),
                                    FontSize = 10.5,
                                    FontFamily = new FontFamily("Consolas"),
                                    FontWeight = FontWeights.SemiBold,
                                    Foreground = _owner.B("SecondaryForegroundBrush"),
                                    Margin = new Thickness(0, 12, 0, 2),
                                });
                            }
                        }
                        if (!old.Remove(key, out var row)) row = new BudgetRow(_owner, this, key);
                        _rows[key] = row;
                        _rowsPanel.Children.Add(row.Root);
                    }
                    foreach (var gone in old.Values) gone.StopTimer();
                    _rowSignature = signature;
                }

                foreach (var entry in entries)
                {
                    if (_rows.TryGetValue(entry.Key!, out var row))
                    {
                        row.Apply(entry, costLog, force: entry.Key == forceKey);
                    }
                }
            }
        }

        // =====================================================================================
        // One budget row
        // =====================================================================================

        internal sealed class BudgetRow
        {
            private readonly AiBudgetsWindow _owner;
            private readonly ModuleCard _card;
            private readonly TextBlock _labelText;
            private readonly TextBlock _metaText;
            private readonly TextBlock _messageText;
            private readonly TextBlock _notesText;
            private readonly TextBlock _costText;
            private readonly TextBox _input;
            private readonly TextBlock _rangeHint;
            private readonly Button _defaultButton;
            private readonly TextBlock _saveStatus;
            private readonly TextBlock _spentText;
            private readonly TextBlock _leftText;
            private readonly DispatcherTimer _debounce;
            private bool _suppressTextChanged;

            public BudgetRow(AiBudgetsWindow owner, ModuleCard card, string key)
            {
                _owner = owner;
                _card = card;
                Key = key;
                Entry = new AiBudgetEntry { Key = key };

                _labelText = new TextBlock { FontSize = 12.5, FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap, Foreground = owner.B("PrimaryForegroundBrush") };
                _metaText = new TextBlock { FontSize = 10.5, Margin = new Thickness(0, 2, 0, 0), TextWrapping = TextWrapping.Wrap, Foreground = owner.B("MutedForegroundBrush") };
                _messageText = new TextBlock { FontSize = 11, Margin = new Thickness(0, 4, 0, 0), TextWrapping = TextWrapping.Wrap, Visibility = Visibility.Collapsed };
                AutomationProperties.SetLiveSetting(_messageText, AutomationLiveSetting.Polite);
                _notesText = new TextBlock { FontSize = 10.5, Margin = new Thickness(0, 4, 0, 0), TextWrapping = TextWrapping.Wrap, Foreground = owner.B("QuietForegroundBrush") };
                _costText = new TextBlock { FontSize = 10.5, Margin = new Thickness(0, 4, 0, 0), TextWrapping = TextWrapping.Wrap, Foreground = owner.B("MutedForegroundBrush"), Visibility = Visibility.Collapsed };

                var info = new StackPanel { Margin = new Thickness(0, 0, 16, 0) };
                info.Children.Add(_labelText);
                info.Children.Add(_metaText);
                info.Children.Add(_messageText);
                info.Children.Add(_notesText);
                info.Children.Add(_costText);

                _input = new TextBox
                {
                    Style = (Style)owner.FindResource("BudgetInputStyle"),
                    Width = 86,
                    MaxLength = 12,
                    Tag = this,
                };
                _input.TextChanged += Input_TextChanged;
                _input.PreviewKeyDown += Input_PreviewKeyDown;
                _input.LostKeyboardFocus += (_, __) => SaveNow(explicitRequest: false);

                var inputLine = new StackPanel { Orientation = Orientation.Horizontal };
                inputLine.Children.Add(new TextBlock
                {
                    Text = "$",
                    FontFamily = new FontFamily("Consolas"),
                    FontSize = 12.5,
                    Foreground = owner.B("SecondaryForegroundBrush"),
                    VerticalAlignment = VerticalAlignment.Center,
                    Margin = new Thickness(0, 0, 4, 0),
                });
                inputLine.Children.Add(_input);

                _rangeHint = new TextBlock { FontSize = 10, Margin = new Thickness(0, 3, 0, 0), Foreground = owner.B("MutedForegroundBrush"), TextWrapping = TextWrapping.Wrap, MaxWidth = 200 };
                AutomationProperties.SetLiveSetting(_rangeHint, AutomationLiveSetting.Polite);
                _defaultButton = new Button { Style = (Style)owner.FindResource("BudgetLinkStyle"), Margin = new Thickness(0, 3, 0, 0), HorizontalAlignment = HorizontalAlignment.Left };
                _defaultButton.Click += DefaultButton_Click;
                _saveStatus = new TextBlock { FontSize = 10.5, Margin = new Thickness(0, 3, 0, 0), TextWrapping = TextWrapping.Wrap, MaxWidth = 220, Visibility = Visibility.Collapsed };
                AutomationProperties.SetLiveSetting(_saveStatus, AutomationLiveSetting.Polite);

                var edit = new StackPanel { Margin = new Thickness(0, 0, 16, 0), Width = 220 };
                edit.Children.Add(inputLine);
                edit.Children.Add(_rangeHint);
                edit.Children.Add(_defaultButton);
                edit.Children.Add(_saveStatus);

                _spentText = new TextBlock { FontSize = 12, FontFamily = new FontFamily("Consolas"), HorizontalAlignment = HorizontalAlignment.Right, Foreground = owner.B("PrimaryForegroundBrush"), TextWrapping = TextWrapping.Wrap, TextAlignment = TextAlignment.Right };
                _leftText = new TextBlock { FontSize = 11, FontFamily = new FontFamily("Consolas"), HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 3, 0, 0), Foreground = owner.B("SecondaryForegroundBrush") };
                var spend = new StackPanel { Width = 190 };
                spend.Children.Add(_spentText);
                spend.Children.Add(_leftText);

                var grid = new Grid();
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                Grid.SetColumn(info, 0);
                Grid.SetColumn(edit, 1);
                Grid.SetColumn(spend, 2);
                grid.Children.Add(info);
                grid.Children.Add(edit);
                grid.Children.Add(spend);

                Root = new Border
                {
                    BorderBrush = owner.B("SubtleBorderBrush"),
                    BorderThickness = new Thickness(0, 1, 0, 0),
                    Padding = new Thickness(0, 10, 0, 10),
                    Child = grid,
                };

                _debounce = new DispatcherTimer(DispatcherPriority.Background, owner.Dispatcher) { Interval = SaveDelay };
                _debounce.Tick += (_, __) =>
                {
                    _debounce.Stop();
                    SaveNow(explicitRequest: false);
                };
            }

            public string Key { get; }
            public AiBudgetEntry Entry { get; private set; }
            public Border Root { get; }
            /// <summary>The field holds a user edit that is not stored yet.</summary>
            public bool Dirty { get; private set; }
            public int EditVersion { get; private set; }
            /// <summary>EditVersion of the last refused PUT (not retried automatically, only on Enter).</summary>
            public int FailedVersion { get; set; } = -1;
            public bool Saving { get; set; }
            public bool SaveAgainWhenDone { get; set; }

            private bool CanEdit =>
                Entry.Editable && Entry.Min is decimal min && Entry.Max is decimal max && min <= max;

            // ---------- rendering ----------

            public void Apply(AiBudgetEntry entry, CostLogSnapshot costLog, bool force)
            {
                Entry = entry;
                var label = entry.Label ?? Key;
                _labelText.Text = label;

                var state = entry.LimitUsd is null ? "limit unknown" : entry.Enabled ? "on" : "off (0 = off)";
                _metaText.Text = $"{PeriodText(entry.Period)} · {state} · {AppliesText(entry.Applies)}";

                var status = entry.Status ?? "ok";
                if (!string.IsNullOrEmpty(entry.Message))
                {
                    var previous = _messageText.Text;
                    _messageText.Text = entry.Message;
                    _messageText.Foreground = status switch
                    {
                        "blocked" or "paused" => _owner.B("AccentAmberBrush"),
                        "error" => _owner.B("AccentRedBrush"),
                        _ => _owner.B("SecondaryForegroundBrush"),
                    };
                    _messageText.Visibility = Visibility.Visible;
                    if (previous != entry.Message) Announce(_messageText);
                }
                else
                {
                    _messageText.Text = string.Empty;
                    _messageText.Visibility = Visibility.Collapsed;
                }

                _notesText.Text = entry.Notes ?? string.Empty;
                _notesText.Visibility = string.IsNullOrEmpty(entry.Notes) ? Visibility.Collapsed : Visibility.Visible;

                _spentText.Text = $"Spent {AiCostLedger.FormatUsd(entry.SpentUsd)} of {LimitDisplay(entry.LimitUsd)}";
                _leftText.Text = $"{AiCostLedger.FormatUsd(entry.RemainingUsd)} left";

                RenderCostLog(entry, costLog);

                var canEdit = CanEdit;
                _input.IsEnabled = canEdit;
                if (canEdit)
                {
                    _rangeHint.Text = RangeText(entry);
                    _rangeHint.Foreground = _owner.B("MutedForegroundBrush");
                }
                else
                {
                    _rangeHint.Text = "not editable here";
                    _rangeHint.Foreground = _owner.B("MutedForegroundBrush");
                }
                if (canEdit && entry.DefaultUsd is decimal def)
                {
                    _defaultButton.Content = new TextBlock { Text = "Default: " + LimitDisplay(def) };
                    AutomationProperties.SetName(_defaultButton, $"Reset {label} to the default {LimitDisplay(def)}");
                    _defaultButton.Visibility = Visibility.Visible;
                }
                else
                {
                    _defaultButton.Visibility = Visibility.Collapsed;
                }

                var accessible = $"{_card.Provider.Label}, {label}: limit in US dollars {PeriodText(entry.Period)}";
                AutomationProperties.SetName(_input, accessible);
                AutomationProperties.SetHelpText(_input, (canEdit ? RangeText(entry) + ". " : "Not editable. ") +
                    $"Spent {AiCostLedger.FormatUsd(entry.SpentUsd)}, {AiCostLedger.FormatUsd(entry.RemainingUsd)} left. Enter saves, Escape restores the stored value, Up/Down change by one step.");
                AutomationProperties.SetName(Root, $"{label}, {PeriodText(entry.Period)}, {state}, spent {AiCostLedger.FormatUsd(entry.SpentUsd)} of {LimitDisplay(entry.LimitUsd)}");

                if (force)
                {
                    Dirty = false;
                    FailedVersion = -1;
                    SetTextSilently(LimitText(entry.LimitUsd));
                }
                else if (!Dirty && !Saving)
                {
                    SetTextSilently(LimitText(entry.LimitUsd));
                }
            }

            private void RenderCostLog(AiBudgetEntry entry, CostLogSnapshot costLog)
            {
                var refs = entry.CostLog;
                var window = entry.Period switch
                {
                    "day" => CostWindow.Day,
                    "month" => CostWindow.Month,
                    _ => (CostWindow?)null,   // "run" (cap per loop run) has no calendar window
                };
                if (refs is null || refs.Count == 0 || window is null)
                {
                    _costText.Visibility = Visibility.Collapsed;
                    return;
                }
                if (costLog.IsPlaceholder || !costLog.FileExists)
                {
                    _owner.SetCostLogText(_costText, costLog.IsPlaceholder ? "Cost log: reading…" : "Cost log: no AI requests recorded yet");
                    _costText.Visibility = Visibility.Visible;
                    return;
                }
                var totals = costLog.Sum(e => refs.Any(r => string.Equals(r.Module, e.Module, StringComparison.Ordinal) &&
                                                            string.Equals(r.Feature, e.Feature, StringComparison.Ordinal)),
                                         window.Value);
                var when = window == CostWindow.Day ? "today (UTC)" : "this month (UTC)";
                _owner.SetCostLogText(_costText, $"Cost log: {totals.Describe()} {when}");
                _costText.Visibility = Visibility.Visible;
            }

            private void SetTextSilently(string text)
            {
                if (_input.Text == text) return;
                _suppressTextChanged = true;
                try { _input.Text = text; }
                finally { _suppressTextChanged = false; }
            }

            public void ShowSaveStatus(string text, Brush brush, bool announce)
            {
                _saveStatus.Text = text;
                _saveStatus.Foreground = brush;
                _saveStatus.Visibility = Visibility.Visible;
                if (announce) Announce(_saveStatus);
            }

            private void HideSaveStatus()
            {
                _saveStatus.Text = string.Empty;
                _saveStatus.Visibility = Visibility.Collapsed;
            }

            // ---------- editing ----------

            private void Input_TextChanged(object sender, TextChangedEventArgs e)
            {
                if (_suppressTextChanged) return;
                EditVersion++;
                Dirty = true;
                if (!Saving) HideSaveStatus();
                _debounce.Stop();

                if (!TryParseLimit(_input.Text, Entry, out var value, out var error))
                {
                    _rangeHint.Text = error;
                    _rangeHint.Foreground = _owner.B("AccentRedBrush");
                    return;   // invalid -> inline hint, no PUT
                }
                _rangeHint.Text = RangeText(Entry);
                _rangeHint.Foreground = _owner.B("MutedForegroundBrush");
                if (Entry.LimitUsd == value && !Saving)
                {
                    MarkClean();
                    return;
                }
                _debounce.Start();
            }

            private void Input_PreviewKeyDown(object sender, KeyEventArgs e)
            {
                if (e.Key == System.Windows.Input.Key.Enter)
                {
                    SaveNow(explicitRequest: true);
                    e.Handled = true;
                }
                else if (e.Key is System.Windows.Input.Key.Up or System.Windows.Input.Key.Down && CanEdit)
                {
                    var step = Entry.Step is decimal s && s > 0 ? s : 0.1m;
                    var current = TryParseLimit(_input.Text, Entry, out var v, out _) ? v : Entry.LimitUsd ?? Entry.Min ?? 0m;
                    var next = current + (e.Key == System.Windows.Input.Key.Up ? step : -step);
                    next = Math.Max(Entry.Min ?? 0m, Math.Min(Entry.Max ?? next, next));
                    _input.Text = LimitText(Math.Round(next, 6));   // raises TextChanged -> debounce
                    _input.CaretIndex = _input.Text.Length;
                    e.Handled = true;
                }
            }

            private void DefaultButton_Click(object sender, RoutedEventArgs e)
            {
                if (!CanEdit || Entry.DefaultUsd is not decimal def) return;
                _input.Text = LimitText(def);   // TextChanged -> validation + 600 ms save
                _input.Focus();
                _input.CaretIndex = _input.Text.Length;
            }

            public void SaveNow(bool explicitRequest)
            {
                _debounce.Stop();
                if (!Dirty || !CanEdit) return;
                if (!explicitRequest && EditVersion == FailedVersion) return;   // refused before: retry only on Enter
                _ = _owner.SaveRowAsync(_card, this);
            }

            public void RestartDebounce()
            {
                _debounce.Stop();
                if (Dirty) _debounce.Start();
            }

            public bool TryGetValidValue(out decimal value)
            {
                if (TryParseLimit(_input.Text, Entry, out value, out var error)) return true;
                _rangeHint.Text = error;
                _rangeHint.Foreground = _owner.B("AccentRedBrush");
                return false;
            }

            public void MarkClean()
            {
                Dirty = false;
                FailedVersion = -1;
                _debounce.Stop();
            }

            public void Revert()
            {
                MarkClean();
                HideSaveStatus();
                SetTextSilently(LimitText(Entry.LimitUsd));
                if (CanEdit)
                {
                    _rangeHint.Text = RangeText(Entry);
                    _rangeHint.Foreground = _owner.B("MutedForegroundBrush");
                }
            }

            public void StopTimer() => _debounce.Stop();

            // ---------- formatting / parsing ----------

            internal static string PeriodText(string? period) => period switch
            {
                "day" => "per day (UTC)",
                "month" => "per month (UTC)",
                "run" => "per run",
                null or "" => "period unknown",
                _ => "per " + period,
            };

            private static string AppliesText(string? applies) => applies switch
            {
                "next_cycle" => "applies from the next fund cycle",
                "immediately" or null or "" => "applies immediately",
                _ => "applies: " + applies,
            };

            private static string RangeText(AiBudgetEntry entry) =>
                $"{Num(entry.Min)}–{Num(entry.Max)} USD" + (entry.Min == 0m ? " · 0 = off" : string.Empty);

            private static string Num(decimal? v) => v is decimal d ? d.ToString("0.##", Inv) : "—";

            /// <summary>Text for the input field ("1.00", "0.005"); empty when the limit is unknown.</summary>
            internal static string LimitText(decimal? v) => v is decimal d ? d.ToString("0.00####", Inv) : string.Empty;

            /// <summary>"$1.00" / "—".</summary>
            internal static string LimitDisplay(decimal? v) => v is decimal d ? "$" + d.ToString("#,##0.00####", Inv) : "—";

            /// <summary>Accepts "1.5", "1,5", "$1.50"; no signs, no exponents, no thousands separators.</summary>
            internal static bool TryParseLimit(string? text, AiBudgetEntry entry, out decimal value, out string error)
            {
                value = 0m;
                var range = $"Enter a number {Num(entry.Min)}–{Num(entry.Max)}" + (entry.Min == 0m ? " (0 = off)" : string.Empty);
                error = range;
                var s = (text ?? string.Empty).Trim();
                if (s.StartsWith("$", StringComparison.Ordinal)) s = s.Substring(1).TrimStart();
                s = s.Replace(',', '.');
                if (s.Length == 0) return false;
                if (!decimal.TryParse(s, NumberStyles.AllowDecimalPoint, Inv, out var parsed)) return false;
                parsed = Math.Round(parsed, 6);
                if (entry.Min is not decimal min || entry.Max is not decimal max) { error = "not editable here"; return false; }
                if (parsed < min || parsed > max) return false;
                value = parsed;
                error = string.Empty;
                return true;
            }
        }

        // =====================================================================================
        // Cost-log snapshot (informational only)
        // =====================================================================================

        internal enum CostWindow { Day, Month }

        internal readonly struct CostTotals
        {
            public CostTotals(decimal usd, int calls, int withoutCost)
            {
                Usd = usd;
                Calls = calls;
                WithoutCost = withoutCost;
            }

            public decimal Usd { get; }
            public int Calls { get; }
            public int WithoutCost { get; }

            /// <summary>"$0.012 (3 calls, 1 without a reported cost)".</summary>
            public string Describe()
            {
                var calls = Calls == 1 ? "1 call" : Calls.ToString("N0", Inv) + " calls";
                var extra = WithoutCost > 0 ? $", {WithoutCost.ToString("N0", Inv)} without a reported cost" : string.Empty;
                return $"{AiCostLedger.FormatUsd(Usd)} ({calls}{extra})";
            }
        }

        internal sealed class CostLogSnapshot
        {
            /// <summary>Before the first read: shows "reading…", never $0.</summary>
            public static readonly CostLogSnapshot Empty = new(new List<AiCostEntry>(), fileExists: false, DateTimeOffset.UtcNow, isPlaceholder: true);

            private CostLogSnapshot(List<AiCostEntry> entries, bool fileExists, DateTimeOffset nowUtc, bool isPlaceholder = false)
            {
                Entries = entries;
                FileExists = fileExists;
                NowUtc = nowUtc;
                IsPlaceholder = isPlaceholder;
            }

            public bool IsPlaceholder { get; }

            public IReadOnlyList<AiCostEntry> Entries { get; }
            public bool FileExists { get; }
            public DateTimeOffset NowUtc { get; }

            public static async Task<CostLogSnapshot> ReadAsync()
            {
                var now = DateTimeOffset.UtcNow;
                bool exists;
                try { exists = File.Exists(AiCostLedger.LedgerPath); } catch { exists = false; }
                List<AiCostEntry> entries;
                try
                {
                    entries = await Task.Run(() => AiCostLedger.ReadAll());
                }
                catch
                {
                    entries = new List<AiCostEntry>();
                }
                return new CostLogSnapshot(entries, exists, now);
            }

            private bool InWindow(AiCostEntry entry, CostWindow window)
            {
                if (entry.Timestamp is not DateTimeOffset ts) return false;
                var u = ts.UtcDateTime;
                var n = NowUtc.UtcDateTime;
                return window == CostWindow.Day
                    ? u.Date == n.Date
                    : u.Year == n.Year && u.Month == n.Month;
            }

            public CostTotals Sum(Func<AiCostEntry, bool> match, CostWindow window) => Totals(Entries.Where(match), window);

            private CostTotals Totals(IEnumerable<AiCostEntry> items, CostWindow window)
            {
                decimal usd = 0m;
                int calls = 0, withoutCost = 0;
                foreach (var entry in items)
                {
                    if (!InWindow(entry, window)) continue;
                    calls++;
                    if (entry.CostUsd is decimal cost) usd += cost;
                    else withoutCost++;
                }
                return new CostTotals(usd, calls, withoutCost);
            }

            /// <summary>"Cost log: $a today (UTC) · $b this month (UTC)" for these cost-log module names.</summary>
            public string ModuleLine(IReadOnlyList<string> modules)
            {
                if (IsPlaceholder) return "Cost log: reading…";
                if (!FileExists) return "Cost log: no AI requests recorded yet";
                bool Match(AiCostEntry e) => modules.Contains(e.Module, StringComparer.Ordinal);
                var today = Sum(Match, CostWindow.Day);
                var month = Sum(Match, CostWindow.Month);
                var line = $"Cost log: {AiCostLedger.FormatUsd(today.Usd)} today (UTC) · {AiCostLedger.FormatUsd(month.Usd)} this month (UTC)";
                if (month.WithoutCost > 0)
                {
                    line += month.WithoutCost == 1
                        ? " · 1 call without a reported cost"
                        : $" · {month.WithoutCost.ToString("N0", Inv)} calls without a reported cost";
                }
                return line;
            }

            /// <summary>Per-feature lines (this month, UTC) for a module whose backend can't be asked.</summary>
            public List<string> FeatureLines(IReadOnlyList<string> modules, int max)
            {
                var lines = new List<string>();
                if (!FileExists) return lines;
                var groups = Entries
                    .Where(e => modules.Contains(e.Module, StringComparer.Ordinal) && InWindow(e, CostWindow.Month))
                    .GroupBy(e => e.Feature ?? string.Empty, StringComparer.Ordinal)
                    .Select(g => new
                    {
                        Label = AiBudgetText.Clean(g.Select(e => e.FeatureLabel).LastOrDefault(l => !string.IsNullOrWhiteSpace(l)) ?? g.Key, 80),
                        Today = Totals(g, CostWindow.Day),
                        Month = Totals(g, CostWindow.Month),
                    })
                    .OrderByDescending(x => x.Month.Usd)
                    .Take(max);
                foreach (var g in groups)
                {
                    var name = string.IsNullOrEmpty(g.Label) ? "AI request" : g.Label;
                    lines.Add($"{name}: {AiCostLedger.FormatUsd(g.Today.Usd)} today · {g.Month.Describe()} this month");
                }
                return lines;
            }
        }
    }
}

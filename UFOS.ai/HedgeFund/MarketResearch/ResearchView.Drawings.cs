using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// MR5 — drawing tools (plan §4.1/§4.2, SPEC "MR5 backend notes" + "MR5 bridge contract (revision 2)"):
    /// vertical tool rail (all tools, magnet, lock/hide/delete all, undo/redo, style popup), object list panel
    /// (rename, show/hide, lock, delete, jump to, per-drawing timeframes, export/import) and persistence.
    ///
    /// Persistence = one FIFO queue of backend writes (POST / PATCH / DELETE), processed one after another:
    /// - the page reports finished steps (drawingCreated / drawingChanged final / drawingDeleted); object-panel edits go
    ///   THROUGH the page (editDrawing / removeDrawing → echo) so there is one write path and undo works;
    /// - a PATCH waits 400 ms and absorbs later PATCHes of the same drawing (style popup clicks);
    /// - the backend refuses (400 / 409 / 404 / 500) → the page reverts the step (drawingRejected) and the
    ///   backend's message is shown verbatim;
    /// - the backend is not reachable / unavailable (offline, 401, 503) → the queue PAUSES with a visible state
    ///   ("not saved yet — retrying in n s, k waiting") and retries 2, 5, 10, 30, 60, 60 … s — nothing is
    ///   dropped while UFOS.ai runs (bounded: at most 1000 waiting changes; more are refused at once with a
    ///   message). The queue lives in memory: closing UFOS.ai before it drains loses those changes (stated).
    /// </summary>
    public partial class ResearchView
    {
        private const int MaxQueuedDrawingOps = 1000; // ≥ 2 × MAX_DRAWINGS_PER_SYMBOL: "lock all" on 500 drawings fits
        private static readonly int[] DrawingRetrySeconds = { 2, 5, 10, 30, 60 };
        private static readonly TimeSpan PatchDebounce = TimeSpan.FromMilliseconds(400);
        private const int MaxImportBytes = 262_144;        // MAX_IMPORT_BYTES (compact JSON)
        private const int MaxImportFileBytes = 1_048_576;  // raw file read cap (an indented export is larger than compact)

        private sealed class DrawingOp
        {
            public string Kind = "";      // create · patch · delete
            public string Symbol = "";
            public string? LocalId;       // create
            public string? Id;            // patch / delete
            public int Epoch;
            public Dictionary<string, object?>? Body;
            public JsonObject? Before;    // host copy before the step (revert on rejection)
            public DateTime DueUtc;
            public bool InFlight;
        }

        private readonly List<DrawingOp> _drawingOps = new();
        // The last state the BACKEND confirmed per drawing id (load, POST/PATCH answers) — the authoritative state
        // sent with upsertDrawing after a refused change/delete (bridge revision 3, review M4).
        private readonly Dictionary<string, JsonObject> _confirmedDrawings = new(StringComparer.Ordinal);
        private bool _drawingWorkerBusy;
        private int _drawingRetryAttempt;
        private DateTime? _drawingRetryAtUtc;
        private string? _drawingPauseReason;
        private string? _drawingMessage;          // last refusal / load / import / export message (verbatim), until dismissed
        private string? _splitCheckMessage;
        private string? _drawingsLoadError;
        private readonly HashSet<string> _reloadDrawingsAfterDrain = new(StringComparer.Ordinal);
        private DispatcherTimer? _drawingTimer;
        private int _drawingsLoadGeneration;
        private bool _drawingsLoading;

        // ---------- rail state ----------
        private readonly Dictionary<string, Button> _toolButtons = new(StringComparer.Ordinal); // "" = cursor
        private string? _currentTool;
        private string _railColor = "accent";
        private int _railWidth = 1;
        private string _railDash = "solid";
        private Button? _magnetButton, _lockAllButton, _hideAllButton, _deleteAllButton, _undoButton, _redoButton, _styleButton, _saveMeasureButton;
        private string? _selectedDrawingId;
        private string? _selectedTool;
        private bool _selectedIsUnsaved;
        private bool _canUndo, _canRedo;
        private bool _confirmingDeleteAll;
        private StackPanel? _confirmPanel;

        // ---------- objects panel ----------
        private bool _objectsPanelOpen;
        private StackPanel? _objectsList;
        private TextBlock? _objectsHeader;
        private TextBlock? _objectsStatus;
        private Button? _objectsRetryButton;
        private Border? _importPanel;
        private TextBlock? _importText;
        private string? _pendingImport;      // compact UTF-8 JSON of the file (exactly what is sent)
        private string? _pendingImportSymbol;
        private string? _renamingId;
        private Popup? _stylePopup;
        private StackPanel? _stylePopupBody;
        private Popup? _tfPopup;
        private StackPanel? _tfPopupBody;

        /// <summary>Tools in rail order: (tool | null = cursor, glyph, label, shortcut).</summary>
        private static readonly (string? Tool, string Glyph, string Label, string? Shortcut)[] RailTools =
        {
            (null, "↖", "Cursor", "Esc"),
            ("segment", "╱", "Trend line", "Alt+T"),
            ("rayLine", "↗", "Ray", null),
            ("straightLine", "⟷", "Extended line", null),
            ("horizontalStraightLine", "―", "Horizontal line", "Alt+H"),
            ("horizontalRayLine", "→", "Horizontal ray", null),
            ("verticalStraightLine", "│", "Vertical line", null),
            ("priceLine", "$", "Price line (label at the price scale)", null),
            ("parallelStraightLine", "∥", "Parallel channel", null),
            ("priceChannelLine", "⫽", "Price channel", null),
            ("fibonacciLine", "Φ", "Fibonacci retracement (0 – 23.6 – 38.2 – 50 – 61.8 – 78.6 – 100)", "Alt+F"),
            ("rect", "▭", "Rectangle / zone", "Alt+R"),
            ("measure", "⇔", "Measure (Δ price, Δ %, bars, duration) — temporary; Enter or ✓ keeps it", null),
            ("simpleAnnotation", "T", "Text note (max 280 characters, plain text)", null),
            ("simpleTag", "⌑", "Tag", null),
            ("arrowUp", "▲", "Arrow marker up", null),
            ("arrowDown", "▼", "Arrow marker down", null),
            ("brush", "✎", "Freehand", null),
            ("alertLine", "⍾", "Alert line (the alert itself is linked in stage MR7)", null),
        };

        /// <summary>Palette keys → colours (bridge init.theme values; blue/violet = the page's fixed colours).</summary>
        private static readonly (string Key, string Hex, string Name)[] Swatches =
        {
            ("accent", "#5AC8FA", "accent blue"), ("up", "#4ADE80", "green (up)"), ("down", "#ED6A5A", "red (down)"),
            ("caution", "#F0B429", "amber"), ("text", "#BFBFBF", "light grey"), ("muted", "#6F6F6F", "dark grey"),
            ("blue", "#4C8DFF", "blue"), ("violet", "#B48CF2", "violet"),
        };

        // =====================================================================================
        // Setup / lifecycle hooks (called from ResearchView.xaml.cs)
        // =====================================================================================

        private void InitDrawingsAndIndicators()
        {
            _chart.DrawingCreated += Chart_DrawingCreated;
            _chart.DrawingChanged += Chart_DrawingChanged;
            _chart.DrawingDeleted += Chart_DrawingDeleted;
            _chart.DrawingSelectionChanged += Chart_DrawingSelectionChanged;
            _chart.UndoStateChanged += (_, s) => { _canUndo = s.CanUndo; _canRedo = s.CanRedo; RenderRail(); };
            _chart.PageToolChanged += (_, tool) => { _currentTool = tool; RenderRail(); };
            _chart.ShortcutPressed += (_, key) => HandleShortcut(key);
            _chart.PageReady += Chart_PageReady;
            _chart.DrawingNotice += (_, message) => { _drawingMessage = message; RenderDrawingStatus(); }; // page `drawing_invalid`
            _drawingTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromMilliseconds(250) };
            _drawingTimer.Tick += (_, _) => DrawingTimer_Tick();
            BuildToolRail();
            BuildObjectsPanel();
            BuildConfirmPanel();
            InitIndicators();
            RenderRail();
            RenderDrawingStatus();
        }

        private void OnNoSymbol()
        {
            _selectedDrawingId = null;
            _renamingId = null;
            _confirmingDeleteAll = false;
            CancelImport();
            if (_stylePopup != null) _stylePopup.IsOpen = false;
            if (_tfPopup != null) _tfPopup.IsOpen = false;
            CloseIndicatorsPopup();
            UpdateBellButton(); // MR7
            RenderRail();
            RenderObjects();
            RenderDrawingStatus();
        }

        private void ShutdownDrawings()
        {
            _drawingTimer?.Stop();
            FlushChartSettingsSave();
            if (_drawingOps.Count > 0)
                AppLog.Warn(LogModule, "Market Research: " + _drawingOps.Count + " drawing change(s) were not saved before the window closed (backend not reachable).");
        }

        /// <summary>After every successful bars load: the symbol's drawings when they are not loaded yet + its indicators.</summary>
        private void OnChartSeriesLoaded(ResearchTab tab, int generation)
        {
            if (_chart.PageRevision >= 2 && _chart.DrawingsSymbol != tab.Symbol) _ = LoadDrawingsAsync(tab);
            _ = LoadIndicatorsAsync(tab, generation);
            RenderRail();
        }

        private void Chart_PageReady(object? sender, EventArgs e)
        {
            // A new page (first start, pop-out/return, Reload): selection and undo history are gone.
            _selectedDrawingId = null;
            _selectedTool = null;
            _canUndo = _canRedo = false;
            var tab = _active;
            if (tab != null && _chart.PageRevision >= 2 && _chart.HasSeries && _chart.Symbol == tab.Symbol && _chart.DrawingsSymbol != tab.Symbol)
                _ = LoadDrawingsAsync(tab);
            RenderRail();
            RenderObjects();
            RenderDrawingStatus();
        }

        private bool DrawingToolsUsable => _active != null && _chart.PageRevision >= 2 && _chart.HasSeries && _chart.DrawingsSymbol == _active.Symbol;

        // =====================================================================================
        // Loading drawings
        // =====================================================================================

        private async Task LoadDrawingsAsync(ResearchTab tab)
        {
            var generation = ++_drawingsLoadGeneration;
            var symbol = tab.Symbol;
            _drawingsLoading = true;
            _drawingsLoadError = null;
            RenderObjects();
            PlatformResult<DrawingsResponse> result;
            try { result = await ResearchClient.GetDrawingsAsync(symbol); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: loading drawings failed.", ex);
                result = new PlatformResult<DrawingsResponse> { Status = PlatformCallStatus.Error, Message = "unexpected error — see the Backend Log." };
            }
            if (generation != _drawingsLoadGeneration || _shutDown) return;
            _drawingsLoading = false;
            if (_active == null || _active.Symbol != symbol || _chart.Symbol != symbol)
            {
                RenderObjects();
                return;
            }
            if (!result.Ok || result.Value == null)
            {
                _drawingsLoadError = "Drawings not loaded: " + (result.Ok ? "empty response from the backend." : result.Message);
                RenderRail();
                RenderObjects();
                RenderDrawingStatus();
                return;
            }
            var dropped = _chart.LoadDrawings(symbol, result.Value.Drawings);
            RememberConfirmed();
            if (_drawingOps.Any(o => o.Symbol == symbol)) _reloadDrawingsAfterDrain.Add(symbol); // queued changes are not in this answer yet
            var split = result.Value.SplitCheck;
            _splitCheckMessage = split?.State == "failed"
                ? "Split check failed: " + (split.Reason ?? "no reason reported") + " — drawings may be misplaced after a split."
                : null;
            if (dropped > 0) _drawingMessage = dropped + " stored drawing(s) could not be shown (invalid data — see the Backend Log).";
            _selectedDrawingId = null;
            RenderRail();
            RenderObjects();
            RenderDrawingStatus();
        }

        // =====================================================================================
        // Page events → persistence queue
        // =====================================================================================

        private void Chart_DrawingCreated(object? sender, PageDrawingCreated e)
        {
            var symbol = _chart.DrawingsSymbol;
            if (symbol == null || _active == null || symbol != _active.Symbol)
            {
                Reject(symbol ?? _active?.Symbol ?? "", "create", e.LocalId, null, e.Epoch, null,
                       "Drawings for this chart are not loaded — the drawing was not saved. Reload the drawings (objects panel) first.");
                return;
            }
            var body = ChartHost.CreateBody(symbol, e.Drawing, e.ClientId); // client_id: a retried POST never duplicates
            body?.Remove("alert_id"); // MR7: the backend owns the alert link (alert create / switch on/off)
            if (body == null)
            {
                Reject(symbol, "create", e.LocalId, null, e.Epoch, null, "The drawing has invalid points and was not saved.");
                return;
            }
            if (_drawingOps.Count >= MaxQueuedDrawingOps)
            {
                Reject(symbol, "create", e.LocalId, null, e.Epoch, null, QueueFullMessage);
                return;
            }
            Enqueue(new DrawingOp { Kind = "create", Symbol = symbol, LocalId = e.LocalId, Epoch = e.Epoch, Body = body, DueUtc = DateTime.UtcNow });
        }

        private void Chart_DrawingChanged(object? sender, PageDrawingChanged e)
        {
            var symbol = _chart.DrawingsSymbol;
            if (symbol == null) return;
            var body = ChartHost.PatchBody(e.Changes);
            if (body != null)
            {
                body.Remove("alert_id"); // MR7: never written by the WPF — the alert routes own the link
                if (body.Count == 0) return;
            }
            if (body == null)
            {
                Reject(symbol, "change", null, e.Id, e.Epoch, ConfirmedOr(e.Id, e.Before), "The change has invalid points and was not saved.");
                return;
            }
            // Absorb into a waiting PATCH of the same drawing (later keys win). Its Before stays the state before the FIRST
            // absorbed step (= the last state the backend has) — a rejection restores exactly that (review M4).
            var waiting = _drawingOps.LastOrDefault(o => !o.InFlight && o.Kind == "patch" && o.Id == e.Id && o.Symbol == symbol);
            if (waiting != null && ReferenceEquals(waiting, _drawingOps[^1]))
            {
                foreach (var kv in body) waiting.Body![kv.Key] = kv.Value;
                waiting.Epoch = e.Epoch;
                waiting.DueUtc = DateTime.UtcNow + PatchDebounce;
                RenderObjects();
                return;
            }
            if (_drawingOps.Count >= MaxQueuedDrawingOps)
            {
                Reject(symbol, "change", null, e.Id, e.Epoch, ConfirmedOr(e.Id, e.Before), QueueFullMessage);
                return;
            }
            Enqueue(new DrawingOp { Kind = "patch", Symbol = symbol, Id = e.Id, Epoch = e.Epoch, Body = body, Before = e.Before, DueUtc = DateTime.UtcNow + PatchDebounce });
        }

        private void Chart_DrawingDeleted(object? sender, PageDrawingDeleted e)
        {
            var symbol = _chart.DrawingsSymbol;
            if (symbol == null) return;
            if (_selectedDrawingId == e.Id) _selectedDrawingId = null;
            // Waiting PATCHes of a deleted drawing are pointless (in-flight ones finish first, FIFO).
            _drawingOps.RemoveAll(o => !o.InFlight && o.Kind == "patch" && o.Id == e.Id && o.Symbol == symbol);
            if (_drawingOps.Count >= MaxQueuedDrawingOps)
            {
                Reject(symbol, "delete", null, e.Id, e.Epoch, ConfirmedOr(e.Id, e.Before), QueueFullMessage);
                return;
            }
            Enqueue(new DrawingOp { Kind = "delete", Symbol = symbol, Id = e.Id, Epoch = e.Epoch, Before = e.Before, DueUtc = DateTime.UtcNow });
        }

        private const string QueueFullMessage =
            "Too many unsaved drawing changes (1000) — the backend is not reachable. Wait until they are saved, then try again.";

        private void Reject(string symbol, string op, string? localId, string? id, int epoch, JsonObject? before, string message)
        {
            _chart.AnswerRejected(symbol, op, localId, id, epoch, before, message);
            _drawingMessage = message;
            RenderObjects();
            RenderDrawingStatus();
        }

        private void Enqueue(DrawingOp op)
        {
            _drawingOps.Add(op);
            if (_drawingTimer != null && !_drawingTimer.IsEnabled) _drawingTimer.Start();
            RenderObjects();
            RenderDrawingStatus();
            PumpDrawingOps();
        }

        private void DrawingTimer_Tick()
        {
            if (_drawingOps.Count == 0)
            {
                _drawingTimer?.Stop();
                return;
            }
            if (_drawingRetryAtUtc != null) RenderDrawingStatus(); // countdown
            PumpDrawingOps();
        }

        private async void PumpDrawingOps()
        {
            if (_drawingWorkerBusy || _shutDown) return;
            _drawingWorkerBusy = true;
            try
            {
                while (_drawingOps.Count > 0 && !_shutDown)
                {
                    var now = DateTime.UtcNow;
                    if (_drawingRetryAtUtc is DateTime retryAt && retryAt > now) return; // paused; the timer pumps again
                    var op = _drawingOps[0];
                    if (op.DueUtc > now) return;                                       // debounced PATCH; the timer pumps again
                    op.InFlight = true;
                    RenderDrawingStatus();
                    var outcome = await SendDrawingOpAsync(op);
                    op.InFlight = false;
                    if (_shutDown) return;
                    if (outcome.Transient)
                    {
                        _drawingRetryAttempt++;
                        var seconds = DrawingRetrySeconds[Math.Min(_drawingRetryAttempt - 1, DrawingRetrySeconds.Length - 1)];
                        _drawingRetryAtUtc = DateTime.UtcNow.AddSeconds(seconds);
                        _drawingPauseReason = outcome.Message;
                        if (_drawingTimer != null && !_drawingTimer.IsEnabled) _drawingTimer.Start();
                        RenderDrawingStatus();
                        return;
                    }
                    _drawingOps.Remove(op);
                    _drawingRetryAttempt = 0;
                    _drawingRetryAtUtc = null;
                    _drawingPauseReason = null;
                    if (outcome.Message != null) _drawingMessage = outcome.Message;
                    RenderObjects();
                }
                // Drained: symbols whose drawings were loaded while changes were waiting are loaded again.
                if (_drawingOps.Count == 0 && _reloadDrawingsAfterDrain.Count > 0)
                {
                    var tab = _active;
                    var reload = tab != null && _reloadDrawingsAfterDrain.Contains(tab.Symbol);
                    _reloadDrawingsAfterDrain.Clear();
                    if (reload && tab != null) _ = LoadDrawingsAsync(tab);
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: saving drawings failed unexpectedly.", ex);
                _drawingMessage = "Saving drawings failed unexpectedly — see the Backend Log.";
            }
            finally
            {
                _drawingWorkerBusy = false;
                RenderDrawingStatus();
            }
        }

        private readonly record struct OpOutcome(bool Transient, string? Message);

        private void RememberConfirmed()
        {
            _confirmedDrawings.Clear();
            foreach (var d in _chart.Drawings)
                if (Str(d["id"]) is { } id) _confirmedDrawings[id] = (JsonObject)d.DeepClone();
        }

        private void Confirm(DrawingDto saved)
        {
            if (saved.Id != null && ChartHost.DrawingFromApi(saved) is { } b) _confirmedDrawings[saved.Id] = b;
        }

        /// <summary>The backend's last confirmed state of <paramref name="id"/> (falls back to the host copy before the step).</summary>
        private JsonObject? ConfirmedOr(string? id, JsonObject? before)
            => id != null && _confirmedDrawings.TryGetValue(id, out var c) ? (JsonObject)c.DeepClone() : before;

        /// <summary>Offline / token / 503 = transient (queue pauses); everything else is final (success or refusal).</summary>
        private static bool IsTransient<T>(PlatformResult<T> r)
            => r.Status is PlatformCallStatus.Offline or PlatformCallStatus.Unauthorized or PlatformCallStatus.Unavailable;

        private async Task<OpOutcome> SendDrawingOpAsync(DrawingOp op)
        {
            switch (op.Kind)
            {
                case "create":
                {
                    var r = await ResearchClient.CreateDrawingAsync(op.Body!);
                    if (IsTransient(r)) return new OpOutcome(true, r.Message);
                    if (r.Ok && r.Value != null)
                    {
                        Confirm(r.Value);
                        _chart.AnswerSaved(op.Symbol, op.LocalId!, op.Epoch, r.Value);
                        if (r.Value.Tool == "alertLine" && r.Value.AlertId == null) OnAlertLineSaved(op.Symbol, r.Value); // MR7
                        return new OpOutcome(false, null);
                    }
                    var msg = r.Ok ? "Empty response from the backend — the drawing was not saved." : r.Message;
                    _chart.AnswerRejected(op.Symbol, "create", op.LocalId, null, op.Epoch, null, msg);
                    return new OpOutcome(false, msg);
                }
                case "patch":
                {
                    var r = await ResearchClient.PatchDrawingAsync(op.Id!, op.Body!);
                    if (IsTransient(r)) return new OpOutcome(true, r.Message);
                    if (r.Ok && r.Value != null)
                    {
                        Confirm(r.Value);
                        // Later queued changes of this drawing are not in the answer yet — no upsert then (it would undo them on screen).
                        var laterChanges = _drawingOps.Any(o => !ReferenceEquals(o, op) && o.Id == op.Id);
                        if (!laterChanges && _chart.DiffersFromHostCopy(r.Value)) _chart.UpsertDrawing(op.Symbol, r.Value); // e.g. rescaled meanwhile
                        if (r.Value.Tool == "alertLine" && op.Body!.ContainsKey("points")) OnAlertLineMoved(op.Symbol, r.Value); // MR7
                        return new OpOutcome(false, null);
                    }
                    var msg = r.Ok ? "Empty response from the backend — the change was not saved." : r.Message;
                    _chart.AnswerRejected(op.Symbol, "change", null, op.Id, op.Epoch, ConfirmedOr(op.Id, op.Before), msg);
                    return new OpOutcome(false, msg);
                }
                case "delete":
                {
                    var r = await ResearchClient.DeleteDrawingAsync(op.Id!);
                    if (IsTransient(r)) return new OpOutcome(true, r.Message);
                    if (r.Ok || r.HttpStatus == 404)
                    {
                        _confirmedDrawings.Remove(op.Id!); // 404 = already gone
                        if (Str(op.Before?["tool"]) == "alertLine") OnAlertLineDeleted(op.Symbol, op.Id!); // MR7: its alert is switched off
                        return new OpOutcome(false, null);
                    }
                    _chart.AnswerRejected(op.Symbol, "delete", null, op.Id, op.Epoch, ConfirmedOr(op.Id, op.Before), r.Message);
                    return new OpOutcome(false, r.Message);
                }
                default:
                    return new OpOutcome(false, null);
            }
        }

        // =====================================================================================
        // Status line (persistence state · refusals · split check · indicators)
        // =====================================================================================

        private void RenderDrawingStatus()
        {
            if (_confirmPanel == null) return;
            var lines = new List<string>();
            var brush = "SecondaryForegroundBrush";
            var dismissable = false;
            if (_confirmingDeleteAll && _active != null)
            {
                var n = _chart.Drawings.Count;
                lines.Add("Delete all " + n.ToString(CultureInfo.InvariantCulture) + " drawing(s) of " + _active.Symbol +
                          ", locked ones included? This cannot be undone.");
                brush = "CautionBrush";
            }
            else
            {
                if (_drawingRetryAtUtc is DateTime retryAt)
                {
                    var seconds = Math.Max(0, (int)Math.Ceiling((retryAt - DateTime.UtcNow).TotalSeconds));
                    lines.Add("Drawing changes not saved yet — " + (_drawingPauseReason ?? "the backend is not reachable.") +
                              " Retrying in " + seconds + " s (" + _drawingOps.Count + " waiting). They are lost if UFOS.ai is closed before they are saved.");
                    brush = "CautionBrush";
                }
                else if (_drawingOps.Count > 0)
                {
                    lines.Add("Saving drawings… (" + _drawingOps.Count + ")");
                }
                if (_drawingMessage != null) { lines.Add(_drawingMessage); dismissable = true; }
                if (_drawingsLoadError != null && _active != null) lines.Add(_drawingsLoadError);
                if (_splitCheckMessage != null && _active != null) lines.Add(_splitCheckMessage);
                if (_indicatorMessage != null && _active != null) { lines.Add(_indicatorMessage); dismissable = true; }
            }
            if (lines.Count == 0)
            {
                DrawingStatusRow.Visibility = Visibility.Collapsed;
                return;
            }
            var text = string.Join("\n", lines);
            DrawingStatusText.Text = text;
            DrawingStatusText.Foreground = Token(brush);
            AutomationProperties.SetName(DrawingStatusText, text);
            _confirmPanel.Visibility = _confirmingDeleteAll ? Visibility.Visible : Visibility.Collapsed;
            DrawingStatusDismiss.Visibility = !_confirmingDeleteAll && dismissable ? Visibility.Visible : Visibility.Collapsed;
            DrawingStatusRow.Visibility = Visibility.Visible;
        }

        private void DrawingStatusDismiss_Click(object sender, RoutedEventArgs e)
        {
            _drawingMessage = null;
            _indicatorMessage = null;
            RenderDrawingStatus();
        }

        private void BuildConfirmPanel()
        {
            var yes = MakeTextButton("Delete all", "Delete all drawings of this symbol", (_, _) => { _confirmingDeleteAll = false; _ = DeleteAllDrawingsAsync(); });
            var no = MakeTextButton("Cancel", "Keep the drawings", (_, _) => { _confirmingDeleteAll = false; RenderDrawingStatus(); _deleteAllButton?.Focus(); });
            no.Margin = new Thickness(6, 0, 0, 0);
            _confirmPanel = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(8, 0, 0, 0), Visibility = Visibility.Collapsed };
            _confirmPanel.Children.Add(yes);
            _confirmPanel.Children.Add(no);
            Grid.SetColumn(_confirmPanel, 1);
            DrawingStatusRow.Children.Add(_confirmPanel);
        }

        // =====================================================================================
        // Tool rail
        // =====================================================================================

        private void BuildToolRail()
        {
            ToolRailPanel.Children.Clear();
            foreach (var (tool, glyph, label, shortcut) in RailTools)
            {
                var t = tool;
                var tip = label + (shortcut != null ? "  (" + shortcut + ")" : "");
                var b = RailButton(glyph, (tool == null ? "Cursor" : label) + (shortcut != null ? ", " + shortcut : ""), tip, (_, _) => SelectTool(t));
                if (shortcut != null) AutomationProperties.SetAcceleratorKey(b, shortcut);
                _toolButtons[tool ?? ""] = b;
                ToolRailPanel.Children.Add(b);
            }
            ToolRailPanel.Children.Add(RailSeparator());
            _styleButton = RailButton("◐", "Style for the selected drawing or new drawings", "Style: colour, width, solid/dashed — for the selected drawing, or for new drawings when none is selected", (_, _) => OpenStylePopup());
            _magnetButton = RailButton("⊙", "Magnet", "Magnet", (_, _) => CycleMagnet());
            _saveMeasureButton = RailButton("✓", "Keep the selected measurement", "Keep the selected measurement as a drawing (Enter)", (_, _) => _chart.SendCommand("saveMeasure"));
            _undoButton = RailButton("↶", "Undo, Ctrl+Z", "Undo (Ctrl+Z, last 50 steps of this chart)", (_, _) => _chart.SendCommand("undo"));
            _redoButton = RailButton("↷", "Redo, Ctrl+Y", "Redo (Ctrl+Y)", (_, _) => _chart.SendCommand("redo"));
            _lockAllButton = RailButton("⊡", "Lock all drawings", "Lock all drawings", (_, _) => ToggleLockAll());
            _hideAllButton = RailButton("◉", "Hide all drawings", "Hide all drawings (view only — nothing is changed)", (_, _) => ToggleHideAll());
            _deleteAllButton = RailButton("⌫", "Delete all drawings", "Delete all drawings of this symbol (asks first)", (_, _) =>
            {
                if (_active == null) return;
                _confirmingDeleteAll = true;
                RenderDrawingStatus();
            });
            AutomationProperties.SetAcceleratorKey(_undoButton, "Ctrl+Z");
            AutomationProperties.SetAcceleratorKey(_redoButton, "Ctrl+Y");
            foreach (var b in new[] { _styleButton, _magnetButton, _saveMeasureButton, _undoButton, _redoButton, _lockAllButton, _hideAllButton, _deleteAllButton })
                ToolRailPanel.Children.Add(b!);
        }

        private Button RailButton(string glyph, string name, string tooltip, RoutedEventHandler click)
        {
            var b = new Button
            {
                Content = glyph,
                Width = 30,
                Height = 26,
                Margin = new Thickness(0, 0, 0, 2),
                FontSize = 13,
                ToolTip = tooltip,
                Style = ViewStyle("IconButtonStyle"),
            };
            AutomationProperties.SetName(b, name);
            b.Click += click;
            return b;
        }

        private static Border RailSeparator() => new()
        {
            Height = 1,
            Margin = new Thickness(3, 4, 3, 6),
            Background = new SolidColorBrush(Color.FromRgb(0x24, 0x24, 0x24)),
        };

        private Style ViewStyle(string key) => (Style)Resources[key];

        private Button MakeTextButton(string text, string automationName, RoutedEventHandler click)
        {
            var b = new Button { Content = text, Padding = new Thickness(8, 2, 8, 2), Style = ViewStyle("RoundedDarkButtonStyle") };
            AutomationProperties.SetName(b, automationName);
            b.Click += click;
            return b;
        }

        private void RenderRail()
        {
            var usable = DrawingToolsUsable;
            ToolRail.Visibility = _active != null && _chart.PageRevision >= 2 ? Visibility.Visible : Visibility.Collapsed;
            ToolRail.IsEnabled = usable;
            ToolRail.ToolTip = usable ? null
                : _drawingsLoading ? "Loading the drawings…"
                : _drawingsLoadError ?? "Drawing tools are available once the chart and its drawings are loaded.";
            foreach (var kv in _toolButtons)
            {
                var active = (kv.Key.Length == 0 ? null : kv.Key) == _currentTool;
                kv.Value.Background = active ? Tint("AccentBlueBrush", 0x33) : Brushes.Transparent;
                kv.Value.BorderBrush = active ? Token("FocusBrush") : Brushes.Transparent;
                var baseName = AutomationProperties.GetName(kv.Value).Replace(" (selected)", "");
                AutomationProperties.SetName(kv.Value, baseName + (active ? " (selected)" : ""));
            }
            if (_magnetButton != null)
            {
                var mode = _chart.Magnet;
                var word = mode == "weak" ? "weak" : mode == "strong" ? "strong" : "off";
                _magnetButton.Content = new StackPanel
                {
                    Children =
                    {
                        new TextBlock { Text = "⊙", FontSize = 12, HorizontalAlignment = HorizontalAlignment.Center },
                        new TextBlock { Text = word == "strong" ? "STR" : word.ToUpperInvariant(), FontFamily = new FontFamily("Consolas"), FontSize = 7.5, HorizontalAlignment = HorizontalAlignment.Center },
                    },
                };
                _magnetButton.Height = 30;
                _magnetButton.Foreground = mode == "off" ? Token("MutedForegroundBrush") : mode == "weak" ? Token("CautionBrush") : Token("AccentBlueBrush");
                _magnetButton.ToolTip = "Magnet: " + word + " — click for " + (mode == "off" ? "weak" : mode == "weak" ? "strong" : "off") + " (snaps to open/high/low/close)";
                AutomationProperties.SetName(_magnetButton, "Magnet " + word + ", click to change");
            }
            if (_undoButton != null) _undoButton.IsEnabled = _canUndo;
            if (_redoButton != null) _redoButton.IsEnabled = _canRedo;
            if (_saveMeasureButton != null) _saveMeasureButton.Visibility = _selectedTool == "measure" && _selectedIsUnsaved ? Visibility.Visible : Visibility.Collapsed;
            if (_hideAllButton != null)
            {
                var hidden = _chart.DrawingsHidden;
                _hideAllButton.Content = hidden ? "○" : "◉";
                _hideAllButton.ToolTip = hidden ? "Show all drawings again" : "Hide all drawings (view only — nothing is changed)";
                AutomationProperties.SetName(_hideAllButton, hidden ? "Show all drawings" : "Hide all drawings");
            }
            if (_lockAllButton != null)
            {
                var allLocked = _chart.Drawings.Count > 0 && _chart.Drawings.All(d => BoolOf(d["locked"]) == true);
                _lockAllButton.Content = allLocked ? "⊟" : "⊡";
                _lockAllButton.ToolTip = allLocked ? "Unlock all drawings" : "Lock all drawings";
                AutomationProperties.SetName(_lockAllButton, allLocked ? "Unlock all drawings" : "Lock all drawings");
                _lockAllButton.IsEnabled = _chart.Drawings.Count > 0;
            }
            if (_deleteAllButton != null) _deleteAllButton.IsEnabled = _chart.Drawings.Count > 0;
            ObjectsButton.Content = "OBJECTS · " + _chart.Drawings.Count.ToString(CultureInfo.InvariantCulture);
            ObjectsButton.IsEnabled = _active != null && _chart.PageRevision >= 2;
        }

        private void SelectTool(string? tool)
        {
            if (!DrawingToolsUsable) return;
            _currentTool = tool;
            _chart.SetTool(tool, tool == null ? null : RailStyleJson());
            if (tool == null) _chart.SendCommand("cancel");
            RenderRail();
        }

        private JsonObject RailStyleJson() => new() { ["color"] = _railColor, ["width"] = _railWidth, ["dash"] = _railDash };

        private void CycleMagnet()
        {
            var next = _chart.Magnet == "off" ? "weak" : _chart.Magnet == "weak" ? "strong" : "off";
            _chart.SetMagnet(next);
            RenderRail();
            ScheduleChartSettingsSave();
        }

        private void ToggleLockAll()
        {
            var allLocked = _chart.Drawings.Count > 0 && _chart.Drawings.All(d => BoolOf(d["locked"]) == true);
            _chart.SendCommand(allLocked ? "unlockAll" : "lockAll"); // undoable on the page, echoed as drawingChanged → PATCH
        }

        private void ToggleHideAll()
        {
            _chart.SendCommand(_chart.DrawingsHidden ? "showAll" : "hideAll");
            RenderRail();
        }

        private async Task DeleteAllDrawingsAsync()
        {
            var tab = _active;
            if (tab == null) return;
            var symbol = tab.Symbol;
            if (_drawingOps.Any(o => o.Symbol == symbol))
            {
                _drawingMessage = "Wait until the pending drawing changes are saved, then delete all.";
                RenderDrawingStatus();
                return;
            }
            var r = await ResearchClient.DeleteAllDrawingsAsync(symbol);
            if (_shutDown) return;
            if (r.Ok)
            {
                var n = r.Value.ValueKind == JsonValueKind.Object && r.Value.TryGetProperty("deleted", out var d) &&
                        d.ValueKind == JsonValueKind.Number && d.TryGetInt32(out var count) ? count : (int?)null;
                if (_active?.Symbol == symbol && _chart.DrawingsSymbol == symbol) { _chart.LoadDrawings(symbol, Array.Empty<DrawingDto>()); RememberConfirmed(); }
                _drawingMessage = "Deleted " + (n?.ToString(CultureInfo.InvariantCulture) ?? "all") + " drawing(s) of " + symbol + ".";
            }
            else _drawingMessage = "Drawings not deleted: " + r.Message;
            _selectedDrawingId = null;
            RenderRail();
            RenderObjects();
            RenderDrawingStatus();
            _deleteAllButton?.Focus();
        }

        // =====================================================================================
        // Selection + style popup
        // =====================================================================================

        private void Chart_DrawingSelectionChanged(object? sender, PageSelection e)
        {
            _selectedDrawingId = e.Id;
            _selectedTool = (e.Drawing?["tool"] as JsonValue)?.TryGetValue<string>(out var t) == true ? t : null;
            _selectedIsUnsaved = e.Id == null && e.Drawing != null;
            RenderRail();
            RenderObjects();
            if (_stylePopup?.IsOpen == true) RenderStylePopup();
        }

        private JsonObject? SelectedDrawing()
            => _selectedDrawingId == null ? null : _chart.Drawings.FirstOrDefault(d => (d["id"] as JsonValue)?.TryGetValue<string>(out var x) == true && x == _selectedDrawingId);

        private void OpenStylePopup()
        {
            if (_stylePopup == null)
            {
                _stylePopupBody = new StackPanel();
                _stylePopup = MakePopup(_stylePopupBody, "Drawing style");
            }
            _stylePopup.PlacementTarget = _styleButton;
            _stylePopup.Placement = PlacementMode.Right;
            RenderStylePopup();
            _stylePopup.IsOpen = true;
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => _stylePopupBody?.Children.OfType<WrapPanel>().FirstOrDefault()?.Children.OfType<Button>().FirstOrDefault()?.Focus()));
        }

        private Popup MakePopup(UIElement body, string name)
        {
            var popup = new Popup
            {
                StaysOpen = false,
                AllowsTransparency = true,
                Child = new Border
                {
                    Background = Token("ModulePanelBackgroundBrush"),
                    BorderBrush = Token("AccentBorderBrush"),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(8),
                    Padding = new Thickness(10),
                    Child = body,
                },
            };
            AutomationProperties.SetName(popup, name);
            popup.PreviewKeyDown += (_, e) =>
            {
                if (e.Key == Key.Escape) { popup.IsOpen = false; e.Handled = true; }
            };
            RootGrid.Children.Add(popup); // logical parent = the view (resources, focus scope)
            return popup;
        }

        private void RenderStylePopup()
        {
            if (_stylePopupBody == null) return;
            _stylePopupBody.Children.Clear();
            var selected = SelectedDrawing();
            var style = selected?["style"] as JsonObject;
            var color = (style?["color"] as JsonValue)?.TryGetValue<string>(out var c) == true ? c : _railColor;
            var width = (style?["width"] as JsonValue)?.TryGetValue<int>(out var w) == true ? w : _railWidth;
            var dash = (style?["dash"] as JsonValue)?.TryGetValue<string>(out var d) == true ? d : _railDash;
            var title = selected != null ? "STYLE · SELECTED DRAWING" : "STYLE · NEW DRAWINGS";
            _stylePopupBody.Children.Add(new TextBlock { Text = title, Style = ViewStyle("CaptionStyle"), Margin = new Thickness(0, 0, 0, 6) });
            if (BoolOf(selected?["locked"]) == true)
                _stylePopupBody.Children.Add(new TextBlock { Text = "Locked — unlock it in the objects panel to change its style.", FontSize = 10.5, Foreground = Token("CautionBrush"), TextWrapping = TextWrapping.Wrap, MaxWidth = 220, Margin = new Thickness(0, 0, 0, 6) });

            var swatches = new WrapPanel { MaxWidth = 220 };
            foreach (var (key, hex, name) in Swatches)
            {
                var k = key;
                var b = new Button
                {
                    Width = 22, Height = 22, Margin = new Thickness(0, 0, 4, 4),
                    Background = new SolidColorBrush((Color)ColorConverter.ConvertFromString(hex)),
                    BorderBrush = key == color ? Token("FocusBrush") : Brushes.Transparent,
                    BorderThickness = new Thickness(2),
                    Style = ViewStyle("IconButtonStyle"),
                    Content = key == color ? "✓" : "",
                    Foreground = Brushes.Black,
                    ToolTip = name,
                };
                AutomationProperties.SetName(b, "Colour " + name + (key == color ? " (selected)" : ""));
                b.Click += (_, _) => ApplyStyle("color", k);
                swatches.Children.Add(b);
            }
            _stylePopupBody.Children.Add(swatches);

            var widths = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 4, 0, 0) };
            widths.Children.Add(new TextBlock { Text = "WIDTH", Style = ViewStyle("CaptionStyle"), VerticalAlignment = VerticalAlignment.Center, Width = 48 });
            for (var i = 1; i <= 4; i++)
            {
                var value = i;
                var b = MakeSegmentButton(i.ToString(CultureInfo.InvariantCulture), "Line width " + i, i == width, () => ApplyStyle("width", value));
                widths.Children.Add(b);
            }
            _stylePopupBody.Children.Add(widths);

            var dashes = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 6, 0, 0) };
            dashes.Children.Add(new TextBlock { Text = "LINE", Style = ViewStyle("CaptionStyle"), VerticalAlignment = VerticalAlignment.Center, Width = 48 });
            dashes.Children.Add(MakeSegmentButton("Solid", "Solid line", dash == "solid", () => ApplyStyle("dash", "solid")));
            dashes.Children.Add(MakeSegmentButton("Dashed", "Dashed line", dash == "dashed", () => ApplyStyle("dash", "dashed")));
            _stylePopupBody.Children.Add(dashes);
        }

        private Button MakeSegmentButton(string text, string name, bool selected, Action click)
        {
            var b = new Button
            {
                Content = text,
                FontFamily = new FontFamily("Consolas"),
                FontSize = 10.5,
                Padding = new Thickness(7, 2, 7, 2),
                Margin = new Thickness(0, 0, 3, 0),
                Style = ViewStyle("IconButtonStyle"),
                Foreground = selected ? Token("AccentBlueBrush") : Token("SecondaryForegroundBrush"),
                BorderBrush = selected ? Tint("AccentBlueBrush", 0x99) : Brushes.Transparent,
                Background = selected ? Tint("AccentBlueBrush", 0x22) : Brushes.Transparent,
            };
            AutomationProperties.SetName(b, name + (selected ? " (selected)" : ""));
            b.Click += (_, _) => click();
            return b;
        }

        private const string LockedMessage = "The drawing is locked — unlock it first (□ in the objects panel).";

        private bool IsLocked(string id)
            => _chart.Drawings.FirstOrDefault(d => Str(d["id"]) == id) is { } d && BoolOf(d["locked"]) == true;

        private void ShowLockedMessage()
        {
            _drawingMessage = LockedMessage;
            RenderDrawingStatus();
        }

        private void ApplyStyle(string key, object value)
        {
            var selected = SelectedDrawing();
            if (selected != null && _selectedDrawingId != null)
            {
                if (BoolOf(selected["locked"]) == true) { ShowLockedMessage(); return; } // the backend would refuse it (409)
                var styleChange = new JsonObject();
                if (value is int i) styleChange[key] = i;
                else styleChange[key] = value.ToString();
                _chart.EditDrawing(_selectedDrawingId, new JsonObject { ["style"] = styleChange }); // undoable on the page, echoed → PATCH
            }
            else
            {
                if (key == "color" && value is string c) _railColor = c;
                if (key == "width" && value is int w) _railWidth = w;
                if (key == "dash" && value is string d) _railDash = d;
                if (_currentTool != null) _chart.SetTool(_currentTool, RailStyleJson());
            }
            // The page's echo updates the host copy; render again shortly after (next dispatcher round).
            Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(RenderStylePopup));
        }

        // =====================================================================================
        // Objects panel
        // =====================================================================================

        private void ObjectsButton_Click(object sender, RoutedEventArgs e)
        {
            _objectsPanelOpen = !_objectsPanelOpen;
            RenderObjects();
        }

        private void BuildObjectsPanel()
        {
            var header = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            _objectsHeader = new TextBlock { Text = "OBJECTS", Style = ViewStyle("CaptionStyle"), VerticalAlignment = VerticalAlignment.Center };
            var close = new Button { Content = "×", Style = ViewStyle("IconButtonStyle"), ToolTip = "Close the objects panel" };
            AutomationProperties.SetName(close, "Close the objects panel");
            close.Click += (_, _) => { _objectsPanelOpen = false; RenderObjects(); ObjectsButton.Focus(); };
            Grid.SetColumn(close, 1);
            header.Children.Add(_objectsHeader);
            header.Children.Add(close);
            DockPanel.SetDock(header, Dock.Top);
            ObjectsDock.Children.Add(header);

            var actions = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            var export = MakeTextButton("Export…", "Export the drawings of this symbol to a JSON file", (_, _) => _ = ExportDrawingsAsync());
            export.ToolTip = "Save all drawings of this symbol as <SYMBOL>-drawings.json";
            var import = MakeTextButton("Import…", "Import drawings from a JSON file", (_, _) => PickImportFile());
            import.ToolTip = "Load drawings from a file exported here (max 256 KiB) — append or replace";
            import.Margin = new Thickness(6, 0, 0, 0);
            actions.Children.Add(export);
            actions.Children.Add(import);
            DockPanel.SetDock(actions, Dock.Top);
            ObjectsDock.Children.Add(actions);

            _importText = new TextBlock { FontSize = 10.5, Foreground = Token("SecondaryForegroundBrush"), TextWrapping = TextWrapping.Wrap };
            var importButtons = new WrapPanel { Margin = new Thickness(0, 6, 0, 0) };
            var append = MakeTextButton("Append", "Import and keep the existing drawings", (_, _) => _ = RunImportAsync("append"));
            var replace = MakeTextButton("Replace all", "Import and delete the existing drawings first", (_, _) => _ = RunImportAsync("replace"));
            replace.Margin = new Thickness(6, 0, 0, 0);
            var cancel = MakeTextButton("Cancel", "Cancel the import", (_, _) => CancelImport());
            cancel.Margin = new Thickness(6, 0, 0, 0);
            importButtons.Children.Add(append);
            importButtons.Children.Add(replace);
            importButtons.Children.Add(cancel);
            var importStack = new StackPanel();
            importStack.Children.Add(_importText);
            importStack.Children.Add(importButtons);
            _importPanel = new Border
            {
                Child = importStack,
                Padding = new Thickness(6),
                Margin = new Thickness(0, 0, 0, 6),
                CornerRadius = new CornerRadius(6),
                BorderThickness = new Thickness(1),
                BorderBrush = Tint("CautionBrush", 0x66),
                Background = Tint("CautionBrush", 0x14),
                Visibility = Visibility.Collapsed,
            };
            DockPanel.SetDock(_importPanel, Dock.Top);
            ObjectsDock.Children.Add(_importPanel);

            var statusStack = new StackPanel();
            _objectsStatus = new TextBlock { FontSize = 10.5, Foreground = Token("SecondaryForegroundBrush"), TextWrapping = TextWrapping.Wrap, Visibility = Visibility.Collapsed, Margin = new Thickness(0, 0, 0, 6) };
            AutomationProperties.SetLiveSetting(_objectsStatus, AutomationLiveSetting.Polite);
            _objectsRetryButton = MakeTextButton("Retry", "Load the drawings again", (_, _) => { if (_active != null) _ = LoadDrawingsAsync(_active); });
            _objectsRetryButton.HorizontalAlignment = HorizontalAlignment.Left;
            _objectsRetryButton.Visibility = Visibility.Collapsed;
            _objectsRetryButton.Margin = new Thickness(0, 0, 0, 6);
            statusStack.Children.Add(_objectsStatus);
            statusStack.Children.Add(_objectsRetryButton);
            DockPanel.SetDock(statusStack, Dock.Top);
            ObjectsDock.Children.Add(statusStack);

            _objectsList = new StackPanel();
            AutomationProperties.SetName(_objectsList, "Drawings of this symbol");
            ObjectsDock.Children.Add(new ScrollViewer
            {
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                Content = _objectsList,
                Focusable = false,
            });
        }

        private static string ToolLabel(string? tool)
        {
            foreach (var r in RailTools) if (r.Tool == tool && tool != null) return r.Label.Split(" (")[0];
            return tool ?? "Drawing";
        }

        private static string? Str(JsonNode? node) => (node as JsonValue)?.TryGetValue<string>(out var s) == true ? s : null;

        private static bool? BoolOf(JsonNode? node) => (node as JsonValue)?.TryGetValue<bool>(out var b) == true ? b : null;

        private void RenderObjects()
        {
            if (_objectsList == null || _objectsHeader == null || _objectsStatus == null || _objectsRetryButton == null) return;
            ObjectsPanel.Visibility = _objectsPanelOpen && _active != null && _chart.PageRevision >= 2 ? Visibility.Visible : Visibility.Collapsed;
            RenderRail();
            if (ObjectsPanel.Visibility != Visibility.Visible) return;
            var drawings = _chart.Drawings;
            _objectsHeader.Text = "OBJECTS · " + (_active?.Symbol ?? "") + " · " + drawings.Count + "/" + ChartHost.MaxDrawings;
            string? status = null;
            if (_drawingsLoading) status = "Loading the drawings…";
            else if (_drawingsLoadError != null) status = _drawingsLoadError;
            else if (_drawingOps.Count > 0) status = _drawingRetryAtUtc != null ? "Not saved yet — see the message above the chart." : "Saving… (" + _drawingOps.Count + ")";
            else if (drawings.Count == 0) status = "No drawings yet. Pick a tool on the left of the chart.";
            _objectsStatus.Text = status ?? "";
            _objectsStatus.Visibility = status == null ? Visibility.Collapsed : Visibility.Visible;
            _objectsRetryButton.Visibility = _drawingsLoadError != null && !_drawingsLoading ? Visibility.Visible : Visibility.Collapsed;

            _objectsList.Children.Clear();
            var tf = _active?.Tf;
            foreach (var d in drawings)
            {
                var id = Str(d["id"]);
                if (id == null) continue;
                _objectsList.Children.Add(BuildObjectRow(d, id, tf));
            }
        }

        private UIElement BuildObjectRow(JsonObject d, string id, string? currentTf)
        {
            var tool = Str(d["tool"]);
            var name = Str(d["name"]);
            var text = Str(d["text"]);
            var locked = BoolOf(d["locked"]) == true;
            var visible = BoolOf(d["visible"]) != false;
            var tfs = d["timeframes"] is JsonArray arr ? arr.Select(Str).Where(x => x != null).Cast<string>().ToList() : null;
            var shownHere = tfs == null || (currentTf != null && tfs.Contains(currentTf));
            var title = name ?? (tool == "simpleAnnotation" && text != null ? "Note: " + (text.Length > 24 ? text.Substring(0, 24) + "…" : text) : ToolLabel(tool));
            var sub = ToolLabel(tool) + " · TF " + (tfs == null ? "all" : string.Join(",", tfs)) + (shownHere ? "" : " · not on " + currentTf) +
                      (locked ? " · locked" : "") + (visible ? "" : " · hidden");
            var selected = id == _selectedDrawingId;

            var row = new Border
            {
                Padding = new Thickness(4, 3, 2, 3),
                Margin = new Thickness(0, 0, 0, 2),
                CornerRadius = new CornerRadius(5),
                BorderThickness = new Thickness(1),
                Background = selected ? Tint("AccentBlueBrush", 0x1F) : Brushes.Transparent,
                BorderBrush = selected ? Tint("AccentBlueBrush", 0x66) : Brushes.Transparent,
            };
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            UIElement label;
            if (_renamingId == id)
            {
                var box = new TextBox { Text = name ?? "", MaxLength = 40, Style = ViewStyle("InputBoxStyle"), FontSize = 11.5 };
                AutomationProperties.SetName(box, "New name for the drawing: Enter saves, Escape cancels, empty removes the name");
                box.KeyDown += (_, e) =>
                {
                    if (e.Key == Key.Enter) { e.Handled = true; CommitRename(id, box.Text); }
                    else if (e.Key == Key.Escape) { e.Handled = true; _renamingId = null; RenderObjects(); }
                };
                box.Loaded += (_, _) => { box.Focus(); box.SelectAll(); };
                label = new Border { Style = ViewStyle("InputBorderStyle"), Child = box };
            }
            else
            {
                // Name / note text are user text: plain TextBlock only.
                var open = new Button
                {
                    Content = new TextBlock { Text = title, TextTrimming = TextTrimming.CharacterEllipsis, FontSize = 11.5, Foreground = Token(shownHere && visible ? "PrimaryForegroundBrush" : "MutedForegroundBrush") },
                    Style = ViewStyle("ListRowButtonStyle"),
                    HorizontalContentAlignment = HorizontalAlignment.Left,
                    ToolTip = title + "\n" + sub,
                };
                AutomationProperties.SetName(open, title + ", " + sub + (selected ? ", selected" : "") + ". Enter selects it on the chart.");
                open.Click += (_, _) => _chart.SelectDrawing(id);
                label = open;
            }
            Grid.SetColumn(label, 0);
            grid.Children.Add(label);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Top };
            buttons.Children.Add(RowButton("⌖", "Jump to " + title, "Scroll the chart to this drawing", () => _chart.JumpToDrawing(id)));
            buttons.Children.Add(RowButton(visible ? "●" : "○", (visible ? "Hide " : "Show ") + title, visible ? "Hide this drawing" : "Show this drawing",
                () => _chart.EditDrawing(id, new JsonObject { ["visible"] = !visible })));
            buttons.Children.Add(RowButton(locked ? "▣" : "□", (locked ? "Unlock " : "Lock ") + title, locked ? "Unlock (allows moving and editing)" : "Lock (no moving or editing)",
                () => _chart.EditDrawing(id, new JsonObject { ["locked"] = !locked })));
            var tfButton = RowButton("TF", "Timeframes for " + title, "Show this drawing on all timeframes or only on some", () => { });
            tfButton.Click += (_, _) => OpenTimeframesPopup(id, tfs, tfButton);
            buttons.Children.Add(tfButton);
            buttons.Children.Add(RowButton("✎", "Rename " + title, locked ? "Locked — unlock it first" : "Rename (max 40 characters)", () =>
            {
                if (locked) { ShowLockedMessage(); return; }
                _renamingId = id;
                RenderObjects();
            }));
            buttons.Children.Add(RowButton("×", "Delete " + title, locked ? "Locked — unlock it first" : "Delete (Ctrl+Z on the chart undoes it)", () =>
            {
                if (locked) { ShowLockedMessage(); return; }
                _chart.RemoveDrawing(id);
            }));
            Grid.SetColumn(buttons, 1);
            grid.Children.Add(buttons);

            var subText = new TextBlock { Text = sub, FontSize = 9.5, Foreground = Token("QuietForegroundBrush"), TextTrimming = TextTrimming.CharacterEllipsis, Margin = new Thickness(4, 1, 0, 0) };
            Grid.SetRow(subText, 1);
            Grid.SetColumnSpan(subText, 2);
            grid.Children.Add(subText);
            row.Child = grid;
            return row;
        }

        private Button RowButton(string glyph, string name, string tooltip, Action click)
        {
            var b = new Button { Content = glyph, Style = ViewStyle("IconButtonStyle"), ToolTip = tooltip, FontSize = 10.5, Padding = new Thickness(3, 0, 3, 0), MinWidth = 18 };
            AutomationProperties.SetName(b, name);
            b.Click += (_, _) => click();
            return b;
        }

        private void CommitRename(string id, string text)
        {
            _renamingId = null;
            if (IsLocked(id)) { ShowLockedMessage(); RenderObjects(); return; }
            var name = text.Trim();
            if (name.Length > 40 || name.Any(char.IsControl))
            {
                _drawingMessage = "A name has at most 40 characters and no control characters.";
                RenderDrawingStatus();
                RenderObjects();
                return;
            }
            _chart.EditDrawing(id, new JsonObject { ["name"] = name.Length == 0 ? null : name });
            RenderObjects();
        }

        private void OpenTimeframesPopup(string id, List<string>? current, UIElement target)
        {
            if (_tfPopup == null)
            {
                _tfPopupBody = new StackPanel();
                _tfPopup = MakePopup(_tfPopupBody, "Timeframes of the drawing");
            }
            _tfPopupBody!.Children.Clear();
            _tfPopupBody.Children.Add(new TextBlock { Text = "SHOW ON", Style = ViewStyle("CaptionStyle"), Margin = new Thickness(0, 0, 0, 6) });
            var all = new CheckBox { Content = "All timeframes", IsChecked = current == null, Foreground = Token("PrimaryForegroundBrush"), Margin = new Thickness(0, 0, 0, 4) };
            _tfPopupBody.Children.Add(all);
            var boxes = new List<CheckBox>();
            foreach (var tf in ChartHost.Timeframes)
            {
                var cb = new CheckBox { Content = tf, Tag = tf, IsChecked = current == null || current.Contains(tf), IsEnabled = current != null, Foreground = Token("PrimaryForegroundBrush"), FontFamily = new FontFamily("Consolas"), Margin = new Thickness(12, 0, 0, 2) };
                AutomationProperties.SetName(cb, "Show on " + tf);
                boxes.Add(cb);
                _tfPopupBody.Children.Add(cb);
            }
            all.Checked += (_, _) => { foreach (var cb in boxes) { cb.IsChecked = true; cb.IsEnabled = false; } };
            all.Unchecked += (_, _) => { foreach (var cb in boxes) cb.IsEnabled = true; };
            var message = new TextBlock { FontSize = 10.5, Foreground = Token("CautionBrush"), Visibility = Visibility.Collapsed, TextWrapping = TextWrapping.Wrap, MaxWidth = 180 };
            var apply = MakeTextButton("Apply", "Apply the timeframes", (_, _) =>
            {
                JsonNode? value;
                if (all.IsChecked == true) value = "all";
                else
                {
                    var picked = boxes.Where(b => b.IsChecked == true).Select(b => (string)b.Tag).ToList();
                    if (picked.Count == 0) { message.Text = "Pick at least one timeframe (or All)."; message.Visibility = Visibility.Visible; return; }
                    var arr = new JsonArray();
                    foreach (var p in picked) arr.Add(p);
                    value = arr;
                }
                _tfPopup!.IsOpen = false;
                _chart.EditDrawing(id, new JsonObject { ["timeframes"] = value });
            });
            apply.Margin = new Thickness(0, 6, 0, 0);
            apply.HorizontalAlignment = HorizontalAlignment.Left;
            _tfPopupBody.Children.Add(apply);
            _tfPopupBody.Children.Add(message);
            _tfPopup!.PlacementTarget = target;
            _tfPopup.Placement = PlacementMode.Bottom;
            _tfPopup.IsOpen = true;
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => all.Focus()));
        }

        // =====================================================================================
        // Export / import (per symbol, <SYMBOL>-drawings.json)
        // =====================================================================================

        private async Task ExportDrawingsAsync()
        {
            var tab = _active;
            if (tab == null) return;
            var symbol = tab.Symbol;
            var dialog = new Microsoft.Win32.SaveFileDialog
            {
                FileName = symbol.Replace('/', '-') + "-drawings.json",
                Filter = "Drawings (*.json)|*.json",
                DefaultExt = ".json",
                AddExtension = true,
                OverwritePrompt = true,
                Title = "Export the drawings of " + symbol,
            };
            var owner = Window.GetWindow(this);
            if ((owner != null ? dialog.ShowDialog(owner) : dialog.ShowDialog()) != true) return;
            var path = dialog.FileName;
            var r = await ResearchClient.ExportDrawingsAsync(symbol);
            if (_shutDown) return;
            if (!r.Ok || r.Value.ValueKind != JsonValueKind.Object)
            {
                _drawingMessage = "Drawings not exported: " + (r.Ok ? "empty response from the backend." : r.Message);
                RenderDrawingStatus();
                return;
            }
            try
            {
                var json = JsonSerializer.Serialize(r.Value, new JsonSerializerOptions { WriteIndented = true });
                await File.WriteAllTextAsync(path, json, new UTF8Encoding(false));
                var count = r.Value.TryGetProperty("drawings", out var arr) && arr.ValueKind == JsonValueKind.Array ? arr.GetArrayLength() : 0;
                _drawingMessage = "Exported " + count + " drawing(s) of " + symbol + " to " + Path.GetFileName(path) + ".";
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is NotSupportedException || ex is System.Security.SecurityException)
            {
                _drawingMessage = "Drawings not exported: the file could not be written (" + ex.GetType().Name + ": " + ex.Message + ").";
            }
            RenderDrawingStatus();
        }

        private void PickImportFile()
        {
            var tab = _active;
            if (tab == null) return;
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Filter = "Drawings (*.json)|*.json|All files (*.*)|*.*",
                CheckFileExists = true,
                Multiselect = false,
                Title = "Import drawings for " + tab.Symbol,
            };
            var owner = Window.GetWindow(this);
            if ((owner != null ? dialog.ShowDialog(owner) : dialog.ShowDialog()) != true) return;
            var path = dialog.FileName;
            try
            {
                var info = new FileInfo(path);
                if (info.Length > MaxImportFileBytes)
                {
                    ShowImportProblem("The file is larger than 1 MiB — not a drawings export (the import limit is 256 KiB of compact JSON).");
                    return;
                }
                var text = File.ReadAllText(path, Encoding.UTF8);
                using var doc = JsonDocument.Parse(text, new JsonDocumentOptions { MaxDepth = 64 });
                if (doc.RootElement.ValueKind != JsonValueKind.Object)
                {
                    ShowImportProblem("The file is not a drawings export (a JSON object is expected).");
                    return;
                }
                var element = doc.RootElement.Clone();
                // The backend's rule: raw UTF-8 bytes of the compact body (non-ASCII NOT \u-escaped — review L8).
                var compactJson = ResearchClient.CompactJson(element);
                var compact = Encoding.UTF8.GetByteCount(compactJson);
                if (compact > MaxImportBytes)
                {
                    ShowImportProblem("The file holds " + (compact / 1024) + " KiB of drawings; the import limit is 256 KiB.");
                    return;
                }
                var fileSymbol = element.TryGetProperty("symbol", out var s) && s.ValueKind == JsonValueKind.String ? s.GetString() : null;
                var count = element.TryGetProperty("drawings", out var arr) && arr.ValueKind == JsonValueKind.Array ? arr.GetArrayLength() : (int?)null;
                _pendingImport = compactJson;
                _pendingImportSymbol = tab.Symbol;
                // File content is untrusted text: shown in a TextBlock only; the backend checks everything again.
                _importText!.Text = "Import " + (count?.ToString(CultureInfo.InvariantCulture) ?? "the") + " drawing(s) from " + Path.GetFileName(path) +
                                    (fileSymbol != null && fileSymbol != tab.Symbol ? " (the file names " + (fileSymbol.Length > 32 ? fileSymbol.Substring(0, 32) : fileSymbol) + ")" : "") +
                                    " into " + tab.Symbol + "?";
                _importPanel!.Visibility = Visibility.Visible;
                AutomationProperties.SetName(_importPanel, _importText.Text);
            }
            catch (JsonException)
            {
                ShowImportProblem("The file is not valid JSON.");
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is NotSupportedException || ex is System.Security.SecurityException)
            {
                ShowImportProblem("The file could not be read (" + ex.GetType().Name + ": " + ex.Message + ").");
            }
        }

        private void ShowImportProblem(string message)
        {
            CancelImport();
            _drawingMessage = "Not imported: " + message;
            RenderDrawingStatus();
        }

        private void CancelImport()
        {
            _pendingImport = null;
            _pendingImportSymbol = null;
            if (_importPanel != null) _importPanel.Visibility = Visibility.Collapsed;
        }

        private async Task RunImportAsync(string mode)
        {
            var file = _pendingImport;
            var symbol = _pendingImportSymbol;
            CancelImport();
            if (file == null || symbol == null) return;
            if (_drawingOps.Any(o => o.Symbol == symbol))
            {
                _drawingMessage = "Wait until the pending drawing changes are saved, then import.";
                RenderDrawingStatus();
                return;
            }
            var r = await ResearchClient.ImportDrawingsAsync(symbol, mode, file);
            if (_shutDown) return;
            if (r.Ok && r.Value != null)
            {
                _drawingMessage = "Imported " + (r.Value.Imported ?? 0) + " drawing(s)" +
                                  (mode == "replace" ? " (replaced " + (r.Value.Replaced ?? 0) + ")" : "") + " into " + symbol +
                                  ". A split check runs now for the imported prices.";
                var tab = _active;
                if (tab != null && tab.Symbol == symbol) _ = LoadDrawingsAsync(tab); // the backend state (all drawings, new ids)
            }
            else _drawingMessage = r.Ok ? "Not imported: empty response from the backend." : r.Message;
            RenderDrawingStatus();
        }

        // =====================================================================================
        // Keyboard (WPF side) + shortcuts forwarded by the page
        // =====================================================================================

        /// <summary>
        /// Alt+T/H/F/R and Esc choose drawing tools while the focus is in the view (the page handles them itself while
        /// it has the focus); Ctrl+T new tab (palette), Ctrl+W close tab, Ctrl+Tab / Ctrl+Shift+Tab next/previous tab.
        /// </summary>
        private bool HandleChartKeys(KeyEventArgs e)
        {
            if (PaletteOverlay.Visibility == Visibility.Visible) return false;
            if (Keyboard.FocusedElement is TextBox) return false;
            var key = e.Key == Key.System ? e.SystemKey : e.Key;
            var mods = Keyboard.Modifiers;
            if (mods == ModifierKeys.Control && key == Key.T) { e.Handled = true; HandleShortcut("ctrl+t"); return true; }
            if (mods == ModifierKeys.Control && key == Key.W) { e.Handled = true; HandleShortcut("ctrl+w"); return true; }
            if (mods == ModifierKeys.Control && key == Key.Tab) { e.Handled = true; HandleShortcut("ctrl+tab"); return true; }
            if (mods == (ModifierKeys.Control | ModifierKeys.Shift) && key == Key.Tab) { e.Handled = true; HandleShortcut("ctrl+shift+tab"); return true; }
            if (!DrawingToolsUsable) return false;
            if (mods == ModifierKeys.Alt)
            {
                string? tool = key switch { Key.T => "segment", Key.H => "horizontalStraightLine", Key.F => "fibonacciLine", Key.R => "rect", _ => null };
                if (tool != null) { e.Handled = true; SelectTool(tool); return true; }
            }
            if (mods == ModifierKeys.None && key == Key.Escape && (_currentTool != null || _confirmingDeleteAll))
            {
                e.Handled = true;
                if (_confirmingDeleteAll) { _confirmingDeleteAll = false; RenderDrawingStatus(); }
                else SelectTool(null);
                return true;
            }
            return false;
        }

        private void HandleShortcut(string key)
        {
            if (_shutDown) return;
            switch (key)
            {
                case "ctrl+k":
                case "ctrl+t":
                    OpenPalette(PaletteMode.Open, null);
                    break;
                case "ctrl+w":
                    if (_active != null) CloseTab(_active);
                    break;
                case "ctrl+tab":
                case "ctrl+shift+tab":
                {
                    if (_active == null || _tabs.Count < 2) break;
                    var i = _tabs.IndexOf(_active);
                    var next = key == "ctrl+tab" ? (i + 1) % _tabs.Count : (i - 1 + _tabs.Count) % _tabs.Count;
                    Activate(_tabs[next]);
                    break;
                }
            }
        }
    }
}

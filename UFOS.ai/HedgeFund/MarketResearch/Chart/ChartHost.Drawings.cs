using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>Page → host <c>drawingCreated</c> (SPEC "MR5 bridge contract (revision 2)").</summary>
    internal sealed class PageDrawingCreated : EventArgs
    {
        public PageDrawingCreated(string localId, string? clientId, JsonObject drawing, int epoch) { LocalId = localId; ClientId = clientId; Drawing = drawing; Epoch = epoch; }
        public string LocalId { get; }
        /// <summary>Revision 3: the page's UUID v4 for this create — sent as <c>client_id</c> so a retried POST never stores a duplicate.</summary>
        public string? ClientId { get; }
        /// <summary>Bridge drawing (ms <c>t</c>), without <c>id</c>.</summary>
        public JsonObject Drawing { get; }
        /// <summary>The page instance the localId belongs to (<see cref="ChartHost.PageEpoch"/> at that moment).</summary>
        public int Epoch { get; }
    }

    /// <summary>Page → host <c>drawingChanged</c> (final only in MR5).</summary>
    internal sealed class PageDrawingChanged : EventArgs
    {
        public PageDrawingChanged(string id, JsonObject changes, JsonObject? before, int epoch) { Id = id; Changes = changes; Before = before; Epoch = epoch; }
        public string Id { get; }
        /// <summary>Only the changed keys (bridge form; <c>points</c> complete when present).</summary>
        public JsonObject Changes { get; }
        /// <summary>The host's copy BEFORE the change (null if the host did not know the id) — to revert on a rejection.</summary>
        public JsonObject? Before { get; }
        public int Epoch { get; }
    }

    /// <summary>Page → host <c>drawingDeleted</c>.</summary>
    internal sealed class PageDrawingDeleted : EventArgs
    {
        public PageDrawingDeleted(string id, JsonObject? before, int epoch) { Id = id; Before = before; Epoch = epoch; }
        public string Id { get; }
        public JsonObject? Before { get; }
        public int Epoch { get; }
    }

    /// <summary>Page → host <c>selectionChanged</c>.</summary>
    internal sealed class PageSelection : EventArgs
    {
        public PageSelection(string? id, string? localId, JsonObject? drawing) { Id = id; LocalId = localId; Drawing = drawing; }
        public string? Id { get; }
        public string? LocalId { get; }
        public JsonObject? Drawing { get; }
    }

    /// <summary>
    /// MR5 part of <see cref="ChartHost"/>: drawings, tools, magnet, commands and indicator series over the bridge
    /// (SPEC "MR5 bridge contract (revision 2: drawings + indicators)"). The host keeps a copy of the page state —
    /// saved drawings (server ids), tool, style, magnet, hide-all and the indicator set — so a re-created page
    /// (pop-out / Reload) gets everything again after its <c>ready</c>. Persistence (POST/PATCH/DELETE) is NOT done
    /// here: the view subscribes to the page events and answers with <see cref="AnswerSaved"/> /
    /// <see cref="AnswerRejected"/>. Point <c>t</c>: ISO in the API, epoch ms on the bridge — converted only here
    /// (<see cref="DrawingFromApi"/>, <see cref="PointsToApi"/>), SPEC §0 Time.
    /// </summary>
    internal sealed partial class ChartHost
    {
        public const int MaxDrawings = 500;       // MAX_DRAWINGS_PER_SYMBOL
        public const int MaxIndicatorSpecs = 8;   // MAX_INDICATORS
        public const int MaxSubPanes = 3;         // MAX_SUB_PANES
        private const int DrawingChunkBytes = 3 * 1024 * 1024; // loadDrawings chunks (append) stay below the 4 MiB cap

        private static readonly Regex LocalIdPattern = new("^c-[0-9]{1,9}$", RegexOptions.CultureInvariant);
        private static readonly Regex ServerIdPattern = new("^[0-9a-f]{32}$", RegexOptions.CultureInvariant);
        private static readonly Regex ClientIdPattern = new("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", RegexOptions.CultureInvariant);

        /// <summary>Tools and their point counts (min, max) — "MR5 backend notes".</summary>
        public static readonly IReadOnlyDictionary<string, (int Min, int Max)> ToolPoints = new Dictionary<string, (int, int)>(StringComparer.Ordinal)
        {
            ["segment"] = (2, 2), ["rayLine"] = (2, 2), ["straightLine"] = (2, 2),
            ["horizontalStraightLine"] = (1, 1), ["horizontalRayLine"] = (1, 1), ["verticalStraightLine"] = (1, 1),
            ["priceLine"] = (1, 1), ["parallelStraightLine"] = (3, 3), ["priceChannelLine"] = (3, 3),
            ["fibonacciLine"] = (2, 2), ["rect"] = (2, 2), ["measure"] = (2, 2), ["simpleAnnotation"] = (1, 1),
            ["simpleTag"] = (1, 1), ["arrowUp"] = (1, 1), ["arrowDown"] = (1, 1), ["brush"] = (2, 500), ["alertLine"] = (1, 1),
        };

        public static readonly IReadOnlyList<string> PaletteKeys = new[] { "accent", "up", "down", "caution", "text", "muted", "blue", "violet" };
        public static readonly IReadOnlyList<string> Timeframes = new[] { "1m", "5m", "15m", "1H", "4H", "1D", "1W" };
        private static readonly HashSet<string> ChangeKeys = new(StringComparer.Ordinal)
            { "points", "style", "name", "text", "locked", "visible", "timeframes", "alert_id" };
        private static readonly HashSet<string> Commands = new(StringComparer.Ordinal)
            { "undo", "redo", "lock", "lockAll", "unlockAll", "hideAll", "showAll", "deleteSelected", "resetView", "cancel", "saveMeasure" };

        // ---------- page state copy ----------
        private string? _drawingsSymbol;
        private readonly List<JsonObject> _drawings = new(); // saved drawings (server ids), bridge form
        private int _pageEpoch;
        private string? _tool;
        private JsonObject? _toolStyle;
        private string _magnet = "off";
        private bool _drawingsHidden;
        private JsonArray? _indicatorDefs;                    // setIndicators.indicators (null = none sent)
        private readonly List<long> _indicatorT = new();
        private readonly Dictionary<string, List<double?>> _indicatorSeries = new(StringComparer.Ordinal);

        /// <summary>Bridge revision of the loaded page (2 = drawings + indicators; 0/1 = MR3 page without them).</summary>
        public int PageRevision { get; private set; }
        /// <summary>Changes whenever the page's drawing ids could be unknown to it (new page, replacing loadDrawings).</summary>
        public int PageEpoch => _pageEpoch;
        public bool PageIsReady => _pageReady;
        public string? DrawingsSymbol => _drawingsSymbol;
        public IReadOnlyList<JsonObject> Drawings => _drawings;
        public bool DrawingsHidden => _drawingsHidden;
        public string Magnet => _magnet;
        public string? Tool => _tool;

        public event EventHandler<PageDrawingCreated>? DrawingCreated;
        public event EventHandler<PageDrawingChanged>? DrawingChanged;
        public event EventHandler<PageDrawingDeleted>? DrawingDeleted;
        public event EventHandler<PageSelection>? DrawingSelectionChanged;
        /// <summary>(canUndo, canRedo).</summary>
        public event EventHandler<(bool CanUndo, bool CanRedo)>? UndoStateChanged;
        /// <summary>The page switched the tool (finished drawing, Esc, Alt+T/H/F/R); null = cursor.</summary>
        public event EventHandler<string?>? PageToolChanged;
        /// <summary>A WPF shortcut pressed while the chart had the focus: ctrl+k · ctrl+t · ctrl+w · ctrl+tab · ctrl+shift+tab.</summary>
        public event EventHandler<string>? ShortcutPressed;
        /// <summary>The page became ready (after creation or Reload) — revision in <see cref="PageRevision"/>.</summary>
        public event EventHandler? PageReady;
        /// <summary>Page error <c>drawing_invalid</c> (a finished drawing was discarded) — its message, for the drawing status line.</summary>
        public event EventHandler<string>? DrawingNotice;

        // =====================================================================================
        // Host → page API (view)
        // =====================================================================================

        /// <summary>Replaces all drawings of <paramref name="symbol"/> (clears the page's undo history).</summary>
        /// <returns>The number of drawings dropped because they failed the bridge checks (logged).</returns>
        public int LoadDrawings(string symbol, IReadOnlyList<DrawingDto>? drawings)
        {
            _drawingsSymbol = symbol;
            _drawings.Clear();
            var dropped = 0;
            if (drawings != null)
                foreach (var d in drawings)
                {
                    var b = DrawingFromApi(d);
                    if (b == null) { dropped++; continue; }
                    if (_drawings.Count < MaxDrawings) _drawings.Add(b);
                }
            if (dropped > 0) AppLog.Warn(LogModule, "Market Research chart: " + dropped + " stored drawing(s) failed the bridge checks and are not shown.");
            _pageEpoch++;
            SendDrawings();
            return dropped;
        }

        /// <summary>Forgets the drawings (another symbol is about to load).</summary>
        public void ClearDrawings()
        {
            _drawingsSymbol = null;
            _drawings.Clear();
            _pageEpoch++;
            if (PageRevision >= 2) Post(new { type = "loadDrawings", v = 1, drawings = Array.Empty<JsonObject>() });
        }

        /// <summary>POST answer for a page create: <c>drawingSaved</c> when the page still knows the localId, else <c>upsertDrawing</c>.</summary>
        public void AnswerSaved(string symbol, string localId, int epoch, DrawingDto saved)
        {
            var b = DrawingFromApi(saved);
            if (b == null) { LogOnce("savedbad", "Market Research chart: a saved drawing failed the bridge checks."); return; }
            if (symbol != _drawingsSymbol) return; // another symbol is shown now; it is in the backend and appears when the symbol opens
            Replace(b);
            if (epoch == _pageEpoch) Post(new { type = "drawingSaved", v = 1, localId, id = (string?)b["id"]?.GetValue<string>(), drawing = b.DeepClone() });
            else Post(new { type = "upsertDrawing", v = 1, drawing = b.DeepClone() });
        }

        /// <summary>
        /// The backend refused a page step (create / change / delete): the page reverts it (same page instance) or,
        /// for a re-created page, the host restores <paramref name="before"/> with <c>upsertDrawing</c>.
        /// </summary>
        public void AnswerRejected(string symbol, string op, string? localId, string? id, int epoch, JsonObject? before, string message)
        {
            if (symbol != _drawingsSymbol) return;
            if (before != null && op != "create") Replace((JsonObject)before.DeepClone()); // host copy = the reverted state
            if (message.Length > 300) message = message.Substring(0, 300);
            if (epoch == _pageEpoch)
                Post(new { type = "drawingRejected", v = 1, op, localId, id, message });
            // Revision 3: after a refused change/delete the host's state is authoritative — always upsert it (the page
            // keeps its local state until then; a re-created page gets it the same way).
            if (before != null && op != "create")
                Post(new { type = "upsertDrawing", v = 1, drawing = before.DeepClone() });
        }

        /// <summary>Persisted backend state the page does not have yet (import append, split rescale, a differing PATCH answer).</summary>
        public void UpsertDrawing(string symbol, DrawingDto drawing)
        {
            var b = DrawingFromApi(drawing);
            if (b == null || symbol != _drawingsSymbol) return;
            Replace(b);
            Post(new { type = "upsertDrawing", v = 1, drawing = b.DeepClone() });
        }

        /// <summary>Object-panel edit through the page (undoable; persisted on the echoed <c>drawingChanged</c>).</summary>
        public bool EditDrawing(string id, JsonObject changes)
        {
            if (!ServerIdPattern.IsMatch(id) || changes.Count == 0) return false;
            foreach (var kv in changes) if (!ChangeKeys.Contains(kv.Key) || kv.Key == "points") return false;
            return Post(new { type = "editDrawing", v = 1, id, changes });
        }

        /// <summary>Object-panel delete through the page (undoable; echoed as <c>drawingDeleted</c>).</summary>
        public bool RemoveDrawing(string id) => ServerIdPattern.IsMatch(id) && Post(new { type = "removeDrawing", v = 1, id });

        public bool SelectDrawing(string? id) => (id == null || ServerIdPattern.IsMatch(id)) && Post(new { type = "selectDrawing", v = 1, id });

        public bool JumpToDrawing(string id) => ServerIdPattern.IsMatch(id) && Post(new { type = "jumpToDrawing", v = 1, id });

        /// <summary>Tool for new drawings (null = cursor) with the rail's style.</summary>
        public void SetTool(string? tool, JsonObject? style)
        {
            if (tool != null && !ToolPoints.ContainsKey(tool)) return;
            _tool = tool;
            _toolStyle = style == null ? null : (JsonObject)style.DeepClone();
            SendTool();
        }

        public void SetMagnet(string mode)
        {
            if (mode != "off" && mode != "weak" && mode != "strong") return;
            _magnet = mode;
            Post(new { type = "setMagnet", v = 1, mode });
        }

        /// <summary>Bridge <c>command</c> (hideAll/showAll is a view state the host re-applies after a page reload).</summary>
        public bool SendCommand(string name)
        {
            if (!Commands.Contains(name)) return false;
            if (name == "hideAll") _drawingsHidden = true;
            if (name == "showAll") _drawingsHidden = false;
            return Post(new { type = "command", v = 1, name });
        }

        /// <summary>
        /// Replaces the indicator set (an /indicators answer). <paramref name="t"/> ISO bar starts → epoch ms here; series
        /// keep their nulls (no stand-in values). An empty <paramref name="indicators"/> removes all panes.
        /// </summary>
        /// <returns>null when sent / stored, else the reason (shown by the view).</returns>
        public string? SetIndicators(IReadOnlyList<IndicatorInfoDto>? indicators, IReadOnlyList<string>? t, IReadOnlyDictionary<string, List<double?>>? series)
        {
            var defs = new JsonArray();
            var outputs = new HashSet<string>(StringComparer.Ordinal);
            var sub = 0;
            foreach (var info in indicators ?? Array.Empty<IndicatorInfoDto>())
            {
                if (info.Key == null || info.Pane is not ("overlay" or "sub") || info.Outputs == null || info.Outputs.Count == 0) return "unexpected indicator description from the backend";
                if (info.Pane == "sub") sub++;
                var outs = new JsonArray();
                foreach (var o in info.Outputs) { outs.Add(o); outputs.Add(o); }
                var label = info.Label ?? info.Key;
                if (label.Length > 80) label = label.Substring(0, 80);
                var note = info.Note;
                if (note != null && note.Length > 300) note = note.Substring(0, 300);
                defs.Add(new JsonObject
                {
                    ["key"] = info.Key, ["kind"] = info.Kind ?? "", ["pane"] = info.Pane, ["outputs"] = outs, ["label"] = label, ["note"] = note,
                });
            }
            if (defs.Count > MaxIndicatorSpecs) return "more than " + MaxIndicatorSpecs + " indicators";
            if (sub > MaxSubPanes) return "more than " + MaxSubPanes + " indicator panes";
            if (!TryConvertSeries(t, series, outputs, out var times, out var values, out var problem)) return problem;
            _indicatorDefs = defs;
            _indicatorT.Clear();
            _indicatorT.AddRange(times);
            _indicatorSeries.Clear();
            foreach (var kv in values) _indicatorSeries[kv.Key] = kv.Value;
            SendIndicators();
            return null;
        }

        /// <summary>Merges older pages / refreshed values by <c>t</c> into the current set (<c>updateIndicators</c>).</summary>
        public string? UpdateIndicators(IReadOnlyList<string>? t, IReadOnlyDictionary<string, List<double?>>? series)
        {
            if (_indicatorDefs == null || _indicatorDefs.Count == 0) return null;
            var outputs = new HashSet<string>(_indicatorSeries.Keys, StringComparer.Ordinal);
            if (!TryConvertSeries(t, series, outputs, out var times, out var values, out var problem)) return problem;
            if (times.Count == 0) return null;
            // Merge by t into the host copy (newer values win), keep it strictly increasing.
            var index = new SortedDictionary<long, int>();
            for (var i = 0; i < _indicatorT.Count; i++) index[_indicatorT[i]] = i;
            var merged = new SortedSet<long>(_indicatorT);
            foreach (var x in times) merged.Add(x);
            var mergedT = merged.ToList();
            if (mergedT.Count > MaxBars) mergedT = mergedT.GetRange(mergedT.Count - MaxBars, MaxBars);
            var position = new Dictionary<long, int>();
            for (var i = 0; i < times.Count; i++) position[times[i]] = i;
            foreach (var key in _indicatorSeries.Keys.ToList())
            {
                var old = _indicatorSeries[key];
                var incoming = values.TryGetValue(key, out var inc) ? inc : null;
                var list = new List<double?>(mergedT.Count);
                foreach (var x in mergedT)
                {
                    if (incoming != null && position.TryGetValue(x, out var pi)) list.Add(incoming[pi]);
                    else if (index.TryGetValue(x, out var oi)) list.Add(old[oi]);
                    else list.Add(null);
                }
                _indicatorSeries[key] = list;
            }
            _indicatorT.Clear();
            _indicatorT.AddRange(mergedT);
            var payload = new Dictionary<string, object?>(StringComparer.Ordinal);
            foreach (var kv in values) payload[kv.Key] = kv.Value;
            if (PageRevision >= 2) Post(new { type = "updateIndicators", v = 1, t = times, series = payload });
            return null;
        }

        /// <summary>True when the backend's answer differs from the host copy of the page state (→ <see cref="UpsertDrawing"/>).</summary>
        public bool DiffersFromHostCopy(DrawingDto saved)
        {
            var b = DrawingFromApi(saved);
            if (b == null || saved.Id == null) return false; // invalid answers are not pushed to the page
            var current = Find(saved.Id);
            return current == null || Canonical(current) != Canonical(b);
        }

        /// <summary>Order-independent text of a bridge drawing (numbers in invariant "R" form) for comparisons.</summary>
        private static string Canonical(JsonObject d)
        {
            var sb = new StringBuilder();
            foreach (var key in new[] { "id", "tool", "name", "text", "locked", "visible", "alert_id" })
                sb.Append(key).Append('=').Append(d[key]?.ToJsonString() ?? "null").Append(';');
            sb.Append("points=");
            if (d["points"] is JsonArray pts)
                foreach (var p in pts)
                {
                    TryLong(p?["t"], out var t);
                    var pv = 0.0;
                    if (p?["p"] is JsonValue v && !v.TryGetValue<double>(out pv) && v.TryGetValue<long>(out var l)) pv = l;
                    sb.Append(t.ToString(CultureInfo.InvariantCulture)).Append(':').Append(pv.ToString("R", CultureInfo.InvariantCulture)).Append(',');
                }
            sb.Append(";style=");
            if (d["style"] is JsonObject st)
                foreach (var key in new[] { "color", "width", "dash", "levels" })
                    sb.Append(key).Append('=').Append(st[key]?.ToJsonString() ?? "null").Append(',');
            sb.Append(";tf=").Append(d["timeframes"]?.ToJsonString() ?? "null");
            return sb.ToString();
        }

        /// <summary>Removes every indicator pane.</summary>
        public void ClearIndicators()
        {
            var had = _indicatorDefs != null;
            _indicatorDefs = null;
            _indicatorT.Clear();
            _indicatorSeries.Clear();
            if (had && PageRevision >= 2) Post(new { type = "setIndicators", v = 1, indicators = Array.Empty<object>(), t = Array.Empty<long>(), series = new Dictionary<string, object>() });
        }

        // =====================================================================================
        // Page → host (called from HandlePageMessage)
        // =====================================================================================

        /// <summary>Handles the revision-2 page messages; false = not one of them.</summary>
        private bool HandleDrawingMessage(string? type, JsonElement m)
        {
            switch (type)
            {
                case "drawingCreated":
                {
                    var localId = Str(m, "localId");
                    if (localId == null || !LocalIdPattern.IsMatch(localId) || !m.TryGetProperty("drawing", out var d) || d.ValueKind != JsonValueKind.Object)
                    { LogOnce("dc", "Market Research chart: dropped a malformed drawingCreated."); return true; }
                    var clientId = Str(m, "clientId");
                    if (clientId != null && !ClientIdPattern.IsMatch(clientId)) { LogOnce("dccid", "Market Research chart: dropped a drawingCreated with an invalid clientId."); return true; }
                    if (clientId == null && PageRevision >= 3) { LogOnce("dccid0", "Market Research chart: dropped a drawingCreated without clientId (revision 3)."); return true; }
                    var node = JsonNode.Parse(d.GetRawText()) as JsonObject;
                    if (node == null || !LooksLikePageDrawing(node)) { LogOnce("dc2", "Market Research chart: dropped a drawingCreated with an invalid drawing."); return true; }
                    node.Remove("id");
                    node.Remove("client_id");
                    DrawingCreated?.Invoke(this, new PageDrawingCreated(localId, clientId, node, _pageEpoch));
                    return true;
                }
                case "drawingChanged":
                {
                    var id = Str(m, "id");
                    if (id == null || !ServerIdPattern.IsMatch(id) || !m.TryGetProperty("changes", out var ch) || ch.ValueKind != JsonValueKind.Object)
                    { LogOnce("dch", "Market Research chart: dropped a malformed drawingChanged."); return true; }
                    if (m.TryGetProperty("final", out var fin) && fin.ValueKind == JsonValueKind.False) return true; // reserved (MR5 sends final only)
                    var changes = JsonNode.Parse(ch.GetRawText()) as JsonObject;
                    if (changes == null || changes.Count == 0 || changes.Any(kv => !ChangeKeys.Contains(kv.Key)) ||
                        (changes["points"] is JsonNode pts && !PointsLookValid(pts)))
                    { LogOnce("dch2", "Market Research chart: dropped a drawingChanged with invalid changes."); return true; }
                    var current = Find(id);
                    var before = current == null ? null : (JsonObject)current.DeepClone();
                    if (current != null) ApplyChanges(current, changes);
                    DrawingChanged?.Invoke(this, new PageDrawingChanged(id, changes, before, _pageEpoch));
                    return true;
                }
                case "drawingDeleted":
                {
                    var id = Str(m, "id");
                    if (id == null || !ServerIdPattern.IsMatch(id)) { LogOnce("dd", "Market Research chart: dropped a malformed drawingDeleted."); return true; }
                    var current = Find(id);
                    if (current != null) _drawings.Remove(current);
                    DrawingDeleted?.Invoke(this, new PageDrawingDeleted(id, current, _pageEpoch));
                    return true;
                }
                case "selectionChanged":
                {
                    var id = Str(m, "id");
                    var localId = Str(m, "localId");
                    if (id != null && !ServerIdPattern.IsMatch(id)) id = null;
                    if (localId != null && !LocalIdPattern.IsMatch(localId)) localId = null;
                    JsonObject? drawing = null;
                    if (m.TryGetProperty("drawing", out var d) && d.ValueKind == JsonValueKind.Object) drawing = JsonNode.Parse(d.GetRawText()) as JsonObject;
                    DrawingSelectionChanged?.Invoke(this, new PageSelection(id, localId, drawing));
                    return true;
                }
                case "undoState":
                {
                    var canUndo = m.TryGetProperty("canUndo", out var u) && u.ValueKind == JsonValueKind.True;
                    var canRedo = m.TryGetProperty("canRedo", out var r) && r.ValueKind == JsonValueKind.True;
                    UndoStateChanged?.Invoke(this, (canUndo, canRedo));
                    return true;
                }
                case "toolChanged":
                {
                    string? name = null;
                    if (m.TryGetProperty("name", out var n) && n.ValueKind == JsonValueKind.String) name = n.GetString();
                    if (name != null && !ToolPoints.ContainsKey(name)) return true;
                    _tool = name;
                    PageToolChanged?.Invoke(this, name);
                    return true;
                }
                case "shortcut":
                {
                    var key = Str(m, "key");
                    if (key is "ctrl+k" or "ctrl+t" or "ctrl+w" or "ctrl+tab" or "ctrl+shift+tab") ShortcutPressed?.Invoke(this, key);
                    return true;
                }
                default:
                    return false;
            }
        }

        /// <summary>Called on every page <c>ready</c>: revision, new epoch, then the MR5 state after loadBars.</summary>
        private void OnPageReadyMr5(JsonElement m)
        {
            PageRevision = m.TryGetProperty("revision", out var rev) && rev.ValueKind == JsonValueKind.Number && rev.TryGetInt32(out var r) ? r : 1;
            _pageEpoch++;
        }

        /// <summary>Re-sends drawings, indicators, tool, magnet and hide-all to a (re-created) page (after loadBars).</summary>
        private void SendMr5State()
        {
            if (PageRevision < 2) return;
            if (_drawingsSymbol != null && _drawingsSymbol == _symbol) SendDrawings();
            if (_indicatorDefs != null) SendIndicators();
            SendTool();
            Post(new { type = "setMagnet", v = 1, mode = _magnet });
            if (_drawingsHidden) Post(new { type = "command", v = 1, name = "hideAll" });
        }

        private void SendDrawings()
        {
            if (PageRevision < 2) return;
            var chunk = new List<JsonObject>();
            var bytes = 0;
            var first = true;
            foreach (var d in _drawings)
            {
                var size = Encoding.UTF8.GetByteCount(d.ToJsonString());
                if (chunk.Count > 0 && bytes + size > DrawingChunkBytes)
                {
                    PostDrawingChunk(chunk, first);
                    first = false;
                    chunk = new List<JsonObject>();
                    bytes = 0;
                }
                chunk.Add((JsonObject)d.DeepClone());
                bytes += size;
            }
            if (chunk.Count > 0 || first) PostDrawingChunk(chunk, first);
        }

        private void PostDrawingChunk(List<JsonObject> chunk, bool first)
        {
            if (first) Post(new { type = "loadDrawings", v = 1, drawings = chunk });
            else Post(new { type = "loadDrawings", v = 1, drawings = chunk, append = true });
        }

        private void SendTool()
        {
            if (PageRevision < 2) return;
            if (_toolStyle != null) Post(new { type = "setTool", v = 1, name = _tool, style = _toolStyle.DeepClone() });
            else Post(new { type = "setTool", v = 1, name = _tool });
        }

        private void SendIndicators()
        {
            if (PageRevision < 2 || _indicatorDefs == null) return;
            var series = new Dictionary<string, object?>(StringComparer.Ordinal);
            foreach (var kv in _indicatorSeries) series[kv.Key] = kv.Value;
            Post(new { type = "setIndicators", v = 1, indicators = _indicatorDefs.DeepClone(), t = _indicatorT, series });
        }

        // =====================================================================================
        // Conversion + checks (API ISO ↔ bridge ms, the ONE place — SPEC §0 Time)
        // =====================================================================================

        /// <summary>API drawing → bridge drawing; null = fails the checks the page applies (dropped + logged, never repaired).</summary>
        internal static JsonObject? DrawingFromApi(DrawingDto? d)
        {
            if (d?.Id == null || !ServerIdPattern.IsMatch(d.Id) || d.Tool == null || !ToolPoints.TryGetValue(d.Tool, out var count) || d.Points == null) return null;
            if (d.Points.Count < count.Min || d.Points.Count > count.Max) return null;
            var points = new JsonArray();
            foreach (var p in d.Points)
            {
                if (!TryToEpochMs(p.T, out var ms) || p.P is not double price || !double.IsFinite(price) || price <= 0 || price > 1e9) return null;
                points.Add(new JsonObject { ["t"] = ms, ["p"] = price });
            }
            var style = d.Style;
            var color = style?.Color ?? "accent";
            var width = style?.Width ?? 1;
            var dash = style?.Dash ?? "solid";
            if (!PaletteKeys.Contains(color) || width < 1 || width > 4 || (dash != "solid" && dash != "dashed")) return null;
            JsonArray? levels = null;
            if (style?.Levels != null)
            {
                if (d.Tool != "fibonacciLine" || style.Levels.Count < 1 || style.Levels.Count > 12 || style.Levels.Distinct().Count() != style.Levels.Count ||
                    style.Levels.Any(x => !double.IsFinite(x) || x < -100 || x > 400)) return null;
                levels = new JsonArray();
                foreach (var x in style.Levels) levels.Add(x);
            }
            if (d.Name != null && (d.Name.Length < 1 || d.Name.Length > 40)) return null;
            if (d.Text != null && (d.Text.Length < 1 || d.Text.Length > 280)) return null;
            if (d.Tool == "simpleAnnotation" && string.IsNullOrEmpty(d.Text)) return null;
            if (d.AlertId != null && (d.Tool != "alertLine" || !ServerIdPattern.IsMatch(d.AlertId))) return null;
            JsonNode? timeframes;
            if (d.Timeframes.ValueKind == JsonValueKind.String && d.Timeframes.GetString() == "all") timeframes = "all";
            else
            {
                var tfs = d.TimeframeList;
                if (tfs == null || tfs.Count == 0 || tfs.Distinct().Count() != tfs.Count || tfs.Any(x => !Timeframes.Contains(x))) return null;
                var arr = new JsonArray();
                foreach (var x in tfs) arr.Add(x);
                timeframes = arr;
            }
            return new JsonObject
            {
                ["id"] = d.Id,
                ["tool"] = d.Tool,
                ["points"] = points,
                ["style"] = new JsonObject { ["color"] = color, ["width"] = width, ["dash"] = dash, ["levels"] = levels },
                ["name"] = d.Name,
                ["text"] = d.Text,
                ["locked"] = d.Locked == true,
                ["visible"] = d.Visible != false,
                ["timeframes"] = timeframes,
                ["alert_id"] = d.AlertId,
            };
        }

        /// <summary>Bridge points (ms) → API points (ISO); null when a point is not convertible.</summary>
        internal static List<Dictionary<string, object>>? PointsToApi(JsonNode? points)
        {
            if (points is not JsonArray arr) return null;
            var list = new List<Dictionary<string, object>>(arr.Count);
            foreach (var n in arr)
            {
                if (n is not JsonObject o || !TryLong(o["t"], out var ms) || !TryDouble(o["p"], out var p)) return null;
                if (ms < MinEpochMs || ms >= MaxEpochMs) return null;
                list.Add(new Dictionary<string, object> { ["t"] = ToIso(ms), ["p"] = p });
            }
            return list;
        }

        /// <summary>Bridge drawing (from the page) → POST body (+ <c>client_id</c>, revision 3); null when the points can't be converted.</summary>
        internal static Dictionary<string, object?>? CreateBody(string symbol, JsonObject drawing, string? clientId = null)
        {
            var points = PointsToApi(drawing["points"]);
            var tool = (drawing["tool"] as JsonValue)?.TryGetValue<string>(out var t) == true ? t : null;
            if (points == null || tool == null) return null;
            var body = new Dictionary<string, object?>(StringComparer.Ordinal) { ["symbol"] = symbol, ["tool"] = tool, ["points"] = points };
            foreach (var key in new[] { "style", "name", "text", "locked", "visible", "timeframes", "alert_id" })
                if (drawing.TryGetPropertyValue(key, out var v)) body[key] = v?.DeepClone();
            if (clientId != null) body["client_id"] = clientId; // the same id on every retry → no duplicate (backend UNIQUE)
            return body;
        }

        /// <summary>Bridge changes → PATCH body (only the changed keys; points converted); null when not convertible.</summary>
        internal static Dictionary<string, object?>? PatchBody(JsonObject changes)
        {
            var body = new Dictionary<string, object?>(StringComparer.Ordinal);
            foreach (var kv in changes)
            {
                if (!ChangeKeys.Contains(kv.Key)) return null;
                if (kv.Key == "points")
                {
                    var points = PointsToApi(kv.Value);
                    if (points == null) return null;
                    body["points"] = points;
                }
                else body[kv.Key] = kv.Value?.DeepClone();
            }
            return body.Count == 0 ? null : body;
        }

        private static bool LooksLikePageDrawing(JsonObject d)
        {
            var tool = (d["tool"] as JsonValue)?.TryGetValue<string>(out var t) == true ? t : null;
            if (tool == null || !ToolPoints.TryGetValue(tool, out var count)) return false;
            if (d["points"] is not JsonArray pts || pts.Count < count.Min || pts.Count > count.Max) return false;
            return PointsLookValid(pts) && d.All(kv => kv.Key is "tool" or "points" or "id" or "client_id" || ChangeKeys.Contains(kv.Key));
        }

        private static bool PointsLookValid(JsonNode points) => PointsToApi(points) != null;

        private JsonObject? Find(string id)
        {
            foreach (var d in _drawings)
                if ((d["id"] as JsonValue)?.TryGetValue<string>(out var x) == true && x == id) return d;
            return null;
        }

        private void Replace(JsonObject drawing)
        {
            var id = (drawing["id"] as JsonValue)?.TryGetValue<string>(out var x) == true ? x : null;
            if (id == null) return;
            var existing = Find(id);
            if (existing != null) _drawings[_drawings.IndexOf(existing)] = drawing;
            else if (_drawings.Count < MaxDrawings) _drawings.Add(drawing);
        }

        private static void ApplyChanges(JsonObject target, JsonObject changes)
        {
            foreach (var kv in changes)
            {
                if (kv.Key == "style" && kv.Value is JsonObject styleChanges && target["style"] is JsonObject style)
                {
                    foreach (var s in styleChanges) style[s.Key] = s.Value?.DeepClone();
                }
                else target[kv.Key] = kv.Value?.DeepClone();
            }
        }

        private static bool TryConvertSeries(IReadOnlyList<string>? t, IReadOnlyDictionary<string, List<double?>>? series, HashSet<string> outputs,
                                             out List<long> times, out Dictionary<string, List<double?>> values, out string? problem)
        {
            times = new List<long>();
            values = new Dictionary<string, List<double?>>(StringComparer.Ordinal);
            problem = null;
            var n = t?.Count ?? 0;
            if (n > MaxBars) { problem = "more than " + MaxBars + " indicator values"; return false; }
            long last = long.MinValue;
            for (var i = 0; i < n; i++)
            {
                if (!TryToEpochMs(t![i], out var ms) || ms <= last) { problem = "indicator times are not valid / increasing"; return false; }
                times.Add(ms);
                last = ms;
            }
            foreach (var key in outputs)
            {
                if (series == null || !series.TryGetValue(key, out var list) || list == null || list.Count != n)
                { problem = "indicator series '" + ClipText(key, 40) + "' is missing or has the wrong length"; return false; }
                values[key] = list.Select(x => x.HasValue && double.IsFinite(x.Value) ? x : null).ToList();
            }
            return true;
        }

        private static string? Str(JsonElement m, string name)
            => m.TryGetProperty(name, out var e) && e.ValueKind == JsonValueKind.String ? e.GetString() : null;

        private static bool TryLong(JsonNode? n, out long value)
        {
            value = 0;
            if (n is not JsonValue v) return false;
            if (v.TryGetValue<long>(out value)) return true;
            if (v.TryGetValue<double>(out var d) && double.IsFinite(d) && Math.Floor(d) == d && Math.Abs(d) < 9e15) { value = (long)d; return true; }
            return false;
        }

        private static bool TryDouble(JsonNode? n, out double value)
        {
            value = 0;
            if (n is not JsonValue v) return false;
            if (!v.TryGetValue<double>(out value))
            {
                if (v.TryGetValue<long>(out var l)) value = l;
                else return false;
            }
            return double.IsFinite(value) && value > 0 && value <= 1e9;
        }
    }
}

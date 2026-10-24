/*
 * TEST DOUBLE ONLY — never shipped. A minimal stand-in for the KLineChart 10.x API subset that
 * chart.js uses, so the bridge logic can be tested headless while the real engine cannot be
 * downloaded (registry egress blocked, 2026-10-16). Data semantics mirror KLineChart 10.0.3
 * `Store._addData` / `_processDataLoad` (init / 'forward' = older bars prepended / single-bar
 * update: same t replaces last, newer appends, older ignored). MR5: overlays (built-in names +
 * registerOverlay templates, interactive drawing completed by test hooks __finishDrawing / __move /
 * __select), indicator figures in sub panes, removeIndicator, convertTo/FromPixel. Rendering is
 * deliberately simple (candles, area, volume, grid, axes, last-price mark, overlays) — it says
 * nothing about how the real engine looks. Run the harness with --engine real once
 * vendor/klinecharts.min.js exists.
 */
(function () {
  'use strict';
  var templates = {};
  function isObj(o) { return o !== null && typeof o === 'object' && !Array.isArray(o); }
  function merge(target, src) {
    for (var k in src) {
      if (!Object.prototype.hasOwnProperty.call(src, k)) continue;
      var v = src[k];
      if (isObj(v)) { if (!isObj(target[k])) target[k] = {}; merge(target[k], v); }
      else target[k] = Array.isArray(v) ? v.slice() : v;
    }
    return target;
  }
  // Mirrors the KLineChart 10 default style tree for every key chart.js sets (values arbitrary).
  function text() { return { show: true, style: 'fill', color: '#fff', size: 12, family: 'Helvetica Neue', weight: 'normal',
    backgroundColor: '#333', borderColor: '#333', borderSize: 1, borderRadius: 2, paddingLeft: 4, paddingRight: 4, paddingTop: 4, paddingBottom: 4 }; }
  function line() { return { show: true, style: 'dashed', dashedValue: [4, 2], size: 1, color: '#888' }; }
  function axis() { return { show: true, axisLine: { show: true, color: '#333', size: 1 }, tickLine: { show: true, color: '#333', size: 1, length: 3 },
    tickText: { show: true, color: '#888', size: 12, family: 'Helvetica Neue', weight: 'normal', marginStart: 4, marginEnd: 4 } }; }
  function mark() { return { show: true, color: '#D9D9D9', textOffset: 5, textSize: 10, textFamily: 'Helvetica Neue', textWeight: 'normal' }; }
  function defaultStyles() {
    var o = {
      grid: { show: true, horizontal: { show: true, size: 1, color: '#333', style: 'dashed', dashedValue: [2, 2] }, vertical: { show: true, size: 1, color: '#333', style: 'dashed', dashedValue: [2, 2] } },
      candle: {
        type: 'candle_solid',
        bar: { compareRule: 'current_open', upColor: '#2DC08E', downColor: '#F92855', noChangeColor: '#888', upBorderColor: '#2DC08E', downBorderColor: '#F92855', noChangeBorderColor: '#888', upWickColor: '#2DC08E', downWickColor: '#F92855', noChangeWickColor: '#888' },
        area: { lineSize: 2, lineColor: '#1677FF', smooth: false, value: 'close', backgroundColor: [{ offset: 0, color: 'rgba(22,119,255,0.01)' }, { offset: 1, color: 'rgba(22,119,255,0.2)' }], point: { show: true } },
        priceMark: { show: true, high: mark(), low: mark(),
          last: { show: true, compareRule: 'current_open', upColor: '#2DC08E', downColor: '#F92855', noChangeColor: '#888', line: line(), text: text(), extendTexts: [] } },
        tooltip: { offsetLeft: 4, offsetTop: 6, showRule: 'always', showType: 'standard', title: { show: true }, legend: {}, features: [] }
      },
      indicator: { ohlc: {}, bars: [{ style: 'fill', borderStyle: 'solid', borderSize: 1, borderDashedValue: [2, 2], upColor: 'rgba(45,192,142,.7)', downColor: 'rgba(249,40,85,.7)', noChangeColor: '#888' }],
        lines: [], circles: [], lastValueMark: { show: false, text: text() }, tooltip: { offsetLeft: 4, offsetTop: 6, showRule: 'always', showType: 'standard', title: {}, legend: {}, features: [] } },
      xAxis: axis(), yAxis: axis(),
      separator: { size: 1, color: '#888', fill: true, activeBackgroundColor: 'rgba(230,230,230,.15)' },
      crosshair: { show: true, horizontal: { show: true, line: line(), text: text(), features: [] }, vertical: { show: true, line: line(), text: text() } },
      overlay: {}
    };
    var drop = (window.__fakeOpts && window.__fakeOpts.dropStyleKey) || null;   // e.g. 'candle.tooltip'
    if (drop) { var parts = drop.split('.'), t = o; for (var i = 0; i < parts.length - 1; i++) t = t[parts[i]]; delete t[parts[parts.length - 1]]; }
    return o;
  }
  function opt(name, def) {
    if (name === 'resetClearsLoading' && typeof window.__resetClearsLoading === 'boolean') return window.__resetClearsLoading;
    var o = window.__fakeOpts || {};
    return Object.prototype.hasOwnProperty.call(o, name) ? o[name] : def;
  }

  function Chart(dom, options) {
    var self = this;
    this._dom = dom;
    this._styles = merge(defaultStyles(), (options && options.styles) || {});
    this._tz = (options && options.timezone) || 'UTC';
    this._locale = (options && options.locale) || 'en-US';
    this._data = [];
    this._more = { forward: false, backward: false };
    this._loading = false;
    this._symbol = null; this._period = null; this._loader = null;
    this._actions = {};
    this._indicators = [];
    this._barSpace = 8;
    this._offset = -3;              // bars between the last bar and the right edge (negative = gap)
    this._panes = [];               // non-candle panes in creation order
    this._paneHeight = {};
    this._volPane = null;
    this._overlays = {};
    this._drawingId = null;
    this._idSeq = 0;
    this._cross = null;
    this._canvas = document.createElement('canvas');
    this._canvas.style.position = 'absolute'; this._canvas.style.left = '0'; this._canvas.style.top = '0';
    dom.appendChild(this._canvas);
    this._canvas.addEventListener('mousemove', function (e) { self._onMove(e); });
    this._canvas.addEventListener('mouseleave', function () { self._cross = null; self._emit('onCrosshairChange', {}); self._invalidate(); });
    this.resize();
  }
  var P = Chart.prototype;
  P.getDom = function () { return this._dom; };
  P.resize = function () {
    var r = this._dom.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    this._w = Math.max(50, Math.floor(r.width)); this._h = Math.max(50, Math.floor(r.height));
    this._canvas.width = this._w * dpr; this._canvas.height = this._h * dpr;
    this._canvas.style.width = this._w + 'px'; this._canvas.style.height = this._h + 'px';
    this._dpr = dpr;
    this._invalidate();
  };
  P.getSize = function () { return { width: this._w, height: this._h }; };
  P.setStyles = function (s) { merge(this._styles, s || {}); this._invalidate(); };
  P.getStyles = function () { return JSON.parse(JSON.stringify(this._styles)); };
  P.setTimezone = function (tz) { this._tz = tz; this._invalidate(); };
  P.getTimezone = function () { return this._tz; };
  P.setLocale = function (l) { this._locale = l; };
  P.getLocale = function () { return this._locale; };
  P.setSymbol = function (s) { this._symbol = merge({ pricePrecision: 2, volumePrecision: 0 }, s); this.resetData(); };
  P.getSymbol = function () { return this._symbol; };
  P.setPeriod = function (p) { this._period = p; this.resetData(); };
  P.getPeriod = function () { return this._period; };
  P.setDataLoader = function (l) { var self = this; this.resetData(function () { self._loader = l; }); };
  P.getDataList = function () { return this._data; };
  P.resetData = function (fn) {
    if (this._loader && this._symbol && this._period && this._loader.unsubscribeBar) this._loader.unsubscribeBar({ symbol: this._symbol, period: this._period });
    if (fn) fn();
    // Real KLineChart 10.0.3 clears its loading flag here; opts.resetClearsLoading=false models an
    // engine that keeps it until the pending callback runs (review M2).
    if (opt('resetClearsLoading', true)) this._loading = false;
    this._process('init');
  };
  P._process = function (type) {
    if (this._loading || !this._loader || !this._symbol || !this._period) return;
    var self = this;
    this._loading = true;
    var params = { type: type, symbol: this._symbol, period: this._period, timestamp: null, callback: function (data, more) {
      self._loading = false;
      self._add(data, type, more);
      if (type === 'init' && self._loader.subscribeBar) {
        self._loader.subscribeBar({ symbol: self._symbol, period: self._period, callback: function (bar) { self._add(bar, 'update'); } });
      }
    } };
    if (type === 'backward') params.timestamp = this._data.length ? this._data[this._data.length - 1].timestamp : null;
    if (type === 'forward') params.timestamp = this._data.length ? this._data[0].timestamp : null;
    var loader = this._loader;
    if (opt('deferLoad', false)) setTimeout(function () { loader.getBars(params); }, 0);
    else loader.getBars(params);
  };
  P._add = function (data, type, more) {
    var adjust = false;
    if (Array.isArray(data)) {
      var m = typeof more === 'boolean' ? { backward: more, forward: more } : { backward: !!(more && more.backward), forward: !!(more && more.forward) };
      if (type === 'init') { this._data = data; this._more = m; this._offset = -3; adjust = true; }
      else if (type === 'backward') { this._data = this._data.concat(data); this._more.backward = m.backward; adjust = data.length > 0; }
      else if (type === 'forward') { this._data = data.concat(this._data); this._more.forward = m.forward; adjust = data.length > 0; }
    } else {
      var n = this._data.length, last = n ? this._data[n - 1].timestamp : 0;
      if (data.timestamp > last) { this._data.push(data); if (this._offset > 0) this._offset += 1; adjust = true; }
      else if (data.timestamp === last) { this._data[n - 1] = data; adjust = true; }
    }
    if (adjust) { this._calcAll(); this._adjust(); this._invalidate(); }
  };
  P._calcAll = function () {
    for (var i = 0; i < this._indicators.length; i++) {
      var ind = this._indicators[i];
      ind.result = ind.tpl.calc ? ind.tpl.calc(this._data, ind) : [];
    }
  };
  P._range = function () {
    var n = this._data.length, w = this._w - 64;
    var lastF = n - 1 - this._offset;
    var from = Math.max(0, Math.floor(lastF - w / this._barSpace + 1));
    var to = Math.min(n, Math.ceil(lastF) + 1);
    return { from: from, to: Math.max(from, to), realFrom: from, realTo: to, lastF: lastF };
  };
  P._adjust = function () {
    var r = this._range();
    this._emit('onVisibleRangeChange', r);
    if (r.from === 0 && this._more.forward) this._process('forward');
    else if (r.to === this._data.length && this._more.backward) this._process('backward');
  };
  P.getVisibleRange = function () { var r = this._range(); return { from: r.from, to: r.to, realFrom: r.from, realTo: r.to }; };
  P.setBarSpace = function (s) { this._barSpace = Math.max(1, Math.min(50, s)); this._adjust(); this._invalidate(); };
  P.getBarSpace = function () { var b = this._barSpace; if (opt('barSpaceNumber', false)) return b; var g = Math.max(1, Math.floor(b * 0.8)); return { bar: b, halfBar: b / 2, gapBar: g, halfGapBar: g / 2 }; };
  P.scrollToRealTime = function () { this._offset = -3; this._adjust(); this._invalidate(); };
  P.scrollToDataIndex = function (i) {
    var w = this._w - 64;
    this._offset = this._data.length - 1 - (i + w / this._barSpace - 1);
    this._adjust(); this._invalidate();
  };
  P.subscribeAction = function (type, cb) { (this._actions[type] = this._actions[type] || []).push(cb); };
  P.unsubscribeAction = function (type, cb) { var a = this._actions[type] || []; var i = a.indexOf(cb); if (i >= 0) a.splice(i, 1); };
  P._emit = function (type, data) { var a = this._actions[type] || []; for (var i = 0; i < a.length; i++) a[i](data); };
  // ---------------------------------------------------------------- indicators + panes
  P.createIndicator = function (value, isStack, paneOptions) {
    var c = typeof value === 'string' ? { name: value } : value;
    var tpl = templates[c.name];
    if (!tpl) return null;
    var id = 'ind_' + (++this._idSeq);
    // v9 style: pane from paneOptions.id; v10 style: from value.paneId (opts.paneFrom picks one)
    var from = opt('paneFrom', 'both');
    var useValue = from === 'both' || from === 'value', useOpts = from === 'both' || from === 'options';
    var paneId = ((useValue && c.paneId) || (useOpts && paneOptions && paneOptions.id)) || ('pane_' + this._idSeq);
    if (paneId !== 'candle_pane' && this._panes.indexOf(paneId) < 0) {
      this._panes.push(paneId);
      this._paneHeight[paneId] = (paneOptions && paneOptions.height) || 90;
    }
    if (tpl.name === 'VOL') this._volPane = paneId;
    var ind = { id: id, name: c.name, paneId: paneId, zLevel: tpl.zLevel || 0, calcParams: c.calcParams || tpl.calcParams || [], tpl: tpl, result: [] };
    ind.result = tpl.calc ? tpl.calc(this._data, ind) : [];
    this._indicators.push(ind);
    this._invalidate();
    return opt('returnPaneId', false) ? paneId : id;
  };
  P.getIndicators = function (filter) {
    return this._indicators.filter(function (i) {
      return !filter || ((!filter.id || i.id === filter.id) && (!filter.name || i.name === filter.name) && (!filter.paneId || i.paneId === filter.paneId));
    }).map(function (i) { return { id: i.id, name: i.name, paneId: i.paneId }; });
  };
  P.removeIndicator = function (a, b) {
    var f = (a && typeof a === 'object') ? a : { paneId: a, name: b };
    if (opt('removeIndicatorV9Only', false) && typeof a === 'object') return false;
    var before = this._indicators.length;
    this._indicators = this._indicators.filter(function (i) { return !((!f.name || i.name === f.name) && (!f.paneId || i.paneId === f.paneId) && (!f.id || i.id === f.id)); });
    var self = this;
    this._panes = this._panes.filter(function (p) { return self._indicators.some(function (i) { return i.paneId === p; }); });
    if (this._volPane && this._panes.indexOf(this._volPane) < 0) this._volPane = null;
    this._invalidate();
    return this._indicators.length !== before;
  };
  P.setPaneOptions = function (o) { if (o && o.id && o.height) this._paneHeight[o.id] = o.height; this._invalidate(); };

  // ---------------------------------------------------------------- overlays
  P.createOverlay = function (spec) {
    if (!spec || !spec.name || (BUILTINS.indexOf(spec.name) < 0 && !overlayTemplates[spec.name])) return null;
    var id = spec.id || ('ov_' + (++this._idSeq));
    if (this._overlays[id]) return null;
    var o = {
      id: id, name: spec.name, groupId: spec.groupId, lock: !!spec.lock, visible: spec.visible !== false, mode: spec.mode || 'normal',
      styles: spec.styles || {}, extendData: spec.extendData, cb: spec, points: [], finished: false
    };
    if (spec.points && spec.points.length) { o.points = this._resolvePoints(spec.points); o.finished = true; }
    else this._drawingId = id;
    this._overlays[id] = o;
    this._invalidate();
    return id;
  };
  P._resolvePoints = function (pts) {
    var self = this;
    return pts.map(function (p) {
      var di = typeof p.dataIndex === 'number' ? p.dataIndex : self._indexOfT(p.timestamp);
      return { timestamp: p.timestamp, value: p.value, dataIndex: di };
    });
  };
  P._indexOfT = function (t) {
    var d = this._data, lo = 0, hi = d.length - 1;
    if (!d.length || typeof t !== 'number') return null;
    if (t > d[hi].timestamp) return hi + Math.round((t - d[hi].timestamp) / Math.max(1, d[hi].timestamp - (d[hi - 1] || d[hi]).timestamp || 60000));
    while (lo < hi) { var mid = (lo + hi) >> 1; if (d[mid].timestamp < t) lo = mid + 1; else hi = mid; }
    return lo;
  };
  P._ovPublic = function (o) { return { id: o.id, name: o.name, points: o.points.map(function (p) { return { timestamp: p.timestamp, value: p.value, dataIndex: p.dataIndex }; }), extendData: o.extendData, lock: o.lock, visible: o.visible, mode: o.mode }; };
  P.removeOverlay = function (f) {
    var id = typeof f === 'string' ? f : (f && f.id);
    var o = this._overlays[id];
    if (!o) return false;
    delete this._overlays[id];
    if (this._drawingId === id) this._drawingId = null;
    if (o.cb.onRemoved) o.cb.onRemoved({ overlay: this._ovPublic(o) });
    this._invalidate();
    return true;
  };
  P.overrideOverlay = function (ov) {
    var o = ov && this._overlays[ov.id];
    if (!o) return false;
    ['lock', 'visible', 'mode', 'styles', 'extendData'].forEach(function (k) { if (k in ov) o[k] = ov[k]; });
    if (ov.points) o.points = this._resolvePoints(ov.points);
    this._invalidate();
    return true;
  };
  P.getOverlays = function () { var self = this; return Object.keys(this._overlays).map(function (k) { return self._ovPublic(self._overlays[k]); }); };
  P.scrollToTimestamp = function (t) { var i = this._indexOfT(t); this.scrollToDataIndex(Math.max(0, (i || 0) - 20)); };
  // test hooks (the real engine has interactive drawing; the double completes it on request)
  P.__finishDrawing = function (pts) {
    var o = this._overlays[this._drawingId];
    if (!o) return false;
    o.points = this._resolvePoints(pts); o.finished = true; this._drawingId = null;
    var mode = o.mode;
    if (mode !== 'normal') o.points.forEach(function (p) { p.snapped = mode; });
    if (o.cb.onDrawEnd) o.cb.onDrawEnd({ overlay: this._ovPublic(o) });
    this._invalidate();
    return true;
  };
  P.__move = function (id, pts) {
    var o = this._overlays[id];
    if (!o || o.lock) return false;
    o.points = this._resolvePoints(pts);
    if (o.cb.onPressedMoveEnd) o.cb.onPressedMoveEnd({ overlay: this._ovPublic(o) });
    this._invalidate();
    return true;
  };
  P.__select = function (id) { var o = this._overlays[id]; if (o && o.cb.onSelected) o.cb.onSelected({ overlay: this._ovPublic(o) }); };
  P.__deselect = function (id) { var o = this._overlays[id]; if (o && o.cb.onDeselected) o.cb.onDeselected({ overlay: this._ovPublic(o) }); };
  P.__doubleClick = function (id) { var o = this._overlays[id]; if (o && o.cb.onDoubleClick) o.cb.onDoubleClick({ overlay: this._ovPublic(o) }); };
  P.__overlays = function () { return this.getOverlays(); };
  P.__drawingId = function () { return this._drawingId; };
  P.__panes = function () { return { panes: this._panes.slice(), vol: this._volPane, heights: JSON.parse(JSON.stringify(this._paneHeight)) }; };

  // ---------------------------------------------------------------- geometry
  P._layout = function () {
    var W = this._w, H = this._h, axisW = 64, axisH = 24, mainW = W - axisW, self = this;
    var sub = 0;
    this._panes.forEach(function (p) { sub += self._paneHeight[p] || 90; });
    var mainH = Math.max(60, H - axisH - sub);
    var r = this._range(), bs = this._barSpace, data = this._data;
    var lo = Infinity, hi = -Infinity;
    for (var i = r.from; i < r.to; i++) { var d = data[i]; if (d.low < lo) lo = d.low; if (d.high > hi) hi = d.high; }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    var pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
    return {
      W: W, H: H, axisW: axisW, axisH: axisH, mainW: mainW, mainH: mainH, r: r, bs: bs, lo: lo, hi: hi,
      xOf: function (i) { return mainW - (r.lastF - i + 0.5) * bs; },
      yOf: function (p) { return 8 + (hi - p) / (hi - lo) * (mainH - 16); },
      idxOfX: function (x) { return Math.round(r.lastF - (mainW - x) / bs + 0.5); },
      valOfY: function (y) { return hi - (y - 8) / (mainH - 16) * (hi - lo); }
    };
  };
  P.convertToPixel = function (pts) {
    var L = this._layout(), self = this, one = !Array.isArray(pts);
    var arr = (one ? [pts] : pts).map(function (p) {
      var i = typeof p.dataIndex === 'number' ? p.dataIndex : self._indexOfT(p.timestamp);
      return { x: i === null ? undefined : L.xOf(i), y: typeof p.value === 'number' ? L.yOf(p.value) : undefined };
    });
    return one ? arr[0] : arr;
  };
  P.convertFromPixel = function (coords) {
    var L = this._layout(), self = this;
    var arr = (Array.isArray(coords) ? coords : [coords]).map(function (c) {
      var i = L.idxOfX(c.x), d = self._data;
      var t = (i >= 0 && i < d.length) ? d[i].timestamp : undefined;
      return { dataIndex: i, timestamp: t, value: L.valOfY(c.y) };
    });
    return Array.isArray(coords) ? arr : arr[0];
  };

  // ---------------------------------------------------------------- rendering (simple)
  P._invalidate = function () {
    if (this._raf) return;
    var self = this;
    this._raf = requestAnimationFrame(function () { self._raf = 0; self._draw(); });
  };
  P._onMove = function (e) {
    var rect = this._canvas.getBoundingClientRect(), x = e.clientX - rect.left, y = e.clientY - rect.top;
    var L = this._layout();
    var idx = Math.max(0, Math.min(this._data.length - 1, L.idxOfX(x)));
    this._cross = { x: x, y: y, dataIndex: idx, kLineData: this._data[idx], timestamp: this._data[idx] && this._data[idx].timestamp };
    this._emit('onCrosshairChange', this._cross);
    this._invalidate();
  };
  function dash(ctx, st) { ctx.setLineDash(st && st.style === 'dashed' ? (st.dashedValue || [4, 4]) : []); }
  function drawFigure(ctx, f) {
    var a = f.attrs || {}, st = f.styles || {};
    if (f.type === 'line') {
      var c = a.coordinates || []; if (c.length < 2) return;
      ctx.strokeStyle = st.color || '#888'; ctx.lineWidth = st.size || 1; dash(ctx, st);
      ctx.beginPath(); c.forEach(function (p, i) { if (i) ctx.lineTo(p.x, p.y); else ctx.moveTo(p.x, p.y); }); ctx.stroke(); ctx.setLineDash([]);
    } else if (f.type === 'polygon') {
      var q = a.coordinates || []; if (q.length < 3) return;
      ctx.beginPath(); q.forEach(function (p, i) { if (i) ctx.lineTo(p.x, p.y); else ctx.moveTo(p.x, p.y); }); ctx.closePath();
      if (st.style !== 'stroke') { ctx.fillStyle = st.color || 'rgba(128,128,128,.2)'; ctx.fill(); }
      if (st.style === 'stroke' || st.style === 'stroke_fill') { ctx.strokeStyle = st.borderColor || st.color; ctx.lineWidth = st.borderSize || 1; ctx.setLineDash(st.borderStyle === 'dashed' ? (st.borderDashedValue || [4, 4]) : []); ctx.stroke(); ctx.setLineDash([]); }
    } else if (f.type === 'text') {
      ctx.font = (st.size || 11) + 'px ' + (st.family || 'monospace');
      ctx.textAlign = a.align || 'left'; ctx.textBaseline = a.baseline || 'top';
      var w = ctx.measureText(a.text).width;
      if (st.backgroundColor && st.backgroundColor !== 'transparent') {
        var bx = a.align === 'right' ? a.x - w : (a.align === 'center' ? a.x - w / 2 : a.x);
        var by = a.baseline === 'bottom' ? a.y - 14 : (a.baseline === 'middle' ? a.y - 7 : a.y);
        ctx.fillStyle = st.backgroundColor; ctx.fillRect(bx - 2, by - 1, w + 4, 15);
      }
      ctx.fillStyle = st.color || '#ccc'; ctx.fillText(a.text, a.x, a.y);
      ctx.textAlign = 'left';
    }
  }
  P._drawOverlays = function (ctx, L) {
    var self = this;
    Object.keys(this._overlays).forEach(function (id) {
      var o = self._overlays[id];
      if (!o.finished || !o.visible) return;
      var coords = o.points.map(function (p) { return { x: L.xOf(p.dataIndex), y: L.yOf(p.value) }; });
      var tpl = overlayTemplates[o.name];
      var figs = [];
      if (tpl) {
        figs = tpl.createPointFigures({ overlay: self._ovPublic(o), coordinates: coords, bounding: { width: L.mainW, height: L.mainH, left: 0, top: 0 } }) || [];
      } else {
        var ls = (o.styles && o.styles.line) || {}, c0 = coords[0] || { x: 0, y: 0 };
        var line = function (cs) { return { type: 'line', attrs: { coordinates: cs }, styles: ls }; };
        switch (o.name) {
          case 'horizontalStraightLine': figs = [line([{ x: 0, y: c0.y }, { x: L.mainW, y: c0.y }])]; break;
          case 'horizontalRayLine': case 'priceLine': figs = [line([c0, { x: L.mainW, y: c0.y }])]; break;
          case 'verticalStraightLine': figs = [line([{ x: c0.x, y: 0 }, { x: c0.x, y: L.mainH }])]; break;
          case 'simpleAnnotation': figs = [line([c0, { x: c0.x, y: c0.y - 30 }]), { type: 'text', attrs: { x: c0.x, y: c0.y - 32, text: String(o.extendData || ''), align: 'center', baseline: 'bottom' }, styles: o.styles.text || {} }]; break;
          case 'simpleTag': figs = [{ type: 'text', attrs: { x: L.mainW - 4, y: c0.y, text: String(o.extendData || ''), align: 'right', baseline: 'middle' }, styles: o.styles.text || {} }]; break;
          default: figs = coords.length > 1 ? [line(coords)] : [];
        }
      }
      figs.forEach(function (f) { drawFigure(ctx, f); });
    });
  };
  P._drawSeries = function (ctx, paneId, yOf, xOf, r) {
    var self = this;
    this._indicators.forEach(function (ind) {
      if (ind.paneId !== paneId || ind.tpl.draw || !ind.tpl.figures || !ind.tpl.figures.length || ind.name === 'VOL') return;
      ind.tpl.figures.forEach(function (f) {
        if (f.type === 'bar') {
          for (var i = r.from; i < r.to; i++) {
            var row = ind.result[i]; if (!row || typeof row[f.key] !== 'number') continue;
            var st = f.styles ? f.styles({ current: row }) : {};
            ctx.fillStyle = st.color || '#888';
            var y0 = yOf(f.baseValue || 0), y1 = yOf(row[f.key]);
            ctx.fillRect(Math.round(xOf(i)) - 2, Math.min(y0, y1), 4, Math.max(1, Math.abs(y1 - y0)));
          }
          return;
        }
        var st2 = f.styles ? f.styles({}) : {};
        ctx.strokeStyle = st2.color || '#888'; ctx.lineWidth = st2.size || 1; dash(ctx, st2);
        ctx.beginPath();
        var open = false;
        for (var j = r.from; j < r.to; j++) {
          var rw = ind.result[j];
          if (!rw || typeof rw[f.key] !== 'number') { open = false; continue; }
          if (open) ctx.lineTo(xOf(j), yOf(rw[f.key])); else { ctx.moveTo(xOf(j), yOf(rw[f.key])); open = true; }
        }
        ctx.stroke(); ctx.setLineDash([]);
      });
    });
  };
  P._draw = function () {
    var ctx = this._canvas.getContext('2d'), dpr = this._dpr, s = this._styles, self = this;
    var L = this._layout(), W = L.W, H = L.H, axisW = L.axisW, axisH = L.axisH, mainW = L.mainW, mainH = L.mainH;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    var data = this._data, r = L.r, bs = L.bs, xOf = L.xOf, yOf = L.yOf, lo = L.lo, hi = L.hi;
    var xAxis = { convertToPixel: xOf }, yAxis = { convertToPixel: yOf };
    var prec = (this._symbol && this._symbol.pricePrecision) || 2;
    ctx.strokeStyle = s.grid.horizontal.color; ctx.lineWidth = 1;
    ctx.font = (s.yAxis.tickText.size || 11) + 'px ' + (s.yAxis.tickText.family || 'monospace');
    ctx.fillStyle = s.yAxis.tickText.color; ctx.textBaseline = 'middle';
    for (var k = 0; k <= 5; k++) {
      var pv = lo + (hi - lo) * k / 5, y = Math.round(yOf(pv)) + 0.5;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(mainW, y); ctx.stroke();
      ctx.fillText(pv.toFixed(prec), mainW + 6, y);
    }
    ctx.strokeStyle = s.yAxis.axisLine.color;
    ctx.beginPath(); ctx.moveTo(mainW + 0.5, 0); ctx.lineTo(mainW + 0.5, H - axisH); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, H - axisH + 0.5); ctx.lineTo(W, H - axisH + 0.5); ctx.stroke();
    var fmt = new Intl.DateTimeFormat('en-US', { timeZone: this._tz, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
    ctx.textBaseline = 'top'; ctx.fillStyle = s.xAxis.tickText.color; ctx.strokeStyle = s.grid.vertical.color;
    var step = Math.max(1, Math.round(120 / bs));
    for (var xi = r.from - (r.from % step); xi < r.to; xi += step) {
      if (xi < r.from) continue;
      var xx = Math.round(xOf(xi)) + 0.5;
      ctx.beginPath(); ctx.moveTo(xx, 0); ctx.lineTo(xx, H - axisH); ctx.stroke();
      ctx.fillText(fmt.format(new Date(data[xi].timestamp)), xx - 30, H - axisH + 6);
    }
    // candle pane
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, mainW, mainH); ctx.clip();
    this._drawIndicators(ctx, 'candle_pane', xAxis, yAxis, mainW, mainH, true);
    var cs = s.candle;
    if (cs.type === 'area') {
      ctx.beginPath();
      for (var a = r.from; a < r.to; a++) { var ax = xOf(a), ay = yOf(data[a].close); if (a === r.from) ctx.moveTo(ax, ay); else ctx.lineTo(ax, ay); }
      ctx.strokeStyle = cs.area.lineColor; ctx.lineWidth = cs.area.lineSize; ctx.stroke();
      if (r.to > r.from) {
        ctx.lineTo(xOf(r.to - 1), mainH); ctx.lineTo(xOf(r.from), mainH); ctx.closePath();
        var g = ctx.createLinearGradient(0, 0, 0, mainH), bg = cs.area.backgroundColor;
        if (Array.isArray(bg)) { for (var gi = 0; gi < bg.length; gi++) g.addColorStop(bg[gi].offset, bg[gi].color); ctx.fillStyle = g; } else ctx.fillStyle = bg;
        ctx.fill();
      }
    } else {
      var bw = Math.max(1, Math.floor(bs * 0.75));
      for (var c = r.from; c < r.to; c++) {
        var b = data[c], col = b.close > b.open ? cs.bar.upColor : (b.close < b.open ? cs.bar.downColor : cs.bar.noChangeColor);
        var cx = Math.round(xOf(c));
        ctx.fillStyle = col;
        ctx.fillRect(cx, Math.round(yOf(b.high)), 1, Math.max(1, Math.round(yOf(b.low) - yOf(b.high))));
        var top = Math.round(yOf(Math.max(b.open, b.close))), bot = Math.round(yOf(Math.min(b.open, b.close)));
        ctx.fillRect(cx - Math.floor(bw / 2), top, bw, Math.max(1, bot - top));
      }
    }
    this._drawSeries(ctx, 'candle_pane', yOf, xOf, r);
    this._drawIndicators(ctx, 'candle_pane', xAxis, yAxis, mainW, mainH, false);
    this._drawOverlays(ctx, L);
    ctx.restore();
    var lm = cs.priceMark.last;
    if (cs.priceMark.show && lm.show && data.length) {
      var lb = data[data.length - 1], ly = Math.round(yOf(lb.close)) + 0.5;
      var lc = lb.close > lb.open ? lm.upColor : (lb.close < lb.open ? lm.downColor : lm.noChangeColor);
      ctx.strokeStyle = lc; ctx.lineWidth = lm.line.size || 1; ctx.setLineDash(lm.line.dashedValue || [4, 4]);
      ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(mainW, ly); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = lc; ctx.fillRect(mainW + 1, ly - 9, axisW - 2, 18);
      ctx.fillStyle = lm.text.color; ctx.textBaseline = 'middle';
      ctx.font = (lm.text.size || 11) + 'px ' + (lm.text.family || 'monospace');
      ctx.fillText(lb.close.toFixed(prec), mainW + 6, ly);
    }
    // sub panes (volume + indicator panes) in creation order
    var top0 = mainH;
    this._panes.forEach(function (pid) {
      var ph = self._paneHeight[pid] || 90;
      ctx.strokeStyle = s.separator ? s.separator.color : '#333';
      ctx.beginPath(); ctx.moveTo(0, top0 + 0.5); ctx.lineTo(W, top0 + 0.5); ctx.stroke();
      ctx.save(); ctx.translate(0, top0); ctx.beginPath(); ctx.rect(0, 0, mainW, ph); ctx.clip();
      if (pid === self._volPane) {
        var vmax = 0;
        for (var vi0 = r.from; vi0 < r.to; vi0++) if (data[vi0].volume > vmax) vmax = data[vi0].volume;
        var vb = s.indicator.bars[0], vbw = Math.max(1, Math.floor(bs * 0.75));
        var vyOf = function (v) { return ph - (vmax ? v / vmax * (ph - 6) : 0); };
        self._drawIndicators(ctx, pid, xAxis, { convertToPixel: vyOf }, mainW, ph, true);
        for (var vi = r.from; vi < r.to; vi++) {
          var vd = data[vi];
          ctx.fillStyle = vd.close > vd.open ? vb.upColor : (vd.close < vd.open ? vb.downColor : vb.noChangeColor);
          var vy = Math.round(vyOf(vd.volume));
          ctx.fillRect(Math.round(xOf(vi)) - Math.floor(vbw / 2), vy, vbw, ph - vy);
        }
        self._drawIndicators(ctx, pid, xAxis, { convertToPixel: vyOf }, mainW, ph, false);
      } else {
        var mn = Infinity, mx = -Infinity;
        self._indicators.forEach(function (ind) {
          if (ind.paneId !== pid) return;
          if (typeof ind.tpl.minValue === 'number') mn = Math.min(mn, ind.tpl.minValue);
          if (typeof ind.tpl.maxValue === 'number') mx = Math.max(mx, ind.tpl.maxValue);
          for (var i = r.from; i < r.to; i++) { var row = ind.result[i]; if (!row) continue; for (var kk in row) { if (typeof row[kk] === 'number') { mn = Math.min(mn, row[kk]); mx = Math.max(mx, row[kk]); } } }
        });
        if (!isFinite(mn)) { mn = 0; mx = 1; }
        if (mx === mn) mx = mn + 1;
        var syOf = function (v) { return 4 + (mx - v) / (mx - mn) * (ph - 8); };
        self._drawSeries(ctx, pid, syOf, xOf, r);
        ctx.fillStyle = s.yAxis.tickText.color; ctx.textBaseline = 'top';
      }
      ctx.restore();
      top0 += ph;
    });
    if (this._cross && this._cross.x < mainW) {
      ctx.strokeStyle = s.crosshair.vertical.line.color; ctx.setLineDash([4, 3]);
      var cxp = Math.round(xOf(this._cross.dataIndex)) + 0.5;
      ctx.beginPath(); ctx.moveTo(cxp, 0); ctx.lineTo(cxp, H - axisH); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, this._cross.y + 0.5); ctx.lineTo(mainW, this._cross.y + 0.5); ctx.stroke();
      ctx.setLineDash([]);
    }
  };
  P._drawIndicators = function (ctx, paneId, xAxis, yAxis, w, h, below) {
    for (var i = 0; i < this._indicators.length; i++) {
      var ind = this._indicators[i];
      if (ind.paneId !== paneId || !ind.tpl.draw) continue;
      if ((ind.zLevel < 0) !== below) continue;
      ctx.save();
      ind.tpl.draw({ ctx: ctx, chart: this, indicator: { id: ind.id, name: ind.name, paneId: ind.paneId }, bounding: { width: w, height: h, left: 0, top: 0 }, xAxis: xAxis, yAxis: yAxis });
      ctx.restore();
    }
  };

  var BUILTINS = ['segment', 'rayLine', 'straightLine', 'horizontalStraightLine', 'horizontalRayLine', 'horizontalSegment',
    'verticalStraightLine', 'verticalRayLine', 'verticalSegment', 'priceLine', 'parallelStraightLine', 'priceChannelLine',
    'fibonacciLine', 'simpleAnnotation', 'simpleTag'];
  var overlayTemplates = {};

  window.klinecharts = {
    version: function () { return '10.0.3-test-double'; },
    init: function (dom, options) {
      var el = typeof dom === 'string' ? document.getElementById(dom) : dom;
      if (!el) return null;
      var c = new Chart(el, options);
      var drop = opt('dropChartMethods', []);
      for (var i = 0; i < drop.length; i++) c[drop[i]] = undefined;
      window.__fakeChart = c;
      return c;
    },
    dispose: function () {},
    registerIndicator: function (tpl) { templates[tpl.name] = tpl; },
    registerOverlay: function (tpl) { overlayTemplates[tpl.name] = tpl; },
    getSupportedOverlays: function () {
      var hide = opt('hideBuiltins', []);
      return BUILTINS.filter(function (n) { return hide.indexOf(n) < 0; }).concat(Object.keys(overlayTemplates));
    }
  };
  if (opt('noRegisterOverlay', false)) window.klinecharts.registerOverlay = undefined;
  window.klinecharts.registerIndicator({ name: 'VOL', calcParams: [5, 10, 20], calc: function (list) { return list.map(function (k) { return { volume: k.volume }; }); } });
})();

/*
 * UFOS.ai - Market Research chart page (stage MR3a). Proof-of-concept, research only,
 * paper trading only, not investment advice.
 *
 * Bridge: HedgeFund/backend/market_research/SPEC.md section 9 + "MR3 bridge contract v1".
 * Engine: KLineChart 10.x (vendor/klinecharts.min.js, Apache-2.0), used only in the
 * "engine adapter" section below so the engine can be swapped behind the bridge.
 *
 * Rules: no network (CSP connect-src 'none'), no eval / Function / string timers, every
 * message schema-checked (types, ranges, sizes), external text only via textContent.
 * Bar `t` on the bridge = epoch ms UTC (bar start); ChartHost.cs converts from ISO.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------ constants
  var V = 1;
  var LIMITS = {
    inBytes: 4 * 1024 * 1024,     // host -> page, serialized
    outBytes: 64 * 1024,          // page -> host, serialized
    maxBars: 20000,               // loadBars
    maxPrepend: 20000,            // prependBars
    maxReplace: 5000,             // replaceBars (WS parts are <= 500)
    minT: Date.UTC(1990, 0, 1),
    maxT: Date.UTC(2100, 0, 1),
    maxPrice: 1e9,
    maxVolume: 1e15,
    symbolLen: 32,
    maxIndicators: 8,             // indicator specs per setIndicators (backend MAX_INDICATORS)
    maxSubPanes: 3,               // backend MAX_SUB_PANES (the volume pane is not counted)
    maxOutputs: 24,               // series per setIndicators / updateIndicators
    maxDrawings: 500,             // backend MAX_DRAWINGS_PER_SYMBOL
    maxBrushPoints: 500,          // backend MAX_BRUSH_POINTS (other tools: exact counts)
    maxDrawingText: 280,
    maxName: 40,
    maxMarkers: 2000,             // setMarkers (bridge revision 4)
    maxMarkerLabel: 200
  };
  var FEEDS = { sip: 1, iex_tail: 1, sip_daily: 1, crypto: 1 };
  var SESSIONS_FLAG = { pre: 1, rth: 1, post: 1 };
  var TFS = {
    '1m': { type: 'minute', span: 1 }, '5m': { type: 'minute', span: 5 },
    '15m': { type: 'minute', span: 15 }, '1H': { type: 'hour', span: 1 },
    '4H': { type: 'hour', span: 4 }, '1D': { type: 'day', span: 1 }, '1W': { type: 'week', span: 1 }
  };
  var DEFAULT_THEME = {
    background: '#111111', grid: '#202020', text: '#BFBFBF', axisText: '#8F8F8F',
    muted: '#6F6F6F', up: '#4ADE80', down: '#ED6A5A', accent: '#5AC8FA', caution: '#F0B429',
    fontFamily: 'Consolas, "Cascadia Mono", "Courier New", monospace',
    upDim: '#2E7D4F', downDim: null
  };
  var TAIL_LABEL = 'IEX \u00B7 live tail';
  var CANDLE_PANE = 'candle_pane';     // KLineChart's main pane id (v9 and v10)
  var VOL_PANE = 'ufos_volume';
  var REQUIRED_KC = ['init', 'registerIndicator', 'version'];
  var REQUIRED_CHART = ['setDataLoader', 'setSymbol', 'getSymbol', 'setPeriod', 'getPeriod', 'getDataList',
    'resetData', 'setStyles', 'getStyles', 'setTimezone', 'createIndicator', 'subscribeAction',
    'getVisibleRange', 'getBarSpace', 'setBarSpace', 'scrollToRealTime', 'scrollToDataIndex', 'resize'];

  // ------------------------------------------------------------------ state
  var state = {
    initialised: false,
    theme: copy(DEFAULT_THEME),
    locale: 'en-US',
    tz: null,                 // explicit IANA zone from init, null = PC zone
    timeMode: 'local',
    symbol: null, tf: null, session: null, assetClass: null, exchangeTz: null,
    hasMoreLeft: false, sipThrough: null,
    chartType: 'candles',
    lastPrice: null, dayChangeSign: null,
    pricePrecision: 2, volumePrecision: 0,
    loadSeq: 0,
    initList: null,           // bars served to the engine on its next 'init' load
    initServed: false,
    pendingLeft: null,        // {cb, seq} engine callback waiting for prependBars
    liveCb: null,             // engine subscribeBar callback
    hover: null,              // bar under the crosshair (null = last bar)
    refreshQueued: false
  };

  function copy(o) { var r = {}; for (var k in o) { if (Object.prototype.hasOwnProperty.call(o, k)) r[k] = o[k]; } return r; }

  // ------------------------------------------------------------------ bridge transport
  var webview = (window.chrome && window.chrome.webview) ? window.chrome.webview : null;
  var lastErrorAt = Object.create(null);

  function send(msg) {
    msg.v = V;
    var text;
    try { text = JSON.stringify(msg); } catch (e) { return; }
    if (text.length > LIMITS.outBytes) {
      if (msg.type !== 'error') reportError('too_large', 'Outgoing ' + msg.type + ' message exceeds 64 KiB and was dropped.');
      return;
    }
    if (webview) {
      webview.postMessage(msg);
    } else {
      // Plain browser (tests): deliver a copy as a DOM event the harness listens for.
      window.dispatchEvent(new CustomEvent('ufoschart:out', { detail: JSON.parse(text) }));
    }
  }

  function reportError(code, message, dedupeKey) {
    var key = code + '|' + (dedupeKey || '') + '|' + message;
    var now = Date.now();
    if (lastErrorAt[key] && now - lastErrorAt[key] < 2000) return;
    lastErrorAt[key] = now;
    send({ type: 'error', code: String(code).slice(0, 40), message: String(message).slice(0, 300) });
  }

  function receive(data) {
    var msg = data;
    if (typeof data === 'string') {
      if (data.length > LIMITS.inBytes) { reportError('too_large', 'Message larger than 4 MiB rejected.', 'string'); return; }
      try { msg = JSON.parse(data); } catch (e) { reportError('bad_message', 'Message is not valid JSON.', 'string'); return; }
    } else {
      var size;
      try { size = JSON.stringify(data).length; } catch (e) { reportError('bad_message', 'Message is not serializable.', 'object'); return; }
      if (size > LIMITS.inBytes) { reportError('too_large', 'Message larger than 4 MiB rejected.', typeName(data)); return; }
    }
    if (!isPlainObject(msg)) { reportError('bad_message', 'Message must be a JSON object.', 'object'); return; }
    var type = msg.type;
    if (typeof type !== 'string' || !Object.prototype.hasOwnProperty.call(HANDLERS, type)) {
      reportError('bad_message', 'Unknown message type.', 'type:' + (typeof type === 'string' ? type.slice(0, 40) : '?'));
      return;
    }
    if (msg.v !== V) { reportError('bad_message', type + ': unsupported version (v must be 1).', type); return; }
    var h = HANDLERS[type];
    var problem = checkFields(msg, h.fields, type);
    if (problem) { reportError('bad_message', problem, type); return; }
    if (h.needsInit && !state.initialised) { reportError('not_initialised', type + ' received before init.', type); return; }
    if (h.needsEngine && !engine.chart) { reportError('engine_missing', 'Chart engine is not available.', type); return; }
    try {
      h.run(msg);
    } catch (e) {
      reportError('internal', type + ' failed: ' + (e && e.name ? e.name : 'Error'), type);
    }
  }

  function typeName(o) { return isPlainObject(o) && typeof o.type === 'string' ? o.type.slice(0, 40) : 'object'; }

  // ------------------------------------------------------------------ schema helpers
  function isPlainObject(o) { return o !== null && typeof o === 'object' && !Array.isArray(o); }
  function isFiniteNum(x) { return typeof x === 'number' && isFinite(x); }
  function isInt(x) { return typeof x === 'number' && Math.floor(x) === x && isFinite(x); }

  // fields: {name: [required, checkFn(value) -> problem string | null]}
  function checkFields(obj, fields, where) {
    for (var k in obj) {
      if (!Object.prototype.hasOwnProperty.call(obj, k)) continue;
      if (k === 'type' || k === 'v') continue;
      if (!Object.prototype.hasOwnProperty.call(fields, k)) return where + ': unknown field "' + String(k).slice(0, 40) + '".';
    }
    for (var f in fields) {
      if (!Object.prototype.hasOwnProperty.call(fields, f)) continue;
      var spec = fields[f];
      var present = Object.prototype.hasOwnProperty.call(obj, f);
      if (!present) { if (spec[0]) return where + ': missing field "' + f + '".'; continue; }
      var p = spec[1](obj[f]);
      if (p) return where + '.' + f + ': ' + p;
    }
    return null;
  }

  function cBool(x) { return typeof x === 'boolean' ? null : 'must be a boolean'; }
  function cEnum(values) { return function (x) { return (typeof x === 'string' && Object.prototype.hasOwnProperty.call(values, x)) ? null : 'invalid value'; }; }
  function cNullable(fn) { return function (x) { return x === null ? null : fn(x); }; }
  function cColor(x) { return (typeof x === 'string' && /^#[0-9A-Fa-f]{6}$/.test(x)) ? null : 'must be #RRGGBB'; }
  function cSymbol(x) { return (typeof x === 'string' && /^[A-Z0-9][A-Z0-9.\/\-]{0,31}$/.test(x)) ? null : 'invalid symbol'; }
  function cEpoch(x) { return (isInt(x) && x >= LIMITS.minT && x < LIMITS.maxT) ? null : 'must be epoch ms (int) between 1990 and 2100'; }
  function cTz(x) {
    if (typeof x !== 'string' || x.length > 64 || !/^[A-Za-z][A-Za-z0-9_+\-\/]*$/.test(x)) return 'invalid IANA time zone';
    try { new Intl.DateTimeFormat('en-US', { timeZone: x }); } catch (e) { return 'unknown time zone'; }
    return null;
  }
  function cLocale(x) {
    if (typeof x !== 'string' || !/^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$/.test(x)) return 'invalid locale';
    try { new Intl.NumberFormat(x); } catch (e) { return 'unknown locale'; }
    return null;
  }
  function cFont(x) { return (typeof x === 'string' && /^[A-Za-z0-9 ,"'\-]{1,120}$/.test(x)) ? null : 'invalid font family'; }
  function cArray(max, itemFn) {
    return function (x) {
      if (!Array.isArray(x)) return 'must be an array';
      if (x.length > max) return 'too many items (max ' + max + ')';
      if (itemFn) { for (var i = 0; i < x.length; i++) { var p = itemFn(x[i]); if (p) return '[' + i + '] ' + p; } }
      return null;
    };
  }

  var THEME_FIELDS = {
    background: [true, cColor], grid: [true, cColor], text: [true, cColor], axisText: [true, cColor],
    muted: [true, cColor], up: [true, cColor], down: [true, cColor], accent: [true, cColor],
    caution: [true, cColor], fontFamily: [true, cFont], upDim: [false, cColor], downDim: [false, cColor]
  };
  function cTheme(x) { return isPlainObject(x) ? checkFields(x, THEME_FIELDS, 'theme') : 'must be an object'; }

  var BAR_FIELDS = {
    t: [true, cEpoch], o: [true, cPrice], h: [true, cPrice], l: [true, cPrice], c: [true, cPrice],
    v: [true, cNullable(cVolume)], feed: [true, cEnum(FEEDS)], final: [true, cBool],
    s: [false, cEnum(SESSIONS_FLAG)], vwap: [false, cNullable(cPrice)], n: [false, cNullable(cCount)]
  };
  function cPrice(x) { return (isFiniteNum(x) && x > 0 && x <= LIMITS.maxPrice) ? null : 'price must be a finite number in (0, 1e9]'; }
  function cVolume(x) { return (isFiniteNum(x) && x >= 0 && x <= LIMITS.maxVolume) ? null : 'volume must be finite and >= 0'; }
  function cCount(x) { return (isInt(x) && x >= 0 && x <= 1e12) ? null : 'must be a non-negative integer'; }
  function cBar(b) {
    if (!isPlainObject(b)) return 'bar must be an object';
    var p = checkFields(b, BAR_FIELDS, 'bar');
    if (p) return p;
    var eps = 1e-9 * Math.max(1, b.h);
    if (b.h + eps < b.l) return 'bar: h < l';
    if (b.o > b.h + eps || b.o < b.l - eps || b.c > b.h + eps || b.c < b.l - eps) return 'bar: o/c outside [l, h]';
    if (b.feed === 'iex_tail' && b.final) return 'bar: iex_tail bars are never final';
    return null;
  }
  function cBars(max) {
    var arr = cArray(max, cBar);
    return function (x) {
      var p = arr(x);
      if (p) return p;
      for (var i = 1; i < x.length; i++) { if (x[i].t <= x[i - 1].t) return '[' + i + '] t must be strictly increasing'; }
      return null;
    };
  }

  // ---- MR5 schema (bridge revision 2) - mirrors the backend drawing / indicator validation
  var TOOL_POINTS = {
    segment: 2, rayLine: 2, straightLine: 2, horizontalStraightLine: 1, horizontalRayLine: 1,
    verticalStraightLine: 1, priceLine: 1, parallelStraightLine: 3, priceChannelLine: 3, fibonacciLine: 2,
    rect: 2, measure: 2, simpleAnnotation: 1, simpleTag: 1, arrowUp: 1, arrowDown: 1, brush: -1, alertLine: 1
  };
  var PALETTE_KEYS = { accent: 1, up: 1, down: 1, caution: 1, text: 1, muted: 1, blue: 1, violet: 1 };
  var IND_KINDS = { sma: 1, ema: 1, bb: 1, rsi: 1, macd: 1, vwap: 1, cmp: 1 };
  var CTRL_TEXT = /[\u0000-\u0008\u000B-\u001F\u007F]/;     // newline + tab allowed
  var CTRL_NAME = /[\u0000-\u001F\u007F]/;

  function cPlain(max, re) {
    return function (x) {
      if (typeof x !== 'string' || x.length < 1 || x.length > max) return 'must be 1-' + max + ' characters';
      return re.test(x) ? 'control characters are not allowed' : null;
    };
  }
  function cPointPrice(x) { return (isFiniteNum(x) && x > 0 && x <= LIMITS.maxPrice) ? null : 'p must be finite in (0, 1e9]'; }
  var POINT_FIELDS = { t: [true, cEpoch], p: [true, cPointPrice] };
  function cPoint(pt) { return isPlainObject(pt) ? checkFields(pt, POINT_FIELDS, 'point') : 'point must be an object'; }
  function cLevels(x) {
    if (x === null) return null;
    if (!Array.isArray(x) || x.length < 1 || x.length > 12) return 'levels: null or 1-12 numbers';
    var seen = {};
    for (var i = 0; i < x.length; i++) {
      if (!isFiniteNum(x[i]) || x[i] < -100 || x[i] > 400) return 'levels must be in [-100, 400]';
      if (seen[x[i]]) return 'levels must be distinct';
      seen[x[i]] = 1;
    }
    return null;
  }
  var STYLE_FIELDS = {
    color: [true, cEnum(PALETTE_KEYS)],
    width: [true, function (x) { return (isInt(x) && x >= 1 && x <= 4) ? null : 'width must be 1-4'; }],
    dash: [true, cEnum({ solid: 1, dashed: 1 })],
    levels: [true, cLevels]
  };
  var STYLE_PARTIAL = {};
  for (var sk in STYLE_FIELDS) { if (Object.prototype.hasOwnProperty.call(STYLE_FIELDS, sk)) STYLE_PARTIAL[sk] = [false, STYLE_FIELDS[sk][1]]; }
  function cStyle(x) { return isPlainObject(x) ? checkFields(x, STYLE_FIELDS, 'style') : 'must be an object'; }
  function cStylePartial(x) { return isPlainObject(x) ? checkFields(x, STYLE_PARTIAL, 'style') : 'must be an object'; }
  function cTimeframes(x) {
    if (x === 'all') return null;
    if (!Array.isArray(x) || x.length < 1 || x.length > 7) return 'timeframes: "all" or 1-7 timeframes';
    var seen = {};
    for (var i = 0; i < x.length; i++) {
      if (typeof x[i] !== 'string' || !Object.prototype.hasOwnProperty.call(TFS, x[i])) return 'unknown timeframe';
      if (seen[x[i]]) return 'timeframes must be distinct';
      seen[x[i]] = 1;
    }
    return null;
  }
  function cHex32(x) { return (typeof x === 'string' && /^[0-9a-f]{32}$/.test(x)) ? null : 'must be 32 lowercase hex chars'; }
  function cLocalId(x) { return (typeof x === 'string' && /^c-[0-9]{1,9}$/.test(x)) ? null : 'must be "c-<n>"'; }
  function cAny(maxJson) { return function (x) { try { return JSON.stringify(x).length <= maxJson ? null : 'too large'; } catch (e) { return 'invalid'; } }; }
  var DRAWING_BODY = {
    tool: [true, cEnum(TOOL_POINTS)],
    points: [true, cArray(LIMITS.maxBrushPoints, cPoint)],
    style: [true, cStyle],
    name: [true, cNullable(cPlain(LIMITS.maxName, CTRL_NAME))],
    text: [true, cNullable(cPlain(LIMITS.maxDrawingText, CTRL_TEXT))],
    locked: [true, cBool], visible: [true, cBool],
    timeframes: [true, cTimeframes],
    alert_id: [true, cNullable(cHex32)]
  };
  var DRAWING_FIELDS = { id: [true, cHex32] };
  for (var dk in DRAWING_BODY) { if (Object.prototype.hasOwnProperty.call(DRAWING_BODY, dk)) DRAWING_FIELDS[dk] = DRAWING_BODY[dk]; }
  // backend keys a host may pass through unchanged (accepted, ignored)
  DRAWING_FIELDS.symbol = [false, cSymbol];
  DRAWING_FIELDS.price_basis = [false, cNullable(cAny(400))];
  DRAWING_FIELDS.created_at = [false, function (x) { return (typeof x === 'string' && x.length <= 40) ? null : 'invalid time'; }];
  DRAWING_FIELDS.updated_at = DRAWING_FIELDS.created_at;
  DRAWING_FIELDS.client_id = [false, cNullable(function (x) { return (typeof x === 'string' && /^[0-9a-fA-F-]{8,64}$/.test(x)) ? null : 'invalid client_id'; })];
  function drawingCross(d) {
    var n = TOOL_POINTS[d.tool];
    if (n === -1) { if (d.points.length < 2 || d.points.length > LIMITS.maxBrushPoints) return 'brush needs 2-' + LIMITS.maxBrushPoints + ' points'; }
    else if (d.points.length !== n) return d.tool + ' needs exactly ' + n + ' point' + (n > 1 ? 's' : '');
    if (d.tool !== 'fibonacciLine' && d.style.levels !== null) return 'style.levels is only for fibonacciLine';
    if (d.tool === 'simpleAnnotation' && (d.text === null || !d.text.trim())) return 'simpleAnnotation needs text';
    if (d.tool !== 'alertLine' && d.alert_id !== null) return 'alert_id is only for alertLine';
    return null;
  }
  function cDrawing(x) {
    if (!isPlainObject(x)) return 'drawing must be an object';
    return checkFields(x, DRAWING_FIELDS, 'drawing') || drawingCross(x);
  }
  var CHANGE_FIELDS = {
    style: [false, cStylePartial], name: [false, cNullable(cPlain(LIMITS.maxName, CTRL_NAME))],
    text: [false, cNullable(cPlain(LIMITS.maxDrawingText, CTRL_TEXT))], locked: [false, cBool],
    visible: [false, cBool], timeframes: [false, cTimeframes], alert_id: [false, cNullable(cHex32)]
  };
  function cChanges(x) {
    if (!isPlainObject(x)) return 'changes must be an object';
    var p = checkFields(x, CHANGE_FIELDS, 'changes');
    if (p) return p;
    for (var k in x) { if (Object.prototype.hasOwnProperty.call(x, k)) return null; }
    return 'changes must not be empty';
  }
  function cToolName(x) { return (typeof x === 'string' && Object.prototype.hasOwnProperty.call(TOOL_POINTS, x)) ? null : 'unknown tool'; }

  var IND_FIELDS = {
    key: [true, function (x) { return (typeof x === 'string' && /^[a-z]+(:[A-Za-z0-9.\-]+)*$/.test(x) && x.length <= 64) ? null : 'invalid key'; }],
    kind: [true, cEnum(IND_KINDS)],
    pane: [true, cEnum({ overlay: 1, sub: 1 })],
    outputs: [true, cArray(3, function (x) { return (typeof x === 'string' && /^[a-z]+(:[A-Za-z0-9.\-]+)*$/.test(x) && x.length <= 80) ? null : 'invalid output key'; })],
    label: [true, cPlain(80, CTRL_NAME)],
    note: [true, cNullable(function (x) { return (typeof x === 'string' && x.length <= 300) ? null : 'note too long'; })],
    params: [false, cAny(1000)],
    warmup_bars: [false, function (x) { return (isInt(x) && x >= 0) ? null : 'must be an integer >= 0'; }]
  };
  function cIndicator(x) { return isPlainObject(x) ? checkFields(x, IND_FIELDS, 'indicator') : 'must be an object'; }
  function cTimes(x) {
    if (!Array.isArray(x)) return 'must be an array';
    if (x.length > LIMITS.maxBars) return 'too many items (max ' + LIMITS.maxBars + ')';
    for (var i = 0; i < x.length; i++) {
      if (cEpoch(x[i])) return '[' + i + '] must be epoch ms';
      if (i && x[i] <= x[i - 1]) return '[' + i + '] must be strictly increasing';
    }
    return null;
  }
  var MARKER_FIELDS = {
    id: [true, function (x) { return (typeof x === 'string' && x.length >= 1 && x.length <= 40 && !CTRL_NAME.test(x)) ? null : 'id must be 1-40 plain characters'; }],
    t: [true, cEpoch],
    tEnd: [true, cNullable(cEpoch)],
    kind: [true, cEnum({ insider: 1, fomc: 1, release: 1, fill: 1, period_best: 1, period_worst: 1, drawdown: 1 })],
    side: [true, cNullable(cEnum({ buy: 1, sell: 1, 'short': 1, cover: 1 }))],
    label: [true, function (x) {
      if (typeof x !== 'string' || x.length > LIMITS.maxMarkerLabel) return 'label must be a string of <= 200 chars';
      return CTRL_NAME.test(x) ? 'label: control characters are not allowed' : null;
    }]
  };
  function cMarker(x) {
    if (!isPlainObject(x)) return 'marker must be an object';
    var p = checkFields(x, MARKER_FIELDS, 'marker');
    if (p) return p;
    return (x.tEnd !== null && x.tEnd < x.t) ? 'marker: tEnd < t' : null;
  }
  function cSeriesMap(x) {
    if (!isPlainObject(x)) return 'must be an object';
    var n = 0;
    for (var k in x) {
      if (!Object.prototype.hasOwnProperty.call(x, k)) continue;
      if (++n > LIMITS.maxOutputs) return 'too many series';
      var a = x[k];
      if (!Array.isArray(a)) return k + ' must be an array';
      for (var i = 0; i < a.length; i++) { if (a[i] !== null && !(isFiniteNum(a[i]) && Math.abs(a[i]) <= 1e15)) return k + '[' + i + '] must be a finite number or null'; }
    }
    return null;
  }

  // ------------------------------------------------------------------ handlers
  var HANDLERS = {
    init: {
      fields: {
        theme: [true, cTheme], locale: [true, cLocale], tz: [true, cNullable(cTz)],
        timeMode: [false, cEnum({ local: 1, exchange: 1 })]
      },
      needsEngine: true,
      run: function (m) {
        state.theme = copy(DEFAULT_THEME);
        for (var k in m.theme) { if (Object.prototype.hasOwnProperty.call(m.theme, k)) state.theme[k] = m.theme[k]; }
        if (!m.theme.upDim) state.theme.upDim = withAlpha(state.theme.up, 0.55);
        state.locale = m.locale;
        state.tz = m.tz;
        state.timeMode = m.timeMode || 'local';
        state.initialised = true;
        applyCssTheme();
        engine.applyStyles();
        engine.applyTimezone();
        drawings.onTheme();
        renderLegend();
      }
    },
    loadBars: {
      fields: {
        symbol: [true, cSymbol], tf: [true, cEnum(TFS)], session: [true, cEnum({ rth: 1, eth: 1, utc: 1 })],
        assetClass: [true, cEnum({ equity: 1, crypto: 1 })],
        exchangeTz: [true, cEnum({ 'America/New_York': 1, UTC: 1 })],
        bars: [true, cBars(LIMITS.maxBars)], hasMoreLeft: [true, cBool],
        sipThrough: [true, cNullable(cEpoch)],
        pricePrecision: [false, function (x) { return (isInt(x) && x >= 0 && x <= 10) ? null : 'must be 0..10'; }],
        volumePrecision: [false, function (x) { return (isInt(x) && x >= 0 && x <= 8) ? null : 'must be 0..8'; }]
      },
      needsInit: true, needsEngine: true,
      run: function (m) {
        var list = toEngineBars(m.bars);
        // Release a held needMoreLeft callback first: the engine keeps its "loading" flag until that
        // callback runs, and a reload must not depend on resetData clearing it.
        engine.releasePendingLeft();
        state.loadSeq++;
        state.hover = null;
        var prevSymbol = state.symbol, prevTf = state.tf, prevSession = state.session;
        state.symbol = m.symbol; state.tf = m.tf; state.session = m.session;
        state.assetClass = m.assetClass; state.exchangeTz = m.exchangeTz;
        state.hasMoreLeft = m.hasMoreLeft && list.length > 0;
        state.sipThrough = m.sipThrough;
        var pp = isInt(m.pricePrecision) ? m.pricePrecision : guessPricePrecision(m.bars);
        var vp = isInt(m.volumePrecision) ? m.volumePrecision : (m.assetClass === 'crypto' ? 4 : 0);
        var precisionChanged = pp !== state.pricePrecision || vp !== state.volumePrecision;
        state.pricePrecision = pp; state.volumePrecision = vp;
        state.initList = list;
        state.initServed = false;
        engine.applyTimezone();
        engine.load(prevSymbol !== m.symbol || precisionChanged, prevTf !== m.tf);
        // indicator values are keyed by bar t: another symbol/tf/session must never show old values
        if (prevSymbol !== m.symbol || prevTf !== m.tf || prevSession !== m.session) { indicators.clearValues(); engine.refresh(); }
        drawings.onLoadBars(prevSymbol !== m.symbol);
        if (prevSymbol !== m.symbol) markers.clear();      // markers are per symbol (the host re-sends them)
        renderLegend();
      }
    },
    prependBars: {
      fields: { bars: [true, cBars(LIMITS.maxPrepend)], hasMoreLeft: [true, cBool] },
      needsInit: true, needsEngine: true,
      run: function (m) {
        var list = engine.list();
        var firstT = list.length ? list[0].timestamp : Infinity;
        var older = [];
        for (var i = 0; i < m.bars.length; i++) { if (m.bars[i].t < firstT) older.push(m.bars[i]); }
        var add = toEngineBars(older);
        // An empty answer ends paging for this load (the engine would re-trigger at once).
        state.hasMoreLeft = m.hasMoreLeft && add.length > 0;
        engine.prepend(add);
        renderLegend();
      }
    },
    updateBar: {
      fields: { bar: [true, cBar] },
      needsInit: true, needsEngine: true,
      run: function (m) {
        engine.upsert(toEngineBar(m.bar), false);
        renderLegend();
      }
    },
    replaceBars: {
      fields: { bars: [true, cBars(LIMITS.maxReplace)] },
      needsInit: true, needsEngine: true,
      run: function (m) {
        for (var i = 0; i < m.bars.length; i++) engine.upsert(toEngineBar(m.bars[i]), true);
        renderLegend();
      }
    },
    setType: {
      fields: { chartType: [true, cEnum({ candles: 1, line: 1, area: 1 })] },
      needsInit: true, needsEngine: true,
      run: function (m) { state.chartType = m.chartType; engine.applyStyles(); }
    },
    setLastPrice: {
      fields: {
        price: [true, cNullable(cPrice)],
        dayChangeSign: [true, cNullable(function (x) { return (x === -1 || x === 0 || x === 1) ? null : 'must be -1, 0, 1 or null'; })]
      },
      needsInit: true, needsEngine: true,
      run: function (m) { state.lastPrice = m.price; state.dayChangeSign = m.dayChangeSign; engine.applyStyles(); }
    },
    // ---- MR5 (bridge revision 2): indicators
    setIndicators: {
      fields: {
        indicators: [true, cArray(LIMITS.maxIndicators, cIndicator)], t: [true, cTimes],
        series: [true, cSeriesMap], volume: [false, cBool]
      },
      needsInit: true, needsEngine: true,
      run: function (m) {
        var p = indicators.checkSet(m);
        if (p) { reportError('bad_message', 'setIndicators: ' + p, 'setIndicators'); return; }
        indicators.set(m);
        renderLegend();
      }
    },
    // ---- MR8 (bridge revision 4): time markers
    setMarkers: {
      fields: { markers: [true, cArray(LIMITS.maxMarkers, cMarker)] },
      needsInit: true, needsEngine: true,
      run: function (m) {
        var seen = {};
        for (var i = 0; i < m.markers.length; i++) {
          if (seen[m.markers[i].id]) { reportError('bad_message', 'setMarkers: duplicate id ' + m.markers[i].id.slice(0, 40), 'setMarkers'); return; }
          seen[m.markers[i].id] = 1;
        }
        markers.set(m.markers);
      }
    },
    updateIndicators: {
      fields: { t: [true, cTimes], series: [true, cSeriesMap] },
      needsInit: true, needsEngine: true,
      run: function (m) {
        var p = indicators.checkUpdate(m);
        if (p) { reportError('bad_message', 'updateIndicators: ' + p, 'updateIndicators'); return; }
        indicators.merge(m.t, m.series);
        engine.refresh();
      }
    },
    // ---- MR5: drawings
    loadDrawings: {
      fields: { drawings: [true, cArray(LIMITS.maxDrawings, cDrawing)], append: [false, cBool] },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.load(m.drawings, !!m.append); }
    },
    upsertDrawing: {
      fields: { drawing: [true, cDrawing] },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.upsert(m.drawing); }
    },
    drawingSaved: {
      fields: { localId: [true, cLocalId], id: [true, cHex32], drawing: [true, cDrawing] },
      needsInit: true, needsEngine: true,
      run: function (m) {
        if (m.drawing.id !== m.id) { reportError('bad_message', 'drawingSaved: drawing.id differs from id.', 'drawingSaved'); return; }
        drawings.saved(m.localId, m.id, m.drawing);
      }
    },
    drawingRejected: {
      fields: {
        op: [true, cEnum({ create: 1, change: 1, 'delete': 1 })], localId: [true, cNullable(cLocalId)],
        id: [true, cNullable(cHex32)], message: [true, function (x) { return (typeof x === 'string' && x.length <= 300) ? null : 'message must be <= 300 chars'; }]
      },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.rejected(m.op, m.localId, m.id); }
    },
    editDrawing: {
      fields: { id: [true, cHex32], changes: [true, cChanges] },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.edit(m.id, m.changes); }
    },
    removeDrawing: {
      fields: { id: [true, cHex32] },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.removeById(m.id); }
    },
    selectDrawing: {
      fields: { id: [true, cNullable(cHex32)] },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.selectById(m.id, false); }
    },
    jumpToDrawing: {
      fields: { id: [true, cHex32] },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.selectById(m.id, true); }
    },
    setTool: {
      fields: { name: [true, cNullable(cToolName)], style: [false, cStylePartial] },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.setTool(m.name, m.style || null, false); }
    },
    setMagnet: {
      fields: { mode: [true, cEnum({ off: 1, weak: 1, strong: 1 })] },
      needsInit: true, needsEngine: true,
      run: function (m) { drawings.setMagnet(m.mode); }
    },
    command: {
      fields: { name: [true, cEnum({ undo: 1, redo: 1, lock: 1, lockAll: 1, unlockAll: 1, hideAll: 1, showAll: 1, deleteSelected: 1, resetView: 1, cancel: 1, saveMeasure: 1 })] },
      needsInit: true, needsEngine: true,
      run: function (m) {
        if (m.name === 'resetView') engine.resetView();
        else drawings.command(m.name);
      }
    }
  };

  // ------------------------------------------------------------------ data helpers
  function toEngineBar(b) {
    return {
      timestamp: b.t, open: b.o, high: b.h, low: b.l, close: b.c,
      volume: b.v === null ? 0 : b.v, volumeMissing: b.v === null,
      feed: b.feed, final: b.final, s: b.s || null
    };
  }
  function toEngineBars(bars) { var r = new Array(bars.length); for (var i = 0; i < bars.length; i++) r[i] = toEngineBar(bars[i]); return r; }
  function fromEngineBar(k) {
    if (!k) return null;
    var r = { t: k.timestamp, o: k.open, h: k.high, l: k.low, c: k.close, v: k.volumeMissing ? null : k.volume, feed: k.feed, final: !!k.final };
    if (k.s) r.s = k.s;
    return r;
  }
  function findIndex(list, t) {
    var lo = 0, hi = list.length - 1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1, mt = list[mid].timestamp;
      if (mt === t) return mid;
      if (mt < t) lo = mid + 1; else hi = mid - 1;
    }
    return -(lo + 1);   // insertion point encoded
  }
  function guessPricePrecision(bars) {
    var maxDec = 0, n = Math.min(bars.length, 300);
    for (var i = bars.length - n; i < bars.length; i++) {
      var d = decimals(bars[i].c);
      if (d > maxDec) maxDec = d;
    }
    var last = bars.length ? bars[bars.length - 1].c : 100;
    var min = last < 1 ? 4 : 2;
    return Math.max(min, Math.min(maxDec, last < 1 ? 8 : 4));
  }
  function decimals(x) {
    for (var d = 0; d <= 8; d++) { if (Math.abs(Math.round(x * Math.pow(10, d)) - x * Math.pow(10, d)) < 1e-6) return d; }
    return 8;
  }
  function withAlpha(hex, a) {
    var aa = Math.round(Math.max(0, Math.min(1, a)) * 255).toString(16);
    if (aa.length < 2) aa = '0' + aa;
    return hex + aa;
  }
  function displayTz() {
    if (state.timeMode === 'exchange' && state.exchangeTz) return state.exchangeTz;
    if (state.tz) return state.tz;
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) { return 'UTC'; }
  }
  function signColor(sign) {
    if (sign === 1) return state.theme.up;
    if (sign === -1) return state.theme.down;
    return state.theme.muted;
  }

  // ------------------------------------------------------------------ engine adapter (KLineChart 10.x)
  var engine = {
    kc: null,
    chart: null,
    volPaneId: null,

    start: function () {
      var kc = window.klinecharts;
      if (!kc || typeof kc.init !== 'function') {
        fatal('engine_missing', 'Chart engine (vendor/klinecharts.min.js) is missing or failed to load.');
        return false;
      }
      var missing = REQUIRED_KC.filter(function (f) { return typeof kc[f] !== 'function'; });
      if (missing.length) { fatal('engine_api', 'KLineChart API missing: klinecharts.' + missing.join(', klinecharts.')); return false; }
      var ver = String(kc.version());
      if (!/^10\./.test(ver)) {
        fatal('engine_version', 'Chart engine version "' + ver.slice(0, 32) + '" is not KLineChart 10.x.');
        return false;
      }
      this.kc = kc;
      kc.registerIndicator(sessionShadeIndicator());
      kc.registerIndicator(tailVeilIndicator('UFOS_TAIL', true));
      kc.registerIndicator(tailVeilIndicator('UFOS_TAIL_VOL', false));
      kc.registerIndicator(markerBandsIndicator());
      kc.registerIndicator(markerPointsIndicator());
      // No styles in init: the engine defaults are read first to check every style key we set.
      var chart = kc.init(document.getElementById('chart'), { locale: 'en-US', timezone: displayTz() });
      if (!chart) { fatal('engine_init', 'Chart engine could not be initialised.'); return false; }
      missing = REQUIRED_CHART.filter(function (f) { return typeof chart[f] !== 'function'; });
      if (missing.length) { fatal('engine_api', 'KLineChart API missing: chart.' + missing.join(', chart.')); return false; }
      this.chart = chart;
      var unknown = unknownStyleKeys(chart.getStyles(), this.styles());
      if (unknown.length) apiProblem('style keys unknown to the engine: ' + unknown.join(', '));
      chart.setStyles(this.styles());

      // Indicators: documented form createIndicator(value, isStack, paneOptions); `paneId` is
      // also set in the value object for engines that read it there. The return value may be an
      // indicator id (v10) or a pane id (v9) -> the pane is resolved by name, not by the return.
      this.createVolume();
      createOnPane(chart, 'UFOS_SESSION', CANDLE_PANE);
      createOnPane(chart, 'UFOS_TAIL', CANDLE_PANE);
      createOnPane(chart, 'UFOS_MARK_BANDS', CANDLE_PANE);
      createOnPane(chart, 'UFOS_MARK_POINTS', CANDLE_PANE);
      drawings.start(kc, chart);

      chart.subscribeAction('onCrosshairChange', onCrosshair);
      chart.setDataLoader({
        getBars: function (p) { engine.getBars(p); },
        subscribeBar: function (p) { state.liveCb = p && typeof p.callback === 'function' ? p.callback : null; },
        unsubscribeBar: function () { state.liveCb = null; }
      });
      if (typeof ResizeObserver === 'function') {
        new ResizeObserver(function () { if (engine.chart) engine.chart.resize(); }).observe(document.getElementById('chart'));
      }
      return true;
    },

    createVolume: function () {
      var chart = this.chart;
      var volRet = chart.createIndicator({ name: 'VOL', calcParams: [], paneId: VOL_PANE }, false,
        { id: VOL_PANE, height: 84, minHeight: 40 });
      if (volRet === null || volRet === undefined) apiProblem('createIndicator(VOL) returned nothing');
      this.volPaneId = indicatorPane(chart, 'VOL', volRet, VOL_PANE);
      if (!this.volPaneId) { apiProblem('volume pane id not found (createIndicator returned ' + String(volRet).slice(0, 40) + ')'); return; }
      if (this.volPaneId !== VOL_PANE && typeof chart.setPaneOptions === 'function') {
        chart.setPaneOptions({ id: this.volPaneId, height: 84, minHeight: 40 });
      }
      createOnPane(chart, 'UFOS_TAIL_VOL', this.volPaneId, true, { id: this.volPaneId });
    },

    releasePendingLeft: function () {
      var pending = state.pendingLeft;
      state.pendingLeft = null;
      if (pending) {
        try { pending.cb([], { forward: false, backward: false }); } catch (e) { apiProblem('releasing the paging callback failed (' + (e && e.name) + ')'); }
      }
    },

    version: function () { return this.kc ? String(this.kc.version()).slice(0, 32) : ''; },

    list: function () { return this.chart ? this.chart.getDataList() : []; },

    getBars: function (p) {
      if (!p || typeof p.callback !== 'function') { apiProblem('getBars called without a callback'); return; }
      if (p.type === 'init') {
        state.initServed = true;
        var list = state.initList ? state.initList.slice() : [];
        p.callback(list, { forward: state.hasMoreLeft, backward: false });
        return;
      }
      if (p.type === 'forward') {            // KLineChart 10: 'forward' = OLDER bars (left)
        if (!state.hasMoreLeft) { p.callback([], { forward: false, backward: false }); return; }
        var cur = this.list();
        var before = (isInt(p.timestamp) ? p.timestamp : null) || (cur.length ? cur[0].timestamp : null);
        if (before === null) { p.callback([], { forward: false, backward: false }); return; }
        // At most one request in flight: a repeated engine request replaces the held callback
        // (the older one is answered empty, keeping paging possible) without a second needMoreLeft.
        var older = state.pendingLeft;
        state.pendingLeft = { cb: p.callback, seq: state.loadSeq };
        if (older) {
          try { older.cb([], { forward: true, backward: false }); } catch (e) { apiProblem('answering a repeated paging request failed'); }
          return;
        }
        send({ type: 'needMoreLeft', beforeTs: before });
        return;
      }
      p.callback([], { forward: state.hasMoreLeft, backward: false });   // 'backward' = newer: never paged
    },

    load: function (symbolChanged, periodChanged) {
      var c = this.chart;
      state.liveCb = null;
      if (symbolChanged || !c.getSymbol()) {
        c.setSymbol({ ticker: state.symbol, pricePrecision: state.pricePrecision, volumePrecision: state.volumePrecision });
      }
      if (periodChanged || !c.getPeriod()) {
        c.setPeriod(TFS[state.tf]);
      }
      // setSymbol/setPeriod reload through getBars('init'); if neither did (unchanged, or the
      // engine skipped an identical value) reload explicitly.
      if (!state.initServed) c.resetData();
      // initList stays until the next loadBars: an engine that loads 'init' later still gets it.
      c.scrollToRealTime();
      var seq = state.loadSeq, expected = state.initList ? state.initList.length : 0;
      setTimeout(function () {
        if (seq !== state.loadSeq) return;
        if (!state.initServed) apiProblem('the engine did not request the loaded bars (getBars init)');
        else if (engine.list().length < expected) apiProblem('the engine holds ' + engine.list().length + ' of ' + expected + ' loaded bars');
        else if (expected && !state.liveCb) apiProblem('subscribeBar was not called; live updates cannot be drawn');
      }, 400);
    },

    prepend: function (add) {
      var pending = state.pendingLeft;
      state.pendingLeft = null;
      if (pending) {
        pending.cb(add, { forward: state.hasMoreLeft, backward: false });
        return;
      }
      if (!add.length) return;
      // Unsolicited prepend: insert in place (engine list is the live array), then refresh.
      var list = this.list();
      Array.prototype.unshift.apply(list, add);
      this.refresh();
    },

    upsert: function (bar, insertMissing) {
      var list = this.list();
      var n = list.length;
      // Live-candle updates (same t as the last bar) are applied in place and redrawn once per
      // frame via refresh(): the real KLineChart 10 recalculates + lays out on every
      // updateData, ~9 ms each (200 updates took 1.8 s headless, 2026-10-18). A new candle
      // (t > last) still goes to the engine immediately.
      if (n && state.liveCb && bar.timestamp === list[n - 1].timestamp) {
        var last = list[n - 1];
        for (var lk in bar) { if (Object.prototype.hasOwnProperty.call(bar, lk)) last[lk] = bar[lk]; }
        this.refresh();
        return;
      }
      if (!n || bar.timestamp >= list[n - 1].timestamp) {
        if (state.liveCb) { state.liveCb(bar); }
        else { list.push(bar); this.refresh(); }
        return;
      }
      var idx = findIndex(list, bar.timestamp);
      if (idx >= 0) {
        var target = list[idx];
        for (var k in bar) { if (Object.prototype.hasOwnProperty.call(bar, k)) target[k] = bar[k]; }
        this.refresh();
      } else if (insertMissing) {
        list.splice(-idx - 1, 0, bar);
        this.refresh();
      }
      // updateBar for an older t that is not loaded: ignored (contract).
    },

    // In-place edits of older bars: recalc + redraw once per frame by re-sending the last bar
    // (same timestamp -> the engine replaces it with itself, recalculates indicators, lays out).
    refresh: function () {
      if (state.refreshQueued) return;
      state.refreshQueued = true;
      requestAnimationFrame(function () {
        state.refreshQueued = false;
        var list = engine.list();
        if (list.length && state.liveCb) state.liveCb(list[list.length - 1]);
        renderLegend();
      });
    },

    resetView: function () {
      if (!this.chart) return;
      this.chart.setBarSpace(8);
      this.chart.scrollToRealTime();
    },

    applyTimezone: function () { if (this.chart) this.chart.setTimezone(displayTz()); },

    applyStyles: function () { if (this.chart) this.chart.setStyles(this.styles()); },

    styles: function () {
      var t = state.theme;
      var font = t.fontFamily;
      var lastColor = signColor(state.dayChangeSign);
      var downDim = t.downDim || withAlpha(t.down, 0.55);
      var upDim = t.upDim || withAlpha(t.up, 0.55);
      var type = state.chartType === 'candles' ? 'candle_solid' : 'area';
      var areaFill = state.chartType === 'area'
        ? [{ offset: 0, color: withAlpha(t.accent, 0.28) }, { offset: 1, color: withAlpha(t.accent, 0.0) }]
        : [{ offset: 0, color: withAlpha(t.accent, 0.0) }, { offset: 1, color: withAlpha(t.accent, 0.0) }];
      var axisText = { show: true, color: t.axisText, size: 11, family: font, weight: 'normal' };
      var crossText = {
        show: true, color: t.text, size: 11, family: font, weight: 'normal',
        backgroundColor: t.grid, borderColor: t.muted, borderSize: 1, borderRadius: 2,
        paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2
      };
      return {
        grid: {
          show: true,
          horizontal: { show: true, size: 1, color: t.grid, style: 'solid' },
          vertical: { show: true, size: 1, color: t.grid, style: 'solid' }
        },
        candle: {
          type: type,
          bar: {
            upColor: t.up, downColor: t.down, noChangeColor: t.muted,
            upBorderColor: t.up, downBorderColor: t.down, noChangeBorderColor: t.muted,
            upWickColor: t.up, downWickColor: t.down, noChangeWickColor: t.muted
          },
          area: { lineSize: 1.5, lineColor: t.accent, smooth: false, value: 'close', backgroundColor: areaFill },
          priceMark: {
            show: true,
            high: { show: true, color: t.axisText, textSize: 10, textFamily: font },
            low: { show: true, color: t.axisText, textSize: 10, textFamily: font },
            last: {
              show: state.lastPrice !== null,
              upColor: lastColor, downColor: lastColor, noChangeColor: lastColor,
              line: { show: true, style: 'dashed', dashedValue: [4, 3], size: 1 },
              text: {
                show: true, style: 'fill', size: 11, family: font, weight: 'normal', color: t.background,
                borderRadius: 2, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2
              }
            }
          },
          tooltip: { showRule: 'none' }
        },
        indicator: {
          bars: [{ upColor: upDim, downColor: downDim, noChangeColor: withAlpha(t.muted, 0.55) }],
          lastValueMark: { show: false },
          tooltip: { showRule: 'none' }
        },
        xAxis: { axisLine: { show: true, color: t.grid }, tickLine: { show: true, color: t.grid }, tickText: axisText },
        yAxis: { axisLine: { show: true, color: t.grid }, tickLine: { show: true, color: t.grid }, tickText: axisText },
        separator: { size: 1, color: t.grid, fill: true, activeBackgroundColor: withAlpha(t.accent, 0.15) },
        crosshair: {
          show: true,
          horizontal: { show: true, line: { show: true, style: 'dashed', dashedValue: [4, 3], size: 1, color: t.muted }, text: crossText },
          vertical: { show: true, line: { show: true, style: 'dashed', dashedValue: [4, 3], size: 1, color: t.muted }, text: crossText }
        }
      };
    }
  };

  var apiReported = Object.create(null);
  function apiProblem(detail) {          // reported once per page life (draw paths run every frame)
    if (apiReported[detail]) return;
    apiReported[detail] = true;
    reportError('engine_api', 'Chart engine API mismatch: ' + detail, 'api');
  }
  function isObjLike(o) { return o !== null && typeof o === 'object' && !Array.isArray(o); }
  // Paths of style keys we set that do not exist in the engine's default styles (non-fatal:
  // the engine would ignore them and the look would silently differ).
  function unknownStyleKeys(defaults, ours) {
    var out = [];
    (function walk(d, o, path) {
      for (var k in o) {
        if (!Object.prototype.hasOwnProperty.call(o, k)) continue;
        var p = path ? path + '.' + k : k;
        if (!isObjLike(d) || !Object.prototype.hasOwnProperty.call(d, k)) { out.push(p); continue; }
        if (isObjLike(o[k]) && isObjLike(d[k])) walk(d[k], o[k], p);
      }
    })(defaults, ours, '');
    return out.slice(0, 12);
  }
  function indicatorList(chart, name) {
    if (typeof chart.getIndicators !== 'function') return [];
    var r;
    try { r = chart.getIndicators({ name: name }); } catch (e) { return []; }
    var arr = [];
    var visit = function (x) {
      if (!x) return;
      if (Array.isArray(x)) { x.forEach(visit); return; }
      if (typeof x.forEach === 'function' && typeof x.get === 'function') { x.forEach(function (v) { visit(v); }); return; } // Map (v9)
      if (typeof x === 'object' && typeof x.name === 'string') arr.push(x);
    };
    visit(r);
    return arr.filter(function (i) { return i.name === name; });
  }
  function indicatorPane(chart, name, ret, wanted) {
    var found = indicatorList(chart, name);
    for (var i = 0; i < found.length; i++) { if (typeof found[i].paneId === 'string') return found[i].paneId; }
    if (ret === wanted) return wanted;      // engines that return the pane id
    return null;
  }
  function createOnPane(chart, name, paneId, isStack, paneOpts) {
    var ret = chart.createIndicator({ name: name, paneId: paneId }, isStack !== false, paneOpts || { id: paneId });
    if (ret === null || ret === undefined) { apiProblem('createIndicator(' + name + ') on ' + paneId + ' failed'); return; }
    var at = indicatorPane(chart, name, ret, paneId);
    if (at && at !== paneId) apiProblem(name + ' landed on pane ' + String(at).slice(0, 30) + ' instead of ' + paneId);
  }
  function drawParamsOk(p, name) {
    if (p && p.ctx && p.chart && p.bounding && p.xAxis && typeof p.xAxis.convertToPixel === 'function') return true;
    apiProblem('indicator draw params unexpected (' + name + ')');
    return false;
  }

  // Custom indicators (drawn with the engine's canvas; no data of their own).
  function visibleIndexRange(chart, len) {
    var vr = chart.getVisibleRange();
    var from = Math.max(0, Math.floor(vr.from) - 1), to = Math.min(len, Math.ceil(vr.to) + 1);
    return [from, to];
  }
  function halfBarWidth(chart) {
    var bs = chart.getBarSpace();              // v10: {bar, halfBar, gapBar, halfGapBar}; v9: number
    var full = (bs && typeof bs === 'object') ? bs.bar : bs;
    if (!isFiniteNum(full) || full <= 0) { apiProblem('getBarSpace() returned no bar width'); full = 8; }
    return full / 2;
  }
  function emptyCalc(dataList) { var r = new Array(dataList.length); for (var i = 0; i < r.length; i++) r[i] = {}; return r; }

  function sessionShadeIndicator() {
    return {
      name: 'UFOS_SESSION', shortName: '', series: 'price', zLevel: -1, figures: [],
      calc: emptyCalc,
      draw: function (p) {
        if (!drawParamsOk(p, 'UFOS_SESSION')) return true;
        var chart = p.chart, list = chart.getDataList();
        if (!list.length) return true;
        var r = visibleIndexRange(chart, list.length), half = halfBarWidth(chart);
        var ctx = p.ctx, h = p.bounding.height;
        ctx.fillStyle = withAlpha(state.theme.text, 0.06);
        var runStart = -1;
        for (var i = r[0]; i <= r[1]; i++) {
          var ext = i < r[1] && (list[i].s === 'pre' || list[i].s === 'post');
          if (ext && runStart < 0) runStart = i;
          if (!ext && runStart >= 0) {
            var x1 = p.xAxis.convertToPixel(runStart) - half, x2 = p.xAxis.convertToPixel(i - 1) + half;
            ctx.fillRect(x1, 0, x2 - x1, h);
            runStart = -1;
          }
        }
        return true;
      }
    };
  }

  // isMain: price pane (veil + marker + label); otherwise the volume pane (veil only). Two
  // templates instead of reading indicator.paneId inside draw.
  function tailVeilIndicator(name, isMain) {
    return {
      name: name, shortName: '', series: isMain ? 'price' : 'volume', zLevel: 1, figures: [],
      calc: emptyCalc,
      draw: function (p) {
        if (!drawParamsOk(p, name)) return true;
        var chart = p.chart, list = chart.getDataList();
        if (!list.length) return true;
        var r = visibleIndexRange(chart, list.length), half = halfBarWidth(chart);
        var ctx = p.ctx, h = p.bounding.height, w = p.bounding.width;
        var runStart = -1, lastRun = null;
        ctx.fillStyle = withAlpha(state.theme.background, 0.55);
        for (var i = r[0]; i <= r[1]; i++) {
          var tail = i < r[1] && list[i].feed === 'iex_tail';
          if (tail && runStart < 0) runStart = i;
          if (!tail && runStart >= 0) {
            var x1 = p.xAxis.convertToPixel(runStart) - half, x2 = p.xAxis.convertToPixel(i - 1) + half;
            ctx.fillRect(x1, 0, x2 - x1, h);
            lastRun = [x1, x2];
            runStart = -1;
          }
        }
        if (isMain && lastRun) {
          var lx = Math.max(0, lastRun[0]);
          ctx.strokeStyle = withAlpha(state.theme.caution, 0.6);
          ctx.lineWidth = 1;
          ctx.setLineDash([3, 3]);
          ctx.beginPath(); ctx.moveTo(Math.round(lx) + 0.5, 0); ctx.lineTo(Math.round(lx) + 0.5, h); ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = '10px ' + state.theme.fontFamily;
          ctx.fillStyle = withAlpha(state.theme.caution, 0.9);
          ctx.textBaseline = 'top';
          var tw = ctx.measureText(TAIL_LABEL).width;
          var tx = lx + 4;
          if (tx + tw > w - 4) { tx = lx - 4 - tw; }
          ctx.fillText(TAIL_LABEL, Math.max(2, tx), 40);
        }
        return true;
      }
    };
  }

  // ================================================================== MR5: indicators (backend series)
  // Q8: the page never computes an indicator; it plots the backend's series, aligned by bar `t`.
  var IND_COLORS = ['accent', 'violet', 'caution', 'blue', 'text', 'up', 'down', 'muted'];
  var SUB_PANE_PREFIX = 'ufos_sub_';

  function outputRole(kind, out) {
    if (kind === 'bb') return /:mid$/.test(out) ? 'line' : 'band';
    if (kind === 'macd') return /:hist$/.test(out) ? 'hist' : (/:signal$/.test(out) ? 'signal' : 'line');
    if (kind === 'cmp') return /:price$/.test(out) ? 'line' : 'legend';      // pct shown in the legend only
    return 'line';
  }

  var indicators = {
    defs: [],
    values: {},          // output key -> Map(t -> value)
    volume: true,

    outputs: function () { var o = {}; this.defs.forEach(function (d) { d.outputs.forEach(function (k) { o[k] = 1; }); }); return o; },

    checkSet: function (m) {
      var keys = {}, outs = {}, subs = 0, i, k;
      for (i = 0; i < m.indicators.length; i++) {
        var ind = m.indicators[i];
        if (keys[ind.key]) return 'duplicate indicator ' + ind.key;
        keys[ind.key] = 1;
        if (ind.pane === 'sub') subs++;
        if (!ind.outputs.length) return ind.key + ' has no outputs';
        for (var j = 0; j < ind.outputs.length; j++) {
          if (outs[ind.outputs[j]]) return 'duplicate output ' + ind.outputs[j];
          outs[ind.outputs[j]] = 1;
        }
      }
      if (subs > LIMITS.maxSubPanes) return 'at most ' + LIMITS.maxSubPanes + ' sub-pane indicators';
      for (k in outs) { if (!Object.prototype.hasOwnProperty.call(m.series, k)) return 'series ' + k + ' missing'; }
      for (k in m.series) {
        if (!Object.prototype.hasOwnProperty.call(m.series, k)) continue;
        if (!outs[k]) return 'series ' + k + ' is not an output of the indicators';
        if (m.series[k].length !== m.t.length) return 'series ' + k + ' length differs from t';
      }
      return null;
    },

    checkUpdate: function (m) {
      var outs = this.outputs();
      for (var k in m.series) {
        if (!Object.prototype.hasOwnProperty.call(m.series, k)) continue;
        if (!outs[k]) return 'series ' + k + ' is not part of the current indicators';
        if (m.series[k].length !== m.t.length) return 'series ' + k + ' length differs from t';
      }
      return null;
    },

    set: function (m) {
      this.removeAll();
      this.values = {};
      this.defs = [];
      var self = this, sub = 0;
      m.indicators.forEach(function (ind, i) {
        var def = {
          key: ind.key, kind: ind.kind, pane: ind.pane, outputs: ind.outputs.slice(), label: ind.label, note: ind.note,
          color: IND_COLORS[i % IND_COLORS.length],
          name: 'UFOS_I_' + ind.key.replace(/[^A-Za-z0-9]/g, '_'),
          paneId: ind.pane === 'overlay' ? CANDLE_PANE : SUB_PANE_PREFIX + (sub++)
        };
        def.outputs.forEach(function (o) { self.values[o] = new Map(); });
        self.defs.push(def);
      });
      this.merge(m.t, m.series);
      this.setVolume(m.volume !== false);
      this.defs.forEach(function (def) { self.create(def); });
      engine.refresh();
    },

    merge: function (t, series) {
      for (var k in series) {
        if (!Object.prototype.hasOwnProperty.call(series, k)) continue;
        var map = this.values[k], a = series[k];
        if (!map) continue;
        for (var i = 0; i < a.length; i++) { if (a[i] === null) map.delete(t[i]); else map.set(t[i], a[i]); }
      }
    },

    clearValues: function () { for (var k in this.values) { if (Object.prototype.hasOwnProperty.call(this.values, k)) this.values[k] = new Map(); } },

    template: function (def) {
      var self = this;
      var plotted = def.outputs.filter(function (o) { return outputRole(def.kind, o) !== 'legend'; });
      var figures = plotted.map(function (o, i) {
        var role = outputRole(def.kind, o), fkey = 'v' + i;
        var f = { key: fkey, title: o + ': ', type: role === 'hist' ? 'bar' : 'line' };
        if (role === 'hist') {
          f.baseValue = 0;
          f.styles = function (data) {
            var cur = data && data.current ? data.current : null;
            var v = cur && isFiniteNum(cur[fkey]) ? cur[fkey] : 0;
            return { color: v >= 0 ? withAlpha(state.theme.up, 0.6) : withAlpha(state.theme.down, 0.6), style: 'fill' };
          };
        } else {
          f.styles = function () {
            var col = paletteColor(role === 'signal' ? (def.color === 'caution' ? 'blue' : 'caution') : def.color);
            return { color: col, size: role === 'band' ? 1 : 1.5, style: role === 'band' ? 'dashed' : 'solid', dashedValue: [4, 3] };
          };
        }
        return f;
      });
      if (def.kind === 'rsi') {
        ['g30', 'g70'].forEach(function (g) {
          figures.push({ key: g, title: '', type: 'line', styles: function () { return { color: withAlpha(state.theme.muted, 0.8), size: 1, style: 'dashed', dashedValue: [3, 3] }; } });
        });
      }
      var tpl = {
        name: def.name, shortName: def.label, series: def.pane === 'overlay' ? 'price' : 'normal',
        precision: def.pane === 'overlay' ? state.pricePrecision : 2, shouldOhlc: false, figures: figures,
        calc: function (dataList) {
          var maps = plotted.map(function (o) { return self.values[o]; });
          var out = new Array(dataList.length);
          for (var i = 0; i < dataList.length; i++) {
            var r = {}, t = dataList[i].timestamp;
            for (var j = 0; j < maps.length; j++) {
              var v = maps[j] ? maps[j].get(t) : undefined;
              if (v !== undefined) r['v' + j] = v;
            }
            if (def.kind === 'rsi') { r.g30 = 30; r.g70 = 70; }
            out[i] = r;
          }
          return out;
        }
      };
      if (def.kind === 'rsi') { tpl.minValue = 0; tpl.maxValue = 100; }
      return tpl;
    },

    create: function (def) {
      var c = engine.chart;
      engine.kc.registerIndicator(this.template(def));
      if (def.pane === 'overlay') createOnPane(c, def.name, CANDLE_PANE, true, { id: CANDLE_PANE });
      else createOnPane(c, def.name, def.paneId, false, { id: def.paneId, height: 90, minHeight: 50 });
    },

    removeAll: function () {
      var self = this;
      this.defs.forEach(function (def) { removeIndicatorCompat(def.name, def.paneId); });
    },

    setVolume: function (on) {
      this.volume = on;
      if (on && !engine.volPaneId) engine.createVolume();
      else if (!on && engine.volPaneId) {
        removeIndicatorCompat('UFOS_TAIL_VOL', engine.volPaneId);
        removeIndicatorCompat('VOL', engine.volPaneId);
        engine.volPaneId = null;
      }
    },

    valueAt: function (out, t) { var m = this.values[out]; return m ? m.get(t) : undefined; }
  };

  function removeIndicatorCompat(name, paneId) {
    var c = engine.chart;
    if (typeof c.removeIndicator !== 'function') { apiProblem('chart.removeIndicator missing'); return; }
    try { c.removeIndicator({ name: name, paneId: paneId }); } catch (e) { /* v9 signature below */ }
    if (typeof c.getIndicators === 'function' && indicatorList(c, name).length) {
      try { c.removeIndicator(paneId, name); } catch (e2) { /* reported below */ }
      if (indicatorList(c, name).length) apiProblem('removeIndicator(' + name + ') had no effect');
    }
  }

  // ================================================================== MR5: drawings
  var MAGNET_MODE = { off: 'normal', weak: 'weak_magnet', strong: 'strong_magnet' };
  var CUSTOM_OVERLAY = {
    fibonacciLine: 'ufosFib', rect: 'ufosRect', measure: 'ufosMeasure', arrowUp: 'ufosArrowUp',
    arrowDown: 'ufosArrowDown', brush: 'ufosBrush', alertLine: 'ufosAlertLine'
  };
  var BUILTIN_OVERLAYS = ['segment', 'rayLine', 'straightLine', 'horizontalStraightLine', 'horizontalRayLine',
    'verticalStraightLine', 'priceLine', 'parallelStraightLine', 'priceChannelLine', 'simpleAnnotation', 'simpleTag'];
  var DEFAULT_STYLE = { color: 'accent', width: 1, dash: 'solid', levels: null };
  var FIB_DEFAULT = [0, 23.6, 38.2, 50, 61.8, 78.6, 100];
  var FIXED_COLORS = { blue: '#4C8DFF', violet: '#B48CF2' };   // bridge rev 2: fixed page colours
  var HISTORY_MAX = 50;
  var SHORTCUT_TOOLS = { KeyT: 'segment', KeyH: 'horizontalStraightLine', KeyF: 'fibonacciLine', KeyR: 'rect' };
  var BODY_KEYS = ['points', 'style', 'name', 'text', 'locked', 'visible', 'timeframes', 'alert_id'];

  function overlayName(tool) { return CUSTOM_OVERLAY[tool] || tool; }
  function paletteColor(key) {
    if (FIXED_COLORS[key]) return FIXED_COLORS[key];
    var t = state.theme;
    return { accent: t.accent, up: t.up, down: t.down, caution: t.caution, text: t.text, muted: t.muted }[key] || t.accent;
  }
  function bodyOf(src) {
    return {
      tool: src.tool,
      points: src.points.map(function (q) { return { t: q.t, p: q.p }; }),
      style: { color: src.style.color, width: src.style.width, dash: src.style.dash, levels: src.style.levels ? src.style.levels.slice() : null },
      name: src.name, text: src.text, locked: src.locked, visible: src.visible,
      timeframes: Array.isArray(src.timeframes) ? src.timeframes.slice() : 'all',
      alert_id: src.alert_id
    };
  }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function diffBodies(from, to) {
    var changes = {};
    BODY_KEYS.forEach(function (k) { if (!same(from[k], to[k])) changes[k] = JSON.parse(JSON.stringify(to[k])); });
    return changes;
  }
  function uuid4() {                              // RFC 4122 v4 from crypto.getRandomValues
    var b = new Uint8Array(16);
    window.crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = '';
    for (var i = 0; i < 16; i++) h += (b[i] + 256).toString(16).slice(1);
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }
  function periodMs() {
    var p = TFS[state.tf] || TFS['1m'];
    var unit = { minute: 60000, hour: 3600000, day: 86400000, week: 604800000 }[p.type];
    return unit * p.span;
  }
  function timestampFromIndex(i) {
    var list = engine.list(), n = list.length;
    if (!n || !isFiniteNum(i)) return null;
    var r = Math.round(i);
    if (r >= 0 && r < n) return list[r].timestamp;
    if (r >= n) return list[n - 1].timestamp + (r - n + 1) * periodMs();
    return list[0].timestamp + r * periodMs();
  }
  function roundPrice(v) { return Math.round(v * 1e8) / 1e8; }
  function formatPrice(v) { return isFiniteNum(v) ? v.toFixed(state.pricePrecision) : ''; }
  function formatDuration(ms) {
    var m = Math.round(Math.abs(ms) / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
    return (d ? d + 'd ' : '') + (h || d ? h + 'h ' : '') + mm + 'm';
  }
  function extOf(o) { return (o && o.extendData && typeof o.extendData === 'object') ? o.extendData : {}; }
  function figLine(x) { return { color: x.color, size: x.width || 1, style: x.dash === 'dashed' ? 'dashed' : 'solid', dashedValue: [6, 4] }; }
  function figText(color) {
    return { color: color, size: 11, family: state.theme.fontFamily, weight: 'normal', style: 'fill',
      backgroundColor: withAlpha(state.theme.background, 0.85), borderColor: 'transparent', borderSize: 0, borderRadius: 2,
      paddingLeft: 3, paddingRight: 3, paddingTop: 2, paddingBottom: 2 };
  }

  // Custom overlays: all geometry from `coordinates` (+ chart.convertToPixel for the brush path).
  function customOverlays() {
    return [
      {
        name: 'ufosRect', totalStep: 3, needDefaultPointFigure: true,
        createPointFigures: function (p) {
          var c = p.coordinates || [], x = extOf(p.overlay);
          if (c.length < 2) return [];
          return [{ type: 'polygon', attrs: { coordinates: [c[0], { x: c[1].x, y: c[0].y }, c[1], { x: c[0].x, y: c[1].y }] },
            styles: { style: 'stroke_fill', color: withAlpha(x.color || state.theme.accent, 0.12), borderColor: x.color, borderSize: x.width || 1,
              borderStyle: x.dash === 'dashed' ? 'dashed' : 'solid', borderDashedValue: [6, 4] } }];
        }
      },
      {
        name: 'ufosMeasure', totalStep: 3, needDefaultPointFigure: true,
        createPointFigures: function (p) {
          var c = p.coordinates || [], o = p.overlay || {}, pts = o.points || [], x = extOf(o);
          if (c.length < 2 || pts.length < 2) return [];
          var p1 = pts[0].value, p2 = pts[1].value;
          var col = p2 >= p1 ? state.theme.up : state.theme.down;
          var dp = p2 - p1, pct = p1 ? dp / p1 * 100 : 0;
          var bars = (isFiniteNum(pts[0].dataIndex) && isFiniteNum(pts[1].dataIndex)) ? Math.abs(pts[1].dataIndex - pts[0].dataIndex) : null;
          var dur = (isFiniteNum(pts[0].timestamp) && isFiniteNum(pts[1].timestamp)) ? formatDuration(pts[1].timestamp - pts[0].timestamp) : '';
          var l1 = (dp >= 0 ? '+' : '\u2212') + formatPrice(Math.abs(dp)) + ' (' + (dp >= 0 ? '+' : '\u2212') + Math.abs(pct).toFixed(2) + '%)';
          var l2 = (bars !== null ? bars + ' bars' : '') + (dur ? ' \u00B7 ' + dur : '');
          var tx = Math.max(c[0].x, c[1].x) + 6, ty = Math.min(c[0].y, c[1].y);
          var figs = [
            { type: 'polygon', attrs: { coordinates: [c[0], { x: c[1].x, y: c[0].y }, c[1], { x: c[0].x, y: c[1].y }] },
              styles: { style: 'stroke_fill', color: withAlpha(col, 0.14), borderColor: withAlpha(col, 0.7), borderSize: 1, borderStyle: 'dashed', borderDashedValue: [4, 3] } },
            { type: 'line', attrs: { coordinates: [c[0], c[1]] }, styles: { color: col, size: 1, style: 'solid' } },
            { type: 'text', ignoreEvent: true, attrs: { x: tx, y: ty, text: l1, align: 'left', baseline: 'top' }, styles: figText(col) },
            { type: 'text', ignoreEvent: true, attrs: { x: tx, y: ty + 16, text: l2, align: 'left', baseline: 'top' }, styles: figText(state.theme.text) }
          ];
          if (x.temp) figs.push({ type: 'text', ignoreEvent: true, attrs: { x: tx, y: ty + 32, text: 'unsaved \u00B7 Enter keeps it', align: 'left', baseline: 'top' }, styles: figText(state.theme.muted) });
          return figs;
        }
      },
      {
        name: 'ufosFib', totalStep: 3, needDefaultPointFigure: true,
        createPointFigures: function (p) {
          var c = p.coordinates || [], o = p.overlay || {}, pts = o.points || [], x = extOf(o), b = p.bounding || {};
          if (c.length < 2 || pts.length < 2) return [];
          var levels = x.levels || FIB_DEFAULT, xl = Math.min(c[0].x, c[1].x), xr = b.width || Math.max(c[0].x, c[1].x);
          var figs = [{ type: 'line', attrs: { coordinates: [c[0], c[1]] }, styles: { color: withAlpha(x.color, 0.6), size: 1, style: 'dashed', dashedValue: [4, 3] } }];
          levels.forEach(function (lv) {
            var y = c[1].y + (c[0].y - c[1].y) * lv / 100;         // 0 % at the 2nd point, 100 % at the 1st
            var price = pts[1].value + (pts[0].value - pts[1].value) * lv / 100;
            figs.push({ type: 'line', attrs: { coordinates: [{ x: xl, y: y }, { x: xr, y: y }] }, styles: figLine(x) });
            figs.push({ type: 'text', ignoreEvent: true, attrs: { x: xl + 2, y: y - 2, text: lv + '% (' + formatPrice(price) + ')', align: 'left', baseline: 'bottom' }, styles: figText(x.color) });
          });
          return figs;
        }
      },
      arrowOverlay('ufosArrowUp', true),
      arrowOverlay('ufosArrowDown', false),
      {
        name: 'ufosAlertLine', totalStep: 2, needDefaultPointFigure: true,
        createPointFigures: function (p) {
          var c = p.coordinates || [], o = p.overlay || {}, pts = o.points || [], x = extOf(o), b = p.bounding || {};
          if (c.length < 1) return [];
          var w = b.width || 2000, y = c[0].y;
          return [
            { type: 'line', attrs: { coordinates: [{ x: 0, y: y }, { x: w, y: y }] }, styles: figLine(x) },
            { type: 'text', ignoreEvent: true, attrs: { x: 4, y: y - 2, text: '\uD83D\uDD14 ' + (x.linked ? 'Alert ' : 'Alert line ') + formatPrice(pts[0] ? pts[0].value : NaN), align: 'left', baseline: 'bottom' }, styles: figText(x.color) }
          ];
        }
      },
      {
        name: 'ufosBrush', totalStep: 3, needDefaultPointFigure: false,
        createPointFigures: function (p) {
          var x = extOf(p.overlay), path = x.path || [];
          if (path.length < 2 || !engine.chart || typeof engine.chart.convertToPixel !== 'function') return [];
          var px = engine.chart.convertToPixel(path.map(function (q) { return { timestamp: q.t, value: q.p }; }), { paneId: CANDLE_PANE });
          if (!Array.isArray(px)) { apiProblem('convertToPixel(points[]) did not return an array'); return []; }
          var coords = px.filter(function (q) { return q && isFiniteNum(q.x) && isFiniteNum(q.y); });
          return coords.length > 1 ? [{ type: 'line', attrs: { coordinates: coords }, styles: figLine(x) }] : [];
        }
      }
    ];
  }
  function arrowOverlay(name, up) {
    return {
      name: name, totalStep: 2, needDefaultPointFigure: true,
      createPointFigures: function (p) {
        var c = p.coordinates || [], x = extOf(p.overlay);
        if (!c.length) return [];
        var s = 5 + 2 * (x.width || 1), cx = c[0].x, cy = c[0].y, dir = up ? 1 : -1, tip = cy + dir * 4;
        return [{ type: 'polygon', attrs: { coordinates: [{ x: cx, y: tip }, { x: cx - s, y: tip + dir * s * 1.6 }, { x: cx + s, y: tip + dir * s * 1.6 }] },
          styles: { style: 'fill', color: x.color } }];
      }
    };
  }

  var drawings = {
    enabled: false,
    recs: new Map(), byId: new Map(), byLocal: new Map(), byOverlay: new Map(), deletedById: new Map(),
    superseded: new Set(),                        // localIds replaced while their POST was pending
    seqL: 0, seqC: 0, seqDraw: 0,
    undo: [], redo: [],
    tool: null, toolStyle: copy(DEFAULT_STYLE), magnet: 'off', hidden: false,
    selected: null, drawingOverlay: null, removing: false, editor: null,
    brushOn: false, brushPts: null, brushCanvas: null,

    start: function (kc, chart) {
      var need = ['createOverlay', 'removeOverlay', 'overrideOverlay', 'convertToPixel', 'convertFromPixel'];
      var miss = need.filter(function (f) { return typeof chart[f] !== 'function'; });
      if (typeof kc.registerOverlay !== 'function') miss.unshift('klinecharts.registerOverlay');
      if (miss.length) { apiProblem('drawing tools disabled, missing: ' + miss.join(', ')); return; }
      customOverlays().forEach(function (t) { kc.registerOverlay(t); });
      if (typeof kc.getSupportedOverlays === 'function') {
        var sup = kc.getSupportedOverlays() || [];
        var lack = BUILTIN_OVERLAYS.filter(function (n) { return sup.indexOf(n) < 0; });
        if (lack.length) apiProblem('built-in overlays missing: ' + lack.join(', '));
      } else {
        apiProblem('klinecharts.getSupportedOverlays missing (built-in overlays unchecked)');
      }
      this.enabled = true;
      this.installInput();
    },

    // ---------------------------------------------------------------- bookkeeping
    newLid: function () { return 'L' + (++this.seqL); },
    newLocalId: function () { return 'c-' + (++this.seqC); },
    onTf: function (d) { return d.timeframes === 'all' || d.timeframes.indexOf(state.tf) >= 0; },
    sendUndoState: function () { send({ type: 'undoState', canUndo: this.undo.length > 0, canRedo: this.redo.length > 0 }); },
    push: function (entry) {
      this.undo.push(entry);
      if (this.undo.length > HISTORY_MAX) this.undo.shift();
      this.redo = [];
      this.sendUndoState();
    },
    clearHistory: function () { var had = this.undo.length || this.redo.length; this.undo = []; this.redo = []; if (had) this.sendUndoState(); },
    dropHistory: function (lid) {
      var keep = function (e) {
        if (e.op === 'batch') { e.items = e.items.filter(keep); return e.items.length > 0; }
        return e.lid !== lid;
      };
      this.undo = this.undo.filter(keep); this.redo = this.redo.filter(keep);
      this.sendUndoState();
    },
    wire: function (rec) { return Object.assign({ id: rec.id }, bodyOf(rec.d)); },

    // ---------------------------------------------------------------- overlays
    extendFor: function (d, temp) {
      if (d.tool === 'simpleAnnotation' || d.tool === 'simpleTag') return d.text || '';
      return { color: paletteColor(d.style.color), width: d.style.width, dash: d.style.dash, levels: d.style.levels,
        text: d.text, temp: !!temp, linked: !!d.alert_id, path: d.tool === 'brush' ? d.points : null };
    },
    overlayStyles: function (st) {
      var col = paletteColor(st.color);
      return {
        point: { color: col, borderColor: withAlpha(col, 0.5), borderSize: 1, radius: 4, activeColor: col, activeBorderColor: withAlpha(col, 0.5), activeBorderSize: 2, activeRadius: 5 },
        line: { color: col, size: st.width, style: st.dash === 'dashed' ? 'dashed' : 'solid', dashedValue: [6, 4] },
        polygon: { style: 'stroke_fill', color: withAlpha(col, 0.12), borderColor: col, borderSize: st.width, borderStyle: 'solid' },
        rect: { style: 'stroke_fill', color: withAlpha(col, 0.12), borderColor: col, borderSize: st.width, borderStyle: 'solid', borderRadius: 0 },
        text: figText(col)
      };
    },
    createOverlayFor: function (rec) {
      var self = this, d = rec.d, lid = rec.lid;
      var anchor = d.tool === 'brush' ? [d.points[0], d.points[d.points.length - 1]] : d.points;
      var spec = {
        name: overlayName(d.tool), id: 'ufos_' + lid, groupId: 'ufos',
        lock: d.locked || d.tool === 'brush',          // a freehand path is not draggable (anchors only)
        visible: d.visible && !this.hidden, mode: MAGNET_MODE[this.magnet],
        points: anchor.map(function (q) { return { timestamp: q.t, value: q.p }; }),
        styles: this.overlayStyles(d.style), extendData: this.extendFor(d, rec.temp),
        onPressedMoveEnd: function (e) { self.movedByUser(lid, e); return false; },
        onSelected: function () { self.select(lid); return false; },
        onDeselected: function () { if (self.selected === lid) self.select(null); return false; },
        onRemoved: function () { self.removedByEngine(lid); return false; },
        onDoubleClick: function () { var r = self.recs.get(lid); if (r && r.d.tool === 'simpleAnnotation' && !r.d.locked) self.editExistingText(lid); return false; }
      };
      var ret;
      try { ret = engine.chart.createOverlay(spec); } catch (e) { ret = null; }
      var oid = Array.isArray(ret) ? ret[0] : ret;
      if (oid === null || oid === undefined) { apiProblem('createOverlay(' + spec.name + ') returned nothing'); return; }
      rec.overlayId = String(oid);
      this.byOverlay.set(rec.overlayId, lid);
    },
    removeOverlayOf: function (rec) {
      if (!rec.overlayId) return;
      this.removing = true;
      try { engine.chart.removeOverlay({ id: rec.overlayId }); } catch (e) { apiProblem('removeOverlay({id}) failed'); }
      this.removing = false;
      this.byOverlay.delete(rec.overlayId);
      rec.overlayId = null;
    },
    render: function (rec) {
      if (!this.enabled) return;
      this.removeOverlayOf(rec);
      if (!rec.deleted && state.symbol && this.onTf(rec.d)) this.createOverlayFor(rec);
    },
    renderAll: function () { var self = this; this.recs.forEach(function (r) { self.render(r); }); },
    removedByEngine: function (lid) {
      if (this.removing) return;
      var rec = this.recs.get(lid);
      if (!rec || rec.deleted) return;
      if (rec.overlayId) this.byOverlay.delete(rec.overlayId);
      rec.overlayId = null;
      this.userDelete(lid);                       // the engine removed it (e.g. its own key handling)
    },

    // ---------------------------------------------------------------- primitive steps (also used by undo/redo)
    // Revision 3 (review H2): while a create's POST is pending there is exactly ONE drawingCreated;
    // undo/redo before the answer only toggle "delete after save", and drawingSaved settles the rest.
    applyCreate: function (lid, body) {
      var rec = this.recs.get(lid);
      if (!rec) { rec = { lid: lid }; this.recs.set(lid, rec); }
      if (rec.localId && !rec.id && rec.deleteAfterSave) {
        rec.deleteAfterSave = false;              // redo before drawingSaved: reuse localId + clientId
        rec.deleted = false; rec.temp = false; rec.d = bodyOf(body);
        this.render(rec);
        return;
      }
      if (rec.localId && !rec.id) { this.superseded.add(rec.localId); this.byLocal.delete(rec.localId); }
      rec.d = bodyOf(body); rec.deleted = false; rec.temp = false; rec.id = null;
      rec.persisted = null; rec.deleteAfterSave = false;
      rec.localId = this.newLocalId();
      rec.clientId = uuid4();
      this.byLocal.set(rec.localId, lid);
      this.render(rec);
      send({ type: 'drawingCreated', localId: rec.localId, clientId: rec.clientId, drawing: bodyOf(rec.d) });
    },
    applyDelete: function (lid) {
      var rec = this.recs.get(lid);
      if (!rec || rec.deleted) return;
      this.removeOverlayOf(rec);
      rec.deleted = true;
      if (this.selected === lid) this.select(null);
      if (rec.id) {
        this.deletedById.set(rec.id, lid);
        this.byId.delete(rec.id);
        send({ type: 'drawingDeleted', id: rec.id });
        rec.id = null;
      } else if (rec.localId) {
        rec.deleteAfterSave = true;               // POST still running: settled by drawingSaved
      }
    },
    applyChange: function (lid, body) {
      var rec = this.recs.get(lid);
      if (!rec || rec.deleted) return;
      var changes = diffBodies(rec.d, body);
      rec.d = bodyOf(body);
      this.render(rec);
      if (rec.temp || !Object.keys(changes).length) return;
      if (rec.id) {
        rec.persisted = bodyOf(rec.d);
        send({ type: 'drawingChanged', id: rec.id, changes: changes, final: true });
      }                                           // no id yet: drawingSaved sends the difference
      if (this.selected === lid) this.sendSelection();
    },

    // ---------------------------------------------------------------- user steps (undoable)
    userCreate: function (body, temp) {
      var lid = this.newLid();
      if (temp) {
        var rec = { lid: lid, d: bodyOf(body), temp: true, deleted: false, id: null, localId: null };
        this.recs.set(lid, rec);
        this.render(rec);
        return lid;
      }
      this.applyCreate(lid, body);
      this.push({ op: 'create', lid: lid, after: bodyOf(body) });
      return lid;
    },
    userDelete: function (lid) {
      var rec = this.recs.get(lid);
      if (!rec || rec.deleted) return;
      if (rec.temp) { this.removeOverlayOf(rec); this.recs.delete(lid); if (this.selected === lid) this.select(null); return; }
      var before = bodyOf(rec.d);
      this.applyDelete(lid);
      this.push({ op: 'delete', lid: lid, before: before });
    },
    userChange: function (lid, body) {
      var rec = this.recs.get(lid);
      if (!rec || rec.deleted) return;
      var before = bodyOf(rec.d);
      if (same(before, bodyOf(body))) return;
      this.applyChange(lid, body);
      if (!rec.temp) this.push({ op: 'change', lid: lid, before: before, after: bodyOf(body) });
    },
    undoStep: function () {
      var e = this.undo.pop();
      if (!e) return;
      this.invert(e);
      this.redo.push(e);
      this.sendUndoState();
    },
    redoStep: function () {
      var e = this.redo.pop();
      if (!e) return;
      this.forward(e);
      this.undo.push(e);
      this.sendUndoState();
    },
    invert: function (e) {
      if (e.op === 'create') this.applyDelete(e.lid);
      else if (e.op === 'delete') this.applyCreate(e.lid, e.before);
      else if (e.op === 'change') this.applyChange(e.lid, e.before);
      else for (var i = e.items.length - 1; i >= 0; i--) this.invert(e.items[i]);
    },
    forward: function (e) {
      if (e.op === 'create') this.applyCreate(e.lid, e.after);
      else if (e.op === 'delete') this.applyDelete(e.lid);
      else if (e.op === 'change') this.applyChange(e.lid, e.after);
      else for (var i = 0; i < e.items.length; i++) this.forward(e.items[i]);
    },

    // ---------------------------------------------------------------- host messages
    load: function (list, append) {
      var self = this;
      if (!append) this.reset(true);
      list.forEach(function (dr) {
        var lid = self.byId.get(dr.id);
        var rec = lid ? self.recs.get(lid) : null;
        if (!rec) { lid = self.newLid(); rec = { lid: lid }; self.recs.set(lid, rec); self.byId.set(dr.id, lid); }
        rec.id = dr.id; rec.localId = null; rec.d = bodyOf(dr); rec.persisted = bodyOf(dr);
        rec.deleted = false; rec.temp = false; rec.deleteAfterSave = false;
        self.render(rec);
      });
    },
    reset: function (clearHistory) {
      var self = this;
      this.cancelDrawing();
      this.closeEditor(false);
      this.recs.forEach(function (r) { self.removeOverlayOf(r); });
      this.recs = new Map(); this.byId = new Map(); this.byLocal = new Map(); this.byOverlay = new Map(); this.deletedById = new Map();
      if (this.selected !== null) this.select(null);
      if (clearHistory) this.clearHistory();
    },
    // Revision 3 (review M4): the host's state is authoritative; only that drawing's history goes.
    upsert: function (dr) {
      var lid = this.byId.get(dr.id) || this.deletedById.get(dr.id);
      var rec = lid ? this.recs.get(lid) : null;
      if (!rec) { this.load([dr], true); return; }
      this.deletedById.delete(dr.id);
      this.byId.set(dr.id, lid);
      rec.id = dr.id; rec.deleted = false; rec.temp = false; rec.deleteAfterSave = false;
      rec.d = bodyOf(dr); rec.persisted = bodyOf(dr);
      this.render(rec);
      this.dropHistory(lid);
      if (this.selected === lid) this.sendSelection();
    },
    saved: function (localId, id, dr) {
      if (this.superseded.has(localId)) {         // never two stored copies
        this.superseded.delete(localId);
        send({ type: 'drawingDeleted', id: id });
        return;
      }
      var lid = this.byLocal.get(localId);
      if (!lid) return;                           // e.g. the symbol changed meanwhile: nothing to re-key
      var rec = this.recs.get(lid);
      this.byLocal.delete(localId);
      rec.localId = null;
      rec.persisted = bodyOf(dr);
      if (rec.deleteAfterSave || rec.deleted) {
        rec.deleteAfterSave = false;
        this.deletedById.set(id, lid);
        send({ type: 'drawingDeleted', id: id });
        return;
      }
      rec.id = id;
      this.byId.set(id, lid);
      var changes = diffBodies(rec.persisted, rec.d);
      if (Object.keys(changes).length) {
        rec.persisted = bodyOf(rec.d);
        send({ type: 'drawingChanged', id: id, changes: changes, final: true });
      }
      if (this.selected === lid) this.sendSelection();
    },
    rejected: function (op, localId, id) {
      var lid = op === 'create' ? (localId && this.byLocal.get(localId)) : (op === 'delete' ? this.deletedById.get(id) : this.byId.get(id));
      var rec = lid ? this.recs.get(lid) : null;
      if (!rec) { reportError('drawing_unknown', 'drawingRejected for an unknown drawing.', 'rejected'); return; }
      if (op === 'create') {
        this.removeOverlayOf(rec);
        this.byLocal.delete(localId);
        this.recs.delete(lid);
        if (this.selected === lid) this.select(null);
      } else if (op === 'delete') {
        this.deletedById.delete(id);              // restore as it was before the delete
        rec.deleted = false; rec.id = id; this.byId.set(id, lid);
        this.render(rec);
      }
      // op 'change': no local revert - the host follows with upsertDrawing (its authoritative state)
      this.dropHistory(lid);
    },
    edit: function (id, changes) {
      var lid = this.byId.get(id);
      if (!lid) { reportError('drawing_unknown', 'editDrawing: unknown drawing id.', 'edit'); return; }
      var rec = this.recs.get(lid), body = bodyOf(rec.d);
      for (var k in changes) {
        if (!Object.prototype.hasOwnProperty.call(changes, k)) continue;
        if (k === 'style') Object.assign(body.style, changes.style);
        else body[k] = changes[k];
      }
      var p = drawingCross(body);
      if (p) { reportError('bad_message', 'editDrawing: ' + p, 'editDrawing'); return; }
      this.userChange(lid, body);
    },
    removeById: function (id) {
      var lid = this.byId.get(id);
      if (!lid) { reportError('drawing_unknown', 'removeDrawing: unknown drawing id.', 'remove'); return; }
      this.userDelete(lid);
    },
    selectById: function (id, jump) {
      if (id === null) { this.select(null); return; }
      var lid = this.byId.get(id);
      if (!lid) { reportError('drawing_unknown', 'Unknown drawing id.', 'select'); return; }
      this.select(lid);
      if (jump) {
        var t = this.recs.get(lid).d.points[0].t, c = engine.chart;
        if (typeof c.scrollToTimestamp === 'function') c.scrollToTimestamp(t);
        else { var i = findIndex(engine.list(), t); c.scrollToDataIndex(i >= 0 ? i : Math.max(0, -i - 1)); }
      }
    },
    select: function (lid) {
      if (this.selected === lid) return;
      this.selected = lid;
      this.sendSelection();
    },
    sendSelection: function () {
      var rec = this.selected ? this.recs.get(this.selected) : null;
      send({ type: 'selectionChanged', id: rec ? rec.id : null, localId: rec ? rec.localId || null : null, drawing: rec && !rec.temp ? this.wire(rec) : null });
    },
    setMagnet: function (mode) {
      this.magnet = mode;
      var c = engine.chart, self = this;
      if (!this.enabled) return;
      this.recs.forEach(function (r) {
        if (!r.overlayId) return;
        try { c.overrideOverlay({ id: r.overlayId, mode: MAGNET_MODE[mode] }); } catch (e) { apiProblem('overrideOverlay({id, mode}) failed'); }
      });
      if (this.drawingOverlay) { try { c.overrideOverlay({ id: this.drawingOverlay, mode: MAGNET_MODE[mode] }); } catch (e2) { /* reported above */ } }
      void self;
    },
    command: function (name) {
      var self = this;
      switch (name) {
        case 'undo': this.undoStep(); break;
        case 'redo': this.redoStep(); break;
        case 'deleteSelected': if (this.selected) this.userDelete(this.selected); break;
        case 'lock':
          if (this.selected) { var r = this.recs.get(this.selected); if (r && !r.temp) { var b = bodyOf(r.d); b.locked = !b.locked; this.userChange(r.lid, b); } }
          break;
        case 'lockAll': case 'unlockAll':
          var target = name === 'lockAll', items = [];
          this.recs.forEach(function (rec) {
            if (rec.deleted || rec.temp || rec.d.locked === target) return;
            var before = bodyOf(rec.d), after = bodyOf(rec.d);
            after.locked = target;
            self.applyChange(rec.lid, after);
            items.push({ op: 'change', lid: rec.lid, before: before, after: after });
          });
          if (items.length) this.push({ op: 'batch', items: items });
          break;
        case 'hideAll': this.hidden = true; this.renderAll(); break;
        case 'showAll': this.hidden = false; this.renderAll(); break;
        case 'cancel': this.cancelAll(); break;
        case 'saveMeasure': this.saveMeasure(); break;
      }
    },
    cancelAll: function () {
      var self = this, hadTool = this.tool !== null;
      this.closeEditor(false);
      this.cancelDrawing();
      this.recs.forEach(function (r) { if (r.temp) { self.removeOverlayOf(r); self.recs.delete(r.lid); } });
      this.select(null);
      this.tool = null;
      if (hadTool) send({ type: 'toolChanged', name: null });
    },
    saveMeasure: function () {
      var rec = this.selected ? this.recs.get(this.selected) : null;
      if (!rec || !rec.temp) {
        rec = null;
        this.recs.forEach(function (r) { if (r.temp) rec = r; });   // the newest temporary measure
      }
      if (!rec) return;
      var body = bodyOf(rec.d);
      this.applyCreate(rec.lid, body);
      this.push({ op: 'create', lid: rec.lid, after: body });
      if (this.selected === rec.lid) this.sendSelection();
    },

    // ---------------------------------------------------------------- interactive drawing
    setTool: function (name, style, fromPage) {
      if (!this.enabled) { if (name !== null) apiProblem('drawing tools are disabled (see the earlier engine_api error)'); return; }
      this.closeEditor(false);
      this.cancelDrawing();
      if (style) Object.assign(this.toolStyle, style);
      this.tool = name;
      if (fromPage) send({ type: 'toolChanged', name: name });
      if (name === null) return;
      if (name === 'brush') { this.brushOn = true; return; }
      var self = this, st = bodyOf({ tool: name, points: [], style: this.styleFor(name), name: null, text: null, locked: false, visible: true, timeframes: 'all', alert_id: null });
      var spec = {
        name: overlayName(name), id: 'ufos_draw_' + (++this.seqDraw), groupId: 'ufos', mode: MAGNET_MODE[this.magnet],
        styles: this.overlayStyles(st.style), extendData: this.extendFor(st, name === 'measure'),
        onDrawEnd: function (e) { self.drawEnd(name, e); return false; }
      };
      var ret;
      try { ret = engine.chart.createOverlay(spec); } catch (e) { ret = null; }
      var oid = Array.isArray(ret) ? ret[0] : ret;
      if (oid === null || oid === undefined) { apiProblem('createOverlay(' + spec.name + ') for drawing returned nothing'); this.tool = null; return; }
      this.drawingOverlay = String(oid);
    },
    styleFor: function (tool) {
      var s = { color: this.toolStyle.color, width: this.toolStyle.width, dash: this.toolStyle.dash, levels: null };
      if (tool === 'fibonacciLine') s.levels = this.toolStyle.levels || null;
      return s;
    },
    cancelDrawing: function () {
      if (this.drawingOverlay) {
        this.removing = true;
        try { engine.chart.removeOverlay({ id: this.drawingOverlay }); } catch (e) { /* engine already dropped it */ }
        this.removing = false;
        this.drawingOverlay = null;
      }
      this.brushOn = false;
      this.brushPts = null;
      this.clearBrushPreview();
    },
    pointsFromOverlay: function (ov) {
      return (ov && ov.points ? ov.points : []).map(function (q) {
        var t = isInt(q.timestamp) ? q.timestamp : timestampFromIndex(q.dataIndex);
        return { t: t, p: isFiniteNum(q.value) ? roundPrice(q.value) : NaN };
      });
    },
    newBody: function (tool, points, text) {
      return { tool: tool, points: points, style: this.styleFor(tool), name: null, text: text || null,
        locked: false, visible: true, timeframes: 'all', alert_id: null };
    },
    validBody: function (body) {
      for (var i = 0; i < body.points.length; i++) {
        if (cEpoch(body.points[i].t) || cPointPrice(body.points[i].p)) return 'point ' + i + ' is outside the chart range';
      }
      return drawingCross(body);
    },
    drawEnd: function (tool, e) {
      var ov = e && e.overlay ? e.overlay : null;
      var pts = this.pointsFromOverlay(ov);
      this.cancelDrawing();
      this.tool = null;
      send({ type: 'toolChanged', name: null });
      var body = this.newBody(tool, pts, tool === 'simpleAnnotation' ? 'x' : null);
      var p = this.validBody(body);
      if (p) { reportError('drawing_invalid', 'Drawing discarded: ' + p, 'draw'); return; }
      if (tool === 'simpleAnnotation') {
        var self = this;
        this.openEditor(pts[0], '', function (text) { body.text = text; self.select(self.userCreate(body, false)); });
        return;
      }
      var lid = this.userCreate(body, tool === 'measure');
      this.select(lid);
    },
    movedByUser: function (lid, e) {
      var rec = this.recs.get(lid);
      if (!rec || rec.deleted) return;
      var body = bodyOf(rec.d);
      if (rec.d.tool === 'brush') { this.render(rec); return; }
      body.points = this.pointsFromOverlay(e && e.overlay);
      if (rec.d.locked || this.validBody(body)) { this.render(rec); return; }   // locked or off-range: snap back
      this.userChange(lid, body);
    },
    editExistingText: function (lid) {
      var rec = this.recs.get(lid), self = this;
      this.openEditor(rec.d.points[0], rec.d.text || '', function (text) {
        var b = bodyOf(rec.d); b.text = text; self.userChange(lid, b);
      });
    },

    // ---------------------------------------------------------------- inline text editor (plain text only)
    openEditor: function (pt, initial, onCommit) {
      this.closeEditor(false);
      var root = document.getElementById('root'), ta = document.createElement('textarea');
      ta.id = 'note-editor';
      ta.maxLength = LIMITS.maxDrawingText;
      ta.rows = 3;
      ta.spellcheck = false;
      ta.value = initial;
      ta.setAttribute('aria-label', 'Note text (max 280 characters, Enter saves, Esc cancels)');
      var xy = null;
      try { xy = engine.chart.convertToPixel({ timestamp: pt.t, value: pt.p }, { paneId: CANDLE_PANE }); } catch (e) { xy = null; }
      var x = xy && isFiniteNum(xy.x) ? xy.x : 80, y = xy && isFiniteNum(xy.y) ? xy.y : 80;
      ta.style.left = Math.max(4, Math.min(x + 8, root.clientWidth - 230)) + 'px';
      ta.style.top = Math.max(4, Math.min(y - 10, root.clientHeight - 80)) + 'px';
      var self = this;
      this.editor = { el: ta, onCommit: onCommit };
      ta.addEventListener('keydown', function (e) {
        e.stopPropagation();
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); self.closeEditor(true); }
        else if (e.key === 'Escape') { e.preventDefault(); self.closeEditor(false); }
      });
      ta.addEventListener('blur', function () { if (self.editor && self.editor.el === ta) self.closeEditor(true); });
      root.appendChild(ta);
      ta.focus();
    },
    closeEditor: function (commit) {
      var ed = this.editor;
      if (!ed) return;
      this.editor = null;
      var text = ed.el.value.replace(/\r/g, '');
      if (ed.el.parentNode) ed.el.parentNode.removeChild(ed.el);
      if (!commit || !text.trim()) return;
      if (CTRL_TEXT.test(text)) text = text.replace(CTRL_TEXT, '');
      ed.onCommit(text.slice(0, LIMITS.maxDrawingText));
    },

    // ---------------------------------------------------------------- freehand (brush): page pointer capture
    installInput: function () {
      var self = this, box = document.getElementById('chart');
      var local = function (e) { var r = box.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
      box.addEventListener('pointerdown', function (e) {
        if (!self.brushOn || e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        if (box.setPointerCapture) { try { box.setPointerCapture(e.pointerId); } catch (x) { /* synthetic events */ } }
        self.brushPts = [local(e)];
      }, true);
      box.addEventListener('pointermove', function (e) {
        if (!self.brushOn || !self.brushPts) return;
        e.preventDefault(); e.stopPropagation();
        var q = local(e), last = self.brushPts[self.brushPts.length - 1];
        if (Math.abs(q.x - last.x) + Math.abs(q.y - last.y) >= 3) { self.brushPts.push(q); self.drawBrushPreview(); }
      }, true);
      box.addEventListener('pointerup', function (e) {
        if (!self.brushOn || !self.brushPts) return;
        e.preventDefault(); e.stopPropagation();
        self.finishBrush(self.brushPts.concat([local(e)]));
      }, true);
      document.addEventListener('keydown', function (e) { self.onKey(e); });
    },
    finishBrush: function (px) {
      this.brushPts = null;
      this.brushOn = false;
      this.clearBrushPreview();
      this.tool = null;
      send({ type: 'toolChanged', name: null });
      if (px.length > LIMITS.maxBrushPoints) {
        var step = (px.length - 1) / (LIMITS.maxBrushPoints - 1), thin = [];
        for (var i = 0; i < LIMITS.maxBrushPoints; i++) thin.push(px[Math.round(i * step)]);
        px = thin;
      }
      var pts = engine.chart.convertFromPixel(px, { paneId: CANDLE_PANE });
      if (!Array.isArray(pts)) { apiProblem('convertFromPixel(coords[]) did not return an array'); return; }
      var body = this.newBody('brush', this.pointsFromOverlay({ points: pts }), null);
      var p = this.validBody(body);
      if (p) { reportError('drawing_invalid', 'Drawing discarded: ' + p, 'draw'); return; }
      this.select(this.userCreate(body, false));
    },
    drawBrushPreview: function () {
      var box = document.getElementById('chart'), cv = this.brushCanvas;
      if (!cv) {
        cv = this.brushCanvas = document.createElement('canvas');
        cv.className = 'brush-preview';
        box.appendChild(cv);
      }
      var dpr = window.devicePixelRatio || 1;
      if (cv.width !== box.clientWidth * dpr) { cv.width = box.clientWidth * dpr; cv.height = box.clientHeight * dpr; }
      var ctx = cv.getContext('2d'), pts = this.brushPts;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cv.width, cv.height);
      ctx.strokeStyle = paletteColor(this.toolStyle.color);
      ctx.lineWidth = this.toolStyle.width;
      ctx.beginPath();
      for (var i = 0; i < pts.length; i++) { if (i) ctx.lineTo(pts[i].x, pts[i].y); else ctx.moveTo(pts[i].x, pts[i].y); }
      ctx.stroke();
    },
    clearBrushPreview: function () {
      if (this.brushCanvas && this.brushCanvas.parentNode) this.brushCanvas.parentNode.removeChild(this.brushCanvas);
      this.brushCanvas = null;
    },

    // ---------------------------------------------------------------- keyboard
    onKey: function (e) {
      if (this.editor || !state.initialised) return;
      var ctrl = e.ctrlKey || e.metaKey, k = e.key;
      if (ctrl && !e.altKey) {
        var lower = typeof k === 'string' ? k.toLowerCase() : '';
        if (lower === 'z') { e.preventDefault(); if (e.shiftKey) this.redoStep(); else this.undoStep(); }
        else if (lower === 'y') { e.preventDefault(); this.redoStep(); }
        else if (!e.shiftKey && (lower === 'k' || lower === 't' || lower === 'w')) { e.preventDefault(); send({ type: 'shortcut', key: 'ctrl+' + lower }); }
        else if (k === 'Tab') { e.preventDefault(); send({ type: 'shortcut', key: e.shiftKey ? 'ctrl+shift+tab' : 'ctrl+tab' }); }
        return;
      }
      if (e.altKey && !ctrl && SHORTCUT_TOOLS[e.code]) { e.preventDefault(); this.setTool(SHORTCUT_TOOLS[e.code], null, true); return; }
      if (k === 'Delete') { e.preventDefault(); this.command('deleteSelected'); }
      else if (k === 'Escape') { e.preventDefault(); this.cancelAll(); }
      else if (k === 'Enter') { var r = this.selected ? this.recs.get(this.selected) : null; if (r && r.temp) { e.preventDefault(); this.saveMeasure(); } }
    },

    // ---------------------------------------------------------------- symbol / timeframe changes
    onLoadBars: function (symbolChanged) {
      if (!this.enabled) return;
      if (symbolChanged) { this.reset(true); return; }   // the host sends loadDrawings for the new symbol
      this.cancelDrawing();
      this.renderAll();                                  // timeframe filter + re-anchoring
    },
    onTheme: function () { if (this.enabled) this.renderAll(); },

    debugList: function () {
      var out = [], self = this;
      this.recs.forEach(function (r) {
        out.push({ lid: r.lid, id: r.id || null, localId: r.localId || null, tool: r.d.tool, points: r.d.points, style: r.d.style,
          text: r.d.text, locked: r.d.locked, visible: r.d.visible, timeframes: r.d.timeframes, deleted: !!r.deleted,
          temp: !!r.temp, overlayId: r.overlayId || null, onTf: self.onTf(r.d), clientId: r.clientId || null,
          deleteAfterSave: !!r.deleteAfterSave });
      });
      return out;
    }
  };

  // ================================================================== MR8: time markers (bridge revision 4)
  // Drawn by two engine indicators without data (no figures -> never change the price scale):
  // bands behind the candles, glyphs/lines in front. Anchored by time on every timeframe.
  var markers = {
    list: [],
    set: function (arr) {
      this.list = arr.map(function (m) { return { id: m.id, t: m.t, tEnd: m.tEnd, kind: m.kind, side: m.side, label: m.label }; })
        .sort(function (a, b) { return a.t - b.t; });
      engine.refresh();
      renderLegend();
    },
    clear: function () { if (this.list.length) { this.list = []; engine.refresh(); } },
    // index of the bar containing t (start <= t < start + period), else the first bar starting after t;
    // null when t lies outside the loaded data
    barOf: function (list, t) {
      var n = list.length;
      if (!n || t < list[0].timestamp || t >= list[n - 1].timestamp + periodMs()) return null;
      var i = findIndex(list, t);
      if (i >= 0) return i;
      var ins = -i - 1;                               // first bar with start > t
      if (ins > 0 && t < list[ins - 1].timestamp + periodMs()) return ins - 1;
      return ins < n ? ins : null;
    },
    span: function (list, m) {                        // [from, to] bar indexes or null
      var n = list.length;
      if (m.tEnd === null && this.isPoint(m)) { var i = this.barOf(list, m.t); return i === null ? null : [i, i]; }
      if (!n) return null;
      var end = m.tEnd === null ? list[n - 1].timestamp : m.tEnd;
      if (end < list[0].timestamp || m.t >= list[n - 1].timestamp + periodMs()) return null;
      var a = m.t < list[0].timestamp ? 0 : this.barOf(list, m.t);
      var b = end >= list[n - 1].timestamp + periodMs() ? n - 1 : this.barOf(list, end);
      if (a === null || b === null) return null;
      if (b < a) b = a;
      return [a, b];
    },
    isPoint: function (m) { return m.kind === 'insider' || m.kind === 'fill' || m.kind === 'fomc' || m.kind === 'release'; },
    isBand: function (m) { return !this.isPoint(m) || m.tEnd !== null; },
    glyph: function (m) {
      if (m.kind === 'fomc') return 'FOMC';
      if (m.kind === 'release') return 'R';
      if (m.kind === 'period_best') return '\u25AE best';
      if (m.kind === 'period_worst') return '\u25AE worst';
      if (m.kind === 'drawdown') return '\u25AE drawdown';
      if (m.side === 'buy' || m.side === 'cover') return '\u25B2';
      if (m.side === 'sell' || m.side === 'short') return '\u25BC';
      return '\u25CF';
    },
    at: function (list, idx) {
      var self = this;
      return this.list.filter(function (m) { var sp = self.span(list, m); return sp && idx >= sp[0] && idx <= sp[1]; });
    }
  };

  function markerBandsIndicator() {
    return {
      name: 'UFOS_MARK_BANDS', shortName: '', series: 'price', zLevel: -1, figures: [], calc: emptyCalc,
      draw: function (p) {
        if (!markers.list.length || !drawParamsOk(p, 'UFOS_MARK_BANDS')) return true;
        var list = p.chart.getDataList(), r = visibleIndexRange(p.chart, list.length), half = halfBarWidth(p.chart);
        var t = state.theme, upDim = withAlpha(t.up, 0.10), downDim = withAlpha(t.down, 0.10);
        markers.list.forEach(function (m) {
          if (!markers.isBand(m) || markers.isPoint(m)) return;
          var sp = markers.span(list, m);
          if (!sp || sp[1] < r[0] || sp[0] > r[1]) return;
          var x1 = p.xAxis.convertToPixel(sp[0]) - half, x2 = p.xAxis.convertToPixel(sp[1]) + half;
          p.ctx.fillStyle = m.kind === 'period_best' ? upDim : downDim;
          p.ctx.fillRect(x1, 0, x2 - x1, p.bounding.height);
        });
        return true;
      }
    };
  }
  function markerPointsIndicator() {
    return {
      name: 'UFOS_MARK_POINTS', shortName: '', series: 'price', zLevel: 2, figures: [], calc: emptyCalc,
      draw: function (p) {
        if (!markers.list.length || !drawParamsOk(p, 'UFOS_MARK_POINTS')) return true;
        if (!p.yAxis || typeof p.yAxis.convertToPixel !== 'function') { apiProblem('indicator draw params have no yAxis.convertToPixel (markers)'); return true; }
        var list = p.chart.getDataList(), r = visibleIndexRange(p.chart, list.length), ctx = p.ctx, h = p.bounding.height, t = state.theme;
        var stack = {};                                // several markers on one bar stack outwards
        markers.list.forEach(function (m) {
          if (!markers.isPoint(m)) return;
          var sp = markers.span(list, m);
          if (!sp || sp[0] < r[0] || sp[0] > r[1]) return;
          var i = sp[0], x = p.xAxis.convertToPixel(i), bar = list[i];
          if (m.kind === 'fomc' || m.kind === 'release') {
            ctx.strokeStyle = withAlpha(t.caution, 0.75); ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
            ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, 0); ctx.lineTo(Math.round(x) + 0.5, h); ctx.stroke(); ctx.setLineDash([]);
            ctx.font = '10px ' + t.fontFamily; ctx.textBaseline = 'top'; ctx.fillStyle = t.caution;
            ctx.fillText(markers.glyph(m), Math.round(x) + 3, 58);
            return;
          }
          var up = m.side === 'buy' || m.side === 'cover', down = m.side === 'sell' || m.side === 'short';
          var key = i + (down ? 'd' : 'u'), k = stack[key] = (stack[key] || 0) + 1;
          var s = 5, off = 6 + (k - 1) * 12;
          ctx.fillStyle = up ? t.up : (down ? t.down : t.muted);
          ctx.beginPath();
          if (down) { var yt = p.yAxis.convertToPixel(bar.high) - off; ctx.moveTo(x, yt); ctx.lineTo(x - s, yt - 1.6 * s); ctx.lineTo(x + s, yt - 1.6 * s); }
          else if (up) { var yb = p.yAxis.convertToPixel(bar.low) + off; ctx.moveTo(x, yb); ctx.lineTo(x - s, yb + 1.6 * s); ctx.lineTo(x + s, yb + 1.6 * s); }
          else { var yc = p.yAxis.convertToPixel(bar.low) + off + s; ctx.arc(x, yc, s * 0.8, 0, Math.PI * 2); }
          ctx.closePath(); ctx.fill();
        });
        return true;
      }
    };
  }

  // ------------------------------------------------------------------ crosshair + legend
  var lastCrossSent = 0, crossTimer = null, lastCrossT = undefined;
  var crossShapeChecked = false;
  // KLineChart 10.0.3 calls onCrosshairChange with the raw crosshair {x, y, paneId} (no kLineData,
  // verified against the vendored engine 2026-10-18); older builds passed kLineData. Resolve the bar
  // from x via convertFromPixel in that case.
  function crossBar(data) {
    if (data.kLineData) return data.kLineData;
    var list = engine.chart ? engine.chart.getDataList() : [];
    if (!list || !list.length) return null;
    var pts = engine.chart.convertFromPixel([{ x: data.x, y: typeof data.y === 'number' ? data.y : 0 }], { paneId: CANDLE_PANE });
    var p0 = Array.isArray(pts) ? pts[0] : pts;
    if (!crossShapeChecked) {
      crossShapeChecked = true;
      if (!p0 || typeof p0.dataIndex !== 'number') apiProblem('onCrosshairChange payload has no kLineData and convertFromPixel gave no dataIndex');
    }
    var i = p0 && typeof p0.dataIndex === 'number' ? p0.dataIndex : -1;
    return i >= 0 && i < list.length ? list[i] : null;
  }
  function onCrosshair(data) {
    var inside = data && typeof data === 'object' && typeof data.x === 'number';
    var k = inside ? crossBar(data) : null;
    inside = inside && k;
    state.hover = inside ? k : null;
    renderLegend();
    var t = state.hover ? state.hover.timestamp : null;
    if (t === lastCrossT) return;
    lastCrossT = t;
    var now = Date.now();
    var flush = function () { crossTimer = null; lastCrossSent = Date.now(); send({ type: 'crosshair', bar: fromEngineBar(state.hover) }); };
    if (now - lastCrossSent >= 50) flush();
    else if (!crossTimer) crossTimer = setTimeout(flush, 50);
  }

  var fmtCache = { key: null, price: null, pct: null, time: null, day: null };
  function formatters() {
    var key = state.locale + '|' + state.pricePrecision + '|' + displayTz() + '|' + state.tf;
    if (fmtCache.key === key) return fmtCache;
    var intraday = state.tf !== '1D' && state.tf !== '1W';
    fmtCache = {
      key: key,
      price: new Intl.NumberFormat(state.locale, { minimumFractionDigits: state.pricePrecision, maximumFractionDigits: state.pricePrecision }),
      pct: new Intl.NumberFormat(state.locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
      vol: new Intl.NumberFormat(state.locale, { maximumFractionDigits: 2 }),
      time: new Intl.DateTimeFormat(state.locale, intraday
        ? { timeZone: displayTz(), year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }
        : { timeZone: displayTz(), year: 'numeric', month: 'short', day: '2-digit' })
    };
    return fmtCache;
  }
  function compactVolume(v, f) {
    var a = Math.abs(v);
    if (a >= 1e9) return f.vol.format(v / 1e9) + 'B';
    if (a >= 1e6) return f.vol.format(v / 1e6) + 'M';
    if (a >= 1e3) return f.vol.format(v / 1e3) + 'K';
    return f.vol.format(v);
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

  function renderLegend() {
    var main = document.getElementById('legend-main'), sub = document.getElementById('legend-sub');
    if (!main || !sub) return;
    main.textContent = ''; sub.textContent = '';
    var indRow = document.getElementById('legend-ind'), markRow = document.getElementById('legend-marks');
    if (indRow) indRow.textContent = '';
    if (markRow) markRow.textContent = '';
    if (!state.symbol) return;
    var list = engine.chart ? engine.list() : [];
    var bar = state.hover || (list.length ? list[list.length - 1] : null);
    main.appendChild(el('span', 'sym', state.symbol + ' \u00B7 ' + state.tf + (state.assetClass === 'equity' ? ' \u00B7 ' + state.session.toUpperCase() : '')));
    if (!bar) { main.appendChild(el('span', 'muted', 'No bars')); return; }
    var f = formatters();
    var pairs = [['O', bar.open], ['H', bar.high], ['L', bar.low], ['C', bar.close]];
    for (var i = 0; i < pairs.length; i++) {
      main.appendChild(el('span', 'k', pairs[i][0]));
      main.appendChild(el('span', 'val', f.price.format(pairs[i][1])));
    }
    var pct = bar.open ? (bar.close - bar.open) / bar.open * 100 : 0;
    var cls = pct > 0 ? 'up' : (pct < 0 ? 'down' : 'flat');
    var arrow = pct > 0 ? '\u25B2 +' : (pct < 0 ? '\u25BC \u2212' : '\u2022 ');
    var chg = el('span', 'val ' + cls, arrow + f.pct.format(Math.abs(pct)) + '%');
    chg.title = 'Candle change: close vs open';
    main.appendChild(chg);
    main.appendChild(el('span', 'k', 'V'));
    main.appendChild(el('span', 'val', bar.volumeMissing ? '\u2014' : compactVolume(bar.volume, f)));
    if (bar.feed === 'iex_tail') {
      var chip = el('span', 'chip tail', TAIL_LABEL);
      chip.title = 'IEX live tail (last 15 min) \u2014 replaced by SIP when released';
      main.appendChild(chip);
    } else {
      main.appendChild(el('span', 'chip', feedLabel(bar.feed) + (bar.final ? '' : ' \u00B7 live')));
    }
    renderIndicatorLegend(bar);
    renderMarkerLegend(state.hover);
    var tzName = displayTz();
    sub.appendChild(el('span', '', f.time.format(new Date(bar.timestamp))));
    sub.appendChild(el('span', '', '  \u00B7  ' + (state.timeMode === 'exchange' ? 'Exchange time ' : 'Local time ') + tzName));
  }
  function renderMarkerLegend(bar) {
    var row = document.getElementById('legend-marks');
    if (!row) return;
    row.textContent = '';
    if (!bar || !markers.list.length) return;
    var list = engine.list(), idx = findIndex(list, bar.timestamp);
    if (idx < 0) return;
    var hits = markers.at(list, idx);
    hits.slice(0, 3).forEach(function (m) {
      var item = el('span', 'mark ' + (m.side === 'buy' || m.side === 'cover' || m.kind === 'period_best' ? 'up' : (m.side === 'sell' || m.side === 'short' || m.kind === 'period_worst' || m.kind === 'drawdown' ? 'down' : 'caution')));
      item.appendChild(el('span', 'mark-glyph', markers.glyph(m)));
      item.appendChild(el('span', 'mark-label', m.label));
      row.appendChild(item);
    });
    if (hits.length > 3) row.appendChild(el('span', 'muted', '+' + (hits.length - 3) + ' more'));
  }
  function renderIndicatorLegend(bar) {
    var row = document.getElementById('legend-ind');
    if (!row) return;
    row.textContent = '';
    indicators.defs.forEach(function (def) {
      var item = el('span', 'ind');
      var name = el('span', 'ind-name', def.label);
      name.style.color = paletteColor(def.color);
      if (def.note) item.title = def.note;
      item.appendChild(name);
      def.outputs.forEach(function (o) {
        var v = indicators.valueAt(o, bar.timestamp);
        var txt;
        if (v === undefined) txt = '\u2014';
        else if (def.kind === 'cmp') txt = /:pct$/.test(o) ? (v >= 0 ? '+' : '\u2212') + Math.abs(v).toFixed(2) + '%' : null;
        else txt = v.toFixed(def.pane === 'overlay' ? state.pricePrecision : 2);
        if (txt !== null) item.appendChild(el('span', 'val', txt));
      });
      row.appendChild(item);
    });
  }
  function feedLabel(feed) {
    switch (feed) {
      case 'sip': return 'SIP';
      case 'sip_daily': return 'SIP daily';
      case 'crypto': return 'Alpaca crypto';
      default: return 'IEX';
    }
  }

  function applyCssTheme() {
    var t = state.theme, s = document.documentElement.style;
    s.setProperty('--bg', t.background); s.setProperty('--grid', t.grid); s.setProperty('--text', t.text);
    s.setProperty('--axis-text', t.axisText); s.setProperty('--muted', t.muted); s.setProperty('--up', t.up);
    s.setProperty('--down', t.down); s.setProperty('--accent', t.accent); s.setProperty('--caution', t.caution);
    s.setProperty('--font', t.fontFamily);
  }

  function fatal(code, message) {
    var box = document.getElementById('fatal');
    if (box) { box.textContent = 'Chart error (' + code + '): ' + message; box.hidden = false; }
    reportError(code, message, 'fatal');
  }

  // ------------------------------------------------------------------ hardening + start
  // No navigation from the page: block drops of files/links and middle-click / ctrl-click opens.
  window.addEventListener('dragover', function (e) { e.preventDefault(); });
  window.addEventListener('drop', function (e) { e.preventDefault(); });
  window.addEventListener('auxclick', function (e) { e.preventDefault(); });

  if (webview) {
    webview.addEventListener('message', function (e) { receive(e.data); });
  } else {
    window.addEventListener('message', function (e) { if (e.source === window) receive(e.data); });
  }

  if (engine.start()) {
    send({ type: 'ready', engine: 'klinecharts', engineVersion: engine.version(), revision: 4 });
  }

  // Test-only read access in a plain browser (never inside WebView2, where the host is the
  // only party and no page object is exposed).
  if (!webview) {
    window.__ufosChartDebug = Object.freeze({
      count: function () { return engine.list().length; },
      bar: function (i) { var l = engine.list(); return fromEngineBar(l[i < 0 ? l.length + i : i]); },
      state: function () { return { initialised: state.initialised, symbol: state.symbol, tf: state.tf, chartType: state.chartType, hasMoreLeft: state.hasMoreLeft, lastPrice: state.lastPrice, dayChangeSign: state.dayChangeSign, tz: displayTz(), pricePrecision: state.pricePrecision }; },
      styles: function () { return engine.chart ? engine.chart.getStyles() : null; },
      scrollToStart: function () { if (engine.chart) engine.chart.scrollToDataIndex(0); },
      scrollToIndex: function (i) { if (engine.chart) engine.chart.scrollToDataIndex(i); },
      legend: function () { var a = document.getElementById('legend-main'), b = document.getElementById('legend-sub'), c = document.getElementById('legend-ind'), d = document.getElementById('legend-marks'); return (a ? a.textContent : '') + ' | ' + (b ? b.textContent : '') + ' | ' + (c ? c.textContent : '') + ' | ' + (d ? d.textContent : ''); },
      drawings: function () { return drawings.debugList(); },
      drawingState: function () { return { tool: drawings.tool, magnet: drawings.magnet, hidden: drawings.hidden, selected: drawings.selected, undo: drawings.undo.length, redo: drawings.redo.length, enabled: drawings.enabled, drawingOverlay: drawings.drawingOverlay }; },
      markers: function () { var l = engine.list(); return markers.list.map(function (m) { return { id: m.id, span: markers.span(l, m) }; }); },
      indicators: function () { return { volume: indicators.volume, volPane: engine.volPaneId, defs: indicators.defs.map(function (d) { return { key: d.key, name: d.name, pane: d.pane, paneId: d.paneId, values: d.outputs.map(function (o) { return indicators.values[o] ? indicators.values[o].size : 0; }) }; }) }; }
    });
  }
})();

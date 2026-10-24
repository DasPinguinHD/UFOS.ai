#!/usr/bin/env node
/*
 * MR3a headless tests for the Market Research chart page (NOT shipped with the app).
 *
 *   node run_tests.js [--engine fake|real] [--shots <dir>]
 *
 * Needs Playwright (Node) with a Chromium (PLAYWRIGHT_BROWSERS_PATH). Serves ../ (the Chart
 * folder) from a loopback static server; with --engine fake (default while
 * vendor/klinecharts.min.js is absent) the request for vendor/klinecharts.min.js is answered with
 * tests/fake_klinecharts.js, a test double of the KLineChart 10 API subset chart.js uses.
 * Messages are injected with window.postMessage (the page's plain-browser shim); page->host
 * messages are collected from the 'ufoschart:out' DOM event.
 */
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const CHART_DIR = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
function arg(name, def) { const i = args.indexOf(name); return i >= 0 && args[i + 1] ? args[i + 1] : def; }
const realEngine = path.join(CHART_DIR, 'vendor', 'klinecharts.min.js');
const ENGINE = arg('--engine', fs.existsSync(realEngine) ? 'real' : 'fake');
const SHOTS = arg('--shots', path.join(__dirname, 'screenshots'));
fs.mkdirSync(SHOTS, { recursive: true });

const THEME = {
  background: '#111111', grid: '#202020', text: '#BFBFBF', axisText: '#8F8F8F', muted: '#6F6F6F',
  up: '#4ADE80', down: '#ED6A5A', accent: '#5AC8FA', caution: '#F0B429',
  fontFamily: 'Consolas, "Cascadia Mono", "Courier New", monospace'
};
const MIN = 60000;

// ------------------------------------------------------------------ synthetic data
function rng(seed) { let s = seed >>> 0; return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; }
// Extended-hours 1m bars (04:00–20:00 ET, EDT = UTC-4) on weekdays, newest last; the last
// `tail` bars are the IEX live tail.
function makeEquityBars(count, opts) {
  opts = opts || {};
  const r = rng(opts.seed || 7);
  const tail = opts.tail === undefined ? 15 : opts.tail;
  const days = [];
  let day = Date.UTC(2026, 9, 15);                // Thu 2026-10-15, walk backwards
  while (days.length * 960 < count + 960) {
    const wd = new Date(day).getUTCDay();
    if (wd !== 0 && wd !== 6) days.unshift(day);
    day -= 86400000;
  }
  const ts = [];
  for (const d of days) for (let m = 0; m < 960; m++) ts.push({ t: d + 8 * 3600000 + m * MIN, m });
  const pick = ts.slice(ts.length - count);
  let p = opts.start || 187.5;
  const bars = pick.map((x, i) => {
    const o = p;
    const drift = (r() - 0.5) * 0.18 * (x.m >= 330 && x.m < 720 ? 1 : 0.45);
    const c = Math.max(1, o + drift);
    const h = Math.max(o, c) + r() * 0.06;
    const l = Math.min(o, c) - r() * 0.06;
    p = c;
    const rth = x.m >= 330 && x.m < 720;          // 09:30–16:00 ET
    const v = Math.round((rth ? 40000 : 6000) * (0.3 + r()));
    const isTail = i >= count - tail;
    return { t: x.t, o: +o.toFixed(2), h: +h.toFixed(2), l: +l.toFixed(2), c: +c.toFixed(2), v,
      feed: isTail ? 'iex_tail' : 'sip', final: !isTail, s: x.m < 330 ? 'pre' : (rth ? 'rth' : 'post') };
  }).map(fixBar);
  return bars;
}
function fixBar(b) { b.h = Math.max(b.h, b.o, b.c); b.l = Math.min(b.l, b.o, b.c); return b; }
function makeDailyBars(count) {
  const r = rng(11); const out = []; let p = 120; let d = Date.UTC(2024, 9, 1, 4);
  while (out.length < count) {
    const wd = new Date(d).getUTCDay();
    if (wd !== 0 && wd !== 6) {
      const o = p, c = Math.max(1, o * (1 + (r() - 0.48) * 0.04));
      out.push(fixBar({ t: d, o: +o.toFixed(2), h: +(Math.max(o, c) * (1 + r() * 0.01)).toFixed(2), l: +(Math.min(o, c) * (1 - r() * 0.01)).toFixed(2), c: +c.toFixed(2), v: Math.round(4e7 * (0.5 + r())), feed: 'sip_daily', final: true }));
      p = c;
    }
    d += 86400000;
  }
  out[out.length - 1].final = false;
  return out;
}
function loadMsg(bars, extra) {
  return Object.assign({ type: 'loadBars', v: 1, symbol: 'AAPL', tf: '1m', session: 'eth', assetClass: 'equity',
    exchangeTz: 'America/New_York', bars, hasMoreLeft: true, sipThrough: bars.length ? bars[bars.length - 1].t - 14 * MIN : null }, extra || {});
}

// ------------------------------------------------------------------ static server
function serve() {
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
  const server = http.createServer((req, res) => {
    let rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '');
    if (rel === 'vendor/klinecharts.min.js' && ENGINE === 'fake') rel = 'tests/fake_klinecharts.js';
    const file = path.resolve(CHART_DIR, rel);
    if (!file.startsWith(CHART_DIR + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise(ok => server.listen(0, '127.0.0.1', () => ok(server)));
}

// ------------------------------------------------------------------ test plumbing
const results = [];
function check(name, cond, detail) { results.push({ name, ok: !!cond, detail: detail === undefined ? '' : String(detail) }); }

async function main() {
  const server = await serve();
  const base = 'http://127.0.0.1:' + server.address().port + '/chart.html';
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1180, height: 640 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  const consoleErrors = [];
  // CSP refusals provoked by the CSP test below are expected; anything else is a failure.
  page.on('console', m => { if (m.type() === 'error' && !/Content Security Policy/.test(m.text())) consoleErrors.push(m.text()); });
  page.on('pageerror', e => consoleErrors.push('pageerror: ' + e.message));
  const external = [];
  page.on('requestfinished', r => { if (!r.url().startsWith('http://127.0.0.1:')) external.push(r.url()); });
  await page.addInitScript(() => {
    window.__out = [];
    window.addEventListener('ufoschart:out', e => window.__out.push(e.detail));
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
  });
  await page.goto(base);
  await page.waitForFunction(() => window.__out.length > 0, null, { timeout: 5000 });

  const post = (m) => page.evaluate(msg => window.postMessage(msg, '*'), m);
  const out = () => page.evaluate(() => window.__out.slice());
  const clearOut = () => page.evaluate(() => { window.__out.length = 0; });
  const dbg = (fn, a) => page.evaluate(([f, x]) => window.__ufosChartDebug[f](x), [fn, a]);
  const frames = () => page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  const settle = async () => { await page.waitForTimeout(30); await frames(); };
  const errorsSince = async () => (await out()).filter(m => m.type === 'error');

  // T1 ready
  let o = await out();
  check('ready is the first message', o[0] && o[0].type === 'ready' && o[0].v === 1 && o[0].engine === 'klinecharts', JSON.stringify(o[0]));
  check('engine version is 10.x', o[0] && /^10\./.test(o[0].engineVersion), o[0] && o[0].engineVersion);

  // T2 before init
  await clearOut();
  await post(loadMsg(makeEquityBars(10)));
  await settle();
  o = await errorsSince();
  check('loadBars before init -> error not_initialised', o.some(e => e.code === 'not_initialised'), JSON.stringify(o));

  // T3 init + 15k load (performance budget)
  await post({ type: 'init', v: 1, theme: THEME, locale: 'en-US', tz: 'Europe/Berlin' });
  await settle();
  const bars15 = makeEquityBars(15000);
  await clearOut();
  const loadMs = await page.evaluate(async (msg) => {
    const t0 = performance.now();
    window.postMessage(msg, '*');
    await new Promise(r => setTimeout(r, 0));
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    return performance.now() - t0;
  }, loadMsg(bars15));
  check('15k candles loaded', (await dbg('count')) === 15000, await dbg('count'));
  check('initial render of 15k candles < 1500 ms', loadMs < 1500, loadMs.toFixed(0) + ' ms');
  check('no error after valid init/loadBars', (await errorsSince()).length === 0, JSON.stringify(await errorsSince()));
  await post({ type: 'setLastPrice', v: 1, price: bars15[bars15.length - 1].c, dayChangeSign: 1 });
  await settle();
  let legend = await dbg('legend');
  check('legend shows OHLC + candle % + volume', /O.*H.*L.*C.*%.*V/.test(legend), legend);
  check('legend marks the IEX live tail', legend.indexOf('IEX · live tail') >= 0, legend);
  check('legend shows local time zone', legend.indexOf('Europe/Berlin') >= 0, legend);
  await page.screenshot({ path: path.join(SHOTS, '01_candles_1m_eth_tail.png') });

  // T4 paging left
  await clearOut();
  await dbg('scrollToStart');
  await settle();
  await dbg('scrollToStart');
  await settle();
  o = (await out()).filter(m => m.type === 'needMoreLeft');
  check('needMoreLeft sent once with beforeTs = oldest bar t', o.length === 1 && o[0].beforeTs === bars15[0].t, JSON.stringify(o));
  const older = makeEquityBars(17005, { tail: 0, seed: 3 }).slice(0, 2005);   // 2000 older + 5 overlapping
  const overlap = bars15.slice(0, 5);
  const prepend = older.slice(0, 2000).filter(b => b.t < bars15[0].t).concat(overlap);
  await post({ type: 'prependBars', v: 1, bars: prepend, hasMoreLeft: true });
  await settle();
  const cnt = await dbg('count');
  check('prependBars adds only older bars (overlap ignored)', cnt === 15000 + prepend.length - 5, cnt + ' vs ' + (15000 + prepend.length - 5));
  check('oldest bar is the new first bar', (await dbg('bar', 0)).t === prepend[0].t);
  await clearOut();
  await post({ type: 'prependBars', v: 1, bars: [], hasMoreLeft: true });
  await settle();
  check('empty prependBars ends paging for this load', (await dbg('state')).hasMoreLeft === false);
  check('no error on unsolicited empty prepend', (await errorsSince()).length === 0, JSON.stringify(await errorsSince()));

  // T5 live updates
  const nBefore = await dbg('count');
  const last = await dbg('bar', -1);
  await post({ type: 'updateBar', v: 1, bar: fixBar({ t: last.t, o: last.o, h: last.h + 0.5, l: last.l, c: last.h + 0.4, v: last.v + 100, feed: 'iex_tail', final: false }) });
  await settle();
  let nb = await dbg('bar', -1);
  check('updateBar same t replaces the live candle', (await dbg('count')) === nBefore && Math.abs(nb.c - (last.h + 0.4)) < 1e-9, JSON.stringify(nb));
  const nextT = last.t + MIN;
  await post({ type: 'updateBar', v: 1, bar: { t: nextT, o: nb.c, h: nb.c + 0.1, l: nb.c - 0.1, c: nb.c, v: 50, feed: 'iex_tail', final: false } });
  await settle();
  check('updateBar newer t appends', (await dbg('count')) === nBefore + 1 && (await dbg('bar', -1)).t === nextT);
  const old = await dbg('bar', -10);
  await post({ type: 'updateBar', v: 1, bar: fixBar({ t: old.t, o: old.o, h: old.h + 1, l: old.l, c: old.c, v: (old.v || 0) + 999, feed: old.feed, final: old.final }) });
  await settle();
  nb = await dbg('bar', -10);
  check('updateBar with an OLDER existing t replaces that bar (late-trade correction)', nb.t === old.t && Math.abs(nb.h - (old.h + 1)) < 1e-9 && nb.v === (old.v || 0) + 999 && (await dbg('count')) === nBefore + 1, JSON.stringify(nb));
  const first = await dbg('bar', 0);
  await post({ type: 'updateBar', v: 1, bar: { t: first.t - 7 * 86400000, o: 1, h: 1, l: 1, c: 1, v: 1, feed: 'sip', final: true } });
  await settle();
  check('updateBar older and not loaded is ignored', (await dbg('count')) === nBefore + 1);

  // T6 tail -> SIP replacement (+ a missing minute inserted in order)
  const n6 = await dbg('count');
  const tailBars = [];
  for (let i = n6 - 16; i < n6; i++) { const b = await dbg('bar', i); if (b.feed === 'iex_tail') tailBars.push(b); }
  check('there are IEX tail bars before replacement', tailBars.length >= 15, tailBars.length);
  await post({ type: 'command', v: 1, name: 'resetView' });
  await settle();
  await page.screenshot({ path: path.join(SHOTS, '02_before_sip_replace.png') });
  const sip = tailBars.map(b => fixBar({ t: b.t, o: b.o, h: b.h + 0.02, l: b.l, c: b.c, v: (b.v || 0) + 10, feed: 'sip', final: true, s: b.s || 'post' }));
  await post({ type: 'replaceBars', v: 1, bars: sip });
  await settle();
  let tailLeft = 0;
  for (let i = n6 - 16; i < n6; i++) { if ((await dbg('bar', i)).feed === 'iex_tail') tailLeft++; }
  check('replaceBars turns the IEX tail into SIP finals', tailLeft === 0, tailLeft);
  legend = await dbg('legend');
  check('legend tail chip gone after SIP replacement', legend.indexOf('live tail') < 0 && legend.indexOf('SIP') >= 0, legend);
  // a missing minute: the overnight gap after 19:59 ET of a day (20:30 ET is never present)
  let gapIdx = -1;
  for (let i = 1000; i < 3000; i++) { const a = await dbg('bar', i), b = await dbg('bar', i + 1); if (b.t - a.t > 2 * MIN) { gapIdx = i; break; } }
  const g0 = await dbg('bar', gapIdx);
  const ins = { t: g0.t + 31 * MIN, o: g0.c, h: g0.c, l: g0.c, c: g0.c, v: 1, feed: 'sip', final: true };
  await post({ type: 'replaceBars', v: 1, bars: [ins] });
  await settle();
  check('replaceBars inserts a missing bar in time order', (await dbg('count')) === n6 + 1 && (await dbg('bar', gapIdx + 1)).t === ins.t, gapIdx);
  await page.screenshot({ path: path.join(SHOTS, '03_after_sip_replace.png') });

  // T7 last price colour + type switch
  await post({ type: 'setLastPrice', v: 1, price: 190.1, dayChangeSign: -1 });
  await settle();
  let st = await dbg('styles');
  check('last-price line coloured by day change (down)', st.candle.priceMark.last.upColor.toUpperCase() === THEME.down && st.candle.priceMark.last.show === true, JSON.stringify(st.candle.priceMark.last).slice(0, 200));
  await post({ type: 'setLastPrice', v: 1, price: null, dayChangeSign: null });
  await settle();
  st = await dbg('styles');
  check('setLastPrice price:null hides the line', st.candle.priceMark.last.show === false);
  await post({ type: 'setLastPrice', v: 1, price: 190.1, dayChangeSign: 1 });
  await post({ type: 'setType', v: 1, chartType: 'line' });
  await settle();
  st = await dbg('styles');
  check('setType line -> engine area type with transparent fill', st.candle.type === 'area' && (await dbg('state')).chartType === 'line');
  await page.screenshot({ path: path.join(SHOTS, '04_line.png') });
  await post({ type: 'setType', v: 1, chartType: 'area' });
  await settle();
  await page.screenshot({ path: path.join(SHOTS, '05_area.png') });
  await post({ type: 'setType', v: 1, chartType: 'candles' });
  await settle();
  st = await dbg('styles');
  check('setType candles -> candle_solid', st.candle.type === 'candle_solid');

  // T8 crosshair
  await clearOut();
  await page.mouse.move(600, 250);
  await page.waitForTimeout(120);
  o = (await out()).filter(m => m.type === 'crosshair');
  check('crosshair message with a bar on mouse move', o.length >= 1 && o[o.length - 1].bar && typeof o[o.length - 1].bar.t === 'number', JSON.stringify(o.slice(-1)));
  await page.screenshot({ path: path.join(SHOTS, '06_crosshair.png') });
  await page.mouse.move(5, 630);

  // T9 invalid messages
  const nInv = await dbg('count');
  const good = await dbg('bar', -1);
  const bad = [
    ['unknown type', { type: 'nope', v: 1 }, 'bad_message'],
    ['wrong version', { type: 'setType', v: 2, chartType: 'line' }, 'bad_message'],
    ['unknown field', { type: 'setType', v: 1, chartType: 'line', extra: 1 }, 'bad_message'],
    ['bad enum', { type: 'setType', v: 1, chartType: 'heikin' }, 'bad_message'],
    ['h < l', { type: 'updateBar', v: 1, bar: { t: good.t, o: 10, h: 9, l: 11, c: 10, v: 1, feed: 'sip', final: true } }, 'bad_message'],
    ['NaN-like price (string)', { type: 'updateBar', v: 1, bar: { t: good.t, o: '1', h: 1, l: 1, c: 1, v: 1, feed: 'sip', final: true } }, 'bad_message'],
    ['t not integer', { type: 'updateBar', v: 1, bar: { t: good.t + 0.5, o: 1, h: 1, l: 1, c: 1, v: 1, feed: 'sip', final: true } }, 'bad_message'],
    ['iex_tail final', { type: 'updateBar', v: 1, bar: { t: good.t, o: 1, h: 1, l: 1, c: 1, v: 1, feed: 'iex_tail', final: true } }, 'bad_message'],
    ['unknown feed', { type: 'updateBar', v: 1, bar: { t: good.t, o: 1, h: 1, l: 1, c: 1, v: 1, feed: 'yahoo', final: true } }, 'bad_message'],
    ['non-increasing t', { type: 'replaceBars', v: 1, bars: [good, good] }, 'bad_message'],
    ['bad colour', { type: 'init', v: 1, theme: Object.assign({}, THEME, { up: 'red' }), locale: 'en-US', tz: null }, 'bad_message'],
    ['bad tz', { type: 'init', v: 1, theme: THEME, locale: 'en-US', tz: 'Mars/Base' }, 'bad_message'],
    ['bad sign', { type: 'setLastPrice', v: 1, price: 1, dayChangeSign: 2 }, 'bad_message'],
    ['bad symbol', loadMsg([], { symbol: '<script>' }), 'bad_message'],
    ['too many bars', loadMsg(new Array(20001).fill(0).map((_, i) => ({ t: 1.7e12 + i * MIN, o: 1, h: 1, l: 1, c: 1, v: 1, feed: 'sip', final: true }))), 'bad_message'],
    ['not JSON string', 'this is {not json', 'bad_message'],
    ['array message', [1, 2, 3], 'bad_message'],
    ['bad drawing id (MR5 reserved)', { type: 'loadDrawings', v: 1, drawings: [{ id: 'x', tool: 'segment', points: [] }] }, 'bad_message'],
    ['bad command', { type: 'command', v: 1, name: 'rm' }, 'bad_message']
  ];
  for (const [name, msg, code] of bad) {
    await clearOut();
    await post(msg);
    await settle();
    const errs = await errorsSince();
    check('rejects ' + name + ' -> error ' + code, errs.some(e => e.code === code && typeof e.message === 'string' && e.message.length <= 300), JSON.stringify(errs).slice(0, 200));
  }
  await clearOut();
  await page.evaluate(() => window.postMessage('x'.repeat(4 * 1024 * 1024 + 10), '*'));
  await settle();
  check('rejects a > 4 MiB message -> too_large', (await errorsSince()).some(e => e.code === 'too_large'));
  check('state unchanged after invalid messages', (await dbg('count')) === nInv && (await dbg('state')).chartType === 'candles');
  await clearOut();
  await post({ type: 'setIndicators', v: 1, indicators: [], t: [], series: {} });
  await post({ type: 'setTool', v: 1, name: null });
  await post({ type: 'command', v: 1, name: 'resetView' });
  await settle();
  check('valid MR5 messages (empty indicators, cursor tool, resetView) accepted silently', (await errorsSince()).length === 0, JSON.stringify(await errorsSince()));

  // T10 CSP
  const csp = await page.evaluate(async () => {
    const r = {};
    try { await fetch('chart.css'); r.fetch = 'allowed'; } catch (e) { r.fetch = 'blocked'; }
    try { const x = new XMLHttpRequest(); x.open('GET', 'chart.css', false); x.send(); r.xhr = 'allowed'; } catch (e) { r.xhr = 'blocked'; }
    const p = document.createElement('script'); p.src = 'tests/csp_probe.js'; document.body.appendChild(p);
    await new Promise(res => { p.onload = res; p.onerror = res; });
    r.eval = window.__probe ? window.__probe.eval : 'probe missing';
    r.fn = window.__probe ? window.__probe.fn : 'probe missing';
    r.meta = document.querySelector('meta[http-equiv="Content-Security-Policy"]').getAttribute('content');
    window.__inline = false;
    const s = document.createElement('script'); s.textContent = 'window.__inline = true;'; document.body.appendChild(s);
    const img = new Image(); img.src = 'https://example.com/x.png'; document.body.appendChild(img);
    await new Promise(res => setTimeout(res, 150));
    r.inline = window.__inline ? 'ran' : 'blocked';
    r.violations = window.__csp.slice();
    return r;
  });
  check('CSP blocks fetch', csp.fetch === 'blocked', JSON.stringify(csp));
  check('CSP blocks XHR', csp.xhr === 'blocked');
  check('CSP blocks eval and new Function', csp.eval === 'blocked' && csp.fn === 'blocked', csp.eval + '/' + csp.fn);
  check('CSP blocks inline script', csp.inline === 'blocked');
  check('CSP meta is exactly SPEC §9', csp.meta === "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none'; font-src 'self'; base-uri 'none'; form-action 'none'", csp.meta);
  check('CSP violations reported (connect-src / img-src)', csp.violations.some(v => v.indexOf('connect-src') === 0) && csp.violations.some(v => v.indexOf('img-src') === 0), JSON.stringify(csp.violations));
  check('no request left loopback (blocked image never loaded)', external.length === 0, JSON.stringify(external));

  // T11 live-update throughput + 20k load + 1D + exchange time
  const upd = await page.evaluate(async (b) => {
    const t0 = performance.now();
    for (let i = 0; i < 200; i++) {
      const c = +(b.c + (i % 7) * 0.01).toFixed(2);
      window.postMessage({ type: 'updateBar', v: 1, bar: { t: b.t, o: b.o, h: Math.max(b.h, c), l: Math.min(b.l, c), c, v: b.v + i, feed: b.feed, final: false } }, '*');
    }
    await new Promise(r => setTimeout(r, 0));
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    return performance.now() - t0;
  }, await dbg('bar', -1));
  check('200 live updates processed < 1000 ms', upd < 1000, upd.toFixed(0) + ' ms');
  const ms20 = await page.evaluate(async (msg) => {
    const t0 = performance.now(); window.postMessage(msg, '*');
    await new Promise(r => setTimeout(r, 0)); await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    return performance.now() - t0;
  }, loadMsg(makeEquityBars(20000, { seed: 5 })));
  check('20k candles render < 2000 ms', ms20 < 2000 && (await dbg('count')) === 20000, ms20.toFixed(0) + ' ms');
  await post({ type: 'init', v: 1, theme: THEME, locale: 'en-US', tz: 'Europe/Berlin', timeMode: 'exchange' });
  await post(loadMsg(makeDailyBars(504), { tf: '1D', session: 'rth', sipThrough: null, symbol: 'NVDA' }));
  await post({ type: 'setLastPrice', v: 1, price: 1, dayChangeSign: 1 });
  await settle();
  legend = await dbg('legend');
  check('1D load + exchange-time label', (await dbg('count')) === 504 && legend.indexOf('Exchange time America/New_York') >= 0, legend);
  await page.screenshot({ path: path.join(SHOTS, '07_daily_exchange_time.png') });

  // extended-hours shading around an RTH close (16:00 ET) on a 1m ETH chart
  await post({ type: 'init', v: 1, theme: THEME, locale: 'en-US', tz: null, timeMode: 'exchange' });
  const shadeBars = makeEquityBars(3000, { seed: 9 });
  await post(loadMsg(shadeBars));
  await settle();
  let closeIdx = -1;
  for (let i = shadeBars.length - 1; i > 0; i--) { if (shadeBars[i].s === 'post' && shadeBars[i - 1].s === 'rth') { closeIdx = i; break; } }
  await dbg('scrollToIndex', Math.max(0, closeIdx - 70));
  await settle();
  await page.screenshot({ path: path.join(SHOTS, '08_eth_shading_rth_close.png') });
  check('session shading screenshot taken around an RTH close', closeIdx > 0, closeIdx);

  check('no console errors / page errors', consoleErrors.length === 0, JSON.stringify(consoleErrors).slice(0, 400));

  await runVariants(browser, base);
  await runMr5(browser, base);
  await runMarkers(browser, base);

  await browser.close();
  server.close();
  let failed = 0;
  for (const r of results) { if (!r.ok) failed++; console.log((r.ok ? 'PASS ' : 'FAIL ') + r.name + (r.detail && (!r.ok || /ms$/.test(r.detail)) ? '  [' + r.detail + ']' : '')); }
  console.log('\nengine=' + ENGINE + '  ' + (results.length - failed) + '/' + results.length + ' passed  screenshots: ' + SHOTS);
  process.exit(failed ? 1 : 0);
}

// Engine-behaviour variants (review M2/M3): the same reload/paging/live scenario against test
// doubles that behave like different plausible engine builds, plus API mismatches that must be
// reported as error engine_api instead of failing silently. With --engine real only the default
// scenario runs (the options are ignored by the real engine).
async function runVariants(browser, base) {
  const flat = (n, t0, p) => Array.from({ length: n }, (_, i) => ({ t: t0 + i * MIN, o: p, h: p + 1, l: p - 1, c: p, v: 10, feed: 'sip', final: true }));
  const T0 = Date.UTC(2026, 9, 15, 13, 30);
  const good = [
    ['default (10.0.3 semantics)', {}],
    ['resetData keeps the loading flag', { resetClearsLoading: false }],
    ['deferred getBars', { deferLoad: true }],
    ['deferred getBars + loading flag kept', { deferLoad: true, resetClearsLoading: false }],
    ['createIndicator returns pane id, pane from paneOptions, numeric getBarSpace', { returnPaneId: true, paneFrom: 'options', barSpaceNumber: true }],
    ['pane from value.paneId only', { paneFrom: 'value' }]
  ];
  const bad = [
    ['chart.getBarSpace missing', { dropChartMethods: ['getBarSpace'] }, /chart\.getBarSpace/, true],
    ['unknown style key', { dropStyleKey: 'candle.tooltip' }, /candle\.tooltip/, false],
    ['createIndicator ignores the pane', { paneFrom: 'none' }, /landed on pane/, false]
  ];
  const list = ENGINE === 'fake' ? good : good.slice(0, 1);
  for (const [name, opts] of list) {
    const page = await browser.newPage({ viewport: { width: 900, height: 500 } });
    await page.addInitScript((o) => { window.__fakeOpts = o; window.__out = []; window.addEventListener('ufoschart:out', e => window.__out.push(e.detail)); }, opts);
    await page.goto(base);
    await page.waitForFunction(() => window.__out.length > 0, null, { timeout: 5000 });
    const post = m => page.evaluate(x => window.postMessage(x, '*'), m);
    const wait = (ms) => page.waitForTimeout(ms || 80);
    const dbg = (f, a) => page.evaluate(([fn, x]) => window.__ufosChartDebug[fn](x), [f, a]);
    const out = () => page.evaluate(() => window.__out.slice());
    await post({ type: 'init', v: 1, theme: THEME, locale: 'en-US', tz: 'UTC' });
    await post(loadMsg(flat(300, T0, 100), { session: 'rth' }));
    await wait();
    await dbg('scrollToStart'); await wait(); await dbg('scrollToStart'); await wait();
    const nm1 = (await out()).filter(m => m.type === 'needMoreLeft').length;
    await page.evaluate(() => { window.__out.length = 0; });
    // 50 bars fit the view, so the engine asks for older bars right after this load.
    await post(loadMsg(flat(50, T0, 400), { symbol: 'MSFT', session: 'rth' }));
    await wait(500);
    const n = await dbg('count'), lastBar = await dbg('bar', -1), st = await dbg('state');
    check('[' + name + '] reload while needMoreLeft pending shows the NEW bars', nm1 === 1 && n === 50 && lastBar.c === 400 && st.symbol === 'MSFT', JSON.stringify({ nm1, n, c: lastBar && lastBar.c, sym: st.symbol }));
    await dbg('scrollToStart'); await wait();
    const nm2 = (await out()).filter(m => m.type === 'needMoreLeft');
    await post({ type: 'prependBars', v: 1, bars: flat(20, T0 - 20 * MIN, 399), hasMoreLeft: false });
    await wait();
    check('[' + name + '] paging works after the reload', nm2.length === 1 && nm2[0].beforeTs === T0 && (await dbg('count')) === 70, JSON.stringify(nm2) + ' count=' + (await dbg('count')));
    await post({ type: 'updateBar', v: 1, bar: { t: T0 + 50 * MIN, o: 400, h: 402, l: 399, c: 401, v: 5, feed: 'iex_tail', final: false } });
    await post({ type: 'updateBar', v: 1, bar: { t: T0 + 10 * MIN, o: 400, h: 405, l: 399, c: 404, v: 9, feed: 'sip', final: true } });
    await wait();
    const b10 = await dbg('bar', 30);
    check('[' + name + '] live append + older-bar replace', (await dbg('count')) === 71 && b10.t === T0 + 10 * MIN && b10.c === 404, JSON.stringify(b10));
    const errs = (await out()).filter(m => m.type === 'error');
    check('[' + name + '] no error messages', errs.length === 0, JSON.stringify(errs).slice(0, 300));
    await page.close();
  }
  if (ENGINE !== 'fake') return;
  for (const [name, opts, re, fatal] of bad) {
    const page = await browser.newPage({ viewport: { width: 900, height: 500 } });
    await page.addInitScript((o) => { window.__fakeOpts = o; window.__out = []; window.addEventListener('ufoschart:out', e => window.__out.push(e.detail)); }, opts);
    await page.goto(base);
    await page.waitForTimeout(300);
    const o = await page.evaluate(() => window.__out.slice());
    const err = o.find(m => m.type === 'error' && m.code === 'engine_api' && re.test(m.message));
    const ready = o.some(m => m.type === 'ready');
    check('[' + name + '] reported as error engine_api' + (fatal ? ' (fatal, no ready)' : ' (page still ready)'), !!err && ready === !fatal, JSON.stringify(o).slice(0, 300));
    await page.close();
  }
}

// ------------------------------------------------------------------ MR5: drawings + indicators
// Bridge revision 2 (SPEC "MR5 bridge contract"). Interactive drawing is completed through the
// test double's hooks (__finishDrawing/__move/__select); with --engine real only the parts that do
// not need those hooks run (schema, indicators, loadDrawings, timeframe filter).
const TOOL_N = { segment: 2, rayLine: 2, straightLine: 2, horizontalStraightLine: 1, horizontalRayLine: 1, verticalStraightLine: 1,
  priceLine: 1, parallelStraightLine: 3, priceChannelLine: 3, fibonacciLine: 2, rect: 2, measure: 2, simpleAnnotation: 1,
  simpleTag: 1, arrowUp: 1, arrowDown: 1, brush: -1, alertLine: 1 };
const CUSTOM = { fibonacciLine: 'ufosFib', rect: 'ufosRect', measure: 'ufosMeasure', arrowUp: 'ufosArrowUp', arrowDown: 'ufosArrowDown', brush: 'ufosBrush', alertLine: 'ufosAlertLine' };
let hexSeq = 0;
const hex32 = () => (++hexSeq).toString(16).padStart(32, 'a');
function wireDrawing(tool, pts, extra) {
  return Object.assign({ tool, points: pts, style: { color: 'accent', width: 1, dash: 'solid', levels: null }, name: null,
    text: tool === 'simpleAnnotation' ? 'note' : null, locked: false, visible: true, timeframes: 'all', alert_id: null }, extra || {});
}
function sma(vals, n) { const out = []; let s = 0; for (let i = 0; i < vals.length; i++) { s += vals[i]; if (i >= n) s -= vals[i - n]; out.push(i >= n - 1 ? +(s / n).toFixed(4) : null); } return out; }

async function runMr5(browser, base) {
  const fake = ENGINE === 'fake';
  const page = await browser.newPage({ viewport: { width: 1180, height: 700 } });
  const errorsLog = [];
  page.on('pageerror', e => errorsLog.push(e.message));
  await page.addInitScript(() => { window.__out = []; window.addEventListener('ufoschart:out', e => window.__out.push(e.detail)); });
  await page.goto(base);
  await page.waitForFunction(() => window.__out.length > 0, null, { timeout: 5000 });
  const post = m => page.evaluate(x => window.postMessage(x, '*'), m);
  const out = () => page.evaluate(() => window.__out.slice());
  const clear = () => page.evaluate(() => { window.__out.length = 0; });
  const dbg = (f, a) => page.evaluate(([fn, x]) => window.__ufosChartDebug[fn](x), [f, a]);
  const fk = (f, ...a) => page.evaluate(([fn, args]) => window.__fakeChart[fn](...args), [f, a]);
  const settle = async () => { await page.waitForTimeout(40); await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))); };
  const msgs = async (type) => (await out()).filter(m => m.type === type);
  const errs = async () => (await out()).filter(m => m.type === 'error');

  const ready = (await out()).find(m => m.type === 'ready');
  check('MR5: ready carries revision >= 3', ready && ready.revision >= 3, JSON.stringify(ready));
  check('MR5: no engine_api error at start (test double)', !(await errs()).length || !fake, JSON.stringify(await errs()));

  await post({ type: 'init', v: 1, theme: THEME, locale: 'en-US', tz: 'UTC' });
  const bars = makeEquityBars(3000, { seed: 21, tail: 0 });
  await post(loadMsg(bars, { hasMoreLeft: false }));
  await settle();
  const n = bars.length, T = i => bars[n + i].t, Pc = i => bars[n + i].c;

  // ---- schema rejections
  const base1 = wireDrawing('segment', [{ t: T(-40), p: Pc(-40) }, { t: T(-10), p: Pc(-10) }], { id: hex32() });
  const rej = [
    ['brush with 501 points', [Object.assign(wireDrawing('brush', Array.from({ length: 501 }, (_, i) => ({ t: T(-600) + i * MIN, p: 100 }))), { id: hex32() })]],
    ['segment with 3 points', [Object.assign({}, base1, { points: base1.points.concat([{ t: T(-5), p: 1 }]) })]],
    ['levels on a segment', [Object.assign({}, base1, { style: { color: 'accent', width: 1, dash: 'solid', levels: [50] } })]],
    ['text of 281 chars', [Object.assign({}, base1, { text: 'x'.repeat(281) })]],
    ['control char in name', [Object.assign({}, base1, { name: 'a\u0007b' })]],
    ['alert_id on a segment', [Object.assign({}, base1, { alert_id: hex32() })]],
    ['note without text', [Object.assign(wireDrawing('simpleAnnotation', [{ t: T(-5), p: 1 }]), { id: hex32(), text: null })]],
    ['unknown palette colour', [Object.assign({}, base1, { style: { color: 'pink', width: 1, dash: 'solid', levels: null } })]],
    ['width 5', [Object.assign({}, base1, { style: { color: 'accent', width: 5, dash: 'solid', levels: null } })]],
    ['unknown timeframe', [Object.assign({}, base1, { timeframes: ['2h'] })]],
    ['missing key', [(() => { const d = Object.assign({}, base1); delete d.visible; return d; })()]],
    ['unknown tool', [Object.assign({}, base1, { tool: 'pitchfork' })]],
    ['price 0', [Object.assign({}, base1, { points: [{ t: T(-40), p: 0 }, { t: T(-10), p: 1 }] })]]
  ];
  for (const [name, list] of rej) {
    await clear();
    await post({ type: 'loadDrawings', v: 1, drawings: list });
    await settle();
    check('MR5 schema: loadDrawings rejects ' + name, (await errs()).some(e => e.code === 'bad_message'), JSON.stringify(await errs()).slice(0, 160));
  }
  const otherRej = [
    ['drawingSaved with mismatched ids', { type: 'drawingSaved', v: 1, localId: 'c-1', id: hex32(), drawing: base1 }],
    ['setTool unknown tool', { type: 'setTool', v: 1, name: 'pitchfork' }],
    ['setMagnet bad mode', { type: 'setMagnet', v: 1, mode: 'medium' }],
    ['editDrawing empty changes', { type: 'editDrawing', v: 1, id: hex32(), changes: {} }],
    ['editDrawing points (not editable from the host)', { type: 'editDrawing', v: 1, id: hex32(), changes: { points: [] } }],
    ['bad localId', { type: 'drawingRejected', v: 1, op: 'create', localId: 'x-1', id: null, message: 'no' }]
  ];
  for (const [name, msg] of otherRej) {
    await clear(); await post(msg); await settle();
    check('MR5 schema: rejects ' + name, (await errs()).some(e => e.code === 'bad_message'), JSON.stringify(await errs()).slice(0, 160));
  }
  check('MR5 schema: nothing loaded after rejections', (await dbg('drawings')).length === 0);
  await clear();
  const brush500 = Object.assign(wireDrawing('brush', Array.from({ length: 500 }, (_, i) => ({ t: T(-500) + i * MIN, p: 100 + (i % 7) }))), { id: hex32() });
  await post({ type: 'loadDrawings', v: 1, drawings: [brush500] });
  await settle();
  check('MR5 schema: brush with 500 points accepted', (await dbg('drawings')).length === 1 && !(await errs()).length, JSON.stringify(await errs()).slice(0, 160));

  // ---- timeframe filter (all on 1m; [1D] only on 1D) + pass-through backend keys
  const dAll = Object.assign(wireDrawing('horizontalStraightLine', [{ t: T(-30), p: Pc(-30) }]), { id: hex32(), symbol: 'AAPL', price_basis: { as_of: '2026-10-15', factor: 1, rescales: 0 }, created_at: '2026-10-15T14:31:02+00:00', updated_at: '2026-10-15T14:31:02+00:00' });
  const dDay = Object.assign(wireDrawing('segment', [{ t: T(-300), p: Pc(-300) }, { t: T(-20), p: Pc(-20) }]), { id: hex32(), timeframes: ['1D'] });
  await clear();
  await post({ type: 'loadDrawings', v: 1, drawings: [dAll, dDay] });
  await settle();
  let ds = await dbg('drawings');
  const ovOf = (id) => ds.find(d => d.id === id);
  check('MR5 timeframes: "all" drawn on 1m, [1D] not', ovOf(dAll.id).overlayId && !ovOf(dDay.id).overlayId && !(await errs()).length, JSON.stringify(ds.map(d => [d.tool, !!d.overlayId])));
  await post(loadMsg(makeDailyBars(504), { tf: '1D', session: 'rth', sipThrough: null }));
  await settle();
  ds = await dbg('drawings');
  check('MR5 timeframes: same symbol on 1D shows both (anchored by time + price)', ovOf(dAll.id).overlayId && ovOf(dDay.id).overlayId, JSON.stringify(ds.map(d => [d.tool, !!d.overlayId])));
  await post(loadMsg(bars, { hasMoreLeft: false }));
  await settle();

  if (fake) {
    // ---- every tool: setTool -> draw -> drawingCreated -> drawingSaved
    await post({ type: 'loadDrawings', v: 1, drawings: [] });
    await settle();
    const saved = {};
    for (const tool of Object.keys(TOOL_N)) {
      await clear();
      await post({ type: 'setTool', v: 1, name: tool, style: { color: 'violet', width: 2, dash: 'dashed' } });
      await settle();
      if (tool === 'brush') {
        await page.mouse.move(400, 200); await page.mouse.down();
        for (let i = 1; i <= 30; i++) await page.mouse.move(400 + i * 6, 200 + Math.round(20 * Math.sin(i / 4)));
        await page.mouse.up();
      } else {
        const k = TOOL_N[tool];
        const pts = Array.from({ length: k }, (_, i) => ({ timestamp: T(-60 + i * 20), value: Pc(-60 + i * 20) + (i === 2 ? 0.5 : 0) }));
        await fk('__finishDrawing', pts);
      }
      await settle();
      if (tool === 'simpleAnnotation') {
        await page.fill('#note-editor', 'Buy zone <img src=x onerror=alert(1)> & "quotes"');
        await page.press('#note-editor', 'Enter');
        await settle();
      }
      if (tool === 'measure') {
        const before = await msgs('drawingCreated');
        check('MR5 tools: measure is temporary (nothing sent before save)', before.length === 0, JSON.stringify(before));
        await post({ type: 'command', v: 1, name: 'saveMeasure' });
        await settle();
      }
      const created = await msgs('drawingCreated');
      const tc = await msgs('toolChanged');
      const c = created[0];
      const okCount = c && (TOOL_N[tool] === -1 ? c.drawing.points.length >= 2 && c.drawing.points.length <= 500 : c.drawing.points.length === TOOL_N[tool]);
      check('MR5 tools: ' + tool + ' -> drawingCreated (localId, points, style, back to cursor)',
        created.length === 1 && /^c-\d+$/.test(c.localId) && c.drawing.tool === tool && okCount && c.drawing.style.color === 'violet' && c.drawing.style.width === 2 &&
        c.drawing.points.every(q => Number.isInteger(q.t) && q.p > 0) && tc.some(m => m.name === null),
        JSON.stringify(created).slice(0, 240));
      if (!c) continue;
      const id = hex32();
      await post({ type: 'drawingSaved', v: 1, localId: c.localId, id, drawing: Object.assign({ id }, c.drawing) });
      await settle();
      const rec = (await dbg('drawings')).find(d => d.id === id);
      const ov = rec && (await fk('__overlays')).find(o => o.id === rec.overlayId);
      check('MR5 tools: ' + tool + ' re-keyed to the server id and drawn as ' + (CUSTOM[tool] || tool), rec && ov && ov.name === (CUSTOM[tool] || tool), JSON.stringify(rec).slice(0, 200));
      saved[tool] = { id, drawing: c.drawing, overlayId: rec && rec.overlayId };
    }
    const note = saved.simpleAnnotation;
    check('MR5 text note: stored verbatim as plain text, no HTML element created',
      note && note.drawing.text === 'Buy zone <img src=x onerror=alert(1)> & "quotes"' && (await page.evaluate(() => document.querySelectorAll('img').length)) === 0, note && note.drawing.text);
    await page.screenshot({ path: path.join(SHOTS, '10_mr5_all_tools.png') });

    // ---- move -> drawingChanged; undo/redo via keyboard
    const seg = saved.segment;
    await clear();
    const moved = [{ timestamp: T(-90), value: Pc(-90) }, { timestamp: T(-15), value: Pc(-15) + 1 }];
    await fk('__move', seg.overlayId, moved);
    await settle();
    let ch = await msgs('drawingChanged');
    check('MR5 move: drawingChanged {id, changes.points, final:true}', ch.length === 1 && ch[0].id === seg.id && ch[0].final === true && ch[0].changes.points.length === 2 && ch[0].changes.points[0].t === T(-90) && Object.keys(ch[0].changes).join() === 'points', JSON.stringify(ch));
    await clear();
    await page.keyboard.press('Control+z');
    await settle();
    ch = await msgs('drawingChanged');
    check('MR5 undo (Ctrl+Z): points restored and persisted', ch.length === 1 && ch[0].changes.points[0].t === seg.drawing.points[0].t, JSON.stringify(ch).slice(0, 200));
    await clear();
    await page.keyboard.press('Control+y');
    await settle();
    ch = await msgs('drawingChanged');
    const us = await msgs('undoState');
    check('MR5 redo (Ctrl+Y): move re-applied + undoState sent', ch.length === 1 && ch[0].changes.points[0].t === T(-90) && us.length >= 1 && us[us.length - 1].canUndo === true, JSON.stringify(ch).slice(0, 120));

    // ---- select + Delete key, undo -> re-created with a new localId
    await clear();
    const rect = saved.rect;
    let rrec = (await dbg('drawings')).find(d => d.id === rect.id);
    await fk('__select', rrec.overlayId);
    await settle();
    let sel = await msgs('selectionChanged');
    check('MR5 select: selectionChanged {id, drawing}', sel.length && sel[sel.length - 1].id === rect.id && sel[sel.length - 1].drawing.tool === 'rect', JSON.stringify(sel).slice(0, 200));
    await page.keyboard.press('Delete');
    await settle();
    let del = await msgs('drawingDeleted');
    check('MR5 Delete key: drawingDeleted {id}', del.length === 1 && del[0].id === rect.id, JSON.stringify(del));
    await clear();
    await page.keyboard.press('Control+z');
    await settle();
    let cr = await msgs('drawingCreated');
    check('MR5 undo delete: drawingCreated again with a new localId', cr.length === 1 && cr[0].drawing.tool === 'rect' && cr[0].localId !== undefined, JSON.stringify(cr).slice(0, 200));
    const rect2 = hex32();
    await post({ type: 'drawingSaved', v: 1, localId: cr[0].localId, id: rect2, drawing: Object.assign({ id: rect2 }, cr[0].drawing) });
    await settle();

    // ---- lock (selected), engine refuses to move it; lockAll/unlockAll as one undo step
    await clear();
    const hl = saved.horizontalStraightLine;
    let hrec = (await dbg('drawings')).find(d => d.id === hl.id);
    await fk('__select', hrec.overlayId);
    await post({ type: 'command', v: 1, name: 'lock' });
    await settle();
    ch = await msgs('drawingChanged');
    hrec = (await dbg('drawings')).find(d => d.id === hl.id);
    const ovl = (await fk('__overlays')).find(o => o.id === hrec.overlayId);
    const movedLocked = await fk('__move', hrec.overlayId, [{ timestamp: T(-5), value: 1 }]);
    check('MR5 lock: drawingChanged {locked:true}, overlay locked, move refused', ch.length === 1 && ch[0].changes.locked === true && ovl.lock === true && movedLocked === false, JSON.stringify(ch));
    await clear();
    await post({ type: 'command', v: 1, name: 'lockAll' });
    await settle();
    ch = await msgs('drawingChanged');
    const live = (await dbg('drawings')).filter(d => !d.deleted && !d.temp);
    check('MR5 lockAll: every unlocked drawing changed, all locked', ch.length === live.length - 1 && (await dbg('drawings')).filter(d => !d.deleted).every(d => d.locked), ch.length + ' of ' + live.length);
    await clear();
    await post({ type: 'command', v: 1, name: 'undo' });
    await settle();
    ch = await msgs('drawingChanged');
    check('MR5 undo lockAll: one step unlocks the same drawings', ch.length === live.length - 1 && ch.every(m => m.changes.locked === false), String(ch.length));

    // ---- magnet, hide/show (view only)
    await clear();
    await post({ type: 'setMagnet', v: 1, mode: 'strong' });
    await settle();
    let ovs = await fk('__overlays');
    check('MR5 magnet: strong -> every overlay mode strong_magnet', ovs.length > 0 && ovs.every(o => o.mode === 'strong_magnet'), JSON.stringify(ovs.map(o => o.mode)));
    await post({ type: 'setTool', v: 1, name: 'segment' });
    await settle();
    const drawingId = await fk('__drawingId');
    ovs = await fk('__overlays');
    check('MR5 magnet: a new drawing starts in strong_magnet mode', ovs.find(o => o.id === drawingId).mode === 'strong_magnet');
    await page.keyboard.press('Escape');
    await settle();
    check('MR5 Esc: drawing cancelled, toolChanged null', (await fk('__drawingId')) === null && (await msgs('toolChanged')).some(m => m.name === null));
    await clear();
    await post({ type: 'command', v: 1, name: 'hideAll' });
    await settle();
    ovs = await fk('__overlays');
    check('MR5 hideAll: all overlays invisible, nothing persisted', ovs.every(o => !o.visible) && !(await msgs('drawingChanged')).length);
    await page.screenshot({ path: path.join(SHOTS, '11_mr5_hidden.png') });
    await post({ type: 'command', v: 1, name: 'showAll' });
    await post({ type: 'setMagnet', v: 1, mode: 'off' });
    await settle();
    check('MR5 showAll: visible again', (await fk('__overlays')).every(o => o.visible));

    // ---- host edits through the page (undoable echo), remove, unknown id
    await clear();
    await post({ type: 'editDrawing', v: 1, id: seg.id, changes: { style: { color: 'caution', width: 3 }, name: 'Trend A', timeframes: ['1m', '5m'] } });
    await settle();
    ch = await msgs('drawingChanged');
    check('MR5 editDrawing: echoed as drawingChanged (merged style, name, timeframes)', ch.length === 1 && ch[0].changes.style.color === 'caution' && ch[0].changes.style.width === 3 && ch[0].changes.style.dash === 'dashed' && ch[0].changes.name === 'Trend A' && ch[0].changes.timeframes.join() === '1m,5m', JSON.stringify(ch));
    await clear();
    await post({ type: 'removeDrawing', v: 1, id: saved.priceLine.id });
    await post({ type: 'editDrawing', v: 1, id: hex32(), changes: { name: 'x' } });
    await settle();
    check('MR5 removeDrawing echoed; unknown id -> drawing_unknown', (await msgs('drawingDeleted')).some(m => m.id === saved.priceLine.id) && (await errs()).some(e => e.code === 'drawing_unknown'));

    // ---- rejections revert
    await clear();
    await post({ type: 'setTool', v: 1, name: 'segment' });
    await fk('__finishDrawing', [{ timestamp: T(-8), value: Pc(-8) }, { timestamp: T(-3), value: Pc(-3) }]);
    await settle();
    cr = await msgs('drawingCreated');
    await post({ type: 'drawingRejected', v: 1, op: 'create', localId: cr[0].localId, id: null, message: 'At most 500 drawings per symbol.' });
    await settle();
    check('MR5 drawingRejected(create): drawing removed', !(await dbg('drawings')).some(d => d.localId === cr[0].localId) && !(await fk('__overlays')).some(o => o.id === 'ufos_' + cr[0].localId));
    // revision 3 (M4): width 1 -> 2 -> 3, the 3rd PATCH is refused; the page keeps its state until the
    // host's upsertDrawing (authoritative: the state before the refused step) and never shows 2 by itself
    const segNow = (await dbg('drawings')).find(d => d.id === seg.id);
    const base = Object.assign({ id: seg.id }, { tool: 'segment', points: segNow.points, style: Object.assign({}, segNow.style, { width: 1 }), name: 'Trend A', text: null, locked: false, visible: true, timeframes: ['1m', '5m'], alert_id: null, client_id: '3f2b8a52-1c3d-4e5f-8a9b-0c1d2e3f4a5b' });
    await post({ type: 'upsertDrawing', v: 1, drawing: base });
    await post({ type: 'editDrawing', v: 1, id: seg.id, changes: { style: { width: 2 } } });
    await post({ type: 'editDrawing', v: 1, id: seg.id, changes: { style: { width: 3 } } });
    await settle();
    const undoBefore = (await dbg('drawingState')).undo;
    await clear();
    await post({ type: 'drawingRejected', v: 1, op: 'change', localId: null, id: seg.id, message: 'The drawing was changed at the same time.' });
    await settle();
    const afterReject = (await dbg('drawings')).find(d => d.id === seg.id).style.width;
    const undoAfterReject = (await dbg('drawingState')).undo;
    await post({ type: 'upsertDrawing', v: 1, drawing: Object.assign({}, base, { style: Object.assign({}, base.style, { width: 2 }) }) });
    await settle();
    const afterUpsert = (await dbg('drawings')).find(d => d.id === seg.id);
    check('M4 rejected change: no local revert to an intermediate state (stays 3 until the host answers)', afterReject === 3, afterReject);
    check('M4 rejected change: that drawing\'s undo entries dropped', undoAfterReject === undoBefore - 2, undoBefore + ' -> ' + undoAfterReject);
    check('M4 upsertDrawing after the rejection is authoritative, no echo', afterUpsert.style.width === 2 && afterUpsert.overlayId && !(await msgs('drawingChanged')).length, JSON.stringify(afterUpsert.style));
    check('rev3: backend drawing with client_id accepted', !(await errs()).length, JSON.stringify(await errs()));
    await post({ type: 'removeDrawing', v: 1, id: saved.segment.id });
    await post({ type: 'drawingRejected', v: 1, op: 'delete', localId: null, id: saved.segment.id, message: 'locked' });
    await settle();
    const back = (await dbg('drawings')).find(d => d.id === saved.segment.id);
    check('MR5 drawingRejected(delete): drawing restored', back && !back.deleted && back.overlayId);

    // ---- changes before the POST answer are queued; delete before the answer follows it
    await clear();
    await post({ type: 'setTool', v: 1, name: 'segment' });
    await fk('__finishDrawing', [{ timestamp: T(-12), value: Pc(-12) }, { timestamp: T(-4), value: Pc(-4) }]);
    await settle();
    cr = await msgs('drawingCreated');
    let q = (await dbg('drawings')).find(d => d.localId === cr[0].localId);
    await fk('__move', q.overlayId, [{ timestamp: T(-14), value: Pc(-14) }, { timestamp: T(-4), value: Pc(-4) }]);
    await settle();
    const early = await msgs('drawingChanged');
    const qid = hex32();
    await post({ type: 'drawingSaved', v: 1, localId: cr[0].localId, id: qid, drawing: Object.assign({ id: qid }, cr[0].drawing) });
    await settle();
    ch = await msgs('drawingChanged');
    check('MR5 queue: change waits for drawingSaved, then sent with the server id', early.length === 0 && ch.length === 1 && ch[0].id === qid && ch[0].changes.points[0].t === T(-14), JSON.stringify(ch).slice(0, 160));
    await clear();
    await post({ type: 'setTool', v: 1, name: 'segment' });
    await fk('__finishDrawing', [{ timestamp: T(-12), value: Pc(-12) }, { timestamp: T(-4), value: Pc(-4) }]);
    await settle();
    cr = await msgs('drawingCreated');
    await page.keyboard.press('Delete');
    await settle();
    const delEarly = await msgs('drawingDeleted');
    const did = hex32();
    await post({ type: 'drawingSaved', v: 1, localId: cr[0].localId, id: did, drawing: Object.assign({ id: did }, cr[0].drawing) });
    await settle();
    del = await msgs('drawingDeleted');
    check('MR5 queue: delete before drawingSaved -> drawingDeleted right after it', delEarly.length === 0 && del.length === 1 && del[0].id === did, JSON.stringify(del));

    // ---- revision 3 (H2): undo/redo sequences while the POST is pending (e.g. offline save queue)
    const drawSeg = async (a, b) => {
      await post({ type: 'setTool', v: 1, name: 'segment' });
      await fk('__finishDrawing', [{ timestamp: T(a), value: Pc(a) }, { timestamp: T(b), value: Pc(b) }]);
      await settle();
    };
    const key = async (k) => { await page.keyboard.press(k); await settle(); };
    const saveAnswer = async (c, id) => { await post({ type: 'drawingSaved', v: 1, localId: c.localId, id, drawing: Object.assign({ id, client_id: c.clientId }, c.drawing) }); await settle(); };
    // (a) create, undo, redo, undo, redo, then the answer: one create, no delete, no change
    await clear();
    await drawSeg(-33, -27);
    await key('Control+z'); await key('Control+y'); await key('Control+z'); await key('Control+y');
    let c1 = await msgs('drawingCreated');
    const uuidRe = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    check('H2 (a): undo/redo before the answer -> exactly ONE drawingCreated with a UUID v4 clientId', c1.length === 1 && uuidRe.test(c1[0].clientId), JSON.stringify(c1.map(m => [m.localId, m.clientId])));
    const a1 = hex32();
    await saveAnswer(c1[0], a1);
    let rec1 = (await dbg('drawings')).find(d => d.id === a1);
    check('H2 (a): after drawingSaved: drawn, no delete, no change', rec1 && rec1.overlayId && !(await msgs('drawingDeleted')).length && !(await msgs('drawingChanged')).length, JSON.stringify(await out()).slice(0, 200));
    // (b) create, undo, answer -> drawingDeleted for it; redo afterwards = a NEW create (new localId + clientId)
    await clear();
    await drawSeg(-35, -29);
    await key('Control+z');
    let c2 = await msgs('drawingCreated');
    const b1 = hex32();
    await saveAnswer(c2[0], b1);
    let d2 = await msgs('drawingDeleted');
    check('H2 (b): undone before the answer -> drawingDeleted {id} right after drawingSaved', c2.length === 1 && d2.length === 1 && d2[0].id === b1, JSON.stringify(d2));
    await key('Control+y');
    let c3 = await msgs('drawingCreated');
    check('H2 (b): redo after the answer -> a new create with a new localId and clientId', c3.length === 2 && c3[1].localId !== c2[0].localId && c3[1].clientId !== c2[0].clientId && uuidRe.test(c3[1].clientId), JSON.stringify(c3.map(m => [m.localId, m.clientId])));
    // redo-create pending again: undo -> answer -> deleted; nothing else
    await key('Control+z');
    const b2 = hex32();
    await saveAnswer(c3[1], b2);
    d2 = await msgs('drawingDeleted');
    check('H2 (b): the re-created one undone before its answer is deleted too (no stored copy left)', d2.length === 2 && d2[1].id === b2, JSON.stringify(d2));
    // (c) create, move, undo move, undo create, redo create, redo move, answer -> one create + one change (the move)
    await clear();
    await drawSeg(-41, -37);
    let c4 = await msgs('drawingCreated');
    let r4 = (await dbg('drawings')).find(d => d.localId === c4[0].localId);
    await fk('__move', r4.overlayId, [{ timestamp: T(-44), value: Pc(-44) }, { timestamp: T(-37), value: Pc(-37) }]);
    await settle();
    await key('Control+z'); await key('Control+z'); await key('Control+y'); await key('Control+y');
    const early4 = (await msgs('drawingChanged')).length + (await msgs('drawingDeleted')).length;
    const c4b = await msgs('drawingCreated');
    const a4 = hex32();
    await saveAnswer(c4[0], a4);
    let ch4 = await msgs('drawingChanged');
    check('H2 (c): move + undo/redo before the answer -> 1 create, then ONE drawingChanged with the moved points',
      c4b.length === 1 && early4 === 0 && ch4.length === 1 && ch4[0].id === a4 && ch4[0].changes.points[0].t === T(-44) && Object.keys(ch4[0].changes).join() === 'points', JSON.stringify(ch4));
    // (d) offline: three creates, churn, answers late and out of order
    await clear();
    await drawSeg(-60, -55); await drawSeg(-54, -50); await drawSeg(-49, -45);
    await key('Control+z'); await key('Control+z'); await key('Control+y');   // 3rd undone, 2nd undone+redone
    let c5 = await msgs('drawingCreated');
    const ids5 = [hex32(), hex32(), hex32()];
    await saveAnswer(c5[2], ids5[2]); await saveAnswer(c5[0], ids5[0]); await saveAnswer(c5[1], ids5[1]);
    const d5 = await msgs('drawingDeleted'), ch5 = await msgs('drawingChanged');
    const live5 = (await dbg('drawings')).filter(d => ids5.indexOf(d.id) >= 0 && !d.deleted);
    check('H2 (d): offline churn -> 3 creates, exactly the undone one deleted, 2 live, no changes', c5.length === 3 && d5.length === 1 && d5[0].id === ids5[2] && live5.length === 2 && !ch5.length, JSON.stringify({ c: c5.length, d: d5, live: live5.length, ch: ch5.length }));
    check('H2: clientIds unique per create', new Set(c5.map(m => m.clientId)).size === 3);
    // (e) rejected delete -> restored as before the delete
    await clear();
    const keepId = ids5[0];
    await post({ type: 'removeDrawing', v: 1, id: keepId });
    await settle();
    await post({ type: 'drawingRejected', v: 1, op: 'delete', localId: null, id: keepId, message: 'The drawing is locked - unlock it first.' });
    await settle();
    const restored = (await dbg('drawings')).find(d => d.id === keepId);
    check('M4 rejected delete: restored (pre-delete state), history entry dropped', restored && !restored.deleted && restored.overlayId && restored.points[0].t === T(-60), JSON.stringify(restored).slice(0, 160));

    // ---- note edit by double click (undoable), shortcuts
    await clear();
    const nrec = (await dbg('drawings')).find(d => d.id === note.id);
    await fk('__doubleClick', nrec.overlayId);
    await settle();
    await page.fill('#note-editor', 'Edited\nsecond line');
    await page.press('#note-editor', 'Enter');
    await settle();
    ch = await msgs('drawingChanged');
    check('MR5 note: double click edits text (newline kept)', ch.length === 1 && ch[0].changes.text === 'Edited\nsecond line', JSON.stringify(ch));
    await clear();
    await page.keyboard.press('Alt+t');
    await settle();
    let tcs = await msgs('toolChanged');
    const st1 = await dbg('drawingState');
    await page.keyboard.press('Alt+f'); await settle();
    await page.keyboard.press('Alt+r'); await settle();
    await page.keyboard.press('Alt+h'); await settle();
    tcs = await msgs('toolChanged');
    await page.keyboard.press('Escape'); await settle();
    await page.keyboard.press('Control+Shift+k'); await settle();
    await page.keyboard.press('Control+k'); await settle();
    const sc = await msgs('shortcut');
    check('MR5 shortcuts: Alt+T/F/R/H -> toolChanged, Ctrl+K forwarded (Ctrl+Shift+K not)', st1.tool === 'segment' && tcs.map(m => m.name).join() === 'segment,fibonacciLine,rect,horizontalStraightLine' && sc.length === 1 && sc[0].key === 'ctrl+k', JSON.stringify(tcs.map(m => m.name)) + JSON.stringify(sc));

    // ---- history cap 50
    for (let i = 0; i < 55; i++) await post({ type: 'editDrawing', v: 1, id: seg.id, changes: { style: { width: 1 + (i % 4) } } });
    await settle();
    check('MR5 undo history capped at 50 steps', (await dbg('drawingState')).undo === 50, (await dbg('drawingState')).undo);

    // ---- upsertDrawing: a new drawing is added silently; for a drawing with history only ITS entries go
    await clear();
    await post({ type: 'upsertDrawing', v: 1, drawing: Object.assign({}, dAll, { points: [{ t: T(-25), p: Pc(-25) }] }) });
    await settle();
    const undoKept = (await dbg('drawingState')).undo;
    const segState = (await dbg('drawings')).find(d => d.id === seg.id);
    await post({ type: 'upsertDrawing', v: 1, drawing: { id: seg.id, tool: 'segment', points: segState.points, style: segState.style, name: 'Trend A', text: null, locked: false, visible: true, timeframes: 'all', alert_id: null } });
    await settle();
    let ust = await msgs('undoState');
    check('MR5 upsertDrawing: applied silently; other history kept, that drawing\'s entries dropped', undoKept === 50 && !(await msgs('drawingChanged')).length && ust.length && ust[ust.length - 1].canUndo === false && (await dbg('drawings')).find(d => d.id === seg.id).timeframes === 'all', undoKept + ' ' + JSON.stringify(ust.slice(-1)));
  }

  // ---- indicators (backend series; works with both engines)
  await clear();
  const tArr = bars.map(b => b.t), closes = bars.map(b => b.c);
  const s20 = sma(closes, 20), s50 = sma(closes, 50);
  const rsi = closes.map((c, i) => i < 14 ? null : 30 + ((i * 7) % 40));
  const macd = closes.map((c, i) => i < 26 ? null : +(Math.sin(i / 30)).toFixed(4));
  const sig = macd.map((v, i) => v === null || i < 34 ? null : +(v * 0.8).toFixed(4));
  const hist = macd.map((v, i) => (v === null || sig[i] === null) ? null : +(v - sig[i]).toFixed(4));
  const bbMid = s20, bbUp = s20.map(v => v === null ? null : +(v + 0.6).toFixed(4)), bbLo = s20.map(v => v === null ? null : +(v - 0.6).toFixed(4));
  const indMsg = {
    type: 'setIndicators', v: 1, t: tArr,
    indicators: [
      { key: 'sma:20:close', kind: 'sma', pane: 'overlay', outputs: ['sma:20:close'], label: 'SMA 20 (close)', note: null, params: { n: 20, source: 'close' }, warmup_bars: 19 },
      { key: 'bb:20:2:close', kind: 'bb', pane: 'overlay', outputs: ['bb:20:2:close:mid', 'bb:20:2:close:upper', 'bb:20:2:close:lower'], label: 'BB 20 2', note: 'Bands use the sample standard deviation (n - 1).' },
      { key: 'rsi:14', kind: 'rsi', pane: 'sub', outputs: ['rsi:14'], label: 'RSI 14', note: null },
      { key: 'macd:12:26:9', kind: 'macd', pane: 'sub', outputs: ['macd:12:26:9:macd', 'macd:12:26:9:signal', 'macd:12:26:9:hist'], label: 'MACD 12 26 9', note: null }
    ],
    series: { 'sma:20:close': s20, 'bb:20:2:close:mid': bbMid, 'bb:20:2:close:upper': bbUp, 'bb:20:2:close:lower': bbLo, 'rsi:14': rsi, 'macd:12:26:9:macd': macd, 'macd:12:26:9:signal': sig, 'macd:12:26:9:hist': hist }
  };
  await post(indMsg);
  await settle();
  let ind = await dbg('indicators');
  check('MR5 indicators: 4 specs set from backend series (2 overlay, 2 sub panes), volume kept', ind.defs.length === 4 && ind.defs.filter(d => d.pane === 'sub').length === 2 && ind.volPane && ind.defs[0].values[0] === 2981 && !(await errs()).length, JSON.stringify(ind).slice(0, 300) + JSON.stringify(await errs()).slice(0, 200));
  if (fake) {
    const panes = await fk('__panes');
    check('MR5 indicators: engine panes = volume + 2 sub panes', panes.panes.length === 3 && panes.panes[0] === panes.vol, JSON.stringify(panes));
  }
  let lg = await dbg('legend');
  check('MR5 indicators: legend lists SMA/BB/RSI/MACD with values', /SMA 20 \(close\)\d/.test(lg) && lg.indexOf('RSI 14') >= 0 && lg.indexOf('MACD 12 26 9') >= 0, lg.slice(0, 300));
  await page.screenshot({ path: path.join(SHOTS, '12_mr5_indicators.png') });
  await clear();
  const four = JSON.parse(JSON.stringify(indMsg));
  four.indicators.push({ key: 'rsi:7', kind: 'rsi', pane: 'sub', outputs: ['rsi:7'], label: 'RSI 7', note: null }, { key: 'rsi:21', kind: 'rsi', pane: 'sub', outputs: ['rsi:21'], label: 'RSI 21', note: null });
  four.series['rsi:7'] = rsi; four.series['rsi:21'] = rsi;
  await post(four);
  await settle();
  check('MR5 indicators: 4 sub panes rejected (max 3), current set kept', (await errs()).some(e => e.code === 'bad_message' && /at most 3/.test(e.message)) && (await dbg('indicators')).defs.length === 4);
  const indRej = [
    ['series length differs', (() => { const m = JSON.parse(JSON.stringify(indMsg)); m.series['rsi:14'] = m.series['rsi:14'].slice(1); return m; })()],
    ['series key not an output', (() => { const m = JSON.parse(JSON.stringify(indMsg)); m.series['ema:5:close'] = s20; return m; })()],
    ['output without series', (() => { const m = JSON.parse(JSON.stringify(indMsg)); delete m.series['rsi:14']; return m; })()],
    ['9 indicator specs', (() => { const m = JSON.parse(JSON.stringify(indMsg)); for (let i = 0; i < 5; i++) { m.indicators.push({ key: 'sma:' + (30 + i) + ':close', kind: 'sma', pane: 'overlay', outputs: ['sma:' + (30 + i) + ':close'], label: 'SMA', note: null }); m.series['sma:' + (30 + i) + ':close'] = s20; } return m; })()],
    ['non-increasing t', (() => { const m = JSON.parse(JSON.stringify(indMsg)); m.t[5] = m.t[4]; return m; })()],
    ['string value', (() => { const m = JSON.parse(JSON.stringify(indMsg)); m.series['rsi:14'][100] = '50'; return m; })()],
    ['unknown kind', (() => { const m = JSON.parse(JSON.stringify(indMsg)); m.indicators[0].kind = 'kama'; return m; })()]
  ];
  for (const [name, m] of indRej) {
    await clear(); await post(m); await settle();
    check('MR5 indicators: rejects ' + name, (await errs()).some(e => e.code === 'bad_message'), JSON.stringify(await errs()).slice(0, 160));
  }
  await clear();
  await post({ type: 'updateIndicators', v: 1, t: [tArr[0], tArr[1]], series: { 'rsi:14': [55, 56] } });
  await post({ type: 'updateIndicators', v: 1, t: [tArr[0]], series: { 'ema:9:close': [1] } });
  await settle();
  ind = await dbg('indicators');
  check('MR5 updateIndicators: merges by t; unknown key rejected', ind.defs[2].values[0] === 2988 && (await errs()).some(e => e.code === 'bad_message'), JSON.stringify(ind.defs[2]));
  // 3 sub panes + volume off, then compare overlay
  const three = {
    type: 'setIndicators', v: 1, t: tArr, volume: false,
    indicators: [
      { key: 'rsi:14', kind: 'rsi', pane: 'sub', outputs: ['rsi:14'], label: 'RSI 14', note: null },
      { key: 'rsi:7', kind: 'rsi', pane: 'sub', outputs: ['rsi:7'], label: 'RSI 7', note: null },
      { key: 'macd:12:26:9', kind: 'macd', pane: 'sub', outputs: ['macd:12:26:9:macd', 'macd:12:26:9:signal', 'macd:12:26:9:hist'], label: 'MACD', note: null },
      { key: 'cmp:SPY', kind: 'cmp', pane: 'overlay', outputs: ['cmp:SPY:pct', 'cmp:SPY:price'], label: 'SPY', note: null },
      { key: 'sma:50:close', kind: 'sma', pane: 'overlay', outputs: ['sma:50:close'], label: 'SMA 50', note: null }
    ],
    series: { 'rsi:14': rsi, 'rsi:7': rsi, 'macd:12:26:9:macd': macd, 'macd:12:26:9:signal': sig, 'macd:12:26:9:hist': hist,
      'cmp:SPY:pct': closes.map((c, i) => +((c / closes[0] - 1) * 100).toFixed(3)), 'cmp:SPY:price': closes.map(c => +(c * 0.98).toFixed(2)), 'sma:50:close': s50 }
  };
  await clear(); await post(three); await settle();
  ind = await dbg('indicators');
  check('MR5 indicators: 3 sub panes allowed, volume pane removed (volume:false)', ind.defs.length === 5 && !ind.volPane && !(await errs()).length, JSON.stringify(ind).slice(0, 200) + JSON.stringify(await errs()).slice(0, 200));
  if (fake) check('MR5 indicators: engine has exactly the 3 sub panes', (await fk('__panes')).panes.length === 3, JSON.stringify(await fk('__panes')));
  lg = await dbg('legend');
  check('MR5 compare: legend shows % (price line plotted)', /SPY[+−]\d+\.\d\d%/.test(lg), lg.slice(0, 300));
  await page.screenshot({ path: path.join(SHOTS, '13_mr5_three_subpanes.png') });
  await post({ type: 'loadBars', v: 1, symbol: 'MSFT', tf: '1m', session: 'eth', assetClass: 'equity', exchangeTz: 'America/New_York', bars: makeEquityBars(500, { seed: 4 }), hasMoreLeft: false, sipThrough: null });
  await settle();
  ind = await dbg('indicators');
  check('MR5 new symbol: indicator values cleared until the host sends new ones', ind.defs.every(d => d.values.every(v => v === 0)), JSON.stringify(ind.defs.map(d => d.values)));
  check('MR5 new symbol: drawings cleared (host sends loadDrawings)', (await dbg('drawings')).length === 0);
  await clear();
  await post({ type: 'setIndicators', v: 1, indicators: [], t: [], series: {}, volume: true });
  await settle();
  ind = await dbg('indicators');
  check('MR5 indicators: [] removes all, volume back', ind.defs.length === 0 && ind.volPane && !(await errs()).length);
  check('MR5: no page errors', errorsLog.length === 0, JSON.stringify(errorsLog));
  await page.close();
}

// ------------------------------------------------------------------ MR8: markers (bridge revision 4)
async function runMarkers(browser, base) {
  const page = await browser.newPage({ viewport: { width: 1180, height: 640 } });
  const perr = [];
  page.on('pageerror', e => perr.push(e.message));
  await page.addInitScript(() => { window.__out = []; window.addEventListener('ufoschart:out', e => window.__out.push(e.detail)); });
  await page.goto(base);
  await page.waitForFunction(() => window.__out.length > 0, null, { timeout: 5000 });
  const post = m => page.evaluate(x => window.postMessage(x, '*'), m);
  const out = () => page.evaluate(() => window.__out.slice());
  const clear = () => page.evaluate(() => { window.__out.length = 0; });
  const dbg = (f, a) => page.evaluate(([fn, x]) => window.__ufosChartDebug[fn](x), [f, a]);
  const settle = async () => { await page.waitForTimeout(40); await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))); };
  const errs = async () => (await out()).filter(m => m.type === 'error');
  const ready = (await out()).find(m => m.type === 'ready');
  check('MR8: ready carries revision 4', ready && ready.revision === 4, JSON.stringify(ready));
  await post({ type: 'init', v: 1, theme: THEME, locale: 'en-US', tz: 'UTC' });
  const daily = makeDailyBars(504);
  await post(loadMsg(daily, { tf: '1D', session: 'rth', sipThrough: null, symbol: 'NVDA', hasMoreLeft: false }));
  await settle();
  const n = daily.length, D = 86400000;
  const iSat = daily.findIndex((b, i) => i > 300 && new Date(b.t).getUTCDay() === 1);   // a Monday
  const mk = (o) => Object.assign({ id: 'm' + Math.random().toString(36).slice(2, 8), t: daily[400].t, tEnd: null, kind: 'insider', side: 'buy', label: 'Insider buy' }, o);
  const ms = [
    mk({ id: 'in-bar', t: daily[400].t + 14 * 3600000, label: 'Form 4: CEO bought 10,000 sh <b>bold</b>' }),
    mk({ id: 'weekend', t: daily[iSat].t - 2 * D + 3600000, kind: 'fill', side: 'sell', label: 'Backtest exit' }),
    mk({ id: 'before', t: daily[0].t - 10 * D, label: 'too old' }),
    mk({ id: 'after', t: daily[n - 1].t + 3 * D, label: 'future' }),
    mk({ id: 'fomc', t: daily[450].t + 18 * 3600000, kind: 'fomc', side: null, label: 'FOMC decision' }),
    mk({ id: 'release', t: daily[455].t, kind: 'release', side: null, label: 'CPI release' }),
    mk({ id: 'best', t: daily[300].t, tEnd: daily[340].t, kind: 'period_best', side: null, label: 'Best period +18%' }),
    mk({ id: 'dd-open', t: daily[480].t, tEnd: null, kind: 'drawdown', side: null, label: 'Drawdown not recovered' }),
    mk({ id: 'clip', t: daily[0].t - 30 * D, tEnd: daily[5].t, kind: 'period_worst', side: null, label: 'Worst period' }),
    mk({ id: 'short', t: daily[400].t, kind: 'fill', side: 'short', label: 'Backtest short entry' })
  ];
  await clear();
  await post({ type: 'setMarkers', v: 1, markers: ms });
  await settle();
  const spans = Object.fromEntries((await dbg('markers')).map(m => [m.id, m.span]));
  check('MR8 markers accepted (no error)', !(await errs()).length, JSON.stringify(await errs()));
  check('MR8 point inside a bar snaps to that bar', JSON.stringify(spans['in-bar']) === '[400,400]', JSON.stringify(spans['in-bar']));
  check('MR8 point between bars (weekend) -> first bar after it', JSON.stringify(spans.weekend) === JSON.stringify([iSat, iSat]), JSON.stringify(spans.weekend) + ' vs ' + iSat);
  check('MR8 outside the loaded range -> not drawn', spans.before === null && spans.after === null, JSON.stringify([spans.before, spans.after]));
  check('MR8 range: bar of t .. bar of tEnd; open drawdown to the last bar; clipped at the first bar',
    JSON.stringify(spans.best) === '[300,340]' && JSON.stringify(spans['dd-open']) === JSON.stringify([480, n - 1]) && JSON.stringify(spans.clip) === '[0,5]', JSON.stringify([spans.best, spans['dd-open'], spans.clip]));
  await dbg('scrollToIndex', 360);
  await settle();
  await page.screenshot({ path: path.join(SHOTS, '14_mr8_markers.png') });
  if (ENGINE === 'fake') {
    const xy = await page.evaluate((t) => window.__fakeChart.convertToPixel({ timestamp: t, value: 1 }), daily[400].t);
    await page.mouse.move(Math.round(xy.x), 200);
    await settle();
    const lg = await dbg('legend');
    check('MR8 hover: legend shows glyph + label (plain text) for the bar\'s markers', lg.indexOf('▲Form 4: CEO bought 10,000 sh <b>bold</b>') >= 0 && lg.indexOf('▼Backtest short entry') >= 0 && (await page.evaluate(() => document.querySelectorAll('#legend b').length)) === 0, lg.slice(0, 300));
    await page.screenshot({ path: path.join(SHOTS, '15_mr8_marker_hover.png') });
    await page.mouse.move(5, 630);
  }
  // timeframe change keeps markers (same symbol), symbol change clears
  await post(loadMsg(makeEquityBars(2000, { seed: 3 }), { symbol: 'NVDA', hasMoreLeft: false }));
  await settle();
  check('MR8 same symbol, other timeframe: markers kept (re-anchored by time)', (await dbg('markers')).length === ms.length);
  await post(loadMsg(daily, { tf: '1D', session: 'rth', sipThrough: null, symbol: 'AMD', hasMoreLeft: false }));
  await settle();
  check('MR8 symbol change clears markers', (await dbg('markers')).length === 0);
  await post({ type: 'setMarkers', v: 1, markers: ms.slice(0, 2) });
  await post({ type: 'setMarkers', v: 1, markers: [] });
  await settle();
  check('MR8 empty list clears all', (await dbg('markers')).length === 0 && !(await errs()).length);
  const bad = [
    ['2001 markers', Array.from({ length: 2001 }, (_, i) => mk({ id: 'x' + i }))],
    ['duplicate id', [mk({ id: 'a' }), mk({ id: 'a' })]],
    ['unknown kind', [mk({ kind: 'earnings' })]],
    ['unknown side', [mk({ side: 'long' })]],
    ['tEnd < t', [mk({ tEnd: daily[399].t })]],
    ['label 201 chars', [mk({ label: 'x'.repeat(201) })]],
    ['control char in label', [mk({ label: 'a\u0007b' })]],
    ['unknown key', [Object.assign(mk({}), { color: 'red' })]],
    ['id 41 chars', [mk({ id: 'i'.repeat(41) })]],
    ['t not an integer', [mk({ t: daily[400].t + 0.5 })]],
    ['missing side', [(() => { const m = mk({}); delete m.side; return m; })()]]
  ];
  await post({ type: 'setMarkers', v: 1, markers: ms.slice(0, 3) });
  await settle();
  for (const [name, list] of bad) {
    await clear(); await post({ type: 'setMarkers', v: 1, markers: list }); await settle();
    check('MR8 schema: rejects ' + name + ' (whole message ignored)', (await errs()).some(e => e.code === 'bad_message') && (await dbg('markers')).length === 3, JSON.stringify(await errs()).slice(0, 160));
  }
  check('MR8: no page errors', perr.length === 0, JSON.stringify(perr));
  await page.close();
}

main().catch(e => { console.error(e); process.exit(2); });

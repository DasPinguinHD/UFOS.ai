# Market Research SPEC — archive: mr7-notes

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

### MR7 backend notes (implemented 2026-10-17: alerts — routes + exact JSON + engine, binding for MR7-WPF)

**Files.** `alerts.py` (validation, CRUD, `AlertEngine`, `AlertSub`), `alerts_api.py` (`AlertRoutes` mixin,
`ALERT_ROUTES` appended to `api.ROUTES`). Store **v7** (`alerts`, `alert_log`; v6 = MR5 review, so MR7 took the
next free number — the next free one is v8). Supervised task **`alert_engine`** ("Price & news alerts").
Changed additively: `hub/hub.py` (`trade_hook`: called for EVERY stream trade of a streaming symbol, errors
logged and swallowed), `api.py`, `__init__.py`, `store.py`, `limits.py`. Reads (no writes) MR5 drawings via
`store.drawing_get`; no `strategy_platform` change.

**Rules.** `kind` + `params` (exactly these keys; unknown → 400):
- `price_cross` `{level: number > 0 and ≤ 1e9, direction: "up"|"down"}`;
- `line_cross` `{drawing_id: 32 hex, direction?: "up"|"down"|"any" (default any)}` — the drawing must be an
  `alertLine` of the same symbol; one alert per line (409). The level = the line's current price
  (`points[0].p`), re-read every `ALERT_RELOAD_SECONDS` (5 s) and after every alert CRUD — a moved or
  split-rescaled line applies within 5 s. **Link (revised 2026-10-17): the backend owns `drawings.alert_id`
  and writes it in the SAME transaction as the alert** — create (enabled) / switch on → the drawing's
  `alert_id` = the alert id (and its `updated_at` = the alert's write time); switch off / delete / automatic
  switch-off → `alert_id` = null. Inside that transaction the line is checked again (exists, `alertLine`, same
  symbol, not linked to ANOTHER existing alert — a link to a no longer existing alert id is replaced); a
  failure writes nothing (409 "The linked alert line no longer exists." / "This alert line already has an
  alert.", 400 for tool/symbol). A disabled line alert keeps `drawing_id` but the drawing shows no link. **The
  WPF never PATCHes `alert_id` itself** (no second call, no dangling link); after an alert create / PATCH /
  DELETE it reloads that symbol's drawings (`GET /research/drawings?symbol=`) or sends the page an
  `upsertDrawing` with `alert_id` set/cleared (the new `updated_at` comes with the reload). A deleted line → the alert
  is switched off: `enabled: false`, `disabled_reason: "The linked alert line was deleted — this alert is
  switched off."`, audit `alert_disabled`; it cannot be switched on again (409);
- `day_move` `{pct: 0.1–50, direction: "up"|"down"|"any"}` — move of the last trade vs the previous close
  (§7.4: SIP daily close before the reference session, split-adjusted; crypto previous UTC day close —
  `stats.quote.prev_close`, Research pool + research priority, re-read when the day changes, failures retried
  after 60 s); at most ONE trigger per day (NY reference session, crypto UTC day) and side;
- `news` `{}` — a news item for the symbol on the news stream (MR6, after its de-dup); once per news id
  (updates of an item never trigger again).
`cooldown_seconds` 60–86 400 (default 900), `enabled` (default true). ≤ 200 rules (409 "At most 200 alerts —
delete one first."); an identical rule (symbol + kind + params) → 409 "An identical alert for this symbol
already exists."; unknown symbol → 404 `SymbolNotCovered`; `symbol`/`kind` cannot be PATCHed (400).

**Order of work (review M3, 2026-10-17).** The task loop flushes pending triggers FIRST (log + send), then
re-reads the rules (local SQLite), then flushes again; the previous closes for `day_move` are fetched by a
separate coroutine on its own schedule (`ALERT_RELOAD_SECONDS`, failures retried after 60 s) — a slow or
hanging Alpaca request never delays a trigger (test: a trigger is written and sent < 1 s while the fetch
hangs). A crash of that coroutine ends the task like any crash.

**Evaluation (`AlertEngine`, event loop — arithmetic only; store writes on the Research pool).** Trades
come from `hub.trade_hook` (every trade, not the coalesced `trade` messages). A crossing needs a PREVIOUS live
trade of the symbol: `up` = prev < level ≤ last, `down` = prev > level ≥ last — a jump over the level
between two trades (gap) fires; the first trade after a start, a restart or a stream gap (`symbol_status`
`live: false`) only sets the baseline. Late (older) trades are ignored. IEX trades of the extended session
count (stocks); with the market closed nothing trades, the rule stays armed (status note). De-dup = one
trigger per rule per cooldown (`last_triggered_at`, persisted) + the day / news-id rules above (state
persisted in `alerts.state_json`). A trigger is written FIRST (`alert_log` row + rule state, one transaction)
and then sent — a restart never repeats it (cooldown and day/news de-dup survive; the baseline rule prevents
a trigger from the first post-restart trade). Store unavailable at that moment → the message is still sent
with `logged: false`. Minute bars are not evaluated separately (the IEX bars are built from the same trades).

**Demand / limits.** Every ENABLED rule is one hub subscriber of kind `alert` (topic `trades:<SYM>`, news
rules `news:<SYM>`) → `/research/feeds/status` `demand.alerts` = enabled rules and the streams run while ≥ 1
is enabled (stated: `runtime.note`). Priority for the 30-symbol stock limit: views first, then alerts in
creation order; a rule beyond it is `not_live` with "Stream limit: 30 symbols — X is not live. This alert
cannot fire while the symbol is not live." (no snapshot, no polling — Q10). Switching a rule off / deleting it
releases its demand at once; the task ending (crash / shutdown) unregisters all.

**Alert object** (every key always present):
```json
{"id": "9a0e…32 hex", "symbol": "AAPL", "kind": "price_cross", "params": {"level": 190.0, "direction": "up"},
 "cooldown_seconds": 900, "enabled": true, "drawing_id": null, "last_triggered_at": null,
 "disabled_reason": null, "created_at": "2026-10-17T12:00:00+00:00", "updated_at": "2026-10-17T12:00:00+00:00",
 "status": {"state": "armed", "reason": null, "live": true, "level": 190.0,
            "last_price": {"price": 187.12, "ts": "2026-10-17T14:00:01+00:00"}, "prev_close": null,
            "cooldown_until": null}}
```
`status.state` ∈ `armed` · `waiting` (reason: "Armed with the next live trade — a crossing needs a previous
price." [+ " Market closed — no trades until the next session."], "Previous close unavailable: …", "Loading …",
"Starting the live stream…") · `cooldown` (`cooldown_until`, reason "Fired at … — quiet until …") ·
`not_live` (the hub's reason + " This alert cannot fire while the symbol is not live.") · `disabled`
(`disabled_reason` or "Switched off.") · `not_running` ("The alert task is not running (see the Research
status). Alerts work only while UFOS.ai runs — …"). `day_move` adds `"levels": {"up", "down"}` (price
thresholds) and `prev_close`; `news` has `level: null`, `last_price: null`.

**Routes.**
- `GET /research/alerts` → `{"alerts": [<alert>…] (oldest first), "limits": {"max_alerts": 200,
  "cooldown_default": 900, "cooldown_min": 60, "cooldown_max": 86400, "day_move_pct_min": 0.1,
  "day_move_pct_max": 50.0}, "runtime": {"running": true, "note": "Alerts work only while UFOS.ai runs — there
  is no background service; nothing is checked while the app is closed.", "store_error": null, "active": 3}}`.
  The WPF shows `runtime.note` in the Alerts section (always visible) and `running: false` as a banner.
- `POST /research/alerts {symbol, kind, params, cooldown_seconds?, enabled?}` → 201 `<alert>` (audit
  `alert_created`). `PATCH /research/alerts/{id} {params?, cooldown_seconds?, enabled?}` (≥ 1 key) → 200
  `<alert>` (audit `alert_updated`; switching on clears `disabled_reason`). `DELETE /research/alerts/{id}` →
  200 `{"deleted": id}` (audit; the log rows stay). Bodies ≤ 4096 bytes; unknown id → 404 `{"error": "Alert
  not found."}`.
- `GET /research/alerts/log?limit=1…500 (100)&alert_id=` → `{"items": [{"id": 12, "alert_id": "9a0e…",
  "symbol": "AAPL", "ts": "2026-10-17T14:00:02+00:00", "kind": "price_cross", "message": "AAPL crossed above
  190.00 — last trade 190.05.", "price": 190.05, "detail": {"side": "up", "previous_price": 189.9, "level": 190.0,
  "trade_ts": "2026-10-17T14:00:02+00:00"}}], "limit": 100, "retention_days": 90}` newest first (90-day
  retention by `ts`). `detail`: day_move `{side, pct, prev_close, day, level}`, news `{news_id, url,
  source_label, level: null}`.

**WebSocket `alerts` topic → toast.** `{"type": "alert", "alert_id": "9a0e…", "symbol": "AAPL", "kind":
"price_cross", "title": "Research alert · AAPL", "message": "AAPL crossed above 190.00 — last trade 190.05.",
"ts": "2026-10-17T14:00:02+00:00", "price": 190.05, "level": 190.0, "log_id": 12, "logged": true}` (news:
`price`/`level` null). The WPF shows `title` + `message` via the existing HedgeFund toast helper (plain text)
and adds the row to the Alerts log view; not queueable → close 1013 (never dropped, §4.3); alerts missed while
the socket was down are in `GET /research/alerts/log`. Messages: price `"<SYM> crossed above|below <level> —
last trade <price>."`, line `"<SYM> crossed the alert line at <level> (upward|downward) — last trade
<price>."`, day move `"<SYM> is up|down <x.xx> % today[ (UTC day)] — last trade <p> vs previous close <pc>."`,
news `"News for <SYM>: <headline ≤ 200>"`.

**Isolation.** Hook errors are caught in the hub (stream continues); one failing rule never stops the others;
an engine crash ends the task (all alert subscribers released, `status` → `not_running`) and the supervisor
restarts it — the paper engine is never affected. Research never places orders from an alert.

**Tests** (`tests/test_mr_alerts.py` 15, `tests/test_mr_alerts_engine.py` 14, kit `tests/mr_alerts_testkit.py`):
every rule type + 21 invalid bodies, line link rules, atomic drawing link (5 tests: set/clear on create, switch
on/off, delete, system disable; line linked to another alert refused, dangling link replaced; checks repeated
inside the transaction; failed insert rolls the link back), limits + duplicates, PATCH/DELETE, log route, store v7 +
90-day retention + disable audit, routes registered; crossing up/down incl. gap and exact touch, baseline at
start / after a stream gap / after a restart, late trades, direction filter, cooldown + suppression, restart
without a repeated trigger, deleted-before-logging, broken rule isolated, moved line → new level / deleted
line → disabled + demand released, day move once per day and side + next day + missing previous close, news
once per id + cooldown, market-closed status, real hub: demand registration/release, 30-symbol limit message,
end-to-end trigger over the fake stream, engine crash releases demand while the hub runs, failing trade hook.
MR tests 308 OK; full suite OK (split runs); scope guard stage0 + mr0 ok.

**Defaults (user away 2026-10-17) — confirm:** first trade after (re)start/gap never triggers; day move
fires once per day and side; extended-hours IEX trades count; the WPF (not the backend) sets the drawing's
the backend (not the WPF) owns the drawing's `alert_id` (revised 2026-10-17); price_cross `direction` only up|down
(line and day move also any). Resolved 2026-10-17 (MR5 follow-up): drawing POST/PATCH refuse `alert_id` (400), imports strip it
(`alert_id_ignored: true`) — see MR5 notes.

### MR7 WPF notes (implemented 2026-10-17: alerts — editor, Alerts section, alert-line link, toasts)

- **Files.** New `MarketResearch/ResearchView.Alerts.cs` (partial: 🔔 editor, Alerts section + log, alert-line
  wiring, live log rows), `MarketResearch/ResearchAlertNotifier.cs` (app-wide toasts). Changed:
  `ResearchClient.cs` (alert routes + DTOs exactly as "MR7 backend notes"), `ResearchSocket.cs` (`alert`
  messages → `AlertReceived`; `SetAlertsWanted`), `ResearchView.xaml` (🔔 `BellButton` in the tab bar, Alerts page
  = `AlertsPanel`), `ResearchView.xaml.cs` (InitAlerts / section hook / shutdown keeps the socket),
  `ResearchView.Watchlists.cs` (topic `alerts` before `feeds`), `ResearchView.Drawings.cs` (alert-line hooks;
  `alert_id` never sent in drawing POST/PATCH), `ResearchView.Indicators.cs` (bell refresh),
  `Fund/FundNotifications.cs` (+ `ShowResearchAlert(title, message)`: same toast path, honours "mute
  notifications"), `HedgeFundWindow.xaml.cs` (+ `ResearchAlertNotifier.EnsureStarted()` next to the engine toasts).
  Checked statically only (no dotnet on the VM).
- **Socket demand (deviation from §4.3.1 "open only while visible", needed for toasts — default, user away
  2026-10-17 — confirm):** the ONE socket also stays open while ≥ 1 alert rule is enabled (checked every 60 s by
  `ResearchAlertNotifier` and after every alert write in the view), with `hello/visible: false` when no Research
  view is shown (the hub counts it as no view; the alert rules keep the streams on themselves) and the topic
  `alerts`. Closing the Hedge Fund window no longer shuts the socket down (only the view's demand). Alerts that
  fire while the socket is down are not toasted afterwards — they are in the alert log. Alerts run only while
  UFOS.ai runs (backend `runtime.note`, shown in the Alerts section and in the editor).
- **Toasts.** Every WS `alert` → `FundNotifications.ShowResearchAlert(title, message)` (plain text, ≤ 120 / 240
  chars, one per `log_id`), muted by the Hedge Fund "mute notifications" option; the view adds the row to its log
  and counts "ALERTS · n NEW" on the sidebar entry until the section is opened.
- **🔔 editor** (tab bar, per open symbol; "🔔 n" = enabled rules of the symbol): Price crosses (above/below +
  level, prefilled with the last trade), Move today (up/down/either + %), News; quiet time in minutes (seconds to
  the backend), On; edit mode for any rule (kind fixed; alert-line rules: direction + quiet time; the level is the
  line). Only number parsing is checked client-side; ranges/duplicates/limits are the backend's messages,
  verbatim (400 field texts "Not saved: params.level: …", 409 "At most 200 alerts …", "An identical alert …").
- **Alerts section** (sidebar ALERTS): "ALERTS · n/200 · k ACTIVE", runtime note (always), task not running /
  store error banner lines, "At most 200 alerts — delete one first." at the limit, per rule: symbol ·
  description · state word (ARMED / WAITING / COOLDOWN / NOT LIVE / OFF / NOT RUNNING, colour + word) + the
  backend's reason verbatim (incl. "Stream limit: 30 symbols — X is not live. This alert cannot fire while the
  symbol is not live."), last fired (local time), quiet time, last price; buttons ON/OFF (PATCH enabled),
  ⌖ open chart, ✎ edit, × delete (inline confirm; log rows stay). Refreshed every 10 s while shown and ~6 s after a
  trigger / a line move. Log: newest first, 90 days (backend retention), 100 rows + "Load more" (+100 up to the
  route's 500).
- **Alert line (⍾).** When an `alertLine` drawing is stored (POST answer without `alert_id`) the view creates its
  `line_cross` alert (direction any, default quiet time) and says so; the backend links the drawing in the same
  transaction, the view re-reads the symbol's drawings and `upsertDrawing`s that line (bell on the page). After
  every line-alert PATCH/DELETE the same re-read happens. Moving the line (PATCH points) → "Alert line moved to X
  — the alert uses the new level within 5 s." and the list shows `status.level` ~6 s later. Deleting the line →
  "its alert is switched off" (backend), the rule stays listed (OFF + reason) until deleted. The WPF never
  PATCHes `alert_id` (it is removed from drawing POST/PATCH bodies).
- **Open / verify on the PC:** first build; a toast while the Research view is hidden / the Hedge Fund window is
  closed (socket kept for alerts); mute option; emoji 🔔 rendering in Consolas fallback.
- **Review fix L4 (2026-10-17).** While no Research view is shown but the socket stays open for alert toasts,
  `ResearchSocket.CurrentTopics` subscribes only `alerts` (the view's `candle:`/`trades:`/`news:`/`watchlist:`/`feeds`
  topics are unsubscribed at the visibility change, so the hub stops building candles / SIP tail refreshes for an
  invisible chart); on show the full set is subscribed again and the view re-fetches the open chart and news (no
  re-sync follows on an open connection); a re-sync while hidden is ignored by the view (re-fetched on show).


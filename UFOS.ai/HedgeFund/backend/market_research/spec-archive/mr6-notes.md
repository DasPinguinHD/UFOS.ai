# Market Research SPEC — archive: mr6-notes

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

### MR6 backend notes (implemented 2026-10-17: news + logos — routes + exact JSON, binding for MR6-WPF)

**Files.** `news_text.py` (untrusted text → plain text, link / image-URL rules, `sanitize_item`), `media.py`
(image checks, `ImageFetcher` = the thumbnail proxy's downloader), `news.py` (`NewsService`: REST news,
stream hook, store queue, thumbnail setting + proxy), `logos.py` (`LogoService`), `news_api.py` (`NewsRoutes`
mixin, `NEWS_ROUTES` appended to `api.ROUTES`, `Binary` answers → `web.Response` in `api.register`). Store
**v5** (`news`, `news_symbols`, `news_images`, `logos`). Supervised task **`news_stream_store`** ("News stream
storage": writes queued stream items, refreshes the cached thumbnail setting). Changed (additive):
`hub/hub.py` (`news_hook`; without it items go out with `images: []`), `hub/protocol.py` (`parse_news` =
`news_text.sanitize_item`), `sources.py` (`news`, `logo`), `api.py`, `__init__.py`, `store.py`, `limits.py`.
Strategy Platform extension (§12, also in its SPEC): `providers/alpaca.py` `images` + `logo()`.

**Untrusted text (§9).** Headline / summary / publisher: HTML tags removed (`script`/`style`/`svg`/… content
dropped, block tags → space), entities decoded, a second strip when the decoded text still contains tags
(`&lt;b&gt;x&lt;/b&gt;` → `x`), a tag cut off at the end removed, control characters / line separators →
space, format / private-use characters removed, whitespace collapsed, caps 300 / 500 / 100 chars ("…").
Plain comparisons survive (`x < y`). The result is plain text — the WPF shows it in a TextBlock, never as
markup. Links (`url`): only `http`/`https`, a host, no `user:pw@`, no whitespace/control characters,
≤ 1000 chars — else `null` (open with the default browser, show the domain). `symbols` = the
Alpaca/Benzinga tags (`AAPL`, crypto `BTCUSD`), ≤ 50, `^[A-Z0-9][A-Z0-9./-]{0,19}$`. Items without a
positive integer id, a headline or `created_at` are dropped.

**`GET /research/symbols/{s}/news?limit=&before=`** (`limit` 1–50, default 20; `before` ISO-8601,
exclusive; unknown / repeated params → 400; symbol 400 / 404 `SymbolNotCovered` as §4.1):
```json
{"symbol": "AAPL", "source": "Alpaca · Benzinga", "source_label": "Benzinga via Alpaca",
 "items": [{"id": 41234567, "headline": "Apple beats estimates", "summary": "Plain text …",
            "publisher": "benzinga", "source_label": "Benzinga via Alpaca",
            "url": "https://www.benzinga.com/news/26/10/41234567/apple-beats",
            "created_at": "2026-10-15T14:00:00+00:00", "updated_at": "2026-10-15T14:05:00+00:00",
            "symbols": ["AAPL", "MSFT"],
            "images": [{"size": "thumb", "path": "/research/news/images/3f9c…64 hex"},
                       {"size": "small", "path": "/research/news/images/a01b…64 hex"}]}],
 "thumbnails": true, "fetched_at": "2026-10-15T15:00:00+00:00", "has_more": true,
 "next_before": "2026-10-14T09:12:00+00:00",
 "note": "News: Benzinga via Alpaca. Headlines and summaries are shown as plain text; links open the original article in your browser."}
```
- Newest first (`created_at`, then `id`). `has_more` = the page is full; next page: `before=next_before`
  (items sharing that exact second with the boundary can be skipped — the WPF de-dups by `id` anyway).
  `summary`, `publisher`, `url`, `updated_at` may be `null`; `images` is `[]` when the item has none or
  thumbnails are off. `images[].size` ∈ `thumb`, `small` (Alpaca's `large` is not passed on — above the
  size cap); at most one per size. `fetched_at` = when Alpaca last answered for this symbol.
- Source: `AlpacaProvider.news` through `sources.news`, Research pool + `research_priority()` (≤ 60/min,
  yields to trading). The first page of a symbol is asked from Alpaca at most every `NEWS_REFRESH_SECONDS`
  (60 s) per symbol (a request for a LARGER page asks again); in between and for the merge the answer is
  read from the store (Alpaca answers + stream items, de-duplicated by id). `before` pages always ask Alpaca
  (`end = before − 1 s`, Alpaca's end is inclusive).
- Alpaca failure → **503** `{"error": "News unavailable: <the source's message>", "kind": "SecretMissing" |
  "SourceAuthRejected" | "DataSourceUnavailable" | …}` — never stored news instead (no fallback). Store
  unavailable → 503 `StoreUnavailable`.

**Store / de-dup / retention.** `news.id` = Alpaca's id; an existing row is replaced only by a NEWER
`updated_at` (Alpaca corrections); a stream copy without images gets the REST images later. 7-day
retention by `created_at` for `news`, `news_symbols`, `news_images` (`news_images.created_at` = the newest
news item using that image URL, so a shared image is kept as long as a current item uses it). Stream items
are queued (≤ 1000; full → delivered but not stored, logged) and written by `news_stream_store`.

**WebSocket `news:<SYM>`** (topic of §4.3, MR2b routing unchanged: the hub subscribes the union of all
`news:` topics on the ONE news stream connection and sends each item once per connection that has a
matching topic): `{"type": "news", "item": <the item object above>}`. A repeat (same `id` and `updated_at`)
is not sent again; an update (newer `updated_at`) is sent again with the same `id` — the WPF replaces the
card by `id`. Items missed while disconnected are not replayed: after a reconnect the WPF re-fetches the
REST list (like bars, §4.3.1). The news feed row of `/research/feeds/status` (`id: "news"`) is the stream's
state (`off` / `connecting` / `live` / `waiting` / `unavailable` + message, MR2b); REST failures are shown on
the news card (503 above).

**Thumbnails — proxy + setting.** The WPF loads ONLY `http://127.0.0.1:8867` + `images[].path` (with the
token header, like every route) and never a third-party URL.
- `GET /research/news/images/{key}` (`key` = sha256 hex of an image URL that came from Alpaca's news data;
  anything else → 404 `{"error": "Unknown news image.", "kind": "UnknownImage"}`): 200 with the image bytes,
  `Content-Type` image/png | image/jpeg | image/gif, `Cache-Control: private, max-age=3600`,
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`. Thumbnails off
  → **409** `{"error": "News thumbnails are switched off (Research settings) — no image is loaded.", "kind":
  "ThumbnailsOff"}` (nothing is fetched). Download / check failure → **503** `{"error": "Thumbnail
  unavailable: <reason>", "kind": "ThumbnailUnavailable"}` (WPF: card without a picture). Thumbnail pool full
  (review M2) → **503** `{"error": "Thumbnail loading is busy — try again shortly.", "kind": "ThumbnailBusy"}`
  — the WPF treats `ThumbnailBusy` (or a 429) as "retry later".
- Download rules (`media.py`): https only, DNS host name (no IP literal / localhost / `.local`), port 443, the
  name must resolve to public addresses only; no automatic redirects — ≤ 3 hops, each checked the same way;
  timeouts 10 s per connect/read and a HARD 20 s total (review H1: every read is ≤ 8 KiB with the socket
  timeout set to the remaining time, and a kill timer shuts the socket at the deadline — dripping headers,
  chunk lines, TLS handshake or body cannot hold a thread longer; DNS runs in a helper thread bounded by the
  remaining time, ≤ 4 lookups pending); the connection goes to the IP that passed the public-address check
  (review L5: no second resolution — `http.client` pinned to that IP, `Host`, TLS SNI and certificate check
  use the host name); ≤ 512 KB (Content-Length first, then while reading);
  `Content-Type` ∈ png/jpeg/gif (SVG, WebP — review L6: no WIC decoding of third-party WebP — and everything
  else refused) AND the file signature must match;
  ≤ 4096 px per side (header). Own `http_gate.HttpGate` source `news_images` (60/min, one attempt) — image
  hosts are not Alpaca, the Alpaca budget is not touched; gate wait ≤ 5 s. Downloads run on their OWN pool
  (review M2: `executor.run_image`, 2 workers, ≤ 8 jobs running + waiting, then 503 `ThumbnailBusy`) — never
  on the 4-worker Research pool, so hanging image hosts cannot delay bars / news / alert writes. Cache in `news_images`: `ok` served from the store;
  `rejected` (rule broken) never retried; `failed` (network / HTTP status) retried after 15 min.
- `GET /research/news/settings` / `PUT /research/news/settings {"thumbnails": bool}` (exactly that key;
  else 400; a change writes an audit row `news_thumbnails_set`):
```json
{"thumbnails": true,
 "note": "News thumbnails are loaded by the HedgeFund backend (never by the app) from the image servers named in the Alpaca/Benzinga news data; those servers see your IP address when the backend fetches an image. Switch thumbnails off and no image request is made.",
 "source_label": "Benzinga via Alpaca",
 "logos": {"state": "not_on_plan", "reason": "Logos are not available on this Alpaca plan: Alpaca refused the logo request (HTTP 403: forbidden) — Alpaca offers the logo endpoint as a paid add-on (Broker API); it is usually not part of the free plan",
           "checked_at": "2026-10-15T15:00:00+00:00"}}
```
  Default on (C4, meta `news_thumbnails`). `logos.state` ∈ `unknown` (not asked yet), `available`,
  `not_on_plan` (`reason`, `checked_at` set; else `null`). The WPF shows the setting "Show news thumbnails"
  with the `note` text (privacy note) in its Research settings.

**`GET /research/symbols/{s}/logo`** (no query parameters): 200 `image/png` bytes (`Cache-Control: private,
max-age=86400`, nosniff, CSP as above), or 404 `{"error": "No logo available from Alpaca for MSFT.", "kind":
"NoLogo"}`, or 404 `{"error": "Logos are not available on this Alpaca plan: <reason>", "kind":
"LogosNotOnPlan"}`; other failures 503 `{error, kind}` (keys missing, network, a logo that fails the checks).
The WPF shows ticker initials as plain text for any non-200 (normal empty state — not a data fallback) and
puts `error` in the tooltip. Source: Alpaca `GET https://data.alpaca.markets/v1beta1/logos/{symbol}`
(crypto: `BTCUSD` spelling — verify), never `?placeholder=true`; keys in headers only; no redirects; checks
HTTP 200, `image/png`, PNG signature, ≤ 256 KB, ≤ 1024 px per side. Alpaca's docs/blog list logos as a paid
add-on of the Broker API (Essential/Core plans) — on the user's free plan a 401/403 is expected: remembered
account-wide (meta `logos_refused`), every symbol answers `LogosNotOnPlan` without a request for 24 h, then
Alpaca is asked again. Cache `logos`: `ok` 30 days, `none` (Alpaca 404) 7 days; pruned after 30 days.

**Tests** (`tests/test_mr_news.py` 25, `tests/test_mr_news_media.py` 22, kit `tests/mr_news_testkit.py`;
fakes only): sanitising (tags, scripts, entities, double-encoded markup, truncated tag, control/format
characters, caps), link and image-URL scheme/host filtering, REST shape + order + no third-party URL in the
answer, research priority on the Research pool, refresh spacing + paging + de-dup, update only when newer,
503 with the cause kind, validation, crypto tag; retention (news + symbols + images 7 days, logos 30 days,
shared image kept); thumbnails setting (off → no paths, proxy 409, nothing fetched, audit; on again); stream:
routing by symbol through the hub, one message per connection, repeat dropped, update re-sent, unsubscribed
symbol not sent, stored by the task, thumbnails off, no image URLs without the MR6 service, hook failure
isolated; image checks (4 types + dimensions, SVG, wrong type, mismatch, size, pixels), fetcher (headers,
Content-Length / streamed oversize, wrong type / SVG, socket timeout, total deadline, network error, HTTP
404, unsafe URLs, private resolution, checked redirects + limit), proxy cache (ok cached, rejected not
retried, failed retried after the window, unknown key); provider `logo` (URL, headers, no redirect, 404 →
None, 401/403 → `LogosNotAvailable`, type / size / signature / redirect checks, crypto path) and news
`images` https-only; logo cache (30 days ok, 7 days none, plan refusal remembered + re-checked, status) and
the binary answer through `api.register` (aiohttp stubbed). MR tests 268 OK; full suite OK (split runs);
scope guard stage0 + mr0 ok.

**Defaults (user away 2026-10-17) — confirm:** thumbnails proxied by the backend and cached 7 days (with the
news); only `thumb`/`small` sizes; 512 KB / 4096 px caps; a 401/403 from the logo endpoint is read as "not on
this plan" (the message names the HTTP status, so wrong keys are recognisable — other Research functions
show the key error anyway); logo refusal re-checked once a day; first news page cached 60 s per symbol.

### MR6 WPF notes (implemented 2026-10-17: news card, Research settings, logos)

- **Files.** New `MarketResearch/ResearchView.News.cs` (partial: news card, live news, thumbnails, logos, settings).
  Changed additively: `ResearchClient.cs` (`GetNewsAsync`, `Get/PutNewsSettingsAsync`, `GetBinaryAsync` + DTOs `NewsList`,
  `NewsItem`, `NewsImage`, `NewsSettings`, `LogosState`, `BinaryResult`), `ResearchView.xaml` (news card, header logo
  `Image`, "⚙ Settings" + popup under the status strip), `ResearchView.xaml.cs` (subscribe/unsubscribe `NewsReceived`,
  re-fetch news on re-sync), `ResearchView.Workspace.cs` (`RenderHeader` → `NewsTrackActive()`, palette rows get a logo
  badge), `ResearchView.Watchlists.cs` (topic `news:<SYM>` of the active tab after its `trades:`; rows get a logo badge).
  `ResearchSocket.cs` (`NewsReceived` event, `case "news"`, `WsNews {item}`) was added by the MR5-fix agent on request.
  Static checks only (no dotnet).
- **News card.** Follows the active tab: GET …/news?limit=20 (newest first), "Older news" = `before=next_before`, de-dup
  by `id` (REST page replaces the list; a stream repeat with the same `updated_at` is ignored, an update replaces the card),
  ≤ 200 kept / 60 shown. Live: `news` messages whose tags contain the symbol (crypto tag `BTCUSD`); after a reconnect the
  first page is re-fetched (nothing replayed). A REST failure → "News unavailable: <reason>" verbatim + Retry, the list is
  cleared (no older list as current); an older-page failure keeps the list with its own line. Card: headline (plain text; a
  button only when the link passes an http/https + host + no user-info check, opened with `NewsroomLinks.Open` = the
  Newsroom's default-browser helper, the host in the tooltip), meta "Benzinga via Alpaca · 12 min ago · publisher"
  (ages tick every 30 s; exact local time in the tooltip), ≤ 8 ticker chips (+n) — a chip looks the tag up in the asset list
  (GET /research/palette, exact match; `XXXUSD` also as `XXX-USD`) and opens it, otherwise a message; summary (2 lines).
  Header chip "● LIVE / NOT LIVE" = the news stream row of feeds/status (or the socket state).
- **Thumbnails.** Only `images[].path` matching `^/research/news/images/[0-9a-f]{64}$`, fetched from 127.0.0.1:8867 with
  the token (`GetBinaryAsync` refuses any path outside `/research/`); ≤ 512 KB (Content-Length and while reading),
  signature PNG/JPEG/GIF/WebP, pixel size ≤ 4096 read from the header before decoding, decoded to 192 px width on a worker
  thread, frozen, session cache ≤ 300; 409 ThumbnailsOff / 503 / 404 → card without a picture (logged). WebP needs the
  Windows WebP codec — without it the card has no picture.
- **Settings (⚙ under the status strip).** GET /research/news/settings when opened; "☑/☐ Show news thumbnails" → PUT
  {thumbnails}, then the image cache is cleared and the news list re-fetched; the backend's privacy `note` verbatim;
  logo state line (available / not on this plan + reason / not checked yet). Esc closes.
- **Logos.** Header 40 px box, watchlist rows 14 px, palette rows 16 px: initials (plain text) are the normal empty state;
  GET /research/symbols/{s}/logo (≤ 2 in flight, ≤ 256 KB, `image/png` + PNG signature, ≤ 1024 px, decoded to 96 px,
  frozen, session cache). `LogosNotOnPlan` (or settings `logos.state = not_on_plan`) stops all further logo requests for
  the session; the reason is in the header logo tooltip. No third-party fetch anywhere.
- **Accessibility.** Names on the card, headline buttons ("News: … Opens host in the browser"), chips ("Open the symbol X"),
  thumbnails, logo badges, settings toggle (with its state); live regions for the news message and the settings message.
- **Verify on the PC:** first build; JPEG/WebP thumbnails; Popup focus for the settings toggle; ticker chip lookup for
  crypto tags; logo route on the free plan (expected: LogosNotOnPlan → initials).
- **Review fixes (2026-10-17).** M2: thumbnails are requested only for cards in the visible part of the news list
  (± 120 px, re-checked on scroll and after every render), at most 2 requests in flight; queued requests are dropped
  when the card scrolls out, the list is re-rendered or the symbol changes; a proxy answer 503 "busy" (kind
  `ThumbnailBusy` or "busy" in `error`) or 429 is retried after 5 / 15 / 30 s (then no picture) — the card simply has
  no picture meanwhile, no error banner. L6: WebP is no longer accepted client-side (PNG/JPEG/GIF signatures only),
  matching the backend proxy's allow-list.


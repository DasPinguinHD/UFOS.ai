# LSA-SRC — LiveStream Agent: official sources instead of YouTube + transcript gaps (user 2026-10-25)

Owner: H (backend) + W (window). Contract first (LSA_SOURCES_API.md) and a short user interview on the source list.

## Why
YouTube's terms forbid downloading / automated access unless YouTube authorises it; `yt-dlp` does exactly that.
User decision 2026-10-25: use official sources that allow it instead of YouTube; remove yt-dlp.

## Scope
1. Source catalogue: an allow-list of official streams/audio whose terms permit programmatic access, each with its
   terms link + date checked (candidates to research and confirm with the user: federalreserve.gov press conferences /
   FOMC, ECB press conferences, US Treasury, BLS/BEA release webcasts…). The window offers a source picker (no free URL
   field for YouTube); a non-allow-listed URL is refused with the reason. Record each source's terms in the README
   "Data sources & licensing" table.
2. Remove yt-dlp (requirements, lock, code, docs); keep ffmpeg only if a source needs transcoding.
3. Transcript gaps — measure, show, fix:
   - Measure: Deepgram results carry start + duration; detect holes in the timeline and log cause candidates
     (audio intake stalled, reconnect, Deepgram close/reopen).
   - Show: insert an explicit marker in the transcript at the gap ("[gap 4.2 s — audio intake fell behind]");
     replace the generic "Text may contain gaps…" line with a count of measured gaps.
   - Fix by cause: buffering between intake and Deepgram, reconnect without losing the segment, keep-alive, endpointing.
   Note: the analysis-backlog skip in main.py only skips ANALYSIS of older segments; the transcript itself keeps them.
4. Tests without network (fake source, fake Deepgram events with timestamps incl. holes).

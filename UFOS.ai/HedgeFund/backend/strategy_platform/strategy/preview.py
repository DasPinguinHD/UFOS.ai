"""Signal preview of a spec on current data (SPEC §7.7).

``preview(spec, as_of=None, days=20) -> {as_of, rows: [{symbol, signal,
rank, target_weight}], history: [{date, weights, rebalance}], warnings,
data: {source, from, to, symbols_missing}}``

- Daily bars via ``data.cache.get_bars`` (the ONE configured source,
  Alpaca). Per symbol: first the range without a freshness requirement; a
  symbol with NO bars at all (not listed yet / no history at the source) is
  excluded and listed in ``symbols_missing`` (a property of the universe,
  not a fallback). A symbol with bars is then read with
  ``require_fresh_through`` = the last NYSE close at/before ``as_of``
  (equities) or ``as_of`` (crypto): when the source cannot deliver the
  current bar, the preview FAILS with that ``DataSourceUnavailable`` — no
  stale signal is shown as current. Any source/storage failure propagates.
- History loaded: the spec's ``max_lookback`` (warm-up included, see
  dsl.py) + ``days`` + HISTORY_MARGIN_BARS bars, converted to calendar days.
- External features are loaded once for the panel (strategy.features);
  their failures propagate.
- ``rows``: the signal of the last panel row (the newest completed bar at
  ``as_of``), ``rank`` 1 = highest signal (ties by symbol; no signal →
  rank null), ``target_weight`` the portfolio rules' weight on that row
  (0 when not selected). ``history``: the weights held after each of the
  last ``days`` rows (targets of the latest rebalance at/before the row).
- Wall-time bound: PREVIEW_TIME_LIMIT_S (60 s) → ``PreviewTimeout``.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import math
import time
from datetime import timedelta
from typing import Callable, Dict, List, Optional

from ..core.errors import InvalidRequest, UserFacingError

PREVIEW_TIME_LIMIT_S = 60.0
HISTORY_MARGIN_BARS = 20
MAX_PREVIEW_DAYS = 250


class PreviewTimeout(UserFacingError):
    """The preview took longer than its wall-time bound."""


def _calendar_days(bars: int, crypto_only: bool) -> int:
    if crypto_only:
        return bars + 3
    return int(math.ceil(bars * 7 / 5 * 1.04)) + 10   # weekends + holidays (~9-10 per year)


def preview(spec: dict, as_of=None, days: int = 20, *, get_bars: Optional[Callable] = None, clock=None,
            ext_values: Optional[dict] = None, ext_catalogue=None,
            configured_source: Optional[Callable[[str], str]] = None,
            time_limit_s: float = PREVIEW_TIME_LIMIT_S, monotonic: Callable[[], float] = time.monotonic) -> dict:
    from ..core import calendars as cal
    from ..core.clock import resolve
    from ..core.instruments import instrument
    from ..data.providers.base import bars_data_type, iso, parse_ts
    from . import portfolio, schema, universe, validator
    from .evaluate import evaluate
    from .panel import Panel

    started = monotonic()

    def check_time(stage: str):
        if monotonic() - started > time_limit_s:
            raise PreviewTimeout(f"The signal preview took longer than {time_limit_s:.0f} s ({stage}) — try a "
                                 "smaller universe or fewer days.")

    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_PREVIEW_DAYS:
        raise InvalidRequest(f"days must be a whole number from 1 to {MAX_PREVIEW_DAYS}.")
    res = validator.validate(spec, ext_catalogue=ext_catalogue)
    if not res.ok:
        first = res.errors[0]
        raise InvalidRequest(f"The strategy is not valid ({first['path']}: {first['message']}).")
    full = schema.with_defaults(spec)
    now = parse_ts(resolve(clock).now_utc())
    as_of_dt = now if as_of is None else parse_ts(as_of)
    if as_of_dt > now:
        raise InvalidRequest("as_of lies in the future.")
    if get_bars is None:
        from ..data import cache
        get_bars = cache.get_bars
    if configured_source is None:
        from ..data import registry
        configured_source = registry.configured_source

    symbols = universe.resolve(full["universe"])
    classes = {s: instrument(s).asset_class for s in symbols}
    crypto_only = all(c == "crypto" for c in classes.values())
    need_bars = res.facts["max_lookback"] + days + HISTORY_MARGIN_BARS
    start = as_of_dt - timedelta(days=_calendar_days(need_bars, crypto_only))
    eq_fresh = cal.last_close_before(as_of_dt) if not crypto_only else None

    bars_by: Dict[str, List[dict]] = {}
    missing: List[str] = []
    for s in symbols:
        check_time(f"loading {s}")
        rows = get_bars(s, "1Day", start, as_of_dt)
        if not rows:
            missing.append(s)
            continue
        fresh = as_of_dt if classes[s] == "crypto" else eq_fresh
        rows = get_bars(s, "1Day", start, as_of_dt, require_fresh_through=fresh)
        bars_by[s] = [b for b in rows if parse_ts(b["known_at"]) <= as_of_dt]
    present = [s for s in symbols if s in bars_by]
    if not present:
        raise InvalidRequest("None of the strategy's symbols has daily bars at the configured source.")
    panel = Panel.from_bars(bars_by, symbols=present)
    if len(panel.dates) == 0:
        raise InvalidRequest("No completed daily bars in the preview range.")
    check_time("building the panel")
    signal = evaluate(full, panel, ext_values=ext_values)
    check_time("evaluating the signal")
    t = len(panel.dates) - 1
    weights = portfolio.target_weights(full, signal[t], panel, t)
    row_sig = [float(v) for v in signal[t]]
    order = sorted((j for j, v in enumerate(row_sig) if math.isfinite(v)),
                   key=lambda j: (-row_sig[j], panel.symbols[j]))
    rank = {j: i + 1 for i, j in enumerate(order)}
    rows_out = []
    for j, s in enumerate(panel.symbols):
        v = row_sig[j]
        rows_out.append({"symbol": s, "signal": v if math.isfinite(v) else None, "rank": rank.get(j),
                         "target_weight": weights.get(s, 0.0)})
    rows_out.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["symbol"]))
    first = max(0, len(panel.dates) - days)
    history = portfolio.held_weights(full, signal, panel, first)
    check_time("building the history")
    warnings = list(res.warnings)
    if missing:
        warnings.append({"path": "$.universe", "code": "symbols_missing",
                         "message": f"No daily bars at the source for {', '.join(missing)} — excluded."})
    nan_now = [panel.symbols[j] for j, v in enumerate(row_sig) if not math.isfinite(v)]
    if nan_now:
        warnings.append({"path": "$.signal", "code": "no_signal",
                         "message": f"No signal value on {panel.dates[t].isoformat()} for {', '.join(nan_now)} "
                                    "(not enough history in the window or missing inputs)."})
    sources = sorted({configured_source(bars_data_type(classes[s])) for s in present})
    return {
        "as_of": iso(as_of_dt),
        "rows": rows_out,
        "history": history,
        "warnings": warnings,
        "data": {"source": ", ".join(sources), "from": panel.dates[0].isoformat(),
                 "to": panel.dates[-1].isoformat(), "symbols_missing": missing},
    }

"""Memory of COMPUTED facts (SPEC §13.2, stage 5a).

When a backtest or validation run finishes (``backtest.jobs._after_job`` calls ``on_run_finished``),
deterministic facts are derived from its run card — metrics, failed gates with value/threshold, cost
share, turnover, dominant periods/regimes and top contributors from the §11 attribution, parameter-grid
flatness (validations), warnings — and written insert-only to ``memory_facts``. Never for holdout runs,
never AI-written text. The Studio prompt gets ``select_for_prompt`` (the current family's facts + the
most relevant facts of other families: same universe, shared operators, recency; ≤ 20 facts,
≤ 6,000 chars) and shows the same list to the user.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Iterable, List, Optional

from ..storage import db

logger = logging.getLogger(__name__)

MAX_FACTS = 20
MAX_CHARS = 6000
OWN_FAMILY_MAX = 12            # at most this many of the current family; the rest from related families
SCAN_LIMIT = 2000              # newest facts considered for the ranking
RUN_KINDS = ("backtest", "validation")      # never "holdout"
KINDS = ("metrics", "gate_failed", "validation_result", "contributors", "periods", "regimes",
         "grid_flatness", "warning", "loop_result")
PROMPT_RUN_KINDS = RUN_KINDS + ("loop",)    # SPEC §16.9: the discovery loop's summary fact (run_id = loop id)


# --- formatting (deterministic) ----------------------------------------------------------------------

def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _pct(x, nd: int = 1) -> str:
    return f"{float(x) * 100:.{nd}f} %" if _num(x) else "n/a"


def _f(x, nd: int = 2) -> str:
    return f"{float(x):.{nd}f}" if _num(x) else "n/a"


def _usd(x) -> str:
    if not _num(x):
        return "n/a"
    return ("-" if x < 0 else "") + f"${abs(float(x)):,.0f}"


def _block(d) -> dict:
    """A §11 block or {} when missing / ``{unavailable}``."""
    return d if isinstance(d, dict) and "unavailable" not in d else {}


# --- spec helpers ------------------------------------------------------------------------------------

def operators_of(spec) -> List[str]:
    """Sorted DSL operator names used anywhere in the spec (signal, features, …)."""
    out = set()

    def walk(n):
        if isinstance(n, dict):
            if isinstance(n.get("op"), str):
                out.add(n["op"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk({k: v for k, v in (spec or {}).items() if k in ("signal", "features", "portfolio", "risk")})
    return sorted(out)


def universe_key_of(spec) -> Optional[str]:
    try:
        from ..validation import families
        return families.universe_key(spec)
    except Exception:     # noqa: BLE001 — an unresolvable universe has no key (no ranking bonus)
        return None


# --- facts of one run ----------------------------------------------------------------------------------

def build_facts(card: dict) -> List[dict]:
    """[{kind, text, data}] of a done backtest/validation card (deterministic)."""
    kind = card.get("kind") or "backtest"
    metrics = card.get("metrics") or {}
    perf, bench = metrics.get("performance") or {}, metrics.get("benchmark") or {}
    costs, expo = metrics.get("costs") or {}, metrics.get("exposure") or {}
    rng = card.get("range") or {}
    span = f"{rng.get('start', '?')}–{rng.get('end', '?')}"
    label = "Validation (training run)" if kind == "validation" else "Backtest"
    out: List[dict] = []
    m = {"sharpe": perf.get("sharpe"), "total_return": perf.get("total_return"), "cagr": perf.get("cagr"),
         "max_drawdown": perf.get("max_drawdown"), "alpha_annual": bench.get("alpha_annual"),
         "beta": bench.get("beta"), "cost_share_of_gross": costs.get("pct_of_gross_pnl"),
         "turnover_annual": expo.get("turnover_annual"), "trades_closed": perf.get("trades_closed")}
    cost_txt = (f"{float(m['cost_share_of_gross']):.1f} % of gross P&L" if _num(m["cost_share_of_gross"])
                else "n/a")
    out.append({"kind": "metrics", "data": m, "text": (
        f"{label} {span}: Sharpe {_f(m['sharpe'])}, total return {_pct(m['total_return'])}, max drawdown "
        f"{_pct(m['max_drawdown'])}, alpha {_pct(m['alpha_annual'])}/yr (beta {_f(m['beta'])}), costs "
        f"{cost_txt}, turnover {_f(m['turnover_annual'], 1)}×/yr, {m['trades_closed'] if _num(m['trades_closed']) else 'n/a'} "
        "closed trades.")})
    attr = card.get("attribution") if isinstance(card.get("attribution"), dict) else {}
    bys = _block(attr.get("by_symbol"))
    top = [r for r in (bys.get("top") or [])[:3] if _num(r.get("pnl"))]
    bottom = [r for r in (bys.get("bottom") or [])[:3] if _num(r.get("pnl"))]
    if top or bottom:
        out.append({"kind": "contributors", "data": {"top": [{"symbol": r.get("symbol"), "pnl": r["pnl"]} for r in top],
                                                     "bottom": [{"symbol": r.get("symbol"), "pnl": r["pnl"]}
                                                                for r in bottom]},
                    "text": "Top contributors: " + (", ".join(f"{r.get('symbol')} {_usd(r['pnl'])}" for r in top)
                                                  or "none")
                            + "; worst: " + (", ".join(f"{r.get('symbol')} {_usd(r['pnl'])}" for r in bottom)
                                             or "none") + "."})
    per = _block(attr.get("periods"))
    best, worst = (per.get("best") or [None])[0], (per.get("worst") or [None])[0]
    if best or worst:
        def ptxt(p):
            return (f"{p.get('start')}–{p.get('end')} strategy {_pct(p.get('strategy_return'))} vs benchmark "
                    f"{_pct(p.get('benchmark_return'))}") if p else "n/a"
        out.append({"kind": "periods", "data": {"best": best, "worst": worst},
                    "text": f"Best 63-session window {ptxt(best)}; worst {ptxt(worst)}."})
    reg = _block(attr.get("by_regime"))
    regs = [r for r in reg.get("regimes") or [] if _num(r.get("strategy_return_ann")) and r.get("days")]
    if regs:
        hi = max(regs, key=lambda r: (r["strategy_return_ann"], r.get("regime") or ""))
        lo = min(regs, key=lambda r: (r["strategy_return_ann"], r.get("regime") or ""))
        out.append({"kind": "regimes", "data": {"best": hi, "worst": lo},
                    "text": f"Best market regime {hi.get('regime')} ({_pct(hi['strategy_return_ann'])}/yr over "
                            f"{hi.get('days')} days); worst {lo.get('regime')} ({_pct(lo['strategy_return_ann'])}/yr "
                            f"over {lo.get('days')} days)."})
    if kind == "validation":
        rep = _validation_report(card)
        gates = (rep or {}).get("gates") or []
        failed = [g for g in gates if g.get("status") == "fail"]
        for g in failed:
            out.append({"kind": "gate_failed", "data": {k: g.get(k) for k in ("gate", "name", "value", "threshold")},
                        "text": f"Gate {g.get('gate')} {g.get('name')} failed: value {_f(g.get('value'), 3)} "
                                f"(threshold {_f(g.get('threshold'), 3)})."})
        if gates:
            out.append({"kind": "validation_result",
                        "data": {"passed": bool((rep or {}).get("passed")), "failed": [g.get("gate") for g in failed],
                                 "trial_count": (rep or {}).get("trial_count")},
                        "text": ("Validation passed." if (rep or {}).get("passed") else
                                 "Validation failed (" + (", ".join(str(g.get("gate")) for g in failed) or "-") + ")")
                                + f" at trial count {(rep or {}).get('trial_count')}."})
        grid = _grid_flatness(rep)
        if grid:
            out.append({"kind": "grid_flatness", "data": grid,
                        "text": f"Parameter grid ({grid.get('configs')} configs): Sharpe min {_f(grid.get('sharpe_min'))}, "
                                f"median {_f(grid.get('sharpe_median'))}, max {_f(grid.get('sharpe_max'))}."})
    for w in (card.get("warnings") or [])[:6]:
        if isinstance(w, dict) and w.get("code"):
            msg = " ".join(str(w.get("message") or "").split())[:300]
            out.append({"kind": "warning", "data": {"code": w.get("code")}, "text": f"Warning {w['code']}: {msg}"})
    return out


def _validation_report(card: dict):
    try:
        from ..validation import report
        return report.effective(card)
    except Exception:     # noqa: BLE001
        return ((card.get("metrics") or {}).get("validation") or {}).get("report")


def _grid_flatness(rep) -> Optional[dict]:
    from ..backtest import attribution
    try:
        gf = (rep or {}).get("gate_facts") or attribution.gate_facts(rep)
    except Exception:     # noqa: BLE001
        return None
    vals = {}
    for g in gf or []:
        for f in g.get("facts") or []:
            fid = f.get("id") or ""
            if fid.startswith("val.grid."):
                vals[fid[len("val.grid."):]] = f.get("value")
    return vals or None


def on_run_finished(run_id: str) -> int:
    """Finish hook: writes the memory facts of a done backtest/validation card -> number written (0 for
    holdout / unfinished / unknown runs or when facts exist already)."""
    if not db.is_open():
        return 0
    from ..backtest import runcard
    card = runcard.get(run_id)
    if card is None or card.get("status") != "done" or (card.get("kind") or "backtest") not in RUN_KINDS:
        return 0
    row = db.strategy_get(card["strategy_id"])
    family_id = (row or {}).get("family_id") or card["strategy_id"]
    spec = card.get("spec") or {}
    common = {"universe_key": universe_key_of(spec), "operators": operators_of(spec),
              "run_kind": card.get("kind") or "backtest",
              "range": {k: (card.get("range") or {}).get(k) for k in ("start", "end")}}
    rows = []
    for f in build_facts(card):
        fact = dict(common, text=f["text"], data=f["data"])
        rows.append({"family_id": family_id, "strategy_id": card["strategy_id"], "version": int(card["version"]),
                     "run_id": run_id, "kind": f["kind"],
                     "fact_json": json.dumps(fact, ensure_ascii=False, sort_keys=True, default=str)})
    n = db.memory_facts_insert(run_id, rows)
    if n:
        logger.info("memory: %d facts written for run %s", n, run_id)
    return n


# --- selection for the Studio prompt ---------------------------------------------------------------------

def short(f: dict) -> dict:
    return {"id": f["id"], "family_id": f["family_id"], "kind": f["kind"], "text": (f.get("fact") or {}).get("text"),
            "run_id": f["run_id"]}


def select_for_prompt(family_id: Optional[str], spec: Optional[dict], *, max_facts: int = MAX_FACTS,
                      max_chars: int = MAX_CHARS, facts: Optional[Iterable[dict]] = None) -> List[dict]:
    """Deterministic memory block: the current family's newest facts (≤ OWN_FAMILY_MAX), then other
    families' facts ranked by (same universe key: +4, shared operators: +1 each up to 3, newer first);
    ≤ ``max_facts`` facts and ≤ ``max_chars`` characters of fact text. -> memory_fact rows."""
    pool = list(facts) if facts is not None else db.memory_facts_list(limit=SCAN_LIMIT)
    pool = [f for f in pool if isinstance(f.get("fact"), dict)
            and (f["fact"].get("run_kind") in PROMPT_RUN_KINDS)]
    ukey = universe_key_of(spec) if spec else None
    ops = set(operators_of(spec)) if spec else set()
    own = sorted((f for f in pool if family_id is not None and f["family_id"] == family_id),
                 key=lambda f: -int(f["id"]))[:OWN_FAMILY_MAX]

    def score(f):
        fact = f["fact"]
        s = 0
        if ukey is not None and fact.get("universe_key") == ukey:
            s += 4
        s += min(3, len(ops & set(fact.get("operators") or [])))
        return s

    others = sorted((f for f in pool if f["family_id"] != family_id),
                    key=lambda f: (-score(f), -int(f["id"])))
    out, chars = [], 0
    for f in own + [o for o in others if score(o) > 0 or not ops]:
        text = str(f["fact"].get("text") or "")
        if len(out) >= max_facts or chars + len(text) > max_chars:
            continue
        out.append(f)
        chars += len(text)
    return out

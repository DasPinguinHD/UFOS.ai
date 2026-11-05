"""Deterministic JSON / CSV export of backtest frames and test results.

Determinism: sorted JSON keys, floats written at full precision (Python's
shortest round-trip ``repr``, so a value read back is bit-identical and the
exported loss / VaR reproduce the exceedance flags), "\\n" line endings,
UTF-8, and a ``meta`` block with the package and numpy versions and the seed.
The same inputs give the same bytes.

Alpha keys are ``repr(float(alpha))`` (e.g. "0.99", "0.975"): distinct alphas
always give distinct keys.

The default output folder is ``RiskLab/out/`` (git-ignored), anchored to the
RiskLab root, never the current directory. If the package is not run from its
source tree (no ``pyproject.toml`` there), ``out_dir`` must be given.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

import numpy as np

from risklab import __version__
from risklab.backtest.base import TestResult
from risklab.engine import BacktestFrame

PathLike = Union[str, Path]
#: RiskLab root of the source tree (``RiskLab/``), from ``src/risklab/report/export.py``.
_RISKLAB_ROOT = Path(__file__).resolve().parents[3]


def default_out_dir() -> Path:
    """``RiskLab/out`` (git-ignored). Raises if the package is not in its source tree."""
    if not (_RISKLAB_ROOT / "pyproject.toml").is_file() or not (_RISKLAB_ROOT / "src" / "risklab").is_dir():
        raise ValueError("risklab is not running from its source tree: pass out_dir explicitly")
    return _RISKLAB_ROOT / "out"


def _f(x) -> Optional[float]:
    """Plain float at full precision (None stays None, NaN/inf refused)."""
    if x is None:
        return None
    v = float(x)
    if not np.isfinite(v):
        raise ValueError("NaN / inf cannot be exported")
    return v


def _fs(x) -> str:
    return "" if x is None else repr(_f(x))


def _akey(alpha: float) -> str:
    return repr(float(alpha))


def _meta(seed: Optional[int], extra: Optional[dict]) -> dict:
    meta = {"risklab_version": __version__, "numpy_version": np.__version__, "seed": seed}
    if extra:
        meta.update(extra)
    return meta


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _write(path: PathLike, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return p


# ---- BacktestFrame -------------------------------------------------------

def frame_to_dict(frame: BacktestFrame, seed: Optional[int] = None) -> dict:
    alphas = frame.alphas
    return {
        "meta": _meta(seed, {"frame": frame.meta}),
        "t": [int(x) for x in frame.t],
        "loss": [_f(x) for x in frame.loss],
        "var": {_akey(a): [_f(x) for x in frame.var[a]] for a in alphas},
        "es": None if frame.es is None else {_akey(a): [_f(x) for x in frame.es[a]] for a in alphas},
        "exceedance": {_akey(a): [bool(x) for x in frame.exceedance[a]] for a in alphas},
    }


def frame_from_dict(d: dict) -> BacktestFrame:
    keys = sorted(d["var"], key=float)
    return BacktestFrame(
        t=np.array(d["t"], dtype=np.int64),
        loss=np.array(d["loss"], dtype=np.float64),
        var={float(k): np.array(d["var"][k], dtype=np.float64) for k in keys},
        es=None if d["es"] is None else {float(k): np.array(d["es"][k], dtype=np.float64) for k in keys},
        exceedance={float(k): np.array(d["exceedance"][k], dtype=bool) for k in keys},
        meta=dict(d["meta"].get("frame", {})),
    )


def frame_to_json(frame: BacktestFrame, path: PathLike, seed: Optional[int] = None) -> Path:
    return _write(path, _dumps(frame_to_dict(frame, seed)))


def read_frame_json(path: PathLike) -> BacktestFrame:
    with open(path, encoding="utf-8") as fh:
        return frame_from_dict(json.load(fh))


def frame_to_csv(frame: BacktestFrame, path: PathLike, seed: Optional[int] = None) -> Path:
    """One row per day: t, loss, then var / es / exceed per alpha.

    The first line is a comment ``# meta: {...}`` with the meta block (JSON).
    """
    alphas = frame.alphas
    header = ["t", "loss"]
    for a in alphas:
        header.append(f"var_{_akey(a)}")
        if frame.es is not None:
            header.append(f"es_{_akey(a)}")
        header.append(f"exceed_{_akey(a)}")
    buf = io.StringIO()
    meta = _meta(seed, {"frame": frame.meta})
    buf.write("# meta: " + json.dumps(meta, sort_keys=True, ensure_ascii=False) + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for i in range(len(frame)):
        row = [str(int(frame.t[i])), _fs(frame.loss[i])]
        for a in alphas:
            row.append(_fs(frame.var[a][i]))
            if frame.es is not None:
                row.append(_fs(frame.es[a][i]))
            row.append("1" if frame.exceedance[a][i] else "0")
        w.writerow(row)
    return _write(path, buf.getvalue())


def read_frame_csv(path: PathLike) -> BacktestFrame:
    with open(path, encoding="utf-8", newline="") as fh:
        first = fh.readline()
        if not first.startswith("# meta: "):
            raise ValueError("missing '# meta:' line")
        meta = json.loads(first[len("# meta: "):])
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    col = {name: i for i, name in enumerate(header)}
    alphas = sorted({float(h[len("var_"):]) for h in header if h.startswith("var_")})
    has_es = any(h.startswith("es_") for h in header)

    def column(name, dtype):
        return np.array([r[col[name]] for r in body], dtype=dtype)

    return BacktestFrame(
        t=column("t", np.int64),
        loss=column("loss", np.float64),
        var={a: column(f"var_{_akey(a)}", np.float64) for a in alphas},
        es={a: column(f"es_{_akey(a)}", np.float64) for a in alphas} if has_es else None,
        exceedance={a: column(f"exceed_{_akey(a)}", np.int64).astype(bool) for a in alphas},
        meta=dict(meta.get("frame", {})),
    )


# ---- TestResult ----------------------------------------------------------

_RESULT_FIELDS = ["name", "statistic", "df", "p_value", "reject_at", "decision", "n_obs",
                  "n_exceed", "notes", "details"]


def _result_dict(r: TestResult) -> dict:
    d = asdict(r)
    for k in ("statistic", "p_value", "reject_at"):
        d[k] = _f(d[k])
    return d


def results_to_json(results: Iterable[TestResult], path: PathLike, seed: Optional[int] = None) -> Path:
    payload = {"meta": _meta(seed, None), "results": [_result_dict(r) for r in results]}
    return _write(path, _dumps(payload))


def read_results_json(path: PathLike) -> List[TestResult]:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return [TestResult(**r) for r in d["results"]]


def results_to_csv(results: Iterable[TestResult], path: PathLike, seed: Optional[int] = None) -> Path:
    buf = io.StringIO()
    buf.write("# meta: " + json.dumps(_meta(seed, None), sort_keys=True) + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_RESULT_FIELDS)
    for r in results:
        d = _result_dict(r)
        row = []
        for k in _RESULT_FIELDS:
            v = d[k]
            if v is None:
                row.append("")
            elif k == "details":
                row.append(json.dumps(v, sort_keys=True, ensure_ascii=False, allow_nan=False))
            elif isinstance(v, float):
                row.append(_fs(v))
            else:
                row.append(str(v))
        w.writerow(row)
    return _write(path, buf.getvalue())


# ---- generic tables (study results) --------------------------------------

def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (float, np.floating)):
        return _fs(v)
    return str(v)


def table_to_json(rows: Sequence[dict], path: PathLike, seed: Optional[int] = None,
                  meta: Optional[dict] = None) -> Path:
    """``{"meta": ..., "rows": [...]}`` with sorted keys and full-precision floats."""
    clean = [{k: (_f(v) if isinstance(v, (float, np.floating)) else v) for k, v in r.items()} for r in rows]
    return _write(path, _dumps({"meta": _meta(seed, meta), "rows": clean}))


def table_to_csv(rows: Sequence[dict], columns: Sequence[str], path: PathLike, seed: Optional[int] = None,
                 meta: Optional[dict] = None) -> Path:
    """CSV with a ``# meta: {...}`` first line and the given column order."""
    buf = io.StringIO()
    buf.write("# meta: " + json.dumps(_meta(seed, meta), sort_keys=True, ensure_ascii=False) + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(list(columns))
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return _write(path, buf.getvalue())


def text_to_file(text: str, path: PathLike) -> Path:
    """Write text (e.g. a Markdown summary) as UTF-8 with "\\n" line endings."""
    return _write(path, text if text.endswith("\n") else text + "\n")


def export_run(frame: BacktestFrame, results: Iterable[TestResult] = (), out_dir: Optional[PathLike] = None,
               stem: str = "run", seed: Optional[int] = None) -> List[Path]:
    """Write ``<stem>_frame.json/.csv`` and ``<stem>_tests.json/.csv`` into ``out_dir``.

    ``out_dir=None`` means :func:`default_out_dir` (``RiskLab/out``).
    """
    results = list(results)
    out = default_out_dir() if out_dir is None else Path(out_dir)
    return [
        frame_to_json(frame, out / f"{stem}_frame.json", seed),
        frame_to_csv(frame, out / f"{stem}_frame.csv", seed),
        results_to_json(results, out / f"{stem}_tests.json", seed),
        results_to_csv(results, out / f"{stem}_tests.csv", seed),
    ]

"""Alerts (SPEC §19.3): events the backend emits; the WPF polls them and shows Windows toasts.

Producers: ``emit`` (decay monitor, report scheduler), ``scan_audit`` (audit rows after the cursor
``mon.alerts.audit_cursor``; first start = the newest id, so history does not flood the user) and ``watch_links``
(connected broker accounts whose link is down ≥ ``BROKER_DOWN_MINUTES``). ``mon_alerts`` / ``mon_alert_acks`` are
insert-only; ``dedupe_key`` is UNIQUE (an event becomes at most one alert). Texts are plain backend text — never AI
output, keys or account numbers.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from ..storage import db

logger = logging.getLogger(__name__)

KINDS = {
    "decay": {"severity": "critical", "toast": True, "label": "Decay — strategy paused"},
    "reconciliation_halt": {"severity": "critical", "toast": True, "label": "Broker halted — reconciliation mismatch"},
    "kill_switch": {"severity": "critical", "toast": True, "label": "Kill switch"},
    "drawdown_kill": {"severity": "critical", "toast": True, "label": "Drawdown kill"},
    "daily_loss_limit": {"severity": "warning", "toast": True, "label": "Daily loss limit reached"},
    "autotrade_off": {"severity": "warning", "toast": True, "label": "Auto trading off (dead-man switch)"},
    "broker_disconnected": {"severity": "warning", "toast": True, "label": "Broker unreachable"},
    "broker_reconnected": {"severity": "info", "toast": False, "label": "Broker reachable again"},
    "incubation_finished": {"severity": "info", "toast": True, "label": "Incubation finished"},
    "protective_exit_while_paused": {"severity": "warning", "toast": True,
                                     "label": "Protective exit of a paused strategy"},
    "report_ready": {"severity": "info", "toast": False, "label": "Report ready"},
    "allocation_failed": {"severity": "warning", "toast": True,                 # fix1023 round 3
                          "label": "Allocation after an incubation start failed"},
    # the Council of AIs as a platform strategy (SP §22.9) — replace the old engine's flip / order toasts
    "council_flip": {"severity": "info", "toast": True, "label": "Council of AIs — recommendation changed"},
    "council_order_filled": {"severity": "info", "toast": True, "label": "Council of AIs — order"},
    "council_run_failed": {"severity": "warning", "toast": True, "label": "Council of AIs — cycle failed"},
    "council_budget_reached": {"severity": "warning", "toast": True, "label": "Council of AIs — AI budget reached"},
    "council_orders_off": {"severity": "warning", "toast": True, "label": "Council of AIs — orders switched off"},
}
CURSOR_KEY = "mon.alerts.audit_cursor"
BROKER_DOWN_MINUTES = 10           # default — user confirm
DOWN_STATES = ("waiting", "error", "auth_failed")
SCAN_BATCH = 500
TITLE_MAX = 160
BODY_MAX = 1000
NOTICE = " Paper/demo trading · simulated money."          # S10-H context line (core/notices.PAPER_DEMO)

_scan_lock = threading.Lock()
_down: Dict[str, dict] = {}        # account_id -> {since, alerted, key}


def _now(now: Optional[datetime] = None) -> datetime:
    from ..execution import accounts
    return (now or accounts.now()).astimezone(timezone.utc)


def emit(kind: str, title: str, body: str, dedupe_key: str, *, account_id: Optional[str] = None,
         strategy_id: Optional[str] = None, ref: Optional[str] = None, data: Optional[dict] = None,
         now: Optional[datetime] = None) -> Optional[int]:
    """Stores one alert; returns its id, or None when the dedupe key exists already."""
    if kind not in KINDS:
        raise ValueError(f"unknown alert kind {kind!r}")
    k = KINDS[kind]
    ts = _now(now).isoformat(timespec="seconds")
    with db._connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO mon_alerts (ts, kind, severity, title, body, account_id, strategy_id, ref,"
                           " toast, dedupe_key, data_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (ts, kind, k["severity"], str(title)[:TITLE_MAX], str(body)[:BODY_MAX], account_id,
                            strategy_id, ref, 1 if k["toast"] else 0, str(dedupe_key)[:200],
                            json.dumps(data or {}, default=str)))
        return int(cur.lastrowid) if cur.rowcount == 1 else None


def _out(r) -> dict:
    return {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "severity": r["severity"], "title": r["title"],
            "body": r["body"], "account_id": r["account_id"], "strategy_id": r["strategy_id"], "ref": r["ref"],
            "toast": bool(r["toast"]), "acked": r["acked_at"] is not None, "acked_at": r["acked_at"],
            "data": json.loads(r["data_json"]) if r["data_json"] else {}}


def list_alerts(after: int = 0, limit: int = 100, unacked: bool = False) -> dict:
    q = ("SELECT a.*, k.ts AS acked_at FROM mon_alerts a LEFT JOIN mon_alert_acks k ON k.alert_id = a.id"
         " WHERE a.id > ?")
    args: list = [int(after)]
    if unacked:
        q += " AND k.alert_id IS NULL"
    q += " ORDER BY a.id DESC LIMIT ?"
    args.append(int(limit))
    with db._connect() as conn:
        rows = conn.execute(q, args).fetchall()
        last = conn.execute("SELECT MAX(id) FROM mon_alerts").fetchone()[0]
        un = conn.execute("SELECT COUNT(*) FROM mon_alerts a LEFT JOIN mon_alert_acks k ON k.alert_id = a.id WHERE"
                          " k.alert_id IS NULL").fetchone()[0]
    return {"alerts": [_out(r) for r in rows], "last_id": int(last or 0), "unacked": int(un),
            "kinds": {k: dict(v) for k, v in KINDS.items()}}


def counts() -> dict:
    with db._connect() as conn:
        last = conn.execute("SELECT MAX(id) FROM mon_alerts").fetchone()[0]
        un = conn.execute("SELECT COUNT(*) FROM mon_alerts a LEFT JOIN mon_alert_acks k ON k.alert_id = a.id WHERE"
                          " k.alert_id IS NULL").fetchone()[0]
    return {"unacked": int(un), "last_id": int(last or 0)}


def ack(ids: Optional[Iterable[int]] = None, all_: bool = False, actor: str = "user",
        now: Optional[datetime] = None) -> dict:
    ts = _now(now).isoformat(timespec="seconds")
    with db._connect() as conn:
        if all_:
            todo = [r[0] for r in conn.execute("SELECT a.id FROM mon_alerts a LEFT JOIN mon_alert_acks k ON"
                                               " k.alert_id = a.id WHERE k.alert_id IS NULL").fetchall()]
        else:
            todo = sorted({int(i) for i in ids or []})
        n = 0
        for i in todo:
            cur = conn.execute("INSERT OR IGNORE INTO mon_alert_acks (alert_id, ts, actor) SELECT id, ?, ? FROM"
                               " mon_alerts WHERE id = ?", (ts, actor, i))
            n += cur.rowcount
    if n:
        try:
            db.add_audit(actor, "alerts_acked", {"count": n, "all": bool(all_)})
        except Exception:        # noqa: BLE001 — an ack is not a trading action
            logger.exception("audit row alerts_acked failed")
    return {"acked": n, "unacked": counts()["unacked"]}


# --- the audit scanner ----------------------------------------------------------------------------------------

def _pct(v) -> str:
    try:
        return f"{float(v):.2f} %"
    except (TypeError, ValueError):
        return "?"


def _strategy_name(sid: Optional[str]) -> str:
    if not sid:
        return "?"
    row = db.strategy_get(sid)
    return (row or {}).get("name") or sid


def map_audit(row: dict) -> Optional[dict]:
    """An audit row -> alert kwargs, or None (not alert-worthy)."""
    a, d, rid = row["action"], row.get("detail") or {}, row["id"]
    acc = d.get("account_id")
    if a == "broker_reconcile_mismatch":
        return dict(kind="reconciliation_halt", title=f"Broker halted: reconciliation mismatch ({acc})",
                    body=f"{d.get('reason') or 'The broker and the platform disagree.'} No new orders on {acc} until "
                         f"you resolve it in Settings → Brokers." + NOTICE, account_id=acc)
    if a == "platform_kill":
        n = len(d.get("accounts") or [])
        return dict(kind="kill_switch", title="Kill switch: all platform accounts",
                    body=f"Reason: {d.get('reason') or 'kill switch pressed'}. {n} account(s): orders cancelled, "
                         "positions closed, auto trading off." + NOTICE)
    if a == "broker_kill":
        return dict(kind="kill_switch", title=f"Kill switch: {acc}",
                    body=f"Reason: {d.get('reason') or 'kill switch'}. Open orders cancelled at the broker, "
                         f"{d.get('orders', 0)} closing order(s)" + (" (pending until the broker answers)"
                                                                     if d.get("pending") else "") + "." + NOTICE,
                    account_id=acc)
    if a == "post_trade_halt" and d.get("kind") == "drawdown":
        return dict(kind="drawdown_kill", title=f"Drawdown kill: {acc}",
                    body=f"Equity {_pct(d.get('drawdown_pct'))} below its peak — orders cancelled, positions closed, "
                         "auto trading off. Resume in Portfolio & Trading." + NOTICE, account_id=acc)
    if a == "post_trade_halt" and d.get("kind") == "daily_loss":
        return dict(kind="daily_loss_limit", title=f"Daily loss limit reached: {acc} ({d.get('class')})",
                    body=f"Day P&L {_pct(d.get('pnl_pct'))} (limit −{d.get('limit_pct')} %): no new risk-increasing "
                         f"{d.get('class')} orders until {d.get('until')}." + NOTICE, account_id=acc)
    if a == "autotrade_heartbeat_off":
        return dict(kind="autotrade_off", title="Auto trading switched off (dead-man switch)",
                    body=f"No heartbeat from the app for {d.get('timeout_minutes')} minutes (last seen "
                         f"{d.get('last_client_seen')}); {d.get('cancelled_orders', 0)} runtime order(s) cancelled, "
                         "positions kept." + NOTICE)
    if a == "incubation_completed":
        sid = d.get("strategy_id")
        res = d.get("result") or "no result"
        return dict(kind="incubation_finished", title=f"Incubation finished: {_strategy_name(sid)} — {res}"[:TITLE_MAX],
                    body=("; ".join(str(x) for x in (d.get("reasons") or []))[:600] or f"Result: {res}.") +
                         " The strategy keeps trading on paper until you abort or retire it." + NOTICE,
                    strategy_id=sid, ref=d.get("run_id"))
    _ = rid
    return None


def scan_audit(now: Optional[datetime] = None) -> int:
    """Turns new audit rows into alerts; returns the number of alerts created."""
    with _scan_lock:
        raw = db.meta_get(CURSOR_KEY)
        with db._connect() as conn:
            newest = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit").fetchone()[0])
        if raw is None:
            db.meta_set(CURSOR_KEY, str(newest))         # first start: no flood of historical alerts
            return 0
        try:
            cursor = int(raw)
        except ValueError:
            cursor = newest
        made = 0
        while True:
            with db._connect() as conn:
                rows = conn.execute("SELECT id, ts, actor, action, detail_json FROM audit WHERE id > ? ORDER BY id"
                                    " LIMIT ?", (cursor, SCAN_BATCH)).fetchall()
            if not rows:
                break
            for r in rows:
                row = {"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                       "detail": json.loads(r["detail_json"]) if r["detail_json"] else {}}
                try:
                    m = map_audit(row)
                except Exception:    # noqa: BLE001 — a strange row never blocks the scanner
                    logger.exception("alert mapping of audit row %s failed", r["id"])
                    m = None
                if m is not None:
                    kind = m.pop("kind")
                    if emit(kind, m.pop("title"), m.pop("body"), f"audit:{r['id']}", now=now,
                            data={"audit_id": r["id"], "action": r["action"]}, **m) is not None:
                        made += 1
                cursor = int(r["id"])
            db.meta_set(CURSOR_KEY, str(cursor))
            if len(rows) < SCAN_BATCH:
                break
        return made


# --- broker link watcher --------------------------------------------------------------------------------------

def watch_links(now: Optional[datetime] = None) -> int:
    from ..execution import accounts
    from ..execution.brokers import mirror
    now = _now(now)
    made = 0
    try:
        ids = [r["id"] for r in accounts.rows() if mirror.is_broker_id(r["id"])]
    except Exception:            # noqa: BLE001 — hedgefund.db unavailable: nothing to watch
        return 0
    for aid in ids:
        v = mirror.link(aid).view()
        st = _down.get(aid)
        if v["state"] in DOWN_STATES:
            if st is None:
                since = now
                if v.get("last_ok_at"):
                    try:
                        since = min(now, datetime.fromisoformat(v["last_ok_at"]))
                    except ValueError:
                        since = now
                st = _down[aid] = {"since": since, "alerted": False}
            if not st["alerted"] and now - st["since"] >= timedelta(minutes=BROKER_DOWN_MINUTES):
                key = f"broker_down:{aid}:{st['since'].isoformat(timespec='seconds')}"
                new_id = emit("broker_disconnected", f"Broker unreachable: {aid}",
                     f"No answer for {int((now - st['since']).total_seconds() // 60)} minutes ({v.get('message')}). "
                     "No new orders on this account until it answers and reconciles; retries continue." + NOTICE,
                     key, account_id=aid, now=now, data={"state": v["state"], "since": st["since"].isoformat()})
                st["alerted"] = True
                made += 1 if new_id is not None else 0           # a deduped alert is not a new one
        elif v["state"] == "ok" and st is not None:
            if st["alerted"]:
                new_id = emit("broker_reconnected", f"Broker reachable again: {aid}",
                     f"The broker answers again (down since {st['since'].isoformat(timespec='minutes')}).",
                     f"broker_up:{aid}:{st['since'].isoformat(timespec='seconds')}", account_id=aid, now=now)
                made += 1 if new_id is not None else 0
            _down.pop(aid, None)
    return made


def reset_state() -> None:
    """Tests: forget the link watcher's memory."""
    _down.clear()


def in_period(start: str, end: str) -> List[dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT kind, account_id FROM mon_alerts WHERE ts >= ? AND ts < ?", (start, end)).fetchall()
    return [dict(r) for r in rows]

"""OS1 routes (SP §21.4, payloads in SP §21.11): ``/v2/options-archive/*`` — READ-ONLY views of the independent archive
process's ``data/options_archive.db`` (opened read-only) and its status file, the scheduled-task registration DATA for
the WPF (paths + summary — no script text; the WPF builds the task XML and runs schtasks.exe), and the audit endpoint.
The backend never starts / stops the archive and never registers a task. Sandbox mode: register / run-once audit
actions are refused (409 ``sandbox``)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Mapping

from .trading_routes import _bad, _body, _unknown

logger = logging.getLogger(__name__)

AUDIT_ACTIONS = ("registered", "register_failed", "unregistered", "unregister_failed", "run_once", "run_once_failed")
SANDBOX_REFUSED = ("registered", "register_failed", "run_once", "run_once_failed")
DETAIL_MAX = 300


def _cfg(data_dir=None):
    import os
    from strategy_platform.options_archive import config as oconfig
    return oconfig.load(os.path.join(data_dir, "options_archive_config.json") if data_dir else None)


def _config_error(ex) -> tuple:
    return 409, {"error": f"options archive config: {str(ex)[:200]}", "code": "config_invalid"}


class OptionsArchiveRoutesMixin:
    """Handlers of the OS1 routes."""

    data_dir = None                  # tests point it at a temp dir
    now_fn = None
    archive_sandbox = None           # tests: True / False; None = sandbox_profile.is_sandbox()
    archive_python = None            # tests: a fake venv python path; None = sys.executable

    def _availability(self):
        from strategy_platform.options_archive import registration
        return registration.availability(self.archive_python, self.archive_sandbox)

    def _now(self):
        return self.now_fn() if self.now_fn else datetime.now(timezone.utc)

    async def options_archive_status(self, query: Mapping, body, match: Mapping):
        from strategy_platform.options_archive import report
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        try:
            cfg = _cfg(self.data_dir)
        except (ValueError, OSError) as ex:
            return _config_error(ex)
        payload = await asyncio.to_thread(report.status_payload, cfg, self._now(), self.data_dir,
                                          self._availability())
        return 200, payload

    async def options_archive_gaps(self, query: Mapping, body, match: Mapping):
        from strategy_platform.options_archive import config as oconfig
        from strategy_platform.options_archive import db as odb
        from strategy_platform.options_archive import report
        from strategy_platform.options_archive.slots import NY
        errors = {k: "unknown parameter" for k in query if k not in ("date", "from", "to")}
        today = self._now().astimezone(NY).date()

        def day(name):
            v = query.get(name)
            if v is None:
                return None
            try:
                d_ = date.fromisoformat(v)
                if not 2000 <= d_.year <= 2100:
                    raise ValueError
                return d_
            except (ValueError, TypeError):
                errors[name] = "YYYY-MM-DD (years 2000 … 2100)"
                return None
        d, a, b = day("date"), day("from"), day("to")
        if "date" in query and ("from" in query or "to" in query):
            errors["date"] = "either date or from/to"
        start, end = (d, d) if d else (a or b or today, b or a or today)
        if not errors and (end < start or (end - start).days >= report.GAP_DAYS_MAX):
            errors["to"] = f"from … to must span 1 … {report.GAP_DAYS_MAX} days (both inclusive)"
        if errors:
            return _bad(errors)
        try:
            cfg = _cfg(self.data_dir)
        except (ValueError, OSError) as ex:
            return _config_error(ex)

        def go():
            conn = odb.open_ro(oconfig.paths(self.data_dir)["db"])
            try:
                return report.gaps(conn, start, end, cfg, self._now())
            finally:
                if conn is not None:
                    conn.close()
        return 200, await asyncio.to_thread(go)

    async def options_archive_runs(self, query: Mapping, body, match: Mapping):
        from strategy_platform.options_archive import config as oconfig
        from strategy_platform.options_archive import db as odb
        from strategy_platform.options_archive import report
        errors = {k: "unknown parameter" for k in query if k != "limit"}
        limit = 50
        if "limit" in query:
            try:
                limit = int(query["limit"])
                if not 1 <= limit <= 200:
                    raise ValueError
            except ValueError:
                errors["limit"] = "1..200"
        if errors:
            return _bad(errors)

        def go():
            conn = odb.open_ro(oconfig.paths(self.data_dir)["db"])
            try:
                return {"runs": report.runs(conn, limit)}
            finally:
                if conn is not None:
                    conn.close()
        return 200, await asyncio.to_thread(go)

    async def options_archive_registration(self, query: Mapping, body, match: Mapping):
        from strategy_platform.options_archive import registration
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        try:
            cfg = _cfg(self.data_dir)
        except (ValueError, OSError) as ex:
            return _config_error(ex)
        return 200, registration.payload(cfg, self.data_dir, executable=self.archive_python,
                                         sandbox=self.archive_sandbox)

    async def options_archive_audit(self, query: Mapping, body, match: Mapping):
        from ..storage import db
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object {action, detail?}"} if b is None else
                        {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("action", "detail"))
        action = b.get("action")
        if action not in AUDIT_ACTIONS:
            errors["action"] = " | ".join(AUDIT_ACTIONS)
        detail = b.get("detail")
        if detail is not None and (not isinstance(detail, str) or len(detail) > DETAIL_MAX):
            errors["detail"] = f"a string of at most {DETAIL_MAX} characters"
        if errors:
            return _bad(errors)
        if action in SANDBOX_REFUSED and self._availability()["sandbox"]:
            return 409, {"error": "The options archive cannot be registered or run manually while the backend is in "
                                  "sandbox mode (switch it off with tools/sandbox.py off). Unregister still works.",
                         "code": "sandbox"}
        try:
            aid = await asyncio.to_thread(db.add_audit, "user", f"options_archive_{action}",
                                          {"action": action, "detail": detail})
        except Exception as ex:      # noqa: BLE001 — storage unavailable: said, never a stack trace
            logger.warning("options archive audit failed: %s", type(ex).__name__)
            return 503, {"error": "The audit entry could not be stored (database unavailable).",
                         "code": "audit_unavailable"}
        return 200, {"ok": True, "audit_id": aid}


OPTIONS_ARCHIVE_ROUTES = (
    ("GET", "/v2/options-archive/status", "options_archive_status", False),
    ("GET", "/v2/options-archive/gaps", "options_archive_gaps", False),
    ("GET", "/v2/options-archive/runs", "options_archive_runs", False),
    ("GET", "/v2/options-archive/registration", "options_archive_registration", False),
    ("POST", "/v2/options-archive/audit", "options_archive_audit", True),
)

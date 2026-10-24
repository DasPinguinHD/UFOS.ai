"""Stage 9 routes (SPEC §19.10, payloads §19.11): ``/v2/monitoring/*`` and ``/v2/shadow/*`` — a mixin of
``routes.V2Api`` (same handler shape and strict input as the stage-7/8 routes; refused requests ``{error, code, …}``;
never a stack trace). GETs touch the ONE heartbeat (the WPF polls alerts). The CSV upload is
``multipart/form-data`` with one part ``file`` (``read_csv_body``, chunked, capped). PAPER / DEMO ONLY.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Mapping

from .trading_routes import STRAT_RE, _bad, _body, _note, _num, _reason, _unknown

logger = logging.getLogger(__name__)

REPORT_RE = re.compile(r"^rep-[0-9a-f]{12}$")
SHADOW_RE = re.compile(r"^sha-[0-9a-f]{12}$")
PROFILE_RE = re.compile(r"^prf-[0-9a-f]{12}$")
IMPORT_RE = re.compile(r"^imp-[0-9a-f]{12}$")
KEY_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,31}$")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$|^[A-Z0-9]{2,10}-USD$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEMPLATES = ("auto", "trade_republic", "scalable", "ibkr_flex", "generic")
SHADOW_NOT_FOUND = {"error": "No such Shadow account.", "code": "not_found"}


async def read_csv_body(request):
    """POST …/imports: multipart/form-data with ONE part "file" (read in chunks, capped) -> {"_csv": bytes,
    "_filename"}; refused -> {"_multipart_error", "_code"}."""
    from ..monitoring.shadow import importers
    ctype = (request.headers.get("Content-Type") or "").lower()
    if not ctype.startswith("multipart/form-data"):
        return {"_multipart_error": "expected multipart/form-data with one part named 'file' (the CSV)",
                "_code": "csv_invalid"}
    try:
        reader = await request.multipart()
        part = await reader.next()
        if part is None or getattr(part, "name", None) != "file":
            return {"_multipart_error": "expected one part named 'file' (the CSV)", "_code": "csv_invalid"}
        data = bytearray()
        while True:
            chunk = await part.read_chunk(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > importers.MAX_BYTES:
                return {"_multipart_error": f"The file is larger than {importers.MAX_BYTES // (1024 * 1024)} MB — "
                                            "split it.", "_code": "too_large"}
        if await reader.next() is not None:
            return {"_multipart_error": "expected exactly one part named 'file'", "_code": "csv_invalid"}
        return {"_csv": bytes(data), "_filename": getattr(part, "filename", None)}
    except (ValueError, AssertionError, RuntimeError):
        return {"_multipart_error": "malformed multipart upload", "_code": "csv_invalid"}


def _bool_param(v):
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    return None


def _int_param(query, name, lo, hi, default, errors):
    v = query.get(name)
    if v is None:
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        errors[name] = f"an integer {lo}..{hi}"
        return default
    if not lo <= n <= hi:
        errors[name] = f"an integer {lo}..{hi}"
        return default
    return n


class MonitoringRoutesMixin:
    """Handlers of the stage-9 routes."""

    def _only(self, query, allowed):
        bad = {k: "unknown parameter" for k in query if k not in allowed}
        return _bad(bad) if bad else None

    async def _post9(self, body, query, allowed, fn, qallowed=()):
        b = _body(body)
        if b is None:
            return _bad({"_": "expected a JSON object"})
        errors = {k: "unknown parameter" for k in query if k not in qallowed}
        errors.update(_unknown(b, allowed))
        if errors:
            return _bad(errors)
        return await fn(b)

    # --- monitoring ---------------------------------------------------------------------------------------------

    async def mon_overview(self, query: Mapping, body, match: Mapping):
        from ..monitoring import service
        bad = self._only(query, ())
        if bad:
            return bad
        self._touch()
        err, payload = await self._trading(service.overview)
        return err or (200, payload)

    async def mon_strategy(self, query: Mapping, body, match: Mapping):
        from ..monitoring import decay
        sid = match.get("strategy_id") or ""
        if not STRAT_RE.fullmatch(sid):
            return 404, {"error": decay.NOT_FOUND, "code": "not_found"}
        bad = self._only(query, ())
        if bad:
            return bad
        self._touch()
        err, payload = await self._trading(decay.detail, sid)
        return err or (200, payload)

    async def mon_decision(self, query: Mapping, body, match: Mapping):
        from ..monitoring import decay
        sid = match.get("strategy_id") or ""
        if not STRAT_RE.fullmatch(sid):
            return 404, {"error": decay.NOT_FOUND, "code": "not_found"}

        async def go(b):
            errors = {}
            action = b.get("action")
            if action not in decay.ACTIONS:
                errors["action"] = "keep_running | demote"
            reason = _note(b, errors)
            conf = b.get("confirm")                  # accepted and ignored (fix1023)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(decay.decide, sid, action, reason, conf, "user")
            return err or (200, payload)
        return await self._post9(body, query, ("action", "reason", "confirm"), go)

    async def mon_decay_rules(self, query: Mapping, body, match: Mapping):
        from ..monitoring import decay
        bad = self._only(query, ())
        return bad or (200, decay.rules_payload())

    async def mon_decay_run(self, query: Mapping, body, match: Mapping):
        from ..monitoring import decay

        async def go(b):
            err, payload = await self._trading(decay.run_checks)
            return err or (200, payload)
        return await self._post9(body, query, (), go)

    async def mon_alerts(self, query: Mapping, body, match: Mapping):
        from ..monitoring import alerts
        errors = {k: "unknown parameter" for k in query if k not in ("after", "limit", "unacked")}
        after = _int_param(query, "after", 0, 2 ** 53, 0, errors)
        limit = _int_param(query, "limit", 1, 500, 100, errors)
        un = query.get("unacked")
        unacked = False
        if un is not None:
            unacked = _bool_param(un)
            if unacked is None:
                errors["unacked"] = "0 | 1"
        if errors:
            return _bad(errors)
        self._touch()          # SP §22.5: the app-wide MonitoringNotifier poll is the client heartbeat (R2 + R3)
        err, payload = await self._trading(alerts.list_alerts, after, limit, bool(unacked))
        return err or (200, payload)

    async def mon_alerts_ack(self, query: Mapping, body, match: Mapping):
        from ..monitoring import alerts

        async def go(b):
            ids, all_ = b.get("ids"), b.get("all")
            errors = {}
            if all_ is not None and all_ is not True:
                errors["all"] = "true"
            if ids is not None:
                if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or not all(
                        isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids):
                    errors["ids"] = "a list of 1..500 alert ids"
            if (ids is None) == (all_ is None):
                errors["_"] = "send either {ids: [...]} or {all: true}"
            if errors:
                return _bad(errors)
            err, payload = await self._trading(alerts.ack, ids, bool(all_), "user")
            return err or (200, payload)
        return await self._post9(body, query, ("ids", "all"), go)

    async def mon_reports(self, query: Mapping, body, match: Mapping):
        from ..monitoring import reports
        errors = {k: "unknown parameter" for k in query if k not in ("period", "limit")}
        period = query.get("period")
        if period is not None and period not in reports.PERIODS:
            errors["period"] = "daily | weekly"
        limit = _int_param(query, "limit", 1, 200, 50, errors)
        if errors:
            return _bad(errors)
        self._touch()
        err, payload = await self._trading(reports.list_reports, period, limit)
        return err or (200, payload)

    async def mon_report_create(self, query: Mapping, body, match: Mapping):
        from ..monitoring import reports

        async def go(b):
            errors = {}
            period = b.get("period")
            if period not in reports.PERIODS:
                errors["period"] = "daily | weekly"
            day = None
            if b.get("date") is not None:
                try:
                    if not isinstance(b["date"], str) or not DATE_RE.fullmatch(b["date"]):
                        raise ValueError
                    day = date.fromisoformat(b["date"])
                except ValueError:
                    errors["date"] = "YYYY-MM-DD"
            if errors:
                return _bad(errors)
            err, payload = await self._trading(reports.create, period, day, "user")
            return err or (201, {"report": payload})
        return await self._post9(body, query, ("period", "date"), go)

    async def mon_report_get(self, query: Mapping, body, match: Mapping):
        from ..monitoring import reports
        rid = match.get("id") or ""
        if not REPORT_RE.fullmatch(rid):
            return 404, {"error": reports.NOT_FOUND, "code": "not_found"}
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(reports.get, rid)
        return err or (200, {"report": payload})

    def _ai_body(self, b):
        from ..monitoring import reports
        errors = {}
        if b.get("model") not in reports.TIERS:
            errors["model"] = "standard | premium"
        if b.get("language") not in reports.LANGUAGES:
            errors["language"] = "en | de"
        return errors

    async def mon_report_commentary(self, query: Mapping, body, match: Mapping):
        from ..monitoring import reports
        rid = match.get("id") or ""
        if not REPORT_RE.fullmatch(rid):
            return 404, {"error": reports.NOT_FOUND, "code": "not_found"}

        async def go(b):
            errors = self._ai_body(b)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(reports.report_commentary, rid, b["model"], b["language"])
            return err or (200, {"commentary": payload})
        return await self._post9(body, query, ("model", "language"), go)

    async def mon_tax_notice(self, query: Mapping, body, match: Mapping):
        """GET /v2/monitoring/tax-notice — the fixed wording (user decision 2026-10-18: no tax export)."""
        from ..monitoring import tax_notice
        bad = self._only(query, ())
        return bad or (200, tax_notice.payload())

    # --- stage 9b: portfolio insights (SPEC §20; GETs do NOT touch the trading heartbeat) ---------------------------

    async def insights_summary(self, query: Mapping, body, match: Mapping):
        from ..monitoring import insights
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(insights.summary)
        return err or (200, payload)

    async def insights_portfolio(self, query: Mapping, body, match: Mapping):
        from ..monitoring import insights
        errors = {k: "unknown parameter" for k in query if k not in ("year", "account")}
        year = _int_param(query, "year", 2000, 2100, None, errors)
        acc = query.get("account")
        if acc is not None and not SHADOW_RE.fullmatch(acc):
            return 404, SHADOW_NOT_FOUND
        if errors:
            return _bad(errors)
        err, payload = await self._trading(insights.portfolio, year, acc)
        return err or (200, payload)

    # --- shadow ------------------------------------------------------------------------------------------------

    async def sh_fx_get(self, query: Mapping, body, match: Mapping):
        from ..monitoring import fx
        errors = {k: "unknown parameter" for k in query if k != "year"}
        year = _int_param(query, "year", 2000, 2100, None, errors)
        if year is None and "year" not in errors:
            errors["year"] = "required (2000..2100)"
        if errors:
            return _bad(errors)
        err, payload = await self._trading(fx.coverage, year)
        return err or (200, payload)

    async def sh_fx_post(self, query: Mapping, body, match: Mapping):
        from ..monitoring import fx

        async def go(b):
            y = b.get("year")
            if not isinstance(y, int) or isinstance(y, bool) or not 2000 <= y <= 2100:
                return _bad({"year": "an integer 2000..2100"})
            err, payload = await self._trading(fx.refresh, y)
            return err or (200, payload)
        return await self._post9(body, query, ("year",), go)

    def _sid(self, match):
        sid = match.get("id") or ""
        return sid if SHADOW_RE.fullmatch(sid) else None

    async def sh_templates(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import importers
        bad = self._only(query, ())
        return bad or (200, importers.templates_payload())

    async def sh_profiles(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(service.profiles_list)
        return err or (200, payload)

    async def sh_profile_create(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import importers, service

        async def go(b):
            errors = {}
            name = b.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
                errors["name"] = "1..60 characters"
            errors.update(importers.check_mapping(b.get("mapping")))
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.save_profile, name, b["mapping"], "user")
            return err or (201, {"profile": payload})
        return await self._post9(body, query, ("name", "mapping"), go)

    async def sh_accounts(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(service.accounts_list)
        return err or (200, payload)

    async def sh_account_create(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service

        async def go(b):
            errors = {}
            label = b.get("label")
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
                errors["label"] = "1..60 characters"
            if b.get("base_currency") not in ("EUR", "USD"):
                errors["base_currency"] = "EUR | USD"
            if b.get("broker_country") not in ("DE", "other"):
                errors["broker_country"] = "DE | other"
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.create_account, label, b["base_currency"],
                                               b["broker_country"], "user")
            return err or (201, {"account": payload})
        return await self._post9(body, query, ("label", "base_currency", "broker_country"), go)

    async def sh_account_get(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        sid = self._sid(match)
        if sid is None:
            return 404, SHADOW_NOT_FOUND
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(service.detail, sid)
        return err or (200, payload)

    async def sh_import(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import importers, service
        sid = self._sid(match)
        if sid is None:
            return 404, SHADOW_NOT_FOUND
        errors = {k: "unknown parameter" for k in query if k not in ("template", "profile_id", "dry_run")}
        template = query.get("template", "auto")
        if template not in TEMPLATES:
            errors["template"] = " | ".join(TEMPLATES)
        pid = query.get("profile_id")
        if pid is not None and not PROFILE_RE.fullmatch(pid):
            errors["profile_id"] = "a profile id (prf-…)"
        if template == "generic" and pid is None:
            errors["profile_id"] = "required for the generic importer"
        if template != "generic" and pid is not None:
            errors["profile_id"] = "only with template=generic"
        dry = _bool_param(query.get("dry_run", "false"))
        if dry is None:
            errors["dry_run"] = "true | false"
        if not isinstance(body, dict) or ("_csv" not in body and "_multipart_error" not in body):
            errors["file"] = "multipart/form-data with one part named 'file' (the CSV)"
        elif "_multipart_error" in body:
            return 400, {"error": body["_multipart_error"], "code": body.get("_code", "csv_invalid")}
        if errors:
            return _bad(errors)

        def run():
            try:
                return None, service.import_file(sid, body["_csv"], body.get("_filename"), template, pid, dry, "user")
            except importers.ImportRefused as ex:
                out = {"error": ex.message, "code": ex.code}
                if ex.detection is not None:
                    out["detection"] = ex.detection
                return (ex.status, out), None
        err, res = await self._trading(run)
        if err:
            return err
        err2, payload = res
        if err2:
            return err2
        return (200 if dry else 201), payload

    async def sh_import_remove(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        sid = self._sid(match)
        iid = match.get("import_id") or ""
        if sid is None or not IMPORT_RE.fullmatch(iid):
            return 404, SHADOW_NOT_FOUND if sid is None else {"error": "No such import in this Shadow account.",
                                                              "code": "not_found"}

        async def go(b):
            errors = {}
            reason = _note(b, errors)               # confirm: accepted and ignored (fix1023)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.remove_import, sid, iid, b.get("confirm"), reason, "user")
            return err or (200, payload)
        return await self._post9(body, query, ("confirm", "reason"), go)

    async def sh_transactions(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        sid = self._sid(match)
        if sid is None:
            return 404, SHADOW_NOT_FOUND
        errors = {k: "unknown parameter" for k in query if k not in ("limit", "offset")}
        limit = _int_param(query, "limit", 1, 1000, 200, errors)
        offset = _int_param(query, "offset", 0, 10 ** 7, 0, errors)
        if errors:
            return _bad(errors)
        err, payload = await self._trading(service.transactions_page, sid, limit, offset)
        return err or (200, payload)

    async def sh_valuation(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        sid = self._sid(match)
        if sid is None:
            return 404, SHADOW_NOT_FOUND

        async def go(b):
            errors = {}
            as_of = b.get("as_of")
            try:
                if not isinstance(as_of, str) or not DATE_RE.fullmatch(as_of):
                    raise ValueError
                date.fromisoformat(as_of)
            except ValueError:
                errors["as_of"] = "YYYY-MM-DD"
            prices = b.get("prices")
            clean = []
            if not isinstance(prices, list) or not 1 <= len(prices) <= service.MAX_VALUATION_PRICES:
                errors["prices"] = f"a list of 1..{service.MAX_VALUATION_PRICES} {{key, price}}"
            else:
                from ..monitoring.shadow import normalize as _nz
                for i, p in enumerate(prices):
                    if not isinstance(p, dict) or not {"key", "price"} <= set(p) <= {"key", "price", "currency"} \
                            or not isinstance(p["key"], str) or not KEY_RE.fullmatch(p["key"].strip().upper()) \
                            or not _num(p["price"]) or not 0 < float(p["price"]) <= 1e9 \
                            or ("currency" in p and p["currency"] not in _nz.CURRENCIES):
                        errors[f"prices[{i}]"] = "{key: ISIN or symbol, price > 0, currency?: EUR|USD|…}"
                        break
                    c = {"key": p["key"].strip().upper(), "price": float(p["price"])}
                    if "currency" in p:
                        c["currency"] = p["currency"]
                    clean.append(c)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.put_valuation, sid, as_of, clean, "user")
            return err or (200, {"valuation": payload})
        return await self._post9(body, query, ("as_of", "prices"), go)

    async def sh_classifications(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(service.classifications)
        return err or (200, {"classifications": list((payload or {}).values())})

    async def sh_classification_put(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import normalize, service

        async def go(b):
            errors = {}
            key = b.get("key")
            if not isinstance(key, str) or not KEY_RE.fullmatch(key.strip().upper()):
                errors["key"] = "an ISIN or a symbol"
            if b.get("asset_class") not in normalize.ASSET_CLASSES:
                errors["asset_class"] = " | ".join(normalize.ASSET_CLASSES)
            reason = _note(b, errors) or None          # fix1023: an optional note (1–2 characters are fine)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.put_classification, key.strip().upper(), b["asset_class"],
                                               reason, "user")
            return err or (200, {"classification": payload})
        return await self._post9(body, query, ("key", "asset_class", "reason"), go)

    # --- symbol mapping (§20.10) ---------------------------------------------------------------------------------

    async def sh_symbol_maps(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import symbols
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(symbols.payload)
        return err or (200, payload)

    async def sh_symbol_map_put(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import symbols

        async def go(b):
            errors = {}
            if b.get("source") == "eodhd":                      # §20.11: a listing picked from the EODHD search
                isin, listing = b.get("isin"), b.get("listing")
                if not isinstance(isin, str) or not symbols.ISIN_RE.fullmatch(isin.strip().upper()):
                    errors["isin"] = "an ISIN (12 characters)"
                if not isinstance(listing, str) or not listing.strip():
                    errors["listing"] = "a listing CODE.EXCHANGE from the search (e.g. SAP.XETRA)"
                # confirm_listing: accepted and ignored (fix1023 — the dialog summary is the confirmation)
                extra = [k for k in b if k not in ("isin", "source", "listing", "confirm_listing", "reason")]
                for k in extra:
                    errors[k] = "unknown field"
                reason = _note(b, errors) or None
                if errors:
                    return _bad(errors)
                err, payload = await self._trading(symbols.put_listing, isin.strip().upper(), listing.strip().upper(),
                                                   b.get("confirm_listing") is True, reason, "user")
                return err or (200, {"mapping": payload})
            if "source" in b and b.get("source") != "alpaca_us":
                return _bad({"source": "alpaca_us | eodhd"})
            for k in ("listing", "confirm_listing"):
                if k in b:
                    errors[k] = "only with source eodhd"
            isin, sym = b.get("isin"), b.get("symbol")
            if not isinstance(isin, str) or not symbols.ISIN_RE.fullmatch(isin.strip().upper()):
                errors["isin"] = "an ISIN (12 characters, e.g. US78409V1044)"
            if not isinstance(sym, str) or not symbols.TICKER_RE.fullmatch(sym.strip().upper()):
                errors["symbol"] = "a US ticker in Alpaca spelling (e.g. SPGI, BRK.B)"
            # confirm_us_listing: optional (fix1023) — recorded as provenance (confirmed_via), never required
            reason = _note(b, errors) or None
            if errors:
                return _bad(errors)
            err, payload = await self._trading(symbols.put, isin.strip().upper(), sym.strip().upper(),
                                               b.get("confirm_us_listing") is True, reason, "user")
            return err or (200, {"mapping": payload})
        return await self._post9(body, query, ("isin", "symbol", "confirm_us_listing", "reason", "source", "listing",
                                               "confirm_listing"), go)

    async def sh_symbol_check(self, query: Mapping, body, match: Mapping):
        """GET /v2/shadow/symbol-check?ticker=&isin= (fix1023 round 2): read-only — the Alpaca asset of a typed US
        ticker for the mapping dialog BEFORE saving (name, exchange, status, usable / refused reason, mapped to)."""
        from ..monitoring.shadow import symbols
        bad = self._only(query, ("ticker", "isin"))
        if bad:
            return bad
        errors = {}
        ticker = (query.get("ticker") or "").strip().upper()
        isin = (query.get("isin") or "").strip().upper() or None
        if not symbols.TICKER_RE.fullmatch(ticker):
            errors["ticker"] = "a US ticker in Alpaca spelling (e.g. SPGI, BRK.B)"
        if isin is not None and not symbols.ISIN_RE.fullmatch(isin):
            errors["isin"] = "an ISIN (12 characters)"
        if errors:
            return _bad(errors)
        err, payload = await self._trading(symbols.lookup, ticker, isin)
        return err or (200, payload)

    async def sh_price_sources(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import symbols
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(symbols.price_sources)
        return err or (200, payload)

    async def sh_eodhd_refresh(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import symbols

        async def go(b):
            listing = b.get("listing")
            if not isinstance(listing, str) or not listing.strip():
                return _bad({"listing": "a listing CODE.EXCHANGE (e.g. SAP.XETRA)"})
            err, payload = await self._trading(symbols.refresh_listing, listing.strip().upper(), "user")
            return err or (200, payload)
        return await self._post9(body, query, ("listing",), go)

    async def sh_listings(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import symbols
        bad = self._only(query, ("isin", "refresh"))
        if bad:
            return bad
        isin = (query.get("isin") or "").strip().upper()
        if not symbols.ISIN_RE.fullmatch(isin):
            return _bad({"isin": "an ISIN (12 characters)"})
        refresh = _bool_param(query.get("refresh")) if query.get("refresh") is not None else False
        if refresh is None:
            return _bad({"refresh": "true | false"})
        err, payload = await self._trading(symbols.listings, isin, refresh)
        return err or (200, payload)

    async def sh_symbol_map_delete(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import symbols
        isin = (match.get("isin") or "").strip().upper()
        if not symbols.ISIN_RE.fullmatch(isin):
            return 404, {"error": symbols.NOT_FOUND, "code": "not_found"}
        bad = self._only(query, ())
        if bad:
            return bad
        err, payload = await self._trading(symbols.delete, isin, None, "user")
        return err or (200, payload)

    def _benchmark(self, query, errors):
        b = (query.get("benchmark") or "SPY").strip().upper()
        if not SYMBOL_RE.fullmatch(b):
            errors["benchmark"] = "a ticker such as SPY"
        return b

    async def sh_diagnostics(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        sid = self._sid(match)
        if sid is None:
            return 404, SHADOW_NOT_FOUND
        errors = {k: "unknown parameter" for k in query if k != "benchmark"}
        bench = self._benchmark(query, errors)
        if errors:
            return _bad(errors)
        err, payload = await self._trading(service.diagnose, sid, bench)
        return err or (200, payload)

    def _rules(self, b, errors, required=True):
        from ..monitoring.shadow import shadow_portfolio
        rules = b.get("rules")
        if rules is None and not required:
            return None
        if not isinstance(rules, list) or not 1 <= len(rules) <= shadow_portfolio.MAX_RULES:
            errors["rules"] = f"a list of 1..{shadow_portfolio.MAX_RULES} rules"
            return None
        for i, r in enumerate(rules):
            msg = shadow_portfolio.check_rule(r)
            if msg:
                errors[f"rules[{i}]"] = msg
                return None
        return rules

    async def sh_shadows(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import service
        sid = self._sid(match)
        if sid is None:
            return 404, SHADOW_NOT_FOUND

        async def go(b):
            errors = {}
            rules = self._rules(b, errors)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.shadows, sid, rules)
            return err or (200, payload)
        return await self._post9(body, query, ("rules",), go)

    async def sh_report(self, query: Mapping, body, match: Mapping):
        from ..monitoring.shadow import report
        sid = self._sid(match)
        if sid is None:
            return 404, SHADOW_NOT_FOUND

        async def go(b):
            errors = {}
            fmt = b.get("format", "html")
            if fmt not in ("html", "json"):
                errors["format"] = "html | json"
            rules = self._rules(b, errors, required=False)
            bench = self._benchmark({"benchmark": b.get("benchmark") or "SPY"} if isinstance(
                b.get("benchmark", "SPY"), str) else {"benchmark": "?"}, errors)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(report.build, sid, fmt, rules, bench, "user")
            return err or (200, payload)
        return await self._post9(body, query, ("format", "rules", "benchmark"), go)

    async def sh_commentary(self, query: Mapping, body, match: Mapping):
        import asyncio
        from ..monitoring import reports
        from ..monitoring.shadow import service
        sid = self._sid(match)
        if sid is None:
            return 404, SHADOW_NOT_FOUND

        async def go(b):
            errors = self._ai_body(b)
            bench = self._benchmark({"benchmark": b.get("benchmark") or "SPY"} if isinstance(
                b.get("benchmark", "SPY"), str) else {"benchmark": "?"}, errors)
            if errors:
                return _bad(errors)

            async def run():
                facts = await asyncio.to_thread(service.report_facts, sid, bench)
                return await reports.commentary("shadow", sid, facts, b["model"], b["language"])
            err, payload = await self._trading(run)
            return err or (200, {"commentary": payload})
        return await self._post9(body, query, ("model", "language", "benchmark"), go)


MONITORING_ROUTES = (
    ("GET", "/v2/monitoring/overview", "mon_overview", False),
    ("GET", "/v2/monitoring/strategies/{strategy_id}", "mon_strategy", False),
    ("POST", "/v2/monitoring/strategies/{strategy_id}/decision", "mon_decision", True),
    ("GET", "/v2/monitoring/decay/rules", "mon_decay_rules", False),
    ("POST", "/v2/monitoring/decay/run", "mon_decay_run", True),
    ("GET", "/v2/monitoring/alerts", "mon_alerts", False),
    ("POST", "/v2/monitoring/alerts/ack", "mon_alerts_ack", True),
    ("GET", "/v2/monitoring/reports", "mon_reports", False),
    ("POST", "/v2/monitoring/reports", "mon_report_create", True),
    ("GET", "/v2/monitoring/reports/{id}", "mon_report_get", False),
    ("POST", "/v2/monitoring/reports/{id}/commentary", "mon_report_commentary", True),
    ("GET", "/v2/monitoring/tax-notice", "mon_tax_notice", False),
    ("GET", "/v2/insights/summary", "insights_summary", False),
    ("GET", "/v2/insights/portfolio", "insights_portfolio", False),
    ("GET", "/v2/shadow/fx", "sh_fx_get", False),
    ("POST", "/v2/shadow/fx", "sh_fx_post", True),
    ("GET", "/v2/shadow/templates", "sh_templates", False),
    ("GET", "/v2/shadow/profiles", "sh_profiles", False),
    ("POST", "/v2/shadow/profiles", "sh_profile_create", True),
    ("GET", "/v2/shadow/accounts", "sh_accounts", False),
    ("POST", "/v2/shadow/accounts", "sh_account_create", True),
    ("GET", "/v2/shadow/accounts/{id}", "sh_account_get", False),
    ("POST", "/v2/shadow/accounts/{id}/imports", "sh_import", "csv"),
    ("POST", "/v2/shadow/accounts/{id}/imports/{import_id}/remove", "sh_import_remove", True),
    ("GET", "/v2/shadow/accounts/{id}/transactions", "sh_transactions", False),
    ("PUT", "/v2/shadow/accounts/{id}/valuation", "sh_valuation", True),
    ("GET", "/v2/shadow/classifications", "sh_classifications", False),
    ("PUT", "/v2/shadow/classifications", "sh_classification_put", True),
    ("GET", "/v2/shadow/symbol-mappings", "sh_symbol_maps", False),
    ("PUT", "/v2/shadow/symbol-mappings", "sh_symbol_map_put", True),
    ("GET", "/v2/shadow/symbol-check", "sh_symbol_check", False),
    ("DELETE", "/v2/shadow/symbol-mappings/{isin}", "sh_symbol_map_delete", False),
    ("GET", "/v2/shadow/price-sources", "sh_price_sources", False),
    ("GET", "/v2/shadow/listings", "sh_listings", False),
    ("POST", "/v2/shadow/price-sources/eodhd/refresh", "sh_eodhd_refresh", True),
    ("GET", "/v2/shadow/accounts/{id}/diagnostics", "sh_diagnostics", False),
    ("POST", "/v2/shadow/accounts/{id}/shadows", "sh_shadows", True),
    ("POST", "/v2/shadow/accounts/{id}/report", "sh_report", True),
    ("POST", "/v2/shadow/accounts/{id}/commentary", "sh_commentary", True),
)

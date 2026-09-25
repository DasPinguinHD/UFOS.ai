"""Offline tests for the Insider Trades section (Rule 10b5-1 flag, insider
clusters, AI table flags). Stdlib unittest only; no network, no .env.

Run from HedgeFund/backend:  python -m unittest tests.test_insider -v

aiohttp, config and the newsroom package are stubbed so the two modules
under test load without the backend's dependencies or its package __init__.
"""
import importlib.util
import os
import sys
import types
import unittest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NEWSROOM = os.path.join(_BACKEND, "newsroom")


def _load_modules():
    saved = {name: sys.modules.get(name) for name in
             ("aiohttp", "config", "newsroom", "newsroom.store",
              "newsroom.sec_edgar_collector", "newsroom.insider_summary")}
    try:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientSession = object
        aiohttp.ClientTimeout = lambda **kw: None
        sys.modules["aiohttp"] = aiohttp

        config = types.ModuleType("config")
        config.SEC_EDGAR_USER_AGENT = "test test@example.com"
        config.SEC_EDGAR_POLL_SECONDS = 60
        config.SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS = 0
        sys.modules["config"] = config

        pkg = types.ModuleType("newsroom")
        pkg.__path__ = [_NEWSROOM]
        sys.modules["newsroom"] = pkg
        store = types.ModuleType("newsroom.store")
        store.make_event = lambda **kw: dict(kw)
        sys.modules["newsroom.store"] = store

        loaded = []
        for name in ("sec_edgar_collector", "insider_summary"):
            spec = importlib.util.spec_from_file_location(f"newsroom.{name}", os.path.join(_NEWSROOM, name + ".py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            loaded.append(mod)
        return loaded
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


sec, summary = _load_modules()


FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0508</schemaVersion>
    <documentType>4</documentType>
    <periodOfReport>2026-09-10</periodOfReport>
    <aff10b5One>{checkbox}</aff10b5One>
    <issuer>
        <issuerCik>0000123456</issuerCik>
        <issuerName>ACME WIDGETS CORP</issuerName>
        <issuerTradingSymbol>ACME</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0000999999</rptOwnerCik>
            <rptOwnerName>Doe Jane</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isOfficer>1</isOfficer>
            <officerTitle>CFO</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-09-10</value></transactionDate>
            <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>10000</value></transactionShares>
                <transactionPricePerShare><value>50</value><footnoteId id="F1"/></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
    <footnotes>
        {footnote}
    </footnotes>
</ownershipDocument>"""

PLAN_FOOTNOTE = ('<footnote id="F1">The sales reported were effected pursuant to a Rule '
                 '10b5-1 trading plan adopted by the reporting person on March 3, 2026.</footnote>')
PLAIN_FOOTNOTE = '<footnote id="F1">Weighted average price.</footnote>'
NEGATED_FOOTNOTE = '<footnote id="F1">These sales were not made pursuant to a Rule 10b5-1 plan.</footnote>'


class Form4ParseTests(unittest.TestCase):
    def test_checkbox_true(self):
        d = sec._parse_form4(FORM4.format(checkbox="true", footnote=PLAN_FOOTNOTE).encode())
        self.assertTrue(d["plan_10b5_1"])
        self.assertEqual(d["plan_10b5_1_source"], "checkbox")
        self.assertEqual(d["issuer_ticker"], "ACME")
        self.assertEqual(d["transactions"][0]["value_usd"], 500000)

    def test_checkbox_numeric(self):
        d = sec._parse_form4(FORM4.format(checkbox="1", footnote=PLAIN_FOOTNOTE))
        self.assertEqual(d["plan_10b5_1_source"], "checkbox")

    def test_footnote_fallback(self):
        d = sec._parse_form4(FORM4.format(checkbox="0", footnote=PLAN_FOOTNOTE))
        self.assertTrue(d["plan_10b5_1"])
        self.assertEqual(d["plan_10b5_1_source"], "footnote")

    def test_no_plan(self):
        d = sec._parse_form4(FORM4.format(checkbox="false", footnote=PLAIN_FOOTNOTE))
        self.assertFalse(d["plan_10b5_1"])
        self.assertIsNone(d["plan_10b5_1_source"])
        self.assertNotIn("10b5-1", sec._describe(d)[1])

    def test_negated_footnote_is_not_a_plan(self):
        d = sec._parse_form4(FORM4.format(checkbox="false", footnote=NEGATED_FOOTNOTE))
        self.assertFalse(d["plan_10b5_1"])

    def test_missing_checkbox_element(self):
        xml = FORM4.format(checkbox="", footnote=PLAIN_FOOTNOTE).replace("<aff10b5One></aff10b5One>", "")
        self.assertFalse(sec._parse_form4(xml)["plan_10b5_1"])

    def test_describe_marks_pre_planned_sale(self):
        d = sec._parse_form4(FORM4.format(checkbox="true", footnote=PLAN_FOOTNOTE))
        title, text = sec._describe(d)
        self.assertIn("ACME Widgets Corp (ACME)", title)  # ticker word stays upper case
        self.assertIn("pre-planned sale under a Rule 10b5-1 trading plan", text)


def _event(owner, cik, code, when, shares=1000, price=10.0, issuer="ACME WIDGETS", ticker="ACME",
           issuer_cik="111", planned=False, after=100000):
    return {
        "category": "insider",
        "filed_at": when + "T16:00:00-04:00",
        "details": {
            "issuer_name": issuer, "issuer_ticker": ticker, "issuer_cik": issuer_cik,
            "owner_name": owner, "owner_cik": cik, "owner_roles": ["Director"],
            "has_derivative_transactions": False,
            "plan_10b5_1": planned, "plan_10b5_1_source": "checkbox" if planned else None,
            "transactions": [{
                "code": code, "type": sec.TRANSACTION_CODES.get(code, code), "security": "Common Stock",
                "derivative": False, "date": when, "shares": shares, "price": price,
                "value_usd": shares * price, "acquired_disposed": "A" if code == "P" else "D",
                "shares_after": after,
            }],
        },
    }


class ClusterTests(unittest.TestCase):
    def test_buy_cluster_two_insiders(self):
        events = [_event("Alice", "1", "P", "2026-09-10"), _event("Bob", "2", "P", "2026-09-18"),
                  _event("Alice", "1", "P", "2026-09-11")]
        clusters = summary.find_clusters(events)
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        self.assertEqual((c["kind"], c["count"], c["first_date"], c["last_date"]),
                         ("buy", 2, "2026-09-10", "2026-09-18"))
        self.assertEqual(c["insiders"], ["Alice", "Bob"])
        self.assertEqual(c["total_usd"], 30000)

    def test_same_insider_twice_is_not_a_cluster(self):
        events = [_event("Alice", "1", "P", "2026-09-10"), _event("Alice", "1", "P", "2026-09-12")]
        self.assertEqual(summary.find_clusters(events), [])

    def test_outside_window(self):
        events = [_event("Alice", "1", "P", "2026-09-01"), _event("Bob", "2", "P", "2026-09-20")]
        self.assertEqual(summary.find_clusters(events), [])

    def test_owner_name_fallback(self):
        events = [_event("Alice", "", "P", "2026-09-10"), _event("Bob", "", "P", "2026-09-11")]
        self.assertEqual(summary.find_clusters(events)[0]["count"], 2)

    def test_different_issuers_do_not_mix(self):
        events = [_event("Alice", "1", "P", "2026-09-10"),
                  _event("Bob", "2", "P", "2026-09-11", issuer="OTHER CO", ticker="OTH", issuer_cik="222")]
        self.assertEqual(summary.find_clusters(events), [])

    def test_sell_cluster_needs_three_unplanned(self):
        two = [_event("A", "1", "S", "2026-09-10"), _event("B", "2", "S", "2026-09-11")]
        self.assertEqual(summary.find_clusters(two), [])
        planned_third = two + [_event("C", "3", "S", "2026-09-12", planned=True)]
        self.assertEqual(summary.find_clusters(planned_third), [])
        three = two + [_event("C", "3", "S", "2026-09-12")]
        clusters = summary.find_clusters(three)
        self.assertEqual([(c["kind"], c["count"]) for c in clusters], [("sell", 3)])

    def test_buys_sorted_before_sells(self):
        events = [_event("A", "1", "S", "2026-09-10"), _event("B", "2", "S", "2026-09-11"),
                  _event("C", "3", "S", "2026-09-12"),
                  _event("X", "7", "P", "2026-09-10", issuer="BUY CO", ticker="BUY", issuer_cik="333"),
                  _event("Y", "8", "P", "2026-09-10", issuer="BUY CO", ticker="BUY", issuer_cik="333")]
        self.assertEqual([c["kind"] for c in summary.find_clusters(events)], ["buy", "sell"])


class BuildTableTests(unittest.TestCase):
    def test_flags(self):
        events = [
            _event("Alice", "1", "P", "2026-09-10"),
            _event("Bob", "2", "P", "2026-09-12"),
            # Large planned sale that would otherwise be a BIG-STAKE-CHANGE (sold 90% of holdings).
            _event("Carol", "3", "S", "2026-09-12", shares=90000, price=100.0, after=10000,
                   issuer="BIG CO", ticker="BIG", issuer_cik="444", planned=True),
        ]
        table, used = summary.build_table(events)
        self.assertEqual(used, 3)
        lines = {l.split(" | ")[1].split(" [")[0]: l for l in table.splitlines() if " | " in l}
        self.assertIn("CLUSTER-BUY (2 insiders", lines["Alice"])
        self.assertIn("CLUSTER-BUY (2 insiders", lines["Bob"])
        self.assertIn("PRE-PLANNED-10b5-1", lines["Carol"])
        self.assertNotIn("BIG-STAKE-CHANGE", lines["Carol"])
        self.assertIn("pre-planned", lines["Carol"])
        self.assertIn("CLUSTER-BUY: 2 insiders bought ACME WIDGETS (ACME)", table)

    def test_unplanned_big_sale_still_flagged(self):
        e = _event("Carol", "3", "S", "2026-09-12", shares=90000, price=100.0, after=10000)
        table, _ = summary.build_table([e])
        self.assertIn("BIG-STAKE-CHANGE", table)
        self.assertNotIn("PRE-PLANNED", table)


if __name__ == "__main__":
    unittest.main()

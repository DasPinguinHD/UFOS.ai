"""SPEC §14.1 (stage 5b): paper ingestion — URL allow-list, redirects, SSRF (private IPs, DNS rebinding),
size/time limits, SSRN login page, the PDF extractor subprocess (fake extractor scripts + a small real
pypdf PDF, skipped when pypdf is missing), storage (text file + sha256 only, duplicates, insert-only),
routes. No network.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_papers -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_platform.api import routes  # noqa: E402
from strategy_platform.research import papers  # noqa: E402
from strategy_platform.storage import db  # noqa: E402

PUBLIC = "151.101.1.1"


def call(name, body=routes.NO_BODY, query=None, match=None):
    return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))


def tiny_pdf(text="Momentum strategy test", pages=1) -> bytes:
    """A minimal valid PDF with one text line per page (hand-built, offsets computed)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>")
    font = 3 + 2 * pages
    for i in range(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> >>")
        stream = f"BT /F1 12 Tf 20 100 Td ({text} page {i + 1}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class Resp:
    def __init__(self, status=200, ctype="application/pdf", body=b"%PDF-1.4 fake", location=None, length=None):
        self.status = status
        self.headers = {"Content-Type": ctype}
        if location:
            self.headers["Location"] = location
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self._body, self.closed = body, False

    def read(self, n):
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def close(self):
        self.closed = True


class FakeNet:
    def __init__(self, routes_map, dns=None):
        self.routes, self.dns, self.opened, self.resolved = routes_map, dns or {}, [], []

    def resolver(self, host, timeout):
        self.resolved.append(host)
        v = self.dns.get(host, [PUBLIC])
        return v.pop(0) if v and isinstance(v[0], list) else v

    def opener(self, url, ip, headers, timeout, deadline):
        self.opened.append((url, ip))
        r = self.routes[url]
        return r() if callable(r) else r


class Gate:
    def call(self, fn, data_type, classify):
        return fn()


def fetcher(net, **kw):
    return papers.PaperFetcher(opener=net.opener, resolver=net.resolver, gate=Gate(), **kw)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp5b-papers-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        self.addCleanup(setattr, papers, "FETCHER", None)
        self.addCleanup(setattr, papers, "WORKER_ARGV", papers._worker_argv)

    def fake_worker(self, code: str):
        path = os.path.join(self.tmp, "fake_worker.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("import json, sys, time\n" + code + "\n")
        papers.WORKER_ARGV = lambda pdf: [sys.executable, "-I", path, pdf]
        return path


class UrlRulesTest(unittest.TestCase):
    def test_allowed(self):
        c = papers.classify_url
        self.assertEqual(c("https://arxiv.org/abs/2101.01234"), ("arxiv", "https://arxiv.org/pdf/2101.01234"))
        self.assertEqual(c("https://arxiv.org/pdf/2101.01234v3.pdf"), ("arxiv", "https://arxiv.org/pdf/2101.01234v3"))
        self.assertEqual(c("https://export.arxiv.org/abs/q-fin/0601001"), ("arxiv", "https://arxiv.org/pdf/q-fin/0601001"))
        self.assertEqual(c("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=123456"),
                         ("ssrn", "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=123456"))

    def test_rejected(self):
        for url in ("http://arxiv.org/abs/2101.01234", "https://arxiv.org.evil.com/abs/2101.01234",
                    "https://www.arxiv.org/abs/2101.01234", "https://arxiv.org:8443/abs/2101.01234",
                    "https://user@arxiv.org/abs/2101.01234", "https://arxiv.org/abs/2101.01234?x=1",
                    "https://arxiv.org/list/q-fin", "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1&x=2",
                    "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=abc", "https://ssrn.com/abstract=1",
                    "https://example.com/paper.pdf", "https://10.0.0.1/abs/2101.01234", "ftp://arxiv.org/abs/1",
                    "https://arxiv.org/abs/2101.01234#x", 5, None, "x" * 600):
            with self.assertRaises(papers.PaperError, msg=str(url)[:60]) as cm:
                papers.classify_url(url)
            self.assertEqual((cm.exception.status, cm.exception.code), (400, "url_not_allowed"))


class FetchTest(Case):
    def test_redirect_rules(self):
        net = FakeNet({"https://arxiv.org/pdf/1": Resp(301, location="https://export.arxiv.org/pdf/1"),
                       "https://export.arxiv.org/pdf/1": Resp(body=b"%PDF-1.4 ok")})
        self.assertEqual(fetcher(net).get("https://arxiv.org/pdf/1", ("application/pdf",))[1], b"%PDF-1.4 ok")
        for loc in ("https://evil.com/x.pdf", "http://arxiv.org/pdf/1", "https://arxiv.org:444/pdf/1"):
            net = FakeNet({"https://arxiv.org/pdf/1": Resp(302, location=loc)})
            with self.assertRaises(papers.PaperError) as cm:
                fetcher(net).get("https://arxiv.org/pdf/1", ("application/pdf",))
            self.assertEqual(cm.exception.code, "fetch_rejected", loc)
        loop = {f"https://arxiv.org/pdf/{i}": Resp(302, location=f"https://arxiv.org/pdf/{i + 1}") for i in range(10)}
        with self.assertRaises(papers.PaperError) as cm:
            fetcher(FakeNet(loop)).get("https://arxiv.org/pdf/0", ("application/pdf",))
        self.assertIn("redirects", cm.exception.user_message)

    def test_ssrf_private_and_rebinding(self):
        for ips in (["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], [PUBLIC, "192.168.1.2"], ["::1"], ["fd00::1"]):
            net = FakeNet({"https://arxiv.org/pdf/1": Resp()}, dns={"arxiv.org": ips})
            with self.assertRaises(papers.PaperError) as cm:
                fetcher(net).get("https://arxiv.org/pdf/1", ("application/pdf",))
            self.assertEqual(cm.exception.code, "fetch_rejected", ips)
            self.assertEqual(net.opened, [])
        # DNS rebinding: public on the first lookup, private on the next hop -> refused; the connection always
        # goes to the CHECKED address
        net = FakeNet({"https://arxiv.org/pdf/1": Resp(301, location="https://arxiv.org/pdf/2"),
                       "https://arxiv.org/pdf/2": Resp()}, dns={"arxiv.org": [[PUBLIC], ["10.1.2.3"]]})
        with self.assertRaises(papers.PaperError) as cm:
            fetcher(net).get("https://arxiv.org/pdf/1", ("application/pdf",))
        self.assertEqual(cm.exception.code, "fetch_rejected")
        self.assertEqual(net.opened, [("https://arxiv.org/pdf/1", PUBLIC)])

    def test_size_time_type(self):
        net = FakeNet({"https://arxiv.org/pdf/1": Resp(length=10 ** 9)})
        with self.assertRaises(papers.PaperError) as cm:
            fetcher(net).get("https://arxiv.org/pdf/1", ("application/pdf",))
        self.assertEqual(cm.exception.code, "too_large")
        net = FakeNet({"https://arxiv.org/pdf/1": Resp(body=b"x" * 5000)})
        with self.assertRaises(papers.PaperError) as cm:
            fetcher(net, max_bytes=1000).get("https://arxiv.org/pdf/1", ("application/pdf",))
        self.assertEqual(cm.exception.code, "too_large")
        clock = iter([0.0, 0.0, 0.0, 40.0, 40.0, 40.0])
        net = FakeNet({"https://arxiv.org/pdf/1": Resp(body=b"x" * 200000)})
        with self.assertRaises(papers.PaperError) as cm:
            fetcher(net, monotonic=lambda: next(clock, 99.0)).get("https://arxiv.org/pdf/1", ("application/pdf",))
        self.assertIn("30 s", cm.exception.user_message)
        papers.FETCHER = fetcher(FakeNet({"https://arxiv.org/pdf/2101.01234": Resp(ctype="text/html", body=b"<html>")}))
        with self.assertRaises(papers.PaperError) as cm:
            papers.fetch_link("https://arxiv.org/abs/2101.01234")
        self.assertEqual(cm.exception.code, "fetch_rejected")

    def test_ssrn(self):
        abs_url = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=42"
        dl = "https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID42.pdf?abstractid=42&mirid=1"
        html = b'<a href="Delivery.cfm/SSRN_ID42.pdf?abstractid=42&amp;mirid=1">Download</a>'
        papers.FETCHER = fetcher(FakeNet({abs_url: Resp(ctype="text/html", body=html), dl: Resp(body=b"%PDF-1.5 x")}))
        self.assertEqual(papers.fetch_link(abs_url)[:2], ("ssrn", b"%PDF-1.5 x"))
        for net in (FakeNet({abs_url: Resp(ctype="text/html", body=b"<html>Please sign in</html>")}),
                    FakeNet({abs_url: Resp(ctype="text/html", body=html),
                             dl: Resp(ctype="text/html", body=b"<html>login</html>")}),
                    FakeNet({abs_url: Resp(ctype="text/html", body=html), dl: Resp(301, location="https://hq.ssrn.com/login")})):
            papers.FETCHER = fetcher(net)
            with self.assertRaises(papers.PaperError) as cm:
                papers.fetch_link(abs_url)
            self.assertIn(cm.exception.code, ("ssrn_login", "fetch_rejected"))
            if cm.exception.code == "ssrn_login":
                self.assertEqual(cm.exception.user_message, papers.SSRN_LOGIN_MESSAGE)


class ExtractorTest(Case):
    def test_fake_worker_outcomes(self):
        pdf = b"%PDF-1.4 fake"
        self.fake_worker('print(json.dumps({"pages": 2, "text": "A momentum paper text", "title": "T", '
                         '"authors": "A", "year": 2019, "truncated": False}))')
        self.assertEqual(papers.extract_pdf(pdf)["pages"], 2)
        self.fake_worker("time.sleep(10)")
        with self.assertRaises(papers.PaperError) as cm:
            papers.extract_pdf(pdf, timeout=0.5)
        self.assertEqual((cm.exception.status, cm.exception.code), (504, "extract_timeout"))
        self.fake_worker("sys.exit(3)")
        with self.assertRaises(papers.PaperError) as cm:
            papers.extract_pdf(pdf)
        self.assertIn("memory", cm.exception.user_message)
        self.fake_worker('print(json.dumps({"error": "too_many_pages", "message": "The PDF has 900 pages."}))')
        with self.assertRaises(papers.PaperError) as cm:
            papers.extract_pdf(pdf)
        self.assertEqual(cm.exception.user_message, "The PDF has 900 pages.")
        self.fake_worker('print(json.dumps({"pages": 1, "text": "   "}))')
        with self.assertRaises(papers.PaperError) as cm:
            papers.extract_pdf(pdf)
        self.assertIn("no extractable text", cm.exception.user_message)
        self.fake_worker('print("not json"); sys.exit(1)')
        with self.assertRaises(papers.PaperError):
            papers.extract_pdf(pdf)
        for bad in (b"", b"<html>", b"%PDF" + b"x" * (papers.MAX_PDF_BYTES + 1)):
            with self.assertRaises(papers.PaperError):
                papers.extract_pdf(bad)
        left = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("ufos-paper-")]
        self.assertEqual(left, [])                       # the app's temp PDFs are removed

    @unittest.skipUnless(__import__("importlib").util.find_spec("pypdf"), "pypdf not installed")
    def test_real_pypdf(self):
        out = papers.extract_pdf(tiny_pdf("Momentum strategy test", pages=3))
        self.assertEqual(out["pages"], 3)
        self.assertIn("Momentum strategy test page 2", out["text"])
        old = papers.MAX_PAGES
        papers.MAX_PAGES = 2
        self.addCleanup(setattr, papers, "MAX_PAGES", old)
        with self.assertRaises(papers.PaperError) as cm:
            papers.extract_pdf(tiny_pdf(pages=3))
        self.assertIn("3 pages", cm.exception.user_message)


class StorageRouteTest(Case):
    def test_text_paper_duplicate_and_insert_only(self):
        status, p = call("papers_create", {"text": "We test a 12-1 momentum strategy on US equities.", "title": "Mom"})
        self.assertEqual(status, 200, p)
        self.assertEqual((p["source_kind"], p["status"], p["duplicate"], p["title"]), ("text", "new", False, "Mom"))
        path = os.path.join(self.tmp, "papers", p["sha256"] + ".txt")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "papers")), [p["sha256"] + ".txt"])   # no PDF kept
        status, p2 = call("papers_create", {"text": "We test a 12-1 momentum strategy on US equities."})
        self.assertEqual((p2["id"], p2["duplicate"]), (p["id"], True))
        with self.assertRaises(db.StorageUnavailable):
            with db._connect() as conn:
                conn.execute("UPDATE papers SET title = 'x'")
        status, g = call("paper_get", match={"id": p["id"]})
        self.assertEqual((status, g["text_preview"][:7]), (200, "We test"))
        self.assertEqual(call("papers_list")[1]["papers"][0]["id"], p["id"])

    def test_pdf_and_url_routes(self):
        self.fake_worker('print(json.dumps({"pages": 1, "text": "Value strategy paper", "title": "V", '
                         '"authors": None, "year": 2001, "truncated": False}))')
        status, p = call("papers_create", {"_pdf": b"%PDF-1.4 something", "_filename": "C:\\\\x\\\\paper.pdf"})
        self.assertEqual(status, 200, p)
        self.assertEqual((p["source_kind"], p["filename"], p["year"]), ("pdf", "paper.pdf", 2001))
        papers.FETCHER = fetcher(FakeNet({"https://arxiv.org/pdf/2101.01234": Resp(body=b"%PDF-1.4 arxiv")}))
        status, p = call("papers_create", {"url": "https://arxiv.org/abs/2101.01234"})
        self.assertEqual((status, p["source_kind"]), (200, "arxiv"))
        status, out = call("papers_create", {"url": "https://example.com/x.pdf"})
        self.assertEqual((status, out["code"]), (400, "url_not_allowed"))
        status, out = call("papers_create", {"_multipart_error": "The PDF is larger than 20 MB.", "_code": "too_large"})
        self.assertEqual((status, out["code"]), (400, "too_large"))
        for body in ({}, {"text": "a", "url": "https://arxiv.org/abs/1"}, {"text": ""}, {"text": "x" * 200001},
                     {"text": "x", "title": "t" * 301}, {"text": "x", "extra": 1}, {"url": 5}, [], routes.NO_BODY):
            self.assertEqual(call("papers_create", body)[0], 400, str(body)[:60])
        self.assertEqual(call("paper_get", match={"id": "p-000000000000"})[0], 404)


if __name__ == "__main__":
    unittest.main()

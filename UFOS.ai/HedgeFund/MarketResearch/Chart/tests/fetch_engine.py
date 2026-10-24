#!/usr/bin/env python3
"""Vendor the pinned KLineChart engine into ../vendor (MR3a; NOT shipped, a maintenance tool).

    python fetch_engine.py [--expect-integrity sha512-...]

Reads the live registry metadata of the pinned version (https://registry.npmjs.org/klinecharts/10.0.3)
and requires its `dist.integrity` == the pinned sha512 and `dist.tarball` == the pinned URL, then
downloads the tarball and requires its sha512 == both (three-way match),
extracts ONLY package/dist/umd/klinecharts.min.js, package/LICENSE and package/NOTICE (if present)
and writes vendor/MANIFEST in the bridge-contract format (SPEC "MR3 bridge contract v1"):
    <file> <version> <source_url> <sha256> <fetched_at>
ChartHost.cs hashes every listed file at load. Existing vendor files are renamed to *.stale
(never deleted). Uses the system/env HTTPS proxy (urllib). Standard library only.

Pinned version: klinecharts 10.0.3 (npm, published 2026-08-27; Apache-2.0). 10.0.4 (2026-10-12)
and 10.1.0 (2026-10-14) were < 2 weeks old at pin time (2026-10-16). The integrity value below
was read from the registry through a web reader on 2026-10-16. If the registry disagrees the script
stops; only after checking by hand (`npm view klinecharts@10.0.3 dist.integrity`) may the value be
overridden with --expect-integrity (printed loudly; the registry and the tarball must still match it).
"""
import argparse
import base64
import datetime
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.request

NAME = "klinecharts"
VERSION = "10.0.3"
TARBALL = "https://registry.npmjs.org/klinecharts/-/klinecharts-10.0.3.tgz"
METADATA = "https://registry.npmjs.org/klinecharts/10.0.3"
MAX_METADATA = 5 * 1024 * 1024
INTEGRITY = "sha512-phvf5CyS7oGE46nuC+nxQxl9D+8+5w8q3reExh0oNeMV1Jws4X0CMHNkOI9xlNA/XWctNS5whZ3PqIJi8YM2Tw=="
MAX_TARBALL = 30 * 1024 * 1024
MEMBERS = [  # (tar member, vendored name, required)
    ("package/dist/umd/klinecharts.min.js", "klinecharts.min.js", True),
    ("package/LICENSE", "LICENSE", True),
    ("package/NOTICE", "NOTICE", False),
]
VENDOR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor"))


def fail(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def download(url, limit, what):
    req = urllib.request.Request(url, headers={"User-Agent": "UFOS.ai-vendor-fetch", "Accept": "application/json, */*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(limit + 1)
    except Exception as e:  # network / HTTP error: say what failed, no traceback
        fail("could not download the %s from %s (%s: %s)" % (what, url, type(e).__name__, e))
    if len(data) > limit:
        fail("%s larger than %d bytes" % (what, limit))
    return data


def check_metadata(raw, expected):
    try:
        meta = json.loads(raw.decode("utf-8"))
        dist = meta["dist"]
        integrity, tarball, name, version = dist["integrity"], dist["tarball"], meta["name"], meta["version"]
    except Exception:
        fail("registry metadata is not the expected JSON (name/version/dist.integrity/dist.tarball)")
    if name != NAME or version != VERSION:
        fail("registry metadata is for %s@%s, expected %s@%s" % (name, version, NAME, VERSION))
    if tarball != TARBALL:
        fail("registry dist.tarball mismatch\n  pinned   %s\n  registry %s" % (TARBALL, tarball))
    if integrity != expected:
        fail("registry dist.integrity does not match the expected value\n  expected %s\n  registry %s\n"
             "The package may have been republished or the pin is wrong - stop and check by hand." % (expected, integrity))
    print("registry metadata ok: %s@%s %s" % (name, version, integrity))


def stale(path):
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        os.replace(path, "%s.%s.stale" % (path, stamp))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--expect-integrity", default=INTEGRITY)
    ap.add_argument("--tarball", help="use a local .tgz instead of downloading (still verified)")
    ap.add_argument("--metadata", help="use a saved copy of the registry metadata JSON (still verified)")
    a = ap.parse_args()
    if not a.expect_integrity.startswith("sha512-"):
        fail("--expect-integrity must be an npm sha512 integrity string")
    expected = a.expect_integrity
    if expected != INTEGRITY:
        bar = "!" * 78
        print("%s\n!! OVERRIDE: --expect-integrity replaces the pinned value\n!!   pinned   %s\n!!   override %s\n"
              "!! Record the new value in this script and in SPEC MR3a notes.\n%s" % (bar, INTEGRITY, expected, bar))

    raw = open(a.metadata, "rb").read(MAX_METADATA + 1) if a.metadata else download(METADATA, MAX_METADATA, "registry metadata")
    check_metadata(raw, expected)

    data = open(a.tarball, "rb").read() if a.tarball else download(TARBALL, MAX_TARBALL, "tarball")
    got = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if got != expected:
        fail("tarball sha512 does not match the pinned/registry integrity\n  expected %s\n  computed %s" % (expected, got))
    print("tarball integrity ok (pin == registry == tarball): " + got)

    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member, out, required in MEMBERS:
            try:
                info = tar.getmember(member)
            except KeyError:
                if required:
                    fail("tarball has no " + member)
                continue
            if not info.isfile() or info.size > 10 * 1024 * 1024:
                fail(member + " is not a regular file of sane size")
            files[out] = tar.extractfile(info).read()

    js = files["klinecharts.min.js"].decode("utf-8", "replace")
    for needle in ("eval(", "new Function(", "Function(\"", "Function('"):
        if needle in js:
            print("WARNING: engine contains %r - the page CSP has no 'unsafe-eval'; run the "
                  "headless tests (tests/run_tests.js --engine real) before shipping." % needle)
    head = js[:400]
    if VERSION not in js:
        print("WARNING: version string %s not found in the bundle header: %r" % (VERSION, head[:120]))

    os.makedirs(VENDOR, exist_ok=True)
    fetched_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "# UFOS.ai Market Research chart engine - vendored files (SPEC MR3 bridge contract v1).",
        "# Format: <file> <version> <source_url> <sha256> <fetched_at>   ('#' lines are comments)",
        "# package %s@%s, npm integrity %s, licence Apache-2.0" % (NAME, VERSION, got),
        "# extracted members: " + ", ".join(m for m, o, _ in MEMBERS if o in files),
    ]
    for out in [o for _, o, _ in MEMBERS if o in files]:
        path = os.path.join(VENDOR, out)
        stale(path)
        with open(path, "wb") as fh:
            fh.write(files[out])
        lines.append("%s %s %s %s %s" % (out, VERSION, TARBALL, hashlib.sha256(files[out]).hexdigest(), fetched_at))
        print("wrote vendor/%s (%d bytes)" % (out, len(files[out])))
    manifest = os.path.join(VENDOR, "MANIFEST")
    stale(manifest)
    with open(manifest, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print("wrote vendor/MANIFEST")


if __name__ == "__main__":
    main()

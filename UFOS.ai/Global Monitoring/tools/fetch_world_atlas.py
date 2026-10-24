#!/usr/bin/env python3
"""Vendor the pinned Natural Earth land outlines (world-atlas) into ../vendor (S10-A2 M4; a maintenance tool).

    python fetch_world_atlas.py [--expect-integrity sha512-...] [--tarball FILE] [--metadata FILE]

Reads the live registry metadata of the pinned version (https://registry.npmjs.org/world-atlas/2.0.2)
and requires its `dist.integrity` == the pinned sha512 and `dist.tarball` == the pinned URL, then
downloads the tarball and requires its sha512 == both (three-way match), extracts ONLY
package/land-110m.json and the package licence file (LICENSE / LICENSE.md / LICENSE.txt), checks that
the JSON is a TopoJSON topology with a "land" object, and writes vendor/MANIFEST in the same format as
the Market Research chart engine (HedgeFund/MarketResearch/Chart/vendor/MANIFEST):
    <file> <version> <source_url> <sha256> <fetched_at>
Global Monitoring embeds vendor/land-110m.json, vendor/MANIFEST and the licence at BUILD time
(GlobalMonitoring.csproj, only when the files exist) and checks the sha256 at load — so rebuild the app
after running this. Existing vendor files are renamed to *.stale (never deleted). Uses the system/env
HTTPS proxy (urllib). Standard library only (any Python 3.8+, a venv is not needed).

Pinned version: world-atlas 2.0.2 (npm; ISC licence; data: Natural Earth, public domain). The integrity
value below was read from the registry through a web reader on 2026-10-24. If the registry disagrees the
script stops; only after checking by hand (`npm view world-atlas@2.0.2 dist.integrity`) may the value be
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

NAME = "world-atlas"
VERSION = "2.0.2"
TARBALL = "https://registry.npmjs.org/world-atlas/-/world-atlas-2.0.2.tgz"
METADATA = "https://registry.npmjs.org/world-atlas/2.0.2"
MAX_METADATA = 5 * 1024 * 1024
INTEGRITY = "sha512-IXfV0qwlKXpckz1FhwXVwKRjiIhOnWttOskm5CtxMsjgE/MXAYRHWJqgXOpM8IkcPBoXnyTU5lFHcYa5ChG0LQ=="
MAX_TARBALL = 30 * 1024 * 1024
MAX_MEMBER = 5 * 1024 * 1024
DATA_MEMBER = ("package/land-110m.json", "land-110m.json")
LICENSE_MEMBERS = ["package/LICENSE", "package/LICENSE.md", "package/LICENSE.txt"]  # the first one present is taken
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
    print("registry metadata ok: %s@%s %s (licence %s)" % (name, version, integrity, meta.get("license")))


def read_member(tar, member):
    info = tar.getmember(member)
    if not info.isfile() or info.size > MAX_MEMBER:
        fail(member + " is not a regular file of sane size")
    return tar.extractfile(info).read()


def check_topology(raw):
    try:
        topo = json.loads(raw.decode("utf-8"))
        ok = (topo.get("type") == "Topology" and isinstance(topo.get("arcs"), list)
              and "land" in topo.get("objects", {}) and "scale" in topo.get("transform", {}))
    except Exception:
        ok = False
    if not ok:
        fail("land-110m.json is not a TopoJSON topology with transform, arcs and objects.land")


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
              "!! Record the new value in this script.\n%s" % (bar, INTEGRITY, expected, bar))

    raw = open(a.metadata, "rb").read(MAX_METADATA + 1) if a.metadata else download(METADATA, MAX_METADATA, "registry metadata")
    check_metadata(raw, expected)

    data = open(a.tarball, "rb").read(MAX_TARBALL + 1) if a.tarball else download(TARBALL, MAX_TARBALL, "tarball")
    if len(data) > MAX_TARBALL:
        fail("tarball larger than %d bytes" % MAX_TARBALL)
    got = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if got != expected:
        fail("tarball sha512 does not match the pinned/registry integrity\n  expected %s\n  computed %s" % (expected, got))
    print("tarball integrity ok (pin == registry == tarball): " + got)

    files = []  # (vendored name, source member, bytes)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = set(tar.getnames())
        member, out = DATA_MEMBER
        if member not in names:
            fail("tarball has no " + member)
        land = read_member(tar, member)
        check_topology(land)
        files.append((out, member, land))
        lic = next((m for m in LICENSE_MEMBERS if m in names), None)
        if lic is None:
            fail("tarball has no licence file (looked for %s)" % ", ".join(LICENSE_MEMBERS))
        files.append((os.path.basename(lic), lic, read_member(tar, lic)))

    os.makedirs(VENDOR, exist_ok=True)
    fetched_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "# UFOS.ai Global Monitoring - vendored Natural Earth land outlines (S10-A2 M4).",
        "# Format: <file> <version> <source_url> <sha256> <fetched_at>   ('#' lines are comments)",
        "# package %s@%s, npm integrity %s, licence ISC (data: Natural Earth, public domain)" % (NAME, VERSION, got),
        "# extracted members: " + ", ".join(m for _, m, _ in files),
    ]
    for out, _, blob in files:
        path = os.path.join(VENDOR, out)
        stale(path)
        with open(path, "wb") as fh:
            fh.write(blob)
        lines.append("%s %s %s %s %s" % (out, VERSION, TARBALL, hashlib.sha256(blob).hexdigest(), fetched_at))
        print("wrote vendor/%s (%d bytes)" % (out, len(blob)))
    manifest = os.path.join(VENDOR, "MANIFEST")
    stale(manifest)
    with open(manifest, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print("wrote vendor/MANIFEST")
    print("Now rebuild Global Monitoring (the vendored files are embedded at build time).")


if __name__ == "__main__":
    main()

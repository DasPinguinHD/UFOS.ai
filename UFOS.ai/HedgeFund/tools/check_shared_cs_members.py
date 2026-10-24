#!/usr/bin/env python3
"""Static check for UFOS.ai/Shared/*.cs (no compiler here): duplicate member and type names.

    python check_shared_cs_members.py [Shared folder]     # exit 0 = clean, 1 = duplicates (listed), 2 = folder missing

Why: Shared/*.cs is linked into four assemblies (main app, Hedge Fund, Global Monitoring, LiveStream Agent); a duplicate
breaks all four builds at once (2026-10-24: two `LockFileName` constants in PythonEnvironment.cs → CS0102 / CS0229,
9 errors). Checked per type (partial types across files merged, nested types separately):
  - two fields / constants / properties / events / nested types with the same name,
  - a method with the name of such a member (methods themselves may overload),
  - two top-level types with the same name in the same namespace (across all Shared files).
A light tokenizer (comments, strings incl. verbatim / interpolated / raw / char literals skipped), not a C# parser —
good for this code base's style. Used by backend/tests/test_s10_shared_cs.py. Stdlib only.
"""
import os
import re
import sys

KEYWORDS = {
    "public", "private", "protected", "internal", "static", "readonly", "const", "volatile", "new", "override",
    "virtual", "abstract", "sealed", "extern", "unsafe", "partial", "async", "required", "file", "event", "ref",
    "implicit", "explicit", "operator", "this", "fixed", "in", "out", "params", "return", "if", "while", "for",
    "foreach", "switch", "using", "lock", "catch", "typeof", "sizeof", "nameof", "default", "where", "when",
}
TYPE_KW = ("class", "struct", "record", "interface", "enum")
IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


def blank_literals(src):
    """Same length text with comments, strings and char literals replaced by spaces (newlines kept)."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j); i = j; continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j); i = j; continue
        m = re.match(r'\$*@?\$*"""+', src[i:i + 12])
        if m and '"""' in m.group(0):           # raw string literal
            quotes = m.group(0).count('"')
            j = src.find('"' * quotes, i + len(m.group(0)))
            j = n if j < 0 else j + quotes
            blank(i, j); i = j; continue
        m = re.match(r'(\$@|@\$|@|\$)?"', src[i:i + 3])
        if m:
            verbatim = "@" in (m.group(1) or "")
            j = i + len(m.group(0))
            while j < n:
                if verbatim and src.startswith('""', j):
                    j += 2; continue
                if not verbatim and src[j] == "\\":
                    j += 2; continue
                if src[j] == '"':
                    j += 1; break
                if src[j] == "\n" and not verbatim:
                    break
                j += 1
            blank(i, j); i = j; continue
        if c == "'":
            m = re.match(r"'(\\.[^']*|[^'\\])'", src[i:i + 12])
            if m:
                blank(i, i + len(m.group(0))); i += len(m.group(0)); continue
        i += 1
    return "".join(out)


def member_name(head):
    """(name, kind) of one member declaration head (text before its ';' or '{' / '=>'), or None."""
    h = re.sub(r"\[[^\[\]]*\]", " ", head)          # attributes
    h = " ".join(h.split())
    if not h:
        return None
    m = re.search(r"\b(" + "|".join(TYPE_KW) + r")\s+(" + IDENT + r")", h)
    if m and not re.search(r"\bnew\s*\(", h[:m.start()]):
        return (m.group(2), "type")
    eq = h.find("=")
    lambda_at = h.find("=>")
    paren = h.find("(")
    first_stop = min([p for p in (eq, lambda_at) if p >= 0] or [len(h)])
    if paren >= 0 and paren < first_stop:
        before = h[:paren].rstrip()
        before = re.sub(r"<[^<>]*>$", "", before).rstrip()        # generic method
        mm = re.search(r"(" + IDENT + r")$", before)
        if mm and mm.group(1) not in KEYWORDS and before[:mm.start()].strip():
            return (mm.group(1), "method")
        if mm and mm.group(1) not in KEYWORDS and not before[:mm.start()].strip():
            return None                                          # a statement-like line, not a declaration
        if not mm or mm.group(1) in KEYWORDS:
            # tuple-typed field / property: "(string A, int B)[] Name = …" — name before = / ; / {
            pass
    target = h[:first_stop].rstrip()
    target = re.sub(r"\s*\{\s*$", "", target)
    names = [p.strip() for p in target.split(",")]
    mm = re.search(r"(" + IDENT + r")\s*$", names[0] if len(names) == 1 else target)
    if not mm or mm.group(1) in KEYWORDS:
        return None
    kind = "event" if re.search(r"\bevent\b", h) else "const" if re.search(r"\bconst\b", h) else "field/property"
    if "operator" in h.split():
        return None
    return (mm.group(1), kind)


def scan_file(path):
    """Yields (namespace, type path, member name, kind, line) for every member, and top-level types."""
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        src = fh.read()
    code = blank_literals(src)
    results = []
    namespace = ""
    m = re.search(r"\bnamespace\s+([A-Za-z0-9_.]+)\s*;", code)
    if m:
        namespace = m.group(1)
    stack = []          # frames: ("ns", name) / ("type", name) / ("block", None)
    head_start = 0
    i, n = 0, len(code)
    line_of = lambda pos: code.count("\n", 0, pos) + 1   # noqa: E731

    def current_type():
        return next((f for f in reversed(stack) if f[0] != "ns"), None)

    while i < n:
        c = code[i]
        if c in "{;" or code.startswith("=>", i):
            head = code[head_start:i]
            frame = current_type()
            in_type_body = frame is not None and frame[0] == "type"
            if c == "{":
                hs = " ".join(head.split())
                mns = re.match(r"^namespace\s+([A-Za-z0-9_.]+)$", hs)
                if mns and (not stack or all(f[0] == "ns" for f in stack)):
                    stack.append(("ns", mns.group(1)))
                    namespace = ".".join(f[1] for f in stack if f[0] == "ns")
                else:
                    info = member_name(head) if (in_type_body or frame is None) else None
                    if info and info[1] == "type":
                        tpath = ".".join([f[1] for f in stack if f[0] == "type"] + [info[0]])
                        results.append((namespace, tpath, info[0], "type", line_of(i)))
                        stack.append(("type", tpath))
                    else:
                        if in_type_body and info:
                            results.append((namespace, frame[1], info[0], info[1], line_of(i)))
                        stack.append(("block", None))
                i += 1; head_start = i; continue
            if code.startswith("=>", i):
                if in_type_body:
                    info = member_name(head)
                    if info and info[1] != "type":
                        results.append((namespace, frame[1], info[0], info[1], line_of(i)))
                    # skip the expression body up to its ';' at this depth
                    depth, j = 0, i + 2
                    while j < n:
                        if code[j] in "({[":
                            depth += 1
                        elif code[j] in ")}]":
                            depth -= 1
                        elif code[j] == ";" and depth <= 0:
                            break
                        j += 1
                    i = j + 1; head_start = i; continue
                i += 2; continue
            # ';'
            if in_type_body:
                hs = head.strip()
                # enum members end with ',' — handled at '}' below; record positional params ignored
                info = member_name(head) if hs else None
                if info and info[1] != "type":
                    results.append((namespace, frame[1], info[0], info[1], line_of(i)))
            i += 1; head_start = i; continue
        if c == "}":
            if stack:
                stack.pop()
            i += 1; head_start = i; continue
        if c in "([":
            # skip a balanced (…)/[…] group so ';' / '{' inside (for-loops, lambdas in arguments) don't split heads
            close = ")" if c == "(" else "]"
            depth, j = 1, i + 1
            while j < n and depth:
                if code[j] == c:
                    depth += 1
                elif code[j] == close:
                    depth -= 1
                j += 1
            i = j; continue
        i += 1
    return results


def check(folder):
    """['<file>:<line>: <message>'] for every duplicate in the folder's *.cs files."""
    members = {}      # (namespace, type) -> {name: [(kind, file, line)]}
    top_types = {}    # (namespace, name) -> [(file, line)]
    partial_types = set()
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".cs"):
            continue
        path = os.path.join(folder, name)
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            raw = fh.read()
        for ns, tpath, member, kind, line in scan_file(path):
            if kind == "type":
                if "." not in tpath:
                    is_partial = re.search(r"\bpartial\s+(?:class|struct|record|interface)\s+" + re.escape(member) + r"\b", raw) is not None
                    if is_partial:
                        partial_types.add((ns, tpath))
                    top_types.setdefault((ns, tpath), []).append((name, line))
                parent = tpath.rsplit(".", 1)[0] if "." in tpath else None
                if parent:
                    members.setdefault((ns, parent), {}).setdefault(member, []).append(("type", name, line))
                continue
            members.setdefault((ns, tpath), {}).setdefault(member, []).append((kind, name, line))
    problems = []
    for (ns, tname), places in top_types.items():
        if len(places) > 1 and (ns, tname) not in partial_types:
            first = places[0]
            for f, ln in places[1:]:
                problems.append(f"{f}:{ln}: type {ns}.{tname} is also declared in {first[0]}:{first[1]}")
    for (ns, tname), names in members.items():
        for member, decls in names.items():
            non_methods = [d for d in decls if d[0] != "method"]
            methods = [d for d in decls if d[0] == "method"]
            if len(non_methods) > 1:
                first = non_methods[0]
                for kind, f, ln in non_methods[1:]:
                    problems.append(f"{f}:{ln}: {tname}.{member} ({kind}) duplicates the {first[0]} at {first[1]}:{first[2]}")
            if non_methods and methods:
                kind, f, ln = methods[0]
                first = non_methods[0]
                problems.append(f"{f}:{ln}: method {tname}.{member} has the name of the {first[0]} at {first[1]}:{first[2]}")
    return sorted(set(problems))


def main(argv):
    folder = argv[1] if len(argv) > 1 else os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Shared"))
    if not os.path.isdir(folder):
        print("folder not found: " + folder, file=sys.stderr)
        return 2
    problems = check(folder)
    for p in problems:
        print(p)
    print("OK - no duplicate member or type names in " + folder if not problems else "%d duplicate(s)" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""S10-E guard (C# / XAML side): no WPF code reads a .env file or a secret from the process environment.

    python check_no_env_cs.py [UFOS.ai folder]      # exit 0 = clean, 1 = violations (listed), 2 = folder not found

User decision 2026-10-24: keys live ONLY in the Windows Credential Manager (Shared/CredentialVault.cs +
Shared/CredentialCatalog.cs), settings in the modules' settings files. The ONE exception is the one-time migration
"Import from old .env files…" (HedgeFund/Platform/EnvMigration*.cs, BackendSettingSpecs.cs), which runs on the user's PC.
Scans every *.cs / *.xaml / *.csproj under the folder (bin, obj, .venv, node_modules and *.stale skipped).
Used by backend/tests/test_s10_env_cs_guard.py. Stdlib only.
"""
import os
import re
import sys

# Exempt by EXACT path (relative to the UFOS.ai folder) and only the one-time importer: "Import from old .env files…"
# has to find and read the old .env files once, on the user's PC, to move the keys into the Credential Manager and
# the settings into data/settings.json (user decision 2026-10-24); BackendSettingSpecs.cs is its .env-name table.
# Nothing else in the solution may read .env.
ALLOWED = {
    "HedgeFund/Platform/EnvMigration.cs",
    "HedgeFund/Platform/EnvMigrationDialog.cs",
    "HedgeFund/Platform/BackendSettingSpecs.cs",
}
SKIP_DIRS = {"bin", "obj", ".venv", "venv", "node_modules", ".git", "__pycache__", ".vs", "backend"}
EXTENSIONS = (".cs", ".xaml", ".csproj")

# Code patterns (comments are stripped first): a ".env" file path, a dotenv library, the retired EnvConfig, or a
# secret-like name read from the process environment.
PATTERNS = [
    (re.compile(r'"[^"\n]*(?<![A-Za-z0-9_])\.env(?:\.example)?"'), 'a ".env" path in a string'),
    (re.compile(r"\bDotNetEnv\b|\bdotenv\b", re.IGNORECASE), "a dotenv library"),
    (re.compile(r"\bEnvConfig\b"), "the retired EnvConfig reader"),
    (re.compile(r'GetEnvironmentVariable\s*\(\s*"[^"]*(KEY|SECRET|TOKEN|PASSW|CREDENTIAL)[^"]*"', re.IGNORECASE),
     "a secret read from the process environment"),
]
CSPROJ_PATTERN = re.compile(r'Include="[^"]*\.env(?:\.example)?"', re.IGNORECASE)


def strip_comments(text):
    """Removes // and /* */ comments outside string literals (good enough for this guard)."""
    out, i, n = [], 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and in_str == '"' and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == in_str:
                in_str = None
            elif c == "\n" and in_str == '"':
                in_str = None
            i += 1
            continue
        if c == '"' or c == "'":
            in_str = c; out.append(c); i += 1; continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c); i += 1
    return "".join(out)


def scan(root, prefix=""):
    """[(relative path, line number, reason)] for every violation under root (the UFOS.ai folder). ``prefix`` = the
    path of ``root`` inside the UFOS.ai folder (e.g. "HedgeFund/" for a HedgeFund-only scan), so the exact
    exemptions in ALLOWED match there too."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(EXTENSIONS):
                continue
            path = os.path.join(dirpath, name)
            rel = prefix + os.path.relpath(path, root).replace(os.sep, "/")
            if rel in ALLOWED:
                continue
            try:
                with open(path, encoding="utf-8-sig", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if name.endswith(".csproj"):
                for no, line in enumerate(text.splitlines(), 1):
                    if CSPROJ_PATTERN.search(line):
                        found.append((rel, no, "a .env file included in the project"))
                continue
            code = strip_comments(text) if name.endswith(".cs") else re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
            for no, line in enumerate(code.splitlines(), 1):
                for pattern, reason in PATTERNS:
                    if pattern.search(line):
                        found.append((rel, no, reason))
    return found


def main(argv):
    root = argv[1] if len(argv) > 1 else os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    if not os.path.isdir(root):
        print("folder not found: " + root, file=sys.stderr)
        return 2
    violations = scan(root)
    for rel, no, reason in violations:
        print("%s:%d: %s" % (rel, no, reason))
    print("OK - no C#/XAML code reads .env" if not violations else "%d violation(s)" % len(violations))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""PDF -> text worker, run ONLY as a separate process by ``research.papers.extract_pdf`` (SPEC §14.1).

argv: <pdf path> <max pages> <max chars> <memory limit MB>. Prints one JSON object to stdout:
{pages, text, title, authors, year, truncated} or {error: code, message}. Pure Python (pypdf, pinned in
requirements.txt); no JavaScript, no embedded files/attachments, no OCR, no network. Memory guard:
RLIMIT_AS on POSIX; on Windows a watchdog thread ends the process when the working set exceeds the limit.
Exit codes: 0 ok / reported error, 3 memory limit, 4 extractor missing.
"""
import json
import os
import sys
import threading
import time


def _guard(limit_mb: int) -> None:
    limit = int(limit_mb) * 1024 * 1024
    try:
        import resource                      # POSIX
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return
    except Exception:                        # noqa: BLE001 — Windows: poll the working set
        pass
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    def watch():
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        h = k32.GetCurrentProcess()
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        while True:
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) and \
                    max(pmc.WorkingSetSize, pmc.PagefileUsage) > limit:
                os._exit(3)
            time.sleep(0.1)
    threading.Thread(target=watch, daemon=True).start()


def _out(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def main(argv) -> int:
    path, max_pages, max_chars, mem_mb = argv[1], int(argv[2]), int(argv[3]), int(argv[4])
    _guard(mem_mb)
    try:
        from pypdf import PdfReader
    except ImportError:
        _out({"error": "missing_extractor", "message": "The PDF extractor (pypdf) is not installed."})
        return 4
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:                # noqa: BLE001
                ok = 0
            if not ok:
                _out({"error": "encrypted", "message": "The PDF is encrypted."})
                return 0
        n = len(reader.pages)
        if n > max_pages:
            _out({"error": "too_many_pages", "message": f"The PDF has {n} pages (at most {max_pages})."})
            return 0
        parts, size, truncated = [], 0, False
        for i, page in enumerate(reader.pages):
            t = page.extract_text() or ""
            chunk = f"[page {i + 1}]\n{t}\n"
            if size + len(chunk) > max_chars:
                parts.append(chunk[:max(0, max_chars - size)])
                truncated = True
                break
            parts.append(chunk)
            size += len(chunk)
        meta = reader.metadata or {}
        title = str(meta.get("/Title") or "") or None
        authors = str(meta.get("/Author") or "") or None
        year = None
        created = str(meta.get("/CreationDate") or "")
        if created.startswith("D:") and created[2:6].isdigit():
            year = int(created[2:6])
        _out({"pages": n, "text": "".join(parts), "title": title, "authors": authors, "year": year,
              "truncated": truncated})
        return 0
    except MemoryError:
        return 3
    except Exception as ex:                  # noqa: BLE001 — reported as an unreadable PDF
        _out({"error": "unreadable", "message": f"The PDF could not be read ({type(ex).__name__})."})
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

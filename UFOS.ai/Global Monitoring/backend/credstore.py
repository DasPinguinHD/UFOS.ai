"""Secrets of this backend — Windows Credential Manager only (stage 10, S10-E: .env was removed, user decision
2026-10-24). The keys are entered or imported in the app (⚙ Keys & data); the backend only READS them, at use time
(a changed key applies without a restart), and never logs, returns or sends a value anywhere except to the service
it belongs to. Nothing is read from .env or from environment variables.

Credential format (contract with the app's credential store, the same as HedgeFund/backend/strategy_platform/
secrets.py): generic credential, TargetName = the target below, CredentialBlob = UTF-8 bytes of the value,
Persist = LOCAL_MACHINE (this PC, this Windows user). Residual risk (told to the user): programs running as the same
Windows user can read these credentials.

``openrouter-api-key`` is ONE key shared by all modules (user decision 2026-10-24): the existing HedgeFund entry.

The same file is used by the Global Monitoring and LiveStreamAgent backends (a test keeps the copies byte-identical).
Stdlib + ctypes only. Part of UFOS.ai, a proof-of-concept portfolio project.
"""
import sys
import threading

# name -> (Credential Manager target, label shown to the user)
TARGETS = {
    "openrouter-api-key": ("UFOS.ai/HedgeFund/openrouter-api-key", "OpenRouter API key"),
    "deepgram-api-key": ("UFOS.ai/LiveStreamAgent/deepgram-api-key", "Deepgram API key"),
    "opensky-client-id": ("UFOS.ai/GlobalMonitoring/opensky-client-id", "OpenSky client id"),
    "opensky-client-secret": ("UFOS.ai/GlobalMonitoring/opensky-client-secret", "OpenSky client secret"),
}
WHERE = "enter it in ⚙ Keys & data"

_ERROR_NOT_FOUND = 1168
_CRED_TYPE_GENERIC = 1


class SecretStoreUnavailable(Exception):
    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


class SecretMissing(Exception):
    def __init__(self, name: str):
        self.name = name
        self.user_message = f"{TARGETS[name][1]} is not set — {WHERE}."
        super().__init__(self.user_message)


def label(name: str) -> str:
    return TARGETS[name][1]


def decode_blob(raw: bytes, name: str = ""):
    """CredentialBlob bytes -> value (str) or None when empty; invalid UTF-8 / NUL -> SecretStoreUnavailable (never
    a partly decoded value)."""
    try:
        value = bytes(raw or b"").decode("utf-8")
    except (UnicodeDecodeError, TypeError, ValueError):
        value = None
    if value is None or "\x00" in value:
        what = TARGETS.get(name, ("", "This key"))[1]
        raise SecretStoreUnavailable(f"{what} is stored in an unreadable format — set it again in ⚙ Keys & data.",
                                     detail="invalid UTF-8 or NUL byte")
    value = value.strip()
    return value or None


class _WindowsCredentialStore:
    """Reads generic credentials via advapi32 CredReadW (ctypes, no dependency)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
                ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
            ]

        self._ctypes = ctypes
        self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self._PCRED = ctypes.POINTER(CREDENTIALW)
        self._advapi.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(self._PCRED)]
        self._advapi.CredReadW.restype = wt.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    def read(self, target: str, name: str = ""):
        ctypes = self._ctypes
        pcred = self._PCRED()
        if not self._advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            err = ctypes.get_last_error()
            if err == _ERROR_NOT_FOUND:
                return None
            raise SecretStoreUnavailable("The Windows Credential Manager could not be read.",
                                         detail=f"CredReadW error {err}")
        try:
            cred = pcred.contents
            return decode_blob(ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize), name)
        finally:
            self._advapi.CredFree(pcred)


class MemoryStore:
    """Test double: target -> value."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def read(self, target: str, name: str = ""):
        v = self.values.get(target)
        return (str(v).strip() or None) if v is not None else None


_lock = threading.Lock()
_store = None


def set_store(store) -> None:
    """Tests inject a MemoryStore (None = back to the Windows Credential Manager)."""
    global _store
    with _lock:
        _store = store


def _get_store():
    global _store
    with _lock:
        if _store is None:
            if sys.platform != "win32":
                raise SecretStoreUnavailable("Keys are stored in the Windows Credential Manager, which is only "
                                             "available on Windows.")
            _store = _WindowsCredentialStore()
        return _store


def get_secret(name: str):
    """The value or None (not set). Unknown name -> KeyError; store failure -> SecretStoreUnavailable."""
    if name not in TARGETS:
        raise KeyError(f"unknown secret {name!r}")
    return _get_store().read(TARGETS[name][0], name)


def require_secret(name: str) -> str:
    value = get_secret(name)
    if not value:
        raise SecretMissing(name)
    return value


def problem(name: str):
    """None when the key is set, else (code, user message): ("key_missing", …) | ("key_store_unavailable", …)."""
    try:
        require_secret(name)
        return None
    except SecretMissing as ex:
        return "key_missing", ex.user_message
    except SecretStoreUnavailable as ex:
        return "key_store_unavailable", ex.user_message


def status(names=None) -> list:
    """[{name, label, target, present, error}] — never values."""
    out = []
    for name in (names or list(TARGETS)):
        target, lab = TARGETS[name]
        entry = {"name": name, "label": lab, "target": target, "present": None, "error": None}
        try:
            entry["present"] = bool(get_secret(name))
        except SecretStoreUnavailable as ex:
            entry["error"] = ex.user_message
        out.append(entry)
    return out

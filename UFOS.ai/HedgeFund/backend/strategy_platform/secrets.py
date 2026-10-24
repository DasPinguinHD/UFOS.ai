"""Secrets for the HedgeFund module — Windows Credential Manager only.

User decisions (2026-10-02): every key the HedgeFund backend needs is stored
in the Windows Credential Manager (DPAPI-encrypted per Windows user), entered
or imported in the WPF window (Hedge Fund → Settings → Keys, see
HedgeFund/Platform/CredentialStore.cs). The backend only READS them, at use
time (so a changed key applies without a restart), and never logs, returns or
sends them anywhere except to the service they belong to.

Nothing is read from .env or the environment (stage 10, S10-E: .env removed;
the app's ⚙ Keys & data imports old .env files once).

Credential format (contract with CredentialStore.cs): generic credential,
TargetName = "UFOS.ai/HedgeFund/<name>", CredentialBlob = UTF-8 bytes of the
value, Persist = LOCAL_MACHINE (stays on this PC, for this Windows user).

Residual risk (told to the user): programs running as the same Windows user
can read these credentials.
"""
import sys
import threading

TARGET_PREFIX = "UFOS.ai/HedgeFund/"

# name -> label shown to the user (stage 10 S10-E: the legacy .env-name column is gone)
SECRETS = {
    # ONE OpenRouter key shared by all modules (user decision 2026-10-24): GM / LSA / the main app read this target too
    "openrouter-api-key": "OpenRouter API key",
    "fred-api-key": "FRED API key",
    "sec-edgar-user-agent": "SEC EDGAR user agent (name + e-mail)",
    "alpaca-paper-key-id": "Alpaca paper API key ID",
    "alpaca-paper-secret-key": "Alpaca paper API secret key",
    # stage 3 (SPEC §8.1): pre-2016 daily prices
    "tiingo-api-key": "Tiingo API key",
    # stage 8b (SPEC §18.14): Binance Spot DEMO MODE keys (demo.binance.com → API Management, HMAC)
    "binance-demo-api-key": "Binance Spot demo API key",
    "binance-demo-api-secret": "Binance Spot demo API secret (HMAC)",
    # stage 9d (SPEC §20.11): EODHD end-of-day prices + ISIN search for the Shadow Account
    "eodhd-api-key": "EODHD API key",
}

# Key names of the OTHER modules (stage 10 S10-E, the app's shared credential catalog Shared/CredentialCatalog.cs):
# never read here, but POST /v2/settings/keys/changed audits their set / imported / removed like HedgeFund's own.
OTHER_MODULE_SECRETS = {
    "deepgram-api-key": "Deepgram API key (LiveStream Agent)",
    "opensky-client-id": "OpenSky client id (Global Monitoring)",
    "opensky-client-secret": "OpenSky client secret (Global Monitoring)",
}

# Retired key names (stage 10, S10-K: the Kraken Futures demo was retired 2026-10-19 and its adapter removed). Nothing
# reads them; POST /v2/settings/keys/changed still accepts them for action "removed", so removing a still-stored key
# from the Credential Manager (⚙ Keys & data) is audited cleanly.
RETIRED_SECRETS = {
    "kraken-demo-api-key": "Kraken Futures demo API key (retired)",
    "kraken-demo-api-secret": "Kraken Futures demo API secret (retired)",
}

_ERROR_NOT_FOUND = 1168
_CRED_TYPE_GENERIC = 1


def _label_for_target(target_name: str) -> str:
    name = str(target_name or "")[len(TARGET_PREFIX):] if str(target_name or "").startswith(TARGET_PREFIX) else ""
    return SECRETS.get(name, "This key")


def decode_blob(raw: bytes, target_name: str = ""):
    """CredentialBlob bytes -> value (str) or None when empty. Invalid UTF-8 or
    NUL bytes -> SecretStoreUnavailable "<label> is stored in an unreadable
    format — set it again in Settings → Keys" (never a UnicodeDecodeError,
    never a partly decoded value)."""
    from .core.errors import SecretStoreUnavailable
    try:
        value = bytes(raw or b"").decode("utf-8")
    except (UnicodeDecodeError, TypeError, ValueError):
        value = None
    if value is None or "\x00" in value:
        raise SecretStoreUnavailable(f"{_label_for_target(target_name)} is stored in an unreadable format — "
                                     f"set it again in Settings → Keys.", detail="invalid UTF-8 or NUL byte")
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

    def read(self, target: str):
        ctypes = self._ctypes
        pcred = self._PCRED()
        if not self._advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            err = ctypes.get_last_error()
            if err == _ERROR_NOT_FOUND:
                return None
            from .core.errors import SecretStoreUnavailable
            raise SecretStoreUnavailable("The Windows Credential Manager could not be read.",
                                         detail=f"CredReadW error {err}")
        try:
            cred = pcred.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return decode_blob(raw, target)
        finally:
            self._advapi.CredFree(pcred)


class MemoryStore:
    """Test double (and the store on non-Windows systems in tests): target -> value."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def read(self, target: str):
        return self.values.get(target)


_lock = threading.Lock()
_store = None


def _default_store():
    if sys.platform != "win32":
        from .core.errors import SecretStoreUnavailable
        raise SecretStoreUnavailable("Keys are stored in the Windows Credential Manager, which is "
                                     "only available on Windows.")
    return _WindowsCredentialStore()


def set_store(store) -> None:
    """Tests inject a MemoryStore here (`None` = back to the default)."""
    global _store
    with _lock:
        _store = store


def _get_store():
    global _store
    with _lock:
        if _store is None:
            _store = _default_store()
        return _store


def target(name: str) -> str:
    return TARGET_PREFIX + name


def get_secret(name: str):
    """The value, or None when not set. Raises SecretStoreUnavailable when the store fails."""
    if name not in SECRETS:
        raise KeyError(f"unknown secret {name!r}")
    return _get_store().read(target(name))


def require_secret(name: str) -> str:
    """The value, or SecretMissing (user-facing "enter it in Settings → Keys")."""
    value = get_secret(name)
    if not value:
        from .core.errors import SecretMissing
        raise SecretMissing(name, SECRETS[name])
    return value


def status() -> list:
    """For GET /v2/settings/keys — never contains values."""
    out = []
    for name, label in SECRETS.items():
        entry = {"name": name, "label": label, "target": target(name)}
        try:
            entry["present"] = bool(get_secret(name))
            entry["error"] = None
        except Exception as ex:  # SecretStoreUnavailable or OS error
            entry["present"] = None
            entry["error"] = getattr(ex, "user_message", None) or "The Credential Manager could not be read."
        out.append(entry)
    return out

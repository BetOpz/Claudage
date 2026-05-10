"""
claude_api.py - Fetch live usage data from the claude.ai internal API.

Endpoint: GET /api/organizations/{org_id}/usage
Auth:     sessionKey cookie (auto-extracted from any installed browser)
Returns:  five_hour and seven_day utilization percentages direct from Anthropic.
"""

import ctypes
import json
import logging
import shutil
import sqlite3
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CLAUDE_AI_BASE = "https://claude.ai"
SESSION_STORE  = Path.home() / ".claude-usage-widget" / "session.json"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── DPAPI decryption (Chrome-family encrypted cookies) ───────────────────────

def _dpapi_decrypt(ciphertext: bytes) -> Optional[bytes]:
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

    p_in = DATA_BLOB(len(ciphertext), ctypes.cast(ctypes.c_char_p(ciphertext), ctypes.POINTER(ctypes.c_char)))
    p_out = DATA_BLOB()
    try:
        if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(p_in), None, None, None, None, 0, ctypes.byref(p_out)
        ):
            result = ctypes.string_at(p_out.pbData, p_out.cbData)
            ctypes.windll.kernel32.LocalFree(p_out.pbData)
            return result
    except Exception:
        pass
    return None


def _chrome_decrypt(encrypted_value: bytes, aes_key: Optional[bytes]) -> Optional[str]:
    """Decrypt a Chrome-family cookie value."""
    if not encrypted_value:
        return None

    # v10/v20 prefix = AES-256-GCM with app-bound key (Chrome 127+) — skip for now
    if encrypted_value[:3] in (b"v20",):
        return None

    # v10 prefix = AES-256-GCM with DPAPI-wrapped key
    if encrypted_value[:3] == b"v10" and aes_key:
        try:
            from Crypto.Cipher import AES
            iv = encrypted_value[3:15]
            payload = encrypted_value[15:]
            cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv)
            return cipher.decrypt(payload[:-16]).decode("utf-8")
        except Exception:
            pass

    # Fallback: raw DPAPI (older Chrome / some profiles)
    try:
        plain = _dpapi_decrypt(encrypted_value)
        if plain:
            return plain.decode("utf-8")
    except Exception:
        pass

    return None


def _get_chrome_aes_key(browser_root: Path) -> Optional[bytes]:
    """Extract and unwrap the AES key from Local State."""
    local_state = browser_root / "Local State"
    if not local_state.exists():
        return None
    try:
        with open(local_state, encoding="utf-8") as f:
            state = json.load(f)
        b64_key = state["os_crypt"]["encrypted_key"]
        import base64
        enc_key = base64.b64decode(b64_key)[5:]  # strip DPAPI prefix
        return _dpapi_decrypt(enc_key)
    except Exception:
        return None


# ── Browser cookie extraction ─────────────────────────────────────────────────

def _read_firefox_cookie(profile: Path) -> Optional[str]:
    cookies_db = profile / "cookies.sqlite"
    if not cookies_db.exists():
        return None
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(cookies_db, tmp_path)
        con = sqlite3.connect(tmp_path)
        row = con.execute(
            "SELECT value FROM moz_cookies WHERE host LIKE '%claude.ai' AND name='sessionKey' LIMIT 1"
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception as e:
        logger.debug("Firefox cookie read error: %s", e)
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_chromium_cookie(cookies_db: Path, aes_key: Optional[bytes]) -> Optional[str]:
    if not cookies_db.exists():
        return None
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(cookies_db, tmp_path)
        con = sqlite3.connect(tmp_path)
        # Try both column names used across Chrome versions
        for col in ("encrypted_value", "value"):
            try:
                row = con.execute(
                    f"SELECT {col} FROM cookies WHERE host_key LIKE '%claude.ai' AND name='sessionKey' LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    val = row[0]
                    if isinstance(val, bytes) and col == "encrypted_value":
                        decrypted = _chrome_decrypt(val, aes_key)
                        if decrypted:
                            return decrypted
                    elif isinstance(val, str) and val:
                        return val
            except Exception:
                continue
        con.close()
    except Exception as e:
        logger.debug("Chromium cookie read error: %s", e)
    finally:
        tmp_path.unlink(missing_ok=True)
    return None


def _browser_candidates() -> list[tuple[str, str, Path]]:
    """Return (browser_name, type, cookies_path_or_profile_root) tuples to try."""
    home = Path.home()
    app_data = home / "AppData"
    local = app_data / "Local"
    roaming = app_data / "Roaming"

    # (name, "firefox"|"chromium", path)
    return [
        # Firefox family
        ("Firefox",          "firefox",  roaming / "Mozilla" / "Firefox" / "Profiles"),
        ("Waterfox",         "firefox",  roaming / "Waterfox" / "Profiles"),
        ("LibreWolf",        "firefox",  roaming / "LibreWolf" / "Profiles"),
        # Chromium family — path is the browser root (contains "Local State")
        ("Chrome",           "chromium", local / "Google" / "Chrome" / "User Data"),
        ("Edge",             "chromium", local / "Microsoft" / "Edge" / "User Data"),
        ("Brave",            "chromium", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
        ("Opera",            "chromium", roaming / "Opera Software" / "Opera Stable"),
        ("Opera GX",         "chromium", roaming / "Opera Software" / "Opera GX Stable"),
        ("Vivaldi",          "chromium", local / "Vivaldi" / "User Data"),
        ("Comet",            "chromium", local / "Comet" / "User Data"),
        ("Atlas",            "chromium", local / "Atlas" / "User Data"),
        ("Yandex",           "chromium", local / "Yandex" / "YandexBrowser" / "User Data"),
        ("Chromium",         "chromium", local / "Chromium" / "User Data"),
        ("Arc",              "chromium", local / "Arc" / "User Data"),
        ("Thorium",          "chromium", local / "Thorium" / "User Data"),
        ("Iron",             "chromium", local / "Srware Iron" / "User Data"),
        ("CentBrowser",      "chromium", local / "CentBrowser" / "User Data"),
    ]


def _extract_session_key() -> Optional[str]:
    """Try every known browser and return the first valid claude.ai sessionKey."""
    for name, kind, root in _browser_candidates():
        try:
            if kind == "firefox":
                if not root.exists():
                    continue
                for profile in root.iterdir():
                    key = _read_firefox_cookie(profile)
                    if key:
                        logger.info("Got sessionKey from %s", name)
                        return key

            elif kind == "chromium":
                if not root.exists():
                    continue
                aes_key = _get_chrome_aes_key(root)
                # Check Default profile and numbered profiles
                for profile_dir in ["Default", "Profile 1", "Profile 2", "Profile 3"]:
                    cookies_db = root / profile_dir / "Network" / "Cookies"
                    if not cookies_db.exists():
                        cookies_db = root / profile_dir / "Cookies"
                    key = _read_chromium_cookie(cookies_db, aes_key)
                    if key:
                        logger.info("Got sessionKey from %s (%s)", name, profile_dir)
                        return key
        except Exception as e:
            logger.debug("Browser sweep error for %s: %s", name, e)

    return None


# ── API helpers ───────────────────────────────────────────────────────────────

def _fetch_org_id(session_key: str) -> Optional[str]:
    req = urllib.request.Request(f"{CLAUDE_AI_BASE}/api/organizations")
    req.add_header("Cookie", f"sessionKey={session_key}")
    req.add_header("Accept", "application/json")
    req.add_header("anthropic-client-platform", "web_claude_ai")
    req.add_header("User-Agent", _UA)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            orgs = json.loads(resp.read())
        if isinstance(orgs, list) and orgs:
            return orgs[0].get("uuid") or orgs[0].get("id")
    except Exception as e:
        logger.debug("org fetch failed: %s", e)
    return None


def save_session(org_id: str, session_key: str):
    SESSION_STORE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_STORE, "w") as f:
        json.dump({"org_id": org_id, "session_key": session_key}, f)


def load_session() -> tuple[Optional[str], Optional[str]]:
    if not SESSION_STORE.exists():
        return None, None
    try:
        with open(SESSION_STORE) as f:
            d = json.load(f)
        return d.get("org_id"), d.get("session_key")
    except Exception:
        return None, None


@dataclass
class LiveUsage:
    session_pct: float
    weekly_pct: float
    session_resets_at: Optional[datetime]
    weekly_resets_at: Optional[datetime]
    extra_credits_pct: Optional[float]
    error: Optional[str] = None


def fetch_live_usage(org_id: str, session_key: str) -> LiveUsage:
    url = f"{CLAUDE_AI_BASE}/api/organizations/{org_id}/usage"
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"sessionKey={session_key}")
    req.add_header("Accept", "application/json")
    req.add_header("Referer", "https://claude.ai/settings/usage")
    req.add_header("anthropic-client-platform", "web_claude_ai")
    req.add_header("User-Agent", _UA)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        def parse_dt(s) -> Optional[datetime]:
            if not s:
                return None
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return None

        fh = data.get("five_hour") or {}
        sd = data.get("seven_day") or {}
        ex = data.get("extra_usage") or {}

        return LiveUsage(
            session_pct=float(fh.get("utilization", 0)),
            weekly_pct=float(sd.get("utilization", 0)),
            session_resets_at=parse_dt(fh.get("resets_at")),
            weekly_resets_at=parse_dt(sd.get("resets_at")),
            extra_credits_pct=float(ex["utilization"]) if ex.get("utilization") is not None else None,
        )

    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code} — session may have expired"
        logger.warning("claude.ai usage fetch failed: %s", msg)
        return LiveUsage(0, 0, None, None, None, error=msg)
    except Exception as e:
        logger.warning("claude.ai usage fetch failed: %s", e)
        return LiveUsage(0, 0, None, None, None, error=str(e))


def get_live_usage() -> Optional[LiveUsage]:
    """Fetch live usage, auto-extracting session from any installed browser."""
    org_id, session_key = load_session()

    # Always check browsers for a fresher key
    browser_key = _extract_session_key()
    if browser_key and browser_key != session_key:
        new_org_id = _fetch_org_id(browser_key)
        if new_org_id:
            save_session(new_org_id, browser_key)
            org_id, session_key = new_org_id, browser_key

    if not org_id or not session_key:
        return None

    usage = fetch_live_usage(org_id, session_key)

    # Session expired — try refreshing from browser
    if usage.error and ("401" in usage.error or "expired" in usage.error):
        browser_key = _extract_session_key()
        if browser_key:
            new_org_id = _fetch_org_id(browser_key)
            if new_org_id:
                save_session(new_org_id, browser_key)
                return fetch_live_usage(new_org_id, browser_key)

    return usage

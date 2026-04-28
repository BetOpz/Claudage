"""
claude_api.py - Fetch live usage data from the claude.ai internal API.

Endpoint: GET /api/organizations/{org_id}/usage
Auth:     sessionKey cookie (from browser login to claude.ai)
Returns:  five_hour and seven_day utilization percentages direct from Anthropic.
"""

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CLAUDE_AI_BASE = "https://claude.ai"
SESSION_STORE  = Path.home() / ".claude-usage-widget" / "session.json"


@dataclass
class LiveUsage:
    session_pct: float          # five_hour utilization 0-100
    weekly_pct: float           # seven_day utilization 0-100
    session_resets_at: Optional[datetime]
    weekly_resets_at: Optional[datetime]
    extra_credits_pct: Optional[float]   # extra_usage utilization if enabled
    error: Optional[str] = None


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


def fetch_live_usage(org_id: str, session_key: str) -> LiveUsage:
    url = f"{CLAUDE_AI_BASE}/api/organizations/{org_id}/usage"
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"sessionKey={session_key}")
    req.add_header("Accept", "application/json")
    req.add_header("Referer", "https://claude.ai/settings/usage")
    req.add_header("anthropic-client-platform", "web_claude_ai")
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36",
    )

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
    """Load saved session and fetch live usage. Returns None if not configured."""
    org_id, session_key = load_session()
    if not org_id or not session_key:
        return None
    return fetch_live_usage(org_id, session_key)

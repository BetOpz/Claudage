"""
usage_monitor.py - Read Claude Code JSONL files and calculate real-time usage metrics.

Data source: ~/.claude/projects/**/*.jsonl  (Windows: %USERPROFILE%\\.claude\\projects)
Each line is a JSON record containing token usage for one Claude Code API call.
"""

import json
import os
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

# ── Plan limits ──────────────────────────────────────────────────────────────
# Tokens per 5-hour session block, sourced from reference monitor project.
PLAN_SESSION_LIMITS = {
    "pro":    44_000,
    "max5":   220_000,
    "max20":  880_000,
    "custom": 44_000,
}

SESSION_DURATION_HOURS = 5
SESSIONS_PER_WEEK = (7 * 24) / SESSION_DURATION_HOURS  # ≈ 33.6


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class UsageEntry:
    timestamp: datetime
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    model: str
    message_id: str
    request_id: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class SessionBlock:
    """A 5-hour rolling window of usage entries."""
    start_time: datetime
    end_time: datetime
    entries: List[UsageEntry] = field(default_factory=list)
    total_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def is_active(self) -> bool:
        return self.end_time > datetime.now(timezone.utc)

    @property
    def duration_minutes(self) -> float:
        end = min(self.end_time, datetime.now(timezone.utc))
        return max((end - self.start_time).total_seconds() / 60, 1.0)

    @property
    def burn_rate_per_min(self) -> float:
        return self.total_tokens / self.duration_minutes


@dataclass
class UsageMetrics:
    session_tokens: int = 0
    session_limit: int = 19_000
    session_pct: float = 0.0
    weekly_tokens: int = 0
    weekly_limit: int = 0
    weekly_pct: float = 0.0
    burn_rate_per_min: float = 0.0
    session_remaining_minutes: Optional[float] = None
    weekly_remaining_minutes: Optional[float] = None
    current_session_start: Optional[datetime] = None
    current_session_end: Optional[datetime] = None
    active_session: Optional[SessionBlock] = None
    data_path: Optional[Path] = None
    error: Optional[str] = None


# ── Path discovery ────────────────────────────────────────────────────────────

def get_claude_data_candidates() -> List[Path]:
    """Return ordered list of candidate directories for Claude Code JSONL data."""
    home = Path.home()
    candidates = [
        home / ".claude" / "projects",
        home / ".config" / "claude" / "projects",
    ]
    # Windows-specific locations
    for env_var in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        base = os.environ.get(env_var, "")
        if base:
            candidates += [
                Path(base) / "Claude" / "projects",
                Path(base) / "Claude" / "Code" / "User" / "globalStorage",
                Path(base) / ".claude" / "projects",
            ]
    return candidates


def find_data_path(custom_path: Optional[str] = None) -> Optional[Path]:
    """Return the first directory that contains *.jsonl files."""
    if custom_path:
        p = Path(custom_path)
        if p.exists():
            return p
    for candidate in get_claude_data_candidates():
        if candidate.exists() and any(candidate.rglob("*.jsonl")):
            return candidate
    return None


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_timestamp(value) -> Optional[datetime]:
    """Convert various timestamp formats to a timezone-aware UTC datetime."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            # Unix ms or seconds
            ts = value / 1000 if value > 1e10 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(value)
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _extract_tokens(data: dict) -> Tuple[int, int, int, int]:
    """Pull (input, output, cache_creation, cache_read) from a usage record."""
    # Priority: message.usage > usage > top-level fields
    usage: dict = {}
    msg = data.get("message")
    if isinstance(msg, dict):
        usage = msg.get("usage") or {}
    if not usage:
        usage = data.get("usage") or {}

    def _get(d: dict, *keys) -> int:
        for k in keys:
            v = d.get(k)
            if v is not None:
                return int(v)
        return 0

    if usage:
        inp  = _get(usage, "input_tokens",                "inputTokens")
        out  = _get(usage, "output_tokens",               "outputTokens")
        cc   = _get(usage, "cache_creation_input_tokens", "cacheCreationInputTokens")
        cr   = _get(usage, "cache_read_input_tokens",     "cacheReadInputTokens")
    else:
        inp  = _get(data,  "input_tokens",  "inputTokens")
        out  = _get(data,  "output_tokens", "outputTokens")
        cc   = _get(data,  "cache_creation_tokens", "cacheCreationTokens")
        cr   = _get(data,  "cache_read_tokens",     "cacheReadTokens")
    return inp, out, cc, cr


def _parse_jsonl_file(filepath: Path, seen_ids: set) -> List[UsageEntry]:
    """Parse a single JSONL file; skip duplicates tracked via seen_ids."""
    entries: List[UsageEntry] = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Only assistant records carry usage
                if data.get("type") != "assistant":
                    continue

                ts = _parse_timestamp(
                    data.get("timestamp")
                    or data.get("created_at")
                    or data.get("createdAt")
                )
                if ts is None:
                    continue

                msg = data.get("message") or {}
                msg_id = (
                    msg.get("id")
                    or data.get("message_id")
                    or data.get("messageId")
                    or ""
                )
                request_id = data.get("requestId") or data.get("request_id") or ""
                dedup_key = f"{msg_id}:{request_id}" if msg_id else hashlib.md5(line.encode()).hexdigest()
                if dedup_key in seen_ids:
                    continue
                seen_ids.add(dedup_key)

                inp, out, cc, cr = _extract_tokens(data)
                if inp + out + cc + cr == 0:
                    continue

                cost = float(
                    data.get("cost_usd")
                    or data.get("costUsd")
                    or data.get("cost")
                    or 0.0
                )
                model = str(
                    data.get("model")
                    or msg.get("model", "")
                    or ""
                )
                entries.append(UsageEntry(
                    timestamp=ts,
                    input_tokens=inp,
                    output_tokens=out,
                    cache_creation_tokens=cc,
                    cache_read_tokens=cr,
                    cost_usd=cost,
                    model=model,
                    message_id=msg_id,
                    request_id=request_id,
                ))
    except Exception as e:
        logger.error("Error reading %s: %s", filepath, e)
    return entries


# ── Session block builder ─────────────────────────────────────────────────────

def _build_session_blocks(entries: List[UsageEntry]) -> List[SessionBlock]:
    """
    Group entries into non-overlapping 5-hour SessionBlocks.

    A new block starts whenever an entry falls outside the current block's
    end_time.  Block start is rounded down to the nearest hour.
    """
    if not entries:
        return []

    delta = timedelta(hours=SESSION_DURATION_HOURS)
    blocks: List[SessionBlock] = []
    current: Optional[SessionBlock] = None

    for entry in sorted(entries, key=lambda e: e.timestamp):
        if current is None or entry.timestamp >= current.end_time:
            start = entry.timestamp.replace(minute=0, second=0, microsecond=0)
            current = SessionBlock(start_time=start, end_time=start + delta)
            blocks.append(current)
        current.entries.append(entry)
        current.total_tokens += entry.total_tokens
        current.cost_usd += entry.cost_usd

    return blocks


# ── Public API ────────────────────────────────────────────────────────────────

def calculate_metrics(
    session_limit: int,
    weekly_limit: int,
    custom_path: Optional[str] = None,
) -> UsageMetrics:
    """
    Read all Claude Code JSONL files and compute current usage metrics.

    Returns a UsageMetrics object.  On error, metrics.error is set and
    all numeric fields default to zero.
    """
    metrics = UsageMetrics(session_limit=session_limit, weekly_limit=weekly_limit)

    data_path = find_data_path(custom_path)
    if data_path is None:
        metrics.error = (
            "Claude Code data not found. "
            "Make sure Claude Code is installed and has been used at least once. "
            "Set custom_data_path in the Settings dialog if your data is elsewhere."
        )
        return metrics

    metrics.data_path = data_path

    try:
        now = datetime.now(timezone.utc)
        cutoff_7d = now - timedelta(days=7)

        # Collect all entries from the past 7 days
        seen_ids: set = set()
        all_entries: List[UsageEntry] = []
        for jsonl_file in data_path.rglob("*.jsonl"):
            all_entries.extend(_parse_jsonl_file(jsonl_file, seen_ids))

        recent = [e for e in all_entries if e.timestamp >= cutoff_7d]
        if not recent:
            return metrics

        # Weekly aggregate
        metrics.weekly_tokens = sum(e.total_tokens for e in recent)
        if weekly_limit > 0:
            metrics.weekly_pct = min(100.0, metrics.weekly_tokens / weekly_limit * 100)

        # Build 5-hour blocks and find the active one
        blocks = _build_session_blocks(recent)
        active = [b for b in blocks if b.is_active]

        if active:
            block = active[-1]
            metrics.active_session = block
            metrics.current_session_start = block.start_time
            metrics.current_session_end = block.end_time
            metrics.session_tokens = block.total_tokens
            if session_limit > 0:
                metrics.session_pct = min(100.0, block.total_tokens / session_limit * 100)
            metrics.burn_rate_per_min = block.burn_rate_per_min

            if metrics.burn_rate_per_min > 0:
                session_remaining = max(0, session_limit - block.total_tokens)
                metrics.session_remaining_minutes = session_remaining / metrics.burn_rate_per_min
                weekly_remaining = max(0, weekly_limit - metrics.weekly_tokens)
                metrics.weekly_remaining_minutes = weekly_remaining / metrics.burn_rate_per_min
        else:
            # No active block — show tokens from the most recent completed block
            if blocks:
                last = blocks[-1]
                metrics.session_tokens = last.total_tokens
                if session_limit > 0:
                    metrics.session_pct = min(100.0, last.total_tokens / session_limit * 100)

    except Exception as exc:
        logger.error("Error calculating metrics: %s", exc, exc_info=True)
        metrics.error = f"Error reading data: {exc}"

    return metrics

from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timedelta, timezone

_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_duration(text: str | None) -> timedelta | None:
    """'7d', '12h30m', '1w' -> timedelta. None/'perm'/'forever' -> None."""
    if not text:
        return None
    cleaned = text.strip().lower()
    if cleaned in ("perm", "permanent", "forever", "0"):
        return None
    parts = _DURATION_RE.findall(cleaned)
    if not parts:
        raise ValueError(f"Could not read a duration from {text!r} (try `7d`, `12h`, `30m`).")
    kwargs: dict[str, int] = {}
    for amount, unit in parts:
        key = _UNITS[unit.lower()]
        kwargs[key] = kwargs.get(key, 0) + int(amount)
    return timedelta(**kwargs)


def timestamp(dt: datetime, style: str = "R") -> str:
    """Discord relative timestamp markup."""
    return f"<t:{int(dt.timestamp())}:{style}>"


def random_password(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def chunked(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]

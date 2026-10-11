"""One source of time for the whole host: a clock (replaceable in tests) plus the
configured IANA time zone, used for every stored timestamp, every local time shown to
the character, journal dates and schedule calculations.

Stored timestamps are UTC ISO-8601 with a `Z`. Local presentation and wall-clock
schedules use the configured zone, so DST is handled by zoneinfo rather than by fixed
offsets.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown time zone {name!r}") from e


def detect_timezone() -> str:
    """Best-effort IANA name of the system zone; UTC if it can't be determined."""
    candidates = []
    tz_env = os.environ.get("TZ", "").lstrip(":")
    if tz_env:
        candidates.append(tz_env)
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    for name in candidates:
        try:
            zone(name)
            return name
        except ValueError:
            continue
    return "UTC"


class Timebase:
    def __init__(self, tz: str = "UTC", now: Callable[[], datetime] | None = None):
        self.tz_name = tz
        self.tz = zone(tz)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        return self._now().astimezone(timezone.utc)

    def iso(self, dt: datetime | None = None) -> str:
        return iso(dt or self.now())

    def local(self, when: datetime | str | None = None) -> datetime:
        if when is None:
            dt = self.now()
        elif isinstance(when, str):
            dt = parse(when)
        else:
            dt = when
        return dt.astimezone(self.tz)

    def stamp(self, when: datetime | str | None = None) -> str:
        """Local time as shown to the character, e.g. `2026-10-10 21:05:00 -0700`."""
        return self.local(when).strftime("%Y-%m-%d %H:%M:%S %z")

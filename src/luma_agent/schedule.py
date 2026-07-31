"""The bookable grid: which dates and times exist at all.

This is deliberately separate from availability. "Does 7 PM on August 14 exist as
a seating?" is a static property of the restaurant's calendar. "Is there room at
7 PM on August 14?" is a live question only the API can answer. Conflating the
two is what makes an agent tell a caller a date is "fully booked" when the
restaurant simply is not taking bookings for it.

The reservation API does not expose its grid, so it is loaded from the fixed seed
data shipped with the restaurant. In production this would be a `GET /schedule`
call -- see the API notes in the README.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date as date_type
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("luma.schedule")

_DEFAULT_SEED = Path(__file__).resolve().parents[2] / "starter" / "seed_data.json"


@lru_cache(maxsize=1)
def grid() -> dict[str, list[str]]:
    """Bookable times keyed by date, sorted."""
    path = Path(os.getenv("LUMA_SEED_DATA", _DEFAULT_SEED))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.error("schedule.load_failed", extra={"path": str(path), "error": str(exc)})
        return {}
    return {day: sorted(slots) for day, slots in data.get("availability", {}).items()}


def bookable_dates() -> list[str]:
    return sorted(grid())


def bookable_times(date: str) -> list[str]:
    return grid().get(date, [])


def is_bookable_date(date: str) -> bool:
    return date in grid()


def is_bookable_time(date: str, time: str) -> bool:
    return time in grid().get(date, [])


def spoken_date(value: str) -> str:
    """2026-08-14 -> Friday, August 14."""
    try:
        return date_type.fromisoformat(value).strftime("%A, %B %-d")
    except ValueError:
        return value


def spoken_time(value: str) -> str:
    """18:30 -> 6:30 PM, so the model never has to do arithmetic mid-sentence."""
    hour, minute = (int(part) for part in value.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def describe_window() -> str:
    """One line naming every date the restaurant is actually taking bookings for."""
    dates = bookable_dates()
    if not dates:
        return "The booking calendar is unavailable."
    spoken = [spoken_date(d) for d in dates]
    times = bookable_times(dates[0])
    return (
        f"Bookings are open only for these dates: {', '.join(spoken)}. "
        f"Seating times are {', '.join(spoken_time(t) for t in times)}."
    )

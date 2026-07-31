"""What counts as a legal booking request.

Two related jobs, kept together because they answer the same question — "can I even
ask the API about this?":

  1. Normalizing arguments the LLM produces ("6:30 PM" -> "18:30").
  2. The booking calendar: which dates and times exist at all.

The calendar is deliberately separate from *availability*. "Does 7 PM on August 14
exist as a seating?" is static. "Is there room at 7 PM on August 14?" is a live
question only the API can answer. Conflating the two is what makes an agent tell a
caller a date is "fully booked" when the restaurant simply is not taking bookings
for it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date as date_type
from datetime import datetime
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("luma.rules")

MIN_PARTY_SIZE = 1
MAX_STANDARD_PARTY_SIZE = 8

_DATE_FORMATS = ("%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%m/%d/%Y")
_TIME_12H = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?$", re.IGNORECASE)
_TIME_24H = re.compile(r"^(\d{1,2}):(\d{2})$")

# The API does not expose its calendar, so it comes from the restaurant's fixed seed
# data. In production this would be a GET /schedule call.
_DEFAULT_SEED = Path(__file__).resolve().parents[2] / "starter" / "seed_data.json"


class ArgumentError(ValueError):
    """Raised with a message written for the LLM to act on, not for the caller to hear."""


# --- Argument normalization --------------------------------------------------


def parse_date(value: str) -> str:
    """Normalize a date to YYYY-MM-DD."""
    cleaned = value.strip().replace(",", "")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    raise ArgumentError(
        f"'{value}' is not a usable date. Ask the caller for the date and pass it as YYYY-MM-DD."
    )


def parse_time(value: str) -> str:
    """Normalize a time to 24-hour HH:MM."""
    cleaned = value.strip().lower()

    if match := _TIME_24H.match(cleaned):
        hour, minute = int(match.group(1)), int(match.group(2))
    elif match := _TIME_12H.match(cleaned):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if hour == 12:
            hour = 0
        if match.group(3).lower() == "p":
            hour += 12
    else:
        raise ArgumentError(f"'{value}' is not a usable time. Pass a 24-hour time like 18:30.")

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ArgumentError(f"'{value}' is not a real time of day.")
    return f"{hour:02d}:{minute:02d}"


def validate_party_size(value: int) -> int:
    if value < MIN_PARTY_SIZE:
        raise ArgumentError("Party size must be at least one. Ask the caller how many guests.")
    if value > MAX_STANDARD_PARTY_SIZE:
        raise ArgumentError(
            f"A party of {value} is above the {MAX_STANDARD_PARTY_SIZE}-guest limit for online "
            "booking. Tell the caller a host will arrange it and call transfer_to_human."
        )
    return value


def validate_phone(value: str) -> str:
    """Keep only what the API keeps, then check there are enough digits to be real."""
    normalized = normalize_phone(value)
    digits = sum(c.isdigit() for c in normalized)
    if digits < 10:
        raise ArgumentError(
            f"'{value}' has only {digits} digits. Ask the caller to repeat their full "
            "ten-digit phone number."
        )
    return normalized


def validate_name(value: str) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) < 2:
        raise ArgumentError("That name is too short. Ask the caller for their full name.")
    return cleaned


def validate_confirmation_code(value: str) -> str:
    cleaned = value.strip().upper().replace(" ", "")
    if not re.fullmatch(r"LUMA-?[A-Z0-9]{4}", cleaned):
        raise ArgumentError(
            f"'{value}' is not a Luma confirmation code. They look like LUMA-4821. "
            "Ask the caller to repeat it, or search by phone number instead."
        )
    return cleaned if "-" in cleaned else f"{cleaned[:4]}-{cleaned[4:]}"


def normalize_phone(value: str) -> str:
    """Match the API's own normalization so search-by-phone actually matches."""
    return "".join(c for c in value if c.isdigit() or c == "+")


# --- The booking calendar ----------------------------------------------------


@lru_cache(maxsize=1)
def calendar() -> dict[str, list[str]]:
    """Bookable times keyed by date."""
    path = Path(os.getenv("LUMA_SEED_DATA", _DEFAULT_SEED))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.error("calendar.load_failed", extra={"path": str(path), "error": str(exc)})
        return {}
    return {day: sorted(slots) for day, slots in data.get("availability", {}).items()}


def bookable_dates() -> list[str]:
    return sorted(calendar())


def bookable_times(date: str) -> list[str]:
    return calendar().get(date, [])


def is_bookable_date(date: str) -> bool:
    return date in calendar()


def is_bookable_time(date: str, time: str) -> bool:
    return time in calendar().get(date, [])


def describe_calendar() -> str:
    """One line naming every date the restaurant is actually taking bookings for."""
    dates = bookable_dates()
    if not dates:
        return "The booking calendar is unavailable."
    times = bookable_times(dates[0])
    return (
        f"Bookings are open only for these dates: {', '.join(spoken_date(d) for d in dates)}. "
        f"Seating times are {', '.join(spoken_time(t) for t in times)}."
    )


# --- Speaking numbers --------------------------------------------------------


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


def spoken_phone(value: str) -> str:
    """Group digits so TTS reads a number back at dictation speed."""
    digits = [c for c in value if c.isdigit()]
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return " ".join(digits)
    groups = ["".join(digits[0:3]), "".join(digits[3:6]), "".join(digits[6:10])]
    return ", ".join(" ".join(g) for g in groups)


def spell(code: str) -> str:
    """Space out a confirmation code so TTS reads it character by character."""
    return " ".join(code.replace("-", " "))

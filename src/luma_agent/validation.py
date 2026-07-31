"""Tool-argument validation.

LLMs produce plausible-looking arguments, not valid ones. Everything the model
sends is normalized and range-checked here before it can reach the API, so a
malformed argument becomes a correctable instruction to the model rather than a
422 the caller hears as a failure.
"""

from __future__ import annotations

import re
from datetime import datetime

from .api import normalize_phone

MIN_PARTY_SIZE = 1
MAX_STANDARD_PARTY_SIZE = 8

_DATE_FORMATS = ("%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%m/%d/%Y")
_TIME_12H = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?$", re.IGNORECASE)
_TIME_24H = re.compile(r"^(\d{1,2}):(\d{2})$")


class ArgumentError(ValueError):
    """Raised with a message written for the LLM to act on, not for the caller to hear."""


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

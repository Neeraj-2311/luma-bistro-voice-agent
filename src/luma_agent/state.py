"""Per-call state held outside the LLM context.

The chat context is the source of truth for *conversation*, but not for facts we
must not get wrong. Slot values live here so that a booking is built from data
the code has seen, and so a handoff can hand a human something structured rather
than asking them to read a transcript.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AvailabilityCheck:
    """A slot the API confirmed as bookable. Gates create_reservation."""

    date: str
    time: str
    party_size: int


@dataclass(frozen=True)
class BookingProposal:
    """Exactly what was read back to the caller, and when.

    `create_reservation` will only write a booking identical to a proposal, and
    only once the caller has spoken again after hearing it. That makes "confirm
    before booking" a property of the code rather than a hope about the prompt.
    """

    name: str
    phone: str
    date: str
    time: str
    party_size: int
    notes: str | None
    read_back_at_turn: int

    def matches(self, name: str, phone: str, date: str, time: str, party_size: int) -> bool:
        return (self.name, self.phone, self.date, self.time, self.party_size) == (
            name,
            phone,
            date,
            time,
            party_size,
        )


@dataclass
class CallState:
    name: str | None = None
    phone: str | None = None
    notes: str | None = None

    # The most recent details read back for confirmation. Replaced, not appended:
    # a correction invalidates whatever was proposed before it.
    proposal: BookingProposal | None = None

    # Set only by a successful availability check. create_reservation refuses to
    # write a slot that is not in here, so the agent physically cannot book a
    # time it never verified.
    verified_slots: list[AvailabilityCheck] = field(default_factory=list)

    # Reservations touched during this call, keyed by confirmation code. Lets the
    # agent answer "what did you book me for?" without another API round trip.
    reservations: dict[str, dict[str, Any]] = field(default_factory=dict)

    handed_off: bool = False

    def record_availability(self, date: str, time: str, party_size: int) -> None:
        check = AvailabilityCheck(date=date, time=time, party_size=party_size)
        if check not in self.verified_slots:
            self.verified_slots.append(check)

    def is_verified(self, date: str, time: str, party_size: int) -> bool:
        return AvailabilityCheck(date=date, time=time, party_size=party_size) in self.verified_slots

    def summary(self) -> str:
        """Human-readable digest handed to a person on escalation."""
        lines = [
            f"Caller name: {self.name or 'not provided'}",
            f"Phone: {self.phone or 'not provided'}",
        ]
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        if self.verified_slots:
            slots = ", ".join(f"{s.date} {s.time} for {s.party_size}" for s in self.verified_slots)
            lines.append(f"Availability checked: {slots}")
        if self.reservations:
            for code, res in self.reservations.items():
                lines.append(
                    f"Reservation {code}: {res.get('date')} {res.get('time')} "
                    f"party of {res.get('party_size')} ({res.get('status')})"
                )
        return "\n".join(lines)

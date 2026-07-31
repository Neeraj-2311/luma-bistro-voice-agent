"""The Luma Bistro reservations agent and its tools.

Tool returns are short natural-language strings rather than JSON. The model reads
them straight into speech, and a sentence like "6:30 PM is full" survives a
paraphrase better than a nested object the model has to interpret first.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date as date_type
from typing import Any

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.llm import ToolError

from . import api as luma_api
from . import rules
from .api import (
    ALREADY_CANCELLED,
    INVALID_SLOT,
    NOT_FOUND,
    SLOT_UNAVAILABLE,
    TEMPORARY,
    LumaAPI,
    LumaAPIError,
)
from .prompts import build_instructions
from .rules import (
    ArgumentError,
    parse_date,
    parse_time,
    validate_confirmation_code,
    validate_name,
    validate_party_size,
    validate_phone,
)
from .state import BookingProposal, CallState

logger = logging.getLogger("luma.agent")

_SYSTEM_DOWN = (
    "The booking system is not responding. Tell the caller you are having trouble reaching it "
    "and offer to connect them to a host."
)


class ReservationAgent(Agent):
    def __init__(
        self,
        api: LumaAPI | None = None,
        state: CallState | None = None,
        today: date_type | None = None,
    ) -> None:
        super().__init__(instructions=build_instructions(today))
        self.api = api or LumaAPI()
        self.state = state or CallState()

    # --- Availability -------------------------------------------------------

    @function_tool()
    async def check_availability(
        self, ctx: RunContext, date: str, time: str, party_size: int
    ) -> str:
        """Check whether a table is open. Always call this before promising a time to the caller.

        Args:
            date: Requested date as YYYY-MM-DD, for example 2026-08-14.
            time: Requested time as 24-hour HH:MM, for example 18:30.
            party_size: Number of guests, 1 to 8.
        """
        date, time, party_size = _clean_slot(date, time, party_size)

        # Answer grid questions before spending a request. A date the restaurant is
        # not taking bookings for must never be reported as an availability result,
        # and must never be answered by suggesting another time on that same date --
        # that sends the caller round a loop where every time fails identically.
        if not rules.is_bookable_date(date):
            return (
                f"We are not taking bookings for {rules.spoken_date(date)} at all. This is not a "
                f"question of availability. {rules.describe_calendar()} "
                "Offer those dates. Do not suggest another time on this date."
            )
        if not rules.is_bookable_time(date, time):
            offered = ", ".join(rules.spoken_time(t) for t in rules.bookable_times(date))
            return (
                f"We do not seat at {rules.spoken_time(time)}. This is not a question of "
                f"availability. On {rules.spoken_date(date)} we seat at {offered}. "
                "Offer those times."
            )

        try:
            result = await self.api.check_availability(date, time, party_size)
        except LumaAPIError as exc:
            if exc.code == INVALID_SLOT:
                return (
                    f"{rules.spoken_time(time)} on {rules.spoken_date(date)} is not on our "
                    f"seating calendar. {rules.describe_calendar()}"
                )
            raise ToolError(_SYSTEM_DOWN) from None

        if result["available"]:
            self.state.record_availability(date, time, party_size)
            return f"{rules.spoken_time(time)} on {date} is open for {party_size}."

        alternatives = result.get("alternatives") or []
        if not alternatives:
            return (
                f"{rules.spoken_time(time)} on {date} is full for {party_size} and nothing else is open "
                "that day. Offer a different date."
            )
        offered = ", ".join(rules.spoken_time(a["time"]) for a in alternatives[:3])
        return (
            f"{rules.spoken_time(time)} on {date} is full for {party_size}. "
            f"These times are open instead: {offered}. Offer these exact times and nothing else."
        )

    @function_tool()
    async def find_available_times(self, ctx: RunContext, date: str, party_size: int) -> str:
        """List every seating time still open on a date.

        Use this whenever the caller asks what is available, or has not named a time yet.
        It is one call instead of guessing times one at a time.

        Args:
            date: Date to look at, as YYYY-MM-DD.
            party_size: Number of guests, 1 to 8.
        """
        try:
            date = parse_date(date)
            party_size = validate_party_size(party_size)
        except ArgumentError as exc:
            raise ToolError(str(exc)) from None

        if not rules.is_bookable_date(date):
            return (
                f"We are not taking bookings for {rules.spoken_date(date)} at all. "
                f"{rules.describe_calendar()} Offer those dates."
            )

        # The API only answers one slot per request, so the day view is assembled
        # here by checking the day's seatings concurrently. This is the single
        # strongest argument for the API exposing a whole-day endpoint.
        times = rules.bookable_times(date)
        results = await asyncio.gather(
            *(self.api.check_availability(date, t, party_size) for t in times),
            return_exceptions=True,
        )

        open_times = []
        reachable = False
        for time, result in zip(times, results, strict=True):
            if isinstance(result, BaseException):
                continue
            reachable = True
            if result["available"]:
                open_times.append(time)
                # Recording every open slot means the caller can pick any of them
                # and the booking gate is already satisfied, with no second lookup.
                self.state.record_availability(date, time, party_size)

        if not reachable:
            raise ToolError(_SYSTEM_DOWN)
        if not open_times:
            others = [d for d in rules.bookable_dates() if d != date]
            return f"Nothing is open on {rules.spoken_date(date)} for {party_size}. " + (
                f"Offer another date: {', '.join(rules.spoken_date(d) for d in others)}."
                if others
                else "There are no other dates open."
            )

        offered = ", ".join(rules.spoken_time(t) for t in open_times)
        return (
            f"On {rules.spoken_date(date)} these times are open for {party_size}: {offered}. "
            "Offer these exact times and nothing else."
        )

    # --- Confirm and create -------------------------------------------------

    @function_tool()
    async def read_back_booking(
        self,
        ctx: RunContext,
        name: str,
        phone: str,
        date: str,
        time: str,
        party_size: int,
        notes: str | None = None,
    ) -> str:
        """Read a NEW booking back to the caller before creating it.

        Only for new reservations. Do not call this when changing or cancelling an
        existing one -- for those, say the change out loud and ask for a yes.

        Call this once you have every detail and the time is confirmed open. Say the
        sentence it returns, then wait. You cannot book without calling this first.

        Args:
            name: Caller's full name.
            phone: Caller's phone number.
            date: Date as YYYY-MM-DD.
            time: Time as 24-hour HH:MM.
            party_size: Number of guests, 1 to 8.
            notes: Any special request, or omit if none.
        """
        date, time, party_size = _clean_slot(date, time, party_size)
        try:
            name = validate_name(name)
            phone = validate_phone(phone)
        except ArgumentError as exc:
            raise ToolError(str(exc)) from None

        if not self.state.is_verified(date, time, party_size):
            return (
                f"You have not checked {rules.spoken_time(time)} on {date} for {party_size}. "
                "Check availability first."
            )

        self.state.proposal = BookingProposal(
            name=name,
            phone=phone,
            date=date,
            time=time,
            party_size=party_size,
            notes=notes,
            read_back_at_turn=_user_turns(ctx),
        )
        logger.info("booking.read_back", extra={"date": date, "time": time, "party": party_size})
        return (
            f"Say this and then wait for an answer: that is {name}, {rules.spoken_phone(phone)}, "
            f"{rules.spoken_date(date)} at {rules.spoken_time(time)}, "
            f"for {party_size} guests"
            + (f", with a note: {notes}" if notes else "")
            + ". Ask if that is correct."
        )

    # --- Create -------------------------------------------------------------

    @function_tool()
    async def create_reservation(
        self,
        ctx: RunContext,
        name: str,
        phone: str,
        date: str,
        time: str,
        party_size: int,
        notes: str | None = None,
    ) -> str:
        """Book the table. Only call this after the caller has verbally confirmed every detail.

        Args:
            name: Caller's full name.
            phone: Caller's phone number, digits only or with a country code.
            date: Date as YYYY-MM-DD.
            time: Time as 24-hour HH:MM.
            party_size: Number of guests, 1 to 8.
            notes: Any seating or dietary request, or omit if none.
        """
        date, time, party_size = _clean_slot(date, time, party_size)
        try:
            name = validate_name(name)
            phone = validate_phone(phone)
        except ArgumentError as exc:
            raise ToolError(str(exc)) from None

        # Refuse to write a slot no tool ever confirmed. This is what stops the
        # model from booking a time it hallucinated as open.
        if not self.state.is_verified(date, time, party_size):
            return (
                f"You have not checked {rules.spoken_time(time)} on {date} for {party_size}. "
                "Call check_availability first, then book only if it is open."
            )

        # Second half of the two-phase commit. The details being written must be the
        # ones the caller heard, and the caller must have spoken since hearing them.
        # Without the turn check the model could read back and book in one breath,
        # which is where a late correction gets silently dropped.
        proposal = self.state.proposal
        if proposal is None or not proposal.matches(name, phone, date, time, party_size):
            return (
                "These are not the details you last read back to the caller. Call "
                "read_back_booking with exactly what you are about to book, and wait for a yes."
            )
        if _user_turns(ctx) <= proposal.read_back_at_turn:
            return (
                "You have not given the caller a chance to answer. Say the read-back and wait "
                "for them to confirm before booking."
            )

        # Second line of defence against a repeated tool call: answer from what we
        # already wrote instead of going back to the API at all.
        fingerprint = luma_api.reservation_fingerprint(name, phone, date, time, party_size)
        for existing in self.state.reservations.values():
            if existing.get("_fingerprint") == fingerprint:
                logger.info(
                    "create.duplicate_suppressed", extra={"code": existing["confirmation_code"]}
                )
                return (
                    f"This booking already exists under {rules.spell(existing['confirmation_code'])}. "
                    "Do not book again. Confirm the existing reservation to the caller."
                )

        try:
            reservation = await self.api.create_reservation(
                name, phone, date, time, party_size, notes
            )
        except LumaAPIError as exc:
            if exc.code == SLOT_UNAVAILABLE:
                return f"{rules.spoken_time(time)} was taken before the booking went through. " + (
                    _offer(exc.alternatives) or "Nothing else is open that day."
                )
            if exc.code == TEMPORARY:
                raise ToolError(_SYSTEM_DOWN) from None
            raise ToolError(
                f"The booking system rejected these details: {exc}. Recheck with the caller."
            ) from None

        self._remember(reservation, fingerprint=fingerprint)
        self.state.name, self.state.phone, self.state.notes = name, phone, notes
        logger.info(
            "reservation.created",
            extra={
                "code": reservation["confirmation_code"],
                "date": date,
                "time": time,
                "party": party_size,
            },
        )
        return (
            f"Booked. Confirmation code {rules.spell(reservation['confirmation_code'])} for {name}, "
            f"{party_size} guests, {date} at {rules.spoken_time(time)}. Give the caller the code."
        )

    # --- Lookup -------------------------------------------------------------

    @function_tool()
    async def find_reservation(
        self, ctx: RunContext, confirmation_code: str | None = None, phone: str | None = None
    ) -> str:
        """Look up an existing reservation. Required before modifying or cancelling one.

        Args:
            confirmation_code: Code like LUMA-4821, if the caller has it.
            phone: The phone number on the booking, if they do not have the code.
        """
        if not confirmation_code and not phone:
            raise ToolError(
                "Ask the caller for their confirmation code or the phone number on the booking."
            )

        try:
            if confirmation_code:
                confirmation_code = validate_confirmation_code(confirmation_code)
            if phone:
                phone = validate_phone(phone)
        except ArgumentError as exc:
            raise ToolError(str(exc)) from None

        try:
            results = await self.api.search_reservations(
                phone=phone, confirmation_code=confirmation_code
            )
        except LumaAPIError:
            raise ToolError(_SYSTEM_DOWN) from None

        active = [r for r in results if r["status"] != "cancelled"]
        if not active:
            if results:
                return "That reservation was already cancelled. Ask if they would like to book a new table."
            return (
                "No reservation found. Offer to search by the other detail, or to book a new table."
            )

        for reservation in active:
            self._remember(reservation)

        if len(active) > 1:
            listed = "; ".join(
                f"{rules.spell(r['confirmation_code'])} on {r['date']} at {rules.spoken_time(r['time'])}"
                for r in active
            )
            return f"Found {len(active)} reservations: {listed}. Ask which one they mean."

        found = active[0]
        return (
            f"Found {rules.spell(found['confirmation_code'])}: {found['name']}, {found['party_size']} guests, "
            f"{found['date']} at {rules.spoken_time(found['time'])}"
            f"{', notes: ' + found['notes'] if found.get('notes') else ''}. "
            "Read this back before changing anything."
        )

    # --- Modify / cancel ----------------------------------------------------

    @function_tool()
    async def modify_reservation(
        self,
        ctx: RunContext,
        confirmation_code: str,
        date: str | None = None,
        time: str | None = None,
        party_size: int | None = None,
        notes: str | None = None,
    ) -> str:
        """Change an existing reservation. Only call after the caller confirms the change out loud.

        Args:
            confirmation_code: Code of the reservation found earlier.
            date: New date as YYYY-MM-DD, or omit to keep it.
            time: New time as 24-hour HH:MM, or omit to keep it.
            party_size: New guest count, or omit to keep it.
            notes: New notes, or omit to keep them.
        """
        reservation = self._resolve(confirmation_code)
        try:
            changes: dict[str, Any] = {
                "date": parse_date(date) if date else None,
                "time": parse_time(time) if time else None,
                "party_size": validate_party_size(party_size) if party_size else None,
                "notes": notes,
            }
        except ArgumentError as exc:
            raise ToolError(str(exc)) from None

        if not any(v is not None for v in changes.values()):
            raise ToolError("No change was specified. Ask the caller what they want changed.")

        try:
            updated = await self.api.update_reservation(reservation["reservation_id"], **changes)
        except LumaAPIError as exc:
            if exc.code == SLOT_UNAVAILABLE:
                return "That new time is not available. " + (
                    _offer(exc.alternatives) or "Nothing else is open that day."
                )
            if exc.code == INVALID_SLOT:
                return f"That is not on our seating calendar. {rules.describe_calendar()}"
            if exc.code == ALREADY_CANCELLED:
                return (
                    "That reservation is already cancelled, so it cannot be changed. "
                    "Offer to book a new table."
                )
            if exc.code == NOT_FOUND:
                raise ToolError("That reservation no longer exists. Look it up again.") from None
            raise ToolError(_SYSTEM_DOWN) from None

        self._remember(updated)
        logger.info("reservation.modified", extra={"code": updated["confirmation_code"], **changes})
        return (
            f"Updated {rules.spell(updated['confirmation_code'])}: {updated['party_size']} guests, "
            f"{updated['date']} at {rules.spoken_time(updated['time'])}. Confirm this back to the caller."
        )

    @function_tool()
    async def cancel_reservation(self, ctx: RunContext, confirmation_code: str) -> str:
        """Cancel a reservation. Only call after the caller explicitly says to cancel it.

        Args:
            confirmation_code: Code of the reservation found earlier.
        """
        reservation = self._resolve(confirmation_code)

        if reservation.get("status") == "cancelled":
            return f"{rules.spell(reservation['confirmation_code'])} was already cancelled. Say so and do not cancel again."

        try:
            cancelled = await self.api.cancel_reservation(reservation["reservation_id"])
        except LumaAPIError as exc:
            if exc.code == NOT_FOUND:
                raise ToolError("That reservation no longer exists.") from None
            raise ToolError(_SYSTEM_DOWN) from None

        self._remember(cancelled)
        logger.info("reservation.cancelled", extra={"code": cancelled["confirmation_code"]})
        return f"Cancelled {rules.spell(cancelled['confirmation_code'])}. Confirm the cancellation to the caller."

    # --- Escalation ---------------------------------------------------------

    @function_tool()
    async def transfer_to_human(self, ctx: RunContext, reason: str) -> str:
        """Hand the call to a human host, passing along everything collected so far.

        Args:
            reason: One short sentence on why a person is needed.
        """
        summary = f"Reason: {reason}\n{self.state.summary()}\n\n{_transcript(ctx)}"

        try:
            handoff = await self.api.create_handoff(
                reason=reason, conversation_summary=summary, customer_phone=self.state.phone
            )
            queued = handoff.get("handoff_id")
        except LumaAPIError:
            # A failed handoff must never look successful to the caller, but the
            # summary is still logged so a human can pick the call up manually.
            logger.error("handoff.queue_failed", extra={"summary": summary})
            queued = None

        self.state.handed_off = True
        logger.info("handoff", extra={"reason": reason, "handoff_id": queued})
        return (
            "A host has the caller's details and is being connected. Tell the caller you are "
            "transferring them now and that they will not need to repeat anything."
        )

    # --- Internals ----------------------------------------------------------

    def _remember(self, reservation: dict[str, Any], fingerprint: str | None = None) -> None:
        stored = dict(reservation)
        if fingerprint:
            stored["_fingerprint"] = fingerprint
        self.state.reservations[reservation["confirmation_code"]] = stored

    def _resolve(self, confirmation_code: str) -> dict[str, Any]:
        """Map a spoken confirmation code to a reservation this call has already looked up.

        Tools take the confirmation code, never the internal reservation id, so the
        model can only act on a booking it actually retrieved.
        """
        try:
            code = validate_confirmation_code(confirmation_code)
        except ArgumentError as exc:
            raise ToolError(str(exc)) from None

        reservation = self.state.reservations.get(code)
        if not reservation:
            raise ToolError(f"{code} has not been looked up yet. Call find_reservation first.")
        return reservation


def _offer(alternatives: list[dict[str, Any]]) -> str:
    """Phrase the API's alternative times, or empty if it gave none."""
    times = ", ".join(rules.spoken_time(a["time"]) for a in alternatives[:3])
    return f"Open instead: {times}. Offer these." if times else ""


def _clean_slot(date: str, time: str, party_size: int) -> tuple[str, str, int]:
    try:
        return parse_date(date), parse_time(time), validate_party_size(party_size)
    except ArgumentError as exc:
        raise ToolError(str(exc)) from None


def _messages(ctx: RunContext) -> list[Any]:
    return [i for i in ctx.session.history.items if i.type == "message"]


def _user_turns(ctx: RunContext) -> int:
    return sum(1 for i in _messages(ctx) if i.role == "user")


def _transcript(ctx: RunContext) -> str:
    lines = [
        f"{i.role}: {i.text_content}"
        for i in _messages(ctx)
        if i.role in ("user", "assistant") and i.text_content
    ]
    return "Transcript:\n" + "\n".join(lines[-20:])

"""Direct tests of the tool layer, with no LLM in the loop.

The scenario suite exercises what the model chooses to do. These cover what the
tools do when called with arguments the model *could* produce — including the
safety gates a well-behaved model never trips, which is exactly why they need
their own coverage.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError

from luma_agent.agent import ReservationAgent
from luma_agent.api import reservation_fingerprint
from luma_agent.state import CallState
from luma_agent.validation import (
    ArgumentError,
    parse_date,
    parse_time,
    validate_confirmation_code,
    validate_party_size,
    validate_phone,
)

TODAY = date(2026, 7, 31)


@pytest.fixture
def agent(api):
    return ReservationAgent(api=api, state=CallState(), today=TODAY)


@pytest.fixture
def ctx():
    """The reservation tools read only `session`; history is empty by default."""
    return SimpleNamespace(session=SimpleNamespace(history=SimpleNamespace(items=[])))


@pytest.fixture
def caller_turns(ctx):
    """Set how many turns the caller has taken, which gates the booking commit."""

    def _set(count: int) -> None:
        ctx.session.history.items = [
            SimpleNamespace(type="message", role="user", text_content=f"turn {i}")
            for i in range(count)
        ]

    return _set


# --- Argument normalization --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("18:30", "18:30"), ("6:30 PM", "18:30"), ("6 PM", "18:00"), ("12:15 am", "00:15")],
)
def test_parse_time_accepts_what_an_llm_actually_sends(raw, expected):
    assert parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["half past six", "25:00", ""])
def test_parse_time_rejects_unusable_values(raw):
    with pytest.raises(ArgumentError):
        parse_time(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2026-08-14", "2026-08-14"), ("August 14 2026", "2026-08-14"), ("08/14/2026", "2026-08-14")],
)
def test_parse_date_accepts_common_formats(raw, expected):
    assert parse_date(raw) == expected


def test_validate_phone_strips_formatting_and_rejects_short_numbers():
    assert validate_phone("(310) 555-0199") == "3105550199"
    with pytest.raises(ArgumentError):
        validate_phone("555-0199")


def test_validate_party_size_routes_oversized_parties_to_a_human():
    assert validate_party_size(8) == 8
    with pytest.raises(ArgumentError, match="transfer_to_human"):
        validate_party_size(12)


def test_validate_confirmation_code_normalizes_spoken_forms():
    assert validate_confirmation_code("luma 4821") == "LUMA-4821"
    assert validate_confirmation_code("LUMA-4821") == "LUMA-4821"
    with pytest.raises(ArgumentError):
        validate_confirmation_code("4821")


def test_idempotency_key_ignores_formatting_but_not_content():
    a = reservation_fingerprint("Jordan Lee", "(310) 555-0199", "2026-08-14", "18:00", 4)
    b = reservation_fingerprint("jordan lee", "3105550199", "2026-08-14", "18:00", 4)
    c = reservation_fingerprint("Jordan Lee", "3105550199", "2026-08-14", "18:00", 2)
    assert a == b, "formatting differences must not create a second reservation"
    assert a != c, "a different party size is a different booking"


# --- Safety gates ------------------------------------------------------------


async def test_out_of_grid_time_is_explained_not_guessed(agent, ctx):
    result = await agent.check_availability(ctx, date="2026-08-14", time="21:00", party_size=2)
    assert "not a question of availability" in result
    assert "5:30 PM" in result and "8:00 PM" in result


# --- Regression: unbookable dates must not look like unavailable times -------
#
# A live call asked for a date outside the booking calendar. Every time on that
# date returned INVALID_SLOT, the agent reported each as "not available" and
# offered another time on the same impossible date, and the caller looped until
# they gave up. A date-level failure must never be answered at the time level.


async def test_unbookable_date_is_not_reported_as_unavailability(agent, ctx):
    result = await agent.check_availability(ctx, date="2026-08-01", time="18:00", party_size=2)

    assert "not taking bookings" in result
    assert "not a question of availability" in result
    assert "Do not suggest another time on this date" in result
    # Naming the dates that *are* open is what breaks the loop.
    for spoken in ("August 14", "August 15", "August 16"):
        assert spoken in result


async def test_unbookable_date_costs_no_api_request(agent, ctx):
    """The grid is static, so a dead date is answered without touching the network."""
    await agent.check_availability(ctx, date="2026-08-01", time="18:00", party_size=2)
    assert not [c for c in agent.api.calls if c.path == "/availability"]


async def test_find_available_times_returns_only_slots_with_room(agent, ctx):
    """2026-08-15 has zero capacity at 19:00 and 19:30; both must be absent."""
    result = await agent.find_available_times(ctx, date="2026-08-15", party_size=2)

    assert "5:30 PM" in result and "6:00 PM" in result
    assert "6:30 PM" in result and "8:00 PM" in result
    assert "7:00 PM" not in result and "7:30 PM" not in result


async def test_find_available_times_pre_verifies_every_slot_it_offers(agent, ctx):
    """Offering a time and then refusing to book it would be a bug in its own right."""
    await agent.find_available_times(ctx, date="2026-08-15", party_size=2)

    assert agent.state.is_verified("2026-08-15", "17:30", 2)
    assert agent.state.is_verified("2026-08-15", "20:00", 2)
    assert not agent.state.is_verified("2026-08-15", "19:00", 2)


async def test_find_available_times_rejects_a_date_outside_the_calendar(agent, ctx):
    result = await agent.find_available_times(ctx, date="2026-08-01", party_size=2)
    assert "not taking bookings" in result
    assert not [c for c in agent.api.calls if c.path == "/availability"]


async def test_fully_booked_date_offers_other_dates_not_other_times(agent, ctx):
    """A date with no room is a different message from a date that does not exist.

    Every slot on 2026-08-16 seats at most 4, so a party of 5 fits nowhere.
    """
    result = await agent.find_available_times(ctx, date="2026-08-16", party_size=5)
    assert "Nothing is open" in result
    assert "August 14" in result and "August 15" in result


async def test_booking_requires_a_prior_availability_check(agent, ctx):
    result = await agent.create_reservation(
        ctx, name="Jordan Lee", phone="3105550199", date="2026-08-14", time="18:00", party_size=4
    )
    assert "check_availability first" in result
    assert not await agent.api.search_reservations(phone="3105550199")


async def test_booking_requires_a_read_back_first(agent, ctx):
    await agent.check_availability(ctx, date="2026-08-14", time="18:00", party_size=4)
    result = await agent.create_reservation(
        ctx, name="Jordan Lee", phone="3105550199", date="2026-08-14", time="18:00", party_size=4
    )
    assert "not the details you last read back" in result
    assert not await agent.api.search_reservations(phone="3105550199")


async def test_booking_cannot_happen_in_the_same_turn_as_the_read_back(agent, ctx):
    """Reading back and booking in one breath gives the caller no chance to correct."""
    await agent.check_availability(ctx, date="2026-08-14", time="18:00", party_size=4)
    await agent.read_back_booking(
        ctx, name="Jordan Lee", phone="3105550199", date="2026-08-14", time="18:00", party_size=4
    )
    result = await agent.create_reservation(
        ctx, name="Jordan Lee", phone="3105550199", date="2026-08-14", time="18:00", party_size=4
    )
    assert "chance to answer" in result
    assert not await agent.api.search_reservations(phone="3105550199")


async def test_a_correction_invalidates_the_earlier_read_back(agent, ctx, caller_turns):
    """The exact bug T3 guards: booking the party size the caller just corrected away from."""
    # Read back a party of two, then the caller corrects to four and it is read back again.
    await agent.check_availability(ctx, date="2026-08-15", time="18:30", party_size=2)
    await agent.read_back_booking(
        ctx, name="Casey Brown", phone="2135550114", date="2026-08-15", time="18:30", party_size=2
    )
    await agent.check_availability(ctx, date="2026-08-15", time="18:30", party_size=4)
    await agent.read_back_booking(
        ctx, name="Casey Brown", phone="2135550114", date="2026-08-15", time="18:30", party_size=4
    )
    caller_turns(5)  # the caller says "confirm"

    stale = await agent.create_reservation(
        ctx, name="Casey Brown", phone="2135550114", date="2026-08-15", time="18:30", party_size=2
    )
    assert "not the details you last read back" in stale
    assert not await agent.api.search_reservations(phone="2135550114")

    booked = await agent.create_reservation(
        ctx, name="Casey Brown", phone="2135550114", date="2026-08-15", time="18:30", party_size=4
    )
    assert "Booked" in booked
    written = await agent.api.search_reservations(phone="2135550114")
    assert [r["party_size"] for r in written] == [4]


async def test_read_back_requires_the_slot_to_be_verified(agent, ctx):
    result = await agent.read_back_booking(
        ctx, name="Jordan Lee", phone="3105550199", date="2026-08-14", time="18:00", party_size=4
    )
    assert "Check availability first" in result
    assert agent.state.proposal is None


async def test_oversized_party_cannot_reach_the_api(agent, ctx):
    with pytest.raises(ToolError, match="transfer_to_human"):
        await agent.check_availability(ctx, date="2026-08-14", time="18:00", party_size=12)


async def test_modify_requires_the_reservation_to_have_been_looked_up(agent, ctx):
    with pytest.raises(ToolError, match="find_reservation first"):
        await agent.modify_reservation(ctx, confirmation_code="LUMA-4821", party_size=4)


async def test_cancel_requires_the_reservation_to_have_been_looked_up(agent, ctx):
    with pytest.raises(ToolError, match="find_reservation first"):
        await agent.cancel_reservation(ctx, confirmation_code="LUMA-4821")


async def test_find_reservation_needs_at_least_one_search_key(agent, ctx):
    with pytest.raises(ToolError, match="confirmation code or the phone number"):
        await agent.find_reservation(ctx)


async def test_cancelled_reservations_are_not_offered_as_active(agent, ctx):
    await agent.find_reservation(ctx, confirmation_code="LUMA-4821")
    await agent.cancel_reservation(ctx, confirmation_code="LUMA-4821")

    fresh = ReservationAgent(api=agent.api, state=CallState(), today=TODAY)
    result = await fresh.find_reservation(ctx, confirmation_code="LUMA-4821")
    assert "already cancelled" in result


async def test_handoff_preserves_collected_details(agent, ctx):
    agent.state.name = "Priya Raman"
    agent.state.phone = "4155550123"
    agent.state.record_availability("2026-08-14", "19:00", 8)

    await agent.transfer_to_human(ctx, reason="Party of twelve")

    assert agent.state.handed_off
    summary = agent.state.summary()
    assert "Priya Raman" in summary
    assert "4155550123" in summary
    assert "2026-08-14 19:00" in summary


async def test_transient_failure_is_retried_once_then_surfaced(agent, ctx):
    """The mock API fails the first /availability call for 2026-08-16."""
    result = await agent.check_availability(ctx, date="2026-08-16", time="18:00", party_size=2)

    attempts = [c for c in agent.api.calls if c.path == "/availability"]
    assert [c.outcome for c in attempts] == ["503", "200"]
    assert "is open" in result

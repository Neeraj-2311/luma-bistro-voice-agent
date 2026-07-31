"""The seven standard scenarios from the starter package.

Each test asserts on three layers, weakest last:
  1. API state  -- what actually got written. Deterministic.
  2. Tool calls -- which tools ran, with which arguments. Deterministic.
  3. Wording    -- judged by an LLM, because "did it offer alternatives rather
                   than invent one" has no exact string to match.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import last_reply
from livekit.agents import mock_tools
from livekit.agents.llm import ToolError

from luma_agent.agent import ReservationAgent

pytestmark = pytest.mark.asyncio


async def test_t1_create_available_reservation(harness, judge_llm):
    """T1: check availability, confirm details, create exactly one reservation."""
    await harness.say("Reserve a table for four on Friday, August 14 at 6 PM.")
    await harness.say("Jordan Lee, 310-555-0199.")
    await harness.say("No notes.")
    result = await harness.say("Yes, confirm.")

    booked = await harness.reservations_for("3105550199")
    assert len(booked) == 1, f"expected exactly one reservation, got {len(booked)}"
    assert booked[0]["date"] == "2026-08-14"
    assert booked[0]["time"] == "18:00"
    assert booked[0]["party_size"] == 4
    assert booked[0]["status"] == "confirmed"

    assert "check_availability" in harness.tool_calls()
    assert harness.writes() == ["create_reservation"], "exactly one write expected"

    await last_reply(result).judge(
        judge_llm, intent="States the booking is confirmed and gives a confirmation code."
    )


async def test_t2_unavailable_time_offers_real_alternatives(harness, judge_llm):
    """T2: 18:30 has zero capacity. The agent must offer API alternatives, not invent any."""
    result = await harness.say("Book four people Friday, August 14 at 6:30 PM.")

    await last_reply(result).judge(
        judge_llm,
        intent=(
            "Says 6:30 PM is unavailable and offers alternative times. Must not claim 6:30 PM "
            "is available. Any times offered must be from 5:30 PM, 6:00 PM, or 7:30 PM."
        ),
    )

    await harness.say("I can do 7:30 PM instead.")
    await harness.say("Taylor Kim, 424-555-0188.")
    await harness.say("Confirm.")

    booked = await harness.reservations_for("4245550188")
    assert len(booked) == 1
    assert booked[0]["time"] == "19:30", "should book the alternative the caller chose"
    assert booked[0]["party_size"] == 4

    # The agent must have verified 19:30 before writing it, not just trusted the
    # alternatives list from the earlier failed check.
    assert harness.state.is_verified("2026-08-14", "19:30", 4)


async def test_t3_correction_uses_final_party_size(harness):
    """T3: the caller corrects party size during confirmation. The write must use the new value."""
    await harness.say("Saturday, August 15 at 6:30 PM for two.")
    await harness.say("Casey Brown, 213-555-0114.")
    await harness.say("Sorry, make that four people.")
    await harness.say("Confirm.")

    booked = await harness.reservations_for("2135550114")
    assert len(booked) == 1, "a correction must not produce a second reservation"
    assert booked[0]["party_size"] == 4, "must use the corrected party size, not the original"
    assert booked[0]["date"] == "2026-08-15"
    assert booked[0]["time"] == "18:30"
    assert harness.writes() == ["create_reservation"]


async def test_t4_modify_existing_reservation(harness, judge_llm):
    """T4: find LUMA-4821, confirm the change, then PATCH it."""
    await harness.say("I need to change reservation LUMA-4821.")
    await harness.say("Move it to 7:30 PM on the same date and make it four people.")
    result = await harness.say("Yes, confirm.")

    assert "find_reservation" in harness.tool_calls(), "must look it up before changing it"
    assert harness.writes() == ["modify_reservation"]

    updated = await harness.reservations_for("+13105550147")
    assert updated[0]["time"] == "19:30"
    assert updated[0]["party_size"] == 4
    assert updated[0]["date"] == "2026-08-14", "date was not asked to change"
    assert updated[0]["status"] == "confirmed"

    await last_reply(result).judge(
        judge_llm,
        intent="States that the reservation is now at 7:30 PM for four guests.",
    )


async def test_t5_cancel_existing_reservation(harness, judge_llm):
    """T5: search, ask for confirmation, cancel exactly once."""
    result = await harness.say("I want to cancel reservation LUMA-4821.")

    assert "find_reservation" in harness.tool_calls()
    assert not harness.writes(), "must not cancel before the caller confirms"
    await last_reply(result).judge(
        judge_llm,
        intent=(
            "Reads the reservation details back, then asks a question seeking agreement before "
            "cancelling. Any wording that asks whether to go ahead counts."
        ),
    )

    await harness.say("Yes, cancel it.")

    assert harness.writes() == ["cancel_reservation"], "cancel exactly once"
    found = await harness.reservations_for("+13105550147")
    assert found[0]["status"] == "cancelled"


async def test_t6_transient_api_failure_recovers_silently(harness, judge_llm):
    """T6: the first /availability call for 2026-08-16 returns 503.

    The retry budget is enforced in the HTTP client, so the model never sees the
    failure and cannot decide to retry a third time or to invent an answer.
    """
    result = await harness.say("Can you check Sunday, August 16 at 6 PM for two?")

    attempts = harness.api_calls("/availability")
    assert [c.outcome for c in attempts] == ["503", "200"], f"expected one retry, got {attempts}"

    await last_reply(result).judge(
        judge_llm, intent="Tells the caller 6 PM on Sunday August 16 is available for two."
    )
    assert not harness.writes(), "checking availability must not book anything"


async def test_t7_repeated_create_never_duplicates(harness):
    """T7: replaying the create call must return the original, not write a second row."""
    await harness.say("Book Friday, August 14 at 8 PM for two.")
    await harness.say("Morgan Reed, 310-555-0166.")
    await harness.say("Confirm.")

    first = await harness.reservations_for("3105550166")
    assert len(first) == 1
    original_code = first[0]["confirmation_code"]

    # Replay the tool call exactly as the model would on a duplicate invocation.
    ctx = _run_context(harness)
    replay = await harness.agent.create_reservation(
        ctx, name="Morgan Reed", phone="310-555-0166", date="2026-08-14", time="20:00", party_size=2
    )

    after = await harness.reservations_for("3105550166")
    assert len(after) == 1, "replayed create must not produce a second reservation"
    assert after[0]["confirmation_code"] == original_code
    assert "already exists" in replay.lower()


# --- Beyond the standard set -------------------------------------------------


async def test_persistent_failure_escalates_to_human(harness, judge_llm):
    """When the booking system stays down, the agent must escalate rather than stall."""
    with mock_tools(
        ReservationAgent,
        {"check_availability": lambda: ToolError("The booking system is not responding.")},
    ):
        await harness.say("Do you have a table for two on Friday, August 14 at 6 PM?")
        result = await harness.say("Please just try again.")

    await last_reply(result).judge(
        judge_llm,
        intent=(
            "Tells the caller they are being connected to a human host. Restating what the caller "
            "asked for is fine; asserting that a table was found or confirmed is not."
        ),
    )
    assert "transfer_to_human" in harness.tool_calls()
    assert not harness.writes()


async def test_large_party_hands_off_with_context(harness):
    """A party over eight is out of scope for self-service and must reach a human with context."""
    await harness.say("I need a table for twelve people on Friday, August 14 at 7 PM.")
    result = await harness.say("My name is Priya Raman, 415-555-0123.")

    assert "transfer_to_human" in harness.tool_calls(), "party of 12 must escalate"
    assert not harness.writes(), "must not attempt to book an oversized party"
    assert harness.state.handed_off

    handoff_calls = harness.api_calls("/handoff")
    assert handoff_calls, "the handoff must be recorded, not just spoken"
    del result


async def test_invalid_slot_is_not_presented_as_availability(harness, judge_llm):
    """21:00 is outside the bookable grid. The API 422s; the caller must hear a real answer."""
    result = await harness.say("A table for two at 9 PM on Friday, August 14 please.")

    await last_reply(result).judge(
        judge_llm,
        intent=(
            "Explains that 9 PM cannot be booked and steers the caller toward an earlier time. "
            "Must not say 9 PM is available and must not say it is booked out."
        ),
    )
    assert not harness.writes()


def _run_context(harness):
    """Stand-in RunContext so a tool can be invoked directly, bypassing the model.

    The reservation tools only read `ctx.session`. A real RunContext additionally
    requires a live SpeechHandle, which exists to schedule speech mid-tool — not
    something these tools do.
    """
    return SimpleNamespace(session=harness.session)

"""Shared fixtures for the scenario suite.

These tests drive the real agent and the real mock API over text instead of
audio. That covers everything above the microphone: tool selection, arguments,
API writes, error recovery, and the wording the caller would hear. Acoustic
behaviour (barge-in timing, endpointing) is not testable this way and is
measured from live sessions instead.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest
from dotenv import load_dotenv
from livekit.agents import AgentSession
from livekit.plugins import openai

from luma_agent.agent import ReservationAgent
from luma_agent.api import LumaAPI
from luma_agent.state import CallState

load_dotenv()

# Pinned so relative dates ("Friday, August 14") resolve identically on every run.
TODAY = date(2026, 7, 31)

API_BASE_URL = os.getenv("LUMA_API_BASE_URL", "http://localhost:8000")

# The agent under test. Judges use a separate, cheaper model so a judgement is
# never made by the same weights that produced the answer.
AGENT_MODEL = os.getenv("EVAL_AGENT_MODEL", "gpt-4.1-mini")
JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", "gpt-4.1-mini")


@pytest.fixture
async def api() -> LumaAPI:
    client = LumaAPI(API_BASE_URL)
    try:
        await client.health()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Mock API not reachable at {API_BASE_URL}: {exc}")
    await client.reset()
    yield client
    await client.aclose()


@pytest.fixture
def judge_llm() -> openai.LLM:
    return openai.LLM(model=JUDGE_MODEL, temperature=0)


@pytest.fixture
async def harness(api: LumaAPI, request):
    """Yields a started AgentSession plus the agent, state, and API it is using.

    On teardown it writes one record per scenario so the report can fill in the
    tool-call and API-latency columns the assessment template asks for.
    """
    state = CallState()
    agent = ReservationAgent(api=api, state=state, today=TODAY)
    async with AgentSession(llm=openai.LLM(model=AGENT_MODEL, temperature=0.2)) as session:
        harness = Harness(session=session, agent=agent, api=api, state=state)
        await session.start(agent)
        try:
            yield harness
        finally:
            _record_scenario(request.node.name, harness)


def _record_scenario(test_name: str, harness: Harness) -> None:
    calls = [c for c in harness.api.calls if c.path != "/admin/reset"]
    durations = sorted(c.duration_ms for c in calls)
    record = {
        "test": test_name,
        "tool_calls": harness.tool_calls(),
        "writes": harness.writes(),
        "reservations_created": len(
            [r for r in harness.state.reservations.values() if r.get("_fingerprint")]
        ),
        "handed_off": harness.state.handed_off,
        "api_requests": len(calls),
        "api_retries": len([c for c in calls if c.attempt > 0]),
        "api_p50_ms": durations[len(durations) // 2] if durations else None,
    }
    path = Path(__file__).resolve().parent.parent / "evals" / "results" / "scenarios.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


class Harness:
    def __init__(
        self, session: AgentSession, agent: ReservationAgent, api: LumaAPI, state: CallState
    ):
        self.session = session
        self.agent = agent
        self.api = api
        self.state = state
        self._events: list = []

    async def say(self, text: str):
        result = await self.session.run(user_input=text)
        # Accumulate from the run result rather than reading session.history, which
        # is not guaranteed to be flushed the instant run() returns.
        self._events.extend(result.events)
        return result

    def tool_calls(self) -> list[str]:
        """Every tool the agent invoked this session, in order."""
        return [e.item.name for e in self._events if e.type == "function_call"]

    def writes(self) -> list[str]:
        write_tools = {"create_reservation", "modify_reservation", "cancel_reservation"}
        return [name for name in self.tool_calls() if name in write_tools]

    async def reservations_for(self, phone: str) -> list[dict]:
        return await self.api.search_reservations(phone=phone)

    def api_calls(self, path: str) -> list:
        return [c for c in self.api.calls if c.path == path]


def last_reply(result):
    """Assertion handle for the final assistant message of a turn.

    `contains_message()` matches the *first* assistant message, which is often a
    filler line spoken before a tool call ("let me look that up"). What the caller
    is actually answered with is the last one.
    """
    indexes = [
        i for i, e in enumerate(result.events) if e.type == "message" and e.item.role == "assistant"
    ]
    assert indexes, "the agent produced no spoken reply this turn"
    return result.expect[indexes[-1]].is_message(role="assistant")

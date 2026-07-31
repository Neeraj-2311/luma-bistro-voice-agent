"""Worker entrypoint: builds the voice pipeline and runs one agent per room."""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    TurnHandlingOptions,
    cli,
    inference,
)
from livekit.plugins import cartesia, deepgram, openai, silero

from .agent import ReservationAgent
from .api import LumaAPI
from .metrics import LatencyTracker
from .prompts import GREETING
from .state import CallState

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("luma")

# Words the caller will say that generic models mis-hear. Deepgram boosts these
# at decode time, which matters most for the confirmation code: "LUMA" is
# otherwise transcribed as "Luna" or "loom a" often enough to break lookups.
KEYTERMS = ["Luma", "Luma Bistro", "LUMA", "reservation", "confirmation code", "party of"]

DEFAULT_VOICE = "f786b574-daa5-4673-aa0c-cbe3e8534c02"


def _prewarm(proc: JobProcess) -> None:
    """Load the VAD weights once per worker process, not once per call."""
    proc.userdata["vad"] = silero.VAD.load()


server = AgentServer(setup_fnc=_prewarm)


def _build_session(vad: silero.VAD) -> AgentSession:
    return AgentSession(
        stt=deepgram.STT(
            model="nova-3",
            language="en-US",
            keyterm=KEYTERMS,
            # Interim results are what make barge-in feel immediate: the agent can
            # react to partial speech instead of waiting for a finalized transcript.
            interim_results=True,
        ),
        llm=openai.LLM(
            model="gpt-4.1-mini",
            # Reservations are a fact-carrying task; sampling variance here shows up
            # as wrong party sizes, not as personality.
            temperature=0.2,
            # Serializing tool calls is a correctness guard: it stops the model from
            # issuing check_availability and create_reservation in the same batch,
            # which would race the availability gate.
            parallel_tool_calls=False,
        ),
        tts=cartesia.TTS(model="sonic-3", voice=os.getenv("CARTESIA_VOICE_ID", DEFAULT_VOICE)),
        vad=vad,
        # Semantic turn detection, rather than a fixed silence timeout. A caller
        # pausing mid-sentence ("my number is three one zero... five five five")
        # should not be treated as finished.
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            interruption={
                # Adaptive mode distinguishes a real interruption from "mm-hm".
                "mode": "adaptive",
                # If the agent stops for a noise that turns out not to be speech,
                # pick the sentence back up rather than leaving dead air.
                "resume_false_interruption": True,
                "false_interruption_timeout": 1.5,
            },
            # Runs the LLM against the in-progress transcript so the first token is
            # already available at end of turn. Tool execution still waits for the
            # turn to be committed, so this buys latency without risking a
            # speculative write.
            preemptive_generation={"enabled": True},
        ),
        # check availability -> book is two steps; three leaves room for one
        # recovery step without letting the model loop on a failing tool.
        max_tool_steps=3,
    )


@server.rtc_session(agent_name="luma-bistro")
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}

    api = LumaAPI()
    state = CallState()
    latency = LatencyTracker(session_id=ctx.room.name)
    session = _build_session(ctx.proc.userdata["vad"])

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent) -> None:
        latency.on_metrics(ev)

    async def _shutdown() -> None:
        logger.info(
            "session.end", extra={"latency": latency.summary(), "handed_off": state.handed_off}
        )
        await api.aclose()

    ctx.add_shutdown_callback(_shutdown)

    await session.start(agent=ReservationAgent(api=api, state=state), room=ctx.room)
    await ctx.connect()

    await session.say(GREETING)


if __name__ == "__main__":
    cli.run_app(server)

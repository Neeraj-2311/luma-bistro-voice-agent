"""Per-turn latency measurement.

The number that matters is end-of-speech to first audio out. The pipeline reports
it in three pieces that arrive as separate events, so they are stitched together
per turn and written once all three land:

    eou_delay   silence -> "the caller is done talking"
    llm_ttft    prompt sent -> first token back
    tts_ttfb    first token -> first audio byte

Their sum is the gap the caller actually hears. Keeping them separate is the point:
a blended number tells you a turn was slow, these tell you which vendor to call.
"""

from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path

from livekit.agents import MetricsCollectedEvent
from livekit.agents.metrics import EOUMetrics, LLMMetrics, TTSMetrics

logger = logging.getLogger("luma.metrics")

# Which metric event fills which field. A turn is complete once all three are in.
_FIELDS = {EOUMetrics: "eou_delay", LLMMetrics: "llm_ttft", TTSMetrics: "tts_ttfb"}
_ATTRS = {EOUMetrics: "end_of_utterance_delay", LLMMetrics: "ttft", TTSMetrics: "ttfb"}


class LatencyTracker:
    def __init__(self, session_id: str, log_dir: str | Path = "logs") -> None:
        self.session_id = session_id
        self._open: dict[str, dict[str, float]] = {}
        self._totals: list[float] = []
        self._path = Path(log_dir) / "latency.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def on_metrics(self, ev: MetricsCollectedEvent) -> None:
        field = _FIELDS.get(type(ev.metrics))
        speech_id = getattr(ev.metrics, "speech_id", None)
        if not field or not speech_id:
            return

        seconds = getattr(ev.metrics, _ATTRS[type(ev.metrics)], None)
        if seconds is None or seconds < 0:
            return

        turn = self._open.setdefault(speech_id, {})
        # A turn with tool calls emits LLM and TTS metrics more than once. The first
        # is the one the caller waits on, so later ones must not overwrite it.
        turn.setdefault(field, round(seconds * 1000, 1))

        if len(turn) == len(_FIELDS):
            self._write(speech_id, self._open.pop(speech_id))

    def _write(self, speech_id: str, turn: dict[str, float]) -> None:
        total = round(sum(turn.values()), 1)
        self._totals.append(total)
        row = {"session_id": self.session_id, "speech_id": speech_id, **turn, "total_ms": total}
        with self._path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        logger.info("turn.latency", extra=row)

    def summary(self) -> dict[str, float | int]:
        if not self._totals:
            return {"turns": 0}
        ordered = sorted(self._totals)
        return {
            "turns": len(ordered),
            "p50_ms": round(statistics.median(ordered), 1),
            "max_ms": ordered[-1],
        }

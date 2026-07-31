"""Per-turn latency measurement.

The number that matters for perceived responsiveness is end-of-speech to first
audio out. The pipeline reports it in three pieces that arrive as separate
events, so they are stitched together per speech id and written once the turn is
complete.

    end_of_utterance_delay  silence -> "the caller is done talking"
    llm_ttft                prompt sent -> first token back
    tts_ttfb                first token -> first audio byte

Their sum is what the caller actually experiences as the gap before a reply.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from livekit.agents import MetricsCollectedEvent
from livekit.agents.metrics import EOUMetrics, LLMMetrics, TTSMetrics

logger = logging.getLogger("luma.metrics")


@dataclass
class TurnLatency:
    speech_id: str
    eou_delay_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ttfb_ms: float | None = None

    @property
    def is_complete(self) -> bool:
        return None not in (self.eou_delay_ms, self.llm_ttft_ms, self.tts_ttfb_ms)

    @property
    def total_ms(self) -> float | None:
        if not self.is_complete:
            return None
        return round(self.eou_delay_ms + self.llm_ttft_ms + self.tts_ttfb_ms, 1)  # type: ignore[operator]


class LatencyTracker:
    """Collects per-turn latency and writes one JSONL row per completed turn."""

    def __init__(self, session_id: str, log_dir: str | Path = "logs") -> None:
        self.session_id = session_id
        self._turns: dict[str, TurnLatency] = defaultdict(lambda: TurnLatency(speech_id=""))
        self._completed: list[TurnLatency] = []
        self._path = Path(log_dir) / "latency.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def on_metrics(self, ev: MetricsCollectedEvent) -> None:
        metric = ev.metrics
        speech_id = getattr(metric, "speech_id", None)
        if not speech_id:
            return

        turn = self._turns[speech_id]
        turn.speech_id = speech_id

        if isinstance(metric, EOUMetrics):
            turn.eou_delay_ms = _ms(metric.end_of_utterance_delay)
        elif isinstance(metric, LLMMetrics):
            # A turn with tool calls emits LLMMetrics more than once. The first is
            # the one the caller waits on, so later inferences must not overwrite it.
            if turn.llm_ttft_ms is None:
                turn.llm_ttft_ms = _ms(metric.ttft)
        elif isinstance(metric, TTSMetrics):
            if turn.tts_ttfb_ms is None:
                turn.tts_ttfb_ms = _ms(metric.ttfb)
        else:
            return

        if turn.is_complete:
            self._flush(turn)

    def _flush(self, turn: TurnLatency) -> None:
        self._completed.append(turn)
        self._turns.pop(turn.speech_id, None)
        row = {"session_id": self.session_id, **asdict(turn), "total_ms": turn.total_ms}
        with self._path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        logger.info("turn.latency", extra=row)

    def summary(self) -> dict[str, float | int]:
        totals = [t.total_ms for t in self._completed if t.total_ms is not None]
        if not totals:
            return {"turns": 0}
        ordered = sorted(totals)
        return {
            "turns": len(ordered),
            "p50_ms": round(statistics.median(ordered), 1),
            "p95_ms": round(ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)], 1),
            "max_ms": ordered[-1],
        }


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None or seconds < 0 else round(seconds * 1000, 1)

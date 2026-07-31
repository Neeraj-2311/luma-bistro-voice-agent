"""Decompose LLM time-to-first-token into transport, prefill, and model cost.

The measured p50 for a turn is higher than this pipeline should reach, and
"the LLM is slow" is not an actionable finding. This isolates where the time
actually goes by holding everything constant except one variable at a time:

    tiny prompt, no tools   -> transport floor: what a round trip costs at all
    + system prompt         -> cost of the instructions
    + tool schemas          -> cost of the tool definitions
    + prompt_cache_key      -> whether explicit cache routing helps
    smaller model           -> whether model size is the constraint

Run it from wherever the agent is deployed; the answer is location-dependent.

    uv run python -m evals.latency_probe
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import date

import openai
from dotenv import load_dotenv

from luma_agent.prompts import build_instructions

load_dotenv()

SAMPLES = 12
MODEL = "gpt-4.1-mini"
SMALLER_MODEL = "gpt-4.1-nano"

# A stand-in for the agent's eight tools, sized like the real schemas.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": f"tool_{i}",
            "description": "x" * 220,
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "y" * 60},
                    "time": {"type": "string", "description": "y" * 60},
                    "party_size": {"type": "integer", "description": "y" * 60},
                },
            },
        },
    }
    for i in range(8)
]


async def measure(
    client: openai.AsyncOpenAI,
    label: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str = MODEL,
    cache_key: str | None = None,
) -> None:
    times: list[float] = []
    cached = prompt_tokens = 0
    extra = {"prompt_cache_key": cache_key} if cache_key else {}

    for _ in range(SAMPLES):
        started = time.perf_counter()
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools or openai.NOT_GIVEN,
            stream=True,
            max_completion_tokens=16,
            stream_options={"include_usage": True},
            **extra,
        )
        first: float | None = None
        async for chunk in stream:
            if first is None and chunk.choices:
                first = (time.perf_counter() - started) * 1000
            if chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens
                details = chunk.usage.prompt_tokens_details
                cached = getattr(details, "cached_tokens", 0) if details else 0
        if first is not None:
            times.append(first)

    ordered = sorted(times)
    print(
        f"  {label:<30} p50 {round(statistics.median(ordered)):>5} ms   "
        f"min {round(ordered[0]):>4}   "
        f"input {prompt_tokens:>5} tok, {cached} cached"
    )


async def main() -> None:
    system = build_instructions(date(2026, 8, 1))
    conversation = [
        {"role": "system", "content": system},
        {"role": "user", "content": "table for two Friday at six"},
    ]

    async with openai.AsyncOpenAI() as client:
        print(f"\nLLM time-to-first-token, {SAMPLES} samples each\n")
        await measure(client, "tiny prompt, no tools", [{"role": "user", "content": "hi"}])
        await measure(client, "system prompt only", conversation)
        await measure(client, "system prompt + 8 tools", conversation, TOOLS)
        await measure(client, "+ explicit cache key", conversation, TOOLS, cache_key="luma-v1")
        await measure(
            client, f"{SMALLER_MODEL} + 8 tools", conversation, TOOLS, model=SMALLER_MODEL
        )
        print(
            "\n  The gap between the first row and the rest is prefill; the first row\n"
            "  itself is transport, and is the part you fix by moving the worker.\n"
        )


if __name__ == "__main__":
    asyncio.run(main())

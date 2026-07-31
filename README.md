# Luma Bistro — Real-time Voice Reservation Agent

A browser-based voice agent that takes, changes, and cancels restaurant reservations
over WebRTC. Built on LiveKit Agents with a cascaded speech pipeline.

- **Agent:** Python, [LiveKit Agents 1.6](https://docs.livekit.io/agents/)
- **STT:** Deepgram `nova-3` (streaming, interim results)
- **LLM:** OpenAI `gpt-4.1-mini`
- **TTS:** Cartesia `sonic-3` (streaming)
- **Turn detection:** LiveKit semantic turn detector + Silero VAD
- **Transport:** LiveKit Cloud WebRTC, Next.js frontend
- **Backend:** the starter package's FastAPI mock API, unmodified

---

## Quick start

```bash
# 1. Dependencies
uv sync
cd web && npm install && cd ..

# 2. Secrets — fill in the four provider keys
cp .env.example .env

# 3. Mock reservation API (terminal 1)
cd starter && docker compose up --build      # or: uv run uvicorn app:app --app-dir starter

# 4. Agent worker (terminal 2)
uv run python -m luma_agent.main dev

# 5. Browser frontend (terminal 3)
cd web && npm run dev                        # http://localhost:3000
```

Click **Call the restaurant**, allow the microphone, and talk.

Run the scenario suite:

```bash
uv run python -m evals.report      # runs pytest, writes EVALUATION_RESULTS.md
```

---

New to the code? Start with **[CODE_WALKTHROUGH.md](CODE_WALKTHROUGH.md)** — every
file in one sentence, and every design decision with its reasoning.

## Architecture

```
Browser ──WebRTC──> LiveKit Cloud ──> Agent worker (Python)
                                        │
                       ┌────────────────┼────────────────┐
                       ▼                ▼                ▼
                  Deepgram STT     OpenAI LLM      Cartesia TTS
                   (streaming)     (tool calls)     (streaming)
                                        │
                                        ▼
                              tools ──> HTTP client ──> Mock reservation API
                                        (retry, idempotency, typed errors)
```

| Module | Responsibility |
|---|---|
| `src/luma_agent/main.py` | Builds the voice pipeline, starts one agent per call |
| `src/luma_agent/agent.py` | The eight tools — everything the agent can do |
| `src/luma_agent/api.py` | Reservation API client: retries, timeouts, idempotency |
| `src/luma_agent/rules.py` | What inputs are legal, and which dates/times exist |
| `src/luma_agent/state.py` | Per-call memory: collected details, verified slots, read-back |
| `src/luma_agent/prompts.py` | What the agent is told |
| `src/luma_agent/metrics.py` | Per-turn latency, split by stage |
| `tests/test_standard_scenarios.py` | T1–T7 plus three failure cases, driving the real agent |
| `tests/test_tool_layer.py` | Argument validation and safety gates, no LLM in the loop |

---

## Major decisions

### Cascaded pipeline, not a speech-to-speech model

A realtime model would shave roughly 200–300 ms off each turn. I chose the cascaded
path anyway, because this task is dominated by tool correctness rather than prosody:

- **Tool calling is more reliable** on a text LLM than on current realtime models,
  and a wrong `party_size` is a worse failure than a slightly slower reply.
- **Per-stage latency is measurable.** The assessment asks for latency numbers;
  a cascaded pipeline reports EOU delay, LLM TTFT, and TTS TTFB separately, so a
  regression can be attributed to a stage instead of guessed at.
- **Every stage is swappable and independently testable.** The scenario suite drives
  the exact same agent and tools over text, with no audio involved.
- **Cost** is roughly an order of magnitude lower per minute.

### Duplicate prevention is structural, not prompted

`POST /reservations` needs an `Idempotency-Key`. Rather than generating a UUID per
call, the key is a SHA-256 of the booking's identity — name, phone, date, time, party
size (`api.reservation_fingerprint`). The LLM never sees it.

This means a duplicate write is impossible by construction, not merely unlikely: if
the model calls `create_reservation` twice — a retry, a garbled confirmation, a
repeated tool call — both requests carry the same key and the API returns the
original reservation. A second, in-process guard short-circuits the repeat before it
even reaches the network.

It also makes `POST` safe to retry, which is why the HTTP client retries writes at all.

### The agent cannot book a slot it never checked

`create_reservation` refuses to write unless `check_availability` previously returned
"open" for that exact date, time, and party size (`state.verified_slots`). "Do not
invent availability" is therefore enforced in code rather than requested in a prompt.
The same idea guards modify and cancel: those tools take a **confirmation code**, never
an internal reservation id, and resolve it against reservations this call actually
looked up — so the model cannot act on a booking it has not retrieved.

### Confirmation is a two-phase commit

Booking is split across two tools. `read_back_booking` records exactly what was recited
to the caller and on which turn; `create_reservation` refuses to write anything that
does not match that proposal, and refuses to write at all until the caller has spoken
again since hearing it.

This started as a prompt rule and a turn-count heuristic, and it failed about one run in
six: the model would occasionally read the details back and book them in the same breath,
so a correction on the next turn arrived after the write. Making the read-back a recorded
artifact rather than a hoped-for behaviour turned "confirm before booking" into something
the code guarantees. A correction now invalidates the previous proposal by construction.

### Nothing is written in the same breath as the caller's words

The same rule covers all three writes, in code rather than in the prompt. A booking
cannot be created unless it matches a read-back the caller has since responded to, and
a reservation cannot be modified or cancelled in the same turn it was looked up. In
every case the caller must have spoken *after* hearing what is about to happen.

Silence gets the same treatment. A prompt rule cannot handle a caller going quiet,
because silence produces no turn for the model to respond to — so the session reports
the caller as away after 12 seconds and `main.py` drives the recovery directly: check in
once, then close the call politely rather than holding a dead line open.

### "That date is closed" and "that time is full" are different failures

The mock API only holds three dates, and any other date 422s per-slot. The first version
of this agent treated that as an availability answer: it told the caller the time was
unavailable and offered a different time *on the same impossible date*, which produced an
endless loop and, worse, a fabricated shrinking list of "available" times assembled from
accumulated failures.

`rules.py` now owns the booking calendar — which dates and times exist at all — separately
from availability, which only the API can answer. A date outside the calendar is rejected
before any request is sent, with a message that names the dates that are open and
explicitly forbids suggesting another time. The distinction is load-bearing: conflating
the two is how an agent ends up inventing availability.

### The day view is assembled client-side

Callers ask "what do you have on Saturday?" far more often than they name an exact time,
but `GET /availability` answers one slot per request and the starter API is fixed.
`find_available_times` fans out across that day's seatings concurrently and returns only
the times with room, pre-verifying each so the caller can pick any of them without a
second lookup. It costs six parallel requests where one should do, which is the clearest
argument for the API change described below.

### Retries are enforced in the HTTP client, not by the model

The mock API fails the first `/availability` call for 2026-08-16 with a 503. The client
retries once, after 500 ms, and the model never learns it happened.

Two reasons. First, "at most one retry" becomes a guarantee instead of something a
prompt asks for and a model may or may not honour. Second, a blip that clears in half a
second should not cost the caller an audible "let me try that again". When both attempts
fail, the tool raises and the agent tells the caller and offers a human — it never
fabricates a result. Only 5xx, 429, and transport errors retry; 4xx never does, because
a bad argument will not fix itself.

### Preemptive generation is safe here

The session runs the LLM against the in-progress transcript so the first token is ready
at end of turn. That would be dangerous for a booking agent if it also executed tools
speculatively — but in the SDK, `perform_tool_executions` runs *after* the
`_wait_for_scheduled()` gate, so a preemptively generated turn produces the tool-call
decision early and executes it only once the turn is committed. Latency win, no risk of
a speculative write.

`parallel_tool_calls` is disabled for a related reason: batching `check_availability`
and `create_reservation` into one response would race the availability gate.

### Handoff preserves the call

`transfer_to_human` posts to `/handoff` with the structured details collected so far
(name, phone, notes, every slot checked, every reservation touched) plus the last 20
transcript turns. If the handoff endpoint itself fails, the summary is written to the
error log rather than silently dropped, and the caller is never told a transfer
succeeded when it did not.

---

## Testing

`tests/` drives the real agent against the real mock API over text. Each scenario
asserts in three layers, weakest last:

1. **API state** — what actually got written. Fully deterministic.
2. **Tool calls** — which tools ran, in what order. Fully deterministic.
3. **Wording** — LLM-judged, because "offered real alternatives rather than inventing
   one" has no exact string to match.

Most assertions are in the first two layers on purpose; a suite that only judges
transcripts passes while writing the wrong row to the database.

Beyond the seven standard scenarios, the suite covers a persistent API outage
(escalates instead of stalling), a party of twelve (hands off with context), and an
out-of-grid time (explains it instead of claiming availability).

See [EVALUATION_RESULTS.md](EVALUATION_RESULTS.md) for the current run: 10/10 scenarios
and 33/33 tool-layer tests, with zero duplicate writes.

### On the latency numbers

Measured p50 end-of-speech to first audio is ~1.8 s, which is higher than the ~900 ms
this pipeline should reach. The breakdown localizes it: TTS time-to-first-byte is 154 ms
and end-of-utterance detection is 606 ms, but **LLM time-to-first-token is ~850 ms** and
dominates. Two contributors, in order of size: these runs were made from India against
OpenAI's US endpoints, and the system prompt plus seven tool definitions is a large
prefix to process on every turn. The fixes are regional colocation and prompt caching,
both described under Scaling — neither is a redesign. Being able to say which stage owns
the number, rather than guessing, is the practical argument for the cascaded pipeline.

---

## Known limitations

- **Barge-in is not covered by the automated suite.** Text-mode tests cannot exercise
  acoustic behaviour. T3 asserts the *semantics* of a mid-flow correction (the final
  party size is what gets written); the acoustic side is configured
  (`interruption.mode="adaptive"`, false-interruption resume) and shown in the demo.
- **State is in memory.** `CallState` lives in the worker process, so a crash mid-call
  loses collected details. See scaling below.
- **Booking grid is narrow, and the agent learns it from seed data.** The mock API
  accepts six times across three dates and does not expose that grid over HTTP, so
  `rules.py` reads it from the fixed `starter/seed_data.json`. In production this
  would be a `GET /schedule` call; the coupling is a workaround for a missing endpoint,
  not a design preference.
- **No caller authentication.** Anyone with a confirmation code can cancel a booking.
  Real deployments need at least a phone-number match.
- **English only.** `nova-3` is configured for `en-US`; multilingual is a config change,
  not a redesign.
- **Cost and latency are untuned for scale.** Single worker, no connection pooling
  across sessions, no response caching.

---

## What I would change about the supplied API

- **`GET /availability` needs a whole-day variant, and the grid needs an endpoint.**
  This is the single most consequential gap. Checking one slot at a time forces a round
  trip per candidate time — `find_available_times` issues six concurrent requests to
  answer one ordinary question. Worse, there is no way to learn which dates and times
  exist at all except by probing and collecting 422s, and an agent that cannot tell
  "closed" from "full" will eventually tell a caller something untrue.
- `PATCH /reservations/{id}` is not idempotent, unlike `POST`. A retried modify can
  double-apply against a concurrent change; it should accept an `If-Match` / version.
- Errors return codes but not remediation. `INVALID_SLOT` could include the valid times
  for that date, which would remove an entire class of caller-facing dead ends.
- Search by phone returns cancelled reservations mixed in with active ones; the client
  filters them.

---

## Scaling

**10 concurrent calls.** What is here, deployed. One worker process handles multiple
sessions; LiveKit Cloud handles media. Move `CallState` to Redis keyed by room so a
worker restart does not lose a call.

**100 concurrent calls.** Horizontal workers behind LiveKit's job dispatch, which
already load-balances across registered workers — `num_idle_processes` keeps warm
processes so no caller waits on a cold model load. Provider rate limits become the
binding constraint before CPU does: Deepgram and Cartesia both need raised quotas, and
the LLM needs either provisioned throughput or a fallback model on 429. Add per-session
tracing (OpenTelemetry, already supported by the SDK) because grepping logs stops
working at this point.

**1,000 concurrent calls.** Regional worker pools pinned near the media edge — a
cross-region hop adds 80–150 ms to every turn and is the single largest avoidable
latency. Batch and cache the reservation API behind a read replica for availability
lookups, since availability reads dominate writes maybe 10:1. Warm-pool TTS
connections. At this scale the economics favour re-evaluating a realtime model for the
conversational parts while keeping the cascaded path for tool-heavy turns, but that is
a measured decision, not an assumption.

**Cost per five-minute call** (rough, list prices): STT ~$0.03, LLM ~$0.01 at
~15k tokens with caching, TTS ~$0.05 at ~1,800 characters, LiveKit media ~$0.01.
About **$0.10 per call**, dominated by TTS. A realtime model would be roughly $0.60–0.80
for the same call.

---

## Security notes

Phone numbers and names are PII. In this assessment build they appear in logs; a real
deployment would redact them at the logging layer, keep transcripts encrypted with a
short retention window, disable recordings by default, and keep provider keys in a
secret manager rather than a `.env` file. LiveKit tokens are minted server-side by the
Next.js route handler and scoped to a single room.

---

## AI assistance disclosure

This project was built with Claude (Anthropic) as a coding assistant, used for
scaffolding, SDK research against current LiveKit documentation, and drafting. All
architecture decisions, the tool design, the safety gates, and the evaluation strategy
are mine, and I can explain and modify any part of the submitted code.

# Luma Bistro — Real-time Voice Reservation Agent

A browser voice agent that books, changes, and cancels restaurant tables over WebRTC.

| | |
|---|---|
| Agent | Python, [LiveKit Agents 1.6](https://docs.livekit.io/agents/) |
| STT | Deepgram `nova-3`, streaming |
| LLM | OpenAI `gpt-4.1-mini` |
| TTS | Cartesia `sonic-3`, streaming |
| Turn detection | LiveKit semantic turn detector + Silero VAD |
| Transport | LiveKit Cloud WebRTC, Next.js frontend |
| Backend | the starter's FastAPI mock API, unmodified |

Also here: [EVALUATION_RESULTS.md](EVALUATION_RESULTS.md) for scenario results and measured
latency, [ARCHITECTURE.md](ARCHITECTURE.md) for the twelve questions in the starter package.

---

## Quick start

**Prerequisites**

- [uv](https://docs.astral.sh/uv/getting-started/installation/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Node 20 or newer
- Accounts with LiveKit Cloud, Deepgram, OpenAI, Cartesia. Free tiers cover this.

`uv sync` creates `.venv`, and every Python command below starts with `uv run`, which uses
it automatically. There is no virtualenv to activate in any terminal. The frontend terminal
is Node only. If you prefer, `source .venv/bin/activate` lets you drop the prefix.

```bash
# 1. Install
uv sync                      # creates .venv from uv.lock
npm --prefix web install

# 2. Credentials. Two files: the agent and the frontend are separate processes.
cp .env.example .env                 # LiveKit + Deepgram + OpenAI + Cartesia
cp web/.env.example web/.env.local   # the same LiveKit values
```

The frontend reuses the agent's LiveKit URL, key, and secret. It also needs
`AGENT_NAME=luma-bistro`, matching the name the worker registers under.

```bash
# 3. Mock reservation API                        (terminal 1)
uv run uvicorn app:app --app-dir starter --port 8000
#    or the starter's container:                 cd starter && docker compose up --build

# 4. Agent worker                                (terminal 2)
uv run python -m luma_agent.main dev

# 5. Frontend                                    (terminal 3)
npm --prefix web run dev                         # http://localhost:3000
```

Terminal 2 should print `registered worker` with `"agent_name": "luma-bistro"`. Without
that line the page loads but nobody answers.

Open <http://localhost:3000>, click **Call the restaurant**, allow the mic, and talk.

`uv run python -m luma_agent.main console` gives you a voice session in the terminal with
no browser. Ending a call is a no-op there, since the SDK skips room deletion in console
mode, so use the browser to test that path.

Tests and the latency breakdown:

```bash
uv run python -m evals.report         # runs pytest, writes EVALUATION_RESULTS.md
uv run python -m evals.latency_probe  # decomposes LLM time-to-first-token
```

---

## Architecture

### One turn, end to end

```
  Caller speaks
       │  Opus 48 kHz over WebRTC
       ▼
  LiveKit edge (nearest the caller)
       │
       ▼
  ┌──────────────── Agent worker, one process per call ────────────────┐
  │                                                                    │
  │  1  VAD            Silero, local     speech or silence?            │
  │  2  STT            Deepgram nova-3   partial text as they talk     │
  │  3  Turn detector  LiveKit semantic  finished, or just pausing?    │
  │  4  LLM            gpt-4.1-mini      answer, or call a tool?       │
  │  5  Tools          validate → gate → HTTP → reservation API        │
  │  6  LLM            turn the result into something speakable        │
  │  7  TTS            Cartesia sonic-3  audio streams as tokens land  │
  │                                                                    │
  └────────────────────────────┬───────────────────────────────────────┘
                               ▼
                      LiveKit edge → caller hears the reply
```

**VAD** runs locally, so it costs nothing per call. It also notices the caller talking over
the agent.

**STT** streams interim results, so text arrives while the caller is still speaking. That is
what lets the next two stages start early.

**Turn detection** is the piece that makes this feel human. A silence timer cuts people off
mid-sentence ("my number is three one zero… five five five"). A semantic detector reads the
transcript and the acoustics together and waits for a real ending.

**LLM** is the only stage that decides anything. Tool calls are serialized
(`parallel_tool_calls=False`) so it cannot check availability and book in one breath.
Generation starts on the in-progress transcript, but tool execution waits for the turn to
commit, so nothing is written speculatively.

**Tools** normalize and range-check arguments, run the safety gates, then hand off to one
HTTP client that owns retries and idempotency. They return sentences, not JSON, because the
model reads them straight into speech.

**TTS** streams chunk by chunk, so the caller hears the first word while the last is still
being written.

**On interruption**, VAD fires mid-reply. The LLM and TTS streams cancel, playback stops,
and the assistant's message is truncated in the chat context to only the words the caller
actually heard. Skip that last part and the next turn is built on a sentence they never got.

The three latency numbers in [EVALUATION_RESULTS.md](EVALUATION_RESULTS.md) are measured at
steps 3, 4, and 7. Their sum is the gap the caller hears.

### Modules

| Module | Responsibility |
|---|---|
| `src/luma_agent/main.py` | Builds the pipeline, starts one agent per call |
| `src/luma_agent/agent.py` | The nine tools |
| `src/luma_agent/api.py` | Reservation API client: retries, timeouts, idempotency |
| `src/luma_agent/rules.py` | Legal inputs, and which dates and times exist |
| `src/luma_agent/state.py` | Per-call memory: details, verified slots, read-back |
| `src/luma_agent/prompts.py` | What the agent is told |
| `src/luma_agent/metrics.py` | Per-turn latency, split by stage |
| `tests/test_standard_scenarios.py` | T1–T7 plus three failure cases, real agent |
| `tests/test_tool_layer.py` | Validation and safety gates, no LLM |

---

## Major decisions

### Cascaded pipeline over speech-to-speech

A realtime model saves roughly 200–300 ms per turn. This task is dominated by tool
correctness, not prosody, so I took the trade:

- Tool calling is more reliable on a text LLM. A wrong `party_size` is worse than a slower
  reply.
- Per-stage latency is measurable, so a regression can be attributed instead of guessed at.
- Every stage is swappable and testable. The scenario suite drives the same agent over text.
- Roughly an order of magnitude cheaper per minute.

### Duplicate writes are impossible, not unlikely

`POST /reservations` needs an `Idempotency-Key`. Instead of a UUID per call, the key is a
SHA-256 of the booking itself: name, phone, date, time, party size
(`api.reservation_fingerprint`). The LLM never sees it.

Two identical create attempts therefore carry the same key, and the API returns the original
row. A retry, a garbled confirmation, a repeated tool call all collapse to one reservation.
An in-process guard catches the repeat before it even reaches the network.

This is also why the HTTP client is willing to retry a `POST` at all.

### The agent cannot book a slot it never checked

`create_reservation` refuses to write unless `check_availability` already returned "open"
for that exact date, time, and party size. "Do not invent availability" is enforced in code,
not asked for in a prompt.

Modify and cancel take a confirmation code, never an internal reservation id, and resolve it
against bookings this call actually looked up. The model cannot touch something it never
retrieved.

### Nothing is written in the same breath as the caller's words

`read_back_booking` records what was recited and on which turn. `create_reservation` refuses
anything that does not match, and refuses until the caller has spoken since. Modify and
cancel are gated the same way: not in the same turn as the lookup.

This started as a prompt rule with a turn-count check and failed about one run in six. The
model would read the details back and book them in one breath, so a correction on the next
turn landed after the write. Making the read-back a recorded artifact turned it into
something the code guarantees.

Silence gets handled in `main.py` rather than the prompt, because silence produces no turn
for the model to react to. The session reports the caller away after 12 seconds: one
check-in, then goodbye and hang up.

### Ending a call

Three paths close a call: the caller is done (`end_call`), they are handed to a person
(`transfer_to_human`), or they have gone quiet twice. All three run through one helper that
waits for the farewell to finish playing before closing the room. Return the sign-off as
text instead and it gets cut off mid-word, because the room is already gone.

A transfer that leaves the agent on the line is not a transfer. An abandoned call that never
closes holds a concurrent session and keeps billing three providers for silence.

### "That date is closed" and "that time is full" are different answers

The mock API holds three dates. Any other date 422s per slot. The first version treated that
as an availability answer, told the caller the time was unavailable, and offered a different
time on the same impossible date. The result was an endless loop and a fabricated shrinking
list of "available" times assembled from the failures.

`rules.py` now owns the booking calendar, which dates and times exist at all, separately
from availability, which only the API can answer. A date outside the calendar is rejected
before any request goes out, and the message names the dates that are open. Conflating those
two is how an agent ends up inventing availability.

### The day view is built client-side

Callers ask "what do you have on Saturday?" more often than they name a time, but
`GET /availability` answers one slot per request and the starter API is fixed.
`find_available_times` checks that day's seatings concurrently and returns only the ones with
room, pre-verifying each so the caller can pick any without a second lookup. Six parallel
requests to answer one ordinary question, which is the clearest argument for the API change
noted below.

### Retries live in the HTTP client

The mock API fails the first `/availability` call for 2026-08-16 with a 503. The client
retries once after 500 ms and the model never learns it happened.

Two reasons. "At most one retry" becomes a guarantee rather than something a prompt asks for.
And a blip that clears in half a second should not cost the caller an audible "let me try
that again". When both attempts fail the tool raises, and the agent says so and offers a
human. It never fabricates a result.

Only 5xx, 429, and transport errors retry. A 4xx will not fix itself.

### Preemptive generation is safe here

The session runs the LLM against the in-progress transcript so the first token is ready at
end of turn. That would be dangerous if it also executed tools speculatively, but in the SDK
`perform_tool_executions` runs after the `_wait_for_scheduled()` gate. The tool-call decision
arrives early; execution waits for the turn to commit.

---

## Testing

Two suites. `tests/test_standard_scenarios.py` drives the real agent against the real mock
API over text, asserting in three layers, weakest last:

1. **API state.** What actually got written. Deterministic.
2. **Tool calls.** Which ran, in what order. Deterministic.
3. **Wording.** LLM-judged, because "offered real alternatives instead of inventing one" has
   no exact string to match.

Most assertions sit in the first two. A suite that only judges transcripts will pass while
writing the wrong row to the database.

`tests/test_tool_layer.py` calls tools directly with no LLM, covering the safety gates a
well-behaved model never trips. It runs in about a second.

Beyond the seven standard scenarios: a persistent API outage (escalates instead of stalling),
a party of twelve (hands off with context), and an out-of-grid time (explains it instead of
claiming availability).

Current run: 10/10 scenarios, 37/37 tool-layer tests, zero duplicate writes.

---

## Latency, and what would fix it

Measured p50 from end of speech to first audio is **~1,800 ms**, against the ~900 ms this
pipeline should reach. The per-stage split says which component owns it:

| Stage | p50 |
|---|---:|
| End-of-utterance detection | 606 ms |
| **LLM time to first token** | **854 ms** |
| TTS time to first byte | 154 ms |

TTS is fine. The LLM dominates, so I measured why instead of guessing.
`uv run python -m evals.latency_probe` holds everything constant and varies one thing:

| Configuration | p50 TTFT | Input tokens | Cached |
|---|---:|---:|---:|
| 8-token prompt, no tools | **635 ms** | 8 | 0 |
| + full system prompt | 686 ms | 1,244 | 1,024 |
| + 8 tool schemas | 829 ms | 2,104 | 2,048 |
| + explicit `prompt_cache_key` | 749 ms | 2,104 | 2,048 |
| `gpt-4.1-nano` instead | 625 ms | 2,104 | 2,048 |

An 8-token prompt with no tools still takes 635 ms. That is ~75% of the total and has
nothing to do with this codebase. It is the round trip from India to OpenAI's US origin.
ICMP to `api.openai.com` is 7 ms and TLS completes in 28 ms, so the request reaches a nearby
edge instantly and then spends most of a second being backhauled.

Three plausible guesses this rules out:

- **Prompt size.** The full 1,244-token system prompt adds ~50 ms.
- **Prompt caching.** Already on: 2,048 of 2,104 tokens come from cache. An explicit
  `prompt_cache_key` moves the number less than run-to-run noise.
- **Tool schemas.** ~140 ms, real but second-order, and buying it back means shortening the
  descriptions that keep tool calling reliable.

The fix is geography. A worker in a US region collapses the 635 ms floor to ~50 ms and takes
the turn to roughly 900 ms, while LiveKit still terminates the caller's media at an edge near
them. Media close to the user, inference close to the model.

`gpt-4.1-nano` is a genuine 200 ms saving and I did not take it. This workload is scored on
tool-calling reliability, and the brief prefers a smaller reliable system to a faster fragile
one. Worth revisiting behind an eval showing nano holds up.

---

## Known limitations

- **Barge-in is not in the automated suite.** Text-mode tests cannot exercise acoustics. T3
  asserts the semantics of a mid-flow correction, so the final party size is what gets
  written. The acoustic side is configuration plus the demo.
- **State is in memory.** `CallState` lives in the worker process, so a crash mid-call loses
  collected details.
- **The booking calendar comes from seed data.** The mock API accepts six times across three
  dates and does not expose that grid over HTTP, so `rules.py` reads
  `starter/seed_data.json`. In production this would be a `GET /schedule` call. It is a
  workaround for a missing endpoint, not a preference.
- **No caller authentication.** Anyone with a confirmation code can cancel a booking. Real
  deployments need at least a phone-number match.
- **English only.** `nova-3` is set to `en-US`. Multilingual is config, not redesign.
- **Untuned for scale.** Single worker, no connection pooling, no response caching.

---

## What I would change about the supplied API

- **`GET /availability` needs a whole-day variant, and the grid needs an endpoint.** The
  biggest gap. One slot per request means a round trip per candidate time, and there is no
  way to learn which dates and times exist except by probing and collecting 422s. An agent
  that cannot tell "closed" from "full" will eventually tell a caller something untrue.
- **`PATCH /reservations/{id}` is not idempotent** the way `POST` is. A retried modify can
  double-apply against a concurrent change. It should take an `If-Match` or a version.
- **Errors carry codes but no remedy.** `INVALID_SLOT` knows the valid times for that date
  and does not say so.
- **Search by phone returns cancelled bookings** mixed with active ones. The client filters.

---

## Deployment

The agent runs on LiveKit Cloud, the frontend on Vercel. Putting the agent next to the media
server removes a hop, and it sits in a US region deliberately, for the reason in the latency
section.

Two free-plan behaviours are visible in the code:

**Agents sleep.** Once a deployed agent's sessions end it shuts down, and the next caller
waits 10–20 seconds for it to boot. In a voice product that is indistinguishable from a
broken page. The frontend calls `POST /api/warmup` on page load, dispatching the agent to a
throwaway room so the cold start overlaps with the caller granting mic access. It is an
optimisation: if warming fails the call still works, just slower, and the failure never
reaches the caller. A paid plan keeps the agent resident and makes this unnecessary.

**The mock API runs inside the agent process** (`LUMA_EMBED_MOCK_API`), because LiveKit
requires the container to launch the agent directly rather than a script starting a second
service. Right for assessment scaffolding, wrong for a real backend. A production deployment
points `LUMA_API_BASE_URL` at the restaurant's API and leaves the flag unset.

`/api/token` mints LiveKit tokens without authentication, so it refuses to start unless
`ALLOW_PUBLIC_DEMO` is set. That keeps an open endpoint a deliberate choice for a demo. A
real deployment authenticates the caller first.

---

## Scaling

**10 concurrent calls.** What is here. One worker handles multiple sessions; LiveKit handles
media. Move `CallState` to Redis keyed by room so a restart does not lose a call.

**100.** Horizontal workers behind LiveKit's job dispatch, which already load-balances across
registered workers. `num_idle_processes` keeps warm processes so nobody waits on a cold model
load. Provider rate limits bind before CPU does: Deepgram and Cartesia need raised quotas,
and the LLM needs provisioned throughput or a fallback model on 429. Add per-session tracing,
because grepping logs stops working here.

**1,000.** Regional worker pools near the media edge, since a cross-region hop adds 80–150 ms
to every turn. Put availability reads behind a read replica; they outnumber writes maybe
10:1. Warm-pool TTS connections. At this scale the economics justify re-examining a realtime
model for conversational turns while keeping the cascaded path for tool-heavy ones, but that
is a measured decision.

**Cost per five-minute call**, list prices: STT ~$0.03, LLM ~$0.01 at ~15k tokens with
caching, TTS ~$0.05 at ~1,800 characters, LiveKit media ~$0.01. About **$0.10**, dominated by
TTS. The same call on a realtime model runs roughly $0.60–0.80.

---

## Security

Names and phone numbers are PII and currently appear in logs. A real deployment would redact
at the logging layer, encrypt transcripts with a short retention window, keep recordings off
by default, and move provider keys into a secret manager. LiveKit tokens are already minted
server-side and scoped to a single room.

---

## AI assistance

Built with Claude (Anthropic) as a coding assistant, for scaffolding, SDK research against
current LiveKit docs, and drafting. The architecture, tool design, safety gates, and
evaluation strategy are mine, and I can explain or change any part of it.

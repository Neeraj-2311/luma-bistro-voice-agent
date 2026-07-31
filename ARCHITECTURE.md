# Architecture Questions

Answers to `starter/ARCHITECTURE_QUESTIONS.md`.

### 1. Why this voice framework, STT, LLM, TTS, and transport?

**LiveKit Agents** because the transport, turn detection, interruption handling, and
metrics are one integrated stack rather than four libraries I have to keep in sync,
and because its test framework lets the scenario suite drive the *same* agent object
over text — the tests are not a parallel reimplementation.

**Deepgram nova-3** for STT: streaming with interim results, which is what makes
barge-in feel immediate, plus `keyterm` boosting. That last one is not cosmetic —
"LUMA" is otherwise transcribed as "Luna" often enough to break confirmation-code
lookups.

**gpt-4.1-mini** for the LLM: this workload is tool selection and slot filling, not
reasoning. A larger model costs more latency per turn than it buys in accuracy here.
`temperature=0.2` because sampling variance in a reservation agent shows up as a wrong
party size, not as personality.

**Cartesia sonic-3** for TTS: sub-200 ms time-to-first-byte in practice, which is the
only TTS number that matters in a conversation.

**WebRTC via LiveKit Cloud** rather than SIP: the assessment accepts a browser
experience, and WebRTC gets Opus at 48 kHz instead of the 8 kHz G.711 a phone line
would impose. Better audio in means better transcription. A SIP trunk is an additive
change, not a rewrite.

I chose a **cascaded pipeline over a speech-to-speech model** deliberately — the
reasoning is in the README under "Major decisions".

### 2. How is session and reservation state stored?

Reservations live in the API. The agent holds only per-call state (`state.py`) in
process memory: collected name and phone, which slots have been verified available,
which reservations this call has touched, and whether a handoff happened.

That split is intentional. The chat context is the record of the *conversation*; it is
not a safe place to keep facts the code needs to be right about, because it gets
summarized, truncated, and paraphrased. Anything a safety gate depends on lives in
`CallState`, not in the transcript.

In-process memory is the honest limitation: a worker crash mid-call loses the collected
details. Redis keyed by room name is the fix and is a small change, since `CallState`
is already the single place that state lives.

### 3. How do you cancel generation during barge-in?

The framework handles it, which is a large part of why I chose it. On detected user
speech it cancels the LLM stream and the TTS stream, stops playout, and — the part that
actually matters for correctness — **truncates the assistant message in the chat
context to only the portion the caller actually heard**. Without that, the model
believes it said a sentence the caller never received, and the next turn is built on a
false premise.

Configuration choices: `mode="adaptive"` so "mm-hm" does not count as an interruption,
and `resume_false_interruption=True` with a 1.5 s timeout so a cough leaves the agent
picking its sentence back up rather than sitting in dead air.

Tool calls in flight are a separate concern: they complete rather than being cancelled,
because a half-executed `POST` is worse than a wasted one. The idempotency key means a
completed-but-discarded create is harmless.

### 4. How are tool arguments validated?

In `rules.py`, before anything reaches the network. Every argument is normalized
first (`"6:30 PM"` → `18:30`, `"(310) 555-0199"` → `3105550199`, `"luma 4821"` →
`LUMA-4821`) and then range-checked.

The important design point is what a failure produces: an `ArgumentError` whose message
is written **for the model to act on**, not for the caller to hear —
`"'555-0199' has only 7 digits. Ask the caller to repeat their full ten-digit phone
number."` It is raised as a `ToolError`, which the framework feeds back into the
conversation, so a bad argument becomes a self-correcting turn instead of a 422 the
caller experiences as a failure.

### 5. How are duplicate writes prevented?

Three layers, and only the first is load-bearing:

1. **Content-derived idempotency key.** `Idempotency-Key` is a SHA-256 of name, phone,
   date, time, and party size — not a per-call UUID. The model never sees it. Two
   identical create attempts therefore carry the same key by construction, and the API
   returns the original reservation. This is what makes duplicate writes *impossible*
   rather than *unlikely*.
2. **In-process short-circuit.** If this call already created a booking with that
   fingerprint, the tool answers from memory without a network round trip.
3. **`parallel_tool_calls=False`**, so the model cannot batch `check_availability` and
   `create_reservation` into one response and race the availability gate.

The key is also what makes retrying a `POST` safe, which is why the HTTP client retries
writes at all.

### 6. Which failures are retried?

5xx, 429, and transport errors: once, after 500 ms. Nothing else. A 4xx will not fix
itself, so retrying it just adds latency before the same failure.

The retry lives in the HTTP client, not the prompt. Two reasons. It makes "at most one
retry" a guarantee instead of an instruction a model may or may not honour. And a blip
that clears in half a second should not cost the caller an audible "let me try that
again" — the mock API's synthetic 503 on `2026-08-16` is invisible to both the model
and the caller, which the T6 test asserts by inspecting the actual HTTP attempt log.

When both attempts fail, the tool raises and the agent says so and offers a human. It
never fabricates a result.

### 7. How is context preserved during handoff?

`transfer_to_human` posts to `/handoff` with two things: the **structured** state
(name, phone, notes, every slot checked, every reservation touched, and why the
escalation happened) and the last 20 turns of transcript. The structured part matters
more — a human picking up the call should not have to read a transcript to learn the
caller's phone number.

If `/handoff` itself fails, the summary is written to the error log rather than
dropped, and the caller is never told the transfer succeeded when it did not.

**Silence** is handled in `main.py` rather than the prompt, for the same reason: no turn
is produced, so there is nothing for the model to react to. The session's `user_away_timeout`
fires the recovery.

### 8. Which production metrics and logs matter?

**Latency, split by stage.** End-of-utterance delay, LLM time-to-first-token, and TTS
time-to-first-byte are collected per turn and summed (`metrics.py`). The sum is
what the caller experiences; the split is what tells you which vendor to call when it
regresses. A single blended number is not actionable.

**Task success, not uptime.** Reservations created per call started, handoff rate, and
handoff reason distribution. An agent that never errors and never books is a broken
agent that looks healthy.

**Tool-call error rate by tool and by error code**, which is the early warning for a
prompt regression — validation errors climbing means the model has started producing
worse arguments.

**Duplicate-write rate**, which should be structurally zero; any non-zero value means
the fingerprint logic broke.

Every API attempt is logged with method, path, attempt number, outcome, and duration
(`APICall`), so the retry behaviour is auditable after the fact rather than inferred.

### 9. How would the system change at 10, 100, and 1,000 concurrent calls?

Covered in the README under "Scaling". Short version: 10 is what is here plus Redis for
session state; 100 is horizontal workers, warm processes, and raised provider quotas,
with per-session tracing because log-grepping stops working; 1,000 is regional worker
pools near the media edge, a read replica for availability lookups, and a genuine
re-evaluation of realtime-vs-cascaded on measured cost.

### 10. What would you improve in the supplied API?

- **A whole-day availability endpoint.** One slot per request means a round trip per
  candidate time, and the only way to learn the bookable grid is to probe it and collect
  422s. This is the single biggest source of avoidable latency in the agent.
- **Make `PATCH` idempotent** like `POST` already is. A retried modify can double-apply
  against a concurrent change; an `If-Match` / version field would fix it.
- **Errors should carry remediation.** `INVALID_SLOT` knows which times are valid for
  that date and does not say so, which forces the agent to guess or ask.
- **Search returns cancelled reservations mixed with active ones**, so every client has
  to filter. Mine does.
- **No pagination or auth**, both of which a real system needs before it sees traffic.

### 11. How would you protect PII, recordings, transcripts, and secrets?

Names and phone numbers are PII and currently appear in logs — acceptable for an
assessment, not for production. The changes: redact at the logging layer so PII never
reaches disk in the first place; encrypt transcripts at rest with a short retention
window (30 days is typical for dispute resolution); keep recordings off by default and
consent-gated where they are on; move provider keys from `.env` into a secret manager
with rotation.

LiveKit access tokens are already minted server-side by the Next.js route handler and
scoped to a single room, so a leaked client token grants one room, not the project.

The one genuine security hole in the current build is **no caller authentication** —
anyone with a confirmation code can cancel a booking. A real deployment needs at minimum
a match between the caller's number and the number on the reservation.

### 12. Estimate cost per five-minute call.

Assuming a caller who speaks for about 90 seconds of the 5 minutes and an agent that
speaks about 1,800 characters:

| Component | Basis | Cost |
|---|---|---|
| Deepgram nova-3 | 5 min streaming | ~$0.03 |
| gpt-4.1-mini | ~15k in / 1k out, with prompt caching | ~$0.01 |
| Cartesia sonic-3 | ~1,800 characters | ~$0.05 |
| LiveKit Cloud | 5 participant-minutes | ~$0.01 |
| **Total** | | **~$0.10** |

TTS dominates, which is the useful conclusion: the cheapest meaningful optimization is
making the agent say less, not switching LLMs. Shorter replies improve both cost and
perceived latency, which is why the prompt caps replies at one or two sentences.

For comparison, the same call on a realtime speech-to-speech model runs roughly
$0.60–0.80 — about 7x — which is a large part of why the cascaded pipeline is the right
default for a tool-heavy workload.

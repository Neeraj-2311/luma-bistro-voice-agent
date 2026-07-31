# Code Walkthrough

A guide to explaining this codebase out loud. Six source files, one job each.

---

## The 30-second version

> A caller talks to a browser page. Their audio goes over WebRTC to LiveKit Cloud,
> which hands it to a Python worker. The worker runs a three-stage pipeline —
> Deepgram turns speech into text, GPT-4.1-mini decides what to do and which tool
> to call, Cartesia turns the reply back into speech. The tools talk to the
> restaurant's reservation API over HTTP. Everything that must not go wrong —
> booking twice, booking a time we never checked, booking before the caller
> agreed — is enforced in code, not asked for in the prompt.

---

## The six files

| File | One sentence | Lines |
|---|---|---:|
| `main.py` | Builds the voice pipeline and starts one agent per call. | 126 |
| `agent.py` | The eight tools — everything the agent can actually do. | 559 |
| `api.py` | Talks to the reservation API: retries, timeouts, idempotency. | 231 |
| `rules.py` | What inputs are legal, and which dates/times exist at all. | 200 |
| `state.py` | What we've collected on this call, and what we read back. | 97 |
| `prompts.py` | What the agent is told. | 102 |
| `metrics.py` | Per-turn latency, split by stage. | 74 |

**How to describe the shape:** `main.py` is the wiring, `agent.py` is the behaviour,
and the other four are things `agent.py` leans on so it doesn't have to do
networking, parsing, remembering, and measuring itself.

---

## Read them in this order

### 1. `main.py` — the voice pipeline

This is the only file that knows about audio. `_build_session()` picks the four
components and the turn-taking behaviour. Everything in it is a decision you can
be asked about:

```python
stt = deepgram.STT(model="nova-3", keyterm=KEYTERMS, interim_results=True)
llm = openai.LLM(model="gpt-4.1-mini", temperature=0.2, parallel_tool_calls=False)
tts = cartesia.TTS(model="sonic-3")
```

- **`keyterm`** boosts restaurant words at decode time. Without it "LUMA" transcribes
  as "Luna" often enough to break confirmation-code lookups.
- **`interim_results=True`** is what makes barge-in feel instant — the agent reacts to
  partial speech instead of waiting for a finalized transcript.
- **`temperature=0.2`** because sampling variance here shows up as a wrong party size,
  not as personality.
- **`parallel_tool_calls=False`** stops the model batching `check_availability` and
  `create_reservation` into one response, which would race the availability gate.

Then turn-taking:

- **Semantic turn detection** instead of a fixed silence timeout, so a caller pausing
  mid-sentence ("my number is three one zero… five five five") isn't cut off.
- **`interruption.mode="adaptive"`** distinguishes a real interruption from "mm-hm".
- **`resume_false_interruption`** — if the agent stops for a cough, it picks the
  sentence back up instead of leaving dead air.
- **`preemptive_generation`** runs the LLM against the in-progress transcript so the
  first token is ready at end of turn.

> **Likely question: "Isn't preemptive generation dangerous with tools?"**
> I checked the SDK source before enabling it. `perform_tool_executions` runs *after*
> the `_wait_for_scheduled()` gate, so a preemptively generated turn produces the
> tool-call *decision* early but only *executes* it once the turn is committed.
> Latency win, no risk of a speculative write.

### 2. `agent.py` — the tools

Eight tools. Read the tool names first; they're the whole feature set:

| Tool | Purpose |
|---|---|
| `check_availability` | Is this one slot open? |
| `find_available_times` | What's open all day? |
| `read_back_booking` | Recite the booking for confirmation |
| `create_reservation` | Write it |
| `find_reservation` | Look up an existing booking |
| `modify_reservation` | Change it |
| `cancel_reservation` | Cancel it |
| `transfer_to_human` | Escalate with context |

**Tools return sentences, not JSON.** The model reads them into speech, and
"6:30 PM is full" survives a paraphrase better than a nested object.

Many returns end with an instruction to the model — *"Offer these exact times and
nothing else"*. That's deliberate: the tool result is the last thing the model sees
before it speaks, so it's the highest-leverage place to steer it.

### 3. `api.py` — the network boundary

Two things worth explaining:

**One exception class, not seven.** The API returns an error code on every failure,
so the client passes that code through and callers branch on `err.code`. Earlier
this was a class per failure; that was six extra concepts buying nothing.

**The retry lives here, not in the prompt.** Only 5xx, 429, and transport errors
retry, once, after 500ms. 4xx never retries — a bad argument won't fix itself.

### 4. `rules.py` — legality

Two related jobs: normalizing arguments (`"6:30 PM"` → `"18:30"`), and the booking
calendar (which dates and times exist at all).

Validation failures raise `ArgumentError` with a message written **for the model**,
not the caller: *"'555-0199' has only 7 digits. Ask the caller to repeat their full
ten-digit number."* That becomes a self-correcting turn instead of a 422.

### 5. `state.py` — per-call memory

`CallState` holds the name, phone, which slots we've verified, which reservations
we've touched, and the current `BookingProposal`.

> **Likely question: "Why not keep this in the chat context?"**
> The chat context is the record of the *conversation*. It gets summarized,
> truncated, and paraphrased. Anything a safety gate depends on can't live somewhere
> that a summarizer might rewrite.

---

## The five decisions to have ready

### 1. Duplicate prevention is structural

`POST /reservations` needs an `Idempotency-Key`. Instead of a random UUID per call,
the key is a **hash of the booking itself** — name, phone, date, time, party size:

```python
def reservation_fingerprint(name, phone, date, time, party_size) -> str:
    digest = hashlib.sha256("|".join([...]).encode()).hexdigest()
    return f"luma-{digest[:32]}"
```

The LLM never sees it. Two identical create attempts produce the same key, so the API
returns the original reservation instead of writing a second row. **A duplicate is
impossible by construction, not merely unlikely.** It's also why retrying a `POST`
is safe.

### 2. The agent can't book a slot it never checked

`create_reservation` refuses unless `check_availability` already returned "open" for
that exact date, time, and party size. "Don't invent availability" is enforced in
code rather than requested in a prompt.

Same idea for modify/cancel: those take a **confirmation code**, never an internal
reservation id, and resolve it against bookings this call actually looked up — so the
model can't act on something it never retrieved.

### 3. Confirmation is a two-phase commit

`read_back_booking` records exactly what was recited and on which turn.
`create_reservation` refuses anything that doesn't match, and refuses until the caller
has spoken *since*.

> **Tell the story:** this started as a prompt rule plus a turn-count check, and it
> failed about **one run in six** — the model would occasionally read back and book in
> the same breath, so a correction on the next turn landed after the write. Making the
> read-back a recorded artifact rather than a hoped-for behaviour turned it into
> something the code guarantees.

### 4. "Closed" and "full" are different failures

The API holds three dates; any other date 422s per-slot. The first version reported
that as *unavailability* and offered another time **on the same impossible date** —
an infinite loop, plus a fabricated shrinking list of "available" times built from
accumulated failures.

`rules.py` now owns the calendar (static) separately from availability (live, API
only). A dead date is rejected before any request, names the dates that *are* open,
and is forbidden from suggesting another time.

> This is the best bug to volunteer. It was found by actually making a call, the fix
> is a conceptual separation rather than a patch, and there's a regression test.

### 5. Cascaded pipeline over speech-to-speech

Realtime would save ~200–300ms. I chose cascaded because this task is dominated by
tool correctness: tool calling is more reliable on a text LLM, per-stage latency is
measurable, every stage is independently testable, and it's ~7x cheaper.

**The payoff is visible in the numbers:** p50 is ~1.8s, and the split says why — TTS
154ms, EOU 606ms, but **LLM TTFT ~850ms** dominates (India → OpenAI US, plus a large
prompt+tools prefix each turn). A blended number would have told me the turn was slow;
the split tells me which vendor to call.

---

## Testing

Two suites, on purpose:

- **`test_standard_scenarios.py`** — drives the real agent and real API over text.
  Asserts in three layers, weakest last: **API state** (what got written) →
  **tool calls** (which ran) → **wording** (LLM-judged). Most assertions are in the
  first two, because a suite that only judges transcripts passes while writing the
  wrong row to the database.
- **`test_tool_layer.py`** — calls tools directly, no LLM. Covers the safety gates a
  well-behaved model never trips, which is exactly why they need their own coverage.
  Runs in under a second.

> **Likely question: "How do you test barge-in?"**
> I don't, automatically — text-mode tests can't exercise acoustic behaviour. T3 asserts
> the *semantics* of a mid-flow correction (the final party size is what gets written).
> The acoustic side is configuration plus the demo. I'd rather say that than claim
> coverage I don't have.

---

## Known weak spots (say these before you're asked)

- **State is in memory.** A worker crash mid-call loses collected details. Redis keyed
  by room is the fix; `CallState` is already the single place it lives.
- **No caller authentication.** Anyone with a confirmation code can cancel a booking.
- **The calendar comes from seed data**, because the API doesn't expose it. That's a
  workaround for a missing endpoint, not a design preference.
- **Latency is ~2x what it should be**, and I know which stage owns it.

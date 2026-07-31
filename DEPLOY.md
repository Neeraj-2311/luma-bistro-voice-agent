# Deployment

Two things to deploy: the **agent worker** (Fly.io, entirely from your terminal) and the
**frontend** (Vercel, entirely from the browser). LiveKit Cloud is already set up.

```
Reviewer's browser
      │
      │  1. loads the page
      ▼
  Vercel ───────────────► Next.js frontend, public URL, mints LiveKit tokens
      │
      │  2. joins a room over WebRTC (audio never touches Vercel)
      ▼
  LiveKit Cloud ────────► media server, terminates audio at an edge near the caller
      │
      │  3. dispatches the room to the worker registered as "luma-bistro"
      ▼
  Fly.io (iad) ─────────► agent worker + mock reservation API, one container
      │
      ├──► Deepgram / OpenAI / Cartesia
      └──► mock reservation API on 127.0.0.1:8000
```

**Why the worker is not on LiveKit Cloud.** It does not need to be. The worker dials
*out* to LiveKit and registers; LiveKit never connects in. Self-hosting avoids competing
with the agents already on this account and — the real reason — lets it sit in
**us-east**, next to OpenAI. `evals/latency_probe.py` showed ~75% of LLM
time-to-first-token was the round trip from India to OpenAI's US origin. Moving the
worker is the fix. Callers are unaffected: LiveKit still terminates their media at an
edge near them.

---

## Before you start

| | |
|---|---|
| **Time** | ~15 minutes total |
| **Fly.io cost** | New accounts get **$5 trial credit**, then pay-as-you-go — the free tier was removed. This machine is roughly **$5–7/month** if left running. A card is required at signup. |
| **Vercel cost** | Free. |
| **Keep the bill at zero** | Deploy, record the demo, then `fly apps destroy luma-bistro-agent`. A week of uptime is ~$1.50 and fits inside the trial credit. |

You do **not** create anything on the Fly website. Fly is CLI-first: you sign up through
the browser once, and everything after that is terminal commands run from this repo.

---

## Part 1 — Agent worker on Fly.io (terminal)

### Step 1. Install the CLI

```bash
curl -L https://fly.io/install.sh | sh
```

It prints a line to add to your shell profile. Either follow it or start a new terminal,
then confirm:

```bash
fly version
```

### Step 2. Sign up

```bash
fly auth signup      # already have an account? use: fly auth login
```

This opens your browser. Create the account, add a card when asked, come back to the
terminal. Confirm you are signed in:

```bash
fly auth whoami
```

### Step 3. Create the app

From the repo root (`~/Projects/parse-voice-assessment`):

```bash
fly launch --no-deploy --copy-config --name luma-bistro-agent --region iad
```

- `--copy-config` uses the `fly.toml` already in this repo instead of guessing.
- `--no-deploy` creates the app but does not start it — secrets come first.
- `--region iad` is US East (Virginia). **This is the latency decision; do not change it.**

If it says the name is taken, pick another (`--name luma-bistro-neeraj`) — the name only
affects the internal `.fly.dev` hostname, which nobody visits.

If it asks about a database or Redis, say **no** to both.

### Step 4. Set the secrets

Six values. They are encrypted at rest and injected as environment variables at runtime.
Never put them in `fly.toml` — that file is committed to a public repo.

Your `.env` already has all six, so let the shell read them rather than copy-pasting:

```bash
set -a && source .env && set +a
fly secrets set \
  LIVEKIT_URL="$LIVEKIT_URL" \
  LIVEKIT_API_KEY="$LIVEKIT_API_KEY" \
  LIVEKIT_API_SECRET="$LIVEKIT_API_SECRET" \
  DEEPGRAM_API_KEY="$DEEPGRAM_API_KEY" \
  OPENAI_API_KEY="$OPENAI_API_KEY" \
  CARTESIA_API_KEY="$CARTESIA_API_KEY"
```

Check all six landed (values are never shown, only names and digests):

```bash
fly secrets list
```

### Step 5. Deploy

```bash
fly deploy
```

First build takes 3–5 minutes. Then watch the logs:

```bash
fly logs
```

**The line you are looking for:**

```
registered worker  {"agent_name": "luma-bistro", "url": "wss://personal-...livekit.cloud"}
```

That means the worker is connected to LiveKit and waiting for calls. If you see it, the
backend is done.

```bash
fly status     # should show one machine, started, in iad
```

---

## Part 2 — Frontend on Vercel (browser)

1. Go to <https://vercel.com/new> and sign in with GitHub.
2. Import **`Neeraj-2311/luma-bistro-voice-agent`**.
3. **Set Root Directory to `web`.** This is the one setting that matters — without it the
   build fails, because the repo root is a Python project.
4. Expand **Environment Variables** and add four:

   | Name | Value |
   |---|---|
   | `LIVEKIT_URL` | `wss://personal-eam73zme.livekit.cloud` |
   | `LIVEKIT_API_KEY` | same as your `.env` |
   | `LIVEKIT_API_SECRET` | same as your `.env` |
   | `AGENT_NAME` | `luma-bistro` |

5. Click **Deploy**. You get a URL like `https://luma-bistro-voice-agent.vercel.app`.

`AGENT_NAME` is the important one. It makes the frontend request this agent by name,
which is why the other agents on your LiveKit account never join these rooms.

---

## Part 3 — Test it

Open the Vercel URL, click **Call the restaurant**, allow the microphone, and say:

> "Do you have a table for two on Friday, August fourteenth at six PM?"

Watch `fly logs` in a terminal while you talk — you will see the tool calls and the
per-turn latency line for each turn.

---

## How a call flows once deployed

1. The reviewer opens the Vercel URL. On **Call**, Vercel mints a short-lived LiveKit
   token scoped to one room, with `roomConfig.agents = ["luma-bistro"]`.
2. The browser joins that room over WebRTC via whichever LiveKit edge is closest to them.
3. LiveKit sees the room wants `luma-bistro` and dispatches the job to your worker in
   `iad`, which spawns a dedicated process for that call.
4. Audio streams caller → LiveKit → worker. Deepgram transcribes, GPT-4.1-mini decides,
   Cartesia speaks, and the reply streams back the same way.
5. Tool calls hit the mock API on `127.0.0.1:8000` inside the same container — ~3 ms, so
   tool latency never distorts the voice numbers.
6. On hangup the worker logs the session's latency summary and tears the process down.

**Concurrency.** One machine handles several simultaneous calls, each in its own process.
`min_machines_running = 1` and `auto_stop_machines = false` keep it always registered — a
scaled-to-zero worker is not registered at all, so a caller would find *no agent* rather
than a slow one. That is the worse failure.

**Memory is the sizing constraint, not CPU.** Each resident agent process preloads Silero
VAD, so memory is set by `LUMA_IDLE_PROCESSES` (2 here) plus live calls. The framework's
production default is ten idle processes, which does not fit in 1GB — that is why it is
set explicitly.

**Cost.** ~$5–7/month if left running; provider usage is ~$0.10 per five-minute call,
dominated by TTS.

---

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| No `registered worker` line | Bad LiveKit credentials | `fly secrets list`, re-set the three `LIVEKIT_*` values |
| Machine restarts in a loop | Out of memory | `fly logs` for `OOM`; raise `memory` in `fly.toml` or lower `LUMA_IDLE_PROCESSES` |
| Vercel build fails | Root Directory not set to `web` | Project → Settings → General → Root Directory |
| Page loads, "Call" does nothing | `AGENT_NAME` missing or misspelled | Must be exactly `luma-bistro`, then redeploy |
| Agent answers but every booking fails | Mock API did not start | `fly logs` for `mock reservation API ready` |

---

## What is *not* production-ready here

Worth saying out loud rather than implying otherwise:

- **The mock API ships inside the container** and holds reservations in memory. Restart
  the machine and every booking is gone. It is the assessment's stand-in for a booking
  backend; a real deployment points `LUMA_API_BASE_URL` at the real API and drops it.
- **Two processes in one container.** Fine for a demo, wrong for production — the API and
  the worker should scale and restart independently.
- **Per-call state is in memory** (`CallState`), so a restart mid-call loses the details
  collected so far. Redis keyed by room is the fix.
- **One region, one machine.** No failover. At real traffic you would run worker pools in
  several regions and let LiveKit route to the nearest.

---

## Updating and teardown

```bash
fly deploy                          # rebuild and roll the machine
fly apps destroy luma-bistro-agent  # stop billing
```

Vercel redeploys itself on every push to `main`; delete the project there to finish.

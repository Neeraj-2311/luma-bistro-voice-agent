# Deployment

Three pieces, deployed once, then left alone:

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
      │  3. dispatches the room to a worker registered as "luma-bistro"
      ▼
  Fly.io (iad) ─────────► agent worker + mock reservation API, one container
      │
      ├──► Deepgram / OpenAI / Cartesia
      └──► mock reservation API on 127.0.0.1:8000
```

**Why the worker is not on LiveKit Cloud.** It does not need to be. The worker dials
*out* to LiveKit and registers; LiveKit never connects in. Hosting it yourself avoids
competing with the agents already deployed on this account, and — the real reason — lets
it sit in **us-east**, next to OpenAI. `evals/latency_probe.py` showed ~75% of LLM
time-to-first-token was the round trip from India to OpenAI's US origin. Moving the
worker is the fix. The caller's audio is unaffected: LiveKit still terminates their media
at an edge near them and backhauls to the worker.

---

## 1. Agent worker → Fly.io

```bash
# Install flyctl and sign in
curl -L https://fly.io/install.sh | sh
fly auth signup          # or: fly auth login

# Create the app without deploying yet (fly.toml is already in the repo)
fly launch --no-deploy --copy-config --name luma-bistro-agent --region iad

# Secrets. These are encrypted at rest and injected as env vars at runtime --
# never put them in fly.toml, which is committed.
fly secrets set \
  LIVEKIT_URL="wss://personal-eam73zme.livekit.cloud" \
  LIVEKIT_API_KEY="..." \
  LIVEKIT_API_SECRET="..." \
  DEEPGRAM_API_KEY="..." \
  OPENAI_API_KEY="..." \
  CARTESIA_API_KEY="..."

fly deploy
```

Confirm it came up:

```bash
fly logs      # look for: registered worker  agent_name=luma-bistro
fly status    # one machine, started, in iad
```

The line you want is `registered worker` with `"agent_name": "luma-bistro"`. That means
the worker is connected to LiveKit and waiting for calls.

## 2. Frontend → Vercel

The frontend is a normal Next.js app in `web/`.

1. Go to <https://vercel.com/new> and import `Neeraj-2311/luma-bistro-voice-agent`.
2. Set **Root Directory** to `web`. Everything else is auto-detected.
3. Add these environment variables:

   | Name | Value |
   |---|---|
   | `LIVEKIT_URL` | `wss://personal-eam73zme.livekit.cloud` |
   | `LIVEKIT_API_KEY` | your key |
   | `LIVEKIT_API_SECRET` | your secret |
   | `AGENT_NAME` | `luma-bistro` |

4. Deploy. You get a URL like `https://luma-bistro-voice-agent.vercel.app`.

`AGENT_NAME` is the important one. It tells the frontend to request this specific agent
by name, which is why the other agents on this LiveKit account never join these rooms.

## 3. Try it

Open the Vercel URL, click **Call the restaurant**, allow the microphone, and talk.

---

## How a call flows once deployed

1. The reviewer opens the Vercel URL. Vercel serves the page and, on **Call**, mints a
   short-lived LiveKit token scoped to one room, with `roomConfig.agents = ["luma-bistro"]`.
2. The browser joins that room over WebRTC, connecting to whichever LiveKit edge is
   closest to them.
3. LiveKit sees the room wants `luma-bistro` and dispatches the job to the registered
   worker in `iad`. The worker spawns a process for that call.
4. Audio streams caller → LiveKit → worker. Deepgram transcribes, GPT-4.1-mini decides,
   Cartesia speaks, and the reply streams back the same way.
5. Tool calls hit the mock API on `127.0.0.1:8000` inside the same container — roughly
   3 ms, so tool latency never distorts the voice numbers.
6. On hangup the worker logs the session's latency summary and tears the process down.

**Concurrency.** One machine handles multiple simultaneous calls; each gets its own
process. `min_machines_running = 1` and `auto_stop_machines = false` keep it always
registered — a sleeping worker is not registered at all, so a caller would find no agent
rather than a slow one.

**Cost.** A `shared-cpu-1x` / 1GB machine sits inside Fly's free allowance. Provider usage
is roughly **$0.10 per five-minute call**, dominated by TTS (see the README).

---

## What is *not* production-ready here

Worth saying out loud rather than implying otherwise:

- **The mock API ships inside the container** and holds reservations in memory. Restart
  the machine and every booking is gone. It is the assessment's stand-in for a booking
  backend; a real deployment points `LUMA_API_BASE_URL` at the real API and drops it.
- **Two processes in one container.** Fine for a demo, wrong for production — the API and
  the worker should scale and restart independently.
- **Per-call state is in memory** (`CallState`), so a machine restart mid-call loses the
  details collected so far. Redis keyed by room is the fix.
- **One region, one machine.** No failover. At real traffic you would run worker pools in
  several regions and let LiveKit route to the nearest.

---

## Updating

`fly deploy` from the repo root rebuilds and rolls the machine. Vercel redeploys itself on
every push to `main`.

## Teardown

```bash
fly apps destroy luma-bistro-agent
```

Then delete the project in the Vercel dashboard.

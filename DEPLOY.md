# Deployment

Two things to deploy: the **agent** to LiveKit Cloud (terminal), and the **frontend** to
Vercel (browser). About 15 minutes.

```
Reviewer's browser
      │
      │  1. loads the page, which immediately warms the agent
      ▼
  Vercel ───────────────► Next.js frontend, public URL, mints LiveKit tokens
      │
      │  2. joins a room over WebRTC (audio never touches Vercel)
      ▼
  LiveKit Cloud ────────► media server + the agent itself
      │
      ├──► Deepgram / OpenAI / Cartesia
      └──► mock reservation API, hosted in the agent process
```

The agent runs on LiveKit Cloud, so it sits next to the media server and needs no
separate host. The starter's mock reservation API runs **inside the agent process**
(`LUMA_EMBED_MOCK_API=1`), because LiveKit requires the container to launch the agent
directly with no wrapper script starting a second service. A real deployment points
`LUMA_API_BASE_URL` at the restaurant's own booking system and leaves that flag unset.

---

## Part 1 — Agent on LiveKit Cloud (terminal)

The CLI is already installed. Everything happens from the repo root.

### Step 1. Point the CLI at the account you want

```bash
lk cloud auth          # opens a browser; sign in to the account you want to deploy to
lk project list        # confirm the right project is marked with *
```

If you have several projects, add `--project <name>` to the commands below, or switch the
default with `lk project set-default <name>`.

### Step 2. Create the agent

```bash
lk agent create --region us-east .
```

- `--region us-east` is the latency decision. `evals/latency_probe.py` showed ~75% of LLM
  time-to-first-token was the round trip from India to OpenAI's US origin, so the agent
  belongs near the model. Callers are unaffected — LiveKit still terminates their media
  at an edge near them. If `us-east` is rejected, run without `--region` and note it.
- It builds the `Dockerfile` in this repo and writes a `livekit.toml` with the new agent
  id. Commit that file.

It will prompt for secrets. You need **three** — LiveKit injects `LIVEKIT_URL`,
`LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` automatically:

```
DEEPGRAM_API_KEY=...
OPENAI_API_KEY=...
CARTESIA_API_KEY=...
```

To pass them non-interactively instead:

```bash
lk agent create --region us-east \
  --secrets "DEEPGRAM_API_KEY=...,OPENAI_API_KEY=...,CARTESIA_API_KEY=..." .
```

### Step 3. Confirm it is up

```bash
lk agent status
lk agent logs
```

The line you want in the logs is:

```
registered worker  {"agent_name": "luma-bistro", ...}
```

That means the agent is connected and waiting for calls.

### Redeploying after a code change

```bash
lk agent deploy
```

---

## Part 2 — Frontend on Vercel (browser)

1. Go to <https://vercel.com/new> and import **`Neeraj-2311/luma-bistro-voice-agent`**.
2. **Set Root Directory to `web`.** Without it the build fails, because the repo root is
   a Python project.
3. Add five environment variables:

   | Name | Value |
   |---|---|
   | `LIVEKIT_URL` | `wss://<your-project>.livekit.cloud` |
   | `LIVEKIT_API_KEY` | from `lk project list` |
   | `LIVEKIT_API_SECRET` | from the LiveKit dashboard |
   | `AGENT_NAME` | `luma-bistro` |
   | `ALLOW_PUBLIC_DEMO` | `true` |

4. Deploy. You get a URL like `https://luma-bistro-voice-agent.vercel.app`.

**`AGENT_NAME`** makes the frontend request this agent by name. That is why other agents
on the same LiveKit account never join these rooms, and it is also what the warm-up uses.

**`ALLOW_PUBLIC_DEMO`** is required because `/api/token` mints LiveKit tokens with no
authentication — anyone with the URL can start a session. That is an accepted trade for a
public demo, and the flag keeps it a deliberate choice. A real deployment authenticates
the caller and issues a token scoped to them.

---

## The cold start, and the warm-up

On LiveKit Cloud's free Build plan, **a deployed agent is shut down once its sessions
end**. The next caller then waits 10–20 seconds for it to boot before the agent joins —
which, in a voice demo, is indistinguishable from a broken page.

The frontend calls `POST /api/warmup` as soon as it loads. That dispatches the agent to a
throwaway room, so it boots while the caller is still reading the page and granting
microphone access rather than after they press **Call**. The agent finds nobody in the
warm-up room and the empty room is reaped on its own.

It is an optimisation, not a guarantee. If warming fails the call still works, just
slower, and it never surfaces an error to the caller.

**Before recording the demo video, load the page once and wait ten seconds.** That
guarantees a warm agent for the take.

---

## How a call flows once deployed

1. The reviewer opens the Vercel URL. The page warms the agent immediately.
2. On **Call**, Vercel mints a short-lived token scoped to one room, tagged
   `roomConfig.agents = ["luma-bistro"]`.
3. The browser joins over WebRTC via the nearest LiveKit edge.
4. LiveKit dispatches the room to the agent, which spawns a process for that call.
5. Deepgram transcribes, GPT-4.1-mini decides, Cartesia speaks. Tool calls hit the mock
   API in the same process, so tool latency never distorts the voice numbers.
6. On hangup the agent logs the session's latency summary.

**Limits on the free plan:** 5 concurrent agent sessions, which is ample for a demo.

**Cost.** LiveKit's free plan covers the agent. Provider usage is roughly **$0.10 per
five-minute call**, dominated by TTS.

---

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| No `registered worker` in logs | Missing provider secrets | `lk agent status`, re-set with `lk agent update-secrets` |
| Long silence after pressing Call | Cold start; warm-up did not fire | Reload the page and wait 10s before calling |
| Vercel build fails | Root Directory not set to `web` | Project → Settings → General → Root Directory |
| Page loads, Call returns 503 | `ALLOW_PUBLIC_DEMO` not set | Add it in Vercel, redeploy |
| Call connects but no agent joins | `AGENT_NAME` missing or misspelled | Must be exactly `luma-bistro` |
| Bookings fail with a system error | Mock API did not start | `lk agent logs` for `mock_api.embedded` |

---

## What is *not* production-ready here

- **The mock API runs inside the agent process** and holds reservations in memory.
  Restart the agent and every booking is gone. It is the assessment's stand-in for a
  booking backend, not a service.
- **Per-call state is in memory** (`CallState`), so a restart mid-call loses the details
  collected so far. Redis keyed by room is the fix.
- **The token endpoint is unauthenticated**, gated only by `ALLOW_PUBLIC_DEMO`.
- **Cold starts** are a free-plan behaviour. A paid plan keeps the agent resident and the
  warm-up becomes unnecessary.

## Teardown

```bash
lk agent delete
```

Then delete the project in the Vercel dashboard.

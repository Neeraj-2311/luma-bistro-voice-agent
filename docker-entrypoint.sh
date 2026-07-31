#!/usr/bin/env bash
# Run the mock reservation API and the agent worker side by side.
#
# Two processes in one container is a deliberate simplification for a demo: the
# mock API is assessment scaffolding, not a service with its own lifecycle. If
# either dies the container exits, so the platform restarts the pair together
# rather than leaving a worker talking to a dead backend.
set -euo pipefail

uv run uvicorn app:app --host 127.0.0.1 --port 8000 --app-dir starter &
API_PID=$!

# Wait for the API before the agent starts taking calls, so the first caller
# never hits a tool that is still booting.
for _ in $(seq 1 30); do
  if uv run python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8000/health').status_code==200 else 1)" 2>/dev/null; then
    echo "mock reservation API ready"
    break
  fi
  sleep 1
done

uv run python -m luma_agent.main start &
AGENT_PID=$!

# Exit as soon as either process does, and take the other with it.
wait -n "$API_PID" "$AGENT_PID"
EXIT_CODE=$?
echo "a process exited (code $EXIT_CODE); shutting down"
kill "$API_PID" "$AGENT_PID" 2>/dev/null || true
exit "$EXIT_CODE"

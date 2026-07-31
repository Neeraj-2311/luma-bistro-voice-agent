# Agent worker image. Works on LiveKit Cloud (`lk agent create`) and on any
# container host that can run it always-on.
#
# The container launches the agent directly, with no wrapper script and nothing
# backgrounded, which is what LiveKit Cloud requires. The starter's mock
# reservation API is hosted in-process instead, enabled by LUMA_EMBED_MOCK_API.
# A real deployment sets LUMA_API_BASE_URL to the restaurant's own booking system
# and leaves that flag unset.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    XDG_CACHE_HOME=/home/agent/.cache \
    LUMA_EMBED_MOCK_API=1 \
    LUMA_API_BASE_URL=http://127.0.0.1:8000

RUN useradd -m -u 1000 agent
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Dependencies first, so a code change does not reinstall the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY starter/ ./starter/
RUN uv sync --frozen --no-dev

# Fetch Silero VAD at build time. Without this the first caller on a cold worker
# waits for the weights to download.
RUN uv run python -m livekit.agents download-files

RUN mkdir -p /app/logs && chown -R agent:agent /app /home/agent
USER agent

# LiveKit's worker health endpoint.
EXPOSE 8081

# `start` is production mode: register with LiveKit and wait for jobs.
CMD ["uv", "run", "python", "-m", "luma_agent.main", "start"]

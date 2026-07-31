# Agent worker + the starter's mock reservation API in one image.
#
# They ship together on purpose: the mock API is the assessment's stand-in for a
# real booking backend, and co-locating it keeps tool latency at ~3ms so the
# numbers in EVALUATION_RESULTS.md reflect the voice pipeline rather than a
# network hop between two hosts. A real deployment would point LUMA_API_BASE_URL
# at the restaurant's actual API and drop the mock entirely.

FROM python:3.11-slim

# Model weights and caches live here rather than in a read-only home directory.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    XDG_CACHE_HOME=/home/agent/.cache

RUN useradd -m -u 1000 agent
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Dependencies first, so a code change does not reinstall the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY starter/ ./starter/
RUN uv sync --frozen --no-dev

# Silero VAD and the turn detector are downloaded at build time, not on the
# first call. Without this the first caller of a cold worker waits for weights.
RUN uv run python -m livekit.agents download-files || true

RUN mkdir -p /app/logs && chown -R agent:agent /app /home/agent
USER agent

# LiveKit's worker health endpoint. Fly checks this to know the worker is live.
EXPOSE 8081

COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Backend image. Python 3.12 slim, no build toolchain left in the final layer.
#
# No secret is present in any layer, including build arguments. The build stage
# installs dependencies only; nothing that reaches the daemon carries a
# credential (FR-042, Constitution IX).

# --- stage 1: build the dependency set ---------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Compiled wheels for the vector SDK and lxml need a toolchain; it does not
# need to survive into the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# --- stage 2: runtime ---------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

# curl is used by the compose healthcheck. libpq5 is required by asyncpg at
# runtime; build-essential is deliberately absent.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser tests ./tests

USER appuser

EXPOSE 8000

# No `--reload`. The mounted-source development workflow is `make dev`, and a
# reload watcher in a demonstration image is a way to serve stale code.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

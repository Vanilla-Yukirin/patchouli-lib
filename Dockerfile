# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4 AS uv

FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34 AS builder

COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34 AS runtime

ENV PATH=/app/.venv/bin:$PATH \
    PATCHOULI_DATABASE_URL=sqlite:////data/patchouli.db \
    PATCHOULI_ENVIRONMENT=production \
    PATCHOULI_LOG_LEVEL=info \
    PYTHONUNBUFFERED=1

RUN groupadd --system patchouli \
    && useradd --system --gid patchouli --home-dir /app patchouli \
    && install -d -o patchouli -g patchouli /app /data

WORKDIR /app
COPY --from=builder --chown=patchouli:patchouli /app /app
COPY --chown=patchouli:patchouli docker/entrypoint.sh /app/docker/entrypoint.sh

USER patchouli
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)"]

ENTRYPOINT ["sh", "/app/docker/entrypoint.sh"]
CMD ["python", "-m", "patchouli_lib"]

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev \
    && addgroup --system --gid 10001 rescuedesk \
    && adduser --system --uid 10001 --ingroup rescuedesk rescuedesk \
    && mkdir -p /app/.runtime/uploads /app/fixtures/contracts \
    && chown -R rescuedesk:rescuedesk /app

USER 10001:10001
EXPOSE 8000
CMD ["/app/.venv/bin/rescuedesk-serve"]

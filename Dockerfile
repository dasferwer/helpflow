FROM ghcr.io/astral-sh/uv:0.11.17 AS uv

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM base AS runtime
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
RUN chmod +x ./scripts/docker-entrypoint.sh
ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "helpflow.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS test
RUN uv sync --frozen --extra dev
COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests
CMD ["pytest"]

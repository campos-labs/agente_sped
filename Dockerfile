FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN groupadd --gid 10001 agente-sped && \
    useradd --uid 10001 --gid agente-sped --create-home \
    --shell /usr/sbin/nologin agente-sped

COPY --from=builder /app/.venv /app/.venv

USER agente-sped

CMD ["python", "-m", "agente_sped"]

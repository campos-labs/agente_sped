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
    SPED_SESSION_ROOT=/home/agente-sped/sessions \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN groupadd --gid 10001 agente-sped && \
    useradd --uid 10001 --gid agente-sped --create-home \
    --shell /usr/sbin/nologin agente-sped && \
    mkdir -p /home/agente-sped/sessions && \
    chown agente-sped:agente-sped /home/agente-sped/sessions

COPY --from=builder /app/.venv /app/.venv

USER agente-sped

CMD ["uvicorn", "agente_sped.interface.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log"]

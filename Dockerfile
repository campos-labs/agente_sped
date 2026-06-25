FROM python:3.14.3-slim AS builder

# Configura variáveis de ambiente do Poetry
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

# Instala o Poetry diretamente (sem precisar de pacotes de sistema adicionais)
RUN pip install --no-cache-dir poetry==2.3.2

WORKDIR /app

# Copia os arquivos de dependências
COPY pyproject.toml poetry.lock ./

# Instala as dependências (Vai baixar os binários WHEEL prontos instantaneamente)
RUN poetry install --only main --no-root && rm -rf $POETRY_CACHE_DIR

# ==========================================
# Estágio 2: Runtime (Imagem Final Limpa e Rápida)
# ==========================================
FROM python:3.14.3-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app"

# Copia o ambiente virtual pronto do estágio anterior
COPY --from=builder /app/.venv /app/.venv
COPY agente_sped/ /app/agente_sped/
COPY data/ /app/data/

# Cria o usuário de segurança no padrão Debian/Ubuntu (usando useradd)
RUN useradd -u 1000 speduser && \
    chown -R speduser:speduser /app
USER speduser

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "agente_sped.main"]
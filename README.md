# Agente SPED Fiscal

Projeto de análises locais e rastreáveis de EFD ICMS/IPI, em etapa de fundação técnica da v1.0.

## Disponível nesta etapa

Pacote Python instalável, configuração sem credenciais obrigatórias e imagem Docker. O comando `agente-sped` verifica a configuração e informa o estágio disponível.

Parser, catálogo de análises, orquestração LangGraph, relatórios, exportações e interface serão entregues nas próximas FEATs. O programa ainda não processa arquivos fiscais. A arquitetura-alvo está em [DESIGN.md](DESIGN.md).

## Executar localmente

Com Docker e Docker Compose disponíveis, na raiz do repositório:

```bash
docker compose build
docker compose run --rm agente-sped
```

O contêiner informa que a fundação está instalada e encerra. Esta etapa não inicia servidor HTTP, não publica portas e não requer `.env` ou API key.

## Configuração

Os valores padrão dispensam arquivo de configuração. Se necessário, copie `.env.example` para `.env` ao executar localmente com uv.

As flags `AI_ROUTING_ENABLED`, `AI_NARRATIVE_ENABLED` e `EXPLORATORY_SQL_ENABLED` permanecem `false`. Ativar uma capacidade ainda não implementada produz erro explícito; nenhuma chamada de IA é realizada nesta etapa.

Utilize apenas dados sintéticos em demonstrações, Issues e PRs. Dados locais e credenciais não devem ser versionados.

## Desenvolvimento

Use Python 3.14 e uv. Na raiz do repositório:

```bash
uv sync --locked
uv run --locked agente-sped
uv run --locked ruff check .
uv run --locked ruff format --check .
```

`uv run --locked python -m agente_sped` executa o mesmo ponto de entrada. `agente-sped --help` e `agente-sped --version` estão disponíveis no ambiente instalado.

O código fica em `src/agente_sped`: `interface` contém a CLI e `infra` contém a configuração. As demais responsabilidades serão introduzidas conforme os incrementos. A suíte pytest e a CI serão entregues pelas Issues [#2](https://github.com/campos-labs/agente_sped/issues/2) e [#3](https://github.com/campos-labs/agente_sped/issues/3).

Os comandos e dependências estão em [pyproject.toml](pyproject.toml); as orientações para agentes, em [AGENTS.md](AGENTS.md).

Contribuições seguem o [fluxo da Campos Labs](https://github.com/campos-labs/.github/blob/main/CONTRIBUTING.md). Para relatar vulnerabilidades, consulte a [política de segurança](https://github.com/campos-labs/.github/blob/main/SECURITY.md).

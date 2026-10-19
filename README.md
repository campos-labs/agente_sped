# Agente SPED Fiscal

Análises locais e rastreáveis de EFD ICMS/IPI com `COD_VER=020`.

## Disponível nesta etapa

Parser orientado ao leiaute, catálogo versionado de oito análises e workspace DuckDB local, sem credenciais obrigatórias. A CLI cria um workspace com `analyze` e o reutiliza com `run`.

A cobertura do leiaute é parcial e não substitui a validação pelo PVA. LangGraph, Gemini, relatórios, exportações e interface pertencem aos próximos incrementos. A arquitetura-alvo está em [DESIGN.md](DESIGN.md).

## Executar localmente

Com Python 3.14 e uv, na raiz do repositório:

```bash
uv sync --locked
mkdir workspaces
uv run --locked agente-sped analyze tests/fixtures/sped_icms_analysis_reference.txt --workspace workspaces/exemplo --analysis movement_by_cfop
uv run --locked agente-sped run --workspace workspaces/exemplo --analysis cancelled_documents
```

Crie `workspaces` uma vez e escolha um destino novo para cada ingestão. Após uma falha de consulta, o workspace permanece disponível para `run`. Os resultados são mascarados por padrão; `--show-sensitive` exibe explicitamente as colunas sensíveis.

Para construir a imagem Docker e consultar os comandos disponíveis:

```bash
docker compose build
docker compose run --rm agente-sped python -m agente_sped --help
```

O processamento dispensa `.env` e API key. A imagem não inicia servidor HTTP nem publica portas; arquivos e workspaces usados no contêiner precisam de volumes locais.

## Configuração

Os valores padrão dispensam arquivo de configuração. Se necessário, copie `.env.example` para `.env` ao executar localmente com uv.

As flags `AI_ROUTING_ENABLED`, `AI_NARRATIVE_ENABLED` e `EXPLORATORY_SQL_ENABLED` permanecem `false`. Ativar uma capacidade ainda não implementada produz erro explícito; nenhuma chamada de IA é realizada nesta etapa.

Utilize apenas dados sintéticos em demonstrações, Issues e PRs. Dados locais e credenciais não devem ser versionados.

## Desenvolvimento

Use Python 3.14 e uv. Na raiz do repositório:

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
```

`uv run --locked python -m agente_sped` executa o mesmo ponto de entrada. `agente-sped --help` e `agente-sped --version` estão disponíveis no ambiente instalado.

O código fica em `src/agente_sped`, dividido entre `domain`, `application`, `infra` e `interface`. A suíte inclui fixtures sintéticas e regressões das oito análises; configuração compartilhada e CI seguem nas Issues [#2](https://github.com/campos-labs/agente_sped/issues/2) e [#3](https://github.com/campos-labs/agente_sped/issues/3).

Os comandos e dependências estão em [pyproject.toml](pyproject.toml); as orientações para agentes, em [AGENTS.md](AGENTS.md).

Contribuições seguem o [fluxo da Campos Labs](https://github.com/campos-labs/.github/blob/main/CONTRIBUTING.md). Para relatar vulnerabilidades, consulte a [política de segurança](https://github.com/campos-labs/.github/blob/main/SECURITY.md).

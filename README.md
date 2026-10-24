# Agente SPED Fiscal

Análises locais e rastreáveis de EFD ICMS/IPI com `COD_VER=020`.

## Disponível nesta etapa

Parser orientado ao leiaute, oito análises catalogadas e workspace DuckDB local. A interface web local e a CLI oferecem seleção ou pergunta, orquestração, síntese determinística, sugestões e exportação de resultados salvos, sem credenciais obrigatórias.

A cobertura do leiaute é parcial e não substitui a validação pelo PVA. A arquitetura-alvo está em [DESIGN.md](DESIGN.md).

## Executar localmente

Com Python 3.14 e uv, na raiz do repositório:

```bash
uv sync --locked
uv run --locked agente-sped serve
```

Abra [http://127.0.0.1:8000](http://127.0.0.1:8000), carregue o TXT e use **Perguntar** ou **Executar análise selecionada**. As ações são distintas; selecionar uma análise confirma seus critérios. Dados sensíveis aparecem mascarados, com revelação explícita. CSV e seu contexto têm downloads separados; Excel inclui o contexto no arquivo.

A sessão persiste em `~/.agente-sped/sessions`, configurável por `SPED_SESSION_ROOT`, e expira após 24 horas sem atividade (`SPED_RETENTION_HOURS`). Reabrir o navegador recupera resultados salvos sem recalcular; operações interrompidas não são retomadas. A limpeza explícita remove o arquivo e os resultados da sessão.

Para usar a CLI, em outro terminal:

```bash
mkdir workspaces
uv run --locked agente-sped analyze tests/fixtures/sped_icms_analysis_reference.txt --workspace workspaces/exemplo --analysis movement_by_cfop
uv run --locked agente-sped run --workspace workspaces/exemplo --analysis cancelled_documents
uv run --locked agente-sped ask "Qual foi o valor das saídas por CFOP?" --workspace workspaces/exemplo
uv run --locked agente-sped ask --workspace workspaces/exemplo --analysis cancelled_documents
uv run --locked agente-sped report --workspace workspaces/exemplo --run-id RUN_ID
uv run --locked agente-sped export --workspace workspaces/exemplo --run-id RUN_ID --format xlsx
```

Crie `workspaces` uma vez e escolha um destino novo para cada ingestão. Após uma falha de consulta, o workspace permanece disponível para `run`. Os resultados são mascarados por padrão; `--show-sensitive` exibe explicitamente as colunas sensíveis.

`ask` salva o relatório em `results/` e informa seu `run_id`. Perguntas ambíguas ou com filtros indisponíveis pedem esclarecimento; selecionar o ID confirma a análise completa e seus critérios. Sugestões aguardam seleção e não executam consultas automaticamente. `report` e `export` reutilizam o snapshot, sem consultar novamente o DuckDB ou chamar IA.

Exportações são mascaradas por padrão; dados sem máscara exigem `--show-sensitive` naquela exportação. XLSX contém abas `Dados` e `Contexto`, com decimais e identificadores como texto exato e contagens numéricas. CSV acompanha JSON de contexto; importe códigos como texto. Textos interpretáveis como fórmula são neutralizados no CSV, com a transformação registrada. Resultados truncados exportam somente as linhas salvas, com contagens e limitações explícitas.

Para executar a interface com Docker:

```bash
docker compose up --build
```

Abra o mesmo endereço local. O Compose publica a porta somente em `127.0.0.1` e mantém as sessões em volume nomeado persistente. O processamento dispensa `.env` e API key; credenciais locais não são passadas automaticamente ao contêiner. `docker compose down` preserva as sessões; a opção `--volumes` apaga o volume.

## Configuração

Os valores padrão dispensam arquivo de configuração. Se necessário, copie `.env.example` para `.env` ao executar localmente com uv.

As flags permanecem `false` por padrão. `AI_ROUTING_ENABLED=true` permite roteamento Gemini somente quando a intenção não foi resolvida localmente; configure `GEMINI_API_KEY` no ambiente ou no `.env` ignorado pelo Git. O modelo padrão é `gemini-3.5-flash-lite`, configurável por `GEMINI_MODEL`; `GEMINI_TIMEOUT_SECONDS` aceita 1 a 60 segundos. O provedor recebe uma pergunta minimizada e metadados públicos do catálogo, sem SQL ou resultados fiscais. A remoção de termos desconhecidos pode exigir esclarecimento. Falhas externas mantêm disponível a seleção explícita, sem IA.

`AI_NARRATIVE_ENABLED` e `EXPLORATORY_SQL_ENABLED` continuam indisponíveis; ativá-las produz erro explícito. Os testes usam provedor simulado, sem credenciais ou chamadas reais. Configurações herdadas de tracing e backends incompatíveis são recusadas; o fluxo não envia telemetria.

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

O código fica em `src/agente_sped`, dividido entre `domain`, `application`, `infra`, `orchestration` e `interface`. A suíte inclui fixtures sintéticas, regressões das oito análises, roteamento e exportações; configuração compartilhada e CI seguem nas Issues [#2](https://github.com/campos-labs/agente_sped/issues/2) e [#3](https://github.com/campos-labs/agente_sped/issues/3).

Os comandos e dependências estão em [pyproject.toml](pyproject.toml); as orientações para agentes, em [AGENTS.md](AGENTS.md).

Contribuições seguem o [fluxo da Campos Labs](https://github.com/campos-labs/.github/blob/main/CONTRIBUTING.md). Para relatar vulnerabilidades, consulte a [política de segurança](https://github.com/campos-labs/.github/blob/main/SECURITY.md).

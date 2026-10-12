# Design Architecture: Agente SPED Fiscal

## 🎯 Objetivo (Problema de Negócio)

Permitir que analistas investiguem arquivos EFD ICMS/IPI por perguntas em linguagem natural ou seleção de análises, com resultados reproduzíveis e rastreabilidade até os registros de origem.

O sistema executa análises catalogadas sobre dados locais. A IA é opcional e auxilia a identificação da intenção; cálculos, critérios e relatórios permanecem determinísticos.

O produto apoia a investigação técnica. Não substitui o Programa Validador e Assinador (PVA), não transmite escriturações e não produz parecer fiscal ou jurídico conclusivo.

## 📌 Premissas e Decisões

* **Organização:** Monólito modular com domínio, aplicação, infraestrutura, orquestração e interface separados. Regras analíticas não dependem de FastAPI, LangGraph ou SDKs de IA.
* **Base técnica:** Python 3.14, uv, DuckDB, LangGraph, FastAPI, Jinja2 e HTMX. Dependências efetivas permanecem em `pyproject.toml` e `uv.lock`.
* **Contrato analítico:** `analysis_catalog.yml` concentra as definições versionadas das análises. SQL, parâmetros e critérios não são reconstruídos pelo modelo durante a execução.
* **Operação local:** Um arquivo por workspace, armazenamento no ambiente do usuário e funcionamento sem credenciais de IA. Docker preserva os dados de sessão em volume local.
* **Controle humano:** Cada análise ou exportação decorre de ação explícita do usuário. Sugestões não iniciam procedimentos automaticamente.

## ⚙️ Escopo da Solução

* **Entrada:** TXT EFD ICMS/IPI com `COD_VER=020`, pergunta ou seleção de análise e parâmetros permitidos.
* **Processamento:** Validar arquivo e leiaute; interpretar registros; materializar DuckDB; produzir o perfil; resolver intenção e parâmetros; executar a análise; apresentar resultado, sugestões e exportações.
* **Saída:** Resultado estruturado, síntese determinística, critérios aplicados, limitações, evidências de origem e arquivos CSV/Excel.
* **Cobertura:** Oito análises individuais de referência do catálogo, sem duplicar suas regras ou gabaritos neste documento.
* **Fora do Escopo:** Execução de Text-to-SQL, análises compostas, múltiplos arquivos por workspace, outras obrigações, auditorias cruzadas, RAG, multiagentes, PDF, autenticação corporativa, API pública e processamento distribuído.

## 👥 Usuários e Papéis

* **Analista Fiscal:** Seleciona o arquivo, formula perguntas, confirma parâmetros, inspeciona evidências e decide os próximos passos.
* **Operador Local:** Mantém ambiente, catálogos, retenção de sessões e configuração opcional do provedor.
* **Provedor de IA:** Propõe intenções e parâmetros estruturados; não acessa o DuckDB, escolhe caminhos de arquivos ou executa cálculos.

## 🧩 Modelo de Domínio e Dados

### Leiaute e Persistência

* **Catálogo de leiaute:** `layout_catalog.duckdb` é derivado do CSV controlado e de seu builder versionado. Origem, snapshot e transformações permanecem rastreáveis.
* **Workspace analítico:** DuckDB em arquivo contém as relações interpretadas. É separado do catálogo de leiaute e reconstruível a partir do TXT e das versões dos componentes utilizados.
* **Identidade:** `record_id` é inteiro sequencial por arquivo; `(file_id, record_id)` identifica o registro. `source_line` preserva a linha original; hash identifica o conteúdo, não substitui esses IDs.
* **Hierarquia:** `parent_record_id` e `parent_record_code` representam somente o pai estrutural direto. Relações entre registros de negócio são declaradas nas consultas catalogadas.
* **Tipagem:** `raw_values` e `typed_values` permanecem distintos. Valores monetários usam Decimal; datas são validadas; códigos e identificadores preservam zeros e caracteres, incluindo CNPJ alfanumérico. Valores inválidos não viram zero silenciosamente.

### Contratos Estruturais

Pydantic valida as fronteiras da aplicação e as respostas do provedor; os contratos do domínio permanecem independentes dos adaptadores.

* **Workspace:** Identidade, arquivo, hash, leiaute, versões de ingestão, localização privada do DuckDB, estado e atividade da sessão.
* **FileProfile:** Período, contagens por registro e anomalias estruturais ou de campos, sem inferências fiscais.
* **ParsedRecord:** Código, nível, identidade, linha de origem, pai direto, valores brutos, valores tipados e erros de campo.
* **AnalysisDefinition:** Definição em `analysis_catalog.yml`: identidade, versão, roteamento, disponibilidade, parâmetros, critérios, SQL, resultado, privacidade, narrativa e limitações.
* **AnalysisRequest / RoutingDecision:** Solicitação do usuário e decisão de rota, com `analysis_id`, parâmetros propostos e eventual necessidade de esclarecimento.
* **AnalysisResult:** `run_id`, análise e versão, parâmetros efetivos, estado, métricas, referências ao resultado local e evidências. Contagens e truncamento distinguem total conhecido de linhas retornadas.
* **ReportPackage:** Representação comum para HTML, CSV e Excel, contendo resultado, contexto da execução, critérios, limitações e rastreabilidade.
* **SuggestedAnalysis:** Rótulo, `analysis_id` e parâmetros permitidos para uma pílula executável.
* **NarrativePayload:** Projeção reduzida e mascarada do resultado, com critérios, limitações e contagens; não substitui o resultado local.
* **AgentState:** Estado leve por execução, com intenção, rota, IDs, parâmetros validados, referências de resultado e erros controlados. Não contém TXT integral, DataFrames, conexões, resultados irrestritos ou credenciais.

### Schema versus Ocorrência

Uma relação conhecida pode existir vazia para preservar o schema do leiaute. A disponibilidade considera campos exigidos, ocorrências reais e `record_presence_required`; ausência de enriquecimento opcional, resultado vazio e análise indisponível são situações distintas.

Erros que comprometam a estrutura ou campos necessários à análise impedem seu processamento; as demais anomalias permanecem identificadas, sem descarte silencioso.

### Ciclo de Vida do Workspace

* **Persistência:** TXT, `analysis.duckdb`, resultados concluídos e metadados ficam em diretório privado por workspace, fora do Git. Um `session.json` mínimo, gravado atomicamente, mantém atividade e referências necessárias à retomada.
* **Retomada:** Reabrir a interface recupera o workspace e os resultados disponíveis em melhor esforço. Não exige nova chamada à IA nem promete continuar uma execução interrompida.
* **Compatibilidade:** Versões de parser, leiaute e catálogo acompanham as execuções. Uma incompatibilidade exige reprocessamento explícito, sem apresentar resultados antigos como recém-calculados.
* **Retenção:** Expiração configurável por inatividade, inicialmente 24 horas. Limpeza oportunista na inicialização e nos acessos remove workspaces expirados que não estejam em uso.
* **Descarte:** Fechar a aba não encerra a sessão. Reiniciar ou limpar exige confirmação e remove os dados associados por operação idempotente.

## 🔌 Serviços de Aplicação e Integrações

* **Loader dos catálogos:** Valida schema, tipos, identificadores, parâmetros e compatibilidade. Chaves duplicadas, colisões de campos ou referências inválidas são recusadas antes da execução.
* **Builders e Parser:** Compilam metadados e interpretam o TXT segundo o leiaute suportado, preservando identidade, hierarquia e valores originais. Versões desconhecidas são recusadas.
* **WorkspaceManager:** Controla ingestão, materialização, reabertura e limpeza. Somente disponibiliza relações após ingestão consistente; escritas e descarte são coordenados por workspace.
* **Roteador Analítico:** Prioriza seleção explícita e correspondência determinística. Quando habilitada, a IA auxilia intenções ambíguas; análise e parâmetros retornados passam pela mesma validação local.
* **Executor Analítico:** Executa somente SQL de leitura presente no catálogo, com parâmetros vinculados, uma instrução por execução e limites configurados. Não recebe SQL arbitrário da interface ou do provedor.
* **Relatórios e Exportadores:** Constroem `ReportPackage`, texto determinístico e arquivos CSV/Excel. A exportação reutiliza uma execução identificada, sem chamar o LLM ou recalcular silenciosamente a análise.
* **Adaptador de IA:** Integra Gemini por `langchain-google-genai`, com Structured Outputs validados por Pydantic. Modelo e parâmetros são configuráveis; falhas externas não impedem seleção e execução locais.

### Orquestração LangGraph

`StateGraph` mantém nós explícitos para `ROUTE_INTENT`, `VALIDATE_CONTRACT`, `EXECUTE_CATALOG`, `BUILD_REPORT` e `SUGGEST_NEXT`. `EXPORT_REPORT` coordena a exportação sob solicitação, usando o resultado já produzido.

Os nós delegam aos serviços de aplicação; regras, cálculos e autorização não ficam embutidos nas arestas. Ações simples de interface, como copiar ou reconhecer cortesia, não acionam o provedor.

As transições distinguem `out_of_catalog`, `needs_clarification`, `analysis_unavailable`, `empty_result`, `execution_error` e `provider_failure`. Falha técnica do provedor não equivale a pergunta fora do catálogo.

### Feature Flags

* **`AI_ROUTING_ENABLED=false`:** Controla o roteamento assistido, sem afetar o caminho determinístico.
* **`AI_NARRATIVE_ENABLED=false`:** Reserva o enriquecimento narrativo sobre `NarrativePayload`; a saída suportada é determinística.
* **`EXPLORATORY_SQL_ENABLED=false`:** Reserva uma rota exploratória independente, sem registrar o executor SQL livre legado no grafo ativo.

Flags não substituem implementação, validação ou autorização. Ativar uma capacidade ainda não implementada gera erro explícito de configuração. Controles da interface não habilitam recursos bloqueados.

## 📡 Contratos de API e Interface

* **Interface local:** FastAPI serve HTML e atualizações parciais com Jinja2/HTMX. Upload, análise, recuperação e exportação usam os mesmos serviços de aplicação, sem API pública ou webhooks.
* **Experiência:** Painel de interação menor para arquivo, chat, sugestões e estado; área principal para perfil, resultados, critérios, tabelas, SQL e histórico recente da sessão.
* **Pílulas e parâmetros:** Sugestões apontam para análises disponíveis e parâmetros autorizados. Ambiguidades pedem esclarecimento; a interface apresenta a análise escolhida e os filtros efetivos.
* **Contratos HTTP:** Entrada tipada, erros controlados e referências por IDs. Datas são serializadas em ISO 8601; valores decimais preservam precisão, com formatação localizada apenas na apresentação.
* **Sessão do navegador:** Cookie persistente e opaco, `HttpOnly` e `SameSite`, identifica a sessão. Caminhos locais, TXT e resultados não são persistidos em cookies ou armazenamento web.
* **Runtime:** `lifespan` gerencia recursos da aplicação. Operação em processo local único, com execução controlada e escrita coordenada; sem broker, fila durável ou dependência de checkpoints LangGraph para recuperar a tela.

## 🛡️ Segurança e Restrições

* **Exposição local:** A interface fica restrita ao loopback do host, inclusive na publicação de portas do Docker. Requisições que alteram estado possuem validação de origem e proteção contra CSRF.
* **Arquivos e consultas:** A aplicação resolve caminhos sob sua raiz privada, valida limites de upload e restringe o executor às relações autorizadas. Leitura analítica não concede escrita, acesso arbitrário ao filesystem, rede ou extensões.
* **Minimização:** TXT e resultados completos permanecem locais. O roteamento externo recebe somente pergunta higienizada e projeção necessária do catálogo, sem SQL, gabaritos ou dados transacionais. Mascaramento ocorre antes do envio.
* **Apresentação e exportação:** Visualização mascarada por padrão; evidências não mascaradas exigem ação explícita. HTML escapa conteúdo e exportadores neutralizam texto interpretável como fórmula, preservando códigos e identificadores.
* **Retenção e segredos:** Sessões expiradas deixam de ser acessíveis. Dados reais, resultados, credenciais e `.env` não são versionados nem enviados à telemetria. Logs não expõem conteúdo fiscal ou tokens de sessão.
* **Limites efetivos:** Tamanho de arquivo, tempo de consulta, linhas e payloads têm limites aplicados pelo runtime. A guarda de SQL catalogado não é uma sandbox para consultas produzidas por terceiros.

## ⚖️ Trade-offs Arquiteturais

* **TXT direto versus staging:** Consultar o TXT diretamente evita materialização, mas repete leitura e interpretação. DuckDB permite reutilizar o workspace; o TXT continua sendo a fonte para reconstrução.
* **DuckDB versus Parquet:** DuckDB reúne persistência e execução local. Parquet adicionaria outra representação sem benefício necessário ao escopo; a escolha não implica superioridade universal.
* **Catálogo versus geração SQL:** Consultas conhecidas ficam versionadas e testáveis. Contexto e few-shots podem apoiar exploração, mas não justificam regenerar obrigatoriamente as regras já definidas.
* **LangGraph versus fluxo Python direto:** Nós e transições tornam a orquestração explícita; os serviços permanecem utilizáveis e testáveis fora do grafo.
* **Persistência versus cache:** Reutilizar o workspace evita a principal recomputação. Cache de resultados/LLM, checkpoints persistentes e serviços distribuídos não são requisitos da solução.

## 🧪 Observabilidade, Testes e DoD

* **Testes:** Pytest com fixtures sintéticas cobre leiaute, tipagem, hierarquia, CNPJ alfanumérico, catálogo, parâmetros, oito gabaritos, disponibilidade, limites, mascaramento e exportações.
* **Fluxos:** Testes de aplicação e interface cobrem roteamento, pílulas, flags, falhas de provedor, retomada, expiração e limpeza. Categorias de decisão são avaliadas separadamente da identificação da análise.
* **CI:** GitHub Actions executa Ruff e pytest nos PRs e na `main`, com dependências fixadas pelo `uv.lock`, sem credenciais ou chamadas reais de IA. Avaliações live permanecem separadas e explicitamente autorizadas.
* **Observabilidade:** Logs registram IDs técnicos, versões, duração, contagens, rota e erros higienizados. O modelo utilizado é identificado quando houver chamada externa; prompts e resultados completos não entram nos logs.
* **Definition of Done:** Ingestão consistente, oito análises reproduzindo os gabaritos, estados de falha distinguíveis, relatórios/exportações rastreáveis e sessão recuperável dentro da retenção. O fluxo funciona sem IA e os checks automatizados passam.

## 📎 Referências

* **Portal SPED — Receita Federal:** documentação oficial, manuais, guias e documentos técnicos das escriturações digitais — https://www.gov.br/sped/pt-br.

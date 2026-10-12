# Agente SPED Fiscal

Análises rastreáveis de EFD ICMS/IPI em uma interface local, com consultas reproduzíveis e IA opcional na triagem.

Carregue uma escrituração, selecione uma análise ou formule uma pergunta. Consulte resultados, critérios aplicados e registros de origem; exporte as evidências em CSV ou Excel sem depender de geração livre de SQL.

## O que você pode analisar

As oito análises do catálogo cobrem:

- **Operações:** valores de saída por CFOP e concentração de compras por fornecedor.
- **Documentos:** cancelamentos e outras situações documentais, possíveis duplicidades e lacunas de numeração.
- **Produção:** atividade declarada no Bloco K.
- **Revisão fiscal e cadastral:** itens de alto valor com NCM genérico e candidatos à investigação de ICMS em operações de uso ou consumo.

Cada resultado apresenta parâmetros, critérios, limitações e referências à escrituração. Indícios orientam a revisão do analista; não são conclusões automáticas de irregularidade.

## Executar localmente

Com Docker e Docker Compose disponíveis:

```bash
git clone https://github.com/campos-labs/agente_sped.git
cd agente_sped
docker compose up --build
```

Abra o endereço local informado na inicialização. Para conhecer o fluxo, utilize a fixture sintética `sped_icms_analysis_reference.txt` da suíte de testes.

Envie o TXT, confira o período e o perfil do arquivo e escolha uma análise. A interface reúne perguntas e sugestões em um painel, com tabelas, síntese, SQL e evidências na área principal. As pílulas permitem continuar a investigação; exportações reutilizam o resultado selecionado.

## IA opcional, resultados determinísticos

O funcionamento padrão dispensa credenciais de IA. Com o provedor configurado e `AI_ROUTING_ENABLED=true`, o Gemini auxilia a identificação de intenções ambíguas; a análise e seus parâmetros continuam sujeitos à validação local.

SQL, cálculos e respostas analíticas seguem o catálogo versionado. O modelo não consulta livremente a base nem decide quais procedimentos executar sem ação do usuário.

O TXT e os resultados completos permanecem no ambiente local. Quando habilitada, a triagem externa recebe somente a pergunta higienizada e o contexto mínimo necessário do catálogo.

## Sessão e privacidade

Fechar a aba não apaga o trabalho. A sessão pode ser recuperada no mesmo navegador enquanto o identificador e os dados locais estiverem disponíveis, com retenção padrão de 24 horas por inatividade, configurável.

A ação de limpar remove o arquivo e os dados associados ao workspace. A retomada recupera resultados concluídos; não garante continuação de uma execução interrompida.

A interface é destinada ao uso local, sem exposição pública. Utilize apenas dados sintéticos em demonstrações, Issues e PRs.

## Escopo

Suporta EFD ICMS/IPI com `COD_VER=020`, um arquivo por workspace e análises individuais catalogadas. Não executa consultas SQL livres, cruzamentos entre arquivos ou outras obrigações SPED.

O produto apoia investigação técnica: não substitui o PVA, não transmite escriturações e não emite parecer fiscal ou jurídico conclusivo.

## Desenvolvimento

Python 3.14, uv, DuckDB e LangGraph; interface com FastAPI, Jinja2 e HTMX. Testes com pytest e qualidade de código com Ruff.

Para preparar o ambiente de desenvolvimento:

```bash
uv sync --locked
```

A arquitetura está em [DESIGN.md](DESIGN.md), os comandos e configurações em [pyproject.toml](pyproject.toml) e as orientações para agentes de desenvolvimento em [AGENTS.md](AGENTS.md).

Contribuições seguem o [fluxo da Campos Labs](https://github.com/campos-labs/.github/blob/main/CONTRIBUTING.md). Para relatar vulnerabilidades, consulte a [política de segurança](https://github.com/campos-labs/.github/blob/main/SECURITY.md).

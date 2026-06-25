# agente_sped/agent/graph.py
from langgraph.graph import END, START, StateGraph

from agente_sped.agent_.nodes.executor import execute_query_node
from agente_sped.agent_.nodes.formatter import format_response_node
from agente_sped.agent_.nodes.generator import generate_sql_node
from agente_sped.agent_.state import SPEDAgentState


def router_edge(state: SPEDAgentState) -> str:
    """
    Roteador condicional que analisa a última mensagem do estado.
    Se a LLM decidiu que precisa rodar uma query (tool_calls), envia para o executor.
    Se a LLM respondeu diretamente ou já processou os dados, envia para o formatador.
    """
    last_message = state["messages"][-1]

    # Se a última mensagem contiver solicitações de ferramentas (tool_calls)
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:  # type: ignore
        return "execute_query"

    # Caso contrário, o fluxo de dados terminou e podemos formatar a resposta
    return "format_response"


# 1. Inicializa o construtor do grafo passando a nossa estrutura de State refatorada
workflow = StateGraph(SPEDAgentState)

# 2. Adiciona os Nós (Nodes) correspondentes aos blocos do desenho
workflow.add_node("LLM_GENERATE_SQL", generate_sql_node)
workflow.add_node("DUCKDB_EXECUTE_QUERY", execute_query_node)
workflow.add_node("LLM_FORMAT_RESPONSE", format_response_node)

# 3. Estabelece o ponto de entrada do fluxo
workflow.add_edge(START, "LLM_GENERATE_SQL")

# 4. Define a lógica condicional saindo do Gerador de SQL
# Se o router_edge retornar "execute_query", vai para o nó DUCKDB_EXECUTE_QUERY
# Se retornar "format_response", vai direto para o nó LLM_FORMAT_RESPONSE
workflow.add_conditional_edges(
    "LLM_GENERATE_SQL",
    router_edge,
    {
        "execute_query": "DUCKDB_EXECUTE_QUERY",
        "format_response": "LLM_FORMAT_RESPONSE"
    }
)

# 5. Após executar a query no DuckDB, voltamos para o Gerador (Loop de Auto-Correção / Próximo Passo)
# A LLM verá o resultado do JSON (ou o erro) no histórico e decidirá o que fazer a seguir
workflow.add_edge("DUCKDB_EXECUTE_QUERY", "LLM_GENERATE_SQL")

# 6. Uma vez formatada a resposta final, o grafo encerra a execução
workflow.add_edge("LLM_FORMAT_RESPONSE", END)

# 7. Compila o grafo para transformá-lo em uma aplicação executável (.invoke())
graph = workflow.compile()

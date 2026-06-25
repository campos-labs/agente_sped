from langgraph.graph import END, START, StateGraph
from agente_sped.agent_.state import SPEDAgentState
from agente_sped.agent_.nodes.executor import execute_query_node 
from agente_sped.agent_.nodes.formatter import format_response_node
from agente_sped.agent_.nodes.generator import generate_sql_node

MAX_ITERATIONS = 5  # limite de ciclos SQL

def router_edge(state: SPEDAgentState) -> str:
    last_message = state["messages"][-1]
    iterations = state.get("iterations", 0)

    # Guardrail: se excedeu iterações, força formatação da resposta
    if iterations >= MAX_ITERATIONS:
        return "format_response"

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "execute_query"

    return "format_response"


def increment_iterations(state: SPEDAgentState) -> dict:
    """Nó intermediário para contar iterações."""
    return {"iterations": state.get("iterations", 0) + 1}


workflow = StateGraph(SPEDAgentState)

workflow.add_node("LLM_GENERATE_SQL", generate_sql_node)
workflow.add_node("DUCKDB_EXECUTE_QUERY", execute_query_node)
workflow.add_node("LLM_FORMAT_RESPONSE", format_response_node)
workflow.add_node("INCREMENT_ITER", increment_iterations)  # novo

workflow.add_edge(START, "LLM_GENERATE_SQL")

workflow.add_conditional_edges(
    "LLM_GENERATE_SQL",
    router_edge,
    {
        "execute_query": "DUCKDB_EXECUTE_QUERY",
        "format_response": "LLM_FORMAT_RESPONSE",
    },
)

# Incrementa antes de voltar ao LLM
workflow.add_edge("DUCKDB_EXECUTE_QUERY", "INCREMENT_ITER")
workflow.add_edge("INCREMENT_ITER", "LLM_GENERATE_SQL")

workflow.add_edge("LLM_FORMAT_RESPONSE", END)

# Limite de segurança explícito no compile
graph = workflow.compile()
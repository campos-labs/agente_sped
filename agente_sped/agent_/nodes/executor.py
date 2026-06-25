# agente_sped/agent/nodes/executor.py
import json

from langchain_core.messages import ToolMessage

from agente_sped.agent_.state import SPEDAgentState
from agente_sped.database.duckdb_client import execute_duckdb_query


def execute_query_node(state: SPEDAgentState):
    last_message = state["messages"][-1]

    # Caso de salvaguarda se o nó for chamado sem um tool_call
    if not last_message.tool_calls:  # type: ignore
        return {"sql_error": "Nenhuma ferramenta foi solicitada pela LLM."}

    tool_call = last_message.tool_calls[0]  # type: ignore

    # Executa a nossa ferramenta passando o caminho do arquivo guardado no State
    tool_output_str = execute_duckdb_query.invoke({
        "sped_file_path": state["sped_file_path"],
        "sql_query": tool_call["args"]["sql_query"]
    })

    # Trata a resposta da ferramenta
    try:
        data = json.loads(tool_output_str)
    except Exception:
        data = {"error": "Falha ao decodificar retorno do DuckDB"}

    # Verifica se a ferramenta retornou uma mensagem de erro interna do DuckDB
    sql_error = ""
    duckdb_result = []
    if isinstance(data, dict) and "error" in data:
        sql_error = data["error"]
        print(f"\n🚨 [ERRO DUCKDB]: {data.get('details', sql_error)}")
    else:
        duckdb_result = data

    # Cria a mensagem de resposta da Tool exigida pelo LangGraph para manter o histórico íntegro
    tool_message = ToolMessage(
        content=tool_output_str,
        tool_call_id=tool_call["id"],
        name=tool_call["name"]
    )

    return {
        "messages": [tool_message],
        "duckdb_result": duckdb_result,
        "sql_error": sql_error
    }

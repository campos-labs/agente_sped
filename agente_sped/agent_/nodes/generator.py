from langchain_core.prompts import ChatPromptTemplate

from agente_sped.agent_.prompts.generator_prompt import generator_prompt
from agente_sped.agent_.state import SPEDAgentState
from agente_sped.config.llm_config import call_llm
from agente_sped.database.duckdb_client import execute_duckdb_query


def generate_sql_node(state: SPEDAgentState):
    llm = call_llm()

    # SEM tool_choice: o modelo decide se chama a tool ou responde diretamente
    llm_with_tools = llm.bind_tools([execute_duckdb_query])

    prompt = ChatPromptTemplate.from_messages([
        ("system", generator_prompt.content),
        ("placeholder", "{messages}")
    ])

    chain = prompt | llm_with_tools
    response = chain.invoke({"messages": state["messages"]})

    generated_sql = ""
    if response.tool_calls:
        generated_sql = response.tool_calls[0]["args"].get("sql_query", "")

    return {
        "messages": [response],
        "generated_sql": generated_sql,
    }

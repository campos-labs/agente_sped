# agente_sped/agent/nodes/formatter.py
from agente_sped.agent_.prompts.formatter_prompt import formatter_prompt
from agente_sped.agent_.state import SPEDAgentState
from agente_sped.config.llm_config import call_llm


def format_response_node(state: SPEDAgentState):
    llm = call_llm()

    # Prompt instruindo a IA a interpretar os dados fiscais obtidos
    system_instruction = formatter_prompt

    # Invocamos a LLM passando todo o histórico (onde agora constam os dados da ferramenta)
    messages_to_send = [system_instruction] + state["messages"]
    response = llm.invoke(messages_to_send)

    return {"messages": [response]}

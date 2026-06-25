from typing import Annotated, Any, Dict, List, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class SPEDAgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    sped_file_path: str
    generated_sql: str
    duckdb_result: List[Dict[str, Any]]
    sql_error: str

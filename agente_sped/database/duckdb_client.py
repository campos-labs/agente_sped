# agente_sped/database/duckdb_client.py
import json
import re

import duckdb
from langchain_core.tools import tool


@tool
def execute_duckdb_query(sped_file_path: str, sql_query: str) -> str:
    """
    Executa uma query SQL analítica diretamente em um arquivo de SPED Fiscal (.txt) utilizando o DuckDB.
    """
    conn = None
    try:
        conn = duckdb.connect(database=":memory:")

        # 1. Limpa as aspas duplas que a LLM costuma usar erroneamente para strings (ex: "C100" -> 'C100')
        # Buscamos por valores de igualdade comuns no SPED para corrigir a sintaxe
        sql_query = re.sub(r'=\s*"([^"]+)"', r"= '\1'", sql_query)

        # 2. Intercepta a chamada do read_csv e força os parâmetros de segurança corretos,
        # ignorando completamente o que a LLM tentou passar ali dentro.
        safe_read_csv = f"read_csv('{sped_file_path}', delim='|', header=False, all_varchar=True, sample_size=-1, null_padding=True)"

        # Substitui todo o bloco 'read_csv(...)' original pela nossa versão blindada
        formatted_query = re.sub(
            r"read_csv\([^)]+\)",
            safe_read_csv,
            sql_query,
            flags=re.IGNORECASE
        )

        # Opcional: Se quiser ver no terminal como ficou a query higienizada antes de rodar
        # print(f"\n⚡ [QUERY EXECUTADA]: {formatted_query}")

        # Executa no DuckDB
        cursor = conn.execute(formatted_query)

        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()

        result = [dict(zip(columns, row)) for row in rows]
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)

    except Exception as e:
        error_payload = {
            "error": "Falha na execução do DuckDB",
            "details": str(e),
            "attempted_query": sql_query
        }
        return json.dumps(error_payload, ensure_ascii=False)

    finally:
        if conn is not None:
            conn.close()

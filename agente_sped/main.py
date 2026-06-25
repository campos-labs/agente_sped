import os

from langchain_core.messages import HumanMessage

from agente_sped.agent_.graph import graph


def run_agent_cli():
    print("==================================================")
    print("BEM-VINDO AO AGENTE SPED FISCAL -- Campos-Labs")
    print("==================================================")

    sped_file_path = "data/exemplo_sped.txt"

    if not os.path.exists(sped_file_path):
        print(f"❌ Erro: O arquivo '{sped_file_path}' não foi encontrado.")
        print("Por favor, verifique o caminho definido na variável 'sped_file_path'.")
        return

    print(f"📂 Arquivo configurado para análise: {sped_file_path}")
    print("Digite 'sair' para encerrar o programa.\n")

    while True:
        try:
            user_question = input("👤 Pergunta: ")
            if user_question.strip().lower() == "sair":
                print("\n🤖 Até logo!")
                break

            if not user_question.strip():
                continue

            # Monta o estado inicial injetando a variável local fixa
            initial_state = {
                "messages": [HumanMessage(content=user_question)],
                "sped_file_path": sped_file_path,
                "generated_sql": "",
                "duckdb_result": [],
                "sql_error": ""
            }

            print("⚙️  O agente está processando o arquivo com DuckDB...")

            # Executa o grafo de ponta a ponta
            final_state = graph.invoke(initial_state)  # type: ignore

            # Captura a resposta final (última mensagem do histórico)
            final_message = final_state["messages"][-1].content

            print(f"\n🤖 Resposta:\n{final_message}\n")
            print("-" * 50)

        except Exception as e:
            print(f"\n❌ Ocorreu um erro durante a execução do grafo: {e}\n")


if __name__ == "__main__":
    run_agent_cli()

# Qual foi o valor total faturado de notas fiscais de saída (C100) no mês e quanto gerou de ICMS?

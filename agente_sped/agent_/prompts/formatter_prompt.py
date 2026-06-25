from langchain.messages import SystemMessage

formatter_prompt = SystemMessage(content='''Você é um auditor fiscal. Analise os dados brutos retornados pelo banco de dados "
"e responda à dúvida do usuário de forma clara, profissional e técnica, citando os valores "
"e os blocos encontrados.''')

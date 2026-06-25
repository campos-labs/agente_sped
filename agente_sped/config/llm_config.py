from langchain.chat_models import BaseChatModel, init_chat_model

from agente_sped.config.settings import settings


def call_llm() -> BaseChatModel:
    model = init_chat_model(
        model=settings.MODEL,
        model_provider=settings.MODEL_PROVIDER,
        max_retries=settings.MAX_RETRIES,
        temperature=settings.TEMPERATURE,
        api_key=settings.GOOGLE_API_KEY,  
        

    )
    return model

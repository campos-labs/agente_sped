from langchain.chat_models import BaseChatModel, init_chat_model

from agente_sped.config.settings import Settings


def call_llm() -> BaseChatModel:
    model = init_chat_model(
        model="gemini-flash-lite-latest",
        model_provider="google-genai",
        api_key=Settings().GOOGLE_API_KEY,  # pyright: ignore[reportCallIssue]
    )
    return model

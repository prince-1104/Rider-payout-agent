"""Configured LangChain structured chat adapter; no credentials enter prompts."""

import logging
from functools import lru_cache

from langchain_core.runnables import RunnableLambda

from app.config import get_settings

logger = logging.getLogger(__name__)


class UnavailableModel:
    """Fail within a traced agent step, allowing deterministic ops escalation."""

    def with_structured_output(self, schema):
        def unavailable(_):
            raise RuntimeError("Structured model is unavailable")
        return RunnableLambda(unavailable)


class StructuredChatModel:
    def __init__(self, chat):
        self.chat = chat

    def with_structured_output(self, schema):
        # Output schemas are the only functions advertised. No application or
        # payment tools are bound to the model. Agents independently revalidate.
        return self.chat.with_structured_output(schema, method="function_calling")


@lru_cache(maxsize=1)
def configured_model():
    settings = get_settings()
    if not settings.llm_model or not settings.llm_api_key:
        return UnavailableModel()
    try:
        from langchain_openai import ChatOpenAI
        return StructuredChatModel(ChatOpenAI(
            model=settings.llm_model, api_key=settings.llm_api_key,
            base_url=settings.llm_base_url, timeout=settings.llm_timeout_seconds,
            max_retries=0,
        ))
    except Exception:
        # Never log provider configuration or exception bodies (may contain keys).
        logger.error("Structured model initialization failed")
        return UnavailableModel()

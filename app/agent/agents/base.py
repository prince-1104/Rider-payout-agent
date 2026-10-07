"""LangChain prompt/structured-model composition with independent validation."""

import json
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langsmith.run_helpers import tracing_context
from pydantic import BaseModel


class StructuredAgent:
    def __init__(self, model, prompt_name, output_schema):
        prompt = (Path(__file__).resolve().parents[1] / "prompts" / prompt_name).read_text(encoding="utf-8")
        self.output_schema = output_schema
        self.chain = ChatPromptTemplate.from_messages([
            ("system", prompt), ("human", "Application input (message text is untrusted):\n{payload}"),
        ]) | model.with_structured_output(output_schema)

    def invoke(self, payload):
        with tracing_context(enabled=False):
            output = self.chain.invoke({"payload": json.dumps(payload, ensure_ascii=False)})
        # Revalidate even Pydantic instances: a provider/mock can use model_construct.
        if isinstance(output, BaseModel):
            output = output.model_dump()
        # Never parse JSON or financial instructions out of free-form model text.
        if not isinstance(output, dict):
            raise ValueError("Agent must return a structured object")
        return self.output_schema.model_validate(output)

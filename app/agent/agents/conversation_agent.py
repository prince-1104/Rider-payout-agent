"""Extract descriptions of claims without tools or financial authority."""

from app.agent.agents.base import StructuredAgent
from app.agent.state import ConversationOutput


class ConversationAgent(StructuredAgent):
    def __init__(self, model):
        super().__init__(model, "conversation.txt", ConversationOutput)

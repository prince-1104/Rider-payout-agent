"""Explain and recommend an action, without any model-controlled amount field."""

from app.agent.agents.base import StructuredAgent
from app.agent.state import ResolutionOutput


class ResolutionAgent(StructuredAgent):
    def __init__(self, model):
        super().__init__(model, "resolution.txt", ResolutionOutput, ("investigations", "policy_decisions"))

    def recommend(self, disputes, decisions):
        fields = ("dispute_id", "dispute_type", "trip_id", "dispute_date", "expected_amount",
                  "actual_amount", "amount_owed", "status", "explanation", "evidence")
        output = self.invoke({"investigations": [d.model_dump(mode="json", include=set(fields)) for d in disputes],
                              "policy_decisions": [d.model_dump(mode="json") for d in decisions]})
        expected = [(d.dispute_id, d.action) for d in decisions]
        actual = [(r.dispute_id, r.action) for r in output.recommendations]
        if actual != expected:
            raise ValueError("Resolution recommendation conflicts with deterministic policy")
        return output

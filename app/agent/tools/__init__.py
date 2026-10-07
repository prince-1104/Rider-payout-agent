"""Construct per-request read capabilities from trusted application context.

Only ``read_tools`` should ever be passed to a model. Write operations remain in
application services and are not LangChain tools or present in the registry.
"""

from dataclasses import dataclass
from langchain_core.tools import BaseTool
from app.agent.tools import dispute_tools, payout_tools, payswift_tools, policy_tools, rider_tools, trip_tools
from app.agent.tools.base import ToolContext


@dataclass(frozen=True)
class ToolSet:
    read_tools: tuple[BaseTool, ...]
    write_tools: tuple[BaseTool, ...] = ()


def build_toolset(context: ToolContext) -> ToolSet:
    return ToolSet(tuple(tool for module in (
        rider_tools, trip_tools, payout_tools, dispute_tools, policy_tools, payswift_tools,
    ) for tool in module.build_tools(context)))

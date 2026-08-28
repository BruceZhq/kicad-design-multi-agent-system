from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph

from agents.ratsnestpro.ratsnestpro_agent import ratsnestpro_multi_agent
from agents.ratsnestpro.single_agent_eval import ratsnestpro_single_agent_eval
from core import settings
from schema import AgentInfo

DEFAULT_AGENT = "ratsnestpro-multi-agent"

# Type alias to handle LangGraph's different agent patterns
# - @entrypoint functions return Pregel
# - StateGraph().compile() returns CompiledStateGraph
AgentGraph = CompiledStateGraph


@dataclass
class Agent:
    description: str
    graph: AgentGraph


agents: dict[str, Agent] = {
    "ratsnestpro-multi-agent": Agent(
        description=(
            "A supervised multi-agent PCB team for RatsNestPro design, generation, "
            "verification, review, repair, and grounded part search."
        ),
        graph=ratsnestpro_multi_agent,
    ),
    "ratsnestpro-single-agent-eval": Agent(
        description=(
            "Internal paired-evaluation control: one continuous-context agent using "
            "the production evidence tools, Temporal pipeline, and deterministic gates."
        ),
        graph=ratsnestpro_single_agent_eval,
    ),
}


def _enabled(agent_id: str) -> bool:
    return (
        agent_id != "ratsnestpro-single-agent-eval"
        or settings.RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED
    )


async def load_agent(agent_id: str) -> None:
    """Validate the fixed production agent identifier."""
    get_agent(agent_id)


def get_agent(agent_id: str) -> AgentGraph:
    """Get the fixed RatsNestPro production graph."""
    if not _enabled(agent_id):
        raise KeyError(agent_id)
    return agents[agent_id].graph


def get_all_agent_info() -> list[AgentInfo]:
    return [
        AgentInfo(key=agent_id, description=agent.description)
        for agent_id, agent in agents.items()
        if _enabled(agent_id)
    ]

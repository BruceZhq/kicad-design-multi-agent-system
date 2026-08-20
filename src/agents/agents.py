from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph

from agents.ratsnestpro.ratsnestpro_agent import ratsnestpro_multi_agent
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
}


async def load_agent(agent_id: str) -> None:
    """Validate the fixed production agent identifier."""
    get_agent(agent_id)


def get_agent(agent_id: str) -> AgentGraph:
    """Get the fixed RatsNestPro production graph."""
    return agents[agent_id].graph


def get_all_agent_info() -> list[AgentInfo]:
    return [
        AgentInfo(key=agent_id, description=agent.description) for agent_id, agent in agents.items()
    ]

"""The two typed seams of the AHE system: brains propose, backends build."""

from pathlib import Path

import pytest

from ratsnest.config import Config
from ratsnest.protocols import DesignBackend, LlmBrain


class FakeBrain:
    @property
    def available(self) -> bool:
        return True

    def complete_json(self, agent, system, user, max_tokens=2000):
        return {}


def test_fake_and_real_brains_satisfy_llm_brain():
    from ratsnest.llm import LlmClient
    assert isinstance(FakeBrain(), LlmBrain)
    assert isinstance(LlmClient(Config.load()), LlmBrain)


def test_all_three_backends_satisfy_design_backend():
    from ratsnest.crews import CreatorCrew
    from ratsnest.design_gen.generator import TemplateBackend
    from ratsnest.mcp_exec import KiCadMcpBackend
    config = Config.load()
    assert isinstance(TemplateBackend(config), DesignBackend)
    assert isinstance(CreatorCrew(config, None), DesignBackend)
    assert isinstance(KiCadMcpBackend(config, None), DesignBackend)


def test_registry_rejects_unknown_backend(tmp_path):
    from ratsnest.pipeline import generate_for_backend
    from ratsnest.schemas import StrategyBundle
    strategy = StrategyBundle.model_construct(solver_params={})
    with pytest.raises(ValueError, match="backend must be one of"):
        generate_for_backend("5V to 3.3V", tmp_path, "quantum",
                            strategy, Config.load())

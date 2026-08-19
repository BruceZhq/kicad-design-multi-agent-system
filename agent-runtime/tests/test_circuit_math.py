"""circuit_math: the one home for circuit-domain solving (AHE-governed)."""

import pytest

from ratsnest.circuit_math import (
    GenerationError,
    format_ohms,
    pick_divider,
    resistor_mpn,
)
from ratsnest.config import Config
from ratsnest.schemas import StrategyBundle


def test_format_ohms_docstring_examples():
    assert format_ohms(3000) == "3k"
    assert format_ohms(4700) == "4.7k"
    assert format_ohms(330) == "330"
    assert format_ohms(1_500_000) == "1.5M"


def test_resistor_mpn_map_hit_then_pattern():
    strategy = StrategyBundle.model_construct(solver_params={
        "mpn_map": {"3k": "EXPLICIT-3K"},
        "resistor_mpn_pattern": "RC0805FR-07{code}L",
    })
    assert resistor_mpn(strategy, "3k") == "EXPLICIT-3K"
    assert resistor_mpn(strategy, "1.6k") == "RC0805FR-071K6L"
    assert resistor_mpn(strategy, "330") == "RC0805FR-07330RL"
    assert resistor_mpn(
        strategy, "3k", "yageo.rc1206fr") == "RC1206FR-073KL"


def test_pick_divider_raises_outside_tolerance():
    config = Config.load()
    with pytest.raises(GenerationError, match="divider"):
        pick_divider(config, target=0.5, vref=1.25, tolerance_pct=2.0)

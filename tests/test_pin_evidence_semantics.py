import asyncio
import pytest
from agents.ratsnestpro.pin_evidence import pin_differences


@pytest.mark.parametrize("expected,observed,valid", [
    ("PF0", "PF0-OSC_IN", True),
    ("PA9/PA11", "PA11 [PA9]", True),
    ("NC/PA9", "PA9", False),
    ("PB3", "PB4", False),
    ("GND", "AGND", False),
    ("VIN", "VIN — Input Voltage", True),
])
def test_pin_semantics_preserve_conditions(expected, observed, valid):
    differences = pin_differences([{"number": "1", "name": expected}],
                                 {"pins": [{"number": "1", "functions": [observed], "page": 2}]})
    assert (not differences) == valid


def test_human_action_reaches_interrupt_not_final(monkeypatch):
    from agents.ratsnestpro import ratsnestpro_agent as agent
    state = {"workspace_run_name": "test", "hardware": {"ahe": {"agentic_recovery": {
        "history": [{"turn_id": "one", "revision": 3, "status": "awaiting_human"}],
    }}, "release_blockers": ["U1 evidence missing"]}}
    assert agent._after_hardware(state) == "hardware_evidence_input"
    calls = []
    monkeypatch.setattr(agent, "interrupt", lambda request: calls.append(request) or "继续")
    result = asyncio.run(agent.hardware_evidence_input(state))
    assert result == {"incremental_resume": True}
    assert calls[0]["requestedBy"] == "hardware-engineer"
    assert "U1 evidence missing" in calls[0]["question"]

import hashlib
from typing import Any

from agents.ratsnestpro import ratsnestpro_agent as agent


def test_handoff_event_emits_the_runner_contract(monkeypatch) -> None:
    observed: list[tuple[str, str, dict[str, Any]]] = []

    def capture(
        phase: str,
        status: str,
        *,
        attributes: dict[str, Any],
        **_: Any,
    ) -> None:
        observed.append((phase, status, attributes))

    monkeypatch.setattr(agent, "_workflow_event", capture)
    agent._handoff_event("architect", "parts-specialist", {"status": "ok"})

    assert observed == [
        (
            "architect->parts-specialist",
            "handoff",
            {
                "event_type": "handoff",
                "handoff_id": "architect->parts-specialist",
                "producer": "architect",
                "consumer": "parts-specialist",
                "handoff_status": "accepted",
                "payload_digest": hashlib.sha256(b'{"status":"ok"}').hexdigest(),
            },
        )
    ]


def test_build_routes_emit_canonical_role_handoffs(monkeypatch) -> None:
    observed: list[tuple[str, str]] = []

    def capture(producer: str, consumer: str, payload: Any, **_: Any) -> None:
        observed.append((producer, consumer))

    monkeypatch.setattr(agent, "_handoff_event", capture)

    state: dict[str, Any] = {
        "workflow_mode": "build",
        "project_name": "board",
        "capability_profile": {},
        "architecture": {"status": "ok"},
        "parts": {"status": "ok"},
        "hardware": {"review_candidate_ready": True},
        "review": {"status": "ok"},
        "team_members": [],
    }
    assert agent._after_initialize(state) == agent._ARCHITECT_NODE
    assert agent._after_architect(state) == agent._PARTS_NODE
    assert agent._after_parts(state) == agent._HARDWARE_NODE
    assert agent._after_hardware(state) == agent._REVIEWER_NODE
    assert agent._after_review(state) == "final_report"

    assert observed == [
        ("supervisor", "architect"),
        ("architect", "parts-specialist"),
        ("parts-specialist", "hardware-engineer"),
        ("hardware-engineer", "reviewer"),
        ("reviewer", "supervisor"),
    ]


def test_early_terminal_routes_return_results_to_supervisor(monkeypatch) -> None:
    observed: list[tuple[str, str]] = []

    def capture(producer: str, consumer: str, payload: Any, **_: Any) -> None:
        observed.append((producer, consumer))

    monkeypatch.setattr(agent, "_handoff_event", capture)

    assert (
        agent._after_architect(
            {"workflow_mode": "research", "architecture": {"status": "ok"}}
        )
        == "final_report"
    )
    assert (
        agent._after_parts({"workflow_mode": "parts", "parts": {"status": "ok"}})
        == "final_report"
    )
    assert (
        agent._after_hardware({"hardware": {"review_candidate_ready": False}})
        == "final_report"
    )

    assert observed == [
        ("architect", "supervisor"),
        ("parts-specialist", "supervisor"),
        ("hardware-engineer", "supervisor"),
    ]

from __future__ import annotations

import json
from types import SimpleNamespace

from ratsnestpro.agents import LlmError

from agents.ratsnestpro import tools


class _DeterministicReport:
    def gate(self, _name: str) -> None:
        return None


def test_deterministic_report_is_published_before_llm_advisory(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "runs" / "review-order"
    project.mkdir(parents=True)
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(workspace))

    deterministic = SimpleNamespace(
        schematic_path=None,
        pcb_path=None,
        blocked=False,
        advisory_markdown="# Advisory review\n\nDeterministic baseline.",
        result=SimpleNamespace(
            report=_DeterministicReport(),
            source="deterministic",
        ),
    )
    monkeypatch.setattr(tools, "review_project", lambda *_args, **_kwargs: deterministic)
    monkeypatch.setattr(tools, "_ToolkitLlmClient", lambda **_kwargs: object())

    report_path = workspace / "reviews" / "ordered-review.md"

    class _FailingReviewer:
        def review(self, *_args, **_kwargs):
            assert report_path.is_file()
            assert "# Authoritative review verdict" in report_path.read_text(encoding="utf-8")
            raise LlmError("advisory provider timed out")

    monkeypatch.setattr(tools, "Reviewer", _FailingReviewer)

    result = json.loads(
        tools.ratsnest_review_kicad_project(
            str(project),
            report_name="ordered-review.md",
            llm_mode="required",
            model_name="test-model",
        )
    )

    assert result["status"] == "ok"
    assert result["report_path"] == str(report_path)
    assert result["advisory_review"]["status"] == "unavailable"
    assert "advisory provider timed out" in result["advisory_review"]["error"]
    assert report_path.is_file()
    assert "LLM advisory enrichment was unavailable" in report_path.read_text(
        encoding="utf-8"
    )

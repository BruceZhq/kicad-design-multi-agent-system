"""User limits must not discard or reject appended engineering evidence."""
import pytest
from pydantic import ValidationError

from ratsnestpro.domain.contracts import RequirementSpec


BOUNDARY = (
    "\n\nVALIDATED CAPABILITY PROFILE — this is a scope, evidence, budget, and "
    "acceptance boundary, not a fixed circuit answer:\n"
)


def test_large_legacy_evidence_is_preserved_and_round_trips():
    source = "双层板，≤40×30 mm。\n电源主干 ≥0.40 mm。"
    evidence = BOUNDARY + "器件引脚证据" * 25000
    spec = RequirementSpec(raw_text=source + evidence)
    assert spec.raw_text == source
    assert spec.engineering_context == evidence
    assert spec.complete_text == source + evidence
    assert RequirementSpec.model_validate_json(spec.model_dump_json()) == spec


def test_plain_request_retains_original_bound():
    assert RequirementSpec(raw_text="a" * 100000).complete_text == "a" * 100000
    with pytest.raises(ValidationError):
        RequirementSpec(raw_text="a" * 100001)


def test_refresh_replaces_old_evidence_without_duplication():
    spec = RequirementSpec(raw_text="原需求" + BOUNDARY + "old")
    updated = RequirementSpec.model_validate({
        **spec.model_dump(), "raw_text": "原需求" + BOUNDARY + "new",
        "engineering_context": "",
    })
    assert updated.complete_text == "原需求" + BOUNDARY + "new"

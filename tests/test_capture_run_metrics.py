from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/eval/capture_run_metrics.py"
SPEC = importlib.util.spec_from_file_location("capture_run_metrics", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)
WORKFLOW = "fixture-workflow"


def _record(record_id="one", usage=None):
    return {
        "kind": "llm_output",
        "record_id": record_id,
        "model": "fixture-model",
        "phase": "hardware-engineer:route_plan",
        "created_at": "2026-09-07T18:00:00+08:00",
        "response_metadata": {"usage_metadata": usage} if usage is not None else {},
    }


def _transcript(tmp_path, records):
    suffix = hashlib.sha256(WORKFLOW.encode()).hexdigest()[:20]
    path = tmp_path / f"llm_outputs-{suffix}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def test_deduplicates_sse_and_preserves_provider_subsets(tmp_path):
    record = _record(
        usage={
            "input_tokens": 100,
            "output_tokens": 30,
            "total_tokens": 130,
            "input_token_details": {"cache_read": 80},
            "output_token_details": {"reasoning": 20},
        }
    )
    transcript = _transcript(tmp_path, [record])
    streamed = {**record, "transcript_ref": transcript.name, "stream_truncated": True}
    envelope = {"type": "message", "content": {"type": "custom", "custom_data": streamed}}
    capture = tmp_path / "events.sse"
    capture.write_text(
        ": heartbeat\n\ndata: " + json.dumps(envelope) + "\n\ndata: [DONE]\n\n",
        encoding="utf-8",
    )
    report = metrics.capture_metrics(tmp_path, WORKFLOW, [capture])
    assert report["deduplicated_records"] == 1
    assert report["provider_usage"]["observed_calls"] == 1
    totals = report["provider_usage"]["tokens"]
    assert totals["total_tokens"]["observed_sum"] == 130
    assert totals["cache_read_input_tokens"]["observed_sum"] == 80
    assert totals["reasoning_output_tokens"]["observed_sum"] == 20
    assert report["cost"] is None
    assert report["workflow_elapsed_seconds"] is None


def test_missing_usage_is_not_zero_or_estimated(tmp_path):
    _transcript(
        tmp_path,
        [
            _record("known", {"input_tokens": 0, "output_tokens": 9}),
            _record("missing"),
        ],
    )
    report = metrics.capture_metrics(tmp_path, WORKFLOW)
    tokens = report["provider_usage"]["tokens"]
    assert tokens["input_tokens"]["observed_sum"] == 0
    assert tokens["input_tokens"]["missing_records"] == 1
    assert not tokens["input_tokens"]["complete_for_observed_records"]
    assert tokens["total_tokens"]["observed_sum"] is None
    assert tokens["total_tokens"]["missing_records"] == 2
    assert tokens["reasoning_output_tokens"]["observed_sum"] is None


def test_partial_live_line_and_foreign_workflow_are_excluded(tmp_path):
    record = _record("foreign", {"total_tokens": 1000})
    record["workflow_id"] = "another-workflow"
    path = _transcript(tmp_path, [_record(usage={"total_tokens": 4}), record])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "llm_output"')
    report = metrics.capture_metrics(tmp_path, WORKFLOW)
    assert report["provider_usage"]["tokens"]["total_tokens"]["observed_sum"] == 4
    assert report["rejected_records"] == 1
    assert "incomplete trailing" in report["warnings"][0]


def test_conflicting_duplicate_usage_fails_closed(tmp_path):
    _transcript(
        tmp_path,
        [
            _record(usage={"total_tokens": 3}),
            _record(usage={"total_tokens": 4}),
        ],
    )
    with pytest.raises(ValueError, match="Conflicting provider usage"):
        metrics.capture_metrics(tmp_path, WORKFLOW)


def test_stale_result_not_reported_as_current_artifact_quality(tmp_path):
    state = {"completed_steps": 14, "intermediate_artifacts": {}}
    digest = hashlib.sha256(
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    state["checkpoint_receipt"] = {"state_sha256": digest, "generation": 90}
    result = {
        "release_ready": True,
        "checkpoint_receipt": {
            "state_sha256": "old-state",
            "generation": 73,
        },
    }
    (tmp_path / "pipeline_state.json").write_text(json.dumps(state), encoding="utf-8")
    (tmp_path / "pipeline_result.json").write_text(json.dumps(result), encoding="utf-8")
    snapshot = metrics.capture_metrics(tmp_path, WORKFLOW)["workspace_checkpoint"]
    assert snapshot["state_digest_valid"] is True
    assert snapshot["result_freshness"] == "stale_receipt_mismatch"
    assert snapshot["artifact_quality_metrics"] is None
    assert "release_ready" not in snapshot


def test_absent_transcript_has_unknown_not_zero_usage(tmp_path):
    report = metrics.capture_metrics(tmp_path, WORKFLOW)
    assert report["provider_usage"]["observed_calls"] == 0
    assert report["provider_usage"]["tokens"]["total_tokens"]["observed_sum"] is None
    assert not report["provider_usage"]["tokens"]["total_tokens"]["complete_for_observed_records"]
    assert report["workspace_checkpoint"]["result_freshness"] == "missing"

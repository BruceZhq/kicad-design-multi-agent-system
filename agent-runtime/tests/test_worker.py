from pathlib import Path

from ratsnest import worker
from ratsnest.config import Config
from ratsnest.design_workflow import parse_approved_plan, plan_sha256


def test_upload_artifact_streams_zip_with_service_identity(
        tmp_path: Path, monkeypatch):
    release = tmp_path / "project.zip"
    release.write_bytes(b"zip-payload")
    captured = {}

    class Response:
        def raise_for_status(self):
            captured["acknowledged"] = True

    def fake_put(url, content, headers, timeout):
        captured.update(
            url=url, body=b"".join(content), headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setenv("RATSNEST_SERVICE_TOKEN", "service-secret")
    monkeypatch.setattr(worker.httpx, "put", fake_put)

    worker._upload_artifact("http://control/artifact", release)

    assert captured == {
        "url": "http://control/artifact",
        "body": b"zip-payload",
        "headers": {
            "Content-Type": "application/zip",
            "X-RatsNest-Service-Token": "service-secret",
            "X-Artifact-Filename": "project.zip",
        },
        "timeout": 120,
        "acknowledged": True,
    }


def test_planning_message_returns_plan_without_creating_project(
        tmp_path: Path, monkeypatch):
    project = tmp_path / "project-must-not-exist"
    captured = {}

    class Response:
        def raise_for_status(self):
            captured["acknowledged"] = True

    def fake_put(url, content, headers, timeout):
        captured.update(url=url, body=content, headers=headers, timeout=timeout)
        return Response()

    config = Config.load()
    config.runs_dir = tmp_path / "runs"
    config.control_plane_url = None
    config.llm_enabled = False
    monkeypatch.setattr(worker.httpx, "put", fake_put)

    worker.handle_request({
        "runId": "control-1",
        "pythonRunId": "run_worker_plan",
        "kind": "design",
        "phase": "plan",
        "requirement": "12V to 5V board with red LED",
        "backend": "crew",
        "projectDir": str(project),
        "planCallbackUrl": "http://control/api/runs/control-1/plan",
    }, config)

    payload = captured["body"]
    approved = parse_approved_plan(payload, plan_sha256(payload))
    assert approved.plan.run_id == "run_worker_plan"
    assert captured["acknowledged"] is True
    assert not project.exists()


def test_execution_message_rejects_missing_approved_hash_before_writes(tmp_path):
    config = Config.load()
    config.control_plane_url = None
    project = tmp_path / "project-must-not-exist"

    import pytest
    with pytest.raises(Exception, match="hash mismatch"):
        worker.handle_request({
            "runId": "control-2",
            "kind": "design",
            "phase": "execute",
            "projectDir": str(project),
            "planJson": "{}",
            "planSha256": "",
        }, config)

    assert not project.exists()

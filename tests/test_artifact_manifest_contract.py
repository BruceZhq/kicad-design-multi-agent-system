import hashlib
import json
from pathlib import Path

from agents.ratsnestpro.artifact_publisher import publish_artifact_manifest


def test_manifest_digest_matches_control_plane_canonical_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "board.kicad_pcb"
    artifact.write_text("board", encoding="utf-8")
    storage = tmp_path / "artifacts"
    run_id = "02524f93-ae91-4a5c-8713-7bdaa722ae75"
    monkeypatch.setenv("RATSNEST_ARTIFACT_BACKEND", "local")
    monkeypatch.setenv("RATSNEST_ARTIFACT_LOCAL_ROOT", str(storage))

    manifest = publish_artifact_manifest(
        paths=[str(artifact)],
        workspace=str(workspace),
        run_id=run_id,
        delivery_status="delivered_with_issues",
    )

    digest_fields = (
        "artifact_id",
        "kind",
        "media_type",
        "name",
        "object_key",
        "sha256",
        "size_bytes",
    )
    canonical = [
        {field: item[field] for field in digest_fields}
        for item in sorted(manifest["artifacts"], key=lambda value: value["artifact_id"])
    ]
    expected = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert manifest["manifest_digest"] == expected
    assert manifest["artifacts"][0]["relative_path"] == "board.kicad_pcb"
    assert manifest["artifacts"][0]["object_key"].startswith(f"runs/{run_id}/")

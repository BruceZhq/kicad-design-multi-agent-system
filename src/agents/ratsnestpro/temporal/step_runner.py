"""Isolated child-process entry point for one checkpointed pipeline advance."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from agents.ratsnestpro.tools import ratsnest_run_pcb_pipeline_until
from core import settings
from service.governance_scope import (
    GOVERNANCE_SCOPE_ENV,
    verify_governance_scope_token,
)

_GOVERNANCE_ENV_FIELDS = {
    "tenant_scope": "RATSNESTPRO_TENANT_SCOPE",
    "project_scope": "RATSNESTPRO_PROJECT_SCOPE",
    "run_scope": "RATSNESTPRO_RUN_SCOPE",
    "harness_version_id": "RATSNESTPRO_HARNESS_VERSION_ID",
    "harness_manifest_digest": "RATSNESTPRO_HARNESS_MANIFEST_DIGEST",
}


def _install_governance_environment(command: dict[str, Any]) -> None:
    # A signed command authenticates its owner, not the code loaded in this
    # Worker. Refuse wrong-version task queues before installing run metadata.
    for field, name in (("harness_version_id", "RATSNEST_HARNESS_VERSION_ID"),
                        ("harness_manifest_digest", "RATSNEST_HARNESS_MANIFEST_DIGEST")):
        deployed = os.environ.get(name, "").strip()
        claimed = str(command.get(field, "")).strip()
        if deployed and claimed and deployed != claimed:
            raise ValueError(f"Worker deployment identity mismatch: {field}; use the version-pinned task queue")
    for name in (GOVERNANCE_SCOPE_ENV, *_GOVERNANCE_ENV_FIELDS.values()):
        os.environ.pop(name, None)
    token = str(command.get("governance_scope_token", "")).strip()
    if not token:
        return
    if settings.RATSNEST_INTERNAL_SIGNING_SECRET is None:
        return
    try:
        scope = verify_governance_scope_token(
            token,
            secret=settings.RATSNEST_INTERNAL_SIGNING_SECRET.get_secret_value(),
        )
    except ValueError:
        return
    for field, environment_name in _GOVERNANCE_ENV_FIELDS.items():
        value = str(command.get(field, ""))
        if value != str(getattr(scope, field)):
            for name in _GOVERNANCE_ENV_FIELDS.values():
                os.environ.pop(name, None)
            return
        os.environ[environment_name] = value
    os.environ[GOVERNANCE_SCOPE_ENV] = token


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command_path", type=Path)
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args()

    try:
        command = _read_object(args.command_path)
        _install_governance_environment(command)
        workflow_id = str(command.get("workflow_id", "")).strip()
        if workflow_id:
            os.environ["RATSNESTPRO_LLM_TRANSCRIPT_WORKFLOW_ID"] = workflow_id
        os.environ["RATSNESTPRO_PIPELINE_STEP"] = str(command["step"])
        raw = ratsnest_run_pcb_pipeline_until(
            requirement=str(command["requirement"]),
            until_step=str(command["step"]),
            run_name=str(command["run_name"]),
            project_name=str(command["project_name"]),
            llm_mode=str(command.get("llm_mode", "required")),
            model_name=(
                str(command["model_name"]) if command.get("model_name") else None
            ),
            model_type=(
                str(command["model_type"]) if command.get("model_type") else None
            ),
            reasoning_effort=(
                str(command["reasoning_effort"])
                if command.get("reasoning_effort")
                else None
            ),
            vision_model_name=(
                str(command["vision_model_name"])
                if command.get("vision_model_name")
                else None
            ),
            vision_reasoning_effort=(
                str(command["vision_reasoning_effort"])
                if command.get("vision_reasoning_effort")
                else None
            ),
            ahe_budget=(
                dict(command["ahe_budget"])
                if isinstance(command.get("ahe_budget"), dict)
                else None
            ),
            approved_component_replacements=(
                dict(command["approved_component_replacements"])
                if isinstance(command.get("approved_component_replacements"), dict)
                else None
            ),
            resume_from_step=(
                str(command["resume_from_step"])
                if command.get("resume_from_step")
                else None
            ),
            resume_token=(workflow_id or None),
        )
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("pipeline tool returned a non-object JSON value")
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - process boundary must stay structured
        result = {
            "status": "error",
            "error_type": "step_runner_error",
            "error": f"{type(exc).__name__}: {exc}",
        }
        exit_code = 1

    args.result_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

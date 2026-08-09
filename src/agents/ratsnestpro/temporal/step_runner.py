"""Isolated child-process entry point for one checkpointed pipeline advance."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from agents.ratsnestpro.tools import ratsnest_run_pcb_pipeline_until


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
            ahe_budget=(
                dict(command["ahe_budget"])
                if isinstance(command.get("ahe_budget"), dict)
                else None
            ),
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

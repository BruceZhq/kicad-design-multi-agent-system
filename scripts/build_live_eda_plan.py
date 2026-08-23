"""Build a versioned live-evaluation plan from the public five-case EDA matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_PROFILES = {
    "01-rp2040-env-logger": "site-control-telemetry@1.1",
    "02-stm32g431-bldc": "site-control-telemetry@1.1",
    "03-esp32c3-isolated-gateway": "radio-control-monitor@1.0",
    "04-nrf52840-motion-beacon": "radio-control-monitor@1.0",
    "05-stm32f072-usb-midi": "site-control-telemetry@1.1",
}

_FIXED_DECISIONS = {
    "01-rp2040-env-logger": (
        "固定评测决策：board outline 70 x 45 mm；每个用户按键使用外部 10 kOhm 上拉。"
    ),
    "02-stm32g431-bldc": (
        "固定评测决策：board outline 80 x 60 mm；使用四个非电镀 M3 安装孔。"
    ),
    "03-esp32c3-isolated-gateway": "固定评测决策：board outline 90 x 60 mm。",
    "04-nrf52840-motion-beacon": (
        "固定评测决策：board outline 45 x 35 mm；用户按键使用外部 10 kOhm 上拉。"
    ),
    "05-stm32f072-usb-midi": (
        "固定评测决策：board outline 160 x 70 mm；按键矩阵输入行使用外部 10 kOhm 上拉。"
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--matrix",
        type=Path,
        default=Path("docs/ratsnestpro/test-cases/five-case-e2e-2026-07-30.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-suffix", required=True)
    args = parser.parse_args()

    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    cases = []
    for raw in matrix["cases"]:
        case_id = raw["case_id"]
        project_name = f"{case_id}-{args.run_suffix}"
        run_name = f"{project_name}-run"
        prompt = raw["prompt"].replace(raw["run_name"], run_name)
        prompt = prompt.replace(raw["project_name"], project_name)
        prompt = f"{prompt}\n\n{_FIXED_DECISIONS[case_id]}"
        cases.append(
            {
                "caseId": f"eda.{case_id}",
                "category": "eda_pipeline",
                "prompt": prompt,
                "expectedIntents": ["build"],
                "requiredPhases": ["intent-router", "architect", "supervisor"],
                "expectedTerminal": "completed",
                "expectReleaseReady": None,
                "profileReference": _PROFILES[case_id],
                "timeoutSeconds": 7200,
                "agentConfig": {
                    "project_name": project_name,
                    "run_name": run_name,
                },
            }
        )

    plan = {
        "schemaVersion": "1.0",
        "planId": f"{matrix['matrix_id']}-{args.run_suffix}",
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

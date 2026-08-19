"""Kafka run worker — the cluster-mode replacement for local dispatch.

Consumes run requests from `ratsnest.run-requests`, executes them with the
same pipeline as the CLI (design generation or repair loop), and PUTs the
RunRecord back to the control plane's callback URL. ATDP events stream to the
control plane during execution exactly as in local mode.

    python -m ratsnest.worker            (env: RATSNEST_KAFKA=host:9092)
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import httpx

from ratsnest.config import Config
from ratsnest.schemas import RunConfig

RUN_REQUEST_TOPIC = os.environ.get("RATSNEST_TOPIC_RUNS", "ratsnest.run-requests")
GROUP_ID = os.environ.get("RATSNEST_WORKER_GROUP", "ratsnest-workers")


def _service_headers(content_type: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    token = os.environ.get("RATSNEST_SERVICE_TOKEN")
    if token:
        headers["X-RatsNest-Service-Token"] = token
    return headers


def _upload_artifact(url: str, release: Path) -> None:
    headers = _service_headers("application/zip")
    headers["X-Artifact-Filename"] = release.name
    with release.open("rb") as source:
        chunks = iter(lambda: source.read(64 * 1024), b"")
        response = httpx.put(url, content=chunks, headers=headers, timeout=120)
    response.raise_for_status()


def handle_request(msg: dict, config: Config) -> None:
    from ratsnest.orchestrator import RunLoop

    kind = msg.get("kind", "fix")
    phase = msg.get("phase", "execute")
    project_dir = msg.get("projectDir")
    max_iter = int(msg.get("maxIterations", 4))
    # ATDP events flow to the control plane during the run
    if msg.get("controlPlaneUrl"):
        config.control_plane_url = msg["controlPlaneUrl"]

    if kind == "design" and phase == "plan":
        from ratsnest.data_proxy import Recorder
        from ratsnest.design_workflow import plan_design, serialize_plan
        from ratsnest.evolution import StrategyRegistry
        from ratsnest.orchestrator import RunStore

        strategy_name, strategy = StrategyRegistry(
            config.strategies_dir).load_active()
        python_run_id = (msg.get("pythonRunId")
                         or f"run_{uuid.uuid4().hex[:12]}")
        recorder = Recorder(
            RunStore(config.runs_dir).run_dir(python_run_id),
            python_run_id, config.control_plane_url,
            base_metadata={"strategy_version_id": strategy.version_id(),
                           "backend": msg.get("backend", "crew"),
                           "workflow_phase": "plan"})
        planned = plan_design(
            msg.get("requirement", ""), msg.get("backend", "crew"),
            strategy_name, strategy, config, recorder=recorder,
            run_id=python_run_id)
        callback = msg.get("planCallbackUrl")
        if not callback:
            raise RuntimeError("planning message has no planCallbackUrl")
        response = httpx.put(
            callback, content=serialize_plan(planned),
            headers=_service_headers("application/json"), timeout=30)
        response.raise_for_status()
        print(f"[worker] run {msg.get('runId')} -> awaiting approval",
              flush=True)
        return

    if project_dir is None:
        raise RuntimeError("execution message has no projectDir")

    spec = None
    strategy = None
    recorder = None
    python_run_id = None
    release_path = None
    if kind == "design":
        from ratsnest.data_proxy import Recorder
        from ratsnest.design_workflow import (
            execute_approved_plan,
            parse_approved_plan,
        )
        from ratsnest.evolution import StrategyRegistry
        from ratsnest.orchestrator import RunStore
        approved = parse_approved_plan(
            str(msg.get("planJson", "")), str(msg.get("planSha256", "")))
        planned = approved.plan
        if msg.get("pythonRunId") and msg["pythonRunId"] != planned.run_id:
            raise RuntimeError("planning and execution run ids differ")
        strategy = StrategyRegistry(config.strategies_dir).load_exact(
            planned.strategy_name, planned.strategy_version_id)
        python_run_id = planned.run_id
        recorder = Recorder(
            RunStore(config.runs_dir).run_dir(python_run_id),
            python_run_id, config.control_plane_url,
            base_metadata={"strategy_version_id": strategy.version_id(),
                           "project": project_dir,
                           "backend": planned.backend,
                           "workflow_phase": "execute"},
            initial_step=planned.trajectory_step)
        spec = execute_approved_plan(
            approved, Path(project_dir), strategy, config,
            recorder=recorder)

    record = RunLoop(config).execute(
        RunConfig(project_dir=project_dir, max_iterations=max_iter,
                  run_erc=True),
        recorder=recorder, run_id=python_run_id)

    # deliverables so the control plane can serve download + previews
    if kind == "design":
        try:
            from ratsnest.pipeline import evaluate_for_release, finalize_outputs
            ev = evaluate_for_release(Path(project_dir), strategy, config)
            outputs = finalize_outputs(Path(project_dir), ev, record, spec, config)
            release_path = outputs.get("release")
        except Exception as exc:
            raise RuntimeError(f"finalize failed: {exc}") from exc

    artifact_url = msg.get("artifactUrl")
    if kind == "design" and artifact_url:
        if release_path is None or not Path(release_path).is_file():
            raise RuntimeError("release artifact was not produced")
        _upload_artifact(artifact_url, Path(release_path))

    callback = msg.get("callbackUrl")
    if callback:
        response = httpx.put(
            callback, content=record.model_dump_json(),
            headers=_service_headers("application/json"), timeout=30)
        response.raise_for_status()
    print(f"[worker] run {msg.get('runId')} -> {record.status}", flush=True)


def main() -> None:
    try:
        from kafka import KafkaConsumer  # kafka-python
    except ImportError as exc:
        raise SystemExit(
            "kafka-python not installed — pip install kafka-python "
            "(cluster mode only)") from exc

    config = Config.load()
    bootstrap = os.environ.get("RATSNEST_KAFKA", "localhost:9092")
    consumer = KafkaConsumer(
        RUN_REQUEST_TOPIC,
        bootstrap_servers=bootstrap,
        group_id=GROUP_ID,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    print(f"[worker] consuming {RUN_REQUEST_TOPIC} from {bootstrap}", flush=True)
    for message in consumer:
        acknowledged = False
        try:
            handle_request(message.value, config)
            acknowledged = True
        except Exception as exc:  # a bad run must not kill the worker
            print(f"[worker] run failed: {exc}", flush=True)
            callback = (message.value or {}).get("callbackUrl")
            if callback:
                try:
                    response = httpx.put(
                        callback,
                        json={"status": "failed", "error": str(exc)[:500]},
                        headers=_service_headers("application/json"),
                        timeout=10)
                    response.raise_for_status()
                    acknowledged = True
                except Exception:
                    pass
        if acknowledged:
            consumer.commit()


if __name__ == "__main__":
    main()

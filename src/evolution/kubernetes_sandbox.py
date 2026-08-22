"""Kubernetes Job executor for untrusted evolution candidate checks."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from temporalio import activity

from evolution.sandbox import CandidateEvalReport, EvalCommandResult, patch_digest
from evolution.temporal.contracts import FIXED_EVAL_IDS
from evolution.temporal.trial_contracts import canonical_json, trial_request_from_command

_WORKLOAD_LABEL = "evolution-candidate-sandbox"
_MAX_CONFIG_MAP_BYTES = 768 * 1024
_ACTIVE_DEADLINE_SECONDS = 300
_POLL_DEADLINE_SECONDS = 360
_SERVICE_ACCOUNT_DIRECTORY = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_MATERIALIZER = "materialize"

_EVAL_CONTAINERS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    (
        "python-compile",
        (
            "/usr/bin/timeout",
            "--signal=KILL",
            "60s",
            "/usr/local/bin/python",
            "-m",
            "compileall",
            "-q",
            "src/evolution",
            "src/agents/ratsnestpro",
        ),
        60,
    ),
    (
        "evolution-core",
        (
            "/usr/bin/timeout",
            "--signal=KILL",
            "180s",
            "/usr/local/bin/python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "pytest_asyncio.plugin",
            "--confcutdir=tests/evolution",
            "tests/evolution/test_evolution_core.py",
            "tests/evolution/test_sandbox.py",
        ),
        180,
    ),
)


class KubernetesSandboxConfigurationError(ValueError):
    """A production sandbox setting is absent or mutable."""


class KubernetesSandboxExecutor:
    """Create one credential-free Job and derive evidence from Pod status only."""

    def __init__(self) -> None:
        self.namespace = _required_dns_name("RATSNEST_EVOLUTION_SANDBOX_NAMESPACE")
        self.image = _required_immutable_image()
        self.mirror_claim = _required_dns_name("RATSNEST_EVOLUTION_GIT_MIRROR_CLAIM")
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443").strip()
        if not host or not port.isdigit():
            raise KubernetesSandboxConfigurationError("in-cluster Kubernetes API is unavailable")
        rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self.api_url = f"https://{rendered_host}:{port}"
        self.token_path = _SERVICE_ACCOUNT_DIRECTORY / "token"
        self.ca_path = _SERVICE_ACCOUNT_DIRECTORY / "ca.crt"
        if not self.token_path.is_file() or not self.ca_path.is_file():
            raise KubernetesSandboxConfigurationError(
                "sandbox controller service-account token or CA is unavailable"
            )

    async def evaluate(self, command: dict[str, Any]) -> CandidateEvalReport:
        request = trial_request_from_command(command)
        source = request.model_dump(mode="json", by_alias=False)
        encoded = canonical_json(source)
        if len(encoded) > _MAX_CONFIG_MAP_BYTES:
            raise KubernetesSandboxConfigurationError("candidate input exceeds ConfigMap bound")

        trial_hash = hashlib.sha256(request.trial_id.encode("utf-8")).hexdigest()[:16]
        suffix = secrets.token_hex(3)
        name = f"ratsnest-eval-{trial_hash}-{suffix}"
        labels = {
            "app.kubernetes.io/name": "ratsnest-evolution-candidate",
            "ratsnest.io/workload-class": _WORKLOAD_LABEL,
            "ratsnest.io/trial-id": trial_hash,
        }
        config_map = self._config_map(name, labels, encoded.decode("utf-8"))
        job = self._job(name, labels)
        config_map_created = False
        job_created = False
        report: CandidateEvalReport | None = None
        cleanup_succeeded = True

        headers = {
            "Authorization": f"Bearer {self.token_path.read_text(encoding='utf-8').strip()}",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
        async with httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            verify=str(self.ca_path),
            timeout=timeout,
        ) as client:
            try:
                await self._create(client, "configmaps", config_map)
                config_map_created = True
                await self._create(client, "jobs", job, batch=True)
                job_created = True
                job_value = await self._wait_for_job(client, name)
                pod = await self._job_pod(client, name)
                report = self._report(command, job_value, pod)
            finally:
                if job_created:
                    cleanup_succeeded = await self._delete(
                        client, "jobs", name, batch=True
                    ) and cleanup_succeeded
                if config_map_created:
                    cleanup_succeeded = await self._delete(
                        client, "configmaps", name
                    ) and cleanup_succeeded

        if report is None:
            raise RuntimeError("sandbox Job completed without an evaluation report")
        if not cleanup_succeeded:
            values = report.model_dump(mode="python")
            values.update(
                cleanup_succeeded=False,
                verdict="error",
                error="sandbox Job or ConfigMap cleanup did not complete",
            )
            return CandidateEvalReport.model_validate(values)
        return report

    async def _create(
        self,
        client: httpx.AsyncClient,
        resource: str,
        value: dict[str, Any],
        *,
        batch: bool = False,
    ) -> None:
        response = await client.post(self._collection_path(resource, batch=batch), json=value)
        response.raise_for_status()

    async def _delete(
        self,
        client: httpx.AsyncClient,
        resource: str,
        name: str,
        *,
        batch: bool = False,
    ) -> bool:
        try:
            response = await client.request(
                "DELETE",
                f"{self._collection_path(resource, batch=batch)}/{name}",
                json={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "gracePeriodSeconds": 0,
                    "propagationPolicy": "Background",
                },
            )
            return response.status_code in {200, 202, 404}
        except httpx.HTTPError:
            return False

    async def _wait_for_job(
        self, client: httpx.AsyncClient, name: str
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _POLL_DEADLINE_SECONDS
        path = f"{self._collection_path('jobs', batch=True)}/{name}"
        while loop.time() < deadline:
            response = await client.get(path)
            response.raise_for_status()
            value = response.json()
            conditions = value.get("status", {}).get("conditions", [])
            if any(
                item.get("status") == "True"
                and item.get("type") in {"Complete", "Failed"}
                for item in conditions
            ):
                return value
            activity.heartbeat({"job": name, "phase": "candidate-evaluation"})
            await asyncio.sleep(2)
        raise TimeoutError("candidate sandbox Job did not reach a terminal condition")

    async def _job_pod(
        self, client: httpx.AsyncClient, name: str
    ) -> dict[str, Any] | None:
        selector = quote(f"job-name={name}", safe="")
        response = await client.get(
            f"/api/v1/namespaces/{self.namespace}/pods?labelSelector={selector}"
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        if not items:
            return None
        items.sort(key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""))
        return items[-1]

    def _report(
        self,
        command: dict[str, Any],
        job: dict[str, Any],
        pod: dict[str, Any] | None,
    ) -> CandidateEvalReport:
        request = trial_request_from_command(command)
        trial_input = request.trial_input
        statuses = {
            item.get("name"): item.get("state", {}).get("terminated", {})
            for item in (pod or {}).get("status", {}).get("initContainerStatuses", [])
            if item.get("state", {}).get("terminated") is not None
        }
        materializer = statuses.get(_MATERIALIZER)
        materializer_code = _exit_code(materializer)
        command_results = [
            _command_result(eval_id, argv, statuses.get(eval_id))
            for eval_id, argv, _ in _EVAL_CONTAINERS
            if statuses.get(eval_id) is not None
        ]
        job_failed = any(
            item.get("type") == "Failed" and item.get("status") == "True"
            for item in job.get("status", {}).get("conditions", [])
        )

        if materializer_code == 10:
            verdict = "policy_rejected"
            error = "immutable materializer rejected the candidate input"
        elif materializer_code != 0:
            verdict = "error"
            error = "immutable materializer or Kubernetes Job failed"
        elif len(command_results) != len(FIXED_EVAL_IDS):
            verdict = "error"
            error = "Kubernetes Job lacks a terminal status for every fixed evaluation"
        elif all(item.passed for item in command_results) and not job_failed:
            verdict = "passed"
            error = None
        else:
            verdict = "failed"
            error = None

        return CandidateEvalReport(
            candidate_id=request.candidate_id,
            base_commit=trial_input.patch_plan.base_commit,
            patch_digest=patch_digest(trial_input.patch_bundle),
            verdict=verdict,
            worktree_created=materializer_code == 0,
            materialized_files=(
                [item.path for item in trial_input.patch_bundle.files]
                if materializer_code == 0
                else []
            ),
            command_results=command_results,
            error=error,
            cleanup_succeeded=True,
            executor_mode="kubernetes_job",
        )

    def _collection_path(self, resource: str, *, batch: bool = False) -> str:
        prefix = "/apis/batch/v1" if batch else "/api/v1"
        return f"{prefix}/namespaces/{self.namespace}/{resource}"

    def _config_map(
        self, name: str, labels: dict[str, str], payload: str
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels},
            "immutable": True,
            "data": {"trial.json": payload},
        }

    def _job(self, name: str, labels: dict[str, str]) -> dict[str, Any]:
        materializer = _container(
            name=_MATERIALIZER,
            image=self.image,
            argv=(
                "/usr/local/bin/python",
                "-m",
                "evolution.kubernetes_sandbox_runner",
                "--input",
                "/input/trial.json",
                "--repository",
                "/repository",
                "--worktree",
                "/workspace/repo",
            ),
            mounts=(
                {"name": "input", "mountPath": "/input", "readOnly": True},
                {"name": "repository", "mountPath": "/repository", "readOnly": True},
                {"name": "workspace", "mountPath": "/workspace"},
                {"name": "tmp", "mountPath": "/tmp"},
            ),
            working_directory="/app",
        )
        evaluators = [
            _container(
                name=eval_id,
                image=self.image,
                argv=argv,
                mounts=(
                    {"name": "workspace", "mountPath": "/workspace"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ),
                working_directory="/workspace/repo",
                candidate_environment=True,
            )
            for eval_id, argv, _ in _EVAL_CONTAINERS
        ]
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels},
            "spec": {
                "activeDeadlineSeconds": _ACTIVE_DEADLINE_SECONDS,
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "restartPolicy": "Never",
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "fsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "initContainers": [materializer, *evaluators],
                        "containers": [
                            _container(
                                name="complete",
                                image=self.image,
                                argv=("/usr/bin/true",),
                                mounts=(),
                                working_directory="/tmp",
                            )
                        ],
                        "volumes": [
                            {"name": "input", "configMap": {"name": name}},
                            {
                                "name": "repository",
                                "persistentVolumeClaim": {
                                    "claimName": self.mirror_claim,
                                    "readOnly": True,
                                },
                            },
                            {"name": "workspace", "emptyDir": {"sizeLimit": "2Gi"}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
                        ],
                    },
                },
            },
        }


def _container(
    *,
    name: str,
    image: str,
    argv: tuple[str, ...],
    mounts: tuple[dict[str, Any], ...],
    working_directory: str,
    candidate_environment: bool = False,
) -> dict[str, Any]:
    environment = [
        {"name": "HOME", "value": "/tmp/evolution-home"},
        {"name": "TMPDIR", "value": "/tmp"},
    ]
    if candidate_environment:
        environment.extend(
            [
                {"name": "PYTHONPATH", "value": "/workspace/repo/src"},
                {"name": "PYTHONHASHSEED", "value": "0"},
                {"name": "PYTHONNOUSERSITE", "value": "1"},
                {"name": "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "value": "1"},
                {"name": "HTTP_PROXY", "value": "http://127.0.0.1:9"},
                {"name": "HTTPS_PROXY", "value": "http://127.0.0.1:9"},
                {"name": "ALL_PROXY", "value": "http://127.0.0.1:9"},
                {"name": "NO_PROXY", "value": ""},
            ]
        )
    return {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": list(argv),
        "workingDir": working_directory,
        "env": environment,
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi", "ephemeral-storage": "64Mi"},
            "limits": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "256Mi"},
        },
        "volumeMounts": list(mounts),
    }


def _command_result(
    eval_id: str,
    argv: tuple[str, ...],
    terminated: dict[str, Any] | None,
) -> EvalCommandResult:
    code = _exit_code(terminated)
    reason = str((terminated or {}).get("reason") or "")[:200]
    started = _timestamp((terminated or {}).get("startedAt"))
    finished = _timestamp((terminated or {}).get("finishedAt"))
    duration_ms = max(0, int((finished - started).total_seconds() * 1_000)) if started and finished else 0
    timed_out = code == 124 or reason in {"DeadlineExceeded"}
    return EvalCommandResult(
        eval_id=eval_id,
        argv=list(argv),
        exit_code=code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        output_limit_exceeded=False,
        output=f"Kubernetes terminated container: reason={reason or 'unspecified'}",
        passed=code == 0 and not timed_out,
    )


def _exit_code(terminated: dict[str, Any] | None) -> int | None:
    value = (terminated or {}).get("exitCode")
    return value if isinstance(value, int) else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _required_dns_name(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", value):
        raise KubernetesSandboxConfigurationError(f"{name} must be an explicit DNS name")
    return value


def _required_immutable_image() -> str:
    value = os.environ.get("RATSNEST_EVOLUTION_SANDBOX_IMAGE", "").strip()
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", value):
        raise KubernetesSandboxConfigurationError(
            "RATSNEST_EVOLUTION_SANDBOX_IMAGE must be pinned by sha256 digest"
        )
    return value

"""Opaque execution scopes derived only from an authenticated internal identity."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Protocol

from evolution.contracts import HarnessIdentity, resolve_harness_identity


class RuntimeIdentityBound(Protocol):
    @property
    def runtime_identity(self) -> tuple[str, str, str] | None: ...


def _scope(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    """Non-reversible scope identifiers safe for checkpoints and audit metadata."""

    principal: str
    tenant: str
    project: str

    @property
    def owner_id(self) -> str:
        return f"rt1:{self.tenant}:{self.project}:{self.principal}"


def scope_identity(
    principal_id: str,
    tenant_id: str,
    project_id: str,
) -> ExecutionScope:
    return ExecutionScope(
        principal=_scope(principal_id),
        tenant=_scope(tenant_id),
        project=_scope(f"{tenant_id}\0{project_id}"),
    )


def execution_scope(value: RuntimeIdentityBound) -> ExecutionScope | None:
    identity = value.runtime_identity
    if identity is None:
        return None
    principal_id, tenant_id, project_id = identity
    return scope_identity(principal_id, tenant_id, project_id)


def request_harness_identity(
    value: RuntimeIdentityBound,
    agent_config: dict[str, Any],
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> HarnessIdentity | None:
    """Accept a run pin only on a transport-authenticated internal request."""

    has_claim = _has_harness_claim(agent_config)
    if execution_scope(value) is None:
        if has_claim:
            raise ValueError("harness_version is reserved for signed internal requests")
        return None

    # The Java runtime contract intentionally has one canonical carrier.  Do
    # not silently accept aliases here: a missing pin or an ambiguous carrier
    # must fail before LangGraph creates a checkpoint or starts work.
    if "harness_version" not in agent_config:
        raise ValueError("signed internal runs require harness_version")
    if any(
        key in agent_config
        for key in (
            "harnessVersion",
            "harness_version_id",
            "harnessVersionId",
            "harness_channel",
            "harnessChannel",
            "harness_manifest_digest",
            "harnessManifestDigest",
        )
    ):
        raise ValueError("signed internal runs must use only harness_version")
    for key in ("runtime_config", "runtimeConfig"):
        runtime_config = agent_config.get(key)
        if isinstance(runtime_config, dict) and (
            "harness_version" in runtime_config or "harnessVersion" in runtime_config
        ):
            raise ValueError("signed internal runs contain multiple harness identity carriers")
    return resolve_harness_identity(
        {"harness_version": agent_config["harness_version"]},
        environ=environ,
        require_explicit=True,
    )


def _has_harness_claim(agent_config: dict[str, Any]) -> bool:
    if any(
        key in agent_config
        for key in (
            "harness_version",
            "harnessVersion",
            "harness_version_id",
            "harnessVersionId",
            "harness_channel",
            "harnessChannel",
            "harness_manifest_digest",
            "harnessManifestDigest",
        )
    ):
        return True
    return any(
        isinstance(agent_config.get(key), dict)
        and (
            "harness_version" in agent_config[key]
            or "harnessVersion" in agent_config[key]
        )
        for key in ("runtime_config", "runtimeConfig")
    )


def effective_user_id(value: RuntimeIdentityBound, fallback: str | None) -> str | None:
    scope = execution_scope(value)
    return scope.owner_id if scope is not None else fallback


def audit_scopes(owner_id: str | None) -> tuple[str | None, str | None]:
    """Extract opaque tenant/project scopes from an internal registry owner."""

    if not owner_id:
        return None, None
    parts = owner_id.split(":")
    if len(parts) != 4 or parts[0] != "rt1":
        return None, None
    if any(len(part) != 16 for part in parts[1:]):
        return None, None
    return parts[1], parts[2]


__all__ = [
    "ExecutionScope",
    "audit_scopes",
    "effective_user_id",
    "execution_scope",
    "request_harness_identity",
    "scope_identity",
]

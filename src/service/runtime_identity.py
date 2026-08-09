"""Opaque execution scopes derived only from an authenticated internal identity."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


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
    "scope_identity",
]

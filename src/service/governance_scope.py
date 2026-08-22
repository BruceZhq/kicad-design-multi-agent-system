"""Signed opaque identity carried from the authenticated Runtime to AHE/EHE."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

GOVERNANCE_SCOPE_ENV = "RATSNESTPRO_GOVERNANCE_SCOPE_TOKEN"

_OPAQUE_SCOPE_RE = re.compile(r"^[0-9a-f]{16,64}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class TrustedGovernanceScope:
    tenant_scope: str
    project_scope: str
    run_scope: str
    harness_version_id: str
    harness_manifest_digest: str

    def __post_init__(self) -> None:
        if not _OPAQUE_SCOPE_RE.fullmatch(self.tenant_scope):
            raise ValueError("tenant_scope is not a valid opaque scope")
        if not _OPAQUE_SCOPE_RE.fullmatch(self.project_scope):
            raise ValueError("project_scope is not a valid opaque scope")
        if not _DIGEST_RE.fullmatch(self.run_scope):
            raise ValueError("run_scope is not a valid signed digest")
        if not _VERSION_RE.fullmatch(self.harness_version_id):
            raise ValueError("harness_version_id is invalid")
        if not _DIGEST_RE.fullmatch(self.harness_manifest_digest):
            raise ValueError("harness_manifest_digest is invalid")

    def payload(self) -> dict[str, str | int]:
        return {"v": 1, **asdict(self)}


def _secret_bytes(secret: str) -> bytes:
    encoded = secret.encode()
    if len(encoded) < 32:
        raise ValueError("governance signing secret must contain at least 32 bytes")
    return encoded


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    try:
        value.encode("ascii")
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("governance scope token encoding is invalid") from exc


def derive_run_scope(
    *,
    secret: str,
    tenant_scope: str,
    project_scope: str,
    request_id: str,
) -> str:
    if not request_id.strip():
        raise ValueError("request_id is required for run scope")
    material = _canonical({
        "v": 1,
        "tenant_scope": tenant_scope,
        "project_scope": project_scope,
        "request_id": request_id,
    })
    return hmac.new(
        _secret_bytes(secret),
        b"ratsnest-governance-run-v1\0" + material,
        hashlib.sha256,
    ).hexdigest()


def issue_governance_scope_token(
    scope: TrustedGovernanceScope,
    *,
    secret: str,
) -> str:
    payload = _canonical(scope.payload())
    signature = hmac.new(
        _secret_bytes(secret),
        b"ratsnest-governance-scope-v1\0" + payload,
        hashlib.sha256,
    ).digest()
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_governance_scope_token(
    token: str,
    *,
    secret: str,
) -> TrustedGovernanceScope:
    parts = token.split(".")
    if len(parts) != 2 or any(not part for part in parts):
        raise ValueError("governance scope token format is invalid")
    payload_bytes = _decode(parts[0])
    supplied = _decode(parts[1])
    expected = hmac.new(
        _secret_bytes(secret),
        b"ratsnest-governance-scope-v1\0" + payload_bytes,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("governance scope token signature is invalid")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("governance scope token JSON is invalid") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("governance scope token version is invalid")
    expected_fields = {
        "v",
        "tenant_scope",
        "project_scope",
        "run_scope",
        "harness_version_id",
        "harness_manifest_digest",
    }
    if set(payload) != expected_fields:
        raise ValueError("governance scope token fields are invalid")
    return TrustedGovernanceScope(
        tenant_scope=str(payload["tenant_scope"]),
        project_scope=str(payload["project_scope"]),
        run_scope=str(payload["run_scope"]),
        harness_version_id=str(payload["harness_version_id"]),
        harness_manifest_digest=str(payload["harness_manifest_digest"]),
    )


def governance_scope_from_environ(
    environ: Mapping[str, str],
    *,
    secret: str | None,
) -> TrustedGovernanceScope | None:
    """Return a verified scope, or no governance authority on any omission/error."""

    token = str(environ.get(GOVERNANCE_SCOPE_ENV, "")).strip()
    if not token or not secret:
        return None
    try:
        scope = verify_governance_scope_token(token, secret=secret)
    except ValueError:
        return None
    comparisons = {
        "RATSNESTPRO_TENANT_SCOPE": scope.tenant_scope,
        "RATSNESTPRO_PROJECT_SCOPE": scope.project_scope,
        "RATSNESTPRO_RUN_SCOPE": scope.run_scope,
        "RATSNESTPRO_HARNESS_VERSION_ID": scope.harness_version_id,
        "RATSNESTPRO_HARNESS_MANIFEST_DIGEST": scope.harness_manifest_digest,
    }
    if any(
        environ.get(name) and not hmac.compare_digest(str(environ[name]), expected)
        for name, expected in comparisons.items()
    ):
        return None
    return scope


__all__ = [
    "GOVERNANCE_SCOPE_ENV",
    "TrustedGovernanceScope",
    "derive_run_scope",
    "governance_scope_from_environ",
    "issue_governance_scope_token",
    "verify_governance_scope_token",
]

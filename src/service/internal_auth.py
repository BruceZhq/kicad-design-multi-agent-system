from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_TEXT_CLAIMS = (
    "iss",
    "aud",
    "sub",
    "tenantId",
    "projectId",
    "runId",
    "method",
    "path",
    "bodySha256",
)


class InternalTokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InternalClaims:
    issuer: str
    audience: str
    subject: str
    tenant_id: str
    project_id: str
    run_id: str
    issued_at: int
    expires_at: int
    method: str
    path: str
    body_sha256: str


def verify_internal_token(
    token: str,
    *,
    secret: str,
    issuer: str,
    audience: str,
    method: str,
    path: str,
    body: bytes,
    now: int | None = None,
    clock_skew_seconds: int = 15,
    max_ttl_seconds: int = 120,
) -> InternalClaims:
    if len(secret.encode("utf-8")) < 32:
        raise InternalTokenError("Internal signing secret must contain at least 32 bytes.")
    if clock_skew_seconds < 0 or max_ttl_seconds < 1:
        raise InternalTokenError("Internal token timing configuration is invalid.")

    encoded_header, encoded_payload, encoded_signature = _segments(token)
    header = _json_object(_decode_segment(encoded_header))
    payload = _json_object(_decode_segment(encoded_payload))
    if header.get("alg") != "HS256" or header.get("typ") != "JWT" or "crit" in header:
        raise InternalTokenError("Internal token header is invalid.")

    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected_signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    supplied_signature = _decode_segment(encoded_signature)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise InternalTokenError("Internal token signature is invalid.")

    text_claims = {name: _text_claim(payload, name) for name in _REQUIRED_TEXT_CLAIMS}
    issued_at = _numeric_date(payload, "iat")
    expires_at = _numeric_date(payload, "exp")
    current_time = int(time.time()) if now is None else now
    if issued_at > current_time + clock_skew_seconds:
        raise InternalTokenError("Internal token is not active yet.")
    if expires_at < current_time - clock_skew_seconds:
        raise InternalTokenError("Internal token has expired.")
    if expires_at <= issued_at or expires_at - issued_at > max_ttl_seconds:
        raise InternalTokenError("Internal token lifetime is invalid.")

    expected_body_hash = hashlib.sha256(body).hexdigest()
    comparisons = (
        (text_claims["iss"], issuer),
        (text_claims["aud"], audience),
        (text_claims["method"], method.upper()),
        (text_claims["path"], path),
        (text_claims["bodySha256"], expected_body_hash),
    )
    if any(not hmac.compare_digest(actual, expected) for actual, expected in comparisons):
        raise InternalTokenError("Internal token claims do not match this request.")
    if not _SHA256_HEX.fullmatch(text_claims["bodySha256"]):
        raise InternalTokenError("Internal request digest is invalid.")

    return InternalClaims(
        issuer=text_claims["iss"],
        audience=text_claims["aud"],
        subject=text_claims["sub"],
        tenant_id=text_claims["tenantId"],
        project_id=text_claims["projectId"],
        run_id=text_claims["runId"],
        issued_at=issued_at,
        expires_at=expires_at,
        method=text_claims["method"],
        path=text_claims["path"],
        body_sha256=text_claims["bodySha256"],
    )


def require_run_id(claims: InternalClaims, request_id: str) -> None:
    if not hmac.compare_digest(claims.run_id, request_id):
        raise InternalTokenError("Internal token runId does not match the request.")


def _segments(token: str) -> tuple[str, str, str]:
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise InternalTokenError("Internal token format is invalid.")
    try:
        for part in parts:
            part.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InternalTokenError("Internal token encoding is invalid.") from exc
    return parts[0], parts[1], parts[2]


def _decode_segment(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise InternalTokenError("Internal token encoding is invalid.") from exc


def _json_object(raw: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InternalTokenError("Internal token contains duplicate claims.")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InternalTokenError("Internal token JSON is invalid.") from exc
    if not isinstance(value, dict):
        raise InternalTokenError("Internal token JSON must be an object.")
    return value


def _text_claim(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > 500:
        raise InternalTokenError(f"Internal token claim {name} is invalid.")
    return value


def _numeric_date(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InternalTokenError(f"Internal token claim {name} is invalid.")
    return value

"""Publish verified RatsNestPro deliverables without exposing local paths."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

DeliveryStatus = Literal[
    "execution_blocked",
    "delivered_with_issues",
    "release_ready",
]

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_EXCLUDED_PREFIXES = ("temporal_input-", "llm_outputs")
_EXCLUDED_NAMES = {"temporal_recovery.json", ".pipeline.lock"}
_EXCLUDED_SUFFIXES = {".lock", ".tmp", ".temp", ".part"}
_MANIFEST_DIGEST_FIELDS = (
    "artifact_id",
    "kind",
    "media_type",
    "name",
    "object_key",
    "sha256",
    "size_bytes",
)


def artifact_workspace_root() -> Path:
    return Path(os.getenv("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro")).expanduser().resolve()


def _storage_setting(name: str, legacy_name: str, default: str = "") -> str:
    """Read the control-plane storage name while preserving old deployments."""

    return os.getenv(name, os.getenv(legacy_name, default)).strip()


def normalize_delivery_status(value: object) -> DeliveryStatus:
    """Map the pre-Increment-7 status name while keeping one public vocabulary."""

    status = str(value or "execution_blocked")
    if status == "completed_with_issues":
        status = "delivered_with_issues"
    if status not in {"execution_blocked", "delivered_with_issues", "release_ready"}:
        return "execution_blocked"
    return status  # type: ignore[return-value]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_path(path: Path) -> Path:
    """Use Win32's extended path form for deeply nested local artifacts."""

    if os.name != "nt":
        return path
    value = str(path)
    if value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".kicad_sch": "kicad_schematic",
        ".kicad_pcb": "kicad_pcb",
        ".dsn": "freerouting_dsn",
        ".ses": "freerouting_ses",
        ".csv": "manufacturing_table",
        ".gbr": "gerber",
        ".drl": "drill",
        ".pdf": "report",
        ".md": "report",
        ".zip": "manufacturing_bundle",
    }.get(suffix, "project_file")


def _is_publishable(path: Path) -> bool:
    name = path.name.lower()
    return not (
        name in _EXCLUDED_NAMES
        or name.startswith(_EXCLUDED_PREFIXES)
        or path.suffix.lower() in _EXCLUDED_SUFFIXES
        or "transcript" in name
    )


def _verified_files(paths: list[str], workspace: Path) -> list[Path]:
    if not paths:
        return []
    root = workspace.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("artifact workspace must be a directory")
    verified: list[Path] = []
    seen: set[Path] = set()
    for value in paths:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved in seen or not resolved.is_file() or not _is_publishable(resolved):
            continue
        seen.add(resolved)
        verified.append(resolved)
    return sorted(verified, key=lambda item: str(item.relative_to(root)))


def _run_uuid(run_id: str) -> UUID:
    try:
        return UUID(run_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"ratsnest-run:{run_id}")


def _object_key(run_id: UUID, digest: str, filename: str) -> str:
    safe_name = _SAFE_NAME.sub("-", filename).strip(".-")[:120] or "artifact"
    return f"runs/{run_id}/{digest}/{safe_name}"


def _publish_local(source: Path, object_key: str, digest: str) -> None:
    root = Path(
        os.getenv(
            "RATSNEST_ARTIFACT_LOCAL_ROOT",
            str(Path(os.getenv("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro")) / "artifacts"),
        )
    ).expanduser().resolve()
    target = (root / object_key).resolve()
    target.relative_to(root)
    native_target = _native_path(target)
    native_target.parent.mkdir(parents=True, exist_ok=True)
    if not native_target.is_file() or _sha256(native_target) != digest:
        shutil.copyfile(_native_path(source), native_target)


def _publish_s3(source: Path, object_key: str, digest: str, media_type: str) -> None:
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError("S3 artifact backend requires the direct boto3 dependency") from exc

    bucket = _storage_setting("RATSNEST_ARTIFACT_BUCKET", "RATSNEST_ARTIFACT_S3_BUCKET")
    if not bucket:
        raise ValueError("RATSNEST_ARTIFACT_BUCKET is required for the S3 backend")
    path_style = _storage_setting(
        "RATSNEST_ARTIFACT_PATH_STYLE",
        "RATSNEST_ARTIFACT_S3_PATH_STYLE",
        "true",
    ).lower() in {"1", "true", "yes", "on"}
    client = boto3.client(
        "s3",
        endpoint_url=(
            _storage_setting("RATSNEST_ARTIFACT_ENDPOINT", "RATSNEST_ARTIFACT_S3_ENDPOINT")
            or None
        ),
        region_name=(
            _storage_setting("RATSNEST_ARTIFACT_REGION", "RATSNEST_ARTIFACT_S3_REGION")
            or None
        ),
        config=Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path" if path_style else "virtual"},
        ),
    )
    exists = False
    try:
        head = client.head_object(Bucket=bucket, Key=object_key)
        exists = str(head.get("Metadata", {}).get("sha256", "")) == digest
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
    if not exists:
        extra: dict[str, Any] = {
            "ContentType": media_type,
            "Metadata": {"sha256": digest},
        }
        encryption = _storage_setting(
            "RATSNEST_ARTIFACT_SSE",
            "RATSNEST_ARTIFACT_S3_SSE",
            "AES256",
        )
        if encryption.lower() not in {"", "false", "none", "off"}:
            extra["ServerSideEncryption"] = encryption
        client.upload_file(str(source), bucket, object_key, ExtraArgs=extra)


def publish_artifact_manifest(
    *,
    paths: list[str],
    workspace: str,
    run_id: str,
    delivery_status: object,
) -> dict[str, Any]:
    """Publish a content-addressed, idempotent manifest for one hardware run."""

    backend = os.getenv("RATSNEST_ARTIFACT_BACKEND", "local").strip().lower()
    root = Path(workspace)
    run_uuid = _run_uuid(run_id)
    artifacts: list[dict[str, Any]] = []
    errors: list[str] = []
    if backend not in {"local", "s3"}:
        sources: list[Path] = []
        errors.append("artifact storage backend configuration is invalid")
    else:
        try:
            sources = _verified_files(paths, root)
        except (OSError, ValueError):
            sources = []
            errors.append("artifact workspace validation failed")
    for source in sources:
        try:
            digest = _sha256(source)
            media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            object_key = _object_key(run_uuid, digest, source.name)
            if backend == "s3":
                _publish_s3(source, object_key, digest, media_type)
            else:
                _publish_local(source, object_key, digest)
            artifacts.append(
                {
                    "artifact_id": str(
                        uuid5(NAMESPACE_URL, f"ratsnest-artifact:{run_uuid}:{object_key}")
                    ),
                    "name": source.name,
                    "relative_path": source.relative_to(root).as_posix(),
                    "kind": _artifact_kind(source),
                    "media_type": media_type,
                    "size_bytes": source.stat().st_size,
                    "sha256": digest,
                    "object_key": object_key,
                }
            )
        except Exception as exc:  # noqa: BLE001 - external storage boundary
            errors.append(f"artifact publication failed for {source.name} ({type(exc).__name__})")
    artifacts.sort(key=lambda item: item["artifact_id"])
    status = normalize_delivery_status(delivery_status)
    if not artifacts:
        errors.append("no publishable artifact files were found in the run workspace")
    if errors:
        status = "execution_blocked"
    canonical_artifacts = [
        {field: artifact[field] for field in _MANIFEST_DIGEST_FIELDS}
        for artifact in artifacts
    ]
    manifest_seed = json.dumps(
        canonical_artifacts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    manifest_digest = hashlib.sha256(manifest_seed.encode("utf-8")).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "manifest_id": str(
            uuid5(NAMESPACE_URL, f"ratsnest-manifest:{run_uuid}:{manifest_digest}")
        ),
        "manifest_digest": manifest_digest,
        "delivery_status": status,
        "storage_backend": backend,
        "artifacts": artifacts,
    }
    if errors:
        manifest["errors"] = errors
    return manifest

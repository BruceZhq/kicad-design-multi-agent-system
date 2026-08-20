"""Strict, immutable registry for versioned hardware capability boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EXPECTED_REFERENCES = frozenset(
    {
        "sipi-channel-pdn-eval@1.0",
        "telecom-48v-power-monitor@1.0",
        "site-control-telemetry@1.0",
        "site-control-telemetry@1.1",
        "sfp-sync-interface@1.0",
        "radio-control-monitor@1.0",
    }
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProfileScope(_StrictModel):
    supported: list[str] = Field(min_length=1, max_length=16)
    excluded: list[str] = Field(min_length=1, max_length=16)


class ToolchainSummary(_StrictModel):
    required: list[str] = Field(min_length=1, max_length=16)
    optional: list[str] = Field(default_factory=list, max_length=16)


class ProfileBudget(_StrictModel):
    max_wall_clock_minutes: int = Field(ge=1, le=600)
    max_llm_tokens: int = Field(ge=1_000, le=2_000_000)
    max_ahe_repairs: int = Field(ge=0, le=12)
    max_same_failure_retries: int = Field(ge=0, le=4)


class AcceptanceRules(_StrictModel):
    required_artifacts: list[str] = Field(min_length=1, max_length=16)
    verification: list[str] = Field(min_length=1, max_length=24)
    delivery_policy: str = Field(min_length=1, max_length=500)


class CapabilityProfileManifest(_StrictModel):
    schema_version: str = Field(pattern=r"^1$")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    version: str = Field(
        max_length=32,
        pattern=r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))?$",
    )
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    scope: ProfileScope
    constraints: list[str] = Field(min_length=1, max_length=32)
    evidence_requirements: list[str] = Field(min_length=1, max_length=24)
    toolchain: ToolchainSummary
    budget: ProfileBudget
    acceptance: AcceptanceRules

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


class CapabilityProfileSelection(_StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    version: str = Field(
        max_length=32,
        pattern=r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))?$",
    )
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapabilityProfileSnapshot(_StrictModel):
    id: str
    version: str
    reference: str
    digest: str
    title: str
    description: str
    manifest: CapabilityProfileManifest

    def selection(self) -> CapabilityProfileSelection:
        return CapabilityProfileSelection(
            id=self.id,
            version=self.version,
            digest=self.digest,
        )


class ProfileRegistryError(ValueError):
    """Raised when a manifest or requested immutable profile is invalid."""


def _canonical_digest(manifest: CapabilityProfileManifest) -> str:
    canonical = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class CapabilityProfileRegistry:
    def __init__(self, directory: Path | None = None) -> None:
        profile_dir = directory or Path(__file__).resolve().parent
        snapshots: dict[str, CapabilityProfileSnapshot] = {}
        for path in sorted(profile_dir.glob("*.json")):
            try:
                manifest = CapabilityProfileManifest.model_validate_json(
                    path.read_text(encoding="utf-8"),
                    strict=True,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed at registry boundary
                raise ProfileRegistryError(f"Invalid capability manifest {path.name}: {exc}") from exc
            if path.stem != manifest.reference:
                raise ProfileRegistryError(
                    f"Manifest filename {path.name} does not match {manifest.reference}"
                )
            if manifest.reference in snapshots:
                raise ProfileRegistryError(f"Duplicate capability profile {manifest.reference}")
            snapshots[manifest.reference] = CapabilityProfileSnapshot(
                id=manifest.id,
                version=manifest.version,
                reference=manifest.reference,
                digest=_canonical_digest(manifest),
                title=manifest.title,
                description=manifest.description,
                manifest=manifest,
            )
        if frozenset(snapshots) != EXPECTED_REFERENCES:
            raise ProfileRegistryError(
                "Capability registry does not match the registered production profile revisions"
            )
        self._snapshots = snapshots

    def all(self) -> tuple[CapabilityProfileSnapshot, ...]:
        return tuple(self._snapshots[key] for key in sorted(self._snapshots))

    def resolve(self, raw_selection: Any) -> CapabilityProfileSnapshot:
        try:
            selection = CapabilityProfileSelection.model_validate(raw_selection, strict=True)
        except Exception as exc:  # noqa: BLE001 - untrusted per-run configuration
            raise ProfileRegistryError("capability_profile must contain id, version, and digest") from exc
        reference = f"{selection.id}@{selection.version}"
        snapshot = self._snapshots.get(reference)
        if snapshot is None:
            raise ProfileRegistryError(f"Unsupported capability profile {reference}")
        if selection.digest != snapshot.digest:
            raise ProfileRegistryError(f"Capability profile digest mismatch for {reference}")
        return snapshot

    def verify_snapshot(self, raw_snapshot: Any) -> CapabilityProfileSnapshot:
        try:
            saved = CapabilityProfileSnapshot.model_validate(raw_snapshot, strict=True)
        except Exception as exc:  # noqa: BLE001 - checkpoint is persistent input
            raise ProfileRegistryError("Saved capability profile snapshot is invalid") from exc
        current = self.resolve(saved.selection())
        if saved != current:
            raise ProfileRegistryError(
                f"Saved capability profile snapshot changed for {saved.reference}"
            )
        return current


REGISTRY = CapabilityProfileRegistry()


def get_profile_metadata() -> list[dict[str, str]]:
    return [
        {
            "id": snapshot.id,
            "version": snapshot.version,
            "digest": snapshot.digest,
            "title": snapshot.title,
            "description": snapshot.description,
        }
        for snapshot in REGISTRY.all()
    ]


def render_profile_boundary(raw_snapshot: Any) -> str:
    snapshot = REGISTRY.verify_snapshot(raw_snapshot)
    return json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False)


def gate_build_profile(
    selection: Any,
    prior_snapshot: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a build profile and prevent a resume from changing its boundary."""

    try:
        prior = REGISTRY.verify_snapshot(prior_snapshot) if prior_snapshot else None
        selected = REGISTRY.resolve(selection) if selection is not None else prior
        if selected is None:
            raise ProfileRegistryError("A capability profile is required for a build")
        if prior is not None and selected.selection() != prior.selection():
            raise ProfileRegistryError(
                f"A resumed run must keep capability profile {prior.reference}"
            )
        return selected.model_dump(mode="json"), None
    except ProfileRegistryError as exc:
        return None, str(exc)

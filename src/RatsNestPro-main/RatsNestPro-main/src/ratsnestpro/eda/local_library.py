"""Evidence-gated generation of workspace-local KiCad device libraries.

This module closes one narrow capability gap: an exact device may be absent
from the installed KiCad libraries even though official documentation contains
everything required to define it.  Generation is allowed only from a complete
structured specification.  It is not a symbol-name guessing or datasheet
scraping layer.

Safety properties:

* exact device identity is carried into the symbol ``Value`` and provenance;
* every logical pin maps bijectively to one physical pad;
* pin table, package dimensions and land pattern all need page-level citations
  on a manufacturer-controlled domain;
* a committed device definition cannot be silently replaced;
* a cross-process lock serializes the shared symbol library update;
* all files use atomic replacement and resolver indexes are invalidated.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Collection
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

from ratsnestpro.eda.library_roots import (
    default_generated_library_root,
    generated_library_roots,
    register_generated_library_root,
)
from ratsnestpro.eda.vendor.library import (
    create_footprint,
    create_symbol,
    register_library,
)

GENERATED_LIBRARY_NICKNAME = "RatsNestGenerated"
_LIBRARY_NICKNAME = GENERATED_LIBRARY_NICKNAME
_REQUIRED_EVIDENCE = {
    "identity",
    "pin_table",
    "package_dimensions",
    "land_pattern",
}
_SYMBOL_ONLY_REQUIRED_EVIDENCE = {
    "identity",
    "pin_table",
    "package_dimensions",
}
_FOOTPRINT_PAD_HEAD_RE = re.compile(
    r'\(\s*pad\s+(?:"((?:\\.|[^"\\])*)"|([^\s()]+))(?=\s)',
)

ElectricalType = Literal[
    "input",
    "output",
    "bidirectional",
    "tri_state",
    "passive",
    "power_in",
    "power_out",
    "open_collector",
    "open_emitter",
    "unspecified",
    "no_connect",
]
PadType = Literal["smd", "thru_hole"]
PadShape = Literal["circle", "rect", "oval", "roundrect", "trapezoid"]
EvidenceCoverage = Literal[
    "identity",
    "pin_table",
    "package_dimensions",
    "land_pattern",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _clean_identifier(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be empty")
    return cleaned


class LocalPinSpec(_StrictModel):
    number: str
    pad_number: str
    name: str
    electrical_type: ElectricalType

    _strip_number = field_validator("number", "pad_number", "name")(_clean_identifier)


class LocalPadSpec(_StrictModel):
    number: str
    x_mm: float
    y_mm: float
    size_x_mm: float = Field(gt=0)
    size_y_mm: float = Field(gt=0)
    pad_type: PadType = "smd"
    drill_mm: float | None = Field(default=None, gt=0)
    shape: PadShape = "rect"
    layers: list[str] = Field(
        default_factory=lambda: ["F.Cu", "F.Paste", "F.Mask"],
        min_length=1,
    )

    _strip_number = field_validator("number")(_clean_identifier)

    @field_validator("layers")
    @classmethod
    def _unique_layers(cls, value: list[str]) -> list[str]:
        cleaned = [_clean_identifier(item) for item in value]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("layers must be unique")
        return cleaned


class LocalPackageSpec(_StrictModel):
    name: str
    body_width_mm: float = Field(gt=0)
    body_height_mm: float = Field(gt=0)
    courtyard_clearance_mm: float = Field(default=0.25, ge=0)
    mount_type: Literal["smd", "tht"]

    _strip_name = field_validator("name")(_clean_identifier)


class OfficialEvidence(_StrictModel):
    device_id: str
    url: HttpUrl
    page_numbers: list[int] = Field(min_length=1)
    document_id: str
    covers: list[EvidenceCoverage] = Field(min_length=1)

    _strip_text = field_validator("device_id", "document_id")(_clean_identifier)

    @field_validator("page_numbers")
    @classmethod
    def _valid_pages(cls, value: list[int]) -> list[int]:
        if any(page < 1 for page in value):
            raise ValueError("page_numbers must be positive")
        if len(set(value)) != len(value):
            raise ValueError("page_numbers must be unique")
        return value

    @field_validator("covers")
    @classmethod
    def _unique_coverage(cls, value: list[EvidenceCoverage]) -> list[EvidenceCoverage]:
        if len(set(value)) != len(value):
            raise ValueError("covers must be unique")
        return value


class LocalDeviceLibrarySpec(_StrictModel):
    """Complete, cited electrical and land-pattern definition for one device."""

    device_id: str
    manufacturer: str
    official_domains: list[str] = Field(min_length=1)
    declared_pin_count: int = Field(gt=0)
    declared_pad_count: int = Field(gt=0)
    pins: list[LocalPinSpec] = Field(min_length=1)
    pads: list[LocalPadSpec] = Field(min_length=1)
    package: LocalPackageSpec
    evidence: list[OfficialEvidence] = Field(min_length=1)

    _strip_text = field_validator("device_id", "manufacturer")(_clean_identifier)

    @field_validator("official_domains")
    @classmethod
    def _valid_official_domains(cls, value: list[str]) -> list[str]:
        domains: list[str] = []
        for item in value:
            domain = item.strip().lower().rstrip(".")
            if not domain or "://" in domain or "/" in domain or "." not in domain:
                raise ValueError(f"invalid official domain: {item!r}")
            domains.append(domain)
        if len(set(domains)) != len(domains):
            raise ValueError("official_domains must be unique")
        return domains

    @model_validator(mode="after")
    def _validate_identity_and_mapping(self) -> LocalDeviceLibrarySpec:
        pin_numbers = [pin.number for pin in self.pins]
        mapped_pad_numbers = [pin.pad_number for pin in self.pins]
        pad_numbers = [pad.number for pad in self.pads]
        if self.declared_pin_count != len(self.pins):
            raise ValueError("declared_pin_count does not equal the complete pin table")
        if self.declared_pad_count != len(self.pads):
            raise ValueError("declared_pad_count does not equal the complete pad table")
        if len(set(pin_numbers)) != len(pin_numbers):
            raise ValueError("logical pin numbers must be unique")
        if len(set(mapped_pad_numbers)) != len(mapped_pad_numbers):
            raise ValueError("pin-to-pad mapping must be one-to-one")
        if len(set(pad_numbers)) != len(pad_numbers):
            raise ValueError("physical pad numbers must be unique")
        if set(mapped_pad_numbers) != set(pad_numbers):
            raise ValueError("pin-to-pad mapping must cover every and only physical pad")
        # KiCad connects a symbol pin to a footprint pad by this exact number.
        if any(pin.number != pin.pad_number for pin in self.pins):
            raise ValueError("KiCad pin number must equal its mapped footprint pad number")
        if self.declared_pin_count != self.declared_pad_count:
            raise ValueError("one-to-one generation requires equal pin and pad counts")

        identity = self.device_id.casefold()
        domains = set(self.official_domains)
        for source in self.evidence:
            if source.device_id.casefold() != identity:
                raise ValueError(
                    "official evidence device_id must exactly match the requested device_id"
                )
            host = (urlparse(str(source.url)).hostname or "").lower().rstrip(".")
            if source.url.scheme != "https":
                raise ValueError("official evidence URLs must use HTTPS")
            if not any(host == domain or host.endswith(f".{domain}") for domain in domains):
                raise ValueError(
                    f"evidence URL host {host!r} is not under an official manufacturer domain"
                )

        expected_pad_type = "smd" if self.package.mount_type == "smd" else "thru_hole"
        if any(pad.pad_type != expected_pad_type for pad in self.pads):
            raise ValueError(f"all electrical pads must use {expected_pad_type!r} for this package")
        for pad in self.pads:
            if pad.pad_type == "smd" and pad.drill_mm is not None:
                raise ValueError("SMD pads cannot define a drill diameter")
            if pad.pad_type == "thru_hole":
                if pad.drill_mm is None:
                    raise ValueError("through-hole pads require a drill diameter")
                if pad.drill_mm >= min(pad.size_x_mm, pad.size_y_mm):
                    raise ValueError("through-hole drill must be smaller than the pad")
                if "*.Cu" not in pad.layers or "*.Mask" not in pad.layers:
                    raise ValueError("through-hole electrical pads require *.Cu and *.Mask layers")
        return self


class LocalSymbolLibrarySpec(_StrictModel):
    """Exact symbol definition backed by a real, allowlisted KiCad footprint."""

    device_id: str
    manufacturer: str
    official_domains: list[str] = Field(min_length=1)
    declared_pin_count: int = Field(gt=0)
    pins: list[LocalPinSpec] = Field(min_length=1)
    package_name: str
    footprint_lib_id: str
    evidence: list[OfficialEvidence] = Field(min_length=1)

    _strip_text = field_validator(
        "device_id",
        "manufacturer",
        "package_name",
        "footprint_lib_id",
    )(_clean_identifier)

    @field_validator("official_domains")
    @classmethod
    def _valid_official_domains(cls, value: list[str]) -> list[str]:
        domains: list[str] = []
        for item in value:
            domain = item.strip().lower().rstrip(".")
            if not domain or "://" in domain or "/" in domain or "." not in domain:
                raise ValueError(f"invalid official domain: {item!r}")
            domains.append(domain)
        if len(set(domains)) != len(domains):
            raise ValueError("official_domains must be unique")
        return domains

    @field_validator("footprint_lib_id")
    @classmethod
    def _valid_footprint_lib_id(cls, value: str) -> str:
        if value.count(":") != 1 or any(not part.strip() for part in value.split(":")):
            raise ValueError("footprint_lib_id must be an exact KiCad Lib:Name identifier")
        return value

    @model_validator(mode="after")
    def _validate_identity_and_mapping(self) -> LocalSymbolLibrarySpec:
        pin_numbers = [pin.number for pin in self.pins]
        mapped_pad_numbers = [pin.pad_number for pin in self.pins]
        if self.declared_pin_count != len(self.pins):
            raise ValueError("declared_pin_count does not equal the complete pin table")
        if len(set(pin_numbers)) != len(pin_numbers):
            raise ValueError("logical pin numbers must be unique")
        if len(set(mapped_pad_numbers)) != len(mapped_pad_numbers):
            raise ValueError("pin-to-pad mapping must be one-to-one")
        if any(pin.number != pin.pad_number for pin in self.pins):
            raise ValueError("KiCad pin number must equal its mapped footprint pad number")

        identity = self.device_id.casefold()
        domains = set(self.official_domains)
        for source in self.evidence:
            if source.device_id.casefold() != identity:
                raise ValueError(
                    "official evidence device_id must exactly match the requested device_id"
                )
            host = (urlparse(str(source.url)).hostname or "").lower().rstrip(".")
            if source.url.scheme != "https":
                raise ValueError("official evidence URLs must use HTTPS")
            if not any(host == domain or host.endswith(f".{domain}") for domain in domains):
                raise ValueError(
                    f"evidence URL host {host!r} is not under an official manufacturer domain"
                )
        return self


class LocalLibraryCapabilityGap(_StrictModel):
    code: str
    message: str
    required_capability: str = "evidence_grounded_local_kicad_library"
    missing_fields: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class LocalLibraryArtifacts(_StrictModel):
    root: str
    symbol_lib_id: str
    footprint_lib_id: str
    symbol_library_path: str
    footprint_path: str
    provenance_path: str
    definition_sha256: str


class LocalLibraryGenerationResult(_StrictModel):
    status: Literal["generated", "existing", "capability_gap"]
    artifacts: LocalLibraryArtifacts | None = None
    capability_gap: LocalLibraryCapabilityGap | None = None


class _LockTimeout(RuntimeError):
    pass


@contextmanager
def _exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float = 10.0,
    stale_after_seconds: float = 120.0,
):
    """Small dependency-free cross-process lock for one generated root."""

    deadline = time.monotonic() + timeout_seconds
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > stale_after_seconds
            except FileNotFoundError:
                continue
            if stale:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise _LockTimeout(f"timed out waiting for {path}") from None
            time.sleep(0.025)
            continue
        try:
            os.write(
                descriptor,
                f"pid={os.getpid()} created={time.time():.6f}\n".encode(),
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        break
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _safe_stem(value: str, *, limit: int = 48) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._-")
    return (stem or "device")[:limit]


def _names(spec: LocalDeviceLibrarySpec) -> tuple[str, str]:
    identity_hash = hashlib.sha256(spec.device_id.casefold().encode()).hexdigest()[:10]
    device = _safe_stem(spec.device_id)
    package = _safe_stem(spec.package.name, limit=32)
    return f"{device}_{identity_hash}", f"{device}_{package}_{identity_hash}"


def _symbol_name(device_id: str) -> str:
    identity_hash = hashlib.sha256(device_id.casefold().encode()).hexdigest()[:10]
    return f"{_safe_stem(device_id)}_{identity_hash}"


def _definition_payload(spec: LocalDeviceLibrarySpec) -> dict[str, Any]:
    return spec.model_dump(
        mode="json",
        include={
            "device_id",
            "declared_pin_count",
            "declared_pad_count",
            "pins",
            "pads",
            "package",
        },
    )


def _symbol_only_definition_payload(
    spec: LocalSymbolLibrarySpec,
    *,
    footprint_sha256: str,
) -> dict[str, Any]:
    payload = spec.model_dump(
        mode="json",
        include={
            "device_id",
            "declared_pin_count",
            "pins",
            "package_name",
            "footprint_lib_id",
        },
    )
    payload["mode"] = "symbol_only"
    payload["footprint_sha256"] = footprint_sha256
    return payload


def _gap(
    code: str,
    message: str,
    *,
    missing_fields: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> LocalLibraryGenerationResult:
    return LocalLibraryGenerationResult(
        status="capability_gap",
        capability_gap=LocalLibraryCapabilityGap(
            code=code,
            message=message,
            missing_fields=missing_fields or [],
            details=details or {},
        ),
    )


def _validation_gap(exc: ValidationError) -> LocalLibraryGenerationResult:
    errors = exc.errors(include_url=False)
    fields = []
    for error in errors:
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = str(error.get("msg", "invalid value"))
        fields.append(f"{location}: {message}" if location else message)
    return _gap(
        "invalid_local_library_spec",
        "Local KiCad generation requires an exact, complete device definition.",
        missing_fields=fields,
        details={"validation_errors": errors},
    )


def _symbol_pin_geometry(
    spec: LocalDeviceLibrarySpec | LocalSymbolLibrarySpec,
) -> tuple[list[dict[str, Any]], float]:
    rows = max(1, math.ceil(len(spec.pins) / 2))
    top = (rows - 1) * 1.27
    pins: list[dict[str, Any]] = []
    for index, pin in enumerate(spec.pins):
        left = index < rows
        row = index if left else index - rows
        pins.append(
            {
                "number": pin.pad_number,
                "name": pin.name,
                "type": pin.electrical_type,
                "x": -5.08 if left else 5.08,
                "y": top - row * 2.54,
                "angle": 0 if left else 180,
                "length": 2.54,
            }
        )
    return pins, max(5.08, rows * 2.54)


def _pad_geometry(spec: LocalDeviceLibrarySpec) -> list[dict[str, Any]]:
    return [
        {
            "number": pad.number,
            "x": pad.x_mm,
            "y": pad.y_mm,
            "size_x": pad.size_x_mm,
            "size_y": pad.size_y_mm,
            "type": pad.pad_type,
            "shape": pad.shape,
            "layers": pad.layers,
            "drill": pad.drill_mm,
        }
        for pad in spec.pads
    ]


def _invalidate_caches() -> None:
    from ratsnestpro.eda import footprints, symbols
    from ratsnestpro.eda.grounding import invalidate_library_indexes

    footprints.invalidate_caches()
    symbols._load_lib_node.cache_clear()
    invalidate_library_indexes()


def _verify_artifacts(
    spec: LocalDeviceLibrarySpec,
    artifacts: LocalLibraryArtifacts,
) -> list[str]:
    from ratsnestpro.eda import footprints, symbols

    _invalidate_caches()
    errors: list[str] = []
    symbol = symbols.symbol_info(artifacts.symbol_lib_id)
    if symbol is None:
        errors.append(f"generated symbol cannot be resolved: {artifacts.symbol_lib_id}")
    else:
        actual_pins = [pin["number"] for pin in symbol["pins"]]
        expected_pins = [pin.pad_number for pin in spec.pins]
        if len(actual_pins) != len(set(actual_pins)) or set(actual_pins) != set(expected_pins):
            errors.append("generated symbol pin numbers do not match the cited pin table")
        properties = symbol["properties"]
        if properties.get("Value") != spec.device_id:
            errors.append("generated symbol Value does not preserve exact device identity")
        if properties.get("Footprint") != artifacts.footprint_lib_id:
            errors.append("generated symbol points at a different footprint identity")

    actual_pads = footprints.footprint_pads(artifacts.footprint_lib_id)
    expected_pads = [pad.number for pad in spec.pads]
    if actual_pads is None:
        errors.append(f"generated footprint cannot be resolved: {artifacts.footprint_lib_id}")
    else:
        pad_numbers = [pad["number"] for pad in actual_pads]
        if len(pad_numbers) != len(set(pad_numbers)) or set(pad_numbers) != set(expected_pads):
            errors.append("generated footprint pad numbers do not match the cited land pattern")

    courtyard = footprints.footprint_courtyard_bbox(artifacts.footprint_lib_id)
    clearance = spec.package.courtyard_clearance_mm
    expected_courtyard = (
        -spec.package.body_width_mm / 2 - clearance,
        -spec.package.body_height_mm / 2 - clearance,
        spec.package.body_width_mm / 2 + clearance,
        spec.package.body_height_mm / 2 + clearance,
    )
    if courtyard is None or any(
        not math.isclose(actual, expected, abs_tol=1e-6)
        for actual, expected in zip(courtyard or (), expected_courtyard, strict=True)
    ):
        errors.append("generated courtyard does not match cited package dimensions")
    return errors


def _reused_footprint_state(lib_id: str) -> tuple[Path, list[str], str] | None:
    from ratsnestpro.eda import footprints

    path = footprints.resolve_footprint(lib_id)
    if path is None:
        return None
    resolved = path.resolve(strict=True)
    nickname, name = lib_id.split(":", 1)
    if resolved.parent.name != f"{nickname}.pretty" or resolved.stem != name:
        # The vendored resolver has a useful name-only fallback for interactive
        # search. Evidence-gated generation must bind the exact library ID.
        return None
    content = resolved.read_bytes()
    source = content.decode("utf-8")
    pad_numbers = [
        (quoted.replace(r"\"", '"').replace(r"\\", "\\") if quoted is not None else atom)
        for quoted, atom in _FOOTPRINT_PAD_HEAD_RE.findall(source)
    ]
    electrical_pad_numbers = [number for number in pad_numbers if number]
    return resolved, electrical_pad_numbers, hashlib.sha256(content).hexdigest()


def _verify_symbol_only_artifacts(
    spec: LocalSymbolLibrarySpec,
    artifacts: LocalLibraryArtifacts,
    *,
    expected_footprint_path: Path,
    expected_footprint_sha256: str,
) -> list[str]:
    from ratsnestpro.eda import symbols

    _invalidate_caches()
    errors: list[str] = []
    symbol = symbols.symbol_info(artifacts.symbol_lib_id)
    expected_pins = [pin.pad_number for pin in spec.pins]
    if symbol is None:
        errors.append(f"generated symbol cannot be resolved: {artifacts.symbol_lib_id}")
    else:
        actual_pins = [pin["number"] for pin in symbol["pins"]]
        if len(actual_pins) != len(set(actual_pins)) or set(actual_pins) != set(expected_pins):
            errors.append("generated symbol pin numbers do not match the cited pin table")
        properties = symbol["properties"]
        if properties.get("Value") != spec.device_id:
            errors.append("generated symbol Value does not preserve exact device identity")
        if properties.get("Footprint") != spec.footprint_lib_id:
            errors.append("generated symbol points at a different footprint identity")

    state = _reused_footprint_state(spec.footprint_lib_id)
    if state is None:
        errors.append(f"reused footprint cannot be resolved exactly: {spec.footprint_lib_id}")
    else:
        footprint_path, pad_numbers, footprint_sha256 = state
        if footprint_path != expected_footprint_path:
            errors.append("reused footprint resolved path changed during symbol generation")
        if footprint_sha256 != expected_footprint_sha256:
            errors.append("reused footprint content changed during symbol generation")
        if len(pad_numbers) != len(set(pad_numbers)) or set(pad_numbers) != set(expected_pins):
            errors.append("reused footprint pad numbers do not match the cited pin table")
    return errors


def _read_provenance(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def generate_local_symbol_library(
    raw_spec: LocalSymbolLibrarySpec | dict[str, Any],
    *,
    allowed_footprint_lib_ids: Collection[str],
    root: str | os.PathLike[str] | None = None,
    project_dir: str | os.PathLike[str] | None = None,
    lock_timeout_seconds: float = 10.0,
) -> LocalLibraryGenerationResult:
    """Generate only an exact symbol and bind it to an installed footprint.

    ``allowed_footprint_lib_ids`` is deliberately outside the structured spec:
    it must come from deterministic library discovery rather than the model
    producing the pin table. The selected footprint is never written or
    registered as a generated footprint library.
    """

    try:
        spec = (
            raw_spec
            if isinstance(raw_spec, LocalSymbolLibrarySpec)
            else LocalSymbolLibrarySpec.model_validate(raw_spec)
        )
    except ValidationError as exc:
        return _validation_gap(exc)

    covered = {coverage for source in spec.evidence for coverage in source.covers}
    missing_evidence = sorted(_SYMBOL_ONLY_REQUIRED_EVIDENCE - covered)
    if missing_evidence:
        return _gap(
            "insufficient_official_evidence",
            "Official page-level evidence is incomplete; symbol generation would require guessing.",
            missing_fields=missing_evidence,
            details={
                "device_id": spec.device_id,
                "covered": sorted(covered),
                "required": sorted(_SYMBOL_ONLY_REQUIRED_EVIDENCE),
            },
        )

    try:
        if isinstance(allowed_footprint_lib_ids, str):
            raise ValueError("allowlist must be a collection of exact library IDs")
        allowed = {_clean_identifier(item) for item in allowed_footprint_lib_ids}
    except (AttributeError, TypeError, ValueError) as exc:
        return _gap(
            "invalid_footprint_allowlist",
            "Installed-footprint reuse requires a non-empty deterministic allowlist.",
            details={"error": str(exc)},
        )
    if not allowed:
        return _gap(
            "invalid_footprint_allowlist",
            "Installed-footprint reuse requires a non-empty deterministic allowlist.",
        )
    if spec.footprint_lib_id not in allowed:
        return _gap(
            "footprint_not_allowlisted",
            "The structured specification selected a footprint outside deterministic discovery.",
            details={
                "footprint_lib_id": spec.footprint_lib_id,
                "allowed_footprint_lib_ids": sorted(allowed),
            },
        )
    if spec.footprint_lib_id.split(":", 1)[0] == _LIBRARY_NICKNAME:
        return _gap(
            "generated_footprint_cannot_be_reused",
            "Symbol-only mode requires an independently installed footprint.",
            details={"footprint_lib_id": spec.footprint_lib_id},
        )

    output_root = (
        Path(root).expanduser().resolve(strict=False)
        if root
        else (default_generated_library_root())
    )
    try:
        footprint_state = _reused_footprint_state(spec.footprint_lib_id)
    except (OSError, UnicodeError, ValueError) as exc:
        return _gap(
            "invalid_reused_footprint",
            "The allowlisted footprint could not be read as a KiCad footprint.",
            details={"footprint_lib_id": spec.footprint_lib_id, "error": str(exc)},
        )
    if footprint_state is None:
        return _gap(
            "reused_footprint_not_found",
            "The allowlisted footprint library ID does not resolve exactly.",
            details={"footprint_lib_id": spec.footprint_lib_id},
        )
    footprint_path, pad_numbers, footprint_sha256 = footprint_state
    try:
        footprint_path.relative_to(output_root)
    except ValueError:
        pass
    else:
        return _gap(
            "generated_footprint_cannot_be_reused",
            "Symbol-only mode requires a footprint outside the generated-library root.",
            details={"footprint_path": str(footprint_path)},
        )

    expected_pad_numbers = [pin.pad_number for pin in spec.pins]
    if len(pad_numbers) != len(set(pad_numbers)):
        return _gap(
            "reused_footprint_duplicate_pad_numbers",
            "The installed footprint has duplicate electrical pad numbers.",
            details={"footprint_lib_id": spec.footprint_lib_id},
        )
    if set(pad_numbers) != set(expected_pad_numbers):
        return _gap(
            "reused_footprint_pad_mismatch",
            "The installed footprint pad signature does not equal the complete official pin table.",
            details={
                "footprint_lib_id": spec.footprint_lib_id,
                "expected_pad_numbers": sorted(expected_pad_numbers),
                "actual_pad_numbers": sorted(pad_numbers),
            },
        )

    output_root.mkdir(parents=True, exist_ok=True)
    if output_root not in generated_library_roots():
        register_generated_library_root(output_root)

    symbol_name = _symbol_name(spec.device_id)
    symbol_lib_id = f"{_LIBRARY_NICKNAME}:{symbol_name}"
    symbol_library = output_root / f"{_LIBRARY_NICKNAME}.kicad_sym"
    provenance_path = output_root / "provenance" / f"{symbol_name}.json"
    definition_payload = _symbol_only_definition_payload(
        spec,
        footprint_sha256=footprint_sha256,
    )
    definition_hash = _canonical_hash(definition_payload)
    spec_hash = _canonical_hash(spec.model_dump(mode="json"))
    artifacts = LocalLibraryArtifacts(
        root=str(output_root),
        symbol_lib_id=symbol_lib_id,
        footprint_lib_id=spec.footprint_lib_id,
        symbol_library_path=str(symbol_library),
        footprint_path=str(footprint_path),
        provenance_path=str(provenance_path),
        definition_sha256=definition_hash,
    )
    provenance_claim = {
        "generation_mode": "symbol_only",
        "device_id": spec.device_id,
        "manufacturer": spec.manufacturer,
        "symbol_lib_id": symbol_lib_id,
        "footprint_lib_id": spec.footprint_lib_id,
        "definition_sha256": definition_hash,
        "spec_sha256": spec_hash,
        "definition": definition_payload,
        "evidence": [source.model_dump(mode="json") for source in spec.evidence],
        "reused_footprint": {
            "lib_id": spec.footprint_lib_id,
            "resolved_path": str(footprint_path),
            "content_sha256": footprint_sha256,
            "content_size_bytes": footprint_path.stat().st_size,
        },
    }

    try:
        with _exclusive_file_lock(
            output_root / ".ratsnestpro-generated-library.lock",
            timeout_seconds=lock_timeout_seconds,
        ):
            previous = _read_provenance(provenance_path)
            if previous and previous.get("definition_sha256") != definition_hash:
                return _gap(
                    "device_definition_conflict",
                    "A different definition is already bound to this exact device identity.",
                    details={
                        "device_id": spec.device_id,
                        "existing_definition_sha256": previous.get("definition_sha256"),
                        "requested_definition_sha256": definition_hash,
                        "provenance_path": str(provenance_path),
                    },
                )

            if previous and previous.get("state") == "committed":
                verification_errors = _verify_symbol_only_artifacts(
                    spec,
                    artifacts,
                    expected_footprint_path=footprint_path,
                    expected_footprint_sha256=footprint_sha256,
                )
                if not verification_errors:
                    if project_dir is not None:
                        register_library(
                            "sym",
                            _LIBRARY_NICKNAME,
                            str(symbol_library),
                            project_dir=str(Path(project_dir).resolve(strict=False)),
                        )
                    return LocalLibraryGenerationResult(status="existing", artifacts=artifacts)

            provenance = {
                "schema_version": 1,
                "state": "pending",
                **provenance_claim,
                "provenance_sha256": _canonical_hash(provenance_claim),
            }
            _atomic_json(provenance_path, provenance)

            symbol_pins, body_height = _symbol_pin_geometry(spec)
            first_source = spec.evidence[0]
            page_text = ",".join(str(page) for page in first_source.page_numbers)
            description = (
                f"{spec.device_id}; official source {first_source.document_id} "
                f"pages {page_text}; reuses {spec.footprint_lib_id}"
            )
            create_symbol(
                str(symbol_library),
                symbol_name,
                symbol_pins,
                properties={
                    "Reference": "U",
                    "Value": spec.device_id,
                    "Footprint": spec.footprint_lib_id,
                    "Datasheet": str(first_source.url),
                    "Description": description,
                },
                body_width=7.62,
                body_height=body_height,
            )
            _invalidate_caches()

            verification_errors = _verify_symbol_only_artifacts(
                spec,
                artifacts,
                expected_footprint_path=footprint_path,
                expected_footprint_sha256=footprint_sha256,
            )
            if verification_errors:
                return _gap(
                    "generated_library_verification_failed",
                    "Generated symbol or reused footprint failed deterministic verification.",
                    details={
                        "device_id": spec.device_id,
                        "errors": verification_errors,
                        "provenance_path": str(provenance_path),
                    },
                )

            if project_dir is not None:
                register_library(
                    "sym",
                    _LIBRARY_NICKNAME,
                    str(symbol_library),
                    project_dir=str(Path(project_dir).resolve(strict=False)),
                )
            provenance["state"] = "committed"
            _atomic_json(provenance_path, provenance)
            return LocalLibraryGenerationResult(status="generated", artifacts=artifacts)
    except _LockTimeout as exc:
        return _gap(
            "generation_lock_timeout",
            "The shared generated library is busy; retry without changing the specification.",
            details={"root": str(output_root), "error": str(exc)},
        )
    except OSError as exc:
        return _gap(
            "local_library_io_error",
            "The validated local symbol could not be written atomically.",
            details={"root": str(output_root), "error": str(exc)},
        )


def generate_local_library(
    raw_spec: LocalDeviceLibrarySpec | dict[str, Any],
    *,
    root: str | os.PathLike[str] | None = None,
    project_dir: str | os.PathLike[str] | None = None,
    lock_timeout_seconds: float = 10.0,
) -> LocalLibraryGenerationResult:
    """Validate and materialize one exact device in a local KiCad library.

    Invalid or insufficient input is returned as a structured capability gap;
    no device file is written in those cases.  ``project_dir`` is optional and,
    when supplied, receives atomic ``sym-lib-table`` and ``fp-lib-table``
    registrations for the generated library.
    """

    try:
        spec = (
            raw_spec
            if isinstance(raw_spec, LocalDeviceLibrarySpec)
            else LocalDeviceLibrarySpec.model_validate(raw_spec)
        )
    except ValidationError as exc:
        return _validation_gap(exc)

    covered = {coverage for source in spec.evidence for coverage in source.covers}
    missing_evidence = sorted(_REQUIRED_EVIDENCE - covered)
    if missing_evidence:
        return _gap(
            "insufficient_official_evidence",
            "Official page-level evidence is incomplete; generation would require guessing.",
            missing_fields=missing_evidence,
            details={
                "device_id": spec.device_id,
                "covered": sorted(covered),
                "required": sorted(_REQUIRED_EVIDENCE),
            },
        )

    output_root = (
        Path(root).expanduser().resolve(strict=False)
        if root
        else (default_generated_library_root())
    )
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root not in generated_library_roots():
        register_generated_library_root(output_root)

    symbol_name, footprint_name = _names(spec)
    symbol_lib_id = f"{_LIBRARY_NICKNAME}:{symbol_name}"
    footprint_lib_id = f"{_LIBRARY_NICKNAME}:{footprint_name}"
    symbol_library = output_root / f"{_LIBRARY_NICKNAME}.kicad_sym"
    footprint_path = output_root / f"{_LIBRARY_NICKNAME}.pretty" / f"{footprint_name}.kicad_mod"
    provenance_path = output_root / "provenance" / f"{symbol_name}.json"
    definition_payload = _definition_payload(spec)
    definition_hash = _canonical_hash(definition_payload)
    spec_payload = spec.model_dump(mode="json")
    spec_hash = _canonical_hash(spec_payload)
    artifacts = LocalLibraryArtifacts(
        root=str(output_root),
        symbol_lib_id=symbol_lib_id,
        footprint_lib_id=footprint_lib_id,
        symbol_library_path=str(symbol_library),
        footprint_path=str(footprint_path),
        provenance_path=str(provenance_path),
        definition_sha256=definition_hash,
    )

    try:
        with _exclusive_file_lock(
            output_root / ".ratsnestpro-generated-library.lock",
            timeout_seconds=lock_timeout_seconds,
        ):
            previous = _read_provenance(provenance_path)
            if previous and previous.get("definition_sha256") != definition_hash:
                return _gap(
                    "device_definition_conflict",
                    "A different definition is already bound to this exact device identity.",
                    details={
                        "device_id": spec.device_id,
                        "existing_definition_sha256": previous.get("definition_sha256"),
                        "requested_definition_sha256": definition_hash,
                        "provenance_path": str(provenance_path),
                    },
                )

            if previous and previous.get("state") == "committed":
                verification_errors = _verify_artifacts(spec, artifacts)
                if not verification_errors:
                    if project_dir is not None:
                        project = str(Path(project_dir).resolve(strict=False))
                        register_library(
                            "sym",
                            _LIBRARY_NICKNAME,
                            str(symbol_library),
                            project_dir=project,
                        )
                        register_library(
                            "fp",
                            _LIBRARY_NICKNAME,
                            str(footprint_path.parent),
                            project_dir=project,
                        )
                    return LocalLibraryGenerationResult(
                        status="existing",
                        artifacts=artifacts,
                    )

            # The pending marker makes a process crash recoverable without
            # allowing a later, different definition to claim this identity.
            provenance = {
                "schema_version": 1,
                "state": "pending",
                "device_id": spec.device_id,
                "manufacturer": spec.manufacturer,
                "symbol_lib_id": symbol_lib_id,
                "footprint_lib_id": footprint_lib_id,
                "definition_sha256": definition_hash,
                "spec_sha256": spec_hash,
                "definition": definition_payload,
                "evidence": [source.model_dump(mode="json") for source in spec.evidence],
            }
            _atomic_json(provenance_path, provenance)

            symbol_pins, body_height = _symbol_pin_geometry(spec)
            first_source = spec.evidence[0]
            page_text = ",".join(str(page) for page in first_source.page_numbers)
            description = (
                f"{spec.device_id}; official source {first_source.document_id} pages {page_text}"
            )
            create_footprint(
                str(footprint_path.parent),
                footprint_name,
                _pad_geometry(spec),
                descr=description,
                body_width_mm=spec.package.body_width_mm,
                body_height_mm=spec.package.body_height_mm,
                courtyard_clearance_mm=spec.package.courtyard_clearance_mm,
                mount_type=spec.package.mount_type,
            )
            create_symbol(
                str(symbol_library),
                symbol_name,
                symbol_pins,
                properties={
                    "Reference": "U",
                    "Value": spec.device_id,
                    "Footprint": footprint_lib_id,
                    "Datasheet": str(first_source.url),
                    "Description": description,
                },
                body_width=7.62,
                body_height=body_height,
            )
            _invalidate_caches()

            verification_errors = _verify_artifacts(spec, artifacts)
            if verification_errors:
                return _gap(
                    "generated_library_verification_failed",
                    "Generated files failed deterministic identity or geometry verification.",
                    details={
                        "device_id": spec.device_id,
                        "errors": verification_errors,
                        "provenance_path": str(provenance_path),
                    },
                )

            if project_dir is not None:
                project = str(Path(project_dir).resolve(strict=False))
                register_library(
                    "sym",
                    _LIBRARY_NICKNAME,
                    str(symbol_library),
                    project_dir=project,
                )
                register_library(
                    "fp",
                    _LIBRARY_NICKNAME,
                    str(footprint_path.parent),
                    project_dir=project,
                )
            provenance["state"] = "committed"
            _atomic_json(provenance_path, provenance)
            return LocalLibraryGenerationResult(status="generated", artifacts=artifacts)
    except _LockTimeout as exc:
        return _gap(
            "generation_lock_timeout",
            "The shared generated library is busy; retry without changing the specification.",
            details={"root": str(output_root), "error": str(exc)},
        )
    except OSError as exc:
        return _gap(
            "local_library_io_error",
            "The validated local library could not be written atomically.",
            details={"root": str(output_root), "error": str(exc)},
        )


__all__ = [
    "GENERATED_LIBRARY_NICKNAME",
    "LocalDeviceLibrarySpec",
    "LocalSymbolLibrarySpec",
    "LocalLibraryArtifacts",
    "LocalLibraryCapabilityGap",
    "LocalLibraryGenerationResult",
    "generate_local_library",
    "generate_local_symbol_library",
]

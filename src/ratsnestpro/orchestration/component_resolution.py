"""Deterministic library closure for selected physical components.

Selection is not closed merely because a model emitted plausible ``Lib:Name``
strings.  This service resolves each symbol and footprint against the live
KiCad libraries, verifies the electrical pin/pad set, and keeps requested
device identity separate from a library's reusable base symbol.

The service is deliberately independent of the pipeline runner so Architect,
Selection, and external tools can share the same resolution contract.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ratsnestpro.eda import footprints, grounding, symbols
from ratsnestpro.eda.library_roots import (
    default_generated_library_root,
    generated_library_roots,
    register_generated_library_root,
)
from ratsnestpro.eda.vendor.library import create_symbol, register_library
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart

_PLACEHOLDER_LIBRARY = "RatsNestPlaceholder"
_GENERIC_SYMBOL_LIBRARIES = {
    "Connector",
    "Connector_Generic",
    "Jumper",
    "Mechanical",
    "Switch",
}
_PARAMETERIZED_DEVICE_RE = re.compile(
    r"^(?:"
    # These are KiCad electrical primitives whose symbol Value is a class,
    # not a manufacturer identity.  The selected component Value remains a
    # separate identity decision.  Keep the allowlist semantic and bounded;
    # do not make every entry in the broad Device library generic.
    r"[RCLD](?:_|$)|"
    r"Crystal|FerriteBead|Fuse|Polyfuse|Thermistor|Varistor"
    r")",
)


def _is_reusable_generic_symbol(lib_id: str) -> bool:
    """Return whether ``lib_id`` represents topology, not device identity."""

    library, _, symbol_name = lib_id.partition(":")
    return library in _GENERIC_SYMBOL_LIBRARIES or (
        library == "Device" and bool(_PARAMETERIZED_DEVICE_RE.match(symbol_name))
    )
_ELECTRICAL_TYPES = Literal[
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
IdentityMode = Literal["fixed_exact", "family_variant", "capability_only"]
EvidenceCoverage = Literal[
    "capability",
    "electrical",
    "package",
    "pin_topology",
]
_REQUIRED_EQUIVALENCE_EVIDENCE = {
    "capability",
    "electrical",
    "package",
    "pin_topology",
}


class ResolutionStatus(StrEnum):
    """Library-closure outcome for one selected physical component."""

    INSTALLED_EXACT = "installed_exact"
    INSTALLED_QUALIFIED_VALIDATED = "installed_qualified_validated"
    REPLACEABLE_GROUNDED = "replaceable_grounded"
    PLACEHOLDER_VERIFIED_NONRELEASE = "placeholder_verified_nonrelease"
    PLACEHOLDER_UNVERIFIED_NONRELEASE = "placeholder_unverified_nonrelease"
    UNRESOLVED_EVIDENCE_GAP = "unresolved_evidence_gap"
    HARNESS_FAILURE = "harness_failure"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifiedPinEvidence(_StrictModel):
    """One pin copied from an already verified source, never inferred."""

    number: str = Field(min_length=1, max_length=32)
    name: str = Field(default="~", max_length=120)
    electrical_type: _ELECTRICAL_TYPES = "passive"
    evidence_id: str = Field(min_length=1, max_length=500)


class SymbolOnlyPlaceholderSpec(_StrictModel):
    """Evidence required to generate only a symbol for a real footprint."""

    requested_identity: str = Field(min_length=1, max_length=200)
    footprint: str = Field(min_length=3, max_length=240)
    pins: list[VerifiedPinEvidence] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _unique_pin_numbers(self) -> SymbolOnlyPlaceholderSpec:
        numbers = [pin.number for pin in self.pins]
        if len(numbers) != len(set(numbers)):
            raise ValueError("verified pin numbers must be unique")
        return self


class UserReplacementApproval(_StrictModel):
    """Content-addressed user decision authorizing one exact replacement.

    This is an orchestration receipt, not a field the component-selection model
    may mint.  It binds the decision to the original identity, the complete
    candidate, its evidence set, and the pipeline revision in which it was
    approved.
    """

    schema_version: Literal["ratsnestpro.replacement-approval.v1"] = (
        "ratsnestpro.replacement-approval.v1"
    )
    decision_id: str = Field(min_length=1, max_length=160)
    approved_by: Literal["user"] = "user"
    target_ref: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z#][A-Za-z0-9_]*$",
    )
    requested_identity: str = Field(min_length=1, max_length=200)
    candidate_symbol: str = Field(min_length=3, max_length=200)
    candidate_value: str = Field(min_length=1, max_length=200)
    candidate_footprint: str = Field(min_length=3, max_length=240)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)
    revision: int = Field(ge=0)
    approval_token: str = Field(pattern=r"^[0-9a-f]{64}$")

    def verifies(self, secret: str | bytes) -> bool:
        payload = self.model_dump(mode="json", exclude={"approval_token"})
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        key = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(key) < 32:
            return False
        expected = hmac.new(
            key,
            b"ratsnestpro.replacement-approval.v1\0" + encoded,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(self.approval_token, expected)


def build_user_replacement_approval(
    *,
    decision_id: str,
    target_ref: str,
    requested_identity: str,
    candidate_symbol: str,
    candidate_value: str,
    candidate_footprint: str,
    evidence_ids: Sequence[str],
    revision: int,
    secret: str | bytes,
) -> UserReplacementApproval:
    payload = {
        "schema_version": "ratsnestpro.replacement-approval.v1",
        "decision_id": decision_id,
        "approved_by": "user",
        "target_ref": target_ref.upper(),
        "requested_identity": requested_identity,
        "candidate_symbol": candidate_symbol,
        "candidate_value": candidate_value,
        "candidate_footprint": candidate_footprint,
        "evidence_ids": list(evidence_ids),
        "revision": revision,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    if len(key) < 32:
        raise ValueError("replacement approval secret must be at least 32 bytes")
    token = hmac.new(
        key,
        b"ratsnestpro.replacement-approval.v1\0" + encoded,
        hashlib.sha256,
    ).hexdigest()
    return UserReplacementApproval.model_validate({
        **payload,
        "approval_token": token,
    })


class GroundedReplacement(_StrictModel):
    """A replacement candidate whose identity decision was made upstream."""

    symbol: str = Field(min_length=3, max_length=200)
    value: str = Field(min_length=1, max_length=200)
    footprint: str = Field(min_length=3, max_length=240)
    identity_relation: Literal[
        "exact",
        "kicad_wildcard",
        "generic_primitive",
        "equivalent_validated",
    ]
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    evidence_covers: set[EvidenceCoverage] = Field(default_factory=set)
    user_approval: UserReplacementApproval | None = None


def verified_replacements_by_ref(
    payload: Mapping[str, GroundedReplacement | Mapping[str, Any]] | None,
    *,
    secret: str | bytes | None,
) -> dict[str, GroundedReplacement]:
    """Validate the internal approval receipt before it enters run state.

    The transport is allowed to carry JSON, but JSON is never authority: each
    exact candidate and target reference must be covered by the server-held
    HMAC receipt.  Component resolution validates the same receipt again
    against the selected part identity and current pipeline revision.
    """

    if not payload:
        return {}
    if secret is None or len(payload) > 64:
        raise ValueError("trusted component replacement approvals are unavailable")
    verified: dict[str, GroundedReplacement] = {}
    for raw_ref, raw_replacement in payload.items():
        ref = str(raw_ref).strip().upper()
        replacement = (
            raw_replacement
            if isinstance(raw_replacement, GroundedReplacement)
            else GroundedReplacement.model_validate(raw_replacement)
        )
        approval = replacement.user_approval
        if (
            not ref
            or approval is None
            or approval.target_ref != ref
            or not approval.verifies(secret)
            or approval.candidate_symbol != replacement.symbol
            or approval.candidate_value != replacement.value
            or approval.candidate_footprint != replacement.footprint
            or approval.evidence_ids != replacement.evidence_ids
        ):
            raise ValueError(f"component replacement approval is invalid for {ref or '<empty>'}")
        if ref in verified:
            raise ValueError(f"component replacement approval is duplicated for {ref}")
        verified[ref] = replacement
    return verified


class ResolutionDiagnostic(_StrictModel):
    """Sanitized structured-boundary failure evidence.

    Raw model output is intentionally absent.  This keeps prompts, credentials,
    and private document text out of checkpoints while retaining enough detail
    to distinguish missing evidence from provider/schema failures.
    """

    category: Literal[
        "evidence_gap",
        "schema_failure",
        "provider_failure",
        "harness_failure",
    ]
    exception_type: str = Field(default="", max_length=160)
    validation_errors: list[str] = Field(default_factory=list, max_length=64)
    output_length: int | None = Field(default=None, ge=0)
    truncated: bool | None = None
    provider_request_id: str = Field(default="", max_length=160)


class SymbolOnlyExtractionResult(_StrictModel):
    status: Literal["ok", "evidence_gap", "harness_failure"]
    spec: SymbolOnlyPlaceholderSpec | None = None
    attempts: int = Field(ge=1, le=3)
    diagnostics: list[ResolutionDiagnostic] = Field(
        default_factory=list,
        max_length=3,
    )


class ComponentResolution(_StrictModel):
    ref: str
    status: ResolutionStatus
    requested_identity: str
    symbol: str
    footprint: str
    release_ready: bool
    blocks_execution: bool
    reason_code: str
    detail: str
    placeholder_generated: bool = False
    diagnostic: ResolutionDiagnostic | None = None
    identity_mode: IdentityMode = "capability_only"
    identity_provenance: str = "selection_proposal"


class LibraryClosureResult(_StrictModel):
    resolutions: list[ComponentResolution]

    @property
    def execution_blockers(self) -> list[ComponentResolution]:
        return [item for item in self.resolutions if item.blocks_execution]

    @property
    def release_ready(self) -> bool:
        return bool(self.resolutions) and all(
            item.release_ready for item in self.resolutions
        )


def _identity_candidates(value: str) -> tuple[str, ...]:
    """Return manufacturer-like tokens without treating values such as 100nF."""

    candidates: list[str] = []
    for token in re.split(r"[\s,;()[\]{}]+", value):
        compact = re.sub(r"[^A-Za-z0-9]", "", token)
        if (
            len(compact) >= 4
            and any(char.isalpha() for char in compact)
            and any(char.isdigit() for char in compact)
        ):
            candidates.append(token.strip())
    return tuple(candidates)


_SEARCH_STOPWORDS = {
    "active",
    "component",
    "device",
    "generic",
    "interface",
    "package",
    "protection",
}
_PRIMARY_PROCESSOR_ROLE_TOKENS = {
    "cpld",
    "cpu",
    "fpga",
    "maincontroller",
    "mainprocessor",
    "mcu",
    "microcontroller",
    "processor",
    "soc",
}


def _search_tokens(value: str) -> frozenset[str]:
    tokens: set[str] = set()
    for raw in re.split(r"[^A-Za-z0-9]+", value.casefold()):
        if not raw:
            continue
        tokens.add(raw)
        tokens.update(re.findall(r"[a-z]+|\d+", raw))
    return frozenset(
        token for token in tokens
        if len(token) > 1 and token not in _SEARCH_STOPWORDS
    )


def _identity_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _common_prefix_ratio(first: str, second: str) -> float:
    first_key = _identity_key(first)
    second_key = _identity_key(second)
    if not first_key or not second_key:
        return 0.0
    common = 0
    for first_char, second_char in zip(first_key, second_key, strict=False):
        if first_char != second_char:
            break
        common += 1
    return common / max(len(first_key), len(second_key))


def _is_primary_processor(part: SelectedPart) -> bool:
    library = part.symbol.partition(":")[0].casefold()
    role_key = _identity_key(part.role)
    role_tokens = _search_tokens(part.role)
    return (
        library.startswith(("mcu_", "fpga_", "cpld_"))
        or bool(role_tokens & _PRIMARY_PROCESSOR_ROLE_TOKENS)
        or any(token in role_key for token in _PRIMARY_PROCESSOR_ROLE_TOKENS)
    )


@dataclass(frozen=True)
class _InstalledCandidate:
    lib_id: str
    library: str
    name: str
    library_tokens: frozenset[str]
    name_tokens: frozenset[str]


@dataclass(frozen=True)
class _InstalledCandidateIndex:
    records: tuple[_InstalledCandidate, ...]
    by_library: dict[str, tuple[_InstalledCandidate, ...]]
    by_token: dict[str, tuple[_InstalledCandidate, ...]]


@lru_cache(maxsize=4)
def _candidate_index(installed_ids: tuple[str, ...]) -> _InstalledCandidateIndex:
    records: list[_InstalledCandidate] = []
    by_library: dict[str, list[_InstalledCandidate]] = {}
    by_token: dict[str, list[_InstalledCandidate]] = {}
    for lib_id in installed_ids:
        library, separator, name = lib_id.partition(":")
        if not separator or not library or not name:
            continue
        record = _InstalledCandidate(
            lib_id=lib_id,
            library=library,
            name=name,
            library_tokens=_search_tokens(library),
            name_tokens=_search_tokens(name),
        )
        records.append(record)
        by_library.setdefault(library.casefold(), []).append(record)
        for token in record.library_tokens | record.name_tokens:
            by_token.setdefault(token, []).append(record)
    return _InstalledCandidateIndex(
        records=tuple(records),
        by_library={
            key: tuple(values)
            for key, values in by_library.items()
        },
        by_token={
            key: tuple(values)
            for key, values in by_token.items()
        },
    )


def _natural_pin_key(number: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(piece)) if piece.isdigit() else (1, piece.casefold())
        for piece in re.findall(r"\d+|\D+", number)
    )


def _pin_numbers(pins: Sequence[Mapping[str, Any]] | None) -> set[str]:
    return {
        str(pin.get("number", "")).strip()
        for pin in (pins or ())
        if str(pin.get("number", "")).strip()
    }


def _pad_numbers(pads: Sequence[Mapping[str, Any]] | None) -> set[str]:
    return {
        str(pad.get("number", "")).strip()
        for pad in (pads or ())
        if str(pad.get("number", "")).strip()
    }


def _compatible_pin_pad_sets(
    symbol: str,
    footprint: str,
    pin_numbers: set[str],
    pad_numbers: set[str],
) -> bool:
    if not pin_numbers and not pad_numbers:
        return (
            symbol.partition(":")[0].casefold() == "mechanical"
            and footprint.partition(":")[0].casefold() == "mountinghole"
        )
    connector = symbol.startswith(("Connector:", "Connector_Generic:"))
    return pin_numbers == pad_numbers or (
        connector and bool(pin_numbers) and pin_numbers.issubset(pad_numbers)
    )


def _pin_function_tokens(name: object) -> frozenset[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9]*", str(name).casefold())
        if token not in {"pin", "nc", "noconnect"}
        and not re.fullmatch(r"pin\d+", token)
    }
    aliases = {
        "vss": "ground",
        "vssa": "ground",
        "gnd": "ground",
        "vdd": "supply",
        "vdda": "supply",
        "vcc": "supply",
    }
    return frozenset(aliases.get(token, token) for token in tokens)


def _pin_functions_compatible(
    source: Sequence[Mapping[str, Any]] | None,
    candidate: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Compare pin functions when the originally requested symbol provides them."""

    source_by_number = {
        str(pin.get("number", "")).strip(): pin
        for pin in (source or ())
        if str(pin.get("number", "")).strip()
        and _pin_function_tokens(pin.get("name", ""))
    }
    if not source_by_number:
        return True
    candidate_by_number = {
        str(pin.get("number", "")).strip(): pin
        for pin in (candidate or ())
        if str(pin.get("number", "")).strip()
    }
    for number, source_pin in source_by_number.items():
        candidate_pin = candidate_by_number.get(number)
        if candidate_pin is None:
            return False
        if not (
            _pin_function_tokens(source_pin.get("name", ""))
            & _pin_function_tokens(candidate_pin.get("name", ""))
        ):
            return False
        source_type = str(
            source_pin.get("type")
            or source_pin.get("electrical_type")
            or ""
        ).casefold()
        candidate_type = str(
            candidate_pin.get("type")
            or candidate_pin.get("electrical_type")
            or ""
        ).casefold()
        weak_types = {"", "passive", "unspecified"}
        if (
            source_type not in weak_types
            and candidate_type not in weak_types
            and source_type != candidate_type
        ):
            return False
    return True


def _declared_footprint_compatible(
    requested: str,
    declared: str,
    requested_pad_numbers: set[str],
    footprint_pads: Callable[[str], Sequence[Mapping[str, Any]] | None],
) -> bool:
    """Reject an automatic candidate whose own package metadata contradicts it."""

    requested = requested.strip()
    declared = declared.strip()
    if not declared:
        return True
    if requested.casefold() == declared.casefold():
        return True
    if "*" in declared and fnmatchcase(requested.casefold(), declared.casefold()):
        return True
    requested_library, separator, requested_name = requested.partition(":")
    declared_library, declared_separator, declared_name = declared.partition(":")
    if not separator or not declared_separator:
        return False
    if requested_library.casefold() != declared_library.casefold():
        return False
    if _common_prefix_ratio(requested_name, declared_name) < 0.45:
        return False
    try:
        declared_pads = _pad_numbers(footprint_pads(declared))
    except Exception:  # noqa: BLE001 - candidate metadata is optional evidence
        return False
    return bool(requested_pad_numbers) and declared_pads == requested_pad_numbers


def _placeholder_name(identity: str, definition_sha: str = "") -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity).strip("._-")
    digest = (
        definition_sha
        or hashlib.sha256(identity.casefold().encode()).hexdigest()
    )[:16]
    return f"UNRESOLVED_{(stem or 'device')[:48]}_{digest}"


def _request_id(provider: object) -> str:
    for name in ("last_request_id", "request_id"):
        value = getattr(provider, name, "")
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    return ""


def _looks_truncated(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    return stripped[-1] not in "}]"


def _validation_summary(exc: ValidationError) -> list[str]:
    summary: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        path = ".".join(str(item) for item in error.get("loc", ()))
        kind = str(error.get("type", "validation_error"))
        message = str(error.get("msg", "invalid value"))
        summary.append(f"{path or '<root>'}: {kind}: {message}"[:500])
    return summary[:64]


def _validation_category(exc: ValidationError) -> Literal[
    "evidence_gap",
    "schema_failure",
]:
    errors = exc.errors(include_input=False, include_url=False)
    evidence_fields = {"requested_identity", "footprint", "pins", "evidence_id"}
    if errors and all(
        error.get("type") in {"missing", "too_short"}
        and any(str(item) in evidence_fields for item in error.get("loc", ()))
        for error in errors
    ):
        return "evidence_gap"
    return "schema_failure"


def extract_symbol_only_spec(
    provider: Callable[[], str],
    *,
    max_retries: int = 2,
) -> SymbolOnlyExtractionResult:
    """Validate a symbol-only response with at most two boundary retries."""

    retry_count = min(2, max(0, int(max_retries)))
    diagnostics: list[ResolutionDiagnostic] = []
    for attempt in range(1, retry_count + 2):
        try:
            raw = provider()
        except Exception as exc:  # noqa: BLE001 - provider boundary
            diagnostics.append(ResolutionDiagnostic(
                category="provider_failure",
                exception_type=type(exc).__name__,
                provider_request_id=_request_id(provider),
            ))
            continue
        try:
            spec = SymbolOnlyPlaceholderSpec.model_validate_json(raw)
        except ValidationError as exc:
            diagnostics.append(ResolutionDiagnostic(
                category=_validation_category(exc),
                exception_type=type(exc).__name__,
                validation_errors=_validation_summary(exc),
                output_length=len(raw),
                truncated=_looks_truncated(raw),
                provider_request_id=_request_id(provider),
            ))
            continue
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            diagnostics.append(ResolutionDiagnostic(
                category="schema_failure",
                exception_type=type(exc).__name__,
                output_length=len(raw) if isinstance(raw, str) else None,
                truncated=(
                    _looks_truncated(raw)
                    if isinstance(raw, str)
                    else None
                ),
                provider_request_id=_request_id(provider),
            ))
            continue
        return SymbolOnlyExtractionResult(
            status="ok",
            spec=spec,
            attempts=attempt,
            diagnostics=diagnostics,
        )

    status = (
        "evidence_gap"
        if diagnostics
        and all(item.category == "evidence_gap" for item in diagnostics)
        else "harness_failure"
    )
    return SymbolOnlyExtractionResult(
        status=status,
        attempts=retry_count + 1,
        diagnostics=diagnostics,
    )


def _try_lock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _stable_file_lock(lock_path: Path, *, timeout: float = 10.0):
    """Use a stable OS-locked inode; process death releases ownership."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                _try_lock_file(handle)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"file lock timed out: {lock_path.name}"
                    ) from None
                time.sleep(0.025)
        yield
    finally:
        if acquired:
            _unlock_file(handle)
        handle.close()


@contextmanager
def _placeholder_library_lock(root: Path):
    """Serialize RMW updates without deleting another process's lock."""

    with _stable_file_lock(root / ".ratsnest-placeholder.lock"):
        yield


@contextmanager
def _project_library_table_lock(project_dir: Path):
    """Serialize project-local sym-lib-table read-modify-write updates."""

    with _stable_file_lock(project_dir / ".ratsnest-sym-lib-table.lock"):
        yield


class ComponentResolutionService:
    """Resolve and, when evidence permits, safely close a selected BOM."""

    def __init__(
        self,
        *,
        project_dir: str | os.PathLike[str] | None = None,
        generated_root: str | os.PathLike[str] | None = None,
        resolve_symbol: Callable[[str], object | None] | None = None,
        symbol_pins: Callable[[str], list[dict[str, Any]] | None] | None = None,
        symbol_properties: Callable[[str], dict[str, str]] | None = None,
        footprint_pads: Callable[[str], list[dict[str, Any]] | None] | None = None,
        symbol_index: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self.project_dir = (
            Path(project_dir).resolve(strict=False)
            if project_dir is not None
            else None
        )
        self.generated_root = (
            Path(generated_root).resolve(strict=False)
            if generated_root is not None
            else default_generated_library_root()
        )
        self._resolve_symbol = resolve_symbol or symbols.resolve_symbol
        self._symbol_pins = symbol_pins or symbols.symbol_pins
        self._symbol_properties = symbol_properties or symbols.symbol_properties
        self._footprint_pads = footprint_pads or footprints.footprint_pads
        self._symbol_index = symbol_index or grounding.symbol_index

    @staticmethod
    def _annotate(part: SelectedPart, result: ComponentResolution) -> None:
        part.requested_identity = result.requested_identity
        part.identity_mode = result.identity_mode
        part.identity_provenance = result.identity_provenance
        part.resolution_status = result.status.value
        part.resolution_detail = result.detail[:1_000]
        part.release_ready = result.release_ready
        part.dnp = not result.release_ready
        part.unresolved = not result.release_ready

    @staticmethod
    def _diagnostic(
        category: Literal[
            "evidence_gap",
            "schema_failure",
            "provider_failure",
            "harness_failure",
        ],
        exc: Exception,
    ) -> ResolutionDiagnostic:
        validation_errors = (
            _validation_summary(exc)
            if isinstance(exc, ValidationError)
            else []
        )
        return ResolutionDiagnostic(
            category=category,
            exception_type=type(exc).__name__,
            validation_errors=validation_errors,
        )

    def _identity_relation(
        self,
        part: SelectedPart,
        properties: Mapping[str, str],
    ) -> str | None:
        library, _, symbol_name = part.symbol.partition(":")
        if library in _GENERIC_SYMBOL_LIBRARIES:
            return "generic_primitive"
        requested = _identity_candidates(
            part.requested_identity or part.value
        )
        if (
            library == "Device"
            and _PARAMETERIZED_DEVICE_RE.match(symbol_name)
        ):
            # A reusable primitive proves pin topology, not a user-fixed
            # manufacturer identity.  Exact identities stay fail-closed until
            # an upstream grounded replacement/equivalence contract supplies
            # the separate device evidence.
            if part.identity_mode == "fixed_exact" and requested:
                return None
            return "generic_primitive"
        if not requested:
            # A generic active/discrete shape is usable when the request itself
            # is generic. A concrete manufacturer code must be grounded below.
            if library in {"Device", "Motor", "Simulation_SPICE"}:
                return "generic_primitive"
            return "unspecified"
        available = tuple(
            value
            for value in (properties.get("Value", ""), symbol_name)
            if value
        )
        relations = [
            relation
            for wanted in requested
            for candidate in available
            if (
                relation := grounding.symbol_identity_match_kind(
                    wanted,
                    candidate,
                )
            )
            is not None
        ]
        if "exact" in relations:
            return "exact"
        if "kicad_wildcard" in relations:
            return "kicad_wildcard"
        if "qualified_base" in relations:
            return "qualified_base"
        return None

    @staticmethod
    def _candidate_identity_relation(
        requested_codes: Sequence[str],
        candidate_name: str,
    ) -> str:
        relations = {
            relation
            for requested in requested_codes
            if (
                relation := grounding.symbol_identity_match_kind(
                    requested,
                    candidate_name,
                )
            )
            is not None
        }
        if "exact" in relations:
            return "exact"
        if "kicad_wildcard" in relations:
            return "kicad_wildcard"
        if "qualified_base" in relations:
            return "qualified_base"
        if any(
            grounding.symbol_identity_match_kind(candidate_name, requested)
            == "qualified_base"
            for requested in requested_codes
        ):
            return "same_model_family"
        return "functional_candidate"

    def _candidate_score(
        self,
        part: SelectedPart,
        record: _InstalledCandidate,
        requested_codes: Sequence[str],
        query_tokens: frozenset[str],
    ) -> tuple[float, str]:
        relation = self._candidate_identity_relation(
            requested_codes,
            record.name,
        )
        identity_scores = {
            "exact": 220.0,
            "kicad_wildcard": 210.0,
            "qualified_base": 200.0,
            "same_model_family": 125.0,
            "functional_candidate": 0.0,
        }
        score = identity_scores[relation]
        proposed_library = part.symbol.partition(":")[0].casefold()
        if record.library.casefold() == proposed_library:
            score += 24.0
        role_tokens = _search_tokens(part.role)
        score += 8.0 * len(role_tokens & record.library_tokens)
        score += 4.0 * len(query_tokens & record.name_tokens)
        for requested in requested_codes:
            score = max(
                score,
                identity_scores[relation]
                + 80.0 * _common_prefix_ratio(requested, record.name)
                + 35.0 * difflib.SequenceMatcher(
                    None,
                    _identity_key(requested),
                    _identity_key(record.name),
                ).ratio()
                + (24.0 if record.library.casefold() == proposed_library else 0.0)
                + 8.0 * len(role_tokens & record.library_tokens)
                + 4.0 * len(query_tokens & record.name_tokens),
            )
        return score, relation

    def _bounded_installed_candidate(
        self,
        part: SelectedPart,
        pad_numbers: set[str],
    ) -> tuple[_InstalledCandidate, str] | None:
        """Return one uniquely best real symbol compatible with real pads."""

        installed = tuple(self._symbol_index())
        if not installed:
            return None
        index = _candidate_index(installed)
        proposed_library = part.symbol.partition(":")[0].casefold()
        query_text = (
            f"{part.requested_identity} {part.value} {part.role} "
            f"{part.symbol} {part.footprint}"
        )
        query_tokens = _search_tokens(query_text)
        requested_codes = (
            _identity_candidates(part.requested_identity or part.value)
            or _identity_candidates(part.symbol.partition(":")[2])
            or (part.symbol.partition(":")[2],)
        )

        pool: dict[str, _InstalledCandidate] = {
            record.lib_id: record
            for record in index.by_library.get(proposed_library, ())
        }
        for token in query_tokens:
            for record in index.by_token.get(token, ()):
                pool.setdefault(record.lib_id, record)

        role_tokens = _search_tokens(part.role)
        related_libraries: list[tuple[int, str]] = []
        for library, records in index.by_library.items():
            if not records:
                continue
            overlap = len(
                (role_tokens | _search_tokens(part.symbol.partition(":")[0]))
                & records[0].library_tokens
            )
            if overlap:
                related_libraries.append((overlap, library))
        related_libraries.sort(reverse=True)
        for _overlap, library in related_libraries[:4]:
            for record in index.by_library[library]:
                pool.setdefault(record.lib_id, record)

        ranked = sorted(
            (
                (*self._candidate_score(
                    part,
                    record,
                    requested_codes,
                    query_tokens,
                ), record)
                for record in pool.values()
            ),
            key=lambda item: (item[0], item[2].lib_id),
            reverse=True,
        )
        source_pins = (
            self._symbol_pins(part.symbol)
            if self._resolve_symbol(part.symbol) is not None
            else None
        )
        compatible: list[tuple[float, str, _InstalledCandidate]] = []
        for score, relation, record in ranked[:48]:
            if score < 45.0:
                continue
            if self._resolve_symbol(record.lib_id) is None:
                continue
            candidate_pin_rows = self._symbol_pins(record.lib_id)
            candidate_pins = _pin_numbers(candidate_pin_rows)
            if not _compatible_pin_pad_sets(
                record.lib_id,
                part.footprint,
                candidate_pins,
                pad_numbers,
            ):
                continue
            properties = self._symbol_properties(record.lib_id)
            if not _declared_footprint_compatible(
                part.footprint,
                properties.get("Footprint", ""),
                pad_numbers,
                self._footprint_pads,
            ):
                continue
            if not _pin_functions_compatible(source_pins, candidate_pin_rows):
                continue
            compatible.append((score, relation, record))
        if not compatible:
            return None
        compatible.sort(
            key=lambda item: (item[0], item[2].lib_id),
            reverse=True,
        )
        best_score, best_relation, best = compatible[0]
        if (
            len(compatible) > 1
            and best_score - compatible[1][0] < 6.0
        ):
            return None
        return best, best_relation

    def _recover_installed_candidate(
        self,
        part: SelectedPart,
        pads: Sequence[Mapping[str, Any]],
    ) -> ComponentResolution | None:
        pad_numbers = _pad_numbers(pads)
        if not pad_numbers:
            return None
        selected = self._bounded_installed_candidate(part, pad_numbers)
        if selected is None:
            return None
        candidate, relation = selected
        requested = part.requested_identity or part.value
        proposed = part.model_copy(update={"symbol": candidate.lib_id})
        candidate_result = self._installed_resolution(proposed)
        if candidate_result.blocks_execution:
            return None
        if candidate_result.release_ready and relation in {
            "exact",
            "kicad_wildcard",
            "qualified_base",
        }:
            return candidate_result.model_copy(update={
                "detail": (
                    f"{part.ref} deterministically resolved missing/incompatible "
                    f"symbol to installed {candidate.lib_id!r}"
                ),
            })
        return ComponentResolution(
            ref=part.ref,
            status=ResolutionStatus.REPLACEABLE_GROUNDED,
            requested_identity=requested,
            symbol=candidate.lib_id,
            footprint=part.footprint,
            release_ready=False,
            blocks_execution=True,
            reason_code="identity_unverified_candidate_suggestion",
            detail=(
                f"{part.ref} has uniquely matched installed candidate "
                f"{candidate.lib_id!r}, but it was not applied; requested identity "
                f"{requested!r}, package defaults, and electrical equivalence "
                "are not jointly verified"
            ),
        )

    def _installed_resolution(
        self,
        part: SelectedPart,
    ) -> ComponentResolution:
        requested = part.requested_identity or part.value
        try:
            if self._resolve_symbol(part.symbol) is None:
                return ComponentResolution(
                    ref=part.ref,
                    status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                    requested_identity=requested,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    blocks_execution=True,
                    reason_code="symbol_not_installed",
                    detail=(
                        f"{part.ref} symbol {part.symbol!r} is not installed; "
                        "no unverified active-device lib_id may enter connectivity"
                    ),
                )
            if not part.footprint:
                return ComponentResolution(
                    ref=part.ref,
                    status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                    requested_identity=requested,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    blocks_execution=True,
                    reason_code="footprint_missing",
                    detail=f"{part.ref} has no physical footprint",
                )
            pads = self._footprint_pads(part.footprint)
            if pads is None:
                return ComponentResolution(
                    ref=part.ref,
                    status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                    requested_identity=requested,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    blocks_execution=True,
                    reason_code="footprint_not_installed",
                    detail=(
                        f"{part.ref} footprint {part.footprint!r} is not installed"
                    ),
                )
            pins = self._symbol_pins(part.symbol)
            pin_numbers = _pin_numbers(pins)
            pad_numbers = _pad_numbers(pads)
            if not _compatible_pin_pad_sets(
                part.symbol,
                part.footprint,
                pin_numbers,
                pad_numbers,
            ):
                return ComponentResolution(
                    ref=part.ref,
                    status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                    requested_identity=requested,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    blocks_execution=True,
                    reason_code="pin_pad_incompatible",
                    detail=(
                        f"{part.ref} symbol pins {sorted(pin_numbers)} do not "
                        f"match footprint pads {sorted(pad_numbers)}"
                    ),
                )
            properties = self._symbol_properties(part.symbol)
            relation = self._identity_relation(part, properties)
            if relation is None:
                return ComponentResolution(
                    ref=part.ref,
                    status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                    requested_identity=requested,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    # A real installed symbol/footprint remains mechanically
                    # usable for an artifact-first draft, but the identity
                    # mismatch is a hard release error.
                    blocks_execution=False,
                    reason_code="device_identity_mismatch",
                    detail=(
                        f"{part.ref} requested identity {requested!r} is not "
                        f"the installed device {properties.get('Value') or part.symbol!r}"
                    ),
                )
            if part.symbol.startswith(f"{_PLACEHOLDER_LIBRARY}:"):
                unverified = (
                    properties.get("RatsNestPinEvidence")
                    == "unverified_pad_numbers_only"
                )
                return ComponentResolution(
                    ref=part.ref,
                    status=(
                        ResolutionStatus.PLACEHOLDER_UNVERIFIED_NONRELEASE
                        if unverified
                        else ResolutionStatus.PLACEHOLDER_VERIFIED_NONRELEASE
                    ),
                    requested_identity=requested,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    blocks_execution=False,
                    reason_code=(
                        "unverified_pin_function_placeholder"
                        if unverified
                        else "verified_placeholder"
                    ),
                    detail=(
                        f"{part.ref} uses a real DNP/UNRESOLVED project-local "
                        "placeholder with numeric pins copied from footprint "
                        "pads; pin functions remain unverified"
                        if unverified
                        else
                        f"{part.ref} uses a real DNP/UNRESOLVED project-local "
                        "placeholder backed by verified pins"
                    ),
                )
            status = (
                ResolutionStatus.INSTALLED_QUALIFIED_VALIDATED
                if relation == "qualified_base"
                else ResolutionStatus.INSTALLED_EXACT
            )
            return ComponentResolution(
                ref=part.ref,
                status=status,
                requested_identity=requested,
                symbol=part.symbol,
                footprint=part.footprint,
                release_ready=True,
                blocks_execution=False,
                reason_code=relation,
                detail=(
                    f"{part.ref} resolved to installed symbol and footprint "
                    "with compatible electrical pins/pads"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - resolver boundary
            return ComponentResolution(
                ref=part.ref,
                status=ResolutionStatus.HARNESS_FAILURE,
                requested_identity=requested,
                symbol=part.symbol,
                footprint=part.footprint,
                release_ready=False,
                blocks_execution=True,
                reason_code="resolver_exception",
                detail=(
                    f"component resolver failed for {part.ref}: "
                    f"{type(exc).__name__}"
                ),
                diagnostic=self._diagnostic("harness_failure", exc),
            )

    def _write_placeholder(
        self,
        part: SelectedPart,
        *,
        footprint: str,
        generated_pins: list[dict[str, Any]],
        evidence_kind: Literal["verified", "unverified_pad_numbers_only"],
        evidence_ids: Sequence[str] = (),
    ) -> ComponentResolution:
        requested = part.requested_identity or part.value
        root = self.generated_root
        canonical_pins = sorted(
            (
                {
                    "number": str(pin.get("number", "")).strip(),
                    "name": str(pin.get("name", "~")).strip() or "~",
                    "type": str(pin.get("type", "passive")).strip() or "passive",
                }
                for pin in generated_pins
            ),
            key=lambda pin: _natural_pin_key(pin["number"]),
        )
        midpoint = (len(canonical_pins) - 1) / 2
        output_pins = [
            {
                **pin,
                "x": -5.08 if index % 2 == 0 else 5.08,
                "y": (midpoint - index) * 1.27,
                "angle": 0 if index % 2 == 0 else 180,
                "length": 2.54,
            }
            for index, pin in enumerate(canonical_pins)
        ]
        unique_evidence_ids = sorted({
            evidence_id.strip()
            for evidence_id in evidence_ids
            if evidence_id.strip()
        })
        evidence_digest = hashlib.sha256(
            json.dumps(
                unique_evidence_ids,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        definition_payload = {
            "requested_identity": requested,
            "footprint": footprint,
            "reference": "U",
            "pins": output_pins,
            "evidence_kind": evidence_kind,
            "evidence_digest": evidence_digest,
            "release_properties": {
                "DNP": "yes",
                "RatsNestStatus": "UNRESOLVED",
                "RatsNestReleaseReady": "no",
            },
        }
        definition_sha = hashlib.sha256(
            json.dumps(
                definition_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        name = _placeholder_name(requested, definition_sha)
        lib_id = f"{_PLACEHOLDER_LIBRARY}:{name}"
        library_path = root / f"{_PLACEHOLDER_LIBRARY}.kicad_sym"
        unverified = evidence_kind == "unverified_pad_numbers_only"
        try:
            root.mkdir(parents=True, exist_ok=True)
            configured_or_registered = {
                path.resolve(strict=False)
                for path in generated_library_roots(existing_only=False)
            }
            if root.resolve(strict=False) not in configured_or_registered:
                register_generated_library_root(root)
            with _placeholder_library_lock(root):
                pin_count = len(output_pins)
                grounding.invalidate_library_indexes()
                existing_path = symbols.resolve_symbol(lib_id)
                if existing_path is not None:
                    existing_sha = symbols.symbol_properties(lib_id).get(
                        "RatsNestDefinitionSha256",
                        "",
                    )
                    if existing_sha != definition_sha:
                        return ComponentResolution(
                            ref=part.ref,
                            status=ResolutionStatus.HARNESS_FAILURE,
                            requested_identity=requested,
                            symbol=part.symbol,
                            footprint=footprint,
                            release_ready=False,
                            blocks_execution=True,
                            reason_code="placeholder_definition_conflict",
                            detail=(
                                f"{lib_id} already exists with a different "
                                "content-addressed definition; existing library "
                                "content was preserved"
                            ),
                        )
                else:
                    create_symbol(
                        str(library_path),
                        name,
                        output_pins,
                        properties={
                            "Reference": "U",
                            "Value": requested,
                            "Footprint": footprint,
                            "Description": (
                                "RatsNestPro DNP/UNRESOLVED placeholder; pin "
                                "functions are unverified and the part is not "
                                "manufacturing-release eligible"
                                if unverified
                                else
                                "RatsNestPro DNP/UNRESOLVED placeholder backed "
                                "by verified pin evidence; not manufacturing-"
                                "release eligible"
                            ),
                            "DNP": "yes",
                            "RatsNestStatus": "UNRESOLVED",
                            "RatsNestReleaseReady": "no",
                            "RatsNestPinEvidence": evidence_kind,
                            "RatsNestEvidenceIds": ";".join(
                                unique_evidence_ids
                            )[:1_000],
                            "RatsNestEvidenceSha256": evidence_digest,
                            "RatsNestDefinitionSha256": definition_sha,
                        },
                        body_width=7.62,
                        body_height=max(5.08, pin_count * 1.27),
                    )
            if self.project_dir is not None:
                self.project_dir.mkdir(parents=True, exist_ok=True)
                with _project_library_table_lock(self.project_dir):
                    register_library(
                        "sym",
                        _PLACEHOLDER_LIBRARY,
                        str(library_path),
                        project_dir=str(self.project_dir),
                    )
            grounding.invalidate_library_indexes()
            part.symbol = lib_id
            part.footprint = footprint
            part.value = requested
            resolved = self._installed_resolution(part)
            if resolved.blocks_execution:
                return resolved
            status = (
                ResolutionStatus.PLACEHOLDER_UNVERIFIED_NONRELEASE
                if unverified
                else ResolutionStatus.PLACEHOLDER_VERIFIED_NONRELEASE
            )
            return resolved.model_copy(update={
                "status": status,
                "release_ready": False,
                "blocks_execution": False,
                "reason_code": (
                    "unverified_pin_function_placeholder"
                    if unverified
                    else "verified_placeholder_generated"
                ),
                "detail": (
                    f"{part.ref} uses a DNP/UNRESOLVED placeholder whose "
                    "numeric pins come only from real footprint pad numbers; "
                    "PIN1... names and electrical functions are unverified"
                    if unverified
                    else
                    f"{part.ref} uses a DNP/UNRESOLVED placeholder backed by "
                    "structured verified pin evidence"
                ),
                "placeholder_generated": True,
            })
        except Exception as exc:  # noqa: BLE001 - filesystem/generator boundary
            return ComponentResolution(
                ref=part.ref,
                status=ResolutionStatus.HARNESS_FAILURE,
                requested_identity=requested,
                symbol=part.symbol,
                footprint=footprint,
                release_ready=False,
                blocks_execution=True,
                reason_code="placeholder_generation_failed",
                detail=(
                    f"placeholder generation failed for {part.ref}: "
                    f"{type(exc).__name__}"
                ),
                diagnostic=self._diagnostic("harness_failure", exc),
            )

    def _generate_placeholder(
        self,
        part: SelectedPart,
        raw_spec: SymbolOnlyPlaceholderSpec | Mapping[str, Any],
    ) -> ComponentResolution:
        requested = part.requested_identity or part.value
        try:
            spec = (
                raw_spec
                if isinstance(raw_spec, SymbolOnlyPlaceholderSpec)
                else SymbolOnlyPlaceholderSpec.model_validate(raw_spec)
            )
        except ValidationError as exc:
            return ComponentResolution(
                ref=part.ref,
                status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                requested_identity=requested,
                symbol=part.symbol,
                footprint=part.footprint,
                release_ready=False,
                blocks_execution=True,
                reason_code="verified_pin_evidence_invalid",
                detail=(
                    f"{part.ref} symbol-only evidence is incomplete or invalid"
                ),
                diagnostic=ResolutionDiagnostic(
                    category=_validation_category(exc),
                    exception_type=type(exc).__name__,
                    validation_errors=_validation_summary(exc),
                ),
            )
        if (
            re.sub(r"[^a-z0-9]", "", spec.requested_identity.casefold())
            != re.sub(r"[^a-z0-9]", "", requested.casefold())
        ):
            return ComponentResolution(
                ref=part.ref,
                status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                requested_identity=requested,
                symbol=part.symbol,
                footprint=part.footprint,
                release_ready=False,
                blocks_execution=True,
                reason_code="placeholder_identity_mismatch",
                detail=(
                    f"{part.ref} verified pin evidence belongs to "
                    f"{spec.requested_identity!r}, not {requested!r}"
                ),
            )
        try:
            pads = self._footprint_pads(spec.footprint)
        except Exception as exc:  # noqa: BLE001 - resolver boundary
            return ComponentResolution(
                ref=part.ref,
                status=ResolutionStatus.HARNESS_FAILURE,
                requested_identity=requested,
                symbol=part.symbol,
                footprint=spec.footprint,
                release_ready=False,
                blocks_execution=True,
                reason_code="footprint_resolver_exception",
                detail=(
                    f"footprint resolver failed for {part.ref}: "
                    f"{type(exc).__name__}"
                ),
                diagnostic=self._diagnostic("harness_failure", exc),
            )
        if pads is None:
            return ComponentResolution(
                ref=part.ref,
                status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                requested_identity=requested,
                symbol=part.symbol,
                footprint=spec.footprint,
                release_ready=False,
                blocks_execution=True,
                reason_code="symbol_only_footprint_not_installed",
                detail=(
                    f"{part.ref} symbol-only generation requires the real "
                    f"installed footprint {spec.footprint!r}"
                ),
            )
        evidence_numbers = {pin.number for pin in spec.pins}
        pad_numbers = _pad_numbers(pads)
        if evidence_numbers != pad_numbers:
            return ComponentResolution(
                ref=part.ref,
                status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                requested_identity=requested,
                symbol=part.symbol,
                footprint=spec.footprint,
                release_ready=False,
                blocks_execution=True,
                reason_code="symbol_only_pin_pad_incompatible",
                detail=(
                    f"{part.ref} verified pins {sorted(evidence_numbers)} do not "
                    f"match installed footprint pads {sorted(pad_numbers)}"
                ),
            )

        midpoint = (len(spec.pins) - 1) / 2
        return self._write_placeholder(
            part,
            footprint=spec.footprint,
            generated_pins=[
                {
                    "number": pin.number,
                    "name": pin.name,
                    "type": pin.electrical_type,
                    "x": -5.08 if index % 2 == 0 else 5.08,
                    "y": (midpoint - index) * 1.27,
                    "angle": 0 if index % 2 == 0 else 180,
                    "length": 2.54,
                }
                for index, pin in enumerate(spec.pins)
            ],
            evidence_kind="verified",
            evidence_ids=[pin.evidence_id for pin in spec.pins],
        )

    def _generate_unverified_placeholder(
        self,
        part: SelectedPart,
        pads: Sequence[Mapping[str, Any]],
    ) -> ComponentResolution:
        numbers = sorted(_pad_numbers(pads), key=_natural_pin_key)
        if not numbers:
            return ComponentResolution(
                ref=part.ref,
                status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
                requested_identity=part.requested_identity or part.value,
                symbol=part.symbol,
                footprint=part.footprint,
                release_ready=False,
                blocks_execution=True,
                reason_code="footprint_has_no_numbered_pads",
                detail=(
                    f"{part.ref} footprint {part.footprint!r} has no numbered "
                    "electrical pads from which an editable placeholder can be made"
                ),
            )
        midpoint = (len(numbers) - 1) / 2
        return self._write_placeholder(
            part,
            footprint=part.footprint,
            generated_pins=[
                {
                    "number": number,
                    "name": f"PIN{number}",
                    "type": "unspecified",
                    "x": -5.08 if index % 2 == 0 else 5.08,
                    "y": (midpoint - index) * 1.27,
                    "angle": 0 if index % 2 == 0 else 180,
                    "length": 2.54,
                }
                for index, number in enumerate(numbers)
            ],
            evidence_kind="unverified_pad_numbers_only",
        )

    @staticmethod
    def _replacement_allowed(
        replacement: GroundedReplacement,
        *,
        target_ref: str,
        requested_identity: str,
        fixed_identity: bool,
        allow_equivalent: bool,
        revision: int,
        approval_secret: str | bytes | None,
    ) -> bool:
        approval = replacement.user_approval
        if approval is None or approval_secret is None or not approval.verifies(
            approval_secret
        ) or (
            approval.target_ref.casefold() != target_ref.casefold()
            or approval.requested_identity.casefold() != requested_identity.casefold()
            or approval.candidate_symbol != replacement.symbol
            or approval.candidate_value != replacement.value
            or approval.candidate_footprint != replacement.footprint
            or approval.evidence_ids != replacement.evidence_ids
            or approval.revision != revision
        ):
            return False
        if fixed_identity:
            actual_relation = grounding.symbol_identity_match_kind(
                requested_identity,
                replacement.value,
            )
            exact_match = (
                replacement.identity_relation in {"exact", "kicad_wildcard"}
                and actual_relation in {"exact", "kicad_wildcard"}
            )
            if exact_match:
                return True
            # A target-bound, revision-bound user receipt is the explicit
            # amendment required to replace a previously fixed identity. It
            # still cannot waive the complete equivalence evidence contract.
            return (
                replacement.identity_relation == "equivalent_validated"
                and bool(replacement.evidence_ids)
                and _REQUIRED_EQUIVALENCE_EVIDENCE.issubset(
                    replacement.evidence_covers
                )
            )
        if replacement.identity_relation in {
            "exact",
            "kicad_wildcard",
            "generic_primitive",
        }:
            return True
        return (
            allow_equivalent
            and replacement.identity_relation == "equivalent_validated"
            and bool(replacement.evidence_ids)
            and _REQUIRED_EQUIVALENCE_EVIDENCE.issubset(
                replacement.evidence_covers
            )
        )

    def resolve(
        self,
        part: SelectedPart,
        *,
        trusted_requested_identity: str | None = None,
        trusted_identity_mode: IdentityMode | None = None,
        trusted_identity_provenance: str | None = None,
        pin_evidence: SymbolOnlyPlaceholderSpec | Mapping[str, Any] | None = None,
        replacement: GroundedReplacement | Mapping[str, Any] | None = None,
        fixed_identity: bool = False,
        allow_equivalent: bool = False,
        approval_revision: int = 0,
        replacement_approval_secret: str | bytes | None = None,
        allow_unverified_placeholder: bool = False,
        mutate: bool = True,
    ) -> ComponentResolution:
        """Resolve one part without allowing lexical identity substitution."""

        if not mutate:
            part = part.model_copy(deep=True)
        # Closure metadata is an output of this deterministic boundary, never
        # an LLM input. Callers preserving a previously closed identity must
        # pass it explicitly.
        identity_mode: IdentityMode = (
            "fixed_exact"
            if fixed_identity
            else trusted_identity_mode or "capability_only"
        )
        identity_provenance = (
            trusted_identity_provenance
            if trusted_identity_mode and trusted_identity_provenance
            else (
                "authoritative_constraint"
                if fixed_identity
                else "selection_proposal"
            )
        )
        part.requested_identity = trusted_requested_identity or part.value
        part.identity_mode = identity_mode
        part.identity_provenance = identity_provenance
        part.resolution_status = ""
        part.resolution_detail = ""
        part.release_ready = False
        part.dnp = False
        part.unresolved = False
        identity_is_hard = (
            fixed_identity
            or identity_mode == "fixed_exact"
            or _is_primary_processor(part)
        )

        def finish(resolution: ComponentResolution) -> ComponentResolution:
            finalized = resolution.model_copy(update={
                "identity_mode": identity_mode,
                "identity_provenance": identity_provenance,
            })
            # ``device_identity_mismatch`` is emitted only after the installed
            # symbol and footprint pin/pad closure has succeeded.  Reaching it
            # for a reusable generic symbol under capability-only identity is
            # therefore a deterministic resolver contradiction, not a board
            # design or evidence problem.  Preserve that provenance so AHE can
            # observe it without trusting an LLM to classify itself.
            if (
                identity_mode == "capability_only"
                and _is_reusable_generic_symbol(part.symbol)
                and finalized.reason_code == "device_identity_mismatch"
            ):
                finalized = finalized.model_copy(update={
                    "status": ResolutionStatus.HARNESS_FAILURE,
                    "release_ready": False,
                    "blocks_execution": True,
                    "reason_code": "generic_capability_closure_contradiction",
                    "detail": (
                        f"component resolver invariant failed for {part.ref}: "
                        "a reusable generic symbol with verified pin/pad closure "
                        "was rejected as a capability-only identity"
                    ),
                    "diagnostic": ResolutionDiagnostic(
                        category="harness_failure",
                        exception_type="ResolutionInvariantError",
                    ),
                })
            artifact_first_placeholder = (
                allow_unverified_placeholder
                and finalized.status in {
                    ResolutionStatus.PLACEHOLDER_VERIFIED_NONRELEASE,
                    ResolutionStatus.PLACEHOLDER_UNVERIFIED_NONRELEASE,
                }
            )
            if (
                identity_is_hard
                and not finalized.release_ready
                and not artifact_first_placeholder
            ):
                finalized = finalized.model_copy(update={
                    "blocks_execution": True,
                    "detail": (
                        f"{finalized.detail}; {part.ref} is a primary processor "
                        "or explicit hard-identity component and cannot use an "
                        "unverified replacement/placeholder"
                    ),
                })
            if mutate:
                self._annotate(part, finalized)
            return finalized

        result = self._installed_resolution(part)
        if (
            identity_mode == "capability_only"
            and _is_reusable_generic_symbol(part.symbol)
            and result.reason_code == "device_identity_mismatch"
        ):
            # Do not let candidate recovery hide a contradiction at the
            # deterministic installed-library boundary.
            return finish(result)
        if result.status in {
            ResolutionStatus.INSTALLED_EXACT,
            ResolutionStatus.INSTALLED_QUALIFIED_VALIDATED,
            ResolutionStatus.PLACEHOLDER_VERIFIED_NONRELEASE,
            ResolutionStatus.PLACEHOLDER_UNVERIFIED_NONRELEASE,
        }:
            return finish(result)

        pads: list[dict[str, Any]] | None = None
        candidate_suggestion: ComponentResolution | None = None
        if part.footprint:
            try:
                pads = self._footprint_pads(part.footprint)
            except Exception as exc:  # noqa: BLE001 - resolver boundary
                result = ComponentResolution(
                    ref=part.ref,
                    status=ResolutionStatus.HARNESS_FAILURE,
                    requested_identity=part.requested_identity,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    blocks_execution=True,
                    reason_code="footprint_resolver_exception",
                    detail=(
                        f"footprint resolver failed for {part.ref}: "
                        f"{type(exc).__name__}"
                    ),
                    diagnostic=self._diagnostic("harness_failure", exc),
                )

        if (
            pads is not None
            and _pad_numbers(pads)
            and result.reason_code in {
                "symbol_not_installed",
                "pin_pad_incompatible",
                "device_identity_mismatch",
            }
        ):
            original_symbol = part.symbol
            try:
                recovered = self._recover_installed_candidate(part, pads)
            except Exception as exc:  # noqa: BLE001 - library-index boundary
                recovered = None
                result = ComponentResolution(
                    ref=part.ref,
                    status=ResolutionStatus.HARNESS_FAILURE,
                    requested_identity=part.requested_identity,
                    symbol=original_symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    blocks_execution=True,
                    reason_code="candidate_search_failed",
                    detail=(
                        f"bounded installed-symbol search failed for {part.ref}: "
                        f"{type(exc).__name__}"
                    ),
                    diagnostic=self._diagnostic("harness_failure", exc),
                )
            if recovered is not None and recovered.release_ready:
                part.symbol = recovered.symbol
                return finish(recovered)
            if recovered is not None:
                candidate_suggestion = recovered
            part.symbol = original_symbol

        if (
            pin_evidence is not None
            and (not identity_is_hard or allow_unverified_placeholder)
            and result.reason_code in {
                "symbol_not_installed",
                "pin_pad_incompatible",
                "device_identity_mismatch",
            }
        ):
            result = self._generate_placeholder(part, pin_evidence)
            return finish(result)

        if replacement is not None:
            try:
                candidate = (
                    replacement
                    if isinstance(replacement, GroundedReplacement)
                    else GroundedReplacement.model_validate(replacement)
                )
            except ValidationError as exc:
                result = ComponentResolution(
                    ref=part.ref,
                    status=ResolutionStatus.HARNESS_FAILURE,
                    requested_identity=part.requested_identity,
                    symbol=part.symbol,
                    footprint=part.footprint,
                    release_ready=False,
                    blocks_execution=True,
                    reason_code="replacement_contract_invalid",
                    detail=f"replacement contract invalid for {part.ref}",
                    diagnostic=ResolutionDiagnostic(
                        category="schema_failure",
                        exception_type=type(exc).__name__,
                        validation_errors=_validation_summary(exc),
                    ),
                )
            else:
                if self._replacement_allowed(
                    candidate,
                    target_ref=part.ref,
                    requested_identity=part.requested_identity,
                    fixed_identity=identity_is_hard,
                    allow_equivalent=allow_equivalent,
                    revision=approval_revision,
                    approval_secret=replacement_approval_secret,
                ):
                    proposed = part.model_copy(update={
                        "symbol": candidate.symbol,
                        "value": candidate.value,
                        "footprint": candidate.footprint,
                        # Validate the installed replacement against its own
                        # grounded identity. The original request remains on
                        # ``part.requested_identity`` and in the result.
                        "requested_identity": candidate.value,
                    })
                    candidate_result = self._installed_resolution(proposed)
                    if candidate_result.release_ready:
                        if mutate:
                            part.symbol = proposed.symbol
                            part.value = proposed.value
                            part.footprint = proposed.footprint
                        result = candidate_result.model_copy(update={
                            "status": ResolutionStatus.REPLACEABLE_GROUNDED,
                            "requested_identity": part.requested_identity,
                            "reason_code": candidate.identity_relation,
                            "detail": (
                                f"{part.ref} uses an explicitly permitted, "
                                "grounded replacement"
                            ),
                        })
                elif identity_is_hard:
                    result = result.model_copy(update={
                        "reason_code": "fixed_identity_replacement_rejected",
                        "detail": (
                            f"{part.ref} fixed identity "
                            f"{part.requested_identity!r} cannot be replaced by "
                            f"{candidate.value!r}"
                        ),
                    })

        if (
            allow_unverified_placeholder
            and pads is not None
            and _pad_numbers(pads)
            and not result.release_ready
            and result.status != ResolutionStatus.HARNESS_FAILURE
        ):
            result = self._generate_unverified_placeholder(part, pads)
        elif (
            candidate_suggestion is not None
            and not identity_is_hard
            and not result.release_ready
            and result.reason_code in {
                "symbol_not_installed",
                "pin_pad_incompatible",
                "device_identity_mismatch",
            }
        ):
            result = candidate_suggestion

        return finish(result)

    def close(
        self,
        parts: Sequence[SelectedPart],
        *,
        pin_evidence_by_ref: Mapping[
            str,
            SymbolOnlyPlaceholderSpec | Mapping[str, Any],
        ]
        | None = None,
        replacements_by_ref: Mapping[
            str,
            GroundedReplacement | Mapping[str, Any],
        ]
        | None = None,
        fixed_identity_refs: set[str] | None = None,
        allow_equivalent_refs: set[str] | None = None,
        approval_revision: int = 0,
        replacement_approval_secret: str | bytes | None = None,
        trusted_requested_identities: Mapping[str, str] | None = None,
        trusted_identity_modes: Mapping[str, IdentityMode] | None = None,
        trusted_identity_provenance: Mapping[str, str] | None = None,
        allow_unverified_placeholders: bool = False,
        mutate: bool = True,
    ) -> LibraryClosureResult:
        evidence = {
            key.upper(): value
            for key, value in (pin_evidence_by_ref or {}).items()
        }
        replacements = {
            key.upper(): value
            for key, value in (replacements_by_ref or {}).items()
        }
        fixed = {ref.upper() for ref in (fixed_identity_refs or set())}
        equivalent = {
            ref.upper() for ref in (allow_equivalent_refs or set())
        }
        trusted_identities = {
            ref.upper(): identity
            for ref, identity in (trusted_requested_identities or {}).items()
            if identity
        }
        trusted_modes = {
            ref.upper(): mode
            for ref, mode in (trusted_identity_modes or {}).items()
            if mode in {"fixed_exact", "family_variant", "capability_only"}
        }
        trusted_provenance = {
            ref.upper(): provenance
            for ref, provenance in (
                trusted_identity_provenance or {}
            ).items()
            if provenance
        }
        return LibraryClosureResult(resolutions=[
            self.resolve(
                part,
                trusted_requested_identity=trusted_identities.get(
                    part.ref.upper()
                ),
                trusted_identity_mode=trusted_modes.get(part.ref.upper()),
                trusted_identity_provenance=trusted_provenance.get(
                    part.ref.upper()
                ),
                pin_evidence=evidence.get(part.ref.upper()),
                replacement=replacements.get(part.ref.upper()),
                fixed_identity=part.ref.upper() in fixed,
                allow_equivalent=part.ref.upper() in equivalent,
                approval_revision=approval_revision,
                replacement_approval_secret=replacement_approval_secret,
                allow_unverified_placeholder=allow_unverified_placeholders,
                mutate=mutate,
            )
            for part in parts
        ])


__all__ = [
    "ComponentResolution",
    "ComponentResolutionService",
    "GroundedReplacement",
    "UserReplacementApproval",
    "build_user_replacement_approval",
    "verified_replacements_by_ref",
    "IdentityMode",
    "LibraryClosureResult",
    "ResolutionDiagnostic",
    "ResolutionStatus",
    "SymbolOnlyExtractionResult",
    "SymbolOnlyPlaceholderSpec",
    "VerifiedPinEvidence",
    "extract_symbol_only_spec",
]

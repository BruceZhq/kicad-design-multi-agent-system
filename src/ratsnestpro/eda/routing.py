"""Signal autorouting via Freerouting, driven through KiCad's own python.

The pipeline stays deterministic and pinned: the LLM decides the routing rules
(layers, net classes, widths — see route_plan/route_planes); this module only
*executes the geometry*. It assigns nets to pads from the pinmap, exports a
Specctra DSN, runs Freerouting, and imports the SES back as real tracks.
Whether an unavailable router blocks the pipeline is decided by the pipeline
context; this module always reports the real execution outcome.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ratsnestpro.orchestration.entity_repairs import (
    CadActionBatch,
    CadActionObservation,
    CadActionResult,
)

# net name -> list of (ref, pad_number)
NetMap = dict[str, list[list[str]]]

_WORKER = Path(__file__).with_name("_route_worker.py")
_CAD_ACTION_WORKER = Path(__file__).with_name("_cad_action_worker.py")


def _router_timeout(layer_count: int) -> int:
    """Return a bounded routing budget, allowing denser multilayer boards more time."""
    default = 3600 if layer_count >= 4 else 1800
    raw = os.environ.get("RATSNESTPRO_ROUTER_TIMEOUT_SECONDS", "")
    try:
        requested = int(raw) if raw else default
    except ValueError:
        requested = default
    return max(300, min(requested, 7200))


def pass_budget(netmap: NetMap, layer_count: int) -> int:
    """Choose a bounded Freerouting pass count from routing complexity.

    A fixed pass count routinely stops one or two connections short on dense
    boards. Count the minimum connection edges implied by the pin map and
    grant multilayer boards a little more rip-up/retry room. This is generic
    execution policy: it neither relaxes design rules nor special-cases a
    component or project.
    """
    connection_edges = sum(max(0, len(pins) - 1) for pins in netmap.values())
    layer_margin = 15 if layer_count >= 4 else 0
    return min(
        100,
        max(20, 10 + math.ceil(connection_edges / 4) + layer_margin),
    )


def _as_int(value: object, default: int) -> int:
    """Coerce a JSON-decoded value (typed ``object``) to int, else ``default``."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def artifact_fingerprint(path: str | os.PathLike[str]) -> str:
    """Return the full SHA-256 used to bind a mutation to one PCB revision."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cad_action_batch_fingerprint(batch: CadActionBatch) -> str:
    """Return a canonical digest for idempotency-key collision detection."""

    payload = json.dumps(
        batch.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cad_observation(
    batch: CadActionBatch,
    *,
    status: str,
    pcb: Path,
    batch_fingerprint: str,
    before_fingerprint: str = "",
    after_fingerprint: str = "",
    action_results: list[CadActionResult] | None = None,
    detail: str = "",
) -> CadActionObservation:
    return CadActionObservation.model_validate({
        "batch_id": batch.batch_id,
        "idempotency_key": batch.idempotency_key,
        "status": status,
        "artifact_path": str(pcb),
        "batch_fingerprint": batch_fingerprint,
        "before_fingerprint": before_fingerprint,
        "after_fingerprint": after_fingerprint,
        "action_results": action_results or [],
        "pending_success_checks": batch.success_checks,
        "detail": detail,
    })


def _run_scoped_pcb(
    pcb_path: str | os.PathLike[str],
    run_dir: str | os.PathLike[str] | None,
) -> tuple[Path, Path]:
    pcb = Path(pcb_path).resolve()
    root = Path(run_dir).resolve() if run_dir is not None else pcb.parent
    try:
        pcb.relative_to(root)
    except ValueError as exc:
        raise ValueError("PCB artifact must be inside the run workspace") from exc
    if pcb.suffix.lower() != ".kicad_pcb":
        raise ValueError("CAD actions accept only a .kicad_pcb artifact")
    if not pcb.is_file() or pcb.stat().st_size <= 0:
        raise ValueError("CAD action PCB artifact is missing or empty")
    return pcb, root


def _receipt_path(root: Path, idempotency_key: str) -> Path:
    safe_name = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return root / ".ratsnestpro" / "cad-actions" / f"{safe_name}.json"


def _read_receipt(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_receipt(
    path: Path,
    batch_fingerprint: str,
    observation: CadActionObservation,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f".{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(
            {
                "batch_fingerprint": batch_fingerprint,
                "observation": observation.model_dump(mode="json"),
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def apply_cad_action_batch(
    pcb_path: str | os.PathLike[str],
    batch: CadActionBatch,
    *,
    run_dir: str | os.PathLike[str] | None = None,
    timeout_seconds: int = 120,
) -> CadActionObservation:
    """Atomically execute a fingerprint-bound batch through KiCad ``pcbnew``.

    The worker writes a separate candidate.  This function replaces the real
    run artifact only after the worker reports success and the saved candidate
    can be fingerprinted.  ERC/DRC and release gates remain downstream checks;
    they are reported as ``pending_success_checks`` in the observation.
    """

    if not isinstance(batch, CadActionBatch):
        batch = CadActionBatch.model_validate(batch)
    try:
        pcb, root = _run_scoped_pcb(pcb_path, run_dir)
    except (OSError, ValueError) as exc:
        unresolved = Path(pcb_path).resolve()
        return _cad_observation(
            batch,
            status="rejected",
            pcb=unresolved,
            batch_fingerprint=cad_action_batch_fingerprint(batch),
            detail=str(exc),
        )

    batch_digest = cad_action_batch_fingerprint(batch)
    before = artifact_fingerprint(pcb)
    receipt_path = _receipt_path(root, batch.idempotency_key)
    receipt = _read_receipt(receipt_path)
    if receipt is not None:
        receipt_digest = str(receipt.get("batch_fingerprint") or "")
        if receipt_digest != batch_digest:
            return _cad_observation(
                batch,
                status="rejected",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                detail="idempotency key is already bound to a different CAD batch",
            )
        prior_raw = receipt.get("observation")
        if isinstance(prior_raw, dict):
            try:
                prior = CadActionObservation.model_validate(prior_raw)
            except ValueError:
                prior = None
            if (
                prior is not None
                and prior.status in {"applied", "already_applied"}
                and prior.after_fingerprint
                and before == prior.after_fingerprint
            ):
                return prior.model_copy(
                    update={
                        "status": "already_applied",
                        "detail": "idempotent replay; artifact already contains the batch",
                    }
                )

    if before != batch.base_artifact_fingerprint:
        observation = _cad_observation(
            batch,
            status="rejected",
            pcb=pcb,
            batch_fingerprint=batch_digest,
            before_fingerprint=before,
            detail="artifact fingerprint no longer matches the planned CAD batch",
        )
        return observation

    kpy = kicad_python()
    if not kpy:
        observation = _cad_observation(
            batch,
            status="error",
            pcb=pcb,
            batch_fingerprint=batch_digest,
            before_fingerprint=before,
            detail="KiCad-python is unavailable; no CAD action was executed",
        )
        return observation

    timeout = max(10, min(int(timeout_seconds), 600))
    with tempfile.TemporaryDirectory(prefix=".cad_action_", dir=root) as temp_dir:
        temp_root = Path(temp_dir)
        batch_path = temp_root / "batch.json"
        candidate_path = temp_root / pcb.name
        batch_path.write_text(batch.model_dump_json(), encoding="utf-8")
        try:
            process = subprocess.run(
                [
                    kpy,
                    str(_CAD_ACTION_WORKER),
                    str(pcb),
                    str(candidate_path),
                    str(batch_path),
                    str(root),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - returned as a typed observation
            observation = _cad_observation(
                batch,
                status="error",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                detail=f"CAD worker invocation failed: {exc}",
            )
            return observation

        worker_result: dict[str, object] = {}
        for line in process.stdout.splitlines():
            if not line.startswith("RESULT "):
                continue
            try:
                decoded = json.loads(line[len("RESULT "):])
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                worker_result = decoded
        if not worker_result:
            tail = (process.stdout or process.stderr)[-600:]
            observation = _cad_observation(
                batch,
                status="error",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                detail=f"CAD worker returned no structured result; tail={tail!r}",
            )
            return observation

        raw_results = worker_result.get("action_results")
        try:
            action_results = [
                CadActionResult.model_validate(result)
                for result in raw_results
                if isinstance(result, dict)
            ] if isinstance(raw_results, list) else []
        except ValueError as exc:
            observation = _cad_observation(
                batch,
                status="error",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                detail=f"CAD worker returned an invalid action receipt: {exc}",
            )
            return observation
        error = str(worker_result.get("error") or "")
        if not worker_result.get("ok") or error:
            observation = _cad_observation(
                batch,
                status="error",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                action_results=action_results,
                detail=error or "CAD worker rejected the candidate",
            )
            return observation
        if not candidate_path.is_file() or candidate_path.stat().st_size <= 0:
            observation = _cad_observation(
                batch,
                status="error",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                action_results=action_results,
                detail="CAD worker reported success without a candidate artifact",
            )
            return observation

        candidate_fingerprint = artifact_fingerprint(candidate_path)
        reported_fingerprint = str(worker_result.get("after_fingerprint") or "")
        if reported_fingerprint != candidate_fingerprint:
            observation = _cad_observation(
                batch,
                status="error",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                action_results=action_results,
                detail="CAD candidate fingerprint differs from the worker receipt",
            )
            return observation
        if candidate_fingerprint == before:
            observation = _cad_observation(
                batch,
                status="rejected",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                after_fingerprint=candidate_fingerprint,
                action_results=action_results,
                detail="CAD batch produced no artifact change",
            )
            return observation

        # Recheck the source after the worker completes so a stale process can
        # never overwrite a concurrent pipeline revision.
        if artifact_fingerprint(pcb) != before:
            observation = _cad_observation(
                batch,
                status="rejected",
                pcb=pcb,
                batch_fingerprint=batch_digest,
                before_fingerprint=before,
                action_results=action_results,
                detail="source PCB changed concurrently; candidate was discarded",
            )
            return observation
        os.replace(candidate_path, pcb)

    after = artifact_fingerprint(pcb)
    observation = _cad_observation(
        batch,
        status="applied",
        pcb=pcb,
        batch_fingerprint=batch_digest,
        before_fingerprint=before,
        after_fingerprint=after,
        action_results=action_results,
        detail="candidate committed; downstream success checks are required",
    )
    _write_receipt(receipt_path, batch_digest, observation)
    return observation


@dataclass
class RouteOutcome:
    """Result of an autoroute attempt (or its graceful degradation)."""

    method: str  # "freerouting" | "deferred" | "error"
    ok: bool
    layers: int
    nets: int
    assigned_pads: int
    routed_tracks: int
    unconnected: int
    note: str
    dsn_path: str = ""
    ses_path: str = ""
    total_connections: int = -1
    routed_connections: int = -1
    metric_basis: str = "unavailable"


def kicad_python() -> str | None:
    """Locate KiCad's bundled python (has ``pcbnew``), derived from kicad-cli."""
    override = os.environ.get("KICAD_PYTHON")
    if override and Path(override).is_file():
        return override
    try:
        from ratsnestpro.eda.vendor.kicad_cli import find_kicad_cli

        cli = find_kicad_cli()
    except Exception:
        return None
    exe = "python.exe" if os.name == "nt" else "python3"
    cand = Path(cli).parent / exe
    return str(cand) if cand.is_file() else None


def freerouting_exe() -> str | None:
    """Locate the Freerouting launcher (container or bundled Windows runtime)."""
    override = os.environ.get("FREEROUTING_EXE")
    if override and Path(override).is_file():
        return override
    on_path = shutil.which("freerouting")
    if on_path:
        return on_path
    candidates = [
        Path("/usr/local/bin/freerouting"),
        Path("/usr/bin/freerouting"),
        Path.home() / "freerouting_app" / "freerouting" / "freerouting.exe",
        Path.home() / "freerouting" / "freerouting.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def available() -> bool:
    """True when both KiCad-python and Freerouting can be located."""
    return bool(kicad_python() and freerouting_exe())


def autoroute(
    pcb_path: str | os.PathLike[str],
    netmap: NetMap,
    max_passes: int = 15,
    layer_count: int = 2,
    clearance_mm: float = 0.2,
    track_width_mm: float = 0.2,
    via_diameter_mm: float = 0.6,
    via_drill_mm: float = 0.3,
    random_seed: int | None = None,
    net_classes: list[dict] | None = None,
    power_nets: list[str] | None = None,
    critical_nets: list[str] | None = None,
) -> RouteOutcome:
    """Assign nets from ``netmap`` onto the board and autoroute it in place."""
    nets = len(netmap)
    kpy, fr = kicad_python(), freerouting_exe()
    if not kpy or not fr:
        missing = "KiCad-python" if not kpy else "Freerouting"
        return RouteOutcome("deferred", False, layer_count, nets, 0, 0, -1,
                            f"{missing} unavailable; signal routing deferred")

    pcb = Path(pcb_path).resolve()
    router_timeout = _router_timeout(layer_count)
    with tempfile.TemporaryDirectory(prefix="rnp_route_") as temp_dir:
        nm_path = Path(temp_dir) / "netmap.json"
        nm_path.write_text(json.dumps(netmap), encoding="utf-8")
        from ratsnestpro.eda.routing_rules import bind_net_classes

        # Validate before invoking any mutating CAD action.
        bound_classes = bind_net_classes(net_classes, list(netmap), power_nets or []) if net_classes else []
        rule_path = Path(temp_dir) / "routing-rules.json"
        rule_path.write_text(json.dumps({"classes": bound_classes,
                                        "critical_nets": [n for n in (critical_nets or []) if n in netmap]}),
                             encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    kpy,
                    str(_WORKER),
                    str(pcb),
                    str(nm_path),
                    fr,
                    str(pcb.parent),
                    str(max_passes),
                    str(layer_count),
                    str(clearance_mm),
                    str(track_width_mm),
                    str(via_diameter_mm),
                    str(via_drill_mm),
                    "" if random_seed is None else str(random_seed),
                    str(rule_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # Leave a small margin beyond the worker's Freerouting timeout
                # so it can serialize its structured result.
                timeout=router_timeout + 60,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the note, never raised
            return RouteOutcome(
                "error",
                False,
                layer_count,
                nets,
                0,
                0,
                -1,
                f"router invocation failed: {exc}",
            )

    data: dict[str, object] = {}
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            try:
                data = json.loads(line[len("RESULT "):])
            except json.JSONDecodeError:
                data = {}
    if not data:
        tail = (proc.stdout or proc.stderr)[-300:]
        return RouteOutcome(
            "error",
            False,
            layer_count,
            nets,
            0,
            0,
            -1,
            f"no worker result; tail={tail!r}",
        )

    ok = bool(data.get("fr_ok"))
    err = str(data.get("error") or "")
    assigned = _as_int(data.get("assigned"), 0)
    tracks = _as_int(data.get("routed_tracks"), 0)
    unconn = _as_int(data.get("unconnected"), -1)
    total_connections = _as_int(data.get("total_connections"), -1)
    routed_connections = _as_int(data.get("routed_connections"), -1)
    def real_artifact_path(value: object) -> str:
        path = Path(str(value or ""))
        try:
            return str(path) if path.is_file() and path.stat().st_size > 0 else ""
        except OSError:
            return ""

    dsn_path = real_artifact_path(data.get("dsn_path"))
    ses_path = real_artifact_path(data.get("ses_path"))
    router_tail = str(data.get("fr_tail") or "")
    note = err or (
        f"tracks={tracks}, connections={routed_connections}/"
        f"{total_connections}, unconnected={unconn}"
    )
    if router_tail:
        note = f"{note}; freerouting_tail={router_tail[-600:]}"
    return RouteOutcome(
        method="freerouting" if ok else ("error" if err else "deferred"),
        ok=ok,
        layers=_as_int(data.get("layers"), layer_count),
        nets=nets,
        assigned_pads=assigned,
        routed_tracks=tracks,
        unconnected=unconn,
        note=note,
        dsn_path=dsn_path,
        ses_path=ses_path,
        total_connections=total_connections,
        routed_connections=routed_connections,
        metric_basis=str(data.get("metric_basis") or "unavailable"),
    )

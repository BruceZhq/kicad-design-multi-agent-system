"""Signal autorouting via Freerouting, driven through KiCad's own python.

The pipeline stays deterministic and pinned: the LLM decides the routing rules
(layers, net classes, widths — see route_plan/route_planes); this module only
*executes the geometry*. It assigns nets to pads from the pinmap, exports a
Specctra DSN, runs Freerouting, and imports the SES back as real tracks.
Whether an unavailable router blocks the pipeline is decided by the pipeline
context; this module always reports the real execution outcome.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# net name -> list of (ref, pad_number)
NetMap = dict[str, list[list[str]]]

_WORKER = Path(__file__).with_name("_route_worker.py")


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

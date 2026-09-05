"""Typed EDA primitives over the vendored kicad-mcp-py core.

This is the agent's *hands*: a small, typed surface that materializes and
inspects KiCad schematics and runs ERC. It imports the vendored core
in-process (no MCP JSON-RPC). Higher layers (orchestration, verifiers) depend
only on this module, never on the vendored internals directly.

Connectivity note: the design flow connects component pins by placing a net
label at each pin coordinate (the net name from the Circuit IR). Net grouping
is therefore derived from label names — which does not require installed KiCad
symbol geometry — while the union-find topology graph is also exposed for
wire-based checks.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ratsnestpro.eda.vendor import kicad_cli as _cli
from ratsnestpro.eda.vendor.connectivity import SchematicGraph
from ratsnestpro.eda.vendor.schematic import Schematic

# --------------------------------------------------------------------------- #
# Schematic document facade
# --------------------------------------------------------------------------- #


@dataclass
class PlacedLabel:
    net: str
    x: float
    y: float


class SchematicDoc:
    """A thin, typed facade around the vendored :class:`Schematic`.

    Exposes exactly the primitives the orchestration layer needs: create,
    add component/wire/net-label/power/no-connect, save, and read back
    components / nets / label-derived netlist.
    """

    def __init__(self, sch: Schematic) -> None:
        self._sch = sch
        self.drawing_receipt: dict[str, Any] = {}

    # -- construction --------------------------------------------------- #

    @classmethod
    def new(cls, paper: str = "A4") -> SchematicDoc:
        return cls(Schematic.blank(paper=paper))

    @classmethod
    def load(cls, path: str | Path) -> SchematicDoc:
        return cls(Schematic.load(Path(path)))

    # -- mutation ------------------------------------------------------- #

    def add_component(
        self,
        lib_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        footprint: str = "",
        dnp: bool = False,
        properties: dict[str, str] | None = None,
    ) -> str:
        return self._sch.add_component(
            lib_id=lib_id,
            reference=reference,
            value=value,
            x=x,
            y=y,
            rotation=rotation,
            footprint=footprint,
            dnp=dnp,
            properties=properties,
        )

    def add_wire(self, x1: float, y1: float, x2: float, y2: float) -> str:
        return self._sch.add_wire(x1, y1, x2, y2)

    def add_net_label(
        self,
        text: str,
        x: float,
        y: float,
        label_type: str = "local",
        rotation: float = 0.0,
    ) -> str:
        return self._sch.add_net_label(text, x, y, label_type=label_type, rotation=rotation)

    def add_power_symbol(self, net: str, x: float, y: float, rotation: float = 0.0) -> str:
        return self._sch.add_power_symbol(net, x, y, rotation=rotation)

    def add_no_connect(self, x: float, y: float) -> str:
        return self._sch.add_no_connect(x, y)

    def add_text(self, text: str, x: float, y: float, rotation: float = 0.0) -> str:
        return self._sch.add_text(text, x, y, rotation=rotation)

    def add_junction(self, x: float, y: float) -> str:
        return self._sch.add_junction(x, y)

    def set_component_property(self, reference: str, name: str, value: str) -> None:
        self._sch.edit_component(reference, properties={name: value})

    def embed_lib_symbols(self) -> list[str]:
        """Populate the ``lib_symbols`` cache with real symbol graphics.

        KiCad expects every placed symbol's graphic definition (body, pins) to
        be cached in the schematic's ``lib_symbols`` block so the sheet renders
        and is self-contained. This scans the placed instances, resolves each
        ``lib_id`` to a flattened symbol definition from the real symbol
        libraries, and injects any that are missing. Returns the list of
        ``lib_id`` values actually embedded. Symbols that cannot be resolved
        (library not configured) are skipped — never faked.
        """
        from ratsnestpro.eda import symbols as _symbols
        from ratsnestpro.eda.vendor.sexpr import find_first, tag_of

        root = self._sch.root
        libnode = find_first(root, "lib_symbols")
        if libnode is None:
            return []
        # lib_ids from top-level symbol instances only (avoid lib_symbols children).
        lib_ids: list[str] = []
        for child in root:
            if isinstance(child, list) and tag_of(child) == "symbol":
                lib = find_first(child, "lib_id")
                if lib and len(lib) > 1:
                    lib_ids.append(str(lib[1]))
        existing = {
            str(c[1])
            for c in libnode
            if isinstance(c, list) and tag_of(c) == "symbol" and len(c) > 1
        }
        embedded: list[str] = []
        for lib_id in dict.fromkeys(lib_ids):
            if lib_id in existing:
                continue
            node = _symbols.symbol_definition(lib_id)
            if node is not None:
                libnode.append(node)
                existing.add(lib_id)
                embedded.append(lib_id)
        return embedded

    # -- inspection ----------------------------------------------------- #

    def components(self) -> list[dict[str, Any]]:
        return self._sch.list_components()

    def references(self) -> list[str]:
        return [c["reference"] for c in self._sch.list_components() if c.get("reference")]

    def nets(self) -> list[str]:
        return self._sch.list_nets()

    def lib_symbol_ids(self) -> list[str]:
        """The ``lib_id`` keys currently cached in the ``lib_symbols`` block."""
        from ratsnestpro.eda.vendor.sexpr import find_first, tag_of

        libnode = find_first(self._sch.root, "lib_symbols")
        if libnode is None:
            return []
        return [
            str(c[1])
            for c in libnode
            if isinstance(c, list) and tag_of(c) == "symbol" and len(c) > 1
        ]

    def labels(self) -> list[PlacedLabel]:
        out: list[PlacedLabel] = []
        for label in self._sch.list_labels():
            at = label.get("at")
            if label.get("text") and at:
                out.append(PlacedLabel(net=label["text"], x=at[0], y=at[1]))
        return out

    def label_netlist(self) -> dict[str, list[tuple[float, float]]]:
        """Nets derived from placed label names → their coordinates.

        Reflects the pin-labeling connection strategy and needs no installed
        KiCad symbol geometry.
        """
        nets: dict[str, list[tuple[float, float]]] = {}
        for label in self.labels():
            nets.setdefault(label.net, []).append((label.x, label.y))
        return nets

    def topology_components(self) -> list[dict[str, Any]]:
        """Union-find connected components over wire/label/pin topology."""
        return SchematicGraph(self._sch).components()

    def shorted_nets(self) -> list[list[str]]:
        return SchematicGraph(self._sch).shorted_nets()

    # -- persistence ---------------------------------------------------- #

    def save(self, path: str | Path) -> Path:
        return self._sch.save(Path(path))

    @property
    def raw(self) -> Schematic:
        return self._sch


# --------------------------------------------------------------------------- #
# ERC via kicad-cli
# --------------------------------------------------------------------------- #


@dataclass
class ErcViolation:
    severity: str
    rule_id: str
    message: str


@dataclass
class ErcResult:
    available: bool
    ran: bool
    ok: bool
    returncode: int | None = None
    report_path: str | None = None
    violations: list[ErcViolation] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def summary(self) -> str:
        if not self.available:
            return "kicad-cli not available; ERC skipped"
        if not self.ran:
            return "ERC did not run"
        return f"ERC ran: {self.error_count} error(s), {self.warning_count} warning(s)"


def kicad_cli_available(explicit_path: str | None = None) -> str | None:
    """Return the resolved kicad-cli path, or None if not found."""
    try:
        return _cli.find_kicad_cli(explicit_path)
    except _cli.KicadCliNotFound:
        return None


def _parse_erc_json(path: Path) -> list[ErcViolation]:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[ErcViolation] = []
    # kicad-cli sch erc --format json emits {"sheets": [{"violations": [...]}]}
    sheets = data.get("sheets") if isinstance(data, dict) else None
    if isinstance(sheets, list):
        for sheet in sheets:
            for v in sheet.get("violations", []) or []:
                out.append(
                    ErcViolation(
                        severity=str(v.get("severity", "error")),
                        rule_id=str(v.get("type", v.get("rule", "erc"))),
                        message=str(v.get("description", v.get("message", ""))),
                    )
                )
    # Some versions put violations at the top level.
    for v in (data.get("violations", []) if isinstance(data, dict) else []) or []:
        out.append(
            ErcViolation(
                severity=str(v.get("severity", "error")),
                rule_id=str(v.get("type", v.get("rule", "erc"))),
                message=str(v.get("description", v.get("message", ""))),
            )
        )
    return out


def run_erc(
    sch_path: str | Path,
    out_dir: str | Path | None = None,
    explicit_cli: str | None = None,
) -> ErcResult:
    """Run ERC via kicad-cli with JSON output. Missing kicad-cli is reported as
    unavailable — never silently treated as a pass."""
    cli_path = kicad_cli_available(explicit_cli)
    if cli_path is None:
        return ErcResult(available=False, ran=False, ok=False)

    sch_path = Path(sch_path)
    out_dir = Path(out_dir) if out_dir else sch_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / (sch_path.stem + ".erc.json")

    try:
        proc = subprocess.run(
            [
                cli_path,
                "sch",
                "erc",
                "--format",
                "json",
                "--severity-all",
                "--output",
                str(report),
                "--exit-code-violations",
                str(sch_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:  # pragma: no cover - environment dependent
        return ErcResult(available=True, ran=False, ok=False, stderr=str(exc))

    violations = _parse_erc_json(report) if report.exists() else []
    errors = sum(1 for v in violations if v.severity == "error")
    warnings = sum(1 for v in violations if v.severity == "warning")
    return ErcResult(
        available=True,
        ran=True,
        ok=(errors == 0),
        returncode=proc.returncode,
        report_path=str(report) if report.exists() else None,
        violations=violations,
        error_count=errors,
        warning_count=warnings,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )

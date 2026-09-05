"""Render actual CAD files for model inspection; never manufacture a preview."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path


def render_cad(source: Path, *, layers: str = "F.Cu,F.Silkscreen,Edge.Cuts") -> dict:
    if source.suffix not in {".kicad_sch", ".kicad_pcb"}:
        raise ValueError("render requires an actual KiCad schematic or PCB")
    cli, rasterizer = shutil.which("kicad-cli"), shutil.which("rsvg-convert")
    if not cli or not rasterizer:
        raise ValueError("visual inspection requires kicad-cli and rsvg-convert (librsvg2-bin)")
    allowed = {"F.Cu", "B.Cu", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts", "F.Fab", "B.Fab"}
    if not set(layers.split(",")) <= allowed:
        raise ValueError("unsupported visual inspection layer")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    key = hashlib.sha256((digest + layers).encode()).hexdigest()[:24]
    directory = source.parent / ".engineering-views" / key
    if not directory.resolve().is_relative_to(source.parent.resolve()):
        raise ValueError("render output must remain inside the run workspace")
    directory.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() or not p.resolve().is_relative_to(directory.resolve()) for p in directory.iterdir()):
        raise ValueError("render cache contains an unsafe link")
    png = directory / "view.png"
    if not png.exists():
        if source.suffix == ".kicad_sch":
            command = [cli, "sch", "export", "svg", "--output", str(directory), str(source)]
        else:
            command = [cli, "pcb", "export", "svg", "--mode-single", "--layers", layers,
                       "--exclude-drawing-sheet", "--fit-page-to-board", "--output",
                       str(directory / "board.svg"), str(source)]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=45)
        if proc.returncode:
            raise ValueError(f"CAD rendering failed: {proc.stderr[-1200:]}")
        pages = sorted(directory.glob("*.svg"))
        if not pages:
            raise ValueError("KiCad did not produce a real SVG")
        proc = subprocess.run(
            [rasterizer, "--keep-aspect-ratio", "--width", "1800", "--height", "1400",
             "--output", str(png), str(pages[0])],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode:
            raise ValueError(f"SVG rasterization failed: {proc.stderr[-1200:]}")
    if not png.resolve().is_relative_to(directory.resolve()):
        raise ValueError("rendered image escaped its output directory")
    data = png.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 4_000_000:
        raise ValueError("invalid or oversized CAD preview")
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError("CAD file changed during rendering; request a fresh observation")
    return {"source": str(source), "source_sha256": digest, "image_path": str(png),
            "image_sha256": hashlib.sha256(data).hexdigest(), "layers": layers,
            "page": 1, "page_count": len(list(directory.glob("*.svg")))}

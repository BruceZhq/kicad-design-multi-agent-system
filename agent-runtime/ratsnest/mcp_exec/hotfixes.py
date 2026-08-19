"""Documented hotfixes applied to the in-process vendored KiCADInterface.

Vendor stays unforked: these are method overrides bound at host creation,
removable one-by-one as fixes land upstream.

HF-1  _extract_components_from_schematic (schematic_handlers.py:2803)
      upstream uses the invalid ElementTree XPath ".\\components\\comp"
      (backslashes), so sync_schematic_to_board NEVER sees schematic
      components -> "0 footprints added, 0 skipped". Also, its kicad-cli
      discovery misses non-standard installs (E:\\KiCad). This override uses
      the correct XPath and RatsNest's configured kicad-cli.
"""

from __future__ import annotations

import subprocess
import tempfile
import types
import xml.etree.ElementTree as ET
from pathlib import Path

from ratsnest.config import Config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def apply_hotfixes(host, config: Config) -> list[str]:
    """Bind fixed methods onto the vendored KiCADInterface instance."""
    applied = []

    def _extract_components_from_schematic(self, schematic_path: str) -> list:
        kicad_cli = None
        if config.kicad_cli and Path(config.kicad_cli).exists():
            kicad_cli = str(config.kicad_cli)
        else:
            try:
                kicad_cli = self._find_kicad_cli_static()
            except Exception:
                kicad_cli = None
        if not kicad_cli:
            return []
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            proc = subprocess.run(
                [kicad_cli, "sch", "export", "netlist", "--format", "kicadxml",
                 "--output", str(tmp_path), schematic_path],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
            if proc.returncode != 0:
                return []
            root = ET.parse(tmp_path).getroot()
            components = []
            for comp in root.findall("./components/comp"):  # HF-1: valid XPath
                components.append({
                    "reference": comp.get("ref", ""),
                    "value": comp.findtext("value", ""),
                    "footprint": comp.findtext("footprint", ""),
                })
            return components
        except Exception:
            return []
        finally:
            tmp_path.unlink(missing_ok=True)

    host._extract_components_from_schematic = types.MethodType(
        _extract_components_from_schematic, host)
    applied.append("HF-1 sync component extraction (XPath + kicad-cli path)")
    return applied

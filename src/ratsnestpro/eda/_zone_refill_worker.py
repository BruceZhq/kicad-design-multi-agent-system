"""Refill PCB copper zones with KiCad's system Python."""

from __future__ import annotations

import sys
from pathlib import Path

import pcbnew  # type: ignore[import-not-found]


def main() -> int:
    pcb_path = Path(sys.argv[1])
    board = pcbnew.LoadBoard(str(pcb_path))
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(str(pcb_path), board)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

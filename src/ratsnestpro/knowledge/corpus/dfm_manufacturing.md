---
role: dfm,routing
title: Design for manufacturing (DFM)
---

# Design for manufacturing

Stay inside the fab's capabilities and leave margin. The bottom-line checks
enforce the numeric minimums; this knowledge explains the intent.

Key DFM rules:
- Track width and clearance at or above the fab minimum (e.g. JLCPCB standard
  ~0.127 mm). Leave margin rather than hugging the limit.
- Via/annular ring and drill within capability; avoid tented-only where a test
  probe is needed.
- Board-edge clearance: keep copper and parts away from the routed edge.
- Silkscreen: minimum line width and text height; do not put silk over pads.
- Solder mask: sliver-free; respect mask expansion and dam width.
- Panelization and fiducials for assembly; keep tooling/keep-out areas clear.
- Add test points on important nets for bring-up and flying-probe test.

When a value would violate capability, widen/space it or move to more layers —
never ship below the fab minimum.

---
role: routing
title: Trace width and current capacity
---

# Trace width vs current (IPC-2221 / IPC-2152)

Trace width is set by current, allowed temperature rise, and copper weight.
IPC-2221 gives a practical external-layer rule of thumb for 1 oz copper and a
~10 °C rise:

- Signal (< ~100 mA): 0.15–0.25 mm is plenty; use the fab minimum as a floor.
- ~0.5 A: about 0.3 mm.
- ~1 A: about 0.5 mm.
- ~2 A: about 0.9–1.0 mm.
- ~3 A: about 1.5 mm.

Internal layers carry less current for the same width (roughly halve it) and
need wider traces. Larger temperature rise allows thinner traces; be
conservative on power nets.

Rules:
- Power and ground traces are wider than signals; prefer pours/planes for them.
- Never go below the fab's minimum track width; treat that as a hard floor
  regardless of current.
- Widen high-current traces and shorten their length; add copper pour if
  space allows.

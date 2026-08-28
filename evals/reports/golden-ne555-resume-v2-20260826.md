# Golden NE555 checkpoint-resume verification — 2026-08-26

## Scope

This report covers the corrected repository only:

`E:\agent-service-toolkit-main\agent-service-toolkit_frame\agent-service-toolkit-main`

It verifies one incremental continuation of the existing `golden.01-ne555-led-blinker` run. It is a fixed golden-board smoke test, not a statistical success-rate claim and not manufacturing approval.

## Outcome

- Final system status: `SUCCESS`
- Hardware pipeline: `17/17`, `status=ok`, `outcome=release_ready`
- Release blockers: `0`
- Independent Reviewer: `PASS`
- Overall release verdict: `PASS`
- Artifact manifest delivery status: `release_ready`
- Human acceptance: not performed (`N/A`)

## Checkpoint-resume evidence

The existing run directory and first fourteen verified pipeline steps were reused. The continuation emitted only these pipeline-start events:

1. `15/17 route_signals`, with `completed_steps=14`
2. `16/17 route_fab`, with `completed_steps=15`
3. `17/17 manufacture`, with `completed_steps=16`

No Activity for steps 1–14 was scheduled. The event interval from the first workflow event through the terminal Supervisor event was approximately `92.93 s`.

The checkpoint revision advanced from `2` to `3`, and the persisted one-shot resume marker is `route_signals`.

## Entity mutation and release receipt

- PCB SHA-256 before repair: `8ec7a1a436782e3c1a8bb80effc3fb9d8c61b6887cf5db846d862b9dd27beb7e`
- PCB SHA-256 after repair: `564d4c10a5c293ebb949c60977d0ecf4a04a16070f37ee0259836175275a482f`
- PCB modification time after repair: `2026-08-26T16:45:05+08:00`
- Raw physical zone count in the final `.kicad_pcb`: `1`
- Release receipt PCB SHA-256: `564d4c10a5c293ebb949c60977d0ecf4a04a16070f37ee0259836175275a482f`
- Receipt `requirement_release_ready`: `true`
- Receipt blockers/findings: none

The new zone is therefore an actual KiCad entity mutation, not a narrative claim. The receipt is bound to the exact final PCB bytes.

## Deterministic gates

| Gate | Result |
| --- | --- |
| Freerouting | `7/7` nets, historical router note `17/17` connections, `unconnected=0` |
| ERC | ran, `0` errors, `26` warnings |
| DRC | ran, `0` errors, `25` warnings, `0` unconnected |
| Component release | passed, no unresolved/non-release component blockers |
| Placement constraints | manifest found, digest valid, no violations |
| Physical requirements | passed; two layers, board bounds, B.Cu GND plane, track widths, decoupling distance and four NPTH M2 holes checked |
| Independent review | `PASS` |
| Overall release | `PASS` |

Warnings remain visible and are not silently converted into errors or erased. In this profile the current release policy blocks on the verified error/unconnected and frozen physical-invariant gates.

## Required artifact verification

The following files were found on disk and their current SHA-256 values matched the emitted artifact manifest:

- `pipeline_result.json`
- `p2-golden.01-ne555-led-blinker.kicad_sch`
- `p2-golden.01-ne555-led-blinker.kicad_pcb`
- `p2-golden.01-ne555-led-blinker.dsn`
- `p2-golden.01-ne555-led-blinker.ses`
- `p2-golden.01-ne555-led-blinker_bom.csv`
- `p2-golden.01-ne555-led-blinker_cpl.csv`
- `p2-golden.01-ne555-led-blinker.release_invariants.json`

The final artifact manifest contains `39` artifacts. `pipeline_result.json` SHA-256 is `fa01132019a5ab7c05d4fa2102ced468378a08b53688c6cf8744b433e96b9dbe`.

## Harness behavior exercised

- The original full run exercised bounded entity-mutating layout repair and rejected a non-monotonic plane patch.
- The deployed harness correction changed the generic plane materialization strategy to solid pad connection with orphan-island removal.
- This continuation exercised the governed checkpoint-resume path and re-ran only the failed release step and its downstream verification/manufacturing steps.
- No upstream replan was used; `repair_attempts=0`, `upstream_replans=0`, and the repaired deterministic worker produced the passing entity directly.
- Infrastructure failures remain on a separate recovery path and cannot be selected by the release-only resume helper.

## Code verification before live execution

- New checkpoint-resume focused tests: `13 passed`
- Adjacent checkpoint, requirement, governance and Temporal tests: `28 passed`
- Ruff: passed
- `compileall`: passed
- Independent static audit: no remaining P0/P1 blocker in the resume patch

The focused suite covers stale-revision rejection, exact upstream-prefix preservation, one-shot Activity retry semantics, invalid resume-step rejection, infrastructure/design failure separation, manifest content binding, legacy adapter behavior and Temporal history compatibility.

## Remaining caveats

- This is one fixed golden-board case; it does not establish a population-level release-ready rate.
- No blind human inspection or manufacturing sign-off was performed.
- Parts procurement remained `partial` because no real-time stock/price/lead-time source was asserted. That did not bypass component/KiCad closure.
- The resumed `RouteResult` exposes `routed_connections=-1/total_connections=-1` while its preserved Freerouting note records `17/17`; authoritative connectivity is the fresh KiCad DRC result (`unconnected=0`). This is a telemetry-normalization issue, not a release-gate bypass.

## Evidence files

- SSE: `evals/reports/golden-ne555-resume-v2-20260826.sse`
- Pipeline result: `data/ratsnestpro/runs/p2-golden.01-ne555-led-blinker-88a2744c--d761b96e562c374c--43d5e1c48d43/pipeline_result.json`
- Release receipt: `data/ratsnestpro/runs/p2-golden.01-ne555-led-blinker-88a2744c--d761b96e562c374c--43d5e1c48d43/p2-golden.01-ne555-led-blinker.release_invariants.json`
- Final PCB: `data/ratsnestpro/runs/p2-golden.01-ne555-led-blinker-88a2744c--d761b96e562c374c--43d5e1c48d43/p2-golden.01-ne555-led-blinker.kicad_pcb`
- Reviewer report: `data/ratsnestpro/reviews/p2-golden.01-ne555-led-blinker-88a2744c-review.md`

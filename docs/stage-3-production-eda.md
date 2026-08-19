# Stage 3 - Production EDA Capability

Date: 2026-07-15
Status: Complete for the frozen support matrix

## Objective

Stage 3 turns the controlled Stage 2 workflow into a deliberately bounded
power-board product. A run is release-reviewable only when the immutable plan
uses a supported circuit family and every required production gate passes.

This stage does **not** claim arbitrary PCB synthesis or regulatory
certification. SPICE is a topology-level functional model, thermal is a
datasheet-based estimate, and the EMC gate checks first-order layout rules. A
hardware engineer still owns design review, prototype validation, and product
qualification.

## Frozen support matrix

| Family | Trusted controller | Qualified design envelope | Production backend |
| --- | --- | --- | --- |
| `adjustable_ldo` v1 | TI `TLV1117-ADJ` | 2.7-13 V input, 1.25-13.7 V output, up to 0.5 A after product derating; dropout, loss, efficiency, and estimated junction temperature must pass | `crew` |
| `asynchronous_buck` v1 | TI `LM2596S-ADJ` | 7-35 V input, 1.23-30 V output, up to 2 A after product derating; duty cycle, inductor, diode, capacitor, and thermal limits must pass | `crew` |

`auto` selects an LDO only when its voltage margin, efficiency, dissipation,
and thermal estimate are safe. Otherwise it selects the Buck family. An
explicit family request outside its envelope is rejected during planning.

The `template` and external `mcp` backends remain development/compatibility
paths. They may produce review artifacts, but a missing PCB or production gate
prevents release review. Only Crew is in the Stage 3 production matrix.

The following remain unsupported and fail closed during planning: boost,
buck-boost, flyback, isolated power, inverters, battery chargers, USB,
Ethernet, CAN, MCU/FPGA, motor-control, and other non-power features. They are
never silently mapped onto an LDO or Buck design.

## Trusted component catalog

The catalog is a versioned, non-evolvable product asset. Every physical
component in a `BoardPlan` binds to a catalog ID containing:

- manufacturer and exact MPN or an approved value-coded resistor family;
- KiCad symbol, footprint, pin roles, and component role;
- lifecycle status, ratings, derating data, and authoritative source URLs;
- BOM inclusion and PCB-placement policy.

LLMs and AHE strategies cannot invent or mutate catalog bindings. Catalog
changes require code review, source evidence, a version bump, and benchmark
requalification.

The Buck v1 power path uses TI `LM2596S-ADJ`, Coilcraft
`MSS1210H-683MED` (68 uH), onsemi `NRVBS540T3G` (5 A / 40 V), Panasonic
`EEUFR1H471` (470 uF / 50 V), Panasonic `EEUFR1H221` (220 uF / 50 V), and
YAGEO `RC1206FR` feedback resistors. The wider 1206 pads preserve the
qualified output-rail copper width at the divider connection. LED current
limiting remains on the approved `RC0805FR` family.
The LDO v1 path uses TI `TLV1117-ADJ` and KEMET
`T491B106M025AT` 10 uF / 25 V capacitors.

The qualified catalog revision is `stage3.1.2`.

## Plan contract v2

`ratsnest.design-plan.v2` adds these immutable fields while retaining v1 read
compatibility in the control plane:

- circuit family and family version;
- trusted catalog version and per-component catalog IDs/roles;
- typed electrical and thermal design limits;
- placement hints, net classes, and required verification gates;
- physical/BOM flags for power symbols and other virtual components.

Execution recomputes the deterministic solution and canonical graph from the
approved `DesignSpec`. A family, part, rating, footprint, value, connection,
or gate mismatch invalidates the plan hash-bound execution.

## Production verification profile

The following gates are required and fail closed:

1. `catalog` - every component matches the approved catalog revision and
   lifecycle/rating policy.
2. `bom` - CSV and manufacturing manifest match the approved plan; all BOM
   lines have MPN, manufacturer, value, footprint, and source evidence.
3. `erc` - KiCad CLI reports zero error-severity electrical violations.
4. `drc` - KiCad CLI reports zero error-severity PCB violations, including
   unrouted, clearance, crossing, short, and schematic-parity failures.
5. `spice` - KiCad's ngspice shared engine runs the family model and verifies
   output accuracy and ripple against the approved values.
6. `thermal` - controller, diode, and inductor estimates remain below the
   product design limits with recorded assumptions.
7. `emc` - first-order placement/loop checks pass; for Buck this includes a
   bounded switch-node loop, feedback placement away from the power path, and
   approved net widths. Pad-local neck-down is bounded to at least 70% of the
   nominal width and no more than 15% of a routed net's total length.

Each gate writes machine-readable evidence under `verification/`, contributes
typed findings, appears in the final `Scorecard`, and is included in the
release artifact. `unavailable`, `error`, and `failed` are all non-releasable.
kicad-happy remains an independent analytical layer above these hard gates.

PCB routing uses Freerouting against the board held by the shared KiCad host.
RatsNest owns the exact CLI argv: GUI and analytics are disabled, the
unbounded optimizer is disabled, routing is single-threaded, and both pass
count and wall-clock time are bounded. The Agent can only request the
zero-argument `autoroute_board` capability. Direct pad-to-pad routing is
development-only and can never satisfy the production DRC/release profile.

## Acceptance benchmark

Stage 3 is complete only when all of the following are reproducible:

- unit/contract tests cover family selection, safe-envelope rejection,
  catalog immutability, BOM reconciliation, ngspice metrics, thermal limits,
  EMC rules, and KiCad report parsing;
- golden LDO and Buck requirements produce approved-plan-compatible KiCad
  projects with all required gates passing;
- seeded defects for unsupported topology, wrong divider, missing/obsolete
  MPN, underrated power part, ERC fault, DRC short/unrouted net, thermal
  overload, and EMC placement are detected by their owning gate;
- the Java control plane refuses release review unless status is `converged`
  and all required gate results are `passed`;
- the frontend displays family, component/BOM evidence, every gate status,
  and blocks release actions when verification is incomplete.

## Acceptance evidence

Reproduced on 2026-07-15 with KiCad 10, Freerouting 2.2.4, ngspice, and the
Crew production backend:

| Golden design | Result | Key measured evidence |
| --- | --- | --- |
| 5 V to 3.3 V, 50 mA LDO with red LED | 100/100; all 7 required gates passed | 3.297502 V average output, 0 mV modeled ripple, 30.27 C estimated junction, zero unconnected items |
| 12 V to 5 V, 0.5 A Buck with red LED | 100/100; all 7 required gates passed | 5.008594 V average output, 13.0004 mV modeled ripple, 43.478 C estimated controller junction, 34.468 mm SW copper, 36.955 mm FB copper, 1.5 mm minimum `+5V` width, zero unconnected items |

Regression and packaging evidence:

- Python: 82 tests passed.
- Frontend: 7 Vitest tests passed; strict TypeScript and Vite production build
  passed.
- Java control plane: 17 tests passed with zero failures/errors; `mvn clean
  package` produced `ratsnest-control-plane-0.1.0.jar`.
- The clean JAR contains one current JS bundle, one current CSS bundle, and the
  SPA entry point; no stale hashed frontend bundles remain.

## Source baseline

- TI LM2596 product and datasheet: https://www.ti.com/product/LM2596 and
  https://www.ti.com/lit/ds/symlink/lm2596.pdf
- TI TLV1117 product and datasheet: https://www.ti.com/product/TLV1117 and
  https://www.ti.com/lit/ds/symlink/tlv1117.pdf
- Coilcraft MSS1210H-683: https://www.coilcraft.com/en-us/products/power/shielded-inductors/ferrite-drum/mss-mos/mss1210h/mss1210h-683/
- onsemi MBRS540/NRVBS540 datasheet: https://www.onsemi.com/download/data-sheet/pdf/mbrs540t3-d.pdf
- Panasonic capacitor records: https://industrial.panasonic.com/ww/products/pt/aluminum-cap-lead/models/EEUFR1H471 and https://industrial.panasonic.com/ww/products/pt/aluminum-cap-lead/models/EEUFR1H221
- Freerouting: https://github.com/freerouting/freerouting

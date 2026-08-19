# RatsNest Implementation Plan (Phases 0–3 core)

> **For agentic workers:** executed inline this session (executing-plans style). Checkboxes track progress.
> Scope adaptations vs the design doc, agreed by pragmatics of this machine: frontend deferred;
> Spring uses H2 + dev-profile subprocess dispatch (RabbitMQ/Postgres/MinIO arrive with docker-compose later);
> agents run in deterministic mode by default (LLM hooks present, off — the loop needs zero API keys to test).

**Goal:** Working evaluate→repair→re-evaluate→converge loop on KiCad projects + AHE v1 with promotion gates, per `../../kicad_auto_evolving_multiagent_plan.md` (rev 2).

**Architecture:** Python 3.11 agent runtime (all intelligence) + minimal Spring Boot 3 control plane (governance skeleton). kicad-happy vendored by reference (`RATSNEST_KICAD_HAPPY_ROOT`), driven via subprocess JSON contract. ATDP trajectory events emitted from every orchestrator node to JSONL (+ optional HTTP sink).

**Tech stack:** Python 3.11.5 (KiCad 10 bundled → venv), pydantic v2, pytest; Java 17 + Maven + Spring Boot 3.2/H2; kicad-cli at `E:\KiCad\10.0\bin\kicad-cli.exe`.

**Environment facts (verified):** system Python 3.9 too old for kicad-happy (PEP 604 at import); KiCad 10 bundles Python 3.11.5; Java 17 + Maven 3.8.5; Docker present; Node 24 present.

---

### Task 1: Scaffold + plan doc — this file. ✅
### Task 2: Python env — venv from KiCad Python 3.11.5; pydantic/pytest/pyyaml/httpx installed. ✅

### Task 3: `ratsnest/schemas/models.py` — Pydantic contracts
Scorecard, AnalyzerOutput (passthrough envelope), RepairOp/PatchPlan (op vocab v1: set_value, set_property, add_component, remove_component), RepairHint, StrategyBundle (content-hashed), TrajectoryEvent (ATDP ⟨o,h,a,y,r,m⟩), RunConfig, IterationRecord, RunRecord, ExperimentReport. `export.py` dumps JSON Schema to `schemas/` for Java codegen. Test: `tests/test_schemas.py` round-trips golden fixtures.

### Task 4: `ratsnest/kh_adapter/` — analyzer runner + scorecard
`runner.py`: locate `.kicad_sch`/`.kicad_pcb` in project dir, run kicad-happy analyzers via venv Python subprocess, parse+validate JSON envelope. `scorecard.py`: severity-normalized formula `100 − w_crit·crit − w_high·high − w_warn·warn − 15·erc_fail` with weights from strategy. Tests use the demo board.

### Task 5: `benchmarks/corpus/demo_board/` — hand-written KiCad 8+ schematic
Buck/LDO with FB divider, I2C pull-ups, LED+resistor, power symbols — parseable by `analyze_schematic.py` (verified by running it). `ratsnest/evolution/seed_defects.py` uses design_edit to create `benchmarks/seeded/demo_board_defective` (wrong FB resistor → wrong Vout, 10k I2C pull-ups @ 400kHz, stripped MPNs). Golden analyzer output snapshot stored for regression.

### Task 6: `ratsnest/design_edit/` — patch applier
`sexp_edit.py` (reuse kicad-happy `skills/bom/scripts/kicad_sexp.py` via import if API fits, else minimal targeted S-expr text editor preserving formatting), `patcher.py` (PatchPlan → edits, SHA-256 before/after, post-edit re-parse via analyzer, auto-rollback on failure), `kicad_cli.py` (ERC wrapper, feature-gated). Tests: value edit round-trip, corrupt-op rollback.

### Task 7: `ratsnest/agents/` + `agent-runtime/strategies/v0/strategy.yaml`
Strategy bundle: scorecard weights, repair mapping table (rule_id → op template + solver), suppressions, prompt fragments (unused in deterministic mode). `repair_planner.py`: findings × mappings → RepairHint[] → PatchPlan; solvers: divider Vout inverse (E-series snap), I2C pull-up sizing, LED resistor, MPN fill from curated map. `synthesizer.py`: dedupe/suppress/prioritize findings deterministically.

### Task 8: `ratsnest/orchestrator/loop.py` + `ratsnest/data_proxy/interceptor.py`
State machine analyze→synthesize→plan→apply→verify→converge/escalate; iteration budget; new-critical veto; score-monotonic acceptance. Recorder emits TrajectoryEvent per node to `runs/<id>/trajectory.jsonl` (+ optional POST `RATSNEST_CONTROL_PLANE_URL`). `run_store.py` persists RunRecord/IterationRecords. `cli.py`: evaluate/fix/evolve/stats/seed-defects.

### Task 9: `ratsnest/evolution/` — AHE v1
`registry.py` (load/hash/ACTIVE pointer/promote/rollback), `variants.py` (weight perturbation, mapping edits), `experiment.py` (candidate vs incumbent on seeded benchmark; gates: replay pass, mean score ↑, zero new critical, no per-board regression), `triggers.py` (per-rule fix success stats from ATDP JSONL → surface proposal). Test: deliberately bad strategy (e.g., mapping that breaks values) is REJECTED by gates.

### Task 10: `backend/` — Spring Boot minimal control plane
pom.xml (web, data-jpa, h2); entities DesignRun, AtdpEvent; REST: POST/GET `/api/runs` (dev-profile dispatch = ProcessBuilder → venv CLI), POST `/api/atdp/events`, GET `/api/runs/{id}/scorecard`. `mvn package` + smoke test.

### Task 11: End-to-end demo
`evaluate` defective board (low score) → `fix` (score climbs, zero new criticals, patches traceable to findings) → `evolve` one experiment (gate report, promotion) → `stats`. Results reported with real numbers.

**Verification commands** (from `agent-runtime/`):
`..\.venv\Scripts\python.exe -m pytest tests -q` · `..\.venv\Scripts\python.exe -m ratsnest fix ..\benchmarks\seeded\demo_board_defective` · `mvn -f ..\backend\pom.xml package`

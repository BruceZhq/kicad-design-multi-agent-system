# High Cohesion / Low Coupling Refactor — Design

**Date:** 2026-07-13
**Status:** Approved
**Scope:** All three tiers (Python agent-runtime, Java Spring Boot backend, React frontend)
**Approach:** C — surgical decoupling plus interface-driven protocols

## Problem

Exploration found five concrete coupling/cohesion defects in a system that is
otherwise verified working (43/43 Python tests, clean TS build, e2e flow green):

1. `eda.py` imports the private `_get_host()` from `crews/creator.py` — the
   in-process KiCad host is a shared tool trapped inside one crew.
2. Circuit-domain math is scattered: `design_gen/generator.py` imports
   `format_ohms`/`resistor_mpn` from `agents/repair_planner.py` (an agent
   doubling as a utility library), while `solve_board_values` lives in the
   generator but is consumed by the creator crew and the MCP backend.
3. The LLM-brain seam and the design-backend seam exist only implicitly —
   consumers depend on the concrete `LlmClient`, and
   `pipeline.generate_for_backend()` dispatches through an `if/elif` chain.
4. Java: `EdaController` copy-pastes the ownership policy from `RunController`
   (the comment admits it), and `EdaController` + `RunDispatchService` each
   build their own Python subprocess bridge.
5. `frontend/src/App.tsx` is 1,493 lines holding AuthBar, TimelinePanel,
   PreviewPanel, ReportPanel, and EdaPanel in one file.

## Design

This is a behavior-preserving refactor. No error semantics, HTTP statuses,
fallback discipline, or output artifacts change.

### 1. Python — shared KiCad host

New module `ratsnest/kicad_host.py`. The `_get_host()` singleton logic
(in-process `KiCADInterface` import, sys.path bootstrap, hotfix application)
moves out of `crews/creator.py` and becomes public `get_host(config)`.
`crews/creator.py` and `eda.py` both import from the new module.

### 2. Python — one home for circuit-domain math

New leaf module `ratsnest/circuit_math.py` (imports only `config`, `khlib`,
`schemas` — E-series snapping needs the kicad-happy scripts path; no
business-module imports):

- `solve_board_values()` — moved from `design_gen/generator.py`
- `format_ohms()` — moved from `agents/repair_planner.py`
- `resistor_mpn()` — moved from `agents/repair_planner.py`

All four consumers (repair planner, generator, creator crew, MCP backend)
import from `circuit_math`. No compatibility re-exports; import sites are
updated in the same commit.

### 3. Python — typed protocols

New leaf module `ratsnest/protocols.py` with two `typing.Protocol` classes
that formalize seams that already exist implicitly:

- **`LlmBrain`** — `available: bool` (property) and
  `complete_json(agent, system, user, ...)`. The five brain seams
  (requirement agent, creator foreman, repair reasoner, evolution proposer,
  orchestrator loop) type-hint parameters against `LlmBrain` instead of the
  concrete `LlmClient`. Tests already inject a FakeLlm; the protocol makes
  the contract explicit and checkable.
- **`DesignBackend`** — `generate(spec: DesignSpec, out_dir: Path,
  strategy: StrategyBundle)`. `CreatorCrew` and `KiCadMcpBackend` already
  conform. A thin new `TemplateBackend` class wraps `generate_project()` so
  all three backends are uniform.

`pipeline.generate_for_backend()` replaces its `if/elif` chain with a
registry of lazy backend factories:
`{"template": ..., "crew": ..., "mcp": ...}`. Adding a backend becomes a
one-line registration. Lazy imports are preserved (factories import on call).

Protocols are static-typing contracts only — zero runtime behavior change.

### 4. Java backend — deduplicate policy and bridge

- **`security/RunAccessPolicy`** (`@Component`): the single implementation of
  the owner/admin/service access check currently duplicated between
  `RunController.currentIsAdmin()`-style logic and `EdaController.canTouch()`.
  Both controllers inject it.
- **`core/PythonBridge`** (`@Service`): the single owner of "invoke
  `python -m ratsnest ...` with the configured `ratsnest.python-exe` and
  `ratsnest.agent-runtime-dir`." One blocking method covers both call sites:
  `BridgeResult run(List<String> args, Duration timeout, Map<String, String> extraEnv)`
  where `BridgeResult(boolean finished, String stdout, String stderr)`. The
  bridge prepends `pythonExe -m ratsnest`, sets the working directory, and
  applies the env overlay. `EdaController` calls it with no env and a 3-minute
  timeout; `RunDispatchService.dispatchLocal()` passes
  `RATSNEST_CONTROL_PLANE_URL`/`RATSNEST_SERVICE_TOKEN` and 15 minutes.
  Kafka dispatch and `applyResult()` are untouched.

### 5. Frontend — decompose App.tsx

`frontend/src/components/` gains `AuthBar.tsx`, `TimelinePanel.tsx`,
`PreviewPanel.tsx`, `ReportPanel.tsx`, `EdaPanel.tsx`, and `RunList.tsx` /
`NewRunForm.tsx` if they extract cleanly. `App.tsx` becomes the composition
root: state ownership, panel switching, layout. Data and callbacks flow via
props; all HTTP stays in `lib/api.ts`; shared types stay in `lib/runData.ts`.
Pure extraction — no behavior or styling changes. The rebuilt Vite bundle is
copied into `backend/src/main/resources/static/` as before.

### 6. Config — `.env` file for model and API key

There is currently no file where a user can put their chosen provider, model,
and API key; `Config.load()` reads only process environment variables.

- `Config.load()` gains `.env` support: if `<repo-root>/.env` exists, parse it
  (`KEY=VALUE` lines, `#` comments and blanks ignored — a ~15-line parser, no
  new dependency) and use its values as *defaults*. Real environment variables
  always take precedence, so docker-compose and Java-backend-injected vars are
  never overridden. Because the Python runtime reads the file itself, it works
  identically from the CLI, the Spring subprocess bridge, and the Kafka worker.
- New `.env.example` at repo root documenting every `RATSNEST_*` setting with
  a commented block per provider (anthropic, openai, deepseek, qwen, moonshot,
  zhipu, ollama): provider, model, API key, base URL, and the
  `RATSNEST_LLM=off|auto|require` mode.
- `.env` is added to `.gitignore` so keys are never committed.

## Error handling

Unchanged. `BrainRequiredError` still raised from `LlmClient` under
`RATSNEST_LLM=require`; deterministic fallbacks still engage under `auto`;
HTTP 404-for-unauthorized policy stays.

## Testing and verification

1. `pytest` — all 43 existing tests green, plus new tests: `TemplateBackend`
   satisfies `DesignBackend` structurally; `circuit_math` functions preserve
   behavior at their new home.
2. New test: `.env` parsing (values load, environment wins, missing file is
   a no-op).
3. Frontend — `npm run build` (strict tsc) and the existing vitest suite.
3. Backend — `mvn package` with JAVA_HOME set to Eclipse Adoptium JDK 17.
4. End-to-end smoke — `python -m ratsnest design --backend template` produces
   the same deliverables (project files, previews, report, release zip).

## Delivery

One commit per section, in order (1) kicad_host, (2) circuit_math,
(3) protocols + registry, (4) Java dedup, (5) frontend decomposition,
(6) .env config support — each independently revertable.

## Non-goals

- No package re-layering of the runtime root (`llm.py`, `pipeline.py`, etc.
  stay put) — rejected as import churn without cohesion gain (Approach B).
- No new runtime abstractions beyond the two protocols.
- No behavior, API, or schema changes.

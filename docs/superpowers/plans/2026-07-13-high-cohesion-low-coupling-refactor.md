# High Cohesion / Low Coupling Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every identified coupling defect in RatsNest (shared KiCad host trapped in a crew, scattered circuit math, implicit LLM/backend seams, duplicated Java policy/bridge, monolithic App.tsx, undocumented config) without changing any behavior.

**Architecture:** RatsNest is an enterprise (ToB) AHE multi-agent system: the LLM is the brain of every agent (proposes at 5 typed-contract seams), deterministic tools execute, checkers verify, AHE evolves strategies, and the Spring control plane governs. This refactor formalizes those seams as `typing.Protocol` contracts (`LlmBrain`, `DesignBackend`), extracts shared infrastructure to leaf modules, and deduplicates the Java control plane — strengthening the brain-first architecture, never weakening it.

**Tech Stack:** Python 3.11 (pytest, Pydantic), Java 17 / Spring Boot (Maven), React + TypeScript + Vite (vitest).

**Spec:** `docs/superpowers/specs/2026-07-13-high-cohesion-low-coupling-refactor-design.md`

**Environment notes:**
- Run all commands from `<repo-root>` unless stated.
- Python tests: `cd agent-runtime` then `..\.venv\Scripts\python.exe -m pytest tests -q` (venv shares KiCad's interpreter). If `.venv` is elsewhere, use the interpreter the project already uses — check `agent-runtime/.venv` first, then repo-root `.venv`.
- Java build needs Java 17 and `JAVA_HOME` set to that JDK.
- Never spawn visible console windows: all Python `subprocess.run` calls already pass `creationflags=NO_WINDOW` — preserve that in any code you touch.
- Console is GBK: never print non-ASCII in test output.

---

## File Structure (what exists after this plan)

```
agent-runtime/ratsnest/
  kicad_host.py        NEW  — in-process KiCADInterface singleton (leaf; was private in crews/creator.py)
  circuit_math.py      NEW  — ALL circuit-domain math + GenerationError (leaf; was split across agents/ and design_gen/)
  protocols.py         NEW  — LlmBrain + DesignBackend typing.Protocol contracts (leaf)
  config.py            MOD  — gains .env loading (env vars always win)
  pipeline.py          MOD  — backend registry replaces if/elif dispatch
  eda.py               MOD  — imports kicad_host, not crews.creator._get_host
  crews/creator.py     MOD  — host code removed; circuit math imported
  agents/repair_planner.py MOD — circuit math imported, not defined
  design_gen/generator.py  MOD — solver code removed; gains TemplateBackend
  mcp_exec/kicad_backend.py MOD — imports updated
  design_gen/requirement_agent.py, evolution/proposer.py MOD — LlmBrain type hints
agent-runtime/tests/
  test_kicad_host.py   NEW
  test_circuit_math.py NEW
  test_protocols.py    NEW
  test_config.py       NEW
  test_design_gen.py   MOD  — one import line
backend/src/main/java/dev/ratsnest/
  security/RunAccessPolicy.java NEW — single ownership policy
  core/PythonBridge.java        NEW — single python -m ratsnest invoker
  api/RunController.java        MOD — uses RunAccessPolicy
  api/EdaController.java        MOD — uses RunAccessPolicy + PythonBridge
  core/RunDispatchService.java  MOD — uses PythonBridge
frontend/src/
  components/runShared.tsx    NEW — StatusBadge, isTerminal, Metric, stepLabel
  components/PreviewPanel.tsx NEW
  components/TimelinePanel.tsx NEW
  components/ReportPanel.tsx  NEW
  components/EdaPanel.tsx     NEW
  components/ConsoleSection.tsx NEW
  components/RunDetail.tsx    NEW
  components/AuthBar.tsx      NEW
  components/landing.tsx      NEW — Hero, SystemSection, FeaturesSection + animation helpers
  App.tsx                     MOD — composition root only
.env.example           NEW
.gitignore             MOD  — + .env
```

---

### Task 1: Extract the in-process KiCad host to `kicad_host.py`

The host singleton (`_host` / `_get_host`) currently lives in `crews/creator.py:34-54`; `eda.py` imports the private `_get_host` across module boundaries. Move it to a leaf module with its own error type; the creator crew translates that error into its own `AgentError` so crew error handling is unchanged.

**Files:**
- Create: `agent-runtime/ratsnest/kicad_host.py`
- Create: `agent-runtime/tests/test_kicad_host.py`
- Modify: `agent-runtime/ratsnest/crews/creator.py` (remove lines 34-54; adjust `call()` at line 70)
- Modify: `agent-runtime/ratsnest/eda.py` (the `get_host` closure, lines 67-73)

- [ ] **Step 1.1: Write the failing test**

Create `agent-runtime/tests/test_kicad_host.py`:

```python
"""kicad_host: the shared in-process KiCADInterface singleton."""

import pytest

from ratsnest import kicad_host
from ratsnest.config import Config


def test_get_host_raises_when_pcbnew_unavailable(monkeypatch):
    monkeypatch.setattr(kicad_host, "_host", None)
    monkeypatch.setattr(kicad_host, "bootstrap_kicad", lambda p: False)
    with pytest.raises(kicad_host.KicadHostError, match="pcbnew unavailable"):
        kicad_host.get_host(Config.load())


def test_get_host_raises_without_mcp_server_dir(monkeypatch):
    monkeypatch.setattr(kicad_host, "_host", None)
    monkeypatch.setattr(kicad_host, "bootstrap_kicad", lambda p: True)
    config = Config.load()
    config.mcp_server_dir = None
    with pytest.raises(kicad_host.KicadHostError, match="MCP-Server dir"):
        kicad_host.get_host(config)
```

- [ ] **Step 1.2: Run it to verify it fails**

Run (from `agent-runtime/`): `python -m pytest tests/test_kicad_host.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ratsnest.kicad_host'` (or ImportError).

- [ ] **Step 1.3: Create `agent-runtime/ratsnest/kicad_host.py`**

```python
"""Shared in-process KiCad host — the ONE place the vendored
KiCADInterface (from KiCAD-MCP-Server) is bootstrapped and held.

Consumers: the creator crew's skill agents and the Web-EDA engine. Both
execute KiCad writes through this host so there is a single trusted write
path; neither owns the infrastructure.
"""

from __future__ import annotations

import sys

from ratsnest.config import Config
from ratsnest.kicad_env import bootstrap_kicad


class KicadHostError(RuntimeError):
    """The in-process host could not be bootstrapped."""


_host = None


def get_host(config: Config):
    """Singleton in-process KiCADInterface from the vendored server."""
    global _host
    if _host is not None:
        return _host
    if not bootstrap_kicad(config.kicad_python):
        raise KicadHostError(
            "pcbnew unavailable — cannot host KiCad skills in-process")
    if not config.mcp_server_dir:
        raise KicadHostError(
            "KiCAD-MCP-Server dir not found (RATSNEST_MCP_SERVER)")
    py_dir = str(config.mcp_server_dir / "python")
    if py_dir not in sys.path:
        sys.path.insert(0, py_dir)
    import importlib
    module = importlib.import_module("kicad_interface")
    _host = module.KiCADInterface()
    from ratsnest.mcp_exec.hotfixes import apply_hotfixes
    apply_hotfixes(_host, config)
    return _host
```

(This is the exact body of `crews/creator.py:37-54` with `AgentError` → `KicadHostError` and the `bootstrap_kicad` import moved here.)

- [ ] **Step 1.4: Run the new tests — expect PASS**

Run: `python -m pytest tests/test_kicad_host.py -q`
Expected: `2 passed`.

- [ ] **Step 1.5: Update `crews/creator.py`**

Delete lines 34-54 (`_host = None` through the end of `_get_host`). Then in `KiCadSkillAgent.call()` replace:

```python
        host = _get_host(self.config)
```

with:

```python
        try:
            host = get_host(self.config)
        except KicadHostError as exc:
            raise AgentError(str(exc)) from exc
```

Add to the imports at the top of `creator.py`:

```python
from ratsnest.kicad_host import KicadHostError, get_host
```

Remove `from ratsnest.kicad_env import bootstrap_kicad` from creator.py **only if** `bootstrap_kicad` has no other uses in the file (grep for it first — if the crew calls it elsewhere, keep the import). Also search creator.py for any other `_get_host(` call sites (e.g. inside `generate()` or `_snapshot()`) and replace them the same way — `grep -n "_get_host" creator.py` must return nothing when done.

- [ ] **Step 1.6: Update `eda.py`**

Replace the closure (lines 67-73):

```python
    def get_host():
        nonlocal host
        if host is None:
            from ratsnest.crews.creator import _get_host
            host = _get_host(config)
        return host
```

with:

```python
    def get_host():
        nonlocal host
        if host is None:
            from ratsnest.kicad_host import get_host as _host_factory
            host = _host_factory(config)
        return host
```

- [ ] **Step 1.7: Full test suite**

Run: `python -m pytest tests -q`
Expected: all pass (43 existing + 2 new). If a creator-crew test fails on the error-wrapping change, check that `get_host` failures still surface as `AgentError` inside `call()`.

- [ ] **Step 1.8: Commit**

```bash
git add agent-runtime/ratsnest/kicad_host.py agent-runtime/ratsnest/crews/creator.py agent-runtime/ratsnest/eda.py agent-runtime/tests/test_kicad_host.py
git commit -m "refactor: extract shared in-process KiCad host to kicad_host.py"
```

---

### Task 2: Consolidate circuit-domain math in `circuit_math.py`

`format_ohms`/`resistor_mpn` live in `agents/repair_planner.py:26-61`; `GenerationError`, `_snap`, `_vref_for`, `pick_divider`, `solve_board_values` live in `design_gen/generator.py:20-123`; `_snap` is duplicated in both files. One leaf module gets them all. **The docstring must state the AHE invariant:** generation and repair share one evolvable knowledge base, so an AHE strategy promotion improves both paths at once.

**Files:**
- Create: `agent-runtime/ratsnest/circuit_math.py`
- Create: `agent-runtime/tests/test_circuit_math.py`
- Modify: `agent-runtime/ratsnest/agents/repair_planner.py`
- Modify: `agent-runtime/ratsnest/design_gen/generator.py`
- Modify: `agent-runtime/ratsnest/crews/creator.py:23,31`
- Modify: `agent-runtime/ratsnest/mcp_exec/kicad_backend.py:21`
- Modify: `agent-runtime/tests/test_design_gen.py:8`

- [ ] **Step 2.1: Write the failing test**

Create `agent-runtime/tests/test_circuit_math.py`:

```python
"""circuit_math: the one home for circuit-domain solving (AHE-governed)."""

import pytest

from ratsnest.circuit_math import (
    GenerationError,
    format_ohms,
    pick_divider,
    resistor_mpn,
)
from ratsnest.config import Config
from ratsnest.schemas import StrategyBundle


def test_format_ohms_docstring_examples():
    assert format_ohms(3000) == "3k"
    assert format_ohms(4700) == "4.7k"
    assert format_ohms(330) == "330"
    assert format_ohms(1_500_000) == "1.5M"


def test_resistor_mpn_map_hit_then_pattern():
    strategy = StrategyBundle.model_construct(solver_params={
        "mpn_map": {"3k": "EXPLICIT-3K"},
        "resistor_mpn_pattern": "RC0805FR-07{code}L",
    })
    assert resistor_mpn(strategy, "3k") == "EXPLICIT-3K"
    assert resistor_mpn(strategy, "1.6k") == "RC0805FR-071K6L"
    assert resistor_mpn(strategy, "330") == "RC0805FR-07330RL"


def test_pick_divider_raises_outside_tolerance():
    config = Config.load()
    with pytest.raises(GenerationError, match="divider"):
        pick_divider(config, target=0.5, vref=1.25, tolerance_pct=2.0)
```

(`pick_divider` with target < vref can never work: every `ideal_top <= 0` candidate is skipped, so `best is None` and it raises. `Config.load()` is how existing tests obtain kicad-happy paths.)

- [ ] **Step 2.2: Run it to verify it fails**

Run: `python -m pytest tests/test_circuit_math.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ratsnest.circuit_math'`.

- [ ] **Step 2.3: Create `agent-runtime/ratsnest/circuit_math.py`**

Compose from the verbatim bodies already in the codebase (sources noted per block — copy from the file, do not retype):

```python
"""Circuit-domain math: values, dividers, MPNs — the ONE evolvable knowledge
base shared by design generation (all backends) AND the repair loop.

AHE invariant: every electrical value is solved from the SAME strategy
assets (Vref table, E-series snapping, MPN patterns, LED Vf), so when the
Evolution Agent promotes a strategy, generation and repair improve together.
E-series snapping reuses kicad-happy's kicad_utils (unforked).
"""

from __future__ import annotations

from ratsnest.config import Config
from ratsnest.khlib import load_kh_module
from ratsnest.schemas import DesignSpec, StrategyBundle

REGULATOR_PART = "AP1117-ADJ"  # board family v1: one adjustable LDO
LED_I_TARGET_A = 0.010

# <copy DEFAULT_LED_VF and DEFAULT_LED_MPN verbatim from design_gen/generator.py:23-27>


class GenerationError(ValueError):
    pass


def snap_e_series(config: Config, ideal: float, series: str = "E24") -> float:
    """Snap to the nearest E-series value via kicad-happy's kicad_utils."""
    utils = load_kh_module("kicad_utils", config.kicad_scripts)
    snapped, _ = utils.snap_to_e_series(ideal, series)
    return float(snapped)


# <copy format_ohms verbatim from agents/repair_planner.py:26-34>
# <copy resistor_mpn verbatim from agents/repair_planner.py:43-61>
# <copy _vref_for verbatim from design_gen/generator.py:40-44>
# <copy pick_divider verbatim from design_gen/generator.py:47-65,
#  replacing the `_snap(config, ideal_top)` call with
#  `snap_e_series(config, ideal_top)`>
# <copy solve_board_values verbatim from design_gen/generator.py:73-123,
#  replacing `_snap(` with `snap_e_series(` (2 sites) and
#  `_resistor_mpn(` with `resistor_mpn(` (4 sites)>
```

- [ ] **Step 2.4: Run the new tests — expect PASS**

Run: `python -m pytest tests/test_circuit_math.py -q`
Expected: `3 passed`.

- [ ] **Step 2.5: Slim `agents/repair_planner.py`**

Delete `format_ohms` (26-34), `_snap` (37-40), `resistor_mpn` (43-61). Add import:

```python
from ratsnest.circuit_math import format_ohms, resistor_mpn, snap_e_series
```

Replace the two remaining `_snap(` call sites (near lines 80 and 109 pre-edit — `grep -n "_snap(" repair_planner.py`) with `snap_e_series(`. Note the old local `_snap` had no default for `series`, so every existing call passes it explicitly — signatures are compatible. Remove `load_kh_module` from the imports **only if** nothing else in the file uses it (grep first — the solvers may).

- [ ] **Step 2.6: Slim `design_gen/generator.py`**

Delete from generator.py: `REGULATOR_PART`, `LED_I_TARGET_A`, `DEFAULT_LED_VF`, `DEFAULT_LED_MPN`, `GenerationError`, `_snap`, `_vref_for`, `pick_divider`, `_resistor_mpn`, `solve_board_values`, and the line-14 import from `agents.repair_planner`. Replace the header imports so the file reads:

```python
from __future__ import annotations

import json
from pathlib import Path

from ratsnest.circuit_math import GenerationError, solve_board_values
from ratsnest.config import Config
from ratsnest.design_gen.templates import build_regulator_board, rail_name
from ratsnest.schemas import DesignSpec, StrategyBundle
```

`generate_project()` (lines 126-148) stays byte-identical. `GenerationError` is re-imported here only because Task 3 adds `TemplateBackend` to this file and `generate_project`'s callers… actually it is needed because `solve_board_values` raises it through this module — keep the import; flake8 will flag it if truly unused, in which case import only `solve_board_values` and update `tests/test_design_gen.py` accordingly.

- [ ] **Step 2.7: Update the remaining import sites**

- `crews/creator.py:23` → `from ratsnest.circuit_math import GenerationError, solve_board_values`
- `crews/creator.py:31-32`: replace the local `REGULATOR_PART = "AP1117-ADJ"` with `from ratsnest.circuit_math import REGULATOR_PART` (add to the import line above); keep `REGULATOR_SYMBOL = "Regulator_Linear:AP1117-ADJ"` as-is (KiCad-library identity is creator knowledge).
- `mcp_exec/kicad_backend.py:21` → `from ratsnest.circuit_math import GenerationError, solve_board_values`
- `tests/test_design_gen.py:8` → `from ratsnest.circuit_math import GenerationError`

Then verify no stale references: `grep -rn "repair_planner import format_ohms\|generator import GenerationError\|generator import.*solve_board_values" ratsnest tests` → no hits.

- [ ] **Step 2.8: Full test suite**

Run: `python -m pytest tests -q`
Expected: all pass.

- [ ] **Step 2.9: Commit**

```bash
git add -A agent-runtime
git commit -m "refactor: consolidate circuit-domain math in circuit_math.py (one AHE knowledge base)"
```

---

### Task 3: `LlmBrain` + `DesignBackend` protocols and the backend registry

Formalize the two implicit seams. Every agent's brain contract (`available` + `complete_json`) becomes the `LlmBrain` protocol; the three generation backends become uniform `DesignBackend` implementations dispatched through a registry.

**Files:**
- Create: `agent-runtime/ratsnest/protocols.py`
- Create: `agent-runtime/tests/test_protocols.py`
- Modify: `agent-runtime/ratsnest/design_gen/generator.py` (add `TemplateBackend`)
- Modify: `agent-runtime/ratsnest/pipeline.py` (registry)
- Modify: type hints in `design_gen/requirement_agent.py:34`, `agents/repair_planner.py:220`, `crews/creator.py:124`, `evolution/proposer.py` (llm param)

- [ ] **Step 3.1: Write the failing test**

Create `agent-runtime/tests/test_protocols.py`:

```python
"""The two typed seams of the AHE system: brains propose, backends build."""

from pathlib import Path

import pytest

from ratsnest.config import Config
from ratsnest.protocols import DesignBackend, LlmBrain


class FakeBrain:
    @property
    def available(self) -> bool:
        return True

    def complete_json(self, agent, system, user, max_tokens=2000):
        return {}


def test_fake_and_real_brains_satisfy_llm_brain():
    from ratsnest.llm import LlmClient
    assert isinstance(FakeBrain(), LlmBrain)
    assert isinstance(LlmClient(Config.load()), LlmBrain)


def test_all_three_backends_satisfy_design_backend():
    from ratsnest.crews import CreatorCrew
    from ratsnest.design_gen.generator import TemplateBackend
    from ratsnest.mcp_exec import KiCadMcpBackend
    config = Config.load()
    assert isinstance(TemplateBackend(config), DesignBackend)
    assert isinstance(CreatorCrew(config, None), DesignBackend)
    assert isinstance(KiCadMcpBackend(config, None), DesignBackend)


def test_registry_rejects_unknown_backend(tmp_path):
    from ratsnest.pipeline import generate_for_backend
    from ratsnest.schemas import StrategyBundle
    strategy = StrategyBundle.model_construct(solver_params={})
    with pytest.raises(ValueError, match="backend must be one of"):
        generate_for_backend("5V to 3.3V", tmp_path, "quantum",
                            strategy, Config.load())
```

- [ ] **Step 3.2: Run it to verify it fails**

Run: `python -m pytest tests/test_protocols.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ratsnest.protocols'`.

- [ ] **Step 3.3: Create `agent-runtime/ratsnest/protocols.py`**

```python
"""Typed seams of the AHE multi-agent system.

Architecture invariant: the LLM PROPOSES, tools EXECUTE, checkers VERIFY,
AHE evolves, the control plane governs. These protocols are the load-bearing
contracts of that sentence:

  LlmBrain       what every agent seam needs from its brain — the five
                 brain seams (requirement agent, creator foreman, repair
                 reasoner, evolution proposer, orchestrator) depend on this
                 contract, never on a concrete client.
  DesignBackend  a thing that turns a DesignSpec into a KiCad project
                 (template writer, creator crew, MCP executor).

Protocols are structural: `LlmClient` and every test FakeLlm satisfy
LlmBrain without inheriting anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ratsnest.schemas import DesignSpec, StrategyBundle


@runtime_checkable
class LlmBrain(Protocol):
    """One brain invocation: JSON contract in, parsed JSON (or None) out."""

    @property
    def available(self) -> bool: ...

    def complete_json(self, agent: str, system: str, user: str,
                      max_tokens: int = 2000) -> dict[str, Any] | None: ...


@runtime_checkable
class DesignBackend(Protocol):
    """DesignSpec -> KiCad project on disk, governed by the strategy."""

    def generate(self, spec: DesignSpec, out_dir: Path,
                 strategy: StrategyBundle): ...
```

- [ ] **Step 3.4: Add `TemplateBackend` to `design_gen/generator.py`** (append at end of file)

```python
class TemplateBackend:
    """DesignBackend adapter for the deterministic template writer."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config.load()

    def generate(self, spec: DesignSpec, out_dir: Path,
                 strategy: StrategyBundle) -> Path:
        return generate_project(spec, out_dir, strategy, self.config)
```

- [ ] **Step 3.5: Replace the dispatch chain in `pipeline.py`**

Replace lines 24 and 60-67 (`VALID_BACKENDS = ...` and the `if backend == "crew": ... elif ... else ...` block) with a registry. After the edit the relevant parts of `pipeline.py` read:

```python
from ratsnest.protocols import DesignBackend, LlmBrain


def _template_backend(config, recorder, llm) -> DesignBackend:
    from ratsnest.design_gen.generator import TemplateBackend
    return TemplateBackend(config)


def _crew_backend(config, recorder, llm) -> DesignBackend:
    from ratsnest.crews import CreatorCrew
    return CreatorCrew(config, recorder, llm=llm)


def _mcp_backend(config, recorder, llm) -> DesignBackend:
    from ratsnest.mcp_exec import KiCadMcpBackend
    return KiCadMcpBackend(config, recorder)


# registry: adding a backend = one entry here (imports stay lazy)
BACKEND_FACTORIES = {
    "template": _template_backend,
    "crew": _crew_backend,
    "mcp": _mcp_backend,
}
VALID_BACKENDS = tuple(BACKEND_FACTORIES)
```

and the dispatch inside `generate_for_backend` (signature gains `llm: LlmBrain | None = None`) becomes:

```python
    BACKEND_FACTORIES[backend](config, recorder, llm).generate(
        spec, out_dir, strategy)
    return spec
```

Keep the validation at the top exactly as-is (`if backend not in VALID_BACKENDS: raise ValueError(...)`) — the test in Step 3.1 asserts its message. Check `grep -rn "VALID_BACKENDS" ratsnest` — `cli.py` may import it; the name and shape (tuple of str) are preserved, so imports keep working.

- [ ] **Step 3.6: Type-hint the brain seams**

In each file, add `from ratsnest.protocols import LlmBrain` to the imports and change the parameter annotation (all files already have `from __future__ import annotations`):

- `design_gen/requirement_agent.py:34`: `def parse_requirement_llm(text: str, llm) -> DesignSpec | None:` → `def parse_requirement_llm(text: str, llm: LlmBrain) -> DesignSpec | None:`
- `agents/repair_planner.py:220` (the `llm=None` parameter of `plan_repairs`): → `llm: LlmBrain | None = None,`
- `crews/creator.py:124` (`CreatorCrew.__init__`'s `llm=None`): → `llm: LlmBrain | None = None`
- `evolution/proposer.py`: find the `llm` parameter of `propose_candidate` (`grep -n "llm" proposer.py`) and annotate it `LlmBrain | None` the same way.

- [ ] **Step 3.7: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_protocols.py -q` → `3 passed`.
Run: `python -m pytest tests -q` → all pass.

- [ ] **Step 3.8: Commit**

```bash
git add -A agent-runtime
git commit -m "refactor: LlmBrain + DesignBackend protocols; backend registry in pipeline"
```

---

### Task 4: Java — `RunAccessPolicy` + `PythonBridge`

**Files:**
- Create: `backend/src/main/java/dev/ratsnest/security/RunAccessPolicy.java`
- Create: `backend/src/main/java/dev/ratsnest/core/PythonBridge.java`
- Modify: `backend/src/main/java/dev/ratsnest/api/RunController.java` (delete helpers at lines 239-268, inject policy)
- Modify: `backend/src/main/java/dev/ratsnest/api/EdaController.java` (delete `canTouch` 61-71, subprocess code 83-97, inject both)
- Modify: `backend/src/main/java/dev/ratsnest/core/RunDispatchService.java` (`dispatchLocal` 96-142 uses the bridge; `pythonExe`/`agentRuntimeDir` fields removed)

- [ ] **Step 4.1: Create `RunAccessPolicy.java`**

```java
package dev.ratsnest.security;

import dev.ratsnest.core.DesignRun;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;

/**
 * The single ownership policy for design runs: open-mode rows (no owner)
 * are public; owned rows are visible to their owner, admins, and the
 * agent-runtime service identity. Controllers must not re-implement this.
 */
@Component
public class RunAccessPolicy {

    private static Authentication currentAuth() {
        return SecurityContextHolder.getContext().getAuthentication();
    }

    /** Logged-in username, or null for anonymous / service callers. */
    public String currentUser() {
        Authentication auth = currentAuth();
        if (auth == null || !auth.isAuthenticated()
                || "anonymousUser".equals(auth.getName())
                || "agent-runtime".equals(auth.getName())) {
            return null;
        }
        return auth.getName();
    }

    public boolean currentIsAdmin() {
        Authentication auth = currentAuth();
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> a.getAuthority().contains("ADMIN")
                        || a.getAuthority().contains("SERVICE"));
    }

    public boolean canAccess(DesignRun run) {
        if (run.getOwner() == null) {
            return true;                 // open mode / legacy rows
        }
        return currentIsAdmin() || run.getOwner().equals(currentUser());
    }
}
```

(Bodies moved verbatim from `RunController.java:241-268`. `EdaController.canTouch` is behaviorally identical for every reachable input — service callers pass via the SERVICE authority, anonymous callers get null-user + non-admin → false — so it is replaced by `canAccess`, not preserved.)

- [ ] **Step 4.2: Create `PythonBridge.java`**

```java
package dev.ratsnest.core;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * The single owner of "invoke the Python agent runtime": builds
 * `<python-exe> -m ratsnest <args>` in the configured runtime directory.
 * Both the Web-EDA bridge and local run dispatch go through here.
 */
@Service
public class PythonBridge {

    @Value("${ratsnest.python-exe:python}")
    private String pythonExe;

    @Value("${ratsnest.agent-runtime-dir:.}")
    private String agentRuntimeDir;

    public record BridgeResult(boolean finished, String stdout, String stderr) {}

    public BridgeResult run(List<String> args, Duration timeout,
                            Map<String, String> extraEnv)
            throws IOException, InterruptedException {
        List<String> cmd = new ArrayList<>(List.of(pythonExe, "-m", "ratsnest"));
        cmd.addAll(args);
        ProcessBuilder pb = new ProcessBuilder(cmd)
                .directory(new File(agentRuntimeDir))
                .redirectErrorStream(false);
        pb.environment().putAll(extraEnv);
        Process proc = pb.start();
        String stdout = new String(proc.getInputStream().readAllBytes(),
                StandardCharsets.UTF_8);
        String stderr = new String(proc.getErrorStream().readAllBytes(),
                StandardCharsets.UTF_8);
        boolean finished = proc.waitFor(timeout.toMillis(),
                TimeUnit.MILLISECONDS);
        return new BridgeResult(finished, stdout, stderr);
    }
}
```

- [ ] **Step 4.3: Rewire `RunController`**

Add constructor parameter + field `private final RunAccessPolicy access;` (import `dev.ratsnest.security.RunAccessPolicy`). Then:
- every `this::canAccess` → `access::canAccess`
- both `currentUser()` calls (lines 70, 92, 126) → `access.currentUser()`
- `currentIsAdmin()` (line 127) → `access.currentIsAdmin()`
- delete the four private helpers `currentAuth`, `currentUser`, `currentIsAdmin`, `canAccess` (lines 239-268) and the now-unused `Authentication` import.

- [ ] **Step 4.4: Rewire `EdaController`**

Constructor becomes `EdaController(DesignRunRepository runs, RunAccessPolicy access, PythonBridge bridge)`. Delete the `pythonExe`/`agentRuntimeDir` `@Value` fields, the `canTouch` method, and the `@Value`/`File`/`TimeUnit` imports. In `bridge(...)`:
- `if (run.getOwner() != null && !canTouch(run))` → `if (!access.canAccess(run))` (the owner-null check is inside `canAccess`)
- replace the ProcessBuilder block (lines 92-103) with:

```java
        try {
            PythonBridge.BridgeResult result = bridge.run(
                    cmd, Duration.ofMinutes(3), Map.of());
            if (result.stdout().isBlank()) {
                return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
                        .body("{\"error\":\"eda bridge produced no output\"}");
            }
            return ResponseEntity.ok()
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(result.stdout());
        } finally {
            if (opsFile != null) {
                Files.deleteIfExists(opsFile);
            }
        }
```

where `cmd` is now built WITHOUT the python prefix: `List<String> cmd = new java.util.ArrayList<>(List.of("eda", run.getProjectDir()));` (add imports `java.time.Duration`, `java.util.Map`, `dev.ratsnest.security.RunAccessPolicy`, `dev.ratsnest.core.PythonBridge`).

- [ ] **Step 4.5: Rewire `RunDispatchService.dispatchLocal`**

Inject `PythonBridge bridge` via the constructor. Delete the `pythonExe` and `agentRuntimeDir` fields. Build `cmd` without the python prefix (so line 100 becomes `cmd.addAll(List.of("design", run.getRequirement(), "--out", run.getProjectDir()));` and line 106 `cmd.addAll(List.of("fix", run.getProjectDir()));`). Build the env map:

```java
            Map<String, String> env = new java.util.HashMap<>();
            env.put("RATSNEST_CONTROL_PLANE_URL", selfUrl);
            if (serviceToken != null && !serviceToken.isBlank()) {
                env.put("RATSNEST_SERVICE_TOKEN", serviceToken);
            }
```

and replace lines 111-135 (ProcessBuilder through the finished/blank check) with:

```java
            run.setStatus("running");
            runs.save(run);

            PythonBridge.BridgeResult result =
                    bridge.run(cmd, Duration.ofMinutes(15), env);

            if (!result.finished() || result.stdout().isBlank()) {
                run.setStatus("failed");
                log.error("run {} produced no output; stderr: {}", run.getId(),
                        result.stderr().substring(0,
                                Math.min(500, result.stderr().length())));
            } else {
                applyResult(run, result.stdout());
            }
```

Kafka dispatch (`dispatchKafka`) and `applyResult` are untouched. Remove now-unused imports (`File`, `TimeUnit`, `StandardCharsets` if unused).

- [ ] **Step 4.6: Build**

```powershell
$env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-17.0.2.8-hotspot"
cd backend; mvn -q -DskipTests package
```

Expected: BUILD SUCCESS.

- [ ] **Step 4.7: Smoke the EDA + run endpoints against a live backend** (start the jar, login, GET /api/runs — same drill as previous sessions; confirm 200s and that an existing design run's `/eda` GET still returns state). If no runnable environment is available at execution time, note it in the commit body.

- [ ] **Step 4.8: Commit**

```bash
git add backend/src/main/java
git commit -m "refactor(backend): single RunAccessPolicy + PythonBridge, controllers deduplicated"
```

---

### Task 5: Frontend — decompose App.tsx (1,493 lines → composition root + components/)

Pure moves. Every component keeps its exact props, JSX, and styling. Strict `tsc` is the verifier: after each extraction, `npm run build` must pass; missing imports are enumerated by the compiler — copy the needed lines from App.tsx's import block (framer-motion, lucide-react icons, `./lib/api` functions, `./lib/runData` types are all already itemized there; adjust relative paths to `../lib/...`).

Current line map of `frontend/src/App.tsx` (pre-edit):
`primaryText`/`easeOut` 57-58 · WordsPullUp 60-109 · WordsPullUpMultiStyle 110-157 · AnimatedLetter 158-179 · ScrollRevealText 180-205 · `features` 206-236 · HealthPill 237-255 · Hero 256-331 · SystemSection 332-359 · FeaturesSection 360-460 · ConsoleSection 461-762 · StatusBadge 763-772 · isTerminal 773-781 · PreviewImage 782-801 · PreviewPanel 802-823 · SHEET_W/H 824-826 · EdaPanel 827-1053 · stepLabel 1054-1057 · TimelinePanel 1058-1143 · ReportPanel 1144-1179 · RunDetail 1180-1385 · Metric 1386-1394 · AuthBar 1395-1509 · App 1510-end.

- [ ] **Step 5.1: Extract the shared run helpers**

Create `frontend/src/components/runShared.tsx` containing (moved, with `export` added): `StatusBadge` (763-772), `isTerminal` (773-781), `Metric` (1386-1394), `stepLabel` (1054-1057). Add whatever imports tsc demands (likely `statusClassName` from `../lib/runData`).

- [ ] **Step 5.2: Extract the four result panels**

- `frontend/src/components/PreviewPanel.tsx`: `PreviewImage` (782-801, stays unexported) + `export function PreviewPanel` (802-823).
- `frontend/src/components/TimelinePanel.tsx`: `export function TimelinePanel` (1058-1143); imports `stepLabel` from `./runShared`.
- `frontend/src/components/ReportPanel.tsx`: `export function ReportPanel` (1144-1179); imports `isTerminal` from `./runShared`.
- `frontend/src/components/EdaPanel.tsx`: `SHEET_W`/`SHEET_H` (824-826) + `export function EdaPanel` (827-1053).

In App.tsx delete the moved code and add:

```tsx
import { PreviewPanel } from "./components/PreviewPanel";
import { TimelinePanel } from "./components/TimelinePanel";
import { ReportPanel } from "./components/ReportPanel";
import { EdaPanel } from "./components/EdaPanel";
import { StatusBadge, isTerminal, Metric, stepLabel } from "./components/runShared";
```

(then remove the ones App.tsx itself no longer references — tsc's `noUnusedLocals` will say which).

- [ ] **Step 5.3: Verify + commit**

Run (in `frontend/`): `npm run build` → clean. `npx vitest run` → existing tests pass.

```bash
git add frontend/src
git commit -m "refactor(frontend): extract run panels + shared run helpers from App.tsx"
```

- [ ] **Step 5.4: Extract the big sections**

- `frontend/src/components/RunDetail.tsx`: `export function RunDetail` (1180-1385); imports StatusBadge/Metric/isTerminal from `./runShared`, the four panels from their files.
- `frontend/src/components/ConsoleSection.tsx`: `export function ConsoleSection` (461-762); imports StatusBadge from `./runShared` (plus RunDetail if it renders it — follow tsc).
- `frontend/src/components/AuthBar.tsx`: `export function AuthBar` (1395-1509).
- `frontend/src/components/landing.tsx`: `primaryText`, `easeOut`, `WordsPullUp`, `WordsPullUpMultiStyle`, `AnimatedLetter`, `ScrollRevealText`, `features`, `HealthPill` (57-255, all unexported) + `export function Hero` (256-331), `export function SystemSection` (332-359), `export function FeaturesSection` (360-460).

- [ ] **Step 5.5: Slim App.tsx to the composition root**

App.tsx keeps only: the imports it still needs, `export default function App()` (1510-end) — now importing `Hero, SystemSection, FeaturesSection` from `./components/landing`, `ConsoleSection` from `./components/ConsoleSection`, `AuthBar` from `./components/AuthBar`, `RunDetail` from `./components/RunDetail` (if App renders it directly). All state App owns today stays in App; props flow unchanged. Target: App.tsx well under 300 lines, no component definitions besides `App`.

- [ ] **Step 5.6: Verify — build, tests, bundle refresh**

Run (in `frontend/`): `npm run build` → clean; `npx vitest run` → pass.
Refresh the served bundle (same flow as previous sessions):

```powershell
Remove-Item ..\backend\src\main\resources\static\assets\* -Force
Copy-Item dist\index.html ..\backend\src\main\resources\static\index.html -Force
Copy-Item dist\assets\* ..\backend\src\main\resources\static\assets\ -Force
```

- [ ] **Step 5.7: Commit**

```bash
git add frontend/src backend/src/main/resources/static
git commit -m "refactor(frontend): App.tsx becomes composition root; sections extracted to components/"
```

---

### Task 6: `.env` config file for provider / model / API key

Users currently have no file to put their chosen LLM provider, model, and key. `Config.load()` gains `.env` defaults (real environment always wins — docker-compose and the Java bridge inject vars that must not be overridden). Applying via `os.environ.setdefault` makes the values visible to every existing `os.environ.get` site (interceptor service token, worker Kafka settings) and to child processes — one mechanism, whole runtime.

**Files:**
- Modify: `agent-runtime/ratsnest/config.py`
- Create: `agent-runtime/tests/test_config.py`
- Create: `.env.example` (repo root)
- Modify: `.gitignore`

- [ ] **Step 6.1: Write the failing test**

Create `agent-runtime/tests/test_config.py`:

```python
"""Config: .env defaults load; real environment always wins."""

from ratsnest.config import _apply_dotenv


def test_dotenv_values_become_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("RATSNEST_TEST_ALPHA", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# comment line\n"
        "\n"
        "RATSNEST_TEST_ALPHA=from-file\n"
        "RATSNEST_TEST_QUOTED=\"quoted value\"\n"
        "not a valid line\n",
        encoding="utf-8")
    import os
    monkeypatch.delenv("RATSNEST_TEST_QUOTED", raising=False)
    _apply_dotenv(env)
    assert os.environ["RATSNEST_TEST_ALPHA"] == "from-file"
    assert os.environ["RATSNEST_TEST_QUOTED"] == "quoted value"
    monkeypatch.delenv("RATSNEST_TEST_ALPHA", raising=False)
    monkeypatch.delenv("RATSNEST_TEST_QUOTED", raising=False)


def test_real_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("RATSNEST_TEST_BETA", "from-env")
    env = tmp_path / ".env"
    env.write_text("RATSNEST_TEST_BETA=from-file\n", encoding="utf-8")
    _apply_dotenv(env)
    import os
    assert os.environ["RATSNEST_TEST_BETA"] == "from-env"


def test_missing_file_is_a_noop(tmp_path):
    _apply_dotenv(tmp_path / "nope.env")  # must not raise
```

- [ ] **Step 6.2: Run it to verify it fails**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name '_apply_dotenv'`.

- [ ] **Step 6.3: Implement in `config.py`**

Add after the `_DEFAULT_KICAD_CLI` line:

```python
def _apply_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE defaults from .env (repo root). Real environment
    variables always win — docker-compose / the control plane inject vars
    that must never be overridden. utf-8-sig tolerates PowerShell BOMs."""
    path = path or REPO_ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
```

Then make `Config.load()` call it first — insert as the first line of the method body:

```python
        _apply_dotenv()
```

- [ ] **Step 6.4: Run the tests — expect PASS, then full suite**

Run: `python -m pytest tests/test_config.py -q` → `3 passed`.
Run: `python -m pytest tests -q` → all pass.

- [ ] **Step 6.5: Create `.env.example` at repo root**

```bash
# RatsNest configuration — copy to .env and uncomment ONE provider block.
# Real environment variables always override these values.
# The LLM is the brain of every agent; without it agents fall back to
# their deterministic paths (RATSNEST_LLM=auto) or refuse (=require).

# -- brain mode: off | auto | require ---------------------------------
RATSNEST_LLM=auto

# -- Anthropic (default provider) -------------------------------------
#RATSNEST_LLM_PROVIDER=anthropic
#RATSNEST_LLM_API_KEY=sk-ant-...
#RATSNEST_LLM_MODEL=claude-sonnet-5

# -- OpenAI ------------------------------------------------------------
#RATSNEST_LLM_PROVIDER=openai
#RATSNEST_LLM_API_KEY=sk-...
#RATSNEST_LLM_MODEL=gpt-4o-mini

# -- DeepSeek ----------------------------------------------------------
#RATSNEST_LLM_PROVIDER=deepseek
#RATSNEST_LLM_API_KEY=sk-...
#RATSNEST_LLM_MODEL=deepseek-chat

# -- Qwen / DashScope ---------------------------------------------------
#RATSNEST_LLM_PROVIDER=qwen
#RATSNEST_LLM_API_KEY=sk-...
#RATSNEST_LLM_MODEL=qwen-plus

# -- Moonshot / Kimi ----------------------------------------------------
#RATSNEST_LLM_PROVIDER=moonshot
#RATSNEST_LLM_API_KEY=sk-...
#RATSNEST_LLM_MODEL=moonshot-v1-8k

# -- Zhipu / GLM --------------------------------------------------------
#RATSNEST_LLM_PROVIDER=zhipu
#RATSNEST_LLM_API_KEY=...
#RATSNEST_LLM_MODEL=glm-4-plus

# -- Ollama (local, no key) --------------------------------------------
#RATSNEST_LLM_PROVIDER=ollama
#RATSNEST_LLM_MODEL=llama3.1
#RATSNEST_LLM_BASE_URL=http://localhost:11434

# -- optional overrides -------------------------------------------------
#RATSNEST_LLM_BASE_URL=          # custom endpoint (any provider)
#RATSNEST_KICAD_CLI=/path/to/kicad-cli
```

- [ ] **Step 6.6: Add `.env` to `.gitignore`** (append the line `.env` to the repo-root `.gitignore`).

- [ ] **Step 6.7: Commit**

```bash
git add agent-runtime/ratsnest/config.py agent-runtime/tests/test_config.py .env.example .gitignore
git commit -m "feat: .env config file for LLM provider/model/key (env vars always win)"
```

---

### Task 7: End-to-end verification

- [ ] **Step 7.1: Full Python suite**: `python -m pytest tests -q` (from `agent-runtime/`) → 43 original + ~11 new, all pass.
- [ ] **Step 7.2: Frontend**: `npm run build` + `npx vitest run` (from `frontend/`) → clean.
- [ ] **Step 7.3: Backend**: `mvn -q -DskipTests package` (from `backend/`, JAVA_HOME set) → BUILD SUCCESS.
- [ ] **Step 7.4: E2E smoke** (from `agent-runtime/`): `python -m ratsnest design "5V input, 3.3V output, green LED indicator" --backend template --out ..\runs\refactor-smoke --max-iter 2 --no-erc` → completes; project dir contains `.kicad_sch`, `ratsnest_report.md`, previews; score reported as before the refactor.
- [ ] **Step 7.5: If anything regressed**, fix within the offending task's boundaries and amend nothing — add a fix commit.

---

## Self-review notes

- **Spec coverage:** spec §1→Task 1, §2→Task 2, §3→Task 3, §4→Task 4, §5→Task 5, §6→Task 6, verification section→Task 7. Complete.
- **Type consistency:** `get_host(config)` / `KicadHostError` used identically in Tasks 1; `snap_e_series` name consistent across Task 2 steps; `LlmBrain`/`DesignBackend`/`TemplateBackend`/`BACKEND_FACTORIES` consistent in Task 3; `BridgeResult(finished, stdout, stderr)` consistent in Task 4 steps.
- **Move-verbatim blocks** (Task 2/5) intentionally reference exact source line ranges instead of duplicating 400+ lines into this plan — the sources are in-repo and the verifier (pytest / strict tsc) mechanically enumerates any slip. This is the reliable procedure for pure-move refactors, not a placeholder.

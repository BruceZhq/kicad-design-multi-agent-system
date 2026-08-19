# Stage 2 - Controlled Autonomous Agent Workflow

Date: 2026-07-15
Status: Complete

## Required state machine

```text
planning
  -> awaiting_plan_approval
      -> plan_rejected (terminal, no KiCad project)
      -> queued -> running -> converged/escalated/failed
          -> release review -> approved/rejected
```

Fix runs retain their existing direct dispatch lifecycle. Design runs always
enter planning first.

## Immutable planning contract

`PlannedDesign` contains:

- contract version;
- original requirement and selected backend;
- validated DesignSpec;
- validated BoardPlan;
- exact strategy name and content hash;
- trajectory continuation metadata and creation time.

The control plane stores the exact JSON bytes and computes SHA-256 itself. A
BoardPlan approval is bound to that hash. Execution receives the same bytes and
hash and rejects any mismatch, unsupported contract, changed requirement,
backend, or strategy.

## Execution boundary

Planning may write trajectory data but must not create the requested KiCad
project directory. Only an approved plan can enqueue execution. Dispatch and
the Python runtime both independently enforce the approval/hash boundary.

During execution:

1. SchematicDesigner and PcbDesigner observe file-derived state.
2. The LLM or deterministic recovery planner proposes typed AgentPlan objects.
3. Contract validation checks capability, arguments, electrical intent,
   geometry, ordering, and budgets.
4. Capability-scoped Tool Services execute approved ToolCall objects.
5. The Blackboard checkpoints state and checkers publish typed findings.
6. RepairAgent assigns bounded work or escalates it to a human.

## Brain policy

- Provider and per-agent model routing are configuration, never user prompt data.
- DeepSeek uses its OpenAI-compatible protocol adapter.
- Per-call timeout, transient retry, call count, output-token, and total-token
  budgets are bounded for each run.
- `RATSNEST_LLM=auto` may use deterministic recovery after a recorded failure.
- `RATSNEST_LLM=require` fails closed and never executes deterministic fallback
  actions after a missing, malformed, over-budget, or unsafe brain response.

## Acceptance tests

- Creating a design produces a reviewable plan and no KiCad files.
- Execution cannot be called before a matching BoardPlan approval.
- Rejected and tampered plans never dispatch.
- Duplicate planning and result callbacks are idempotent and first-write wins.
- Approved execution uses the exact stored plan and strategy version.
- A partial DesignState can be resumed without replaying completed ToolCalls.
- Every LLM proposal, validation, tool action, finding, repair assignment, and
  approval transition is represented in the audit/trajectory surface.
- Python, frontend, backend, migration, and real Crew smoke tests pass.

## Completion evidence

Verified on 2026-07-15:

- 72 Python tests passed, including plan-byte tamper rejection, LLM budgets,
  Blackboard checkpoint restore, and non-replaying Crew resume.
- 7 frontend tests and the strict TypeScript production build passed.
- The complete Maven test/package build passed against fresh Flyway V1/V2
  migrations.
- A real Crew run stayed file-free before approval, then converged from the
  approved six-component plan and produced schematic/PCB previews, a 352,192
  byte checksummed project artifact, a release review, and an approved download.
- Result state is row-locked and committed before artifact/release work, so an
  interrupted dispatcher cannot expose a release approval while the run still
  appears to be executing; identical callbacks resume the remaining steps.

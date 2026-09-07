"""Narrow adapter between the repair engine and the existing EDA pipeline.

This is the only repair module that knows pipeline State or its private CAD
validators. The model, session engine and container broker do not own gates.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from ratsnestpro.agents.llm import LlmError

from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.orchestration.engineering_workspace import (
    EngineeringRequests,
    EngineeringWorkspace,
)
from ratsnestpro.repair.contracts import RepairLimits, SandboxFile, SandboxRequest, SandboxResult
from ratsnestpro.repair.pcb_contract import immutable_signature
from ratsnestpro.repair.session import CandidateAssessment, run_session


@dataclass
class StrongRepairRuntime:
    model: str
    reasoning_effort: str
    complete: Callable[[str, str, list[str], float], str]
    limits: RepairLimits


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


class _BoardHost:
    def __init__(self, state, ctx, record):
        from ratsnestpro.orchestration import pipeline as p

        self.p, self.state, self.ctx, self.record = p, state, ctx, record
        self.live = Path(state.artifact(p.PipelineStep.LAYOUT_WRITE).pcb_path)
        self.live_fingerprint = _digest(self.live)
        base = Path(tempfile.mkdtemp(prefix="candidate-", dir=self.live.parent / ".strong-repair"))
        self.root = base / "runs" / self.live.parent.name
        self.root.mkdir(parents=True)
        self.pcb = self.root / self.live.name
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".placement_constraints.json"):
            source = self.live.with_suffix(suffix)
            if source.is_file():
                shutil.copy2(source, self.pcb.with_suffix(suffix))
        for name in ("fp-lib-table", "sym-lib-table"):
            source = self.live.parent / name
            if source.is_file():
                shutil.copy2(source, self.root / name)
        libraries = self.live.parent / ".ratsnest-libs"
        if libraries.is_dir():
            if any(f.is_symlink() for f in libraries.rglob("*")):
                raise ValueError("repair candidate libraries must not contain symlinks")
            shutil.copytree(libraries, self.root / ".ratsnest-libs")
        receipt = (
            self.live.parent.parent.parent
            / "engineering-approvals"
            / (self.live.parent.name + ".fanout.json")
        )
        if receipt.is_file():
            (base / "engineering-approvals").mkdir()
            shutil.copy2(receipt, base / "engineering-approvals" / receipt.name)
        self.best = self.root / "best.pcb.snapshot"
        self.checkpoint_candidate()
        self.contract = immutable_signature(PcbBoard.load(self.pcb))
        self.view_state = copy.copy(state)
        self.view_state.artifacts = dict(state.artifacts)
        write = state.artifact(p.PipelineStep.LAYOUT_WRITE)
        self.view_state.artifacts[p.PipelineStep.LAYOUT_WRITE] = write.model_copy(
            update={"pcb_path": str(self.pcb)}
        )
        self.workspace = EngineeringWorkspace(
            out_dir=str(self.root),
            step="route_signals",
            artifacts=lambda: {
                key.value: value.model_dump(mode="json")
                for key, value in self.view_state.artifacts.items()
            },
        )
        self.report = self.pcb.with_suffix(".trusted.drc.json")
        self.last = None
        self.gap_nets = set()

    def assess(self):
        p = self.p
        if self.last is not None and self.last.fingerprint == _digest(self.pcb):
            return self.last
        if not p._refill_copper_zones(self.pcb):
            raise RuntimeError("trusted zone refill failed; candidate cannot be graded")
        snapshot = p._run_kicad_drc_snapshot(p.kicad_cli_available(), self.pcb, self.report)
        if snapshot.parse_error:
            raise RuntimeError("fresh KiCad DRC report unavailable")
        self.gap_nets = {
            endpoint.net for gap in snapshot.gaps for endpoint in (gap.left, gap.right)
        }
        board = PcbBoard.load(self.pcb)
        violations = []
        if immutable_signature(board) != self.contract:
            violations.append(
                "component/pad/net identity, outline, courtyard or board rules changed"
            )
        violations.extend(
            str(f)
            for f in p.audit_pcb_invariants(
                p.extract_requirement_invariants(self.state.requirement_text),
                board,
                self.state.artifact(p.PipelineStep.SELECTION).parts,
            )
        )
        violations.extend(p._net_class_geometry_blockers(self.view_state))
        from ratsnestpro.orchestration.placement_constraints import review_pcb_placement_constraints

        placement = review_pcb_placement_constraints(self.pcb)
        violations.extend(placement.violations)
        if placement.error:
            violations.append(placement.error)
        # No need to inspect filesystem state again when the candidate hasn't
        # changed; the next assessment always obtains fresh authoritative DRC.
        warnings = p._kicad_warning_findings(self.report)
        self.last = CandidateAssessment(
            _digest(self.pcb),
            len(snapshot.non_connectivity_errors),
            snapshot.unconnected,
            len(warnings),
            tuple(violations),
        )
        return self.last

    def observe(self):
        from ratsnestpro.repair.routability import probe

        board = PcbBoard.load(self.pcb)
        p = self.p
        obstacles = {}

        def clear(net, a, b, layer, width):
            if (net, layer) not in obstacles:
                obstacles[(net, layer)] = p._copper_obstacles(board, net_name=net, layer=layer)
            items = obstacles[(net, layer)]
            return all(
                p._segment_distance(a, b, o.start, o.end) >= o.radius + width / 2 + 0.2
                for o in items
            )

        data = json.loads(self.report.read_text()) if self.report.is_file() else {}
        geometry = probe(board, clear_segment=clear)
        from ratsnestpro.repair.joint_escape import solve

        def clear_via(net, point, diameter):
            return all(clear(net, point, point, layer, diameter) for layer in ("F.Cu", "B.Cu"))

        plans = []
        for group in geometry["joint_groups"]:
            pads = [pad for pad in group["pads"] if pad["net"] in self.gap_nets]
            if 1 < len(pads) <= 8:
                plans.append(
                    solve(
                        pads,
                        clear_segment=clear,
                        clear_via=clear_via,
                        segment_distance=p._segment_distance,
                    )
                )
            if len(plans) >= 3:
                break
        return {
            "pcb_name": self.pcb.name,
            "immutable_requirement": self.state.requirement_text,
            "pcb_sha256": _digest(self.pcb),
            "tools": self.workspace.instructions,
            "footprints": board.list_footprints(),
            "nets": board.list_nets(),
            "violations": data.get("violations", [])[:40],
            "unconnected_items": data.get("unconnected_items", [])[:40],
            "routability": geometry,
            "joint_escape_candidates": plans,
            "joint_escape_tool": "ratsnestpro.repair.joint_escape.apply(pcb_path, plan, expected_sha256=pcb_sha256); candidate only, trunk routing still required",
            "candidate_score": self.last.score if self.last else None,
        }

    def query(self, value):
        queries = EngineeringRequests.model_validate(value)
        return {"observations": [self.workspace.observe(q) for q in queries.engineering_queries]}

    def images(self):
        if not self.workspace.images:
            from ratsnestpro.orchestration.engineering_workspace import EngineeringQuery

            for layer in ("F.Cu,F.Silkscreen,Edge.Cuts", "B.Cu,B.Silkscreen,Edge.Cuts"):
                result = self.workspace.observe(
                    EngineeringQuery(tool="render", path=str(self.pcb), layers=layer)
                )
                self.record(
                    {
                        "event": "strong_repair.rendered",
                        "layer": layer,
                        "ok": result.get("ok", False),
                    }
                )
        return list(self.workspace.images.values())[-2:]

    def execute(self, script, timeout):
        files = []
        for path in (
            self.pcb,
            self.pcb.with_suffix(".kicad_pro"),
            self.pcb.with_suffix(".kicad_dru"),
            self.report,
            self.pcb.with_suffix(".placement_constraints.json"),
        ):
            if path.is_file():
                files.append(
                    SandboxFile(path=path.name, data=base64.b64encode(path.read_bytes()).decode())
                )
        request = SandboxRequest(
            files=files, script=script, pcb_name=self.pcb.name, timeout_seconds=timeout
        )
        endpoint = os.environ.get("RATSNEST_REPAIR_EXECUTOR_URL", "").rstrip("/")
        key = os.environ.get("RATSNEST_REPAIR_EXECUTOR_TOKEN", "")
        if not endpoint or len(key) < 32:
            raise RuntimeError("isolated repair executor is not configured")
        with httpx.Client(timeout=timeout + 30, trust_env=False) as client:
            response = client.post(
                endpoint + "/v1/repair",
                json=request.model_dump(),
                headers={"Authorization": "Bearer " + key},
            )
            response.raise_for_status()
            result = SandboxResult.model_validate(response.json())
        if result.status == "completed" and result.pcb_data:
            data = base64.b64decode(result.pcb_data, validate=True)
            if len(data) > 24_000_000:
                raise ValueError("candidate exceeds PCB size limit")
            previous = self.pcb.read_bytes()
            self.pcb.write_bytes(data)
            try:
                candidate = PcbBoard.load(self.pcb)
                if not candidate.list_footprints():
                    raise ValueError("candidate lost all footprints")
            except (ValueError, TypeError, IndexError, OSError):
                self.pcb.write_bytes(previous)
                return {
                    "status": "failed",
                    "output": "Invalid PCB returned; previous candidate preserved.",
                }
            # Old renders must never remain visual evidence of new copper.
            self.workspace.images.clear()
        return {"status": result.status, "output": result.output, "exit_code": result.exit_code}

    def checkpoint_candidate(self):
        shutil.copy2(self.pcb, self.best)

    def rollback_candidate(self):
        shutil.copy2(self.best, self.pcb)
        self.workspace.images.clear()

    def commit(self, expected_live_fingerprint):
        if _digest(self.live) != self.live_fingerprint:
            raise RuntimeError("live PCB changed; refusing stale candidate commit")
        assessment = self.assess()
        if assessment.invariant_failures:
            raise RuntimeError("candidate invariant validation failed")
        p = self.p
        placements = {f["reference"]: f["at"] for f in PcbBoard.load(self.pcb).list_footprints()}
        # The ordinary pipeline checkpoint writer persists these synchronized
        # placement artifacts; no circuit/selection prefix is regenerated.
        updates = {}
        for step in (p.PipelineStep.LAYOUT_CRITICAL, p.PipelineStep.LAYOUT_GENERAL):
            plan = self.state.artifact(step)
            if plan is not None and hasattr(plan, "placements"):
                updates[step] = plan.model_copy(
                    update={
                        "placements": [
                            item.model_copy(update=placements[item.ref]) for item in plan.placements
                        ]
                    }
                )
        replacement = self.live.with_suffix(".strong-commit.tmp")
        _atomic_json(
            self.live.parent / ".strong-repair" / "pending-commit.json",
            {
                "source_sha256": self.live_fingerprint,
                "candidate_sha256": assessment.fingerprint,
                "candidate": str(self.pcb),
                "placement_updates": {
                    key.value: value.model_dump(mode="json") for key, value in updates.items()
                },
            },
        )
        shutil.copy2(self.pcb, replacement)
        replacement.replace(self.live)
        self.state.artifacts.update(updates)


def try_strong_repair(state, ctx, artifact):
    """Called only for routing-owned design failure, never infrastructure retry."""
    from ratsnestpro.orchestration import pipeline as p

    runtime = ctx.strong_repair
    if (
        runtime is None
        or not isinstance(artifact, p.RouteResult)
        or artifact.method != "freerouting"
    ):
        return None
    write = state.artifact(p.PipelineStep.LAYOUT_WRITE)
    if write is None or not p.kicad_cli_available():
        return None
    root = Path(write.pcb_path).parent / ".strong-repair"
    root.mkdir(exist_ok=True)
    ledger = root / "ledger.json"
    value = json.loads(ledger.read_text()) if ledger.is_file() else {"sessions": 0, "attempts": 0}
    value["attempts"] += 1
    if (
        value["attempts"] < runtime.limits.stagnation_threshold
        or value["sessions"] >= runtime.limits.max_sessions_per_run
    ):
        _atomic_json(ledger, value)
        return None
    value["sessions"] += 1
    value.update({"model": runtime.model, "reasoning_effort": runtime.reasoning_effort})
    _atomic_json(ledger, value)  # Charge admission before a crash-prone external call.

    def record(event):
        event = {
            **event,
            "step": "route_signals",
            "model": runtime.model,
            "session": value["sessions"],
        }
        with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        if ctx.on_ahe_event:
            ctx.on_ahe_event({k: v for k, v in event.items() if k != "execution"})

    record({"event": "strong_repair.started"})
    try:
        host = _BoardHost(state, ctx, record)
        if not run_session(host, complete=runtime.complete, limits=runtime.limits, record=record):
            return None
        snapshot = p._run_kicad_drc_snapshot(
            p.kicad_cli_available(), host.live, host.live.with_suffix(".strong-final.drc.json")
        )
        return p._synchronize_route_result_with_drc(artifact, snapshot).model_copy(
            update={
                "routed_tracks": len(PcbBoard.load(host.live).list_tracks()),
                "note": artifact.note
                + "; isolated strong-model CAD candidate independently verified",
            }
        )
    except (httpx.HTTPError, TimeoutError, LlmError) as exc:
        record({"event": "strong_repair.infrastructure_failure", "error_type": type(exc).__name__})
        # Provider/broker retries do not masquerade as exhausted design attempts.
        # Token accounting remains cumulative; infrastructure retries are bounded separately.
        value["infrastructure_failures"] = value.get("infrastructure_failures", 0) + 1
        if value["infrastructure_failures"] <= 3:
            value["sessions"] = max(0, value["sessions"] - 1)
        _atomic_json(ledger, value)
        raise  # Existing Temporal/provider policy owns retry; never replan the circuit.
    except Exception as exc:
        # Keep the engineering checkpoint; broker/provider failures are not
        # evidence that the board needs an upstream redesign.
        record({"event": "strong_repair.unavailable", "error_type": type(exc).__name__})
        return None


def recover_pending_commit(state):
    """Reconcile PCB replacement before checkpoint, without overwriting divergent CAD."""
    from ratsnestpro.orchestration import pipeline as p

    write = state.artifact(p.PipelineStep.LAYOUT_WRITE)
    if write is None:
        return
    pcb = Path(write.pcb_path)
    journal = pcb.parent / ".strong-repair" / "pending-commit.json"
    if not journal.is_file() or not pcb.is_file():
        return
    value = json.loads(journal.read_text())
    if _digest(pcb) != value["candidate_sha256"]:
        return
    for key, payload in value["placement_updates"].items():
        step = p.PipelineStep(key)
        previous = state.artifact(step)
        if previous is not None:
            state.artifacts[step] = type(previous).model_validate(payload)

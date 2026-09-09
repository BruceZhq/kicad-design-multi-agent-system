"""A2A task delegation. No model loop or unverified live writes here."""
import base64
import hashlib
import json
import os
import time

import httpx
from a2a.types import AgentCard, Task

from ratsnestpro.repair.a2a_contracts import RepairTaskInput, portable


def delegate(host, runtime):
    from ratsnestpro.repair.joint_candidate import fingerprint

    root = host.live.parent
    from ratsnestpro.repair.handoff import collect
    files, dossier = collect(host)
    request = RepairTaskInput(
        base_digest=fingerprint(host.state.artifacts),
        scope=hashlib.sha256(str(root).encode()).hexdigest(),
        allowance=runtime.allowance_key or hashlib.sha256(str(root).encode()).hexdigest(),
        model=runtime.model, reasoning_effort=runtime.reasoning_effort,
        requirement=host.state.requirement_text, project_name=host.state.project_name,
        artifacts=portable({k.value: a.model_dump(mode="json") for k, a in host.state.artifacts.items()}, str(root), "@project"),
        files=files, dossier=dossier, joint=host.joint,
        fanout_approval=host.observe().get("verified_fanout_approval", {}),
    )
    message_id = request.digest()
    endpoint = os.environ["RATSNEST_A2A_REPAIR_URL"].rstrip("/")
    token = os.environ.get("RATSNEST_A2A_REPAIR_TOKEN", "")
    if len(token) < 32:
        raise ValueError("external repair authentication is not configured")
    body = {"jsonrpc": "2.0", "id": message_id, "method": "message/send", "params": {"message": {
        "kind": "message", "role": "user", "messageId": message_id,
        "parts": [{"kind": "data", "data": request.model_dump(mode="json")}],
    }}}
    if len(json.dumps(body).encode()) > 96_000_000:
        raise ValueError("project snapshot exceeds external repair limit")
    def rpc(client, payload):
        with client.stream("POST", endpoint, json=payload) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 40_000_000:
                    raise ValueError("external repair response too large")
                chunks.append(chunk)
        result = json.loads(b"".join(chunks))
        if result.get("error"):
            raise RuntimeError("external repair RPC failed: " + str(result["error"].get("code")))
        return Task.model_validate(result["result"])
    with httpx.Client(headers={"Authorization": "Bearer " + token}, timeout=30, follow_redirects=False, trust_env=False) as client:
        card_url = endpoint.rsplit("/", 1)[0] + "/.well-known/agent-card.json"
        card_response = client.get(card_url)
        card_response.raise_for_status()
        card = AgentCard.model_validate(card_response.json())
        if card.protocol_version != "0.3.0" or card.version != '2.0.0' or not any(s.id == "repair-cad-project" for s in card.skills):
            raise ValueError("incompatible external repair Agent Card")
        task = rpc(client, body)
        host.record({"event": "strong_repair.a2a_submitted", "task_id": task.id})
        deadline = time.monotonic() + runtime.limits.max_total_seconds
        terminal = {"completed", "failed", "canceled", "rejected", "input-required", "auth-required"}
        previous = None
        while task.status.state.value not in terminal:
            metadata = task.metadata or {}
            progress = (task.status.state.value, metadata.get("event"), metadata.get("turn"))
            if progress != previous:
                host.record({"event": "strong_repair.a2a_progress", "task_id": task.id, "status": task.status.state.value,
                             "turn": metadata.get("turn"), "external_event": metadata.get("event")})
                previous = progress
            if time.monotonic() >= deadline:
                raise TimeoutError("external repair is still running; retry attaches to the same task")
            time.sleep(2)
            task = rpc(client, {"jsonrpc": "2.0", "id": message_id, "method": "tasks/get", "params": {"id": task.id}})
        if task.status.state.value != "completed":
            host.record({"event": "strong_repair.a2a_terminal", "task_id": task.id, "status": task.status.state.value})
            if (task.metadata or {}).get("error_type") == "LlmBudgetExceeded":
                from ratsnestpro.agents.llm import LlmBudgetExceeded
                raise LlmBudgetExceeded("external repair allowance exhausted")
            return False
        result = task.artifacts[0].parts[0].root.data
        if result.get("base_digest") != request.base_digest:
            raise ValueError("external candidate base mismatch")
        if not result.get("improved"):
            return False
        # The remote result is only a candidate. Reuse local trusted grading
        # and atomic commit, including State/live-file optimistic concurrency.
        before = host.assess()
        pcb = base64.b64decode(result["pcb_data"], validate=True)
        if len(pcb) > 24_000_000:
            raise ValueError("candidate too large")
        if host.joint:
            from ratsnestpro.repair.contracts import RepairProposal
            host.execute_joint(RepairProposal(action="joint_candidate", rationale="A2A candidate",
                                              zone_bindings=result.get("zone_bindings", {}),
                                              refresh_evidence=True), 1)
        from ratsnestpro.repair.contracts import SandboxFile
        host.stage(result['pcb_data'], [SandboxFile.model_validate(f) for f in result.get('files', [])])
        host.last = None
        if not host.assess().improves(before):
            host.rollback_candidate()
            return False
        host.commit(before.fingerprint)
        return True

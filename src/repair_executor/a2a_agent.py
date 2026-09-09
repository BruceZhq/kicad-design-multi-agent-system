"""Independent A2A 0.3 repair agent. No LangGraph execution or shared Run mount.

SQLite stores task/idempotency/budget state for a single-replica internal pilot.
Only the separate sandbox broker executes model-written programs.
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from ratsnestpro.repair.a2a_contracts import RepairTaskInput, portable, reject_host_paths

ROOT = Path(os.getenv("RATSNEST_A2A_DATA_DIR", "/data/external-repair"))
POOL = ThreadPoolExecutor(max_workers=1)


@contextmanager
def db():
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / "tasks.sqlite", timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, digest TEXT, body TEXT, state TEXT, result TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS budgets(id TEXT PRIMARY KEY, tokens INTEGER NOT NULL)")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def task_view(row):
    return {"kind": "task", "id": row[0], "contextId": row[0], "status": {"state": row[3]},
            **json.loads(row[4] or "{}")}


@asynccontextmanager
async def lifespan(_app):
    with db() as conn:
        # A process crash is not permission to repeat a possibly paid call.
        conn.execute("UPDATE tasks SET state='failed', result=? WHERE state IN ('submitted','working')",
                     (json.dumps({"metadata": {"reason": "service_interrupted; inspect task before explicit retry"}}),))
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def auth(request, call_next):
    key = os.getenv("RATSNEST_A2A_REPAIR_TOKEN", "")
    if len(key) < 32 or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key):
        return JSONResponse({"detail": "authentication required"}, status_code=403)
    if request.method == "POST":
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 96_000_000:
                return JSONResponse({"detail": "request too large"}, status_code=413)
        request._body = bytes(data)
    return await call_next(request)


@app.get("/.well-known/agent-card.json")
def card():
    return {"protocolVersion": "0.3.0", "name": "External CAD Repair Agent", "version": "2.0.0",
            "description": "Independent observe-program-verify repair of an isolated KiCad snapshot; never declares release readiness.",
            "url": os.getenv("RATSNEST_A2A_PUBLIC_URL", "http://external_repair_agent:8098/a2a"),
            "preferredTransport": "JSONRPC", "capabilities": {"streaming": False, "pushNotifications": False},
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}}, "security": [{"serviceToken": []}],
            "defaultInputModes": ["application/json"], "defaultOutputModes": ["application/json"],
            "skills": [{"id": "repair-cad-project", "name": "Repair CAD candidate", "tags": ["kicad", "repair"],
                        "description": "Own repair loop with real geometry, rendering, sandbox execution and DRC; returned changes require caller validation."}]}


def fail(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


@app.post("/a2a")
async def rpc(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return fail(None, -32700, "Parse error")
    if not isinstance(body, dict):
        return fail(None, -32600, "Invalid request")
    rid = body.get("id")
    method, params = body.get("method"), body.get("params", {})
    if not isinstance(params, dict):
        return fail(rid, -32602, "Invalid params")
    if body.get("jsonrpc") != "2.0" or rid is None:
        return fail(rid, -32600, "Invalid request")
    if method == "message/send":
        try:
            from a2a.types import MessageSendParams
            message = MessageSendParams.model_validate(params).message
            if message.role != "user" or len(message.parts) != 1:
                raise ValueError("one user data part required")
            payload = RepairTaskInput.model_validate(message.parts[0].root.data)
            reject_host_paths(payload.artifacts)
            models = {v.strip() for v in os.getenv("RATSNEST_A2A_ALLOWED_MODELS", "").split(",") if v.strip()}
            if payload.model not in models:
                raise ValueError("model not enabled on external service")
        except (ValueError, AttributeError, TypeError):
            return fail(rid, -32602, "Invalid repair payload or model not enabled")
        tid = hashlib.sha256((payload.scope + message.message_id).encode()).hexdigest()
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if row:
                if row[1] != payload.digest():
                    return fail(rid, -32602, "messageId reused with different input")
                return {"jsonrpc": "2.0", "id": rid, "result": task_view(row)}
            if conn.execute("SELECT count(*) FROM tasks WHERE state IN ('submitted','working')").fetchone()[0]:
                return fail(rid, -32000, "Repair agent busy; retry the same messageId")
            conn.execute("INSERT INTO tasks VALUES(?,?,?,'submitted','{}')", (tid, payload.digest(), payload.model_dump_json()))
        POOL.submit(run_task, tid)
    elif method in {"tasks/get", "tasks/cancel"}:
        tid = params.get("id", "")
        with db() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not row:
                return fail(rid, -32001, "Task not found")
            if method == "tasks/cancel":
                if row[3] not in {"submitted", "working"}:
                    return fail(rid, -32002, "Task not cancelable")
                conn.execute("UPDATE tasks SET state='canceled', result='{}' WHERE id=?", (tid,))
    else:
        return fail(rid, -32601, "Method not found")
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return {"jsonrpc": "2.0", "id": rid, "result": task_view(row)}


def cancelled(tid):
    with db() as conn:
        return conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "canceled"


def run_task(tid):
    # Each task owns its KiCad library registries, model caches and process.
    # Never leak a generated symbol from one project into the next project.
    try:
        subprocess.run([sys.executable, "-m", "repair_executor.a2a_agent", "--task", tid],
                       timeout=900, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        with db() as conn:
            conn.execute("UPDATE tasks SET state='failed',result=? WHERE id=? AND state!='canceled'",
                         (json.dumps({"metadata": {"reason": "worker_process_failed"}}), tid))


def work(tid, payload):
    try:
        if cancelled(tid):
            return
        with db() as conn:
            conn.execute("UPDATE tasks SET state='working' WHERE id=? AND state='submitted'", (tid,))
        result = repair_snapshot(tid, payload)
        output = {"artifacts": [{"artifactId": tid, "name": "candidate-result", "parts": [{"kind": "data", "data": result}]}]}
        state = "completed"
    except Exception as exc:
        # Do not expose model/provider errors or credentials in the public task.
        state = "input-required" if type(exc).__name__ in {"LlmBudgetExceeded", "ToolchainMismatch"} else "failed"
        output = {"metadata": {"error_type": type(exc).__name__}}
    with db() as conn:
        conn.execute("UPDATE tasks SET state=?,result=? WHERE id=? AND state!='canceled'", (state, json.dumps(output), tid))


def repair_snapshot(tid, payload):
    from ratsnestpro.orchestration import pipeline as p
    from ratsnestpro.repair.project_host import ProjectHost, PROGRAM
    from ratsnestpro.repair.session import run_session
    from ratsnestpro.repair.contracts import RepairLimits
    from ratsnestpro.eda.library_roots import register_generated_library_root

    root = ROOT / tid / "runs" / "snapshot"
    root.mkdir(parents=True, exist_ok=True)
    for file in payload.files:
        path = root / file.path
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("unsafe snapshot path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(file.data, validate=True))
    (root / 'handoff-index.json').write_text(json.dumps(payload.dossier, ensure_ascii=False, indent=2), encoding='utf-8')
    if payload.fanout_approval:
        directory = root.parent.parent / "engineering-approvals"
        directory.mkdir(exist_ok=True)
        (directory / "snapshot.fanout.json").write_text(
            json.dumps({**payload.fanout_approval, "workspace": "snapshot"}), encoding="utf-8")
    for name in ("evidence-symbols", ".ratsnest-libs"):
        if (root / name).is_dir():
            register_generated_library_root(root / name)
    state = p.PipelineState(requirement_text=payload.requirement, project_name=payload.project_name)
    for name, value in payload.artifacts.items():
        step = p.PipelineStep(name)
        state.artifacts[step] = p.ARTIFACT_MODELS[step].model_validate(portable(value, "@project", str(root)))
    (root / ".strong-repair").mkdir(exist_ok=True)
    def record(event):
        with (ROOT / tid / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, default=str) + "\n")
        with db() as conn:
            conn.execute("UPDATE tasks SET result=? WHERE id=? AND state='working'",
                         (json.dumps({"metadata": {"event": event.get("event"), "turn": event.get("turn")}}), tid))
    from agents.ratsnestpro.package_evidence import PackageEvidenceFetcher
    ctx = p.PipelineContext(out_dir=str(root), package_evidence_fetcher=PackageEvidenceFetcher(root))
    from ratsnestpro.orchestration.engineering_workspace import EngineeringWorkspace, EngineeringRequests

    class DossierHost(ProjectHost):
        """Read retained reports separately from the mutable candidate workspace."""
        def observe(self):
            observation = super().observe()
            observation['handoff'] = {
                'index': 'handoff/handoff-index.json',
                'trace': 'handoff/handoff-trace.json',
                'goal': 'Continue from existing files; fix observed failures without restarting the completed pipeline.',
                'read': 'Use engineering_queries tool=read_file, path=handoff/<relative file>, offset/limit for pagination. These are retained input files, not the current candidate. Current PCB queries remain authoritative.',
                'release': 'A clean PCB is not release readiness. Main workflow reruns all retained contracts and rebuilds dependent manufacturing outputs before release.',
            }
            return observation

        def query(self, value):
            results = []
            for query in EngineeringRequests.model_validate(value).engineering_queries:
                if query.tool == 'read_file' and query.path.startswith('handoff/'):
                    query = query.model_copy(update={'path': query.path[len('handoff/'):]})
                    results.append(dossier_workspace.observe(query))
                else:
                    results.extend(super().query({'engineering_queries': [query.model_dump()]})['observations'])
            return {'observations': results}

    dossier_workspace = EngineeringWorkspace(out_dir=str(root), artifacts=lambda: payload.artifacts)
    host = DossierHost(state, ctx, record, joint=payload.joint)
    limits = RepairLimits(max_llm_tokens=120000)
    complete = model_client(tid, payload, limits)
    improved = run_session(host, complete=complete, limits=limits, record=record)
    if cancelled(tid):
        raise RuntimeError("task canceled")
    partition = state.artifact(p.PipelineStep.LAYOUT_PARTITION)
    original_bindings = payload.artifacts.get("layout_partition", {}).get("zone_bindings", {})
    return {"base_digest": payload.base_digest, "improved": improved,
            "pcb_data": base64.b64encode(host.live.read_bytes()).decode() if improved else None,
            "files": [{"path": name, "data": base64.b64encode((root / name).read_bytes()).decode()}
                      for name in (host.sch.name, PROGRAM) if improved and (root / name).is_file()],
            "zone_bindings": {k: v for k, v in partition.zone_bindings.items() if original_bindings.get(k) != v} if partition else {},
            "release_ready": False}


def model_client(tid, payload, limits):
    from core import get_model, settings
    from langchain_core.messages import HumanMessage, SystemMessage
    from ratsnestpro.agents.llm import LlmBudgetExceeded

    matches = [m for m in settings.AVAILABLE_MODELS if m.value == payload.model]
    if len(matches) != 1:
        raise ValueError("selected model unavailable on external agent")
    model = get_model(matches[0], reasoning_effort=payload.reasoning_effort)
    key = hashlib.sha256((payload.scope + payload.allowance).encode()).hexdigest()
    def complete(system, user, images, remaining):
        if cancelled(tid):
            raise RuntimeError("task canceled")
        estimate = max(1, (len(system) + len(user)) // 3) + len(images) * 8192 + 8192
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO budgets VALUES(?,0)", (key,))
            used = conn.execute("SELECT tokens FROM budgets WHERE id=?", (key,)).fetchone()[0]
            if used + estimate > limits.max_llm_tokens:
                raise LlmBudgetExceeded("external repair allowance exhausted")
            conn.execute("UPDATE budgets SET tokens=tokens+? WHERE id=?", (estimate, key))
        content = [{"type": "text", "text": user}] + [{"type": "image_url", "image_url": {"url": uri}} for uri in images]
        active = model.model_copy(update={"request_timeout": min(120, max(1, remaining)), "max_retries": 0, "max_tokens": 8192})
        response = active.invoke([SystemMessage(content=system), HumanMessage(content=content)])
        usage = response.usage_metadata or {}
        actual = int(usage.get("total_tokens") or estimate)
        with db() as conn:
            conn.execute("UPDATE budgets SET tokens=tokens+? WHERE id=?", (actual - estimate, key))
        if not isinstance(response.content, str):
            raise ValueError("repair response must be JSON text")
        return response.content
    return complete


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--task":
        raise SystemExit("use uvicorn for server or --task for worker")
    with db() as conn:
        row = conn.execute("SELECT body FROM tasks WHERE id=?", (sys.argv[2],)).fetchone()
    if row is None:
        raise SystemExit("task missing")
    work(sys.argv[2], RepairTaskInput.model_validate_json(row[0]))

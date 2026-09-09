import base64
import json
import time

import pytest
from fastapi.testclient import TestClient
from a2a.types import AgentCard, Task

from ratsnestpro.repair.a2a_contracts import ProjectFile, RepairTaskInput, reject_host_paths


def payload():
    return RepairTaskInput(base_digest="a"*64, scope="b"*64, allowance="c"*64,
                           model="test-model", reasoning_effort="high", requirement="a board",
                           project_name="board", artifacts={}, files=[])


def test_handoff_includes_reports_history_and_retained_cad(tmp_path):
    from types import SimpleNamespace
    from ratsnestpro.repair.handoff import collect
    (tmp_path / 'board.kicad_pcb').write_text('pcb')
    (tmp_path / 'board.ses').write_text('routing')
    (tmp_path / 'review.md').write_text('DRC has gaps')
    (tmp_path / '.env').write_text('private')
    (tmp_path / '.strong-repair').mkdir()
    (tmp_path / '.strong-repair' / 'ledger.json').write_text('{}')
    host = SimpleNamespace(live=tmp_path / 'board.kicad_pcb',
        state=SimpleNamespace(results=[{'step': 'manufacture', 'blocked': True}],
                              recovery_history=[{'reason': 'unconnected'}]), ctx=SimpleNamespace(repair_feedback='repair 24 gaps'))
    files, dossier = collect(host)
    names = {f.path for f in files}
    assert {'board.kicad_pcb', 'board.ses', 'review.md', 'handoff-trace.json'} <= names
    assert '.env' not in names and '.strong-repair/ledger.json' not in names
    trace = json.loads(base64.b64decode(next(f.data for f in files if f.path == 'handoff-trace.json')))
    assert trace['results'][0]['blocked'] is True
    assert trace['recovery_history'][0]['reason'] == 'unconnected'
    assert dossier['files']
    # Volatile admission bookkeeping must not create a new paid A2A request.
    (tmp_path / '.strong-repair' / 'ledger.json').write_text('{"sessions":2}')
    assert collect(host) == (files, dossier)


@pytest.mark.parametrize("path", ["../secret.json", "/secret.json", "C:/secret.json", "a\\b.json", ".env"])
def test_snapshot_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        ProjectFile(path=path, data=base64.b64encode(b"x").decode())


def test_embedded_manifest_cannot_read_service_files():
    with pytest.raises(ValueError):
        reject_host_paths({"manifest": json.dumps({"path": "/etc/private.json"})})
    reject_host_paths({"path": "@project/board.kicad_pcb"})
    with pytest.raises(ValueError):
        reject_host_paths({"path": "@project/../../private.json"})


def test_installed_asset_provenance_is_not_arbitrary_host_access():
    record = {'source_path': '/usr/share/kicad/symbols/Device.kicad_sym'}
    original = json.dumps(record)
    reject_host_paths({'prepared_manifest_json': original})
    assert json.dumps(record) == original
    reject_host_paths({'source_path': '/usr/share/kicad/footprints/Resistor_SMD.pretty/R_0603_1608Metric.kicad_mod'})
    for path in ('/etc/private.json', '/usr/share/kicad/symbols/../../private.kicad_sym',
                 '/usr/share/kicad/symbols/key.json', '/usr/share/kicad/symbols-evil/Device.kicad_sym'):
        with pytest.raises(ValueError):
            reject_host_paths({'source_path': path})
    with pytest.raises(ValueError):
        reject_host_paths({'pcb_path': record['source_path']})


def test_a2a_cancel_is_persistent_and_cannot_be_overwritten(tmp_path, monkeypatch):
    from repair_executor import a2a_agent as server
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("RATSNEST_A2A_REPAIR_TOKEN", "k"*32)
    headers = {"Authorization": "Bearer " + "k"*32}
    with TestClient(server.app) as client:
        with server.db() as conn:
            conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", ("task", "digest", payload().model_dump_json(), "working", "{}"))
        result = client.post("/a2a", headers=headers, json={"jsonrpc": "2.0", "id": "cancel", "method": "tasks/cancel", "params": {"id": "task"}}).json()
        assert result["result"]["status"]["state"] == "canceled"
        monkeypatch.setattr(server, "repair_snapshot", lambda *_: pytest.fail("canceled task must not execute"))
        server.work("task", payload())
        assert server.cancelled("task")


def test_a2a_client_rejects_stale_remote_candidate(tmp_path, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from ratsnestpro.repair import a2a_client
    from repair_executor import a2a_agent
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("original", encoding="utf-8")
    host = SimpleNamespace(live=pcb, state=SimpleNamespace(artifacts={}, requirement_text="board", project_name="board"),
                           joint=False, observe=lambda: {}, record=lambda _: None)
    runtime = SimpleNamespace(allowance_key="a"*64, model="test-model", reasoning_effort="high", limits=SimpleNamespace(max_total_seconds=30))
    monkeypatch.setenv("RATSNEST_A2A_REPAIR_URL", "http://repair/a2a")
    monkeypatch.setenv("RATSNEST_A2A_REPAIR_TOKEN", "k"*32)
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json=a2a_agent.card())
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "result", "result": {
            "kind": "task", "id": "task", "contextId": "task", "status": {"state": "completed"},
            "artifacts": [{"artifactId": "candidate", "parts": [{"kind": "data", "data": {"improved": True, "base_digest": "wrong"}}]}]}})
    original = httpx.Client
    monkeypatch.setattr(a2a_client.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(ValueError, match="base mismatch"):
        a2a_client.delegate(host, runtime)
    assert pcb.read_text() == "original"


def test_remote_completion_claim_cannot_bypass_local_revalidation(tmp_path, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from ratsnestpro.repair import a2a_client
    from ratsnestpro.repair.joint_candidate import fingerprint
    from ratsnestpro.repair.session import CandidateAssessment
    from repair_executor import a2a_agent
    pcb = tmp_path / 'board.kicad_pcb'
    pcb.write_text('original')
    events, stages, rollbacks = [], [], []
    host = SimpleNamespace(live=pcb,
        state=SimpleNamespace(artifacts={}, requirement_text='board', project_name='board'),
        joint=False, observe=lambda: {}, record=events.append,
        assess=lambda: CandidateAssessment('old', 0, 8, 0),
        revalidate=lambda: CandidateAssessment('bad', 1, 0, 0, ('short circuit',)),
        stage=lambda *args: stages.append(args), rollback_candidate=lambda: rollbacks.append(True),
        commit=lambda *_: pytest.fail('remote claim must not bypass local validation'))
    runtime = SimpleNamespace(allowance_key='a'*64, model='test-model', reasoning_effort='high',
                              limits=SimpleNamespace(max_total_seconds=30))
    monkeypatch.setenv('RATSNEST_A2A_REPAIR_URL', 'http://repair/a2a')
    monkeypatch.setenv('RATSNEST_A2A_REPAIR_TOKEN', 'k'*32)
    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200, json=a2a_agent.card())
        data = {'base_digest': fingerprint({}), 'improved': True, 'release_ready': True,
                'repair_status': 'agent_reported_complete', 'validation_status': 'candidate_checks_passed',
                'pcb_data': base64.b64encode(b'candidate').decode()}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 'r', 'result': {
            'kind': 'task', 'id': 'task', 'contextId': 'task', 'status': {'state': 'completed'},
            'artifacts': [{'artifactId': 'candidate', 'parts': [{'kind': 'data', 'data': data}]}]}})
    original = httpx.Client
    monkeypatch.setattr(a2a_client.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert a2a_client.delegate(host, runtime) is False
    assert len(stages) == 1 and rollbacks == [True]
    assert events[-1]['event'] == 'strong_repair.a2a_candidate_rejected'
    assert events[-1]['release_ready'] is False


def test_a2a_real_envelopes_idempotency_auth_and_persistence(tmp_path, monkeypatch):
    from repair_executor import a2a_agent as server
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("RATSNEST_A2A_REPAIR_TOKEN", "k"*32)
    monkeypatch.setenv("RATSNEST_A2A_ALLOWED_MODELS", "test-model")
    calls = []
    def repair(tid, request):
        calls.append(tid)
        return {"improved": False, "base_digest": request.base_digest}
    monkeypatch.setattr(server, "repair_snapshot", repair)
    def local_task(tid):
        with server.db() as conn:
            raw = conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        server.work(tid, RepairTaskInput.model_validate_json(raw))
    monkeypatch.setattr(server, "run_task", local_task)
    headers = {"Authorization": "Bearer " + "k"*32}
    request = {"jsonrpc": "2.0", "id": "rpc1", "method": "message/send", "params": {"message": {
        "kind": "message", "role": "user", "messageId": "msg1", "parts": [{"kind": "data", "data": payload().model_dump()}]}}}
    with TestClient(server.app) as client:
        assert client.get("/.well-known/agent-card.json").status_code == 403
        assert AgentCard.model_validate(client.get("/.well-known/agent-card.json", headers=headers).json()).protocol_version == "0.3.0"
        response = client.post("/a2a", json=request, headers=headers).json()
        task = Task.model_validate(response["result"])
        for _ in range(40):
            data = client.post("/a2a", json={"jsonrpc": "2.0", "id": "get", "method": "tasks/get", "params": {"id": task.id}}, headers=headers).json()
            if data["result"]["status"]["state"] == "completed":
                break
            time.sleep(.025)
        assert Task.model_validate(data["result"]).status.state.value == "completed"
        assert client.post("/a2a", json=request, headers=headers).json()["result"]["id"] == task.id
        assert len(calls) == 1
        request["params"]["message"]["parts"][0]["data"]["requirement"] = "different"
        assert client.post("/a2a", json=request, headers=headers).json()["error"]["code"] == -32602
    with TestClient(server.app) as client:
        response = client.post("/a2a", json={"jsonrpc": "2.0", "id": "get", "method": "tasks/get", "params": {"id": task.id}}, headers=headers).json()
        assert response["result"]["status"]["state"] == "completed"


def test_gateway_mcp_uses_existing_evidence_gate(monkeypatch):
    from agents.ratsnestpro import knowledge_gateway as gateway, knowledge_mcp
    monkeypatch.setenv("RATSNEST_KNOWLEDGE_GATEWAY_URL", "http://rag/mcp")
    monkeypatch.setenv("RATSNEST_KNOWLEDGE_TRANSPORT", "mcp")
    calls = []
    def search(*args):
        calls.append(args[2])
        return {"status": "ok", "evidence_sufficient": True, "results": []}
    monkeypatch.setattr(knowledge_mcp, "search_mcp", search)
    result = gateway.search_external_knowledge(query="MCU pins", role="parts-specialist", limit=3, tenant_scope="tenant-a")
    assert calls[0]["scope"]["tenant"] == "tenant-a"
    assert result["evidence_sufficient"] is False


def test_real_mcp_initialization_discovery_and_call(monkeypatch):
    import socket
    import threading
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from agents.ratsnestpro.knowledge_mcp import search_mcp
    mcp = FastMCP("test-rag", stateless_http=True, json_response=True)
    @mcp.tool()
    def search_knowledge(query: str, **kwargs) -> dict:
        return {"status": "ok", "results": [], "query": query}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        result = search_mcp(f"http://127.0.0.1:{port}/mcp", "", {"query": "pin table", "kwargs": {}}, 5, "search_knowledge")
        assert result["query"] == "pin table"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()

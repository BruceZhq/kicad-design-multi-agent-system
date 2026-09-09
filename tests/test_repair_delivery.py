import base64
import hashlib
import io
import json
import zipfile

import pytest

from ratsnestpro.repair.delivery import build_delivery, persist_delivery, finish_with_issues
from ratsnestpro.repair.session import CandidateAssessment


def test_failed_repair_returns_complete_retained_project_and_errors(tmp_path):
    for name in ('board.kicad_pcb', 'board.kicad_sch', 'board.kicad_pro', 'fp-lib-table'):
        (tmp_path / name).write_text('original')
    (tmp_path / '.env').write_text('PRIVATE')
    (tmp_path / '.strong-repair').mkdir()
    (tmp_path / '.strong-repair/key.json').write_text('PRIVATE')
    assessment = CandidateAssessment('base', 0, 8, 17)
    delivery = build_delivery(tmp_path, outcome={'termination': 'turn_limit'},
                              assessment=assessment, findings=[{'error': '8 unconnected'}])
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(delivery['archive_data']))) as archive:
        assert archive.read('board.kicad_pcb') == b'original'
        assert 'board.kicad_sch' in archive.namelist()
        assert not any('key.json' in n or n == '.env' for n in archive.namelist())
        report = json.loads(archive.read('repair-error-report.json'))
        assert report['score'] == [0, 8, 17] and report['release_ready'] is False
        assert report['remaining_findings'][0]['error'] == '8 unconnected'
    persist_delivery(tmp_path, delivery)
    assert (tmp_path / 'board.kicad_pcb').read_text() == 'original'
    hardware = finish_with_issues({'release_blockers': ['8 unconnected']}, tmp_path)
    assert hardware['release_blockers'] == ['8 unconnected']
    assert hardware['user_ended_repair'] and not hardware['release_ready']
    assert len(hardware['actual_files']) == 2


def test_delivery_rejects_tamper_and_archive_traversal(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('../board.kicad_pcb', 'bad')
    raw = buffer.getvalue()
    data = {'archive_data': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest(), 'report': {}}
    with pytest.raises(ValueError):
        persist_delivery(tmp_path, data)
    data['sha256'] = '0' * 64
    with pytest.raises(ValueError):
        persist_delivery(tmp_path, data)


def test_budget_exhaustion_without_improvement_has_terminal_outcome():
    from ratsnestpro.repair.session import run_session
    from ratsnestpro.repair.contracts import RepairLimits
    from ratsnestpro.agents.llm import LlmBudgetExceeded
    from test_strong_repair_session import Host
    events = []
    def complete(*_):
        raise LlmBudgetExceeded('spent')
    host = Host()
    assert not run_session(host, complete=complete, limits=RepairLimits(), record=events.append)
    assert events[-1]['termination'] == 'budget_exhausted'
    assert not host.committed


def test_hitl_finish_exits_without_more_model_calls(tmp_path, monkeypatch):
    import asyncio
    from agents.ratsnestpro import ratsnestpro_agent as agent
    from ratsnestpro.repair.continuation import FINISH, APPROVE
    root = tmp_path / 'runs/case'
    root.mkdir(parents=True)
    (root / 'board.kicad_pcb').write_text('retained')
    (root / '.strong-repair').mkdir()
    (root / '.strong-repair/ledger.json').write_text('{"budget_exhausted":true}')
    monkeypatch.setattr(agent, '_workspace_root', lambda: tmp_path)
    monkeypatch.setattr(agent, '_workspace_run_name', lambda _: 'case')
    requests = []
    monkeypatch.setattr(agent, 'interrupt', lambda q: requests.append(q) or FINISH)
    state = {'hardware': {'release_ready': False, 'release_blockers': ['DRC failure'],
                         'ahe': {'agentic_recovery': {'history': [{'status': 'awaiting_human'}]}}}}
    result = asyncio.run(agent.hardware_evidence_input(state, {}))
    assert requests[0]['options'] == [APPROVE, FINISH]
    assert agent._after_hardware_input(result) == 'final_report'
    assert agent._hardware_human_request(result) is None
    assert result['hardware']['release_blockers'] == ['DRC failure']
    assert json.loads((root / '.strong-repair/continuation.json').read_text(encoding='utf-8'))['grant'] is False


def test_no_improvement_a2a_response_still_publishes_archive(tmp_path, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from ratsnestpro.repair import a2a_client
    from ratsnestpro.repair.joint_candidate import fingerprint
    from repair_executor.a2a_agent import card
    pcb = tmp_path / 'board.kicad_pcb'
    pcb.write_text('original')
    delivery = build_delivery(tmp_path, outcome={'termination': 'turn_limit'},
                              assessment=CandidateAssessment('old', 0, 8, 17))
    host = SimpleNamespace(live=pcb, state=SimpleNamespace(artifacts={}, requirement_text='board', project_name='board'),
                           joint=False, observe=lambda: {}, record=lambda _: None,
                           stage=lambda *_: pytest.fail('archive must not stage CAD'))
    runtime = SimpleNamespace(allowance_key='a'*64, model='test', reasoning_effort='high',
                              limits=SimpleNamespace(max_total_seconds=30))
    monkeypatch.setenv('RATSNEST_A2A_REPAIR_URL', 'http://repair/a2a')
    monkeypatch.setenv('RATSNEST_A2A_REPAIR_TOKEN', 'k'*32)
    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200, json=card())
        result = {'base_digest': fingerprint({}), 'improved': False, 'delivery': delivery}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 'r', 'result': {
            'kind': 'task', 'id': 'task', 'contextId': 'task', 'status': {'state': 'completed'},
            'artifacts': [{'artifactId': 'candidate', 'parts': [{'kind': 'data', 'data': result}]}]}})
    original = httpx.Client
    monkeypatch.setattr(a2a_client.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert a2a_client.delegate(host, runtime) is False
    assert (tmp_path / 'terra-repair-delivery.zip').is_file()
    assert pcb.read_text() == 'original'

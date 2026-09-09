import base64
import json
from types import SimpleNamespace

import pytest

from ratsnestpro.repair.contracts import SandboxFile, SandboxRequest
from ratsnestpro.repair.project_transaction import apply, digest


def test_sandbox_project_outputs_are_narrow():
    args = dict(files=[SandboxFile(path='board.kicad_pcb', data='')], script='pass', pcb_name='board.kicad_pcb')
    SandboxRequest(**args, return_paths=['board.kicad_sch', 'programs/repair_generator.py'])
    for name in ('../../outside.py', 'programs/grader.py', 'board.kicad_pro'):
        with pytest.raises(ValueError):
            SandboxRequest(**args, return_paths=[name])


def test_project_program_receives_context_and_reports_but_cannot_return_them(tmp_path, monkeypatch):
    from ratsnestpro.repair import project_host
    host = object.__new__(project_host.ProjectHost)
    host.root = tmp_path
    host.pcb, host.sch, host.report = [tmp_path / name for name in
        ('board.kicad_pcb', 'board.kicad_sch', 'board.trusted.drc.json')]
    for path in (host.pcb, host.sch, host.report, host.sch.with_suffix('.erc.json')):
        path.write_text('{}')
    host.state = SimpleNamespace(requirement_text='Keep two copper layers', project_name='original')
    host.view_state = SimpleNamespace(artifacts={})
    host.release_findings = [{'step': 'route_signals', 'error': '8 unconnected'}]
    captured = []
    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json, headers):
            captured.append(SandboxRequest.model_validate(json))
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: {'status': 'failed', 'output': 'inspection only'})
    monkeypatch.setattr(project_host.httpx, 'Client', Client)
    monkeypatch.setenv('RATSNEST_REPAIR_EXECUTOR_URL', 'http://executor')
    monkeypatch.setenv('RATSNEST_REPAIR_EXECUTOR_TOKEN', 'k' * 32)
    host.execute('print("read current files")', 10)
    request = captured[0]
    files = {f.path: base64.b64decode(f.data) for f in request.files}
    context = json.loads(files['repair-context.json'])
    assert context['requirement'] == 'Keep two copper layers'
    assert context['release_findings'] == host.release_findings
    assert {'board.trusted.drc.json', 'board.erc.json'} <= files.keys()
    assert 'repair-context.json' not in request.return_paths
    with pytest.raises(ValueError):
        SandboxRequest.model_validate({**request.model_dump(), 'return_paths': ['repair-context.json']})


def transaction(tmp_path):
    files = []
    for name in ('pcb', 'sch'):
        source, candidate = tmp_path / name, tmp_path / (name + '.candidate')
        source.write_text('old'); candidate.write_text('new')
        files.append(dict(target=str(source), candidate=str(candidate), source_digest=digest(source), candidate_digest=digest(candidate)))
    journal = tmp_path / 'transaction.json'
    journal.write_text(json.dumps(dict(id='test', files=files, updates={}, source_artifacts={})))
    return journal, SimpleNamespace(artifacts={}, draft_execution={})


def test_joint_commit_recovers_after_first_file_replaced(tmp_path):
    journal, state = transaction(tmp_path)
    (tmp_path / 'pcb').write_text('new')  # Crash between replacements.
    apply(state, journal)
    assert (tmp_path / 'sch').read_text() == 'new'
    assert state.draft_execution['manufacturing_refresh_required']
    (tmp_path / 'pcb').write_text('later revision')
    apply(state, journal)  # Already-checkpointed transaction must not reapply.
    assert (tmp_path / 'pcb').read_text() == 'later revision'


def test_joint_commit_checks_all_sources_before_writing_any(tmp_path):
    journal, state = transaction(tmp_path)
    (tmp_path / 'sch').write_text('concurrent edit')
    with pytest.raises(RuntimeError, match='newer file'):
        apply(state, journal)
    assert (tmp_path / 'pcb').read_text() == 'old'


def test_project_stage_requires_schematic(tmp_path):
    from ratsnestpro.repair.project_host import ProjectHost
    host = object.__new__(ProjectHost)
    host.root, host.pcb, host.sch = tmp_path, tmp_path / 'b.kicad_pcb', tmp_path / 'b.kicad_sch'
    host.workspace = SimpleNamespace(images={})
    host.pcb.write_text('original')
    with pytest.raises(ValueError, match='incomplete'):
        host.stage(base64.b64encode(b'candidate').decode(), [])
    assert host.pcb.read_text() == 'original'


def test_toolchain_mismatch_detected_before_model_call(tmp_path, monkeypatch):
    from ratsnestpro.repair import project_host
    pcb = tmp_path / 'b.kicad_pcb'
    pcb.write_text('(kicad_pcb (generator_version "10.0"))')
    monkeypatch.setattr(project_host.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='9.0.2'))
    with pytest.raises(project_host.ToolchainMismatch):
        project_host.check_toolchain(pcb, 'kicad-cli')


def test_embedded_pin_type_cannot_be_changed_to_silence_erc(tmp_path):
    from ratsnestpro.repair.project_host import ProjectHost
    host = object.__new__(ProjectHost)
    host.sch = tmp_path / 'board.kicad_sch'
    host.sch.write_text('(kicad_sch (lib_symbols (symbol "test" (pin passive line))))')
    original = host.schematic_identity()
    host.sch.write_text('(kicad_sch (lib_symbols (symbol "test" (pin power_in line))))')
    assert host.schematic_identity() != original

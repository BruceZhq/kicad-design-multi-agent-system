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

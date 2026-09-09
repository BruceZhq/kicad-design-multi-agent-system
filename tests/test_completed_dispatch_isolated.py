"""Exercise the real dispatch function without provider initialization on import."""
import ast
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace, ModuleType

import pytest


def test_interaction_identity_is_idempotent_but_run_scoped():
    source = Path(__file__).parents[1] / 'src/agents/ratsnestpro/ratsnestpro_agent.py'
    node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(n, ast.FunctionDef) and n.name == '_hardware_interaction_identity')
    namespace = dict(hashlib=hashlib, json=json, _workspace_run_name=lambda s: 'same-workspace')
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    identity = namespace['_hardware_interaction_identity']
    state = {'run_scope': 'run-1', 'hardware_dispatch': {'workflow_id': 'workflow-1'}}
    request = {'turn_id': 'turn', 'revision': 1}
    config = {'configurable': {'request_id': 'request-1'}}
    first = identity(state, request, config)
    assert first == identity(state, request, config)
    assert first != identity({**state, 'run_scope': 'run-2'}, request, config)
    assert first != identity({**state, 'hardware_dispatch': {'workflow_id': 'workflow-2'}}, request, config)


@pytest.mark.parametrize('status,resume,step,continues', [
    ('completed', True, 'manufacture', True),
    ('running', True, 'manufacture', False),
    ('completed', False, None, False),
    ('completed', True, None, False),
])
def test_persisted_completed_dispatch(monkeypatch, status, resume, step, continues):
    source = Path(__file__).parents[1] / 'src/agents/ratsnestpro/ratsnestpro_agent.py'
    node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == 'hardware_dispatch_phase')
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    ast.fix_missing_locations(module)
    calls = []
    client = ModuleType('agents.ratsnestpro.temporal.client')
    async def execution_status(ref):
        return status
    async def dispatch(**kwargs):
        calls.append(kwargs)
        return {'mode': 'temporal', 'status': 'started', 'workflow_id': 'new'}
    client.hardware_workflow_execution_status = execution_status
    client.dispatch_hardware_workflow = dispatch
    client.temporal_enabled = lambda: True
    monkeypatch.setitem(sys.modules, client.__name__, client)
    namespace = dict(settings=SimpleNamespace(DEFAULT_MODEL=None),
                     _workspace_run_name=lambda s: s['workspace_run_name'],
                     _release_repair_resume_step=lambda *a, **k: step,
                     _frozen_hardware_requirement=lambda *a, **k: 'original requirement',
                     _reasoning_effort=lambda c: None, _next_hardware_attempt_number=lambda a: 1,
                     _profile_ahe_budget=lambda s: {}, _workflow_event=lambda *a, **k: None)
    exec(compile(module, str(source), 'exec'), namespace)
    ref = dict(mode='temporal', status='completed', request_id='original-request',
               workflow_id='old', workspace_run_name='unchanged-workspace')
    state = dict(hardware_dispatch=ref, incremental_resume=resume, run_name='original-run',
                 workspace_run_name='unchanged-workspace', project_name='original-project')
    result = asyncio.run(namespace['hardware_dispatch_phase'](state, {'configurable': {'request_id': 'original-request'}}))
    if continues:
        assert len(calls) == 1
        assert calls[0]['request_id'] == 'original-request.continuation.1.manufacture'
        assert calls[0]['resume_from_step'] == 'manufacture'
        assert calls[0]['workspace_run_name'] == 'unchanged-workspace'
        assert result['hardware_dispatch']['resumed_from_workflow_id'] == 'old'
    else:
        assert not calls
        assert result['hardware_dispatch'] == ref

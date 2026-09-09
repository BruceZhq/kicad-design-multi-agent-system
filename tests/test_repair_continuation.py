import json
from types import SimpleNamespace

import pytest

from ratsnestpro.repair.continuation import apply_response, budget_blocked, save_response
from ratsnestpro.repair.continuation import APPROVE


def test_explicit_million_token_grant_and_legacy_cap(tmp_path):
    state = SimpleNamespace(draft_execution={})
    save_response(tmp_path, 'old', 'approved', grant=True)
    apply_response(tmp_path, state)
    assert state.draft_execution['explicit_repair_token_limit'] == 120000
    save_response(tmp_path, 'new', APPROVE, grant=True)
    apply_response(tmp_path, state)
    assert state.draft_execution['explicit_repair_token_limit'] == 1200000


def test_continue_preserves_instruction_without_grant(tmp_path):
    state = SimpleNamespace(draft_execution={})
    save_response(tmp_path, 'one', 'Repair existing copper; preserve two layers')
    assert apply_response(tmp_path, state) == 'Repair existing copper; preserve two layers'
    assert 'allowance_key' not in state.draft_execution


def test_approval_is_idempotent_preserves_instruction_and_spend(tmp_path):
    state = SimpleNamespace(draft_execution={'repair_passes': 7})
    save_response(tmp_path, 'one', 'Fix the actual PCB')
    save_response(tmp_path, 'approval', 'approved', grant=True)
    assert apply_response(tmp_path, state) == 'Fix the actual PCB'
    first = dict(state.draft_execution)
    apply_response(tmp_path, state)
    assert state.draft_execution == first
    assert state.draft_execution['repair_passes'] == 7
    assert state.draft_execution['explicit_repair_session_limit'] == 1


def test_changed_answer_cannot_overwrite_receipt(tmp_path):
    save_response(tmp_path, 'same', 'first')
    with pytest.raises(ValueError):
        save_response(tmp_path, 'same', 'different')


def test_budget_reason_includes_session_limit(tmp_path):
    directory = tmp_path / '.strong-repair'
    directory.mkdir()
    path = directory / 'ledger.json'
    path.write_text(json.dumps({'sessions': 3, 'allowance_start_sessions': 2,
                                'allowance_session_limit': 1}))
    assert budget_blocked(tmp_path)
    path.write_text(json.dumps({'budget_exhausted': True, 'sessions': 0}))
    assert budget_blocked(tmp_path)

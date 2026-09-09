"""Durable HITL instructions and explicit repair consent, separate from requirements."""
import json
from pathlib import Path

APPROVE = '批准新增一轮 Terra 修复：最多 1200000 Token、10 轮、600 秒'
PAUSE = '暂不追加额度，保留当前工程'
FINISH = '结束修复，交付当前工程和剩余错误报告'


def budget_blocked(root):
    path = Path(root) / '.strong-repair' / 'ledger.json'
    if not path.is_file():
        return False
    value = json.loads(path.read_text(encoding='utf-8'))
    return bool(value.get('budget_exhausted')) or (
        int(value.get('sessions', 0)) >= int(value.get('allowance_start_sessions', 0)) +
        int(value.get('allowance_session_limit', 2)))


def save_response(root, identity, answer, *, grant=False):
    from ratsnestpro.repair.pipeline_adapter import _atomic_json
    directory = Path(root) / '.strong-repair'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'continuation.json'
    previous = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    # Budget approval must not overwrite the user's earlier engineering instructions.
    instruction = previous.get('instruction', '') if grant else str(answer).strip()
    if not instruction:
        instruction = 'Repair the retained project using the original requirements and actual failure evidence.'
    receipt = {'interaction_id': identity, 'instruction': instruction, 'grant': grant}
    if grant:
        receipt['max_llm_tokens'] = 1200000 if answer == APPROVE else 120000
    if previous.get('interaction_id') == identity and previous != receipt:
        raise ValueError('Acknowledged repair instruction cannot be changed during replay')
    _atomic_json(path, receipt)


def apply_response(root, state):
    path = Path(root) / '.strong-repair' / 'continuation.json'
    if not path.is_file():
        return ''
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if receipt.get('grant') is True:
        from ratsnestpro.repair.draft import authorize_repair_continuation
        authorize_repair_continuation(state, 'hitl:' + receipt['interaction_id'])
        state.draft_execution['explicit_repair_session_limit'] = 1
        state.draft_execution['explicit_repair_token_limit'] = int(receipt.get('max_llm_tokens', 120000))
    return str(receipt.get('instruction', ''))

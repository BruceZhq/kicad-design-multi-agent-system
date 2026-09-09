"""Bounded, task-local repair dossier. Never export credentials or model traces."""
import base64
import dataclasses
import hashlib
import json
from enum import Enum

from ratsnestpro.repair.a2a_contracts import ProjectFile, portable

SUFFIXES = {'.json', '.pdf', '.md', '.txt', '.csv', '.net', '.dsn', '.ses',
            '.kicad_pcb', '.kicad_sch', '.kicad_pro', '.kicad_dru', '.kicad_sym', '.kicad_mod'}
PRIVATE = ('.env', 'secret', 'credential', 'cookie', 'llm', 'token')
EXCLUDED_DIRS = {'.git', '.history', '.strong-repair', '__pycache__', 'node_modules'}


def serializable(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if dataclasses.is_dataclass(value):
        return serializable(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    return value


def collect(host):
    root = host.live.parent
    files, index, excluded = [], [], []
    total = 0
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(p in EXCLUDED_DIRS for p in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError('repair dossier cannot contain symlinks')
        if not path.is_file():
            continue
        if any(word in str(relative).lower() for word in PRIVATE):
            excluded.append({'path': relative.as_posix(), 'reason': 'private/model-trace'})
            continue
        if path.suffix not in SUFFIXES and path.name not in {'fp-lib-table', 'sym-lib-table'} and relative.as_posix() != 'programs/repair_generator.py':
            excluded.append({'path': relative.as_posix(), 'reason': 'unsupported file type'})
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('repair dossier escaped workspace')
        size = path.stat().st_size
        total += size
        if size > 24_000_000 or total > 65_000_000 or len(files) >= 510:
            raise ValueError('repair dossier exceeds transport limit; no partial dossier submitted')
        data = path.read_bytes()
        files.append(ProjectFile(path=relative.as_posix(), data=base64.b64encode(data).decode()))
        index.append({'path': relative.as_posix(), 'bytes': size, 'sha256': hashlib.sha256(data).hexdigest()})
    trace = {name: serializable(getattr(host.state, name, None)) for name in (
        'revision', 'checkpoint_generation', 'checkpoint_state_sha256', 'results',
        'repair_history', 'replan_history', 'recovery_history', 'capability_gaps',
        'connection_synthesis_report', 'draft_execution')}
    trace = portable(trace, str(root), '@project')
    trace['repair_feedback'] = getattr(getattr(host, 'ctx', None), 'repair_feedback', '')
    # Pretty JSON makes existing line-paginated read_file useful without flooding prompts.
    content = json.dumps(trace, ensure_ascii=False, indent=2, default=str).encode()
    files.append(ProjectFile(path='handoff-trace.json', data=base64.b64encode(content).decode()))
    index.append({'path': 'handoff-trace.json', 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    return files, {'files': index, 'excluded': excluded,
                   'goal': 'repair retained engineering artifacts, then pass independent release validation',
                   'history_scope': 'persisted pipeline artifacts, checks and recovery history; not an unpersisted browser transcript',
                   'excluded_directories': sorted(EXCLUDED_DIRS)}

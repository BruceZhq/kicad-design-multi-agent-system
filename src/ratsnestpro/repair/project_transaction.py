"""Journaled multi-file candidate commit. Recovery never overwrites newer work."""
import hashlib
import json
import shutil
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def apply(state, journal):
    from ratsnestpro.orchestration import pipeline as p
    from ratsnestpro.repair.joint_candidate import fingerprint
    value = json.loads(Path(journal).read_text())
    if value['id'] in state.draft_execution.get('project_commits', []):
        return  # A later checkpoint already contains this transaction.
    updates = {p.PipelineStep(k): p.ARTIFACT_MODELS[p.PipelineStep(k)].model_validate(v)
               for k, v in value['updates'].items()}
    for step, updated in updates.items():
        current = fingerprint({step: state.artifact(step)})
        if current not in {value['source_artifacts'][step.value], fingerprint({step: updated})}:
            raise RuntimeError('project transaction conflicts with newer State')
    # Validate ALL files before replacing ANY. After interruption, roll forward
    # only if each file is still the original or this exact candidate.
    for item in value['files']:
        if digest(item['target']) not in {item['source_digest'], item['candidate_digest']}:
            raise RuntimeError('project transaction conflicts with newer file')
        if digest(item['candidate']) != item['candidate_digest']:
            raise RuntimeError('project transaction staging digest mismatch')
    for item in value['files']:
        target = Path(item['target'])
        if digest(target) != item['candidate_digest']:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_suffix(target.suffix + '.project-commit.tmp')
            shutil.copy2(item['candidate'], staging)
            staging.replace(target)
    state.artifacts.update(updates)
    state.draft_execution['manufacturing_refresh_required'] = True
    state.draft_execution['project_repair_validation'] = 'requires_main_release_validation'
    state.draft_execution.setdefault('project_commits', []).append(value['id'])


def commit(host, updates, paths):
    from ratsnestpro.repair.joint_candidate import fingerprint
    from ratsnestpro.repair.pipeline_adapter import _atomic_json
    if fingerprint(host.state.artifacts) != host.source_state_digest:
        raise RuntimeError('project State changed before commit')
    files = []
    for name in paths:
        source, candidate = host.live.parent / name, host.root / name
        if digest(source) != host.source_files.get(name):
            raise RuntimeError('project file changed before commit')
        files.append({'target': str(source), 'candidate': str(candidate),
                      'source_digest': digest(source), 'candidate_digest': digest(candidate)})
    journal = host.live.parent / '.strong-repair' / 'project-commit.json'
    value = {'files': files,
        'updates': {s.value: a.model_dump(mode='json') for s, a in updates.items()},
        'source_artifacts': {s.value: fingerprint({s: host.state.artifact(s)}) for s in updates}}
    value['id'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    _atomic_json(journal, value)
    apply(host.state, journal)

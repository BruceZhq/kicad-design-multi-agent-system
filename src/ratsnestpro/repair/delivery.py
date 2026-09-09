"""Non-release engineering handoff, separate from candidate commit authority."""
import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

from ratsnestpro.repair.handoff import EXCLUDED_DIRS, PRIVATE, SUFFIXES

MAX_ARCHIVE = 27_000_000
MAX_EXPANDED = 80_000_000


def build_delivery(root, *, outcome, assessment, overrides=None, findings=None):
    """Archive the retained best, never a partially executed sandbox or pass flag."""
    root = Path(root).resolve()
    sources = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(p in EXCLUDED_DIRS for p in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError('delivery cannot contain symlinks')
        if not path.is_file() or any(w in str(relative).lower() for w in PRIVATE):
            continue
        if path.name.startswith(('temporal_input-', 'handoff-', 'terra-repair-delivery')):
            continue
        if path.suffix not in SUFFIXES | {'.gbr', '.drl', '.gbrjob', '.pos'} and path.name not in {'fp-lib-table', 'sym-lib-table'}:
            continue
        sources[relative.as_posix()] = path
    sources.update(overrides or {})
    report = {
        'schema': 'ratsnest.repair-delivery.v1', 'release_ready': False,
        'status': 'repair_attempt_finished', 'session_outcome': outcome,
        'score': assessment.score, 'invariant_failures': assessment.invariant_failures,
        'repairable_failures': assessment.repairable_failures,
        'remaining_findings': findings or [],
        'notice': 'Best retained engineering snapshot, not manufacturing approval. Historical exports may be stale; see digest-bound current-checks reports.',
    }
    buffer, entries, total = io.BytesIO(), [], 0
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(sources.items()):
            path = Path(path)
            if not path.is_file() or path.is_symlink():
                raise ValueError('delivery source unavailable')
            size = path.stat().st_size
            total += size
            if size > 25_000_000 or total > MAX_EXPANDED or len(entries) >= 1024:
                raise ValueError('complete delivery exceeds archive limits; no partial archive emitted')
            data = path.read_bytes()
            archive.writestr(name, data)
            entries.append({'path': name, 'bytes': size, 'sha256': hashlib.sha256(data).hexdigest()})
        report['files'] = entries
        archive.writestr('repair-error-report.json', json.dumps(report, ensure_ascii=False, indent=2))
        archive.writestr('README.md', '# Repair handoff — NOT release-ready\n\nOpen the included .kicad_pro/.kicad_pcb. '
                          'See repair-error-report.json and current-checks/ for remaining errors. '
                          'This is the retained best, not necessarily the last attempted candidate. '
                          'Do not manufacture without independent acceptance.\n')
    raw = buffer.getvalue()
    if len(raw) > MAX_ARCHIVE:
        raise ValueError('complete delivery exceeds transport limit')
    return {'archive_data': base64.b64encode(raw).decode(),
            'sha256': hashlib.sha256(raw).hexdigest(), 'report': report}


def persist_delivery(root, delivery):
    """Save an untrusted remote archive without extracting or touching live CAD."""
    from ratsnestpro.repair.pipeline_adapter import _atomic_json
    raw = base64.b64decode(delivery['archive_data'], validate=True)
    if len(raw) > MAX_ARCHIVE or hashlib.sha256(raw).hexdigest() != delivery['sha256']:
        raise ValueError('repair delivery digest/size mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        from pathlib import PurePosixPath
        import stat
        total = 0
        names = set()
        for item in archive.infolist():
            p = PurePosixPath(item.filename)
            total += item.file_size
            if (p.is_absolute() or '..' in p.parts or ':' in item.filename or '\\' in item.filename
                    or total > MAX_EXPANDED or stat.S_ISLNK(item.external_attr >> 16)
                    or item.filename in names):
                raise ValueError('unsafe repair archive')
            names.add(item.filename)
        if len(archive.infolist()) > 1026:
            raise ValueError('repair archive has too many entries')
    root = Path(root)
    target = root / 'terra-repair-delivery.zip'
    pending = target.with_suffix('.zip.pending')
    pending.write_bytes(raw)
    pending.replace(target)
    report = {**delivery['report'], 'archive_sha256': delivery['sha256'], 'release_ready': False,
              'authority': 'external repair diagnostics; caller release checks remain authoritative'}
    _atomic_json(root / 'terra-repair-delivery.json', report)
    return target


def finish_with_issues(hardware, root):
    """Terminate interaction, not engineering truth; preserve all blockers."""
    from types import SimpleNamespace
    root = Path(root)
    # Also supports legacy tasks that predate external delivery bundles.
    if not (root / 'terra-repair-delivery.zip').is_file():
        assessment = SimpleNamespace(score=None, invariant_failures=[], repairable_failures=[])
        persist_delivery(root, build_delivery(root, assessment=assessment,
            outcome={'termination': 'user_finished', 'validation': 'retained reports; no new CAD validation'},
            findings=hardware.get('release_blockers', [])))
    paths = [str(root / name) for name in ('terra-repair-delivery.zip', 'terra-repair-delivery.json')]
    return {**hardware, 'user_ended_repair': True, 'release_ready': False,
            'outcome': 'delivered_with_issues', 'status': 'delivered_with_issues',
            'actual_files': list(dict.fromkeys([*hardware.get('actual_files', []), *paths]))}

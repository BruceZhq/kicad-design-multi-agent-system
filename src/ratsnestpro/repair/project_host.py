"""Joint schematic/PCB/program workspace with unchanged independent graders."""
import base64
import copy
import hashlib
import json
import os
import shutil
import re
import subprocess

import httpx

from ratsnestpro.repair.pipeline_adapter import _BoardHost
from ratsnestpro.repair.contracts import SandboxFile, SandboxRequest, SandboxResult
from ratsnestpro.repair.session import CandidateAssessment
from ratsnestpro.repair.a2a_contracts import portable
from ratsnestpro.repair.project_transaction import digest

PROGRAM = 'programs/repair_generator.py'


class ToolchainMismatch(RuntimeError):
    """Infrastructure capability mismatch; never spend model tokens on it."""


def check_toolchain(pcb, cli):
    with pcb.open(encoding='utf-8') as stream:
        header = stream.read(2048)
    required = re.search(r'\(generator_version\s+"(\d+)\.', header)
    if required and cli:
        version = subprocess.run([cli, 'version'], capture_output=True, text=True, timeout=10, check=True).stdout
        installed = re.search(r'(\d+)\.', version)
        if installed and int(required[1]) > int(installed[1]):
            raise ToolchainMismatch('PCB requires newer KiCad; align the pinned toolchain before repair')


class ProjectHost(_BoardHost):
    def __init__(self, state, ctx, record, *, joint=True):
        self.project_initialized = False
        super().__init__(state, ctx, record, joint=joint)
        p = self.p
        check_toolchain(self.live, p.kicad_cli_available())
        materialized = state.artifact(p.PipelineStep.SCH_MATERIALIZE)
        self.sch = self.pcb.with_suffix('.kicad_sch')
        if materialized is None or materialized.sch_path != str(self.live.with_suffix('.kicad_sch')):
            raise ValueError('project repair requires a paired existing schematic')
        shutil.copy2(materialized.sch_path, self.sch)
        evidence = self.live.parent / 'technical-evidence'
        if evidence.is_dir():
            if evidence.is_symlink() or any(f.is_symlink() for f in evidence.rglob('*')):
                raise ValueError('evidence snapshot must not contain symlinks')
            shutil.copytree(evidence, self.root / 'technical-evidence', dirs_exist_ok=True)
        self.view_state.artifacts = {s: type(a).model_validate(portable(a.model_dump(mode='json'), str(self.live.parent), str(self.root)))
                                     for s, a in state.artifacts.items()}
        self.tracked_names = (self.pcb.name, self.sch.name, PROGRAM, 'prepared-components.json', 'component-closure.json',
                              self.sch.with_suffix('.erc.json').name, self.sch.with_suffix('.netlist.xml').name, self.report.name)
        self.source_files = {name: digest(self.live.parent / name) for name in self.tracked_names}
        for name in ('prepared-components.json', 'component-closure.json'):
            source = self.live.parent / name
            if source.is_file():
                shutil.copy2(source, self.root / name)
        source_program = self.live.parent / PROGRAM
        if source_program.is_file():
            (self.root / PROGRAM).parent.mkdir(exist_ok=True)
            shutil.copy2(source_program, self.root / PROGRAM)
        self.sch_identity = self.schematic_identity()
        self.release_findings = []
        self.project_initialized = True
        self.checkpoint_candidate()

    def schematic_identity(self):
        from ratsnestpro.eda.vendor.schematic import Schematic
        from ratsnestpro.eda.vendor.sexpr import find_first
        document = Schematic.load(self.sch)
        components = sorted((c['reference'], c['value'], c['footprint'], c['lib_id'], c['dnp'])
                            for c in document.list_components() if not str(c['reference']).startswith('#'))
        # Editing embedded pin types is not a legitimate way to silence ERC.
        definitions = json.dumps(find_first(document.root, 'lib_symbols'), default=str)
        return components, hashlib.sha256(definitions.encode()).hexdigest()

    def observe(self):
        value = super().observe()
        value['project_repair'] = {
            'repair_instruction': getattr(self.ctx, 'repair_feedback', ''),
            'topology': self.view_state.artifact(self.p.PipelineStep.TOPOLOGY).model_dump(mode='json'),
            'schematic': self.sch.name, 'generator': PROGRAM,
            'release_findings': self.release_findings,
            'instructions': 'Your Python script may jointly edit the schematic and PCB in /work. Read repair-context.json and the supplied ERC/DRC reports; all describe the current candidate, not a fresh design. You may create/edit programs/repair_generator.py and run it there. Inspect /app/ratsnestpro source if needed; never alter production source. Preserve locked component identities and the required pin/net contract. Fix actual wires/labels/placement/copper, not reports. All file changes roll back together. Main system independently rebuilds publication outputs.',
        }
        return value

    def execute(self, script, timeout):
        paths = [self.pcb, self.sch, self.pcb.with_suffix('.kicad_pro'), self.pcb.with_suffix('.kicad_dru'), self.root / PROGRAM]
        paths.extend([self.report, self.sch.with_suffix('.erc.json')])
        files = [SandboxFile(path=x.relative_to(self.root).as_posix(), data=base64.b64encode(x.read_bytes()).decode())
                 for x in paths if x.is_file()]
        context = {'schema': 'repair-context.v1', 'requirement': self.state.requirement_text,
                   'repair_instruction': getattr(getattr(self, 'ctx', None), 'repair_feedback', ''),
                   'project_name': self.state.project_name, 'pcb': self.pcb.name,
                   'schematic': self.sch.name,
                   'artifacts': portable({s.value: a.model_dump(mode='json')
                                          for s, a in self.view_state.artifacts.items()}, str(self.root), '/work'),
                   'release_findings': self.release_findings,
                   'notice': 'Input evidence only. Modifying this context or a report cannot change independent validation.'}
        files.append(SandboxFile(path='repair-context.json',
                                data=base64.b64encode(json.dumps(context, ensure_ascii=False).encode()).decode()))
        request = SandboxRequest(files=files, script=script, pcb_name=self.pcb.name,
                                 timeout_seconds=timeout, return_paths=[self.sch.name, PROGRAM])
        endpoint = os.getenv('RATSNEST_REPAIR_EXECUTOR_URL', '').rstrip('/')
        token = os.getenv('RATSNEST_REPAIR_EXECUTOR_TOKEN', '')
        if not endpoint or len(token) < 32:
            raise RuntimeError('isolated executor is not configured')
        with httpx.Client(timeout=timeout + 30, trust_env=False) as client:
            response = client.post(endpoint + '/v1/repair', json=request.model_dump(), headers={'Authorization': 'Bearer ' + token})
            response.raise_for_status()
            result = SandboxResult.model_validate(response.json())
        if result.status == 'completed':
            self.stage(result.pcb_data, result.files)
        return {'status': result.status, 'output': result.output, 'exit_code': result.exit_code}

    def record_proposal(self, turn, proposal):
        directory = self.root / 'attempt-evidence' / str(turn)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'proposal.json').write_text(proposal.model_dump_json(), encoding='utf-8')
        (directory / 'repair.py').write_text(proposal.script or '', encoding='utf-8')

    def record_attempt(self, turn, proposal, assessment):
        """Retain private, pre-rollback diagnostics; never substitute for grading."""
        directory = self.root / 'attempt-evidence' / str(turn)
        self.record_proposal(turn, proposal)
        findings = []
        for source in (self.report, self.sch.with_suffix('.erc.json')):
            if source.exists():
                shutil.copy2(source, directory / source.name)
                report = json.loads(source.read_text(encoding='utf-8'))
                for kind in ('violations', 'unconnected_items'):
                    for item in report.get(kind, []):
                        findings.append({'type': item.get('type'), 'description': item.get('description'),
                                         'items': item.get('items', [])})
        return {'score': assessment.score, 'findings': findings[:20],
                'evidence_path': str(directory), 'finding_count': len(findings)}

    def record_rejection(self, proposal, error):
        # Private candidate diagnostics, excluded from A2A handoff and public
        # events. Preserve the actual rejected program, not only ValueError.
        with (self.root / 'candidate-rejections.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'proposal': proposal.model_dump(mode='json'),
                                     'error_type': type(error).__name__, 'error': str(error)[:3000]},
                                    ensure_ascii=False) + '\n')

    def stage(self, pcb_data, files):
        supplied = {f.path: base64.b64decode(f.data, validate=True) for f in files}
        if not pcb_data or self.sch.name not in supplied or not set(supplied) <= {self.sch.name, PROGRAM}:
            raise ValueError('incomplete or unauthorized joint candidate')
        supplied[self.pcb.name] = base64.b64decode(pcb_data, validate=True)
        if sum(map(len, supplied.values())) > 24_000_000:
            raise ValueError('joint candidate too large')
        for name, data in supplied.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.last = None
        self.workspace.images.clear()

    def revalidate(self):
        """Completion requests cannot reuse cached CAD/tool verdicts."""
        self._project_cache = None
        self.last = None
        return self.assess()

    def assess(self):
        # Board grader retains hard invariants; ERC and full retained step gates
        # are additional observations, never model-supplied pass flags.
        from ratsnestpro.repair.joint_candidate import fingerprint as state_digest
        def cache_key():
            return (digest(self.pcb), digest(self.sch), digest(self.root / PROGRAM), state_digest(self.view_state.artifacts))
        if getattr(self, '_project_cache', None) and self._project_cache[0] == cache_key():
            return self._project_cache[1]
        self.last = None
        board = super().assess()
        p = self.p
        from ratsnestpro.repair.joint_candidate import synchronize_placements
        synchronize_placements(self)
        plan = self.view_state.artifact(p.PipelineStep.LAYOUT_GENERAL)
        write = self.view_state.artifact(p.PipelineStep.LAYOUT_WRITE)
        if plan is not None:
            overlaps, outside = p._placement_geometry_violations(self.view_state, plan)
            self.view_state.artifacts[p.PipelineStep.LAYOUT_WRITE] = write.model_copy(update={'overlaps': overlaps, 'out_of_bounds': outside})
        ctx = copy.copy(self.ctx)
        ctx.out_dir, ctx.draft_first = str(self.root), True
        erc, _ = p.ErcStep().propose(self.view_state, ctx, '')
        self.view_state.artifacts[p.PipelineStep.ERC] = erc
        route = self.view_state.artifact(p.PipelineStep.ROUTE_SIGNALS)
        if route is not None:
            self.view_state.artifacts[p.PipelineStep.ROUTE_SIGNALS] = p._synchronize_route_result_with_drc(route, p._read_drc_snapshot(self.report))
        fab, _ = p.RouteFabStep().propose(self.view_state, ctx, '')
        self.view_state.artifacts[p.PipelineStep.ROUTE_FAB] = fab
        # Manufacture is regenerated only after physical checks are clean.
        physically_clean = not board.errors and not board.unconnected and erc.cli_ran and erc.cli_error_count == 0
        if physically_clean:
            manufactured, _ = p.ManufactureStep().propose(self.view_state, ctx, '')
            self.view_state.artifacts[p.PipelineStep.MANUFACTURE] = manufactured
        findings = []
        for step in p.ALL_STEPS:
            if step.step == p.PipelineStep.MANUFACTURE and not physically_clean:
                continue
            artifact = self.view_state.artifact(step.step)
            if artifact is None:
                findings.append({'step': step.step.value, 'error': 'missing retained artifact'})
                continue
            findings.extend({'step': step.step.value, 'error': c.message} for c in step.check(self.view_state, artifact)
                            if not c.ok and c.severity == p.Severity.ERROR)
        self.release_findings = findings
        violations = list(board.invariant_failures)
        # Missing independent documentation still counts as a release error,
        # but must not discard an otherwise improving CAD candidate. Missing
        # or incompatible local assets remain immutable admission failures.
        from ratsnestpro.repair.draft import deferred_checks
        selected = self.view_state.artifact(p.PipelineStep.SELECTION)
        selection_checks = p.ALL_STEPS[p._ORDER_INDEX[p.PipelineStep.SELECTION]].check(self.view_state, selected)
        for check in deferred_checks(p.PipelineStep.SELECTION, selected, selection_checks):
            if (not check.ok and not check.blocks_execution
                    and check.name == 'prepared_component_manifest'
                    and check.reason_code == 'independent_package_evidence_missing'):
                violations = [v for v in violations if v != 'selection:prepared_component_manifest']
        if self.schematic_identity() != self.sch_identity:
            violations.append('locked schematic component identities changed')
        fingerprint = hashlib.sha256(json.dumps([digest(self.pcb), digest(self.sch), digest(self.root / PROGRAM), state_digest(self.view_state.artifacts)], sort_keys=True).encode()).hexdigest()
        result = CandidateAssessment(fingerprint, board.errors + len(findings) + int(erc.cli_error_count or 0), board.unconnected, board.warnings, tuple(violations), board.repairable_failures)
        self._project_cache = (cache_key(), result)
        return result

    def images(self):
        from ratsnestpro.orchestration.engineering_workspace import EngineeringQuery
        if not self.workspace.images:
            super().images()
            self.workspace.observe(EngineeringQuery(tool='render', path=str(self.sch)))
        images = list(self.workspace.images.values())
        return [images[0], images[-1]] if len(images) > 1 else images

    def checkpoint_candidate(self):
        super().checkpoint_candidate()
        if not self.project_initialized:
            return
        self.best_project = {name: (self.root / name).read_bytes() for name in self.tracked_names
                             if name != self.pcb.name and (self.root / name).is_file()}

    def rollback_candidate(self):
        super().rollback_candidate()
        if not self.project_initialized:
            return
        for name in self.tracked_names:
            if name == self.pcb.name:
                continue
            path = self.root / name
            if name in self.best_project:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.best_project[name])
            elif path.is_file():
                path.unlink()  # Only candidate-local generated files, never user data.

    def commit(self, expected_live_fingerprint):
        from ratsnestpro.repair.project_transaction import commit
        if self.assess().invariant_failures:
            raise RuntimeError('joint candidate violates immutable constraints')
        updates = {s: type(a).model_validate(portable(a.model_dump(mode='json'), str(self.root), str(self.live.parent)))
                   for s, a in self.view_state.artifacts.items()}
        # Manufacturing files are rebuilt by the main pipeline, not accepted from the agent.
        updates.pop(self.p.PipelineStep.MANUFACTURE, None)
        paths = [name for name in self.tracked_names if (self.root / name).is_file()]
        commit(self, updates, paths)

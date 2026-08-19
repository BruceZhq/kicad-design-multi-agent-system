package dev.ratsnest.api;

import dev.ratsnest.artifact.RunArtifact;
import dev.ratsnest.artifact.RunArtifactService;
import dev.ratsnest.approval.RunApprovalService;
import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.core.DesignPlanService;
import dev.ratsnest.core.RunDispatchService;
import dev.ratsnest.core.RunResultService;
import dev.ratsnest.core.RunSubmissionService;
import dev.ratsnest.security.RunAccessPolicy;
import dev.ratsnest.security.ServiceAccessPolicy;
import dev.ratsnest.tenant.HardwareProject;
import dev.ratsnest.tenant.TenantAccessService;
import org.springframework.core.io.InputStreamResource;
import org.springframework.core.io.Resource;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Stream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

@RestController
@RequestMapping("/api")
public class RunController {

    private static final Set<String> BACKENDS = Set.of("template", "crew", "mcp");

    public record CreateRunRequest(
            @jakarta.validation.constraints.NotBlank String projectDir,
            @jakarta.validation.constraints.Min(1)
            @jakarta.validation.constraints.Max(10) Integer maxIterations,
            String projectId) {}

    public record CreateDesignRequest(
            @jakarta.validation.constraints.NotBlank
            @jakarta.validation.constraints.Size(max = 500) String requirement,
            @jakarta.validation.constraints.Min(1)
            @jakarta.validation.constraints.Max(10) Integer maxIterations,
            String backend,
            String projectId) {}

    private final DesignRunRepository runs;
    private final RunDispatchService dispatch;
    private final RunSubmissionService submission;
    private final RunAccessPolicy access;
    private final TenantAccessService tenants;
    private final RunArtifactService artifacts;
    private final ServiceAccessPolicy serviceAccess;
    private final RunApprovalService approvals;
    private final DesignPlanService plans;
    private final RunResultService results;

    public RunController(DesignRunRepository runs, RunDispatchService dispatch,
                         RunSubmissionService submission,
                         RunAccessPolicy access, TenantAccessService tenants,
                         RunArtifactService artifacts,
                         ServiceAccessPolicy serviceAccess,
                         RunApprovalService approvals,
                         DesignPlanService plans,
                         RunResultService results) {
        this.runs = runs;
        this.dispatch = dispatch;
        this.submission = submission;
        this.access = access;
        this.tenants = tenants;
        this.artifacts = artifacts;
        this.serviceAccess = serviceAccess;
        this.approvals = approvals;
        this.plans = plans;
        this.results = results;
    }

    // -- create ---------------------------------------------------------------

    @PostMapping("/runs")
    public ResponseEntity<Map<String, String>> create(
            @jakarta.validation.Valid @RequestBody CreateRunRequest req,
            @RequestHeader(value = "Idempotency-Key", required = false)
            String idempotencyKey) {
        int maxIter = req.maxIterations() == null ? 4 : req.maxIterations();
        HardwareProject project = tenants.resolveProject(req.projectId());
        DesignRun existing = findIdempotent(project, idempotencyKey);
        if (existing != null) {
            return accepted(existing);
        }
        DesignRun run = DesignRun.create(req.projectDir(), maxIter);
        assignOwnership(run, project, idempotencyKey);
        submission.submit(run);
        return accepted(run);
    }

    /** Design generation: requirement + backend in, verified board out. */
    @PostMapping("/designs")
    public ResponseEntity<Map<String, String>> createDesign(
            @jakarta.validation.Valid @RequestBody CreateDesignRequest req,
            @RequestHeader(value = "Idempotency-Key", required = false)
            String idempotencyKey) {
        int maxIter = req.maxIterations() == null ? 4 : req.maxIterations();
        String backend = (req.backend() == null || req.backend().isBlank())
                ? "crew" : req.backend().toLowerCase();
        if (!BACKENDS.contains(backend)) {
            throw new IllegalArgumentException(
                    "backend must be one of template, crew, mcp");
        }
        HardwareProject project = tenants.resolveProject(req.projectId());
        DesignRun existing = findIdempotent(project, idempotencyKey);
        if (existing != null) {
            return accepted(existing);
        }
        String projectDir = System.getProperty("java.io.tmpdir")
                + "/ratsnest-designs/" + UUID.randomUUID();
        DesignRun run = DesignRun.createDesign(
                req.requirement(), projectDir, maxIter, backend);
        assignOwnership(run, project, idempotencyKey);
        submission.submit(run);
        return accepted(run);
    }

    /** Worker callback (kafka dispatch mode): RunRecord JSON in, row updated.
     *  Authenticated by the service token filter, not user JWT. */
    @PutMapping("/runs/{id}/result")
    public ResponseEntity<Map<String, String>> putResult(
            @PathVariable String id, @RequestBody String runRecordJson) {
        serviceAccess.requireServiceOrOpenMode();
        try {
            DesignRun run = results.accept(id, runRecordJson);
            if ("design".equals(run.getKind())
                    && !"failed".equals(run.getStatus())) {
                run = results.requestReleaseReview(id);
            }
            return ResponseEntity.ok(Map.of("status", run.getStatus()));
        } catch (IllegalStateException e) {
            String status = runs.findById(id).map(DesignRun::getStatus)
                    .orElse("unknown");
            return ResponseEntity.status(HttpStatus.CONFLICT)
                    .body(Map.of("status", status, "error", e.getMessage()));
        }
    }

    /** Planning worker callback. The first valid plan becomes immutable. */
    @PutMapping("/runs/{id}/plan")
    public ResponseEntity<Map<String, String>> putPlan(
            @PathVariable String id, @RequestBody String planJson) {
        serviceAccess.requireServiceOrOpenMode();
        DesignRun saved = plans.apply(id, planJson);
        return ResponseEntity.ok(Map.of(
                "status", saved.getStatus(),
                "subjectSha256", saved.getPlanSha256()));
    }

    // -- read -----------------------------------------------------------------

    /** Bounded list, newest first. Non-admin users see only their own runs. */
    @GetMapping("/runs")
    public List<DesignRun> list(@RequestParam(defaultValue = "0") int page,
                                @RequestParam(defaultValue = "100") int size) {
        PageRequest pr = PageRequest.of(Math.max(0, page),
                Math.min(Math.max(1, size), 200),
                Sort.by(Sort.Direction.DESC, "createdAt"));
        String user = access.currentUser();
        if (user == null || access.currentIsAdmin()) {
            return runs.findAll(pr).getContent();       // open mode / admin
        }
        List<String> organizationIds = tenants.currentOrganizationIds();
        if (organizationIds.isEmpty()) {
            return runs.findByOwner(user, pr).getContent();
        }
        return runs.findVisibleToUser(organizationIds, user, pr).getContent();
    }

    @GetMapping("/runs/{id}")
    public ResponseEntity<DesignRun> get(@PathVariable String id) {
        return runs.findById(id)
                .filter(access::canAccess)
                .map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }

    @GetMapping("/runs/{id}/plan")
    public ResponseEntity<DesignPlanService.PlanView> plan(
            @PathVariable String id) {
        DesignRun run = runs.findById(id).filter(access::canAccess)
                .orElse(null);
        if (run == null) {
            return ResponseEntity.notFound().build();
        }
        return plans.view(run).map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }

    /** Download the full generated KiCad project (sch/pcb/pro + report +
     *  previews) as a zip. Local-dispatch: the project lives on this host.
     *  (Cluster/kafka mode moves this behind artifact storage — Phase 3.) */
    @GetMapping("/runs/{id}/download")
    public ResponseEntity<Resource> download(@PathVariable String id) {
        DesignRun run = runs.findById(id).filter(access::canAccess)
                .orElse(null);
        if (run == null || run.getProjectDir() == null) {
            return ResponseEntity.notFound().build();
        }
        if ("review_pending".equals(run.getReleaseStatus())
                || "rejected".equals(run.getReleaseStatus())) {
            return ResponseEntity.status(HttpStatus.CONFLICT).build();
        }
        RunArtifact artifact = artifacts.projectFor(run.getId()).orElse(null);
        if (artifact != null) {
            try {
                return ResponseEntity.ok()
                        .header(HttpHeaders.CONTENT_DISPOSITION,
                                "attachment; filename=\"" + artifact.getFilename()
                                        + "\"")
                        .contentLength(artifact.getSizeBytes())
                        .contentType(MediaType.parseMediaType(
                                artifact.getContentType()))
                        .body(new InputStreamResource(artifacts.open(artifact)));
            } catch (IOException e) {
                return ResponseEntity.internalServerError().build();
            }
        }
        Path dir = Path.of(run.getProjectDir());
        if (!Files.isDirectory(dir)) {
            return ResponseEntity.status(HttpStatus.GONE).build();
        }
        try {
            byte[] zip = zipDirectory(dir);
            String name = dir.getFileName() + ".zip";
            return ResponseEntity.ok()
                    .header(HttpHeaders.CONTENT_DISPOSITION,
                            "attachment; filename=\"" + name + "\"")
                    .contentType(MediaType.parseMediaType("application/zip"))
                    .body(new InputStreamResource(new ByteArrayInputStream(zip)));
        } catch (IOException e) {
            return ResponseEntity.internalServerError().build();
        }
    }

    /** Read-only SVG preview: which = sch | pcb | step_NN_label (execution
     *  timeline frames emitted by the creator crew after each agent action). */
    @GetMapping("/runs/{id}/preview/{which}")
    public ResponseEntity<Resource> preview(@PathVariable String id,
                                            @PathVariable String which) {
        DesignRun run = runs.findById(id).filter(access::canAccess)
                .orElse(null);
        if (run == null || run.getProjectDir() == null
                || !which.matches("sch|pcb|step_[A-Za-z0-9_\\-]{1,60}")) {
            return ResponseEntity.notFound().build();
        }
        String entryName = which.startsWith("step_")
                ? "preview/steps/" + which + ".svg"
                : "preview/" + which + ".svg";
        try {
            var stored = artifacts.readProjectEntry(run.getId(), entryName);
            if (stored.isPresent()) {
                return ResponseEntity.ok()
                        .contentType(MediaType.valueOf("image/svg+xml"))
                        .header(HttpHeaders.CACHE_CONTROL, "no-cache")
                        .body(new InputStreamResource(
                                new ByteArrayInputStream(stored.get())));
            }
        } catch (IOException e) {
            return ResponseEntity.internalServerError().build();
        }
        Path svg = which.startsWith("step_")
                ? Path.of(run.getProjectDir(), "preview", "steps", which + ".svg")
                : Path.of(run.getProjectDir(), "preview", which + ".svg");
        if (!Files.isRegularFile(svg)) {
            return ResponseEntity.notFound().build();
        }
        try {
            byte[] body = Files.readAllBytes(svg);
            return ResponseEntity.ok()
                    .contentType(MediaType.valueOf("image/svg+xml"))
                    .header(HttpHeaders.CACHE_CONTROL, "no-cache")
                    .body(new InputStreamResource(new ByteArrayInputStream(body)));
        } catch (IOException e) {
            return ResponseEntity.internalServerError().build();
        }
    }

    /** Execution timeline frames available for a run, in step order. */
    @GetMapping("/runs/{id}/steps")
    public ResponseEntity<List<String>> steps(@PathVariable String id) {
        DesignRun run = runs.findById(id).filter(access::canAccess).orElse(null);
        if (run == null || run.getProjectDir() == null) {
            return ResponseEntity.notFound().build();
        }
        if (artifacts.projectFor(run.getId()).isPresent()) {
            try {
                return ResponseEntity.ok(artifacts.listProjectEntries(
                                run.getId(), "preview/steps/", ".svg").stream()
                        .map(name -> name.substring(
                                "preview/steps/".length(), name.length() - 4))
                        .toList());
            } catch (IOException e) {
                return ResponseEntity.internalServerError().build();
            }
        }
        Path dir = Path.of(run.getProjectDir(), "preview", "steps");
        if (!Files.isDirectory(dir)) {
            return ResponseEntity.ok(List.of());
        }
        try (Stream<Path> files = Files.list(dir)) {
            return ResponseEntity.ok(files
                    .map(p -> p.getFileName().toString())
                    .filter(n -> n.endsWith(".svg"))
                    .map(n -> n.substring(0, n.length() - 4))
                    .sorted()
                    .toList());
        } catch (IOException e) {
            return ResponseEntity.ok(List.of());
        }
    }

    /** The kicad-happy style design report (markdown). */
    @GetMapping("/runs/{id}/report")
    public ResponseEntity<String> report(@PathVariable String id) {
        DesignRun run = runs.findById(id).filter(access::canAccess).orElse(null);
        if (run == null || run.getProjectDir() == null) {
            return ResponseEntity.notFound().build();
        }
        try {
            var stored = artifacts.readProjectEntry(
                    run.getId(), "ratsnest_report.md");
            if (stored.isPresent()) {
                return ResponseEntity.ok()
                        .contentType(MediaType.valueOf(
                                "text/markdown;charset=UTF-8"))
                        .body(new String(stored.get(),
                                java.nio.charset.StandardCharsets.UTF_8));
            }
        } catch (IOException e) {
            return ResponseEntity.internalServerError().build();
        }
        Path md = Path.of(run.getProjectDir(), "ratsnest_report.md");
        if (!Files.isRegularFile(md)) {
            return ResponseEntity.notFound().build();
        }
        try {
            return ResponseEntity.ok()
                    .contentType(MediaType.valueOf("text/markdown;charset=UTF-8"))
                    .body(Files.readString(md));
        } catch (IOException e) {
            return ResponseEntity.internalServerError().build();
        }
    }

    // -- helpers ----------------------------------------------------------------

    private void assignOwnership(DesignRun run, HardwareProject project,
                                 String idempotencyKey) {
        String owner = access.currentUser();
        run.setOwner(owner);
        String userId = tenants.currentUser().map(
                dev.ratsnest.auth.UserAccount::getId).orElse(null);
        run.assignProject(project, userId);
        run.setIdempotencyKey(validateIdempotencyKey(idempotencyKey));
    }

    private DesignRun findIdempotent(HardwareProject project,
                                     String idempotencyKey) {
        String key = validateIdempotencyKey(idempotencyKey);
        if (key == null) {
            return null;
        }
        if (project != null) {
            return runs.findByOrganizationIdAndIdempotencyKey(
                    project.getOrganizationId(), key).orElse(null);
        }
        String owner = access.currentUser();
        return owner == null ? null
                : runs.findByOwnerAndIdempotencyKey(owner, key).orElse(null);
    }

    private static String validateIdempotencyKey(String value) {
        if (value == null || value.isBlank()) {
            return null;
        }
        String key = value.trim();
        if (!key.matches("[A-Za-z0-9._:-]{8,128}")) {
            throw new IllegalArgumentException(
                    "Idempotency-Key must be 8-128 safe ASCII characters");
        }
        return key;
    }

    private static ResponseEntity<Map<String, String>> accepted(DesignRun run) {
        java.util.HashMap<String, String> body = new java.util.HashMap<>();
        body.put("runId", run.getId());
        body.put("status", run.getStatus());
        if (run.getBackend() != null) body.put("backend", run.getBackend());
        if (run.getProjectDir() != null) body.put("projectDir", run.getProjectDir());
        if (run.getProjectId() != null) body.put("projectId", run.getProjectId());
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(body);
    }

    private static byte[] zipDirectory(Path dir) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        try (ZipOutputStream zos = new ZipOutputStream(buffer);
             Stream<Path> walk = Files.walk(dir)) {
            walk.filter(Files::isRegularFile).forEach(file -> {
                try {
                    zos.putNextEntry(new ZipEntry(dir.relativize(file).toString()
                            .replace('\\', '/')));
                    Files.copy(file, zos);
                    zos.closeEntry();
                } catch (IOException ignored) {
                    // skip unreadable/locked files
                }
            });
        }
        return buffer.toByteArray();
    }
}

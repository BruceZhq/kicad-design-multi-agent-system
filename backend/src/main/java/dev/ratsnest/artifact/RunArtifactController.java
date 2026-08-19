package dev.ratsnest.artifact;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.approval.RunApprovalService;
import dev.ratsnest.security.RunAccessPolicy;
import dev.ratsnest.security.ServiceAccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.io.IOException;
import java.util.List;

@RestController
@RequestMapping("/api/runs/{runId}/artifacts")
public class RunArtifactController {

    public record ArtifactView(String id, String kind, String filename,
                               long sizeBytes, String sha256,
                               java.time.Instant createdAt) {
        static ArtifactView from(RunArtifact artifact) {
            return new ArtifactView(artifact.getId(), artifact.getKind(),
                    artifact.getFilename(), artifact.getSizeBytes(),
                    artifact.getSha256(), artifact.getCreatedAt());
        }
    }

    private final DesignRunRepository runs;
    private final RunArtifactService artifacts;
    private final RunAccessPolicy access;
    private final ServiceAccessPolicy serviceAccess;
    private final RunApprovalService approvals;

    public RunArtifactController(DesignRunRepository runs,
                                 RunArtifactService artifacts,
                                 RunAccessPolicy access,
                                 ServiceAccessPolicy serviceAccess,
                                 RunApprovalService approvals) {
        this.runs = runs;
        this.artifacts = artifacts;
        this.access = access;
        this.serviceAccess = serviceAccess;
        this.approvals = approvals;
    }

    @PutMapping(value = "/project", consumes = "application/zip")
    public ResponseEntity<ArtifactView> uploadProject(
            @PathVariable String runId, HttpServletRequest request)
            throws IOException {
        serviceAccess.requireServiceOrOpenMode();
        DesignRun run = runs.findById(runId).orElse(null);
        if (run == null) {
            return ResponseEntity.notFound().build();
        }
        if ("design".equals(run.getKind())
                && !approvals.isApproved(
                runId, RunApprovalService.BOARD_PLAN)) {
            return ResponseEntity.status(HttpStatus.CONFLICT).build();
        }
        String filename = request.getHeader("X-Artifact-Filename");
        RunArtifact artifact = artifacts.storeProject(
                run, request.getInputStream(), filename);
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(ArtifactView.from(artifact));
    }

    @GetMapping
    public ResponseEntity<List<ArtifactView>> list(
            @PathVariable String runId) {
        return runs.findById(runId).filter(access::canAccess)
                .map(run -> ResponseEntity.ok(artifacts.list(runId).stream()
                        .map(ArtifactView::from).toList()))
                .orElse(ResponseEntity.notFound().build());
    }
}

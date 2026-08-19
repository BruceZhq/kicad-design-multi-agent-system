package dev.ratsnest.approval;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.core.DesignPlanService;
import dev.ratsnest.security.RunAccessPolicy;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.io.IOException;
import java.util.List;

@RestController
@RequestMapping("/api/runs/{runId}")
public class RunApprovalController {

    public record DecisionRequest(
            @NotBlank String decision,
            @Size(max = 1000) String comment) {}
    public record ApprovalView(
            String id, String runId, String type, String status,
            String subjectSha256, java.time.Instant requestedAt,
            java.time.Instant decidedAt, String decidedBy, String comment) {
        static ApprovalView from(RunApproval approval) {
            return new ApprovalView(approval.getId(), approval.getRunId(),
                    approval.getType(), approval.getStatus(),
                    approval.getSubjectSha256(), approval.getRequestedAt(),
                    approval.getDecidedAt(), approval.getDecidedBy(),
                    approval.getComment());
        }
    }

    private final DesignRunRepository runs;
    private final RunApprovalService approvals;
    private final RunApprovalWorkflowService workflow;
    private final DesignPlanService plans;
    private final RunAccessPolicy access;

    public RunApprovalController(DesignRunRepository runs,
                                 RunApprovalService approvals,
                                 RunApprovalWorkflowService workflow,
                                 DesignPlanService plans,
                                 RunAccessPolicy access) {
        this.runs = runs;
        this.approvals = approvals;
        this.workflow = workflow;
        this.plans = plans;
        this.access = access;
    }

    @GetMapping("/approval")
    public ResponseEntity<ApprovalView> get(@PathVariable String runId) {
        DesignRun run = accessible(runId);
        if (run == null) {
            return ResponseEntity.notFound().build();
        }
        return approvals.activeApproval(runId).map(ApprovalView::from)
                .map(ResponseEntity::ok)
                .orElse(ResponseEntity.noContent().build());
    }

    @GetMapping("/approvals")
    public ResponseEntity<List<ApprovalView>> list(
            @PathVariable String runId) {
        DesignRun run = accessible(runId);
        if (run == null) {
            return ResponseEntity.notFound().build();
        }
        return ResponseEntity.ok(approvals.approvals(runId).stream()
                .map(ApprovalView::from).toList());
    }

    @PostMapping("/approval")
    public ResponseEntity<ApprovalView> decide(
            @PathVariable String runId,
            @Valid @RequestBody DecisionRequest request) {
        DesignRun run = accessible(runId);
        if (run == null) {
            return ResponseEntity.notFound().build();
        }
        if (!access.canApprove(run)) {
            return ResponseEntity.notFound().build();
        }
        String actor = access.currentUser() == null
                ? "open-mode-engineer" : access.currentUser();
        RunApproval active = approvals.activeApproval(runId).orElseThrow(() ->
                new org.springframework.web.server.ResponseStatusException(
                        org.springframework.http.HttpStatus.CONFLICT,
                        "run has no pending approval"));
        return ResponseEntity.ok(ApprovalView.from(workflow.decide(
                run.getId(), active.getType(), request.decision().toLowerCase(),
                actor, request.comment())));
    }

    @PostMapping("/approvals/{type}")
    public ResponseEntity<ApprovalView> decideType(
            @PathVariable String runId,
            @PathVariable String type,
            @Valid @RequestBody DecisionRequest request) {
        DesignRun run = accessible(runId);
        if (run == null || !access.canApprove(run)) {
            return ResponseEntity.notFound().build();
        }
        String actor = access.currentUser() == null
                ? "open-mode-engineer" : access.currentUser();
        return ResponseEntity.ok(ApprovalView.from(workflow.decide(
                run.getId(), type, request.decision().toLowerCase(), actor,
                request.comment())));
    }

    @GetMapping(value = "/board-plan", produces = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<String> boardPlan(@PathVariable String runId)
            throws IOException {
        DesignRun run = accessible(runId);
        if (run == null) {
            return ResponseEntity.notFound().build();
        }
        var planned = plans.boardPlanJson(run);
        if (planned.isPresent()) {
            return ResponseEntity.ok(planned.get());
        }
        return approvals.boardPlanJson(run).map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }

    private DesignRun accessible(String runId) {
        return runs.findById(runId).filter(access::canAccess).orElse(null);
    }
}

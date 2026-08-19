package dev.ratsnest.approval;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.ratsnest.artifact.RunArtifactService;
import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.Optional;
import java.util.List;
import java.util.Set;

@Service
public class RunApprovalService {

    public static final String BOARD_PLAN = "board_plan";
    public static final String DESIGN_RELEASE = "design_release";
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Set<String> PRODUCTION_GATES = Set.of(
            "catalog", "bom", "erc", "drc", "spice", "thermal", "emc");

    public record DecisionResult(
            RunApproval approval, DesignRun run, boolean changed) {}

    private final RunApprovalRepository approvals;
    private final DesignRunRepository runs;
    private final RunArtifactService artifacts;

    public RunApprovalService(RunApprovalRepository approvals,
                              DesignRunRepository runs,
                              RunArtifactService artifacts) {
        this.approvals = approvals;
        this.runs = runs;
        this.artifacts = artifacts;
    }

    @Transactional
    public Optional<RunApproval> ensurePlanReview(DesignRun run) {
        if (!"design".equals(run.getKind()) || run.getPlanSha256() == null) {
            return Optional.empty();
        }
        Optional<RunApproval> existing = approvals.findByRunIdAndType(
                run.getId(), BOARD_PLAN);
        if (existing.isPresent()) {
            return existing;
        }
        return Optional.of(approvals.save(RunApproval.pending(
                run.getId(), run.getOrganizationId(), BOARD_PLAN,
                run.getPlanSha256())));
    }

    @Transactional
    public Optional<RunApproval> ensureReleaseReview(DesignRun run) {
        if (!"design".equals(run.getKind())
                || !isApproved(run.getId(), BOARD_PLAN)) {
            return Optional.empty();
        }
        if (!isReleaseEligible(run)) {
            run.setReleaseStatus("blocked");
            return Optional.empty();
        }
        byte[] subject;
        try {
            subject = reviewSubject(run).orElse(null);
        } catch (IOException e) {
            return Optional.empty();
        }
        if (subject == null) {
            return Optional.empty();
        }
        Optional<RunApproval> existing = approvals.findByRunIdAndType(
                run.getId(), DESIGN_RELEASE);
        if (existing.isPresent()) {
            String status = existing.get().getStatus();
            run.setReleaseStatus("pending".equals(status)
                    ? "review_pending" : status);
            return existing;
        }
        RunApproval approval = approvals.save(RunApproval.pending(
                run.getId(), run.getOrganizationId(), DESIGN_RELEASE,
                sha256(subject)));
        run.setReleaseStatus("review_pending");
        return Optional.of(approval);
    }

    public Optional<RunApproval> releaseApproval(String runId) {
        return approvals.findByRunIdAndType(runId, DESIGN_RELEASE);
    }

    public Optional<RunApproval> approval(String runId, String type) {
        requireType(type);
        return approvals.findByRunIdAndType(runId, type);
    }

    public List<RunApproval> approvals(String runId) {
        return approvals.findByRunIdOrderByRequestedAtAsc(runId);
    }

    public Optional<RunApproval> activeApproval(String runId) {
        return releaseApproval(runId).or(() ->
                approvals.findByRunIdAndType(runId, BOARD_PLAN));
    }

    public boolean isApproved(String runId, String type) {
        return approvals.findByRunIdAndType(runId, type)
                .map(value -> "approved".equals(value.getStatus()))
                .orElse(false);
    }

    public boolean isReleaseEligible(DesignRun run) {
        return "design".equals(run.getKind())
                && "crew".equals(run.getBackend())
                && "ratsnest.design-plan.v2".equals(run.getPlanContractVersion())
                && "converged".equals(run.getStatus())
                && hasPassedProductionGates(run.getResultJson());
    }

    public boolean hasPassedProductionGates(String resultJson) {
        if (resultJson == null || resultJson.isBlank()) {
            return false;
        }
        try {
            JsonNode record = MAPPER.readTree(resultJson);
            JsonNode iterations = record.path("iterations");
            if (!iterations.isArray() || iterations.isEmpty()) {
                return false;
            }
            JsonNode scorecard = iterations.get(iterations.size() - 1)
                    .path("scorecard");
            if (!scorecard.path("required_gates_passed").asBoolean(false)) {
                return false;
            }
            JsonNode gates = scorecard.path("gate_results");
            if (!gates.isObject()) {
                return false;
            }
            for (String name : PRODUCTION_GATES) {
                JsonNode gate = gates.path(name);
                if (!"passed".equals(gate.path("status").asText())
                        || !gate.path("required").asBoolean(true)) {
                    return false;
                }
            }
            return true;
        } catch (Exception ignored) {
            return false;
        }
    }

    public Optional<String> boardPlanJson(DesignRun run) throws IOException {
        Optional<byte[]> stored = artifacts.readProjectEntry(
                run.getId(), "boardplan.json");
        if (stored.isPresent()) {
            return Optional.of(new String(stored.get(), StandardCharsets.UTF_8));
        }
        if (run.getProjectDir() == null) {
            return Optional.empty();
        }
        Path file = Path.of(run.getProjectDir(), "boardplan.json");
        return Files.isRegularFile(file)
                ? Optional.of(Files.readString(file)) : Optional.empty();
    }

    @Transactional
    public DecisionResult decide(String runId, String type, String decision,
                                 String actor, String comment) {
        DesignRun run = runs.findLockedById(runId).orElseThrow(() ->
                new ResponseStatusException(HttpStatus.NOT_FOUND));
        requireType(type);
        if (!decision.matches("approved|rejected")) {
            throw new IllegalArgumentException(
                    "decision must be approved or rejected");
        }
        if (DESIGN_RELEASE.equals(type)
                && "approved".equals(decision)
                && !isReleaseEligible(run)) {
            throw new ResponseStatusException(
                    HttpStatus.CONFLICT,
                    "production release gates have not all passed");
        }
        RunApproval approval = approval(run.getId(), type)
                .orElseGet(() -> ensureReview(run, type).orElseThrow(() ->
                        new ResponseStatusException(HttpStatus.CONFLICT,
                                "run has no reviewable approval subject")));
        if (!"pending".equals(approval.getStatus())) {
            if (decision.equals(approval.getStatus())) {
                return new DecisionResult(approval, run, false);
            }
            throw new ResponseStatusException(HttpStatus.CONFLICT,
                    "approval decision is immutable");
        }
        approval.decide(decision, actor,
                comment == null ? null : comment.trim());
        if (BOARD_PLAN.equals(type)) {
            if ("approved".equals(decision)) {
                run.setPlanApprovedAt(java.time.Instant.now());
                run.setStatus("plan_approved");
            } else {
                run.setStatus("plan_rejected");
                run.setFinishedAt(java.time.Instant.now());
            }
        } else {
            run.setReleaseStatus(decision);
        }
        runs.save(run);
        return new DecisionResult(approvals.save(approval), run, true);
    }

    public DecisionResult decide(DesignRun run, String type, String decision,
                                 String actor, String comment) {
        return decide(run.getId(), type, decision, actor, comment);
    }

    /** Compatibility wrapper for the original release-only API. */
    public RunApproval decide(DesignRun run, String decision, String actor,
                              String comment) {
        return decide(run.getId(), DESIGN_RELEASE, decision, actor, comment)
                .approval();
    }

    private Optional<RunApproval> ensureReview(DesignRun run, String type) {
        return BOARD_PLAN.equals(type)
                ? ensurePlanReview(run) : ensureReleaseReview(run);
    }

    private static void requireType(String type) {
        if (!BOARD_PLAN.equals(type) && !DESIGN_RELEASE.equals(type)) {
            throw new IllegalArgumentException(
                    "approval type must be board_plan or design_release");
        }
    }

    private Optional<byte[]> reviewSubject(DesignRun run) throws IOException {
        for (String entry : new String[]{"boardplan.json", "designspec.json"}) {
            Optional<byte[]> stored = artifacts.readProjectEntry(run.getId(), entry);
            if (stored.isPresent()) {
                return stored;
            }
        }
        if (run.getProjectDir() != null) {
            for (String name : new String[]{"boardplan.json", "designspec.json"}) {
                Path file = Path.of(run.getProjectDir(), name);
                if (Files.isRegularFile(file)) {
                    return Optional.of(Files.readAllBytes(file));
                }
            }
        }
        return run.getResultJson() == null ? Optional.empty()
                : Optional.of(run.getResultJson().getBytes(StandardCharsets.UTF_8));
    }

    private static String sha256(byte[] value) {
        try {
            return HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(value));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}

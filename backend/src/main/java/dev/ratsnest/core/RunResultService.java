package dev.ratsnest.core;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.ratsnest.approval.RunApprovalService;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;

/**
 * Owns the idempotent RunRecord-to-DesignRun state transition.
 *
 * Result state is committed before artifact and release-review work begins, so
 * an interrupted dispatcher cannot leave a release approval attached to a run
 * that still appears to be executing.
 */
@Service
public class RunResultService {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final DesignRunRepository runs;
    private final RunApprovalService approvals;

    public RunResultService(DesignRunRepository runs,
                            RunApprovalService approvals) {
        this.runs = runs;
        this.approvals = approvals;
    }

    /** Persist a worker/local RunRecord under a row lock. */
    @Transactional
    public DesignRun accept(String runId, String runRecordJson) {
        DesignRun run = locked(runId);
        boolean changed = applyToEntity(run, runRecordJson);
        if (changed || run.getFinishedAt() == null) {
            run.setFinishedAt(Instant.now());
        }
        return runs.save(run);
    }

    /** Create or restore the release gate after the project artifact exists. */
    @Transactional
    public DesignRun requestReleaseReview(String runId) {
        DesignRun run = locked(runId);
        approvals.ensureReleaseReview(run);
        return runs.save(run);
    }

    /** Persist an execution/infrastructure failure without a stale merge. */
    @Transactional
    public DesignRun fail(String runId, String message) {
        DesignRun run = locked(runId);
        run.setStatus("failed");
        run.setFailureMessage(bounded(message));
        run.setFinishedAt(Instant.now());
        return runs.save(run);
    }

    /**
     * Apply the typed result contract to an entity. Exposed for focused unit
     * tests and compatibility with the existing dispatch API; persistence is
     * deliberately owned by {@link #accept(String, String)}.
     */
    public boolean applyToEntity(DesignRun run, String runRecordJson) {
        if (runRecordJson == null || runRecordJson.isBlank()) {
            throw new IllegalArgumentException("RunRecord JSON is required");
        }
        String resultHash = sha256(runRecordJson);
        if (resultHash.equals(run.getResultSha256())) {
            return false;
        }
        if (run.getResultSha256() != null) {
            throw new IllegalStateException(
                    "conflicting result callback for completed run");
        }

        JsonNode record;
        try {
            record = MAPPER.readTree(runRecordJson);
        } catch (JsonProcessingException e) {
            throw new IllegalArgumentException("invalid RunRecord JSON", e);
        }
        if (record == null || !record.isObject()) {
            throw new IllegalArgumentException(
                    "RunRecord payload must be a JSON object");
        }

        String incomingStatus = record.path("status").asText("failed");
        if ("design".equals(run.getKind())
                && !"failed".equals(incomingStatus)) {
            requireApprovedPlan(run);
        }
        if ("design".equals(run.getKind())
                && "ratsnest.design-plan.v2".equals(run.getPlanContractVersion())
                && "converged".equals(incomingStatus)
                && !approvals.hasPassedProductionGates(runRecordJson)) {
            throw new IllegalArgumentException(
                    "converged production result requires all release gates");
        }

        String callbackRunId = record.path("run_id").asText(null);
        if (callbackRunId != null && !callbackRunId.isBlank()) {
            if (run.getPythonRunId() != null
                    && !run.getPythonRunId().equals(callbackRunId)) {
                throw new IllegalStateException(
                        "result callback belongs to another Python run");
            }
            run.setPythonRunId(callbackRunId);
        }
        run.setStatus(incomingStatus);

        String callbackStrategy = record.path("strategy_version_id")
                .asText(null);
        if (callbackStrategy != null && run.getStrategyVersionId() != null
                && !run.getStrategyVersionId().equals(callbackStrategy)) {
            throw new IllegalStateException(
                    "result strategy differs from the approved plan");
        }
        if (callbackStrategy != null) {
            run.setStrategyVersionId(callbackStrategy);
        }

        JsonNode iterations = record.path("iterations");
        if (iterations.isArray() && !iterations.isEmpty()) {
            JsonNode first = iterations.get(0);
            JsonNode last = iterations.get(iterations.size() - 1);
            run.setFinalScore(last.path("scorecard").path("score").asDouble());
            double delta0 = first.path("score_delta").asDouble(0);
            double score0 = first.path("scorecard").path("score").asDouble();
            run.setInitialScore(score0 - delta0);
        }

        run.setResultJson(runRecordJson);
        run.setResultSha256(resultHash);
        String callbackError = record.path("error").asText(null);
        run.setFailureMessage("failed".equals(incomingStatus)
                && callbackError != null ? bounded(callbackError) : null);
        return true;
    }

    private void requireApprovedPlan(DesignRun run) {
        if (run.getPlanJson() == null || run.getPlanSha256() == null
                || !approvals.isApproved(
                run.getId(), RunApprovalService.BOARD_PLAN)) {
            throw new IllegalStateException(
                    "result requires an approved immutable BoardPlan");
        }
        String actual = DesignPlanService.sha256(run.getPlanJson());
        if (!actual.equals(run.getPlanSha256())) {
            throw new IllegalStateException("persisted BoardPlan hash mismatch");
        }
    }

    private DesignRun locked(String runId) {
        return runs.findLockedById(runId).orElseThrow(() ->
                new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "run not found"));
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static String bounded(String value) {
        String message = value == null || value.isBlank()
                ? "unknown execution failure" : value;
        return message.substring(0, Math.min(1000, message.length()));
    }
}

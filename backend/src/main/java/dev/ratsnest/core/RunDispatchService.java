package dev.ratsnest.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.ratsnest.artifact.RunArtifactService;
import dev.ratsnest.approval.RunApprovalService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Run dispatch — two modes, one contract:
 *   local  (default): spawn the Python agent runtime as a subprocess
 *   kafka  (cluster profile): publish a run request to `ratsnest.run-requests`;
 *          a Python worker consumes it and PUTs the RunRecord back to
 *          /api/runs/{id}/result.
 * Either way the child streams ATDP events to this service while it runs.
 */
@Service
public class RunDispatchService {

    private static final Logger log = LoggerFactory.getLogger(RunDispatchService.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final DesignRunRepository runs;
    private final ObjectProvider<KafkaTemplate<String, String>> kafka;
    private final PythonBridge bridge;
    private final RunArtifactService artifacts;
    private final RunApprovalService approvals;
    private final DesignPlanService plans;
    private final RunResultService results;

    @Value("${ratsnest.topic.run-requests:ratsnest.run-requests}")
    private String runRequestTopic;

    @Value("${ratsnest.self-url:http://localhost:8080}")
    private String selfUrl;

    @Value("${ratsnest.security.service-token:}")
    private String serviceToken;

    public RunDispatchService(DesignRunRepository runs,
                              ObjectProvider<KafkaTemplate<String, String>> kafka,
                              PythonBridge bridge,
                              RunArtifactService artifacts,
                              RunApprovalService approvals,
                              DesignPlanService plans,
                              RunResultService results) {
        this.runs = runs;
        this.kafka = kafka;
        this.bridge = bridge;
        this.artifacts = artifacts;
        this.approvals = approvals;
        this.plans = plans;
        this.results = results;
    }

    @Async
    public void dispatchLocal(String runId) {
        DesignRun run = runs.findById(runId).orElseThrow();
        if ("design".equals(run.getKind())
                && "plan".equals(run.getDispatchPhase())) {
            dispatchPlanLocal(run);
        } else {
            dispatchExecutionLocal(run);
        }
    }

    // -- kafka mode (cluster) -------------------------------------------------
    public void publishKafka(String runId) throws Exception {
        DesignRun run = runs.findById(runId).orElseThrow();
        try {
            ensurePythonRunId(run);
            KafkaTemplate<String, String> template = kafka.getObject();
            ObjectNode msg = MAPPER.createObjectNode();
            msg.put("runId", run.getId());
            msg.put("kind", run.getKind());
            msg.put("phase", run.getDispatchPhase() == null
                    ? "execute" : run.getDispatchPhase());
            msg.put("pythonRunId", run.getPythonRunId());
            msg.put("requirement", run.getRequirement());
            msg.put("projectDir", run.getProjectDir());
            msg.put("maxIterations", run.getMaxIterations());
            msg.put("backend", run.getBackend());
            msg.put("callbackUrl", selfUrl + "/api/runs/" + run.getId() + "/result");
            msg.put("planCallbackUrl", selfUrl + "/api/runs/" + run.getId()
                    + "/plan");
            msg.put("artifactUrl", selfUrl + "/api/runs/" + run.getId()
                    + "/artifacts/project");
            msg.put("controlPlaneUrl", selfUrl);
            if ("execute".equals(run.getDispatchPhase())
                    && "design".equals(run.getKind())) {
                requireApprovedPlan(run);
                msg.put("planJson", run.getPlanJson());
                msg.put("planSha256", run.getPlanSha256());
            }
            template.send(runRequestTopic, run.getId(), msg.toString())
                    .get(10, java.util.concurrent.TimeUnit.SECONDS);
            run.setStatus("plan".equals(run.getDispatchPhase())
                    ? "planning" : "queued");
        } catch (Exception e) {
            log.error("kafka dispatch failed for run {}", run.getId(), e);
            throw e;
        }
        runs.save(run);
    }

    // -- local mode (dev) -------------------------------------------------------
    private void dispatchPlanLocal(DesignRun run) {
        try {
            if (run.getPlanJson() != null) {
                return;
            }
            ensurePythonRunId(run);
            run.setStatus("planning");
            run.setFailureMessage(null);
            runs.save(run);

            List<String> cmd = new ArrayList<>(List.of(
                    "design-plan", run.getRequirement(),
                    "--backend", run.getBackend(),
                    "--run-id", run.getPythonRunId(), "--json"));
            PythonBridge.BridgeResult result = bridge.run(
                    cmd, Duration.ofMinutes(5), runtimeEnvironment());
            if (!result.finished() || result.stdout().isBlank()) {
                throw new IllegalStateException(
                        "agent runtime produced no PlannedDesign");
            }
            plans.apply(run.getId(), result.stdout());
            return;
        } catch (Exception error) {
            log.error("planning failed for run {}", run.getId(), error);
            DesignRun current = runs.findById(run.getId()).orElse(run);
            current.setStatus("failed");
            current.setFailureMessage(boundedMessage(error));
            current.setFinishedAt(Instant.now());
            runs.save(current);
        }
    }

    private void dispatchExecutionLocal(DesignRun run) {
        Path planFile = null;
        try {
            List<String> cmd = new ArrayList<>();
            if ("design".equals(run.getKind())) {
                requireApprovedPlan(run);
                planFile = Files.createTempFile(
                        "ratsnest-plan-" + run.getId() + "-", ".json");
                Files.writeString(planFile, run.getPlanJson(),
                        StandardCharsets.UTF_8);
                cmd.addAll(List.of(
                        "design-execute", "--plan", planFile.toString(),
                        "--plan-sha256", run.getPlanSha256(),
                        "--out", run.getProjectDir()));
            } else {
                cmd.addAll(List.of("fix", run.getProjectDir()));
            }
            cmd.addAll(List.of("--max-iter", String.valueOf(run.getMaxIterations()),
                    "--no-erc", "--json"));

            run.setStatus("running");
            run.setStartedAt(Instant.now());
            run.setAttempt(run.getAttempt() + 1);
            run.setFailureMessage(null);
            run = runs.save(run);

            PythonBridge.BridgeResult result =
                    bridge.run(cmd, Duration.ofMinutes(15), runtimeEnvironment());

            if (!result.finished() || result.stdout().isBlank()) {
                results.fail(run.getId(),
                        "agent runtime produced no RunRecord");
                log.error("run {} produced no output; stderr: {}", run.getId(),
                        result.stderr().substring(0,
                                Math.min(500, result.stderr().length())));
            } else {
                DesignRun completed = results.accept(
                        run.getId(), result.stdout());
                if ("design".equals(completed.getKind())
                        && !"failed".equals(completed.getStatus())) {
                    try {
                        artifacts.captureProjectDirectory(completed);
                        results.requestReleaseReview(completed.getId());
                    } catch (Exception artifactError) {
                        results.fail(completed.getId(),
                                "project artifact capture failed: "
                                        + artifactError.getMessage());
                        log.error("artifact capture failed for run {}",
                                completed.getId(), artifactError);
                    }
                }
            }
        } catch (Exception e) {
            log.error("dispatch failed for run {}", run.getId(), e);
            try {
                results.fail(run.getId(), boundedMessage(e));
            } catch (Exception persistenceError) {
                log.error("could not persist failure for run {}", run.getId(),
                        persistenceError);
            }
        } finally {
            if (planFile != null) {
                try {
                    Files.deleteIfExists(planFile);
                } catch (Exception ignored) {
                    // The OS temp cleaner is the final fallback.
                }
            }
        }
    }

    /** Parse a RunRecord contract payload into the governance row.
     *  Shared by local dispatch and the worker callback endpoint. */
    public void applyResult(DesignRun run, String runRecordJson) {
        results.applyToEntity(run, runRecordJson);
    }

    private void requireApprovedPlan(DesignRun run) {
        if (run.getPlanJson() == null || run.getPlanSha256() == null
                || !approvals.isApproved(
                run.getId(), RunApprovalService.BOARD_PLAN)) {
            throw new IllegalStateException(
                    "KiCad execution requires an approved immutable BoardPlan");
        }
        String actual = DesignPlanService.sha256(run.getPlanJson());
        if (!actual.equals(run.getPlanSha256())) {
            throw new IllegalStateException("persisted BoardPlan hash mismatch");
        }
    }

    private void ensurePythonRunId(DesignRun run) {
        if (run.getPythonRunId() == null || run.getPythonRunId().isBlank()) {
            run.setPythonRunId("run_" + run.getId().replace("-", ""));
        }
    }

    private Map<String, String> runtimeEnvironment() {
        Map<String, String> env = new HashMap<>();
        env.put("RATSNEST_CONTROL_PLANE_URL", selfUrl);
        if (serviceToken != null && !serviceToken.isBlank()) {
            env.put("RATSNEST_SERVICE_TOKEN", serviceToken);
        }
        return env;
    }

    private static String boundedMessage(Exception error) {
        String message = error.getMessage() == null
                ? error.getClass().getSimpleName() : error.getMessage();
        return message.substring(0, Math.min(1000, message.length()));
    }
}

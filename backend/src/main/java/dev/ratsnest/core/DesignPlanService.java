package dev.ratsnest.core;

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
import java.util.Optional;
import java.util.Set;

@Service
public class DesignPlanService {

    public static final String LEGACY_CONTRACT_VERSION = "ratsnest.design-plan.v1";
    public static final String CONTRACT_VERSION = "ratsnest.design-plan.v2";
    private static final Set<String> SUPPORTED_CONTRACT_VERSIONS = Set.of(
            LEGACY_CONTRACT_VERSION, CONTRACT_VERSION);
    private static final int MAX_PLAN_BYTES = 1_048_576;
    private static final Set<String> PLAN_FIELDS = Set.of(
            "contract_version", "run_id", "requirement", "backend",
            "design_spec", "board_plan", "strategy_name",
            "strategy_version_id", "trajectory_step", "created_at");

    public record PlanView(
            String contractVersion,
            String runId,
            String requirement,
            String backend,
            String strategyName,
            String strategyVersionId,
            String subjectSha256,
            Instant createdAt,
            JsonNode designSpec,
            JsonNode boardPlan) {}

    private final DesignRunRepository runs;
    private final RunApprovalService approvals;
    private final ObjectMapper mapper;

    public DesignPlanService(DesignRunRepository runs,
                             RunApprovalService approvals,
                             ObjectMapper mapper) {
        this.runs = runs;
        this.approvals = approvals;
        this.mapper = mapper;
    }

    @Transactional
    public DesignRun apply(String runId, String rawPlanJson) {
        DesignRun run = runs.findLockedById(runId).orElseThrow(() ->
                new ResponseStatusException(HttpStatus.NOT_FOUND));
        if (!"design".equals(run.getKind())) {
            throw new ResponseStatusException(
                    HttpStatus.CONFLICT, "fix runs do not accept design plans");
        }
        if (run.getPlanJson() != null) {
            return run; // immutable first-write wins on worker retry
        }
        String planJson = rawPlanJson == null ? "" : rawPlanJson.strip();
        if (planJson.isEmpty()
                || planJson.getBytes(StandardCharsets.UTF_8).length
                > MAX_PLAN_BYTES) {
            throw new ResponseStatusException(
                    HttpStatus.BAD_REQUEST, "invalid design plan size");
        }
        JsonNode root = validate(run, planJson);
        run.setPlanJson(planJson);
        run.setPlanSha256(sha256(planJson));
        run.setPlanContractVersion(root.path("contract_version").asText());
        run.setPlanCreatedAt(Instant.now());
        run.setStrategyVersionId(root.path("strategy_version_id").asText());
        run.setStatus("awaiting_plan_approval");
        DesignRun saved = runs.save(run);
        approvals.ensurePlanReview(saved);
        return saved;
    }

    public Optional<PlanView> view(DesignRun run) {
        if (run.getPlanJson() == null) {
            return Optional.empty();
        }
        try {
            JsonNode root = mapper.readTree(run.getPlanJson());
            return Optional.of(new PlanView(
                    root.path("contract_version").asText(),
                    root.path("run_id").asText(),
                    root.path("requirement").asText(),
                    root.path("backend").asText(),
                    root.path("strategy_name").asText(),
                    root.path("strategy_version_id").asText(),
                    run.getPlanSha256(), run.getPlanCreatedAt(),
                    root.path("design_spec"), root.path("board_plan")));
        } catch (Exception error) {
            throw new IllegalStateException("persisted design plan is invalid", error);
        }
    }

    public Optional<String> boardPlanJson(DesignRun run) {
        return view(run).map(value -> value.boardPlan().toString());
    }

    private JsonNode validate(DesignRun run, String planJson) {
        final JsonNode root;
        try {
            root = mapper.readTree(planJson);
        } catch (Exception error) {
            throw new ResponseStatusException(
                    HttpStatus.BAD_REQUEST, "design plan is not valid JSON");
        }
        String contractVersion = root.path("contract_version").asText();
        if (!root.isObject()
                || !SUPPORTED_CONTRACT_VERSIONS.contains(contractVersion)) {
            throw invalid("unsupported design plan contract");
        }
        root.fieldNames().forEachRemaining(field -> {
            if (!PLAN_FIELDS.contains(field)) {
                throw invalid("unknown design plan field: " + field);
            }
        });
        String planRunId = root.path("run_id").asText();
        if (run.getPythonRunId() == null) {
            run.setPythonRunId(planRunId);
        } else if (!run.getPythonRunId().equals(planRunId)) {
            throw invalid("design plan belongs to another run");
        }
        if (!run.getRequirement().equals(root.path("requirement").asText())
                || !run.getBackend().equals(root.path("backend").asText())) {
            throw invalid("design plan does not match the requested run");
        }
        if (!root.path("design_spec").isObject()
                || !root.path("board_plan").isObject()
                || !root.path("board_plan").path("components").isArray()
                || root.path("board_plan").path("components").isEmpty()
                || !root.path("board_plan").path("connections").isArray()) {
            throw invalid("design plan is missing typed design content");
        }
        if (CONTRACT_VERSION.equals(contractVersion)) {
            validateProductionPlan(root.path("board_plan"));
        }
        String strategy = root.path("strategy_version_id").asText();
        if (!strategy.matches("strat_[a-f0-9]{8,64}")) {
            throw invalid("design plan has an invalid strategy identity");
        }
        return root;
    }

    private static void validateProductionPlan(JsonNode boardPlan) {
        String catalogVersion = boardPlan.path("catalog_version").asText();
        if (catalogVersion.isBlank() || "legacy".equals(catalogVersion)
                || !boardPlan.path("design_limits").isObject()
                || !boardPlan.path("required_gates").isArray()
                || boardPlan.path("required_gates").isEmpty()) {
            throw invalid("v2 plan is missing production constraints");
        }
        for (JsonNode component : boardPlan.path("components")) {
            if (component.path("catalog_id").asText().isBlank()) {
                throw invalid("v2 component has no catalog binding");
            }
            boolean onBoard = !component.has("on_board")
                    || component.path("on_board").asBoolean();
            if (onBoard && component.path("footprint").asText().isBlank()) {
                throw invalid("v2 physical component has no footprint");
            }
        }
    }

    private static ResponseStatusException invalid(String detail) {
        return new ResponseStatusException(HttpStatus.BAD_REQUEST, detail);
    }

    static String sha256(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
    }
}

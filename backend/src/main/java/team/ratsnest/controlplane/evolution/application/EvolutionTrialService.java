package team.ratsnest.controlplane.evolution.application;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import java.util.UUID;
import java.util.regex.Pattern;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository;
import team.ratsnest.controlplane.harness.application.HarnessVersionService;
import team.ratsnest.controlplane.harness.domain.model.HarnessVersion;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;
import tools.jackson.databind.ObjectMapper;

@Service
public class EvolutionTrialService {

    private static final Pattern DIGEST = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern COMMIT = Pattern.compile("[0-9a-f]{40,64}");
    private static final Set<String> FINAL_VERDICTS = Set.of(
            "PASSED", "FAILED", "REGRESSION", "POLICY_REJECTED",
            "ENVIRONMENT_ISSUE", "CANCELLED");
    private static final List<String> FIXED_EVAL_IDS = List.of(
            "python-compile", "evolution-core");

    private final TenantContext tenantContext;
    private final EvolutionRepository evolution;
    private final HarnessVersionService harnessVersions;
    private final ObjectMapper objectMapper;
    private final String optimizationSuiteDigest;
    private final String holdoutSuiteDigest;
    private final String adversarialSuiteDigest;
    private final String requiredExecutorMode;

    public EvolutionTrialService(
            TenantContext tenantContext,
            EvolutionRepository evolution,
            HarnessVersionService harnessVersions,
            ObjectMapper objectMapper,
            @Value("${ratsnest.evolution.optimization-suite-digest:}") String optimizationSuiteDigest,
            @Value("${ratsnest.evolution.holdout-suite-digest:}") String holdoutSuiteDigest,
            @Value("${ratsnest.evolution.adversarial-suite-digest:}") String adversarialSuiteDigest,
            @Value("${ratsnest.evolution.required-executor-mode:kubernetes_job}") String requiredExecutorMode) {
        this.tenantContext = tenantContext;
        this.evolution = evolution;
        this.harnessVersions = harnessVersions;
        this.objectMapper = objectMapper;
        this.optimizationSuiteDigest = optimizationSuiteDigest == null
                ? "" : optimizationSuiteDigest.strip();
        this.holdoutSuiteDigest = holdoutSuiteDigest == null
                ? "" : holdoutSuiteDigest.strip();
        this.adversarialSuiteDigest = adversarialSuiteDigest == null
                ? "" : adversarialSuiteDigest.strip();
        if (!Set.of("local_process", "kubernetes_job").contains(requiredExecutorMode)) {
            throw new IllegalStateException("Unsupported required evolution executor mode");
        }
        this.requiredExecutorMode = requiredExecutorMode;
    }

    @Transactional
    public PreparedTrial prepareTrial(
            UUID tenantId,
            String candidateId,
            long expectedVersion,
            String idempotencyKey,
            EvaluateCommand command,
            AuthenticatedActor actor) {
        tenantContext.activate(tenantId);
        requireSuiteConfiguration();
        EvolutionCandidate candidate = requireCandidate(tenantId, candidateId);
        HarnessVersion harness = harnessVersions.require(candidate.baseHarnessVersionId());
        requirePinnedBase(candidate, harness);
        Map<String, Object> trialInput = trialInput(candidate, harness, command);
        String inputDigest = canonicalDigest(trialInput);
        UUID trialId = deterministicTrialId(tenantId, candidateId, idempotencyKey);
        EvolutionTrial existing = evolution.findTrial(tenantId, trialId).orElse(null);
        if (existing != null) {
            if (!existing.candidateId().equals(candidateId)
                    || !existing.inputDigest().equals(inputDigest)) {
                throw idempotencyConflict();
            }
            return new PreparedTrial(existing, trialInput, "PENDING".equals(existing.verdict()));
        }
        if (candidate.rowVersion() != expectedVersion) {
            throw stale();
        }
        if (candidate.status() != EvolutionCandidate.Status.ELIGIBLE) {
            throw new ApiException(
                    "EVOLUTION_EVALUATION_INVALID",
                    HttpStatus.CONFLICT,
                    "Only an eligible candidate can start a governed evaluation.");
        }
        if (evolution.findPendingTrial(tenantId, candidateId).isPresent()) {
            throw new ApiException(
                    "EVOLUTION_EVALUATION_ACTIVE",
                    HttpStatus.CONFLICT,
                    "The candidate already has a pending governed evaluation.");
        }
        String evalSuiteDigest = evalSuiteDigest();
        Instant now = Instant.now();
        EvolutionTrial trial = new EvolutionTrial(
                trialId,
                candidateId,
                evolution.nextAttempt(tenantId, candidateId),
                inputDigest,
                candidate.baseManifestDigest(),
                candidateDigest(trialInput),
                evalSuiteDigest,
                null,
                null,
                null,
                null,
                optimizationSuiteDigest,
                holdoutSuiteDigest,
                adversarialSuiteDigest,
                Map.of(),
                Map.of(),
                Map.of(),
                "PENDING",
                null,
                Map.of(),
                null,
                0,
                0,
                1,
                now,
                now,
                null);
        if (!evolution.insertTrial(tenantId, trial)
                || !evolution.transition(
                        tenantId,
                        candidate,
                        EvolutionCandidate.Status.EVALUATING,
                        "governed evaluation started",
                        actor)) {
            throw stale();
        }
        return new PreparedTrial(
                evolution.findTrial(tenantId, trialId).orElseThrow(), trialInput, true);
    }

    @Transactional
    public EvolutionTrial bindWorkflow(
            UUID tenantId,
            UUID trialId,
            String workflowId) {
        tenantContext.activate(tenantId);
        EvolutionTrial trial = requireTrial(tenantId, trialId);
        if (trial.temporalWorkflowId() != null) {
            if (!trial.temporalWorkflowId().equals(workflowId)) {
                throw staleTrial();
            }
            return trial;
        }
        if (!evolution.bindWorkflow(tenantId, trial, workflowId)) {
            throw staleTrial();
        }
        return requireTrial(tenantId, trialId);
    }

    @Transactional
    public EvolutionTrial completeTrial(
            UUID tenantId,
            UUID trialId,
            ResultProof result) {
        tenantContext.activate(tenantId);
        EvolutionTrial trial = requireTrial(tenantId, trialId);
        requireMatchingProof(tenantId, trial, result);
        if (!"PENDING".equals(trial.verdict())) {
            if (Objects.equals(trial.reportDigest(), result.reportDigest())
                    && Objects.equals(trial.verdict(), result.verdict())
                    && Objects.equals(trial.patchSha256(), result.patchDigest())
                    && Objects.equals(trial.authoritativeReport(), result.authoritativeReport())
                    && Objects.equals(trial.completedAt(), result.completedAt())) {
                return trial;
            }
            throw new ApiException(
                    "EVOLUTION_RESULT_CONFLICT",
                    HttpStatus.CONFLICT,
                    "The trial already has a different authoritative result.");
        }
        if (!FINAL_VERDICTS.contains(result.verdict())) {
            throw invalidProof();
        }
        String calculatedReportDigest = canonicalDigest(result.authoritativeReport());
        if (!calculatedReportDigest.equals(result.reportDigest())) {
            throw invalidProof();
        }
        boolean passed = authoritativeGatePassed(result);
        EvolutionCandidate candidate = requireCandidate(tenantId, trial.candidateId());
        if (candidate.status() != EvolutionCandidate.Status.EVALUATING) {
            throw stale();
        }
        EvolutionCandidate.Status target = passed
                ? EvolutionCandidate.Status.AWAITING_APPROVAL
                : EvolutionCandidate.Status.REJECTED;
        Map<String, Object> guardrails = Map.of(
                "runtimeAttested", true,
                "executorMode", requiredExecutorMode,
                "guardrailPassed", result.guardrailPassed(),
                "authoritativeGatePassed", passed,
                "evalSuiteDigest", result.evalSuiteDigest());
        long wallClockMs = reportWallClockMs(result.authoritativeReport());
        EvolutionRepository.TrialResult update = new EvolutionRepository.TrialResult(
                result.temporalWorkflowId(),
                null,
                result.patchDigest(),
                null,
                Map.of(),
                Map.of(),
                guardrails,
                result.verdict(),
                result.reportDigest(),
                result.authoritativeReport(),
                null,
                0,
                wallClockMs,
                result.completedAt());
        if (!evolution.completeTrial(tenantId, trial, update)
                || !evolution.transition(
                        tenantId,
                        candidate,
                        target,
                        passed
                                ? "authoritative evaluation gates passed; human approval required"
                                : "authoritative evaluation gates rejected the candidate",
                        new AuthenticatedActor("ratsnest-agent-runtime", "evolution-worker"))) {
            throw staleTrial();
        }
        return requireTrial(tenantId, trialId);
    }

    private Map<String, Object> trialInput(
            EvolutionCandidate candidate,
            HarnessVersion harness,
            EvaluateCommand command) {
        validatePatch(command, candidate, harness);
        Map<String, Object> candidateValue = new LinkedHashMap<>();
        candidateValue.put("schemaVersion", "1.0");
        candidateValue.put("candidateId", candidate.candidateId());
        candidateValue.put("baseHarnessVersionId", candidate.baseHarnessVersionId());
        candidateValue.put("baseManifestDigest", candidate.baseManifestDigest());
        candidateValue.put("failureSignature", candidate.failureSignature());
        candidateValue.put("step", candidate.step());
        candidateValue.put("checkName", candidate.checkName());
        candidateValue.put("category", candidate.category());
        candidateValue.put("requiredCapability", candidate.requiredCapability());
        candidateValue.put("profileReferences", candidate.profileReferences());
        candidateValue.put("observationIds", candidate.observationIds());
        candidateValue.put("occurrenceCount", candidate.occurrenceCount());
        candidateValue.put("projectCount", candidate.projectCount());
        candidateValue.put("status", candidate.status().wireValue());
        candidateValue.put("riskTier", candidate.riskTier());
        candidateValue.put("changeKind", candidate.changeKind());
        candidateValue.put("createdAt", candidate.createdAt().toString());

        Map<String, Object> manifest = new LinkedHashMap<>();
        manifest.put("schemaVersion", "1.0");
        manifest.put("sourceCommit", harness.sourceCommit());
        manifest.put("sourceTreeDigest", harness.sourceTreeDigest());
        manifest.put("dirty", harness.dirty());
        manifest.put("bundleDigest", harness.bundleDigest());
        manifest.put("contractDigest", harness.contractDigest());
        manifest.put("policyDigest", harness.policyDigest());
        manifest.put("runtimeImageDigest", harness.runtimeImageDigest());
        manifest.put("toolchainDigest", harness.toolchainDigest());
        manifest.put("manifestDigest", harness.manifestDigest());

        Map<String, Object> value = new LinkedHashMap<>();
        value.put("candidate", candidateValue);
        value.put("harnessManifest", manifest);
        value.put("patchPlan", command.patchPlan().asMap());
        value.put("patchBundle", command.patchBundle().asMap());
        value.put("evalIds", FIXED_EVAL_IDS);
        return value;
    }

    private void validatePatch(
            EvaluateCommand command,
            EvolutionCandidate candidate,
            HarnessVersion harness) {
        if (command == null || command.patchPlan() == null || command.patchBundle() == null) {
            throw invalidPatch();
        }
        PatchPlanInput plan = command.patchPlan();
        PatchBundleInput bundle = command.patchBundle();
        if (!"1.0".equals(plan.schemaVersion()) || !"1.0".equals(bundle.schemaVersion())
                || !candidate.candidateId().equals(plan.candidateId())
                || !candidate.candidateId().equals(bundle.candidateId())
                || !harness.sourceCommit().equals(plan.baseCommit())
                || !harness.sourceCommit().equals(bundle.baseCommit())
                || !COMMIT.matcher(plan.baseCommit()).matches()
                || plan.summary() == null || plan.summary().isBlank()
                || plan.summary().length() > 4_000
                || plan.changes() == null || plan.changes().isEmpty()
                || plan.changes().size() > 100
                || plan.preservedInvariants() == null || plan.preservedInvariants().isEmpty()
                || plan.preservedInvariants().size() > 128
                || bundle.files() == null || bundle.files().isEmpty()
                || bundle.files().size() > 8) {
            throw invalidPatch();
        }
        Map<String, String> planned = new LinkedHashMap<>();
        for (PatchChangeInput change : plan.changes()) {
            if (change == null || !Set.of("create", "modify").contains(change.operation())
                    || !safePath(change.path())
                    || change.rationale() == null || change.rationale().isBlank()
                    || change.rationale().length() > 2_000
                    || change.estimatedAddedLines() < 0
                    || change.estimatedAddedLines() > 10_000
                    || planned.put(change.path(), "modify".equals(change.operation())
                            ? "replace" : "create") != null) {
                throw invalidPatch();
            }
        }
        Map<String, String> materialized = new LinkedHashMap<>();
        int totalBytes = 0;
        for (CandidateFilePatchInput file : bundle.files()) {
            if (file == null || !Set.of("create", "replace").contains(file.operation())
                    || !safePath(file.path()) || !"utf-8".equals(file.encoding())
                    || file.content() == null || file.contentSha256() == null
                    || !DIGEST.matcher(file.contentSha256()).matches()) {
                throw invalidPatch();
            }
            byte[] content = file.content().getBytes(StandardCharsets.UTF_8);
            totalBytes = Math.addExact(totalBytes, content.length);
            if (content.length > 64 * 1024
                    || !file.contentSha256().equals(sha256(content))
                    || ("create".equals(file.operation()) && file.expectedOldSha256() != null)
                    || ("replace".equals(file.operation())
                            && (file.expectedOldSha256() == null
                                    || !DIGEST.matcher(file.expectedOldSha256()).matches()))
                    || materialized.put(file.path(), file.operation()) != null) {
                throw invalidPatch();
            }
        }
        if (totalBytes > 256 * 1024 || !planned.equals(materialized)) {
            throw invalidPatch();
        }
    }

    private boolean safePath(String value) {
        if (value == null || value.isBlank() || value.length() > 500
                || value.startsWith("/") || value.contains("\\")
                || value.contains(":") || value.indexOf('\0') >= 0) {
            return false;
        }
        for (String part : value.split("/", -1)) {
            if (part.isBlank() || ".".equals(part) || "..".equals(part)
                    || part.endsWith(".") || part.endsWith(" ")) {
                return false;
            }
        }
        return true;
    }

    private void requirePinnedBase(EvolutionCandidate candidate, HarnessVersion harness) {
        if (!candidate.baseManifestDigest().equals(harness.manifestDigest())
                || harness.dirty()) {
            throw new ApiException(
                    "EVOLUTION_BASE_IDENTITY_INVALID",
                    HttpStatus.CONFLICT,
                    "The candidate is not pinned to a clean immutable harness manifest.");
        }
    }

    private void requireMatchingProof(
            UUID tenantId,
            EvolutionTrial trial,
            ResultProof result) {
        EvolutionCandidate candidate = requireCandidate(tenantId, trial.candidateId());
        if (!trial.trialId().equals(result.trialId())
                || !trial.candidateId().equals(result.candidateId())
                || !trial.candidateDigest().equals(result.candidateDigest())
                || !candidate.baseHarnessVersionId().equals(result.baseHarnessVersionId())
                || !trial.baseManifestDigest().equals(result.baseManifestDigest())
                || !trial.inputDigest().equals(result.inputDigest())
                || !trial.evalSuiteDigest().equals(result.evalSuiteDigest())
                || !trial.optimizationSuiteDigest().equals(result.optimizationSuiteDigest())
                || !trial.holdoutSuiteDigest().equals(result.holdoutSuiteDigest())
                || !trial.adversarialSuiteDigest().equals(result.adversarialSuiteDigest())
                || !trial.evalSuiteDigest().equals(evalSuiteDigest())
                || trial.temporalWorkflowId() == null
                || !trial.temporalWorkflowId().equals(result.temporalWorkflowId())
                || result.patchDigest() == null
                || !DIGEST.matcher(result.patchDigest()).matches()
                || result.reportDigest() == null
                || !DIGEST.matcher(result.reportDigest()).matches()
                || result.completedAt() == null
                || result.completedAt().isBefore(trial.createdAt().minusSeconds(300))
                || result.completedAt().isAfter(Instant.now().plusSeconds(300))) {
            throw invalidProof();
        }
    }

    private boolean authoritativeGatePassed(ResultProof result) {
        Map<String, Object> report = result.authoritativeReport();
        if (!"PASSED".equals(result.verdict()) || !result.guardrailPassed()
                || !Objects.equals(report.get("candidateId"), result.candidateId())
                || !Objects.equals(report.get("patchDigest"), result.patchDigest())
                || !Objects.equals(report.get("executorMode"), requiredExecutorMode)
                || !Objects.equals(report.get("verdict"), "passed")
                || !Boolean.TRUE.equals(report.get("cleanupSucceeded"))
                || !Boolean.FALSE.equals(report.get("automaticMerge"))
                || !Boolean.FALSE.equals(report.get("automaticPush"))
                || !Boolean.FALSE.equals(report.get("automaticDeploy"))) {
            return false;
        }
        Object commands = report.get("commandResults");
        if (!(commands instanceof List<?> values) || values.size() != FIXED_EVAL_IDS.size()) {
            return false;
        }
        Set<String> observed = new java.util.HashSet<>();
        for (Object value : values) {
            if (!(value instanceof Map<?, ?> command)
                    || !(command.get("evalId") instanceof String evalId)
                    || !FIXED_EVAL_IDS.contains(evalId)
                    || !observed.add(evalId)
                    || !Boolean.TRUE.equals(command.get("passed"))) {
                return false;
            }
        }
        return observed.size() == FIXED_EVAL_IDS.size();
    }

    private long reportWallClockMs(Map<String, Object> report) {
        Object commands = report.get("commandResults");
        if (!(commands instanceof List<?> values)) {
            return 0;
        }
        long total = 0;
        for (Object value : values) {
            if (value instanceof Map<?, ?> command
                    && command.get("durationMs") instanceof Number duration) {
                total = Math.addExact(total, Math.max(0, duration.longValue()));
            }
        }
        return total;
    }

    private String evalSuiteDigest() {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("optimizationSuiteDigest", optimizationSuiteDigest);
        value.put("holdoutSuiteDigest", holdoutSuiteDigest);
        value.put("adversarialSuiteDigest", adversarialSuiteDigest);
        value.put("evalIds", FIXED_EVAL_IDS);
        return canonicalDigest(value);
    }

    private void requireSuiteConfiguration() {
        if (!DIGEST.matcher(optimizationSuiteDigest).matches()
                || !DIGEST.matcher(holdoutSuiteDigest).matches()
                || !DIGEST.matcher(adversarialSuiteDigest).matches()) {
            throw new ApiException(
                    "EVOLUTION_SUITE_IDENTITY_UNAVAILABLE",
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "The governed evaluation suite digests are not configured.");
        }
    }

    private String canonicalDigest(Map<String, Object> value) {
        try {
            return sha256(objectMapper.writeValueAsBytes(canonical(value)));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to canonicalize evolution evidence", exception);
        }
    }

    @SuppressWarnings("unchecked")
    private String candidateDigest(Map<String, Object> trialInput) {
        return canonicalDigest((Map<String, Object>) trialInput.get("candidate"));
    }

    private Object canonical(Object value) {
        if (value instanceof Map<?, ?> map) {
            Map<String, Object> sorted = new TreeMap<>();
            map.forEach((key, item) -> sorted.put(String.valueOf(key), canonical(item)));
            return sorted;
        }
        if (value instanceof List<?> list) {
            List<Object> normalized = new ArrayList<>(list.size());
            list.forEach(item -> normalized.add(canonical(item)));
            return normalized;
        }
        return value;
    }

    byte[] canonicalBytes(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsBytes(canonical(value));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to canonicalize evolution evidence", exception);
        }
    }

    private String sha256(byte[] value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to hash evolution evidence", exception);
        }
    }

    private UUID deterministicTrialId(UUID tenantId, String candidateId, String idempotencyKey) {
        return UUID.nameUUIDFromBytes(String.join("\0",
                "evolution-trial-v1", tenantId.toString(), candidateId, idempotencyKey)
                .getBytes(StandardCharsets.UTF_8));
    }

    private EvolutionTrial requireTrial(UUID tenantId, UUID trialId) {
        return evolution.findTrial(tenantId, trialId).orElseThrow(() -> new ApiException(
                "EVOLUTION_TRIAL_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The evolution trial was not found."));
    }

    private EvolutionCandidate requireCandidate(UUID tenantId, String candidateId) {
        return evolution.findCandidate(tenantId, candidateId).orElseThrow(() -> new ApiException(
                "EVOLUTION_CANDIDATE_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The evolution candidate was not found."));
    }

    private ApiException stale() {
        return new ApiException(
                "EVOLUTION_CANDIDATE_STALE",
                HttpStatus.CONFLICT,
                "The evolution candidate changed; reload it before retrying the transition.");
    }

    private ApiException staleTrial() {
        return new ApiException(
                "EVOLUTION_TRIAL_STALE",
                HttpStatus.CONFLICT,
                "The evolution trial changed; reload before retrying.");
    }

    private ApiException idempotencyConflict() {
        return new ApiException(
                "EVOLUTION_IDEMPOTENCY_CONFLICT",
                HttpStatus.CONFLICT,
                "The idempotency key is already associated with different evaluation input.");
    }

    private ApiException invalidPatch() {
        return new ApiException(
                "EVOLUTION_PATCH_INVALID",
                HttpStatus.BAD_REQUEST,
                "The bounded patch plan and patch bundle are invalid or inconsistent.");
    }

    private ApiException invalidProof() {
        return new ApiException(
                "EVOLUTION_RESULT_PROOF_INVALID",
                HttpStatus.UNPROCESSABLE_ENTITY,
                "The Agent Runtime result is not bound to this governed trial.");
    }

    public record PreparedTrial(
            EvolutionTrial trial,
            Map<String, Object> trialInput,
            boolean needsStart) {
    }

    public record EvaluateCommand(PatchPlanInput patchPlan, PatchBundleInput patchBundle) {
    }

    public record PatchPlanInput(
            String schemaVersion,
            String candidateId,
            String baseCommit,
            String summary,
            List<PatchChangeInput> changes,
            List<String> preservedInvariants,
            List<String> publicEvalCaseIds) {

        Map<String, Object> asMap() {
            Map<String, Object> value = new LinkedHashMap<>();
            value.put("schemaVersion", schemaVersion);
            value.put("candidateId", candidateId);
            value.put("baseCommit", baseCommit);
            value.put("summary", summary);
            value.put("changes", changes.stream().map(PatchChangeInput::asMap).toList());
            value.put("preservedInvariants", preservedInvariants);
            value.put("publicEvalCaseIds", publicEvalCaseIds == null ? List.of() : publicEvalCaseIds);
            return value;
        }
    }

    public record PatchChangeInput(
            String operation,
            String path,
            String rationale,
            int estimatedAddedLines) {

        Map<String, Object> asMap() {
            return Map.of(
                    "operation", operation,
                    "path", path,
                    "rationale", rationale,
                    "estimatedAddedLines", estimatedAddedLines);
        }
    }

    public record PatchBundleInput(
            String schemaVersion,
            String candidateId,
            String baseCommit,
            List<CandidateFilePatchInput> files) {

        Map<String, Object> asMap() {
            Map<String, Object> value = new LinkedHashMap<>();
            value.put("schemaVersion", schemaVersion);
            value.put("candidateId", candidateId);
            value.put("baseCommit", baseCommit);
            value.put("files", files.stream().map(CandidateFilePatchInput::asMap).toList());
            return value;
        }
    }

    public record CandidateFilePatchInput(
            String operation,
            String path,
            String expectedOldSha256,
            String contentSha256,
            String content,
            String encoding) {

        Map<String, Object> asMap() {
            Map<String, Object> value = new LinkedHashMap<>();
            value.put("operation", operation);
            value.put("path", path);
            value.put("expectedOldSha256", expectedOldSha256);
            value.put("contentSha256", contentSha256);
            value.put("content", content);
            value.put("encoding", encoding);
            return value;
        }
    }

    public record ResultProof(
            UUID trialId,
            String candidateId,
            String candidateDigest,
            String baseHarnessVersionId,
            String baseManifestDigest,
            String inputDigest,
            String temporalWorkflowId,
            String optimizationSuiteDigest,
            String holdoutSuiteDigest,
            String adversarialSuiteDigest,
            String evalSuiteDigest,
            String patchDigest,
            String reportDigest,
            String verdict,
            boolean guardrailPassed,
            Map<String, Object> authoritativeReport,
            Instant completedAt) {
    }
}

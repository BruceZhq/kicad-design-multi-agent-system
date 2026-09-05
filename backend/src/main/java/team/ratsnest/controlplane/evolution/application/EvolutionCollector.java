package team.ratsnest.controlplane.evolution.application;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import java.util.regex.Pattern;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeMessage;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservation;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservationGovernance;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;
import tools.jackson.databind.ObjectMapper;

/**
 * Converts trusted run-scoped AHE events into bounded, privacy-safe facts.
 * Raw prompts, diagnostic messages and evidence never cross this boundary.
 */
@Service
public class EvolutionCollector {

    private static final Pattern EVENT_TYPE = Pattern.compile("[a-z][a-z0-9_]{1,63}");
    private static final Pattern DIGEST = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern SAFE_TOKEN = Pattern.compile("[A-Za-z0-9][A-Za-z0-9_.:/@-]*");
    private static final Pattern AUDIT_REFERENCE = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]{0,254}");
    private static final Set<String> GOVERNED_FAILURE_REASON_CODES = Set.of(
            "generic_capability_closure_contradiction",
            "missing_mutation_capability",
            "verified_pin_alias_resolution_lost");
    private static final Set<String> TOP_LEVEL_FIELDS = Set.of(
            "kind", "event", "step", "revision", "failure", "repair", "gap", "replan",
            "attribution", "schema_version", "record_id", "created_at", "audit_ref");
    private static final Map<String, Set<String>> DETAIL_FIELDS = Map.of(
            "failure", Set.of(
                    "failure_id", "signature", "step", "check_name", "category",
                    "recoverability", "required_capability", "affected_refs", "origin",
                    "reason_code"),
            "repair", Set.of(
                    "patch_id", "kind", "step", "strategy", "attempt", "failure_ids",
                    "status", "before_score", "after_score", "baseline_fingerprint"),
            "gap", Set.of(
                    "gap_id", "signature", "step", "check_name", "category",
                    "required_capability", "status"),
            "replan", Set.of(
                    "replan_id", "trigger_step", "rollback_to", "attempt", "failure_ids",
                    "status", "before_score", "after_score", "baseline_fingerprint"),
            "attribution", Set.of(
                    "action", "reason_code", "origin", "independent_project_count",
                    "independent_run_count"));

    private final EvolutionRepository evolution;
    private final TenantContext tenantContext;
    private final ObjectMapper objectMapper;
    private final byte[] fingerprintKey;

    public EvolutionCollector(
            EvolutionRepository evolution,
            TenantContext tenantContext,
            ObjectMapper objectMapper,
            @Value("${ratsnest.evolution.fingerprint-secret:${ratsnest.agent-runtime.signing-secret:}}")
                    String fingerprintSecret) {
        this.evolution = evolution;
        this.tenantContext = tenantContext;
        this.objectMapper = objectMapper;
        this.fingerprintKey = Objects.requireNonNullElse(fingerprintSecret, "")
                .getBytes(StandardCharsets.UTF_8);
        if (fingerprintKey.length < 32) {
            throw new IllegalStateException(
                    "Evolution fingerprint secret must contain at least 32 bytes");
        }
    }

    public boolean supports(RuntimeEvent event) {
        RuntimeMessage message = event.message();
        return "message".equals(event.type())
                && message != null
                && "custom".equals(message.type())
                && "ahe_event".equals(message.customData().get("kind"));
    }

    @Transactional
    public void collect(Run run, RuntimeEvent event) {
        CollectedObservation collected = observation(run, event);
        if (collected == null) {
            return;
        }
        EvolutionObservation observation = collected.observation();
        tenantContext.activate(run.tenantId());
        if (!evolution.insertObservation(
                run.tenantId(), observation, collected.governance())) {
            return;
        }
        if (observation.failureSignature() == null
                || !("capability_gap".equals(observation.eventType())
                || "capability_gap_resolved".equals(observation.eventType()))) {
            return;
        }
        refreshCandidate(run, observation);
    }

    private CollectedObservation observation(Run run, RuntimeEvent event) {
        if (!supports(event) || event.eventId() == null || event.eventId() <= 0) {
            return null;
        }
        Map<String, Object> payload = event.message().customData();
        if (!validRecordEnvelope(payload)) {
            return null;
        }
        String recordId = required(payload.get("record_id"), 64, DIGEST);
        String eventType = required(payload.get("event"), 64, EVENT_TYPE);
        String step = required(payload.get("step"), 120, SAFE_TOKEN);
        Integer revision = nonNegativeInteger(payload.get("revision"));
        String manifestDigest = run.harnessManifestDigest();
        if (recordId == null || eventType == null || step == null || revision == null
                || manifestDigest == null
                || run.profileId() == null || run.profileVersion() == null
                || run.profileDigest() == null || !DIGEST.matcher(run.profileDigest()).matches()) {
            return null;
        }

        Map<String, Object> failure = object(payload.get("failure"));
        Map<String, Object> gap = object(payload.get("gap"));
        Map<String, Object> detail = failure.isEmpty() ? gap : failure;
        Map<String, Object> repair = object(payload.get("repair"));
        Map<String, Object> replan = object(payload.get("replan"));
        Map<String, Object> attribution = object(payload.get("attribution"));
        String failureSignature = failureSignature(detail, repair);
        EvolutionObservationGovernance governance = governance(
                eventType, step, failure, gap, attribution, failureSignature);
        if (governance == null || ("capability_gap_resolved".equals(eventType)
                && !validResolution(gap, step, failureSignature))) {
            return null;
        }
        String evidenceDigest = hmac("evidence-v1", payload);
        String scopeFingerprint = hmac(
                "scope-v1", List.of(run.tenantId().toString(), run.runId().toString()));
        String projectFingerprint = hmac(
                "project-v1", List.of(run.tenantId().toString(), run.projectId().toString()));
        Instant now = Instant.now();
        EvolutionObservation observation = new EvolutionObservation(
                recordId,
                run.runId(),
                event.eventId(),
                run.harnessVersionId(),
                run.harnessChannel(),
                manifestDigest,
                run.profileId() + "@" + run.profileVersion(),
                run.profileDigest(),
                scopeFingerprint,
                projectFingerprint,
                eventType,
                failureSignature,
                step,
                optional(detail.get("check_name"), 200),
                optional(detail.get("category"), 80),
                optional(detail.get("recoverability"), 80),
                first(
                        optional(repair.get("strategy"), 160),
                        optional(replan.get("rollback_to"), 160)),
                optional(detail.get("required_capability"), 160),
                outcome(eventType, repair),
                revision,
                evidenceDigest,
                now,
                now);
        return new CollectedObservation(observation, governance);
    }

    private void refreshCandidate(Run run, EvolutionObservation trigger) {
        List<EvolutionObservation> active = evolution.findActiveGaps(
                run.tenantId(),
                trigger.harnessVersionId(),
                trigger.harnessManifestDigest(),
                trigger.failureSignature());
        if (active.isEmpty()) {
            evolution.markAggregateStale(
                    run.tenantId(),
                    trigger.harnessVersionId(),
                    trigger.harnessManifestDigest(),
                    trigger.failureSignature());
            return;
        }
        EvolutionObservation representative = active.get(active.size() - 1);
        List<String> profiles = active.stream()
                .map(EvolutionObservation::profileReference)
                .distinct()
                .sorted()
                .limit(16)
                .toList();
        List<String> observationIds = active.stream()
                .map(EvolutionObservation::observationId)
                .distinct()
                .sorted()
                .limit(10_000)
                .toList();
        int projects = Math.toIntExact(active.stream()
                .map(EvolutionObservation::projectFingerprint)
                .distinct()
                .count());
        int runs = Math.toIntExact(active.stream()
                .map(EvolutionObservation::scopeFingerprint)
                .distinct()
                .count());
        Map<String, Object> identity = new LinkedHashMap<>();
        identity.put("baseHarnessVersionId", representative.harnessVersionId());
        identity.put("baseManifestDigest", representative.harnessManifestDigest());
        identity.put("failureSignature", representative.failureSignature());
        identity.put("step", representative.step());
        identity.put("checkName", representative.checkName());
        Instant now = Instant.now();
        evolution.upsertAggregate(
                run.tenantId(),
                new EvolutionCandidate(
                        sha256(identity),
                        representative.harnessVersionId(),
                        representative.harnessManifestDigest(),
                        representative.failureSignature(),
                        representative.step(),
                        representative.checkName(),
                        representative.category(),
                        representative.requiredCapability(),
                        profiles,
                        observationIds,
                        active.size(),
                        projects,
                        "low",
                        "unclassified",
                        projects >= 2 && runs >= 2
                                ? EvolutionCandidate.Status.ELIGIBLE
                                : EvolutionCandidate.Status.OBSERVED,
                        null,
                        1,
                        now,
                        now));
    }

    private boolean validRecordEnvelope(Map<String, Object> payload) {
        if (!TOP_LEVEL_FIELDS.containsAll(payload.keySet())
                || !integerEquals(payload.get("schema_version"), 1)
                || required(payload.get("record_id"), 64, DIGEST) == null
                || !validCreatedAt(payload.get("created_at"))) {
            return false;
        }
        Object auditReference = payload.get("audit_ref");
        if (auditReference != null) {
            String value = text(auditReference);
            if (value == null || !AUDIT_REFERENCE.matcher(value).matches()
                    || value.contains("..")) {
                return false;
            }
        }
        for (Map.Entry<String, Set<String>> entry : DETAIL_FIELDS.entrySet()) {
            if (!payload.containsKey(entry.getKey())) continue;
            Object raw = payload.get(entry.getKey());
            if (!(raw instanceof Map<?, ?> values)
                    || !entry.getValue().containsAll(values.keySet())
                    || values.values().stream().anyMatch(value -> !boundedValue(value))) {
                return false;
            }
        }
        return true;
    }

    private boolean validCreatedAt(Object value) {
        String timestamp = text(value);
        if (timestamp == null || timestamp.length() > 64) return false;
        try {
            return OffsetDateTime.parse(timestamp).getOffset().getTotalSeconds() == 0;
        } catch (DateTimeParseException exception) {
            return false;
        }
    }

    private boolean boundedValue(Object value) {
        if (value instanceof String text) return text.length() <= 200;
        if (value instanceof Boolean) return true;
        if (value instanceof Number number) return Double.isFinite(number.doubleValue());
        if (!(value instanceof List<?> values) || values.size() > 128) return false;
        return values.stream().allMatch(item ->
                item == null
                        || item instanceof Boolean
                        || item instanceof Number number && Double.isFinite(number.doubleValue())
                        || item instanceof String text && text.length() <= 200);
    }

    private EvolutionObservationGovernance governance(
            String eventType,
            String step,
            Map<String, Object> failure,
            Map<String, Object> gap,
            Map<String, Object> attribution,
            String failureSignature) {
        if ("capability_gap_resolved".equals(eventType)) {
            Integer projectCount = positiveInteger(attribution.get("independent_project_count"));
            Integer runCount = positiveInteger(attribution.get("independent_run_count"));
            if (!validResolution(gap, step, failureSignature)
                    || !"resolve_capability_gap".equals(text(attribution.get("action")))
                    || !"verified_harness_capability_gap_resolved".equals(
                            text(attribution.get("reason_code")))
                    || !"harness".equals(text(attribution.get("origin")))
                    || projectCount == null || runCount == null) {
                return null;
            }
            return new EvolutionObservationGovernance(
                    null,
                    "resolve_capability_gap",
                    "verified_harness_capability_gap_resolved",
                    "harness",
                    projectCount,
                    runCount);
        }
        if (!"capability_gap".equals(eventType)
                && !"harness_defect_observed".equals(eventType)) {
            return EvolutionObservationGovernance.none();
        }
        String expectedRecoverability = "capability_gap".equals(eventType)
                ? "capability_gap"
                : "harness_observation";
        String expectedAction = "capability_gap".equals(eventType)
                ? "capability_gap"
                : "observe_harness";
        String expectedReason = "capability_gap".equals(eventType)
                ? "cross_run_reproducible_harness_defect"
                : "harness_defect_not_yet_cross_run_reproducible";
        Integer projectCount = positiveInteger(attribution.get("independent_project_count"));
        Integer runCount = positiveInteger(attribution.get("independent_run_count"));
        String failureReason = text(failure.get("reason_code"));
        int minimum = "capability_gap".equals(eventType) ? 2 : 1;
        if (failureSignature == null
                || !step.equals(text(failure.get("step")))
                || !"harness".equals(text(failure.get("origin")))
                || !expectedRecoverability.equals(text(failure.get("recoverability")))
                || failureReason == null
                || !GOVERNED_FAILURE_REASON_CODES.contains(failureReason)
                || !expectedAction.equals(text(attribution.get("action")))
                || !expectedReason.equals(text(attribution.get("reason_code")))
                || !"harness".equals(text(attribution.get("origin")))
                || projectCount == null || projectCount < minimum
                || runCount == null || runCount < minimum) {
            return null;
        }
        return new EvolutionObservationGovernance(
                "harness", expectedAction, expectedReason, "harness", projectCount, runCount);
    }

    private boolean validResolution(
            Map<String, Object> detail,
            String step,
            String failureSignature) {
        return failureSignature != null
                && step.equals(text(detail.get("step")))
                && ("gap:" + failureSignature).equals(text(detail.get("gap_id")));
    }

    private String failureSignature(
            Map<String, Object> detail,
            Map<String, Object> repair) {
        String value = text(detail.get("signature"));
        if (value == null && repair.get("failure_ids") instanceof List<?> ids && !ids.isEmpty()) {
            value = text(ids.get(0));
            if (value != null && value.contains(":")) {
                value = value.substring(value.lastIndexOf(':') + 1);
            }
        }
        if (value == null) {
            return null;
        }
        String safe = optional(value, 128);
        return safe == null ? "hmac:" + hmac("failure-signature-v1", value) : safe;
    }

    private String outcome(String eventType, Map<String, Object> repair) {
        if ("capability_gap_resolved".equals(eventType)) {
            return "resolved";
        }
        if ("hard_constraint_conflict".equals(eventType)) {
            return "hard_conflict";
        }
        return switch (Objects.requireNonNullElse(text(repair.get("status")), "")) {
            case "improved" -> "improved";
            case "verified" -> "verified";
            case "rejected" -> "rejected";
            case "error" -> "error";
            default -> "observed";
        };
    }

    private String required(Object value, int maximum, Pattern pattern) {
        String text = text(value);
        return text != null && text.length() <= maximum && pattern.matcher(text).matches()
                ? text
                : null;
    }

    private String optional(Object value, int maximum) {
        return required(value, maximum, SAFE_TOKEN);
    }

    private String first(String first, String second) {
        return first == null ? second : first;
    }

    private String text(Object value) {
        if (!(value instanceof String text)) {
            return null;
        }
        String stripped = text.strip();
        return stripped.isEmpty() ? null : stripped;
    }

    private boolean integerEquals(Object value, int expected) {
        Integer parsed = nonNegativeInteger(value);
        return parsed != null && parsed == expected;
    }

    private Integer positiveInteger(Object value) {
        Integer parsed = nonNegativeInteger(value);
        return parsed != null && parsed > 0 ? parsed : null;
    }

    private Integer nonNegativeInteger(Object value) {
        if (!(value instanceof Number number)) return null;
        double decimal = number.doubleValue();
        long parsed = number.longValue();
        return Double.isFinite(decimal)
                && decimal == parsed
                && parsed >= 0
                && parsed <= Integer.MAX_VALUE
                ? (int) parsed
                : null;
    }

    private Map<String, Object> object(Object value) {
        if (!(value instanceof Map<?, ?> source)) {
            return Map.of();
        }
        Map<String, Object> result = new LinkedHashMap<>();
        source.forEach((key, item) -> {
            if (key instanceof String text) {
                result.put(text, item);
            }
        });
        return result;
    }

    private String hmac(String domain, Object value) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(fingerprintKey, "HmacSHA256"));
            mac.update(domain.getBytes(StandardCharsets.UTF_8));
            mac.update((byte) 0);
            return java.util.HexFormat.of().formatHex(mac.doFinal(canonicalBytes(value)));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to fingerprint evolution observation", exception);
        }
    }

    private String sha256(Object value) {
        try {
            return java.util.HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(canonicalBytes(value)));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to identify evolution observation", exception);
        }
    }

    private byte[] canonicalBytes(Object value) {
        try {
            return objectMapper.writeValueAsBytes(canonicalValue(value));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to serialize evolution evidence", exception);
        }
    }

    private Object canonicalValue(Object value) {
        if (value instanceof Map<?, ?> source) {
            Map<String, Object> sorted = new TreeMap<>();
            source.forEach((key, item) -> {
                if (key instanceof String text) {
                    sorted.put(text, canonicalValue(item));
                }
            });
            return sorted;
        }
        if (value instanceof List<?> source) {
            List<Object> result = new ArrayList<>(source.size());
            source.forEach(item -> result.add(canonicalValue(item)));
            return result;
        }
        return value;
    }

    private record CollectedObservation(
            EvolutionObservation observation,
            EvolutionObservationGovernance governance) {
    }
}

package team.ratsnest.controlplane.evolution;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import java.util.regex.Pattern;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeMessage;
import team.ratsnest.controlplane.run.Run;
import team.ratsnest.controlplane.tenancy.TenantContext;
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
        EvolutionObservation observation = observation(run, event);
        if (observation == null) {
            return;
        }
        tenantContext.activate(run.tenantId());
        if (!evolution.insertObservation(run.tenantId(), observation)) {
            return;
        }
        if (observation.failureSignature() == null
                || !("capability_gap".equals(observation.eventType())
                || "capability_gap_resolved".equals(observation.eventType()))) {
            return;
        }
        refreshCandidate(run, observation);
    }

    private EvolutionObservation observation(Run run, RuntimeEvent event) {
        if (!supports(event) || event.eventId() == null || event.eventId() <= 0) {
            return null;
        }
        Map<String, Object> payload = event.message().customData();
        String eventType = required(payload.get("event"), 64, EVENT_TYPE);
        String step = required(payload.get("step"), 120, SAFE_TOKEN);
        String manifestDigest = run.harnessManifestDigest();
        if (eventType == null || step == null || manifestDigest == null
                || run.profileId() == null || run.profileVersion() == null
                || run.profileDigest() == null || !DIGEST.matcher(run.profileDigest()).matches()) {
            return null;
        }

        Map<String, Object> detail = object(payload.get("failure"));
        if (detail.isEmpty()) {
            detail = object(payload.get("gap"));
        }
        Map<String, Object> repair = object(payload.get("repair"));
        Map<String, Object> replan = object(payload.get("replan"));
        String failureSignature = failureSignature(detail, repair);
        String evidenceDigest = hmac("evidence-v1", payload);
        String scopeFingerprint = hmac(
                "scope-v1", List.of(run.tenantId().toString(), run.runId().toString()));
        String projectFingerprint = hmac(
                "project-v1", List.of(run.tenantId().toString(), run.projectId().toString()));
        Map<String, Object> publicIdentity = new LinkedHashMap<>();
        publicIdentity.put("sourceEventSeq", event.eventId());
        publicIdentity.put("harnessManifestDigest", manifestDigest);
        publicIdentity.put("scopeFingerprint", scopeFingerprint);
        publicIdentity.put("eventType", eventType);
        publicIdentity.put("evidenceDigest", evidenceDigest);
        Instant now = Instant.now();
        return new EvolutionObservation(
                sha256(publicIdentity),
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
                nonNegativeInt(payload.get("revision")),
                evidenceDigest,
                now,
                now);
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
        Map<String, Object> identity = new LinkedHashMap<>();
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
                        projects >= 2
                                ? EvolutionCandidate.Status.ELIGIBLE
                                : EvolutionCandidate.Status.OBSERVED,
                        null,
                        1,
                        now,
                        now));
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

    private int nonNegativeInt(Object value) {
        if (!(value instanceof Number number)) {
            return 0;
        }
        long parsed = number.longValue();
        return (int) Math.max(0, Math.min(parsed, Integer.MAX_VALUE));
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
}

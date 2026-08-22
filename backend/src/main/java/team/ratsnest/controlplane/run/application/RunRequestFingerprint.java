package team.ratsnest.controlplane.run.application;

import java.security.MessageDigest;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;

import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.run.application.model.StartRequest;
import team.ratsnest.controlplane.run.domain.model.Run;
import tools.jackson.databind.ObjectMapper;

/** Canonical SHA-256 fingerprints backing all idempotent run mutations. */
@Component
class RunRequestFingerprint {

    private final ObjectMapper objectMapper;

    RunRequestFingerprint(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    String start(
            UUID tenantId,
            UUID projectId,
            String threadId,
            StartRequest request,
            Map<String, Object> config) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("tenantId", tenantId);
        value.put("projectId", projectId);
        value.put("threadId", threadId);
        value.put("message", request.message());
        value.put("model", request.model());
        value.put("config", config);
        return digest(value, "Unable to fingerprint run request");
    }

    String revision(Run parent, String feedback) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("tenantId", parent.tenantId());
        value.put("projectId", parent.projectId());
        value.put("parentRunId", parent.runId());
        value.put("feedback", feedback);
        return digest(value, "Unable to fingerprint run revision");
    }

    String interaction(String interactionId, String answer, long stateVersion) {
        return digest(
                Map.of(
                        "interactionId", interactionId,
                        "answer", answer,
                        "stateVersion", stateVersion),
                "Unable to fingerprint interaction response");
    }

    String digest(Object value, String failureMessage) {
        try {
            return java.util.HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(
                            objectMapper.writeValueAsBytes(canonicalValue(value))));
        } catch (Exception exception) {
            throw new IllegalStateException(failureMessage, exception);
        }
    }

    private Object canonicalValue(Object value) {
        if (value instanceof Map<?, ?> map) {
            Map<String, Object> sorted = new TreeMap<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!(entry.getKey() instanceof String key)) {
                    throw new IllegalArgumentException("Run request keys must be strings");
                }
                sorted.put(key, canonicalValue(entry.getValue()));
            }
            return sorted;
        }
        if (value instanceof List<?> list) {
            return list.stream().map(this::canonicalValue).toList();
        }
        return value;
    }
}

package team.ratsnest.controlplane.artifact.application;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import java.util.regex.Pattern;

import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.run.domain.model.DeliveryStatus;
import team.ratsnest.controlplane.artifact.domain.model.Artifact;
import team.ratsnest.controlplane.artifact.domain.model.ArtifactManifest;
import tools.jackson.databind.ObjectMapper;

@Component
public final class ArtifactManifestParser {

    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern KIND = Pattern.compile("[a-z0-9][a-z0-9._-]{0,79}");

    private final ObjectMapper objectMapper;

    public ArtifactManifestParser(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public ArtifactManifest parse(UUID runId, Long eventId, Map<String, Object> eventData) {
        Map<String, Object> value = object(eventData.getOrDefault("artifact_manifest", eventData));
        Long sourceEventSeq;
        if (eventId == null) {
            sourceEventSeq = optionalPositiveLong(value.get("source_event_seq"), "source_event_seq");
        } else {
            sourceEventSeq = positiveLong(eventId, "event_id");
        }
        UUID manifestId = uuid(value.get("manifest_id"), "manifest_id");
        DeliveryStatus status = deliveryStatus(value.get("delivery_status"));
        String suppliedDigest = text(value.get("manifest_digest"), "manifest_digest", 64);
        if (!SHA256.matcher(suppliedDigest).matches()) {
            throw invalid("manifest_digest must be a lowercase SHA-256 value");
        }
        List<Artifact> artifacts = list(value.get("artifacts")).stream()
                .map(item -> artifact(runId, object(item)))
                .sorted(Comparator.comparing(item -> item.artifactId().toString()))
                .toList();
        String actualDigest = digest(artifacts);
        boolean trusted = suppliedDigest.equals(actualDigest)
                && artifacts.stream().allMatch(item -> item.objectKey().startsWith("runs/" + runId + "/"));
        if (!trusted) {
            throw invalid("manifest digest or run object namespace could not be verified");
        }
        if (status == DeliveryStatus.RELEASE_READY && artifacts.isEmpty()) {
            throw invalid("release_ready requires a non-empty, digest-verified run artifact manifest");
        }
        return new ArtifactManifest(
                manifestId, sourceEventSeq, status, suppliedDigest, trusted, artifacts);
    }

    private Artifact artifact(UUID runId, Map<String, Object> value) {
        UUID artifactId = uuid(value.get("artifact_id"), "artifact_id");
        String name = text(value.get("name"), "name", 255);
        String kind = text(value.get("kind"), "kind", 80);
        String mediaType = text(value.get("media_type"), "media_type", 255);
        String sha256 = text(value.get("sha256"), "sha256", 64);
        String objectKey = text(value.get("object_key"), "object_key", 1024);
        long sizeBytes = positiveLong(value.get("size_bytes"), "size_bytes");
        if (!KIND.matcher(kind).matches() || !SHA256.matcher(sha256).matches()) {
            throw invalid("artifact kind or SHA-256 is invalid");
        }
        if (name.contains("/") || name.contains("\\") || name.contains("\"")
                || name.chars().anyMatch(Character::isISOControl)
                || objectKey.contains("..")) {
            throw invalid("artifact name or object key is unsafe");
        }
        return new Artifact(
                artifactId, runId, name, kind, mediaType, sizeBytes, sha256, objectKey, null);
    }

    private String digest(List<Artifact> artifacts) {
        List<Map<String, Object>> canonical = artifacts.stream().map(item -> {
            Map<String, Object> value = new TreeMap<>();
            value.put("artifact_id", item.artifactId().toString());
            value.put("kind", item.kind());
            value.put("media_type", item.mediaType());
            value.put("name", item.name());
            value.put("object_key", item.objectKey());
            value.put("sha256", item.sha256());
            value.put("size_bytes", item.sizeBytes());
            return value;
        }).toList();
        try {
            byte[] bytes = objectMapper.writeValueAsString(canonical).getBytes(StandardCharsets.UTF_8);
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to verify artifact manifest", exception);
        }
    }

    private DeliveryStatus deliveryStatus(Object value) {
        try {
            return DeliveryStatus.fromApiValue(text(value, "delivery_status", 32));
        } catch (IllegalArgumentException exception) {
            throw invalid("delivery_status is invalid");
        }
    }

    private UUID uuid(Object value, String field) {
        try {
            return UUID.fromString(text(value, field, 36));
        } catch (IllegalArgumentException exception) {
            throw invalid(field + " must be a UUID");
        }
    }

    private long positiveLong(Object value, String field) {
        if (!(value instanceof Number number)) {
            throw invalid(field + " must be an integer");
        }
        long result = number.longValue();
        if (result <= 0 || number.doubleValue() != result) {
            throw invalid(field + " must be a positive integer");
        }
        return result;
    }

    private Long optionalPositiveLong(Object value, String field) {
        return value == null ? null : positiveLong(value, field);
    }

    private String text(Object value, String field, int maxLength) {
        if (!(value instanceof String text)
                || text.isBlank()
                || text.length() > maxLength
                || !text.equals(text.strip())) {
            throw invalid(field + " is invalid");
        }
        return text;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> object(Object value) {
        if (!(value instanceof Map<?, ?> map)) {
            throw invalid("artifact manifest must be an object");
        }
        return (Map<String, Object>) map;
    }

    private List<?> list(Object value) {
        if (!(value instanceof List<?> list)) {
            throw invalid("artifacts must be an array");
        }
        return list;
    }

    private IllegalArgumentException invalid(String detail) {
        return new IllegalArgumentException("Invalid artifact manifest: " + detail);
    }
}

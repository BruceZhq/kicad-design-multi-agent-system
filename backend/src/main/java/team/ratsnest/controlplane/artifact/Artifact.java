package team.ratsnest.controlplane.artifact;

import java.time.Instant;
import java.util.UUID;

public record Artifact(
        UUID artifactId,
        UUID runId,
        String name,
        String kind,
        String mediaType,
        long sizeBytes,
        String sha256,
        String objectKey,
        Instant createdAt) {
}

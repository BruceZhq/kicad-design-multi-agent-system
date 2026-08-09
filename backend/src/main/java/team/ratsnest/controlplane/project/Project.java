package team.ratsnest.controlplane.project;

import java.time.Instant;
import java.util.UUID;

public record Project(
        UUID tenantId,
        UUID projectId,
        String name,
        String description,
        Instant createdAt,
        Instant updatedAt) {
}

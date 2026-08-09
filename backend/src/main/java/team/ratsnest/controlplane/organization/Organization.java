package team.ratsnest.controlplane.organization;

import java.time.Instant;
import java.util.UUID;

public record Organization(
        UUID tenantId,
        String name,
        Instant createdAt,
        Instant updatedAt) {
}

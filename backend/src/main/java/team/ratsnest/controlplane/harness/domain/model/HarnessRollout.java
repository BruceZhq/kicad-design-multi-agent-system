package team.ratsnest.controlplane.harness.domain.model;

import java.time.Instant;

public record HarnessRollout(
        String rolloutId,
        String stableVersionId,
        String previousStableVersionId,
        String canaryVersionId,
        int canaryPercent,
        long rowVersion,
        String updatedBy,
        Instant updatedAt) {
}

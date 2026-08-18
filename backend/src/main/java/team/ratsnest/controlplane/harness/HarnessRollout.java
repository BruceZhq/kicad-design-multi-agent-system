package team.ratsnest.controlplane.harness;

import java.time.Instant;

record HarnessRollout(
        String rolloutId,
        String stableVersionId,
        String previousStableVersionId,
        String canaryVersionId,
        int canaryPercent,
        long rowVersion,
        String updatedBy,
        Instant updatedAt) {
}

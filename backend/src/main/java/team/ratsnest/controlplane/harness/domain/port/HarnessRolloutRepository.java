package team.ratsnest.controlplane.harness.domain.port;

import java.util.Optional;

import team.ratsnest.controlplane.harness.domain.model.HarnessRollout;

/** Persistence boundary for stable/canary rollout state. */
public interface HarnessRolloutRepository {

    Optional<HarnessRollout> find(String rolloutId);

    boolean configureCanary(
            HarnessRollout current,
            String canaryVersionId,
            int canaryPercent,
            String updatedBy);

    boolean promote(
            HarnessRollout current,
            String promotedVersionId,
            String previousStableVersionId,
            String updatedBy);

    boolean rollback(HarnessRollout current, String targetVersionId, String updatedBy);
}

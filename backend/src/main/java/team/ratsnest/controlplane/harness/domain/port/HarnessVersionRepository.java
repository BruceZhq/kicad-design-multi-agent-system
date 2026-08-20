package team.ratsnest.controlplane.harness.domain.port;

import java.util.Optional;

import team.ratsnest.controlplane.harness.domain.model.HarnessVersion;

/** Persistence boundary for immutable harness versions and their release state. */
public interface HarnessVersionRepository {

    Optional<HarnessVersion> find(String harnessVersionId);

    boolean insert(HarnessVersion value);

    boolean transition(
            HarnessVersion current,
            HarnessVersion.ReleaseStatus target,
            String reason,
            String updatedBy);
}

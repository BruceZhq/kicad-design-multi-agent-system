package team.ratsnest.controlplane.artifact.domain.port;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import team.ratsnest.controlplane.artifact.domain.model.Artifact;
import team.ratsnest.controlplane.artifact.domain.model.ArtifactManifest;

public interface ArtifactStore {

    boolean persist(UUID tenantId, UUID runId, ArtifactManifest manifest);

    List<Artifact> findByRun(UUID tenantId, UUID runId);

    Optional<Artifact> find(UUID tenantId, UUID artifactId);

    boolean hasManifest(UUID tenantId, UUID runId);

    boolean isSuperseded(UUID tenantId, UUID runId);
}

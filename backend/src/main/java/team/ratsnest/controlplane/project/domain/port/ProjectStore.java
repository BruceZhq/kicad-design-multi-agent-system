package team.ratsnest.controlplane.project.domain.port;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.project.domain.model.Project;

public interface ProjectStore {

    void insert(
            UUID tenantId,
            UUID projectId,
            String name,
            String description,
            AuthenticatedActor actor);

    List<Project> findAll(UUID tenantId);

    Optional<Project> find(UUID tenantId, UUID projectId);

    int update(UUID tenantId, UUID projectId, String name, String description);
}

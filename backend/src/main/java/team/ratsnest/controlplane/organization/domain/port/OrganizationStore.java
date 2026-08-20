package team.ratsnest.controlplane.organization.domain.port;

import java.util.Optional;
import java.util.UUID;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.organization.domain.model.Organization;

public interface OrganizationStore {

    void insert(UUID tenantId, String name, AuthenticatedActor actor);

    Optional<Organization> find(UUID tenantId);
}

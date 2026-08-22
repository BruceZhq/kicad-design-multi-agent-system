package team.ratsnest.controlplane.tenancy.domain.port;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.tenancy.domain.model.Membership;
import team.ratsnest.controlplane.tenancy.domain.model.MembershipRole;

public interface MembershipStore {

    Optional<MembershipRole> findRole(UUID tenantId, AuthenticatedActor actor);

    Optional<MembershipRole> findRole(UUID tenantId, String issuer, String subject);

    List<UUID> findTenantIds(AuthenticatedActor actor);

    void upsert(UUID tenantId, AuthenticatedActor actor, MembershipRole role);

    List<Membership> findAll(UUID tenantId);
}

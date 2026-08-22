package team.ratsnest.controlplane.tenancy.domain.port;

import java.util.UUID;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

/** Transaction-scoped PostgreSQL tenant/principal context boundary. */
public interface TenantContext {

    void activate(UUID tenantId);

    void activatePrincipal(AuthenticatedActor actor);
}

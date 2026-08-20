package team.ratsnest.controlplane.tenancy.application;

import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.domain.model.MembershipRole;
import team.ratsnest.controlplane.tenancy.domain.port.MembershipStore;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

@Component
public final class TenantAccess {

    private final TenantContext tenantContext;
    private final MembershipStore memberships;

    public TenantAccess(TenantContext tenantContext, MembershipStore memberships) {
        this.tenantContext = tenantContext;
        this.memberships = memberships;
    }

    public MembershipRole requireMembership(UUID tenantId, AuthenticatedActor actor) {
        tenantContext.activate(tenantId);
        return memberships.findRole(tenantId, actor)
                .orElseThrow(() -> new ApiException(
                        "TENANT_ACCESS_DENIED",
                        HttpStatus.FORBIDDEN,
                        "The authenticated principal is not a member of this organization."));
    }
}

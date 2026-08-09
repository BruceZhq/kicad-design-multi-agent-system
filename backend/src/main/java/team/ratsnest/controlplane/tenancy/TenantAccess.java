package team.ratsnest.controlplane.tenancy;

import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;

@Component
public final class TenantAccess {

    private final TenantContext tenantContext;
    private final MembershipRepository memberships;

    public TenantAccess(TenantContext tenantContext, MembershipRepository memberships) {
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

package team.ratsnest.controlplane.organization;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.MembershipRepository;
import team.ratsnest.controlplane.tenancy.MembershipRole;
import team.ratsnest.controlplane.tenancy.TenantAccess;
import team.ratsnest.controlplane.tenancy.TenantContext;

@Service
public class OrganizationService {

    private final TenantContext tenantContext;
    private final TenantAccess tenantAccess;
    private final OrganizationRepository organizations;
    private final MembershipRepository memberships;

    public OrganizationService(
            TenantContext tenantContext,
            TenantAccess tenantAccess,
            OrganizationRepository organizations,
            MembershipRepository memberships) {
        this.tenantContext = tenantContext;
        this.tenantAccess = tenantAccess;
        this.organizations = organizations;
        this.memberships = memberships;
    }

    @Transactional
    public Organization create(String name, AuthenticatedActor actor) {
        UUID tenantId = UUID.randomUUID();
        tenantContext.activate(tenantId);
        organizations.insert(tenantId, name.strip(), actor);
        memberships.upsert(tenantId, actor, MembershipRole.OWNER);
        return getRequired(tenantId);
    }

    @Transactional(readOnly = true)
    public Organization get(UUID tenantId, AuthenticatedActor actor) {
        tenantAccess.requireMembership(tenantId, actor);
        return getRequired(tenantId);
    }

    @Transactional(readOnly = true)
    public List<Organization> list(AuthenticatedActor actor) {
        tenantContext.activatePrincipal(actor);
        List<UUID> tenantIds = memberships.findTenantIds(actor);
        List<Organization> result = new ArrayList<>(tenantIds.size());
        for (UUID tenantId : tenantIds) {
            tenantContext.activate(tenantId);
            organizations.find(tenantId).ifPresent(result::add);
        }
        return List.copyOf(result);
    }

    private Organization getRequired(UUID tenantId) {
        return organizations.find(tenantId)
                .orElseThrow(() -> new ApiException(
                        "ORGANIZATION_NOT_FOUND",
                        HttpStatus.NOT_FOUND,
                        "The organization was not found."));
    }
}

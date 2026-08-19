package dev.ratsnest.security;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.tenant.TenantAccessService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * The single ownership policy for design runs: open-mode rows (no owner)
 * are public; owned rows are visible to their owner, admins, and the
 * agent-runtime service identity. Controllers must not re-implement this.
 */
@Component
public class RunAccessPolicy {

    private final TenantAccessService tenants;

    @Value("${ratsnest.security.mode:open}")
    private String securityMode;

    public RunAccessPolicy(TenantAccessService tenants) {
        this.tenants = tenants;
    }

    /** Logged-in username, or null for anonymous / service callers. */
    public String currentUser() {
        return tenants.currentUsername();
    }

    public boolean currentIsAdmin() {
        return tenants.currentIsPlatformAdmin() || tenants.currentIsService();
    }

    public boolean canAccess(DesignRun run) {
        if (run.getOrganizationId() != null) {
            return tenants.canAccessOrganization(run.getOrganizationId());
        }
        if (run.getOwner() == null) {
            return true;                 // open mode / legacy rows
        }
        return currentIsAdmin() || run.getOwner().equals(currentUser());
    }

    public boolean canApprove(DesignRun run) {
        if ("open".equalsIgnoreCase(securityMode) && currentUser() == null) {
            return true;
        }
        if (run.getOrganizationId() != null) {
            return tenants.canApproveOrganization(run.getOrganizationId());
        }
        return currentIsAdmin() || (run.getOwner() != null
                && run.getOwner().equals(currentUser()));
    }
}

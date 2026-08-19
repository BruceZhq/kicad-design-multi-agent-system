package dev.ratsnest.tenant;

import dev.ratsnest.auth.UserAccount;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Locale;
import java.util.UUID;

@Service
public class TenantProvisioningService {

    public record ProvisionedTenant(Organization organization,
                                    Workspace workspace,
                                    HardwareProject project) {}

    private final OrganizationRepository organizations;
    private final OrganizationMembershipRepository memberships;
    private final WorkspaceRepository workspaces;
    private final HardwareProjectRepository projects;

    public TenantProvisioningService(OrganizationRepository organizations,
                                     OrganizationMembershipRepository memberships,
                                     WorkspaceRepository workspaces,
                                     HardwareProjectRepository projects) {
        this.organizations = organizations;
        this.memberships = memberships;
        this.workspaces = workspaces;
        this.projects = projects;
    }

    @Transactional
    public ProvisionedTenant provisionPersonalTenant(UserAccount user) {
        return provisionOrganization(user, user.getUsername() + " organization");
    }

    @Transactional
    public ProvisionedTenant provisionOrganization(UserAccount user, String name) {
        String base = slugify(user.getUsername());
        String slug = base + "-" + UUID.randomUUID().toString().substring(0, 8);
        Organization organization = organizations.save(Organization.create(
                name, slug));
        memberships.save(OrganizationMembership.create(
                organization.getId(), user.getId(), "OWNER"));
        Workspace workspace = workspaces.save(Workspace.create(
                organization.getId(), "Default", "default", user.getId()));
        HardwareProject project = projects.save(HardwareProject.create(
                organization.getId(), workspace.getId(), "Sandbox",
                "Default hardware design project", user.getId()));
        return new ProvisionedTenant(organization, workspace, project);
    }

    @Transactional
    public ProvisionedTenant ensureTenant(UserAccount user) {
        var existingMembership = memberships
                .findByUserIdOrderByCreatedAtAsc(user.getId()).stream()
                .findFirst();
        if (existingMembership.isEmpty()) {
            return provisionPersonalTenant(user);
        }
        Organization organization = organizations.findById(
                        existingMembership.get().getOrganizationId())
                .orElseGet(() -> organizations.save(Organization.create(
                        user.getUsername() + " organization",
                        slugify(user.getUsername()) + "-"
                                + UUID.randomUUID().toString().substring(0, 8))));
        Workspace workspace = workspaces
                .findByOrganizationIdOrderByNameAsc(organization.getId())
                .stream().findFirst()
                .orElseGet(() -> workspaces.save(Workspace.create(
                        organization.getId(), "Default", "default",
                        user.getId())));
        HardwareProject project = projects
                .findByWorkspaceIdOrderByNameAsc(workspace.getId())
                .stream().findFirst()
                .orElseGet(() -> projects.save(HardwareProject.create(
                        organization.getId(), workspace.getId(), "Sandbox",
                        "Default hardware design project", user.getId())));
        return new ProvisionedTenant(organization, workspace, project);
    }

    static String slugify(String value) {
        String slug = value.toLowerCase(Locale.ROOT)
                .replaceAll("[^a-z0-9]+", "-")
                .replaceAll("^-+|-+$", "");
        return slug.isBlank() ? "organization" : slug.substring(
                0, Math.min(slug.length(), 50));
    }
}

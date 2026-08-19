package dev.ratsnest.tenant;

import dev.ratsnest.auth.UserAccount;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.util.List;

@RestController
@RequestMapping("/api/tenant")
public class TenantController {

    public record NamedRequest(@NotBlank @Size(max = 120) String name) {}
    public record CreateWorkspaceRequest(
            @NotBlank String organizationId,
            @NotBlank @Size(max = 120) String name) {}
    public record CreateProjectRequest(
            @NotBlank String workspaceId,
            @NotBlank @Size(max = 160) String name,
            @Size(max = 1000) String description) {}

    public record ProjectView(String id, String name, String description) {}
    public record WorkspaceView(String id, String name,
                                List<ProjectView> projects) {}
    public record OrganizationView(String id, String name, String slug,
                                   String role,
                                   List<WorkspaceView> workspaces) {}
    public record TenantContext(String userId, String username,
                                List<OrganizationView> organizations) {}

    private final TenantAccessService access;
    private final TenantProvisioningService provisioning;
    private final OrganizationRepository organizations;
    private final OrganizationMembershipRepository memberships;
    private final WorkspaceRepository workspaces;
    private final HardwareProjectRepository projects;

    public TenantController(TenantAccessService access,
                            TenantProvisioningService provisioning,
                            OrganizationRepository organizations,
                            OrganizationMembershipRepository memberships,
                            WorkspaceRepository workspaces,
                            HardwareProjectRepository projects) {
        this.access = access;
        this.provisioning = provisioning;
        this.organizations = organizations;
        this.memberships = memberships;
        this.workspaces = workspaces;
        this.projects = projects;
    }

    @GetMapping("/context")
    public TenantContext context() {
        UserAccount user = requireUser();
        provisioning.ensureTenant(user);
        List<OrganizationMembership> userMemberships = memberships
                .findByUserIdOrderByCreatedAtAsc(user.getId());
        List<OrganizationView> views = userMemberships.stream()
                .map(membership -> organizations.findById(
                                membership.getOrganizationId())
                        .map(organization -> organizationView(
                                organization, membership.getRole()))
                        .orElse(null))
                .filter(java.util.Objects::nonNull)
                .toList();
        return new TenantContext(user.getId(), user.getUsername(), views);
    }

    @PostMapping("/organizations")
    public ResponseEntity<OrganizationView> createOrganization(
            @Valid @RequestBody NamedRequest request) {
        UserAccount user = requireUser();
        var tenant = provisioning.provisionOrganization(
                user, request.name().trim());
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(organizationView(tenant.organization(), "OWNER"));
    }

    @PostMapping("/workspaces")
    public ResponseEntity<WorkspaceView> createWorkspace(
            @Valid @RequestBody CreateWorkspaceRequest request) {
        UserAccount user = requireUser();
        if (!access.canAccessOrganization(request.organizationId())) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND,
                    "organization not found");
        }
        String slug = TenantProvisioningService.slugify(request.name())
                + "-" + java.util.UUID.randomUUID().toString().substring(0, 6);
        Workspace workspace = workspaces.save(Workspace.create(
                request.organizationId(), request.name().trim(), slug,
                user.getId()));
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(new WorkspaceView(workspace.getId(), workspace.getName(),
                        List.of()));
    }

    @PostMapping("/projects")
    public ResponseEntity<ProjectView> createProject(
            @Valid @RequestBody CreateProjectRequest request) {
        UserAccount user = requireUser();
        Workspace workspace = workspaces.findById(request.workspaceId())
                .filter(value -> access.canAccessOrganization(
                        value.getOrganizationId()))
                .orElseThrow(() -> new ResponseStatusException(
                        HttpStatus.NOT_FOUND, "workspace not found"));
        HardwareProject project = projects.save(HardwareProject.create(
                workspace.getOrganizationId(), workspace.getId(),
                request.name().trim(), request.description(), user.getId()));
        return ResponseEntity.status(HttpStatus.CREATED)
                .body(projectView(project));
    }

    private UserAccount requireUser() {
        return access.currentUser().orElseThrow(() ->
                new ResponseStatusException(HttpStatus.UNAUTHORIZED,
                        "authentication required"));
    }

    private OrganizationView organizationView(Organization organization,
                                              String role) {
        List<WorkspaceView> workspaceViews = workspaces
                .findByOrganizationIdOrderByNameAsc(organization.getId())
                .stream().map(workspace -> new WorkspaceView(
                        workspace.getId(), workspace.getName(),
                        projects.findByWorkspaceIdOrderByNameAsc(
                                        workspace.getId()).stream()
                                .map(TenantController::projectView)
                                .toList()))
                .toList();
        return new OrganizationView(organization.getId(), organization.getName(),
                organization.getSlug(), role, workspaceViews);
    }

    private static ProjectView projectView(HardwareProject project) {
        return new ProjectView(project.getId(), project.getName(),
                project.getDescription());
    }
}

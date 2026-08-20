package team.ratsnest.controlplane.project.application;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.project.domain.model.Project;
import team.ratsnest.controlplane.project.domain.port.ProjectStore;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.application.TenantAccess;
import team.ratsnest.controlplane.tenancy.domain.model.MembershipRole;

@Service
public class ProjectService {

    private final TenantAccess tenantAccess;
    private final ProjectStore projects;

    public ProjectService(TenantAccess tenantAccess, ProjectStore projects) {
        this.tenantAccess = tenantAccess;
        this.projects = projects;
    }

    @Transactional(readOnly = true)
    public List<Project> list(UUID tenantId, AuthenticatedActor actor) {
        tenantAccess.requireMembership(tenantId, actor);
        return projects.findAll(tenantId);
    }

    @Transactional(readOnly = true)
    public Project get(UUID tenantId, UUID projectId, AuthenticatedActor actor) {
        tenantAccess.requireMembership(tenantId, actor);
        return getRequired(tenantId, projectId);
    }

    @Transactional
    public Project create(
            UUID tenantId,
            String name,
            String description,
            AuthenticatedActor actor) {
        requireWriter(tenantId, actor);
        UUID projectId = UUID.randomUUID();
        projects.insert(tenantId, projectId, name.strip(), normalizeDescription(description), actor);
        return getRequired(tenantId, projectId);
    }

    @Transactional
    public Project update(
            UUID tenantId,
            UUID projectId,
            String name,
            String description,
            AuthenticatedActor actor) {
        requireWriter(tenantId, actor);
        int updated = projects.update(
                tenantId,
                projectId,
                name.strip(),
                normalizeDescription(description));
        if (updated == 0) {
            throw notFound();
        }
        return getRequired(tenantId, projectId);
    }

    private void requireWriter(UUID tenantId, AuthenticatedActor actor) {
        MembershipRole role = tenantAccess.requireMembership(tenantId, actor);
        if (!role.canWriteProjects()) {
            throw new ApiException(
                    "PROJECT_WRITE_DENIED",
                    HttpStatus.FORBIDDEN,
                    "The organization role cannot create or modify projects.");
        }
    }

    private Project getRequired(UUID tenantId, UUID projectId) {
        return projects.find(tenantId, projectId).orElseThrow(this::notFound);
    }

    private ApiException notFound() {
        return new ApiException(
                "PROJECT_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The project was not found.");
    }

    private String normalizeDescription(String description) {
        return description == null ? "" : description.strip();
    }
}

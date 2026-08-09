package team.ratsnest.controlplane.project;

import static team.ratsnest.controlplane.organization.OrganizationController.ORGANIZATION_HEADER;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import team.ratsnest.controlplane.identity.AuthenticatedActor;

@RestController
@RequestMapping("/api/v1/projects")
public class ProjectController {

    private final ProjectService projects;

    public ProjectController(ProjectService projects) {
        this.projects = projects;
    }

    @GetMapping
    List<ProjectResponse> list(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @AuthenticationPrincipal Jwt jwt) {
        return projects.list(tenantId, AuthenticatedActor.from(jwt)).stream()
                .map(ProjectResponse::from)
                .toList();
    }

    @GetMapping("/{projectId}")
    ProjectResponse get(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @AuthenticationPrincipal Jwt jwt) {
        return ProjectResponse.from(
                projects.get(tenantId, projectId, AuthenticatedActor.from(jwt)));
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    ProjectResponse create(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @Valid @RequestBody CreateProjectRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return ProjectResponse.from(projects.create(
                tenantId,
                request.name(),
                request.description(),
                AuthenticatedActor.from(jwt)));
    }

    @PutMapping("/{projectId}")
    ProjectResponse update(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @Valid @RequestBody UpdateProjectRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return ProjectResponse.from(projects.update(
                tenantId,
                projectId,
                request.name(),
                request.description(),
                AuthenticatedActor.from(jwt)));
    }

    record CreateProjectRequest(
            @NotBlank @Size(max = 200) String name,
            @Size(max = 2000) String description) {
    }

    record UpdateProjectRequest(
            @NotBlank @Size(max = 200) String name,
            @Size(max = 2000) String description) {
    }

    record ProjectResponse(
            UUID tenantId,
            UUID projectId,
            String name,
            String description,
            Instant createdAt,
            Instant updatedAt) {

        static ProjectResponse from(Project project) {
            return new ProjectResponse(
                    project.tenantId(),
                    project.projectId(),
                    project.name(),
                    project.description(),
                    project.createdAt(),
                    project.updatedAt());
        }
    }
}

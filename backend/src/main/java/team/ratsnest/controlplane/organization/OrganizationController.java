package team.ratsnest.controlplane.organization;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
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
@Validated
@RequestMapping("/api/v1/organizations")
public class OrganizationController {

    public static final String ORGANIZATION_HEADER = "X-Organization-ID";

    private final OrganizationService organizations;

    public OrganizationController(OrganizationService organizations) {
        this.organizations = organizations;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    OrganizationResponse create(
            @Valid @RequestBody CreateOrganizationRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return OrganizationResponse.from(
                organizations.create(request.name(), AuthenticatedActor.from(jwt)));
    }

    @GetMapping
    List<OrganizationResponse> list(@AuthenticationPrincipal Jwt jwt) {
        return organizations.list(AuthenticatedActor.from(jwt)).stream()
                .map(OrganizationResponse::from)
                .toList();
    }

    @GetMapping("/current")
    OrganizationResponse current(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @AuthenticationPrincipal Jwt jwt) {
        return OrganizationResponse.from(
                organizations.get(tenantId, AuthenticatedActor.from(jwt)));
    }

    record CreateOrganizationRequest(
            @NotBlank @Size(max = 200) String name) {
    }

    record OrganizationResponse(
            UUID tenantId,
            String name,
            Instant createdAt,
            Instant updatedAt) {

        static OrganizationResponse from(Organization organization) {
            return new OrganizationResponse(
                    organization.tenantId(),
                    organization.name(),
                    organization.createdAt(),
                    organization.updatedAt());
        }
    }
}

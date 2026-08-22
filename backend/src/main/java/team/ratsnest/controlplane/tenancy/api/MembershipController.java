package team.ratsnest.controlplane.tenancy.api;

import static team.ratsnest.controlplane.shared.web.ApiHeaders.ORGANIZATION_HEADER;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import team.ratsnest.controlplane.identity.api.JwtIdentity;
import team.ratsnest.controlplane.tenancy.application.MembershipService;
import team.ratsnest.controlplane.tenancy.domain.model.Membership;

@RestController
@RequestMapping("/api/v1/memberships")
public class MembershipController {

    private final MembershipService memberships;

    public MembershipController(MembershipService memberships) {
        this.memberships = memberships;
    }

    @GetMapping
    List<MembershipResponse> list(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @AuthenticationPrincipal Jwt jwt) {
        return memberships.list(tenantId, JwtIdentity.from(jwt)).stream()
                .map(MembershipResponse::from)
                .toList();
    }

    @PutMapping
    MembershipResponse put(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @Valid @RequestBody PutMembershipRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return MembershipResponse.from(memberships.put(
                tenantId,
                JwtIdentity.from(jwt),
                request.issuer(),
                request.subject(),
                request.role()));
    }

    record PutMembershipRequest(
            @NotBlank @Size(max = 2048) String issuer,
            @NotBlank @Size(max = 255) String subject,
            @NotBlank @Size(max = 16) String role) {
    }

    record MembershipResponse(
            String issuer,
            String subject,
            String role,
            Instant createdAt,
            Instant updatedAt) {

        static MembershipResponse from(Membership membership) {
            return new MembershipResponse(
                    membership.issuer(),
                    membership.subject(),
                    membership.role().wireValue(),
                    membership.createdAt(),
                    membership.updatedAt());
        }
    }
}

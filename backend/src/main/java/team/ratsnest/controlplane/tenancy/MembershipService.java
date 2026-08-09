package team.ratsnest.controlplane.tenancy;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.MembershipRepository.Membership;

@Service
public class MembershipService {

    private final TenantAccess tenantAccess;
    private final MembershipRepository memberships;

    public MembershipService(TenantAccess tenantAccess, MembershipRepository memberships) {
        this.tenantAccess = tenantAccess;
        this.memberships = memberships;
    }

    @Transactional(readOnly = true)
    public List<Membership> list(UUID tenantId, AuthenticatedActor actor) {
        requireManager(tenantId, actor);
        return memberships.findAll(tenantId);
    }

    @Transactional
    public Membership put(
            UUID tenantId,
            AuthenticatedActor actor,
            String targetIssuer,
            String targetSubject,
            String roleValue) {
        MembershipRole actorRole = requireManager(tenantId, actor);
        MembershipRole targetRole = MembershipRole.fromWireValue(roleValue);
        AuthenticatedActor target = validateTarget(targetIssuer, targetSubject);
        MembershipRole existingRole = memberships.findRole(tenantId, target).orElse(null);

        if (actorRole == MembershipRole.ADMIN
                && (targetRole == MembershipRole.OWNER || existingRole == MembershipRole.OWNER)) {
            throw new ApiException(
                    "OWNER_MEMBERSHIP_REQUIRES_OWNER",
                    HttpStatus.FORBIDDEN,
                    "Only an owner can manage owner memberships.");
        }
        if (existingRole == MembershipRole.OWNER && targetRole != MembershipRole.OWNER) {
            throw new ApiException(
                    "OWNER_DEMOTION_REQUIRES_TRANSFER",
                    HttpStatus.CONFLICT,
                    "Owner membership cannot be demoted without an ownership transfer.");
        }

        memberships.upsert(tenantId, target, targetRole);
        return memberships.findAll(tenantId).stream()
                .filter(membership -> membership.issuer().equals(target.issuer())
                        && membership.subject().equals(target.subject()))
                .findFirst()
                .orElseThrow();
    }

    private MembershipRole requireManager(UUID tenantId, AuthenticatedActor actor) {
        MembershipRole role = tenantAccess.requireMembership(tenantId, actor);
        if (!role.canManageMemberships()) {
            throw new ApiException(
                    "MEMBERSHIP_MANAGEMENT_DENIED",
                    HttpStatus.FORBIDDEN,
                    "The organization role cannot manage memberships.");
        }
        return role;
    }

    private AuthenticatedActor validateTarget(String issuer, String subject) {
        if (issuer == null || issuer.isBlank() || issuer.length() > 2048
                || subject == null || subject.isBlank() || subject.length() > 255) {
            throw new ApiException(
                    "INVALID_MEMBERSHIP_PRINCIPAL",
                    HttpStatus.BAD_REQUEST,
                    "Membership issuer and subject are required.");
        }
        return new AuthenticatedActor(issuer, subject);
    }
}

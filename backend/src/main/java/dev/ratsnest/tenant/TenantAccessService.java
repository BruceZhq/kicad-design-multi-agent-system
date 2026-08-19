package dev.ratsnest.tenant;

import dev.ratsnest.auth.UserAccount;
import dev.ratsnest.auth.UserAccountRepository;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import java.util.List;
import java.util.Optional;

@Service
public class TenantAccessService {

    private final UserAccountRepository users;
    private final OrganizationMembershipRepository memberships;
    private final HardwareProjectRepository projects;

    public TenantAccessService(UserAccountRepository users,
                               OrganizationMembershipRepository memberships,
                               HardwareProjectRepository projects) {
        this.users = users;
        this.memberships = memberships;
        this.projects = projects;
    }

    public Authentication currentAuthentication() {
        return SecurityContextHolder.getContext().getAuthentication();
    }

    public String currentUsername() {
        Authentication auth = currentAuthentication();
        if (auth == null || !auth.isAuthenticated()
                || "anonymousUser".equals(auth.getName())
                || "agent-runtime".equals(auth.getName())) {
            return null;
        }
        return auth.getName();
    }

    public Optional<UserAccount> currentUser() {
        String username = currentUsername();
        return username == null ? Optional.empty() : users.findByUsername(username);
    }

    public boolean currentIsService() {
        Authentication auth = currentAuthentication();
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> a.getAuthority().contains("SERVICE"));
    }

    public boolean currentIsPlatformAdmin() {
        Authentication auth = currentAuthentication();
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(a -> a.getAuthority().equals("ROLE_ADMIN"));
    }

    public List<String> currentOrganizationIds() {
        return currentUser().map(user -> memberships
                        .findByUserIdOrderByCreatedAtAsc(user.getId()).stream()
                        .map(OrganizationMembership::getOrganizationId)
                        .toList())
                .orElseGet(List::of);
    }

    public boolean canAccessOrganization(String organizationId) {
        if (organizationId == null) {
            return false;
        }
        if (currentIsService() || currentIsPlatformAdmin()) {
            return true;
        }
        return currentUser().map(user -> memberships
                        .existsByOrganizationIdAndUserId(
                                organizationId, user.getId()))
                .orElse(false);
    }

    public boolean canApproveOrganization(String organizationId) {
        if (currentIsService() || currentIsPlatformAdmin()) {
            return true;
        }
        return currentUser().flatMap(user -> memberships
                        .findByOrganizationIdAndUserId(
                                organizationId, user.getId()))
                .map(OrganizationMembership::getRole)
                .map(role -> role.matches("OWNER|ADMIN|LEAD|REVIEWER"))
                .orElse(false);
    }

    public HardwareProject resolveProject(String requestedProjectId) {
        Optional<UserAccount> user = currentUser();
        if (user.isEmpty()) {
            if (requestedProjectId != null && !requestedProjectId.isBlank()) {
                throw new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "project not found");
            }
            return null;
        }

        if (requestedProjectId != null && !requestedProjectId.isBlank()) {
            HardwareProject project = projects.findById(requestedProjectId)
                    .orElseThrow(() -> new ResponseStatusException(
                            HttpStatus.NOT_FOUND, "project not found"));
            if (!canAccessOrganization(project.getOrganizationId())) {
                throw new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "project not found");
            }
            return project;
        }

        List<String> organizationIds = currentOrganizationIds();
        return projects.findByOrganizationIdInOrderByCreatedAtAsc(
                        organizationIds).stream().findFirst()
                .orElseThrow(() -> new ResponseStatusException(
                        HttpStatus.CONFLICT,
                        "account has no hardware project"));
    }
}

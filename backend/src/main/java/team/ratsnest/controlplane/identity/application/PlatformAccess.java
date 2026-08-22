package team.ratsnest.controlplane.identity.application;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.identity.application.model.PlatformPrincipal;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;

/** Platform-wide release authority, separate from tenant membership roles. */
@Component
public final class PlatformAccess {

    private static final String ADMIN_ROLE = "ratsnest-platform-admin";
    private static final String ADMIN_SCOPE = "ratsnest.harness.admin";

    public AuthenticatedActor requireHarnessAdmin(PlatformPrincipal principal) {
        if (!principal.roles().contains(ADMIN_ROLE) && !principal.scopes().contains(ADMIN_SCOPE)) {
            throw new ApiException(
                    "PLATFORM_HARNESS_ADMIN_REQUIRED",
                    HttpStatus.FORBIDDEN,
                    "A platform harness administrator role is required.");
        }
        return principal.actor();
    }
}

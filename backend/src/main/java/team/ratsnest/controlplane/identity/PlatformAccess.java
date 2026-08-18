package team.ratsnest.controlplane.identity;

import java.util.Collection;
import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.shared.web.ApiException;

/** Platform-wide release authority, separate from tenant membership roles. */
@Component
public final class PlatformAccess {

    private static final String ADMIN_ROLE = "ratsnest-platform-admin";
    private static final String ADMIN_SCOPE = "ratsnest.harness.admin";

    public AuthenticatedActor requireHarnessAdmin(Jwt jwt) {
        if (!hasRole(jwt) && !hasScopeList(jwt) && !hasScopeText(jwt)) {
            throw new ApiException(
                    "PLATFORM_HARNESS_ADMIN_REQUIRED",
                    HttpStatus.FORBIDDEN,
                    "A platform harness administrator role is required.");
        }
        return AuthenticatedActor.from(jwt);
    }

    private boolean hasScopeList(Jwt jwt) {
        java.util.List<String> scopes = jwt.getClaimAsStringList("scp");
        return scopes != null && scopes.contains(ADMIN_SCOPE);
    }

    private boolean hasScopeText(Jwt jwt) {
        String scope = jwt.getClaimAsString("scope");
        return scope != null
                && java.util.Arrays.asList(scope.split(" ")).contains(ADMIN_SCOPE);
    }

    private boolean hasRole(Jwt jwt) {
        if (contains(jwt.getClaim("roles"), ADMIN_ROLE)) {
            return true;
        }
        Object realm = jwt.getClaim("realm_access");
        return realm instanceof Map<?, ?> values && contains(values.get("roles"), ADMIN_ROLE);
    }

    private boolean contains(Object value, String expected) {
        return value instanceof Collection<?> values && values.contains(expected);
    }
}

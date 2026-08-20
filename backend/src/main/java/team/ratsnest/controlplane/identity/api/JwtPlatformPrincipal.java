package team.ratsnest.controlplane.identity.api;

import java.util.Arrays;
import java.util.Collection;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

import org.springframework.security.oauth2.jwt.Jwt;

import team.ratsnest.controlplane.identity.application.model.PlatformPrincipal;

/** Maps platform roles and scopes from the bearer token at the API boundary. */
public final class JwtPlatformPrincipal {

    private JwtPlatformPrincipal() {
    }

    public static PlatformPrincipal from(Jwt jwt) {
        Set<String> roles = new HashSet<>();
        addStrings(roles, jwt.getClaim("roles"));
        Object realm = jwt.getClaim("realm_access");
        if (realm instanceof Map<?, ?> values) {
            addStrings(roles, values.get("roles"));
        }

        Set<String> scopes = new HashSet<>();
        java.util.List<String> scopeList = jwt.getClaimAsStringList("scp");
        if (scopeList != null) scopes.addAll(scopeList);
        String scopeText = jwt.getClaimAsString("scope");
        if (scopeText != null) scopes.addAll(Arrays.asList(scopeText.split(" ")));
        return new PlatformPrincipal(JwtIdentity.from(jwt), roles, scopes);
    }

    private static void addStrings(Set<String> target, Object value) {
        if (!(value instanceof Collection<?> values)) return;
        for (Object candidate : values) {
            if (candidate instanceof String string) target.add(string);
        }
    }
}

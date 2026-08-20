package team.ratsnest.controlplane.profile.api;

import org.springframework.security.oauth2.jwt.Jwt;

import team.ratsnest.controlplane.identity.api.JwtIdentity;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.profile.application.model.ProfileIdentity;

final class JwtProfileIdentity {

    private JwtProfileIdentity() {
    }

    static ProfileIdentity from(Jwt jwt) {
        AuthenticatedActor actor = JwtIdentity.from(jwt);
        String username = claim(jwt, "preferred_username", 255);
        String email = claim(jwt, "email", 320);
        String name = claim(jwt, "name", 120);
        if (username == null) username = email == null ? actor.subject() : email;
        if (name == null) name = username;
        return new ProfileIdentity(actor, username, email, limit(name, 120));
    }

    private static String claim(Jwt jwt, String name, int maximumLength) {
        Object raw = jwt.getClaims().get(name);
        if (!(raw instanceof String value)) return null;
        String result = value.strip();
        return result.isEmpty() || result.length() > maximumLength ? null : result;
    }

    private static String limit(String value, int maximumLength) {
        if (value.codePointCount(0, value.length()) <= maximumLength) return value;
        return value.substring(0, value.offsetByCodePoints(0, maximumLength));
    }
}

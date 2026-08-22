package team.ratsnest.controlplane.identity.api;

import java.net.URL;

import org.springframework.http.HttpStatus;
import org.springframework.security.oauth2.jwt.Jwt;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;

/** Maps and validates the HTTP bearer-token principal at the API boundary. */
public final class JwtIdentity {

    private JwtIdentity() {
    }

    public static AuthenticatedActor from(Jwt jwt) {
        URL issuer = jwt.getIssuer();
        String subject = jwt.getSubject();
        if (issuer == null || subject == null || subject.isBlank()
                || issuer.toString().length() > 2048 || subject.length() > 255) {
            throw new ApiException(
                    "INVALID_PRINCIPAL",
                    HttpStatus.UNAUTHORIZED,
                    "The bearer token does not contain a valid issuer and subject.");
        }
        return new AuthenticatedActor(issuer.toString(), subject);
    }
}

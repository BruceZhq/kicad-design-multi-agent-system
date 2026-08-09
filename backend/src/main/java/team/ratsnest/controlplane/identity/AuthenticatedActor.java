package team.ratsnest.controlplane.identity;

import java.net.URL;

import org.springframework.http.HttpStatus;
import org.springframework.security.oauth2.jwt.Jwt;

import team.ratsnest.controlplane.shared.web.ApiException;

public record AuthenticatedActor(String issuer, String subject) {

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

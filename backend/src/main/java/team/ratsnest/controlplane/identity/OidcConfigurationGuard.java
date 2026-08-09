package team.ratsnest.controlplane.identity;

import java.net.URI;
import java.util.Arrays;
import java.util.Set;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

@Component
public final class OidcConfigurationGuard {

    private static final Set<String> LOOPBACK_HOSTS = Set.of("localhost", "127.0.0.1", "::1");

    public OidcConfigurationGuard(
            @Value("${spring.security.oauth2.resourceserver.jwt.issuer-uri:}") String issuerUri,
            @Value("${spring.security.oauth2.resourceserver.jwt.jwk-set-uri:}") String jwkSetUri,
            @Value("${spring.security.oauth2.resourceserver.jwt.audiences[0]:}") String audience,
            Environment environment) {
        String[] profiles = environment.getActiveProfiles();
        boolean hasDevelopment = Arrays.asList(profiles).contains("dev");
        if (hasDevelopment && profiles.length != 1) {
            throw new IllegalStateException("The dev profile cannot be combined with other profiles");
        }
        URI issuer = validateEndpoint("issuer URI", issuerUri, hasDevelopment);
        URI jwkSet = validateEndpoint("JWK set URI", jwkSetUri, hasDevelopment);
        if (!sameOrigin(issuer, jwkSet)) {
            throw new IllegalStateException("OIDC issuer and JWK set URI must use the same origin");
        }
        if (audience == null || audience.isBlank()) {
            throw new IllegalStateException("OIDC audience is required");
        }
    }

    private URI validateEndpoint(String name, String value, boolean development) {
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("OIDC " + name + " is required");
        }

        URI uri;
        try {
            uri = URI.create(value);
        } catch (IllegalArgumentException exception) {
            throw new IllegalStateException("OIDC " + name + " is invalid", exception);
        }

        if (!uri.isAbsolute() || uri.getHost() == null || uri.getUserInfo() != null || uri.getFragment() != null) {
            throw new IllegalStateException("OIDC " + name + " must be an absolute endpoint URI");
        }
        if ("https".equalsIgnoreCase(uri.getScheme())) {
            return uri;
        }
        if (development
                && "http".equalsIgnoreCase(uri.getScheme())
                && isDevelopmentLoopbackHost(uri.getHost())) {
            return uri;
        }
        throw new IllegalStateException("OIDC " + name + " must use HTTPS");
    }

    private boolean isDevelopmentLoopbackHost(String host) {
        String normalizedHost = host.toLowerCase();
        return LOOPBACK_HOSTS.contains(normalizedHost) || normalizedHost.endsWith(".localhost");
    }

    private boolean sameOrigin(URI first, URI second) {
        return first.getScheme().equalsIgnoreCase(second.getScheme())
                && first.getHost().equalsIgnoreCase(second.getHost())
                && effectivePort(first) == effectivePort(second);
    }

    private int effectivePort(URI uri) {
        if (uri.getPort() >= 0) {
            return uri.getPort();
        }
        return "https".equalsIgnoreCase(uri.getScheme()) ? 443 : 80;
    }
}

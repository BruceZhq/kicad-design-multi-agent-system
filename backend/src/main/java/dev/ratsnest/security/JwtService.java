package dev.ratsnest.security;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.time.temporal.ChronoUnit;

/** HMAC-signed JWTs (HS256). The secret comes from configuration — set a
 *  strong RATSNEST_JWT_SECRET in every non-dev deployment. */
@Service
public class JwtService {

    private final JwtEncoder encoder;

    @Value("${ratsnest.security.token-hours:24}")
    private long tokenHours;

    public JwtService(JwtEncoder encoder) {
        this.encoder = encoder;
    }

    public String issue(String username, String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer("ratsnest")
                .subject(username)
                .claim("role", role)
                .issuedAt(now)
                .expiresAt(now.plus(tokenHours, ChronoUnit.HOURS))
                .build();
        JwsHeader header = JwsHeader.with(MacAlgorithm.HS256).build();
        return encoder.encode(JwtEncoderParameters.from(header, claims))
                .getTokenValue();
    }
}

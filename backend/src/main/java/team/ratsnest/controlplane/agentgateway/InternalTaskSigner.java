package team.ratsnest.controlplane.agentgateway;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Instant;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import tools.jackson.databind.ObjectMapper;

@Component
public final class InternalTaskSigner {

    private static final Base64.Encoder BASE64 = Base64.getUrlEncoder().withoutPadding();
    private static final String HEADER = BASE64.encodeToString(
            "{\"alg\":\"HS256\",\"typ\":\"JWT\"}".getBytes(StandardCharsets.UTF_8));

    private final byte[] secret;
    private final ObjectMapper objectMapper;
    private final Clock clock;

    @Autowired
    public InternalTaskSigner(
            @Value("${ratsnest.agent-runtime.signing-secret:}") String secret,
            ObjectMapper objectMapper) {
        this(secret, objectMapper, Clock.systemUTC());
    }

    InternalTaskSigner(String secret, ObjectMapper objectMapper, Clock clock) {
        if (secret == null || secret.getBytes(StandardCharsets.UTF_8).length < 32) {
            throw new IllegalStateException("Agent Runtime signing secret must contain at least 32 bytes");
        }
        this.secret = secret.getBytes(StandardCharsets.UTF_8);
        this.objectMapper = objectMapper;
        this.clock = clock;
    }

    public String principalId(
            UUID tenantId,
            UUID projectId,
            AuthenticatedActor actor) {
        String input = String.join(
                "\0",
                "principal-v1",
                tenantId.toString(),
                projectId.toString(),
                actor.issuer(),
                actor.subject());
        return BASE64.encodeToString(hmac(input.getBytes(StandardCharsets.UTF_8)));
    }

    public String token(
            String method,
            String path,
            byte[] body,
            AgentRuntimeGateway.RuntimeIdentity identity,
            String runId) {
        Instant now = clock.instant();
        Map<String, Object> claims = new LinkedHashMap<>();
        claims.put("v", 1);
        claims.put("iss", "ratsnest-control-plane");
        claims.put("aud", "ratsnest-agent-runtime");
        claims.put("sub", identity.principalId());
        claims.put("tenantId", identity.tenantId());
        claims.put("projectId", identity.projectId());
        claims.put("runId", runId);
        claims.put("method", method);
        claims.put("path", path);
        claims.put("bodySha256", sha256Hex(body));
        claims.put("iat", now.getEpochSecond());
        claims.put("exp", now.plusSeconds(90).getEpochSecond());
        claims.put("jti", UUID.randomUUID().toString());
        try {
            String payload = BASE64.encodeToString(objectMapper.writeValueAsBytes(claims));
            String signed = HEADER + "." + payload;
            return signed + "." + BASE64.encodeToString(hmac(signed.getBytes(StandardCharsets.US_ASCII)));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to sign Agent Runtime identity", exception);
        }
    }

    private byte[] hmac(byte[] value) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            return mac.doFinal(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to calculate Agent Runtime signature", exception);
        }
    }

    private String sha256Hex(byte[] body) {
        try {
            return java.util.HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(body));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to hash Agent Runtime request", exception);
        }
    }
}

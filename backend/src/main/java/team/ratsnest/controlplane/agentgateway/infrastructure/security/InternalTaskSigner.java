package team.ratsnest.controlplane.agentgateway.infrastructure.security;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Instant;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.domain.port.RuntimeCredentials;
import team.ratsnest.controlplane.agentgateway.domain.port.RuntimeCredentials.RuntimeClaims;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import tools.jackson.databind.ObjectMapper;

@Component
public final class InternalTaskSigner implements RuntimeCredentials {

    private static final Base64.Encoder BASE64 = Base64.getUrlEncoder().withoutPadding();
    private static final Base64.Decoder BASE64_DECODER = Base64.getUrlDecoder();
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
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

    public InternalTaskSigner(String secret, ObjectMapper objectMapper, Clock clock) {
        if (secret == null || secret.getBytes(StandardCharsets.UTF_8).length < 32) {
            throw new IllegalStateException("Agent Runtime signing secret must contain at least 32 bytes");
        }
        this.secret = secret.getBytes(StandardCharsets.UTF_8);
        this.objectMapper = objectMapper;
        this.clock = clock;
    }

    @Override
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

    @Override
    public String token(
            String method,
            String path,
            byte[] body,
            RuntimeIdentity identity,
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

    @Override
    public RuntimeClaims verifyRuntimeToken(
            String token,
            String method,
            String path,
            byte[] body,
            String expectedRunId) {
        try {
            String[] segments = token == null ? new String[0] : token.split("\\.", -1);
            if (segments.length != 3 || !MessageDigest.isEqual(
                    HEADER.getBytes(StandardCharsets.US_ASCII),
                    segments[0].getBytes(StandardCharsets.US_ASCII))) {
                throw invalidToken();
            }
            byte[] supplied = BASE64_DECODER.decode(segments[2]);
            byte[] expected = hmac((segments[0] + "." + segments[1])
                    .getBytes(StandardCharsets.US_ASCII));
            if (!MessageDigest.isEqual(supplied, expected)) {
                throw invalidToken();
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> claims = objectMapper.readValue(
                    BASE64_DECODER.decode(segments[1]), Map.class);
            long issuedAt = number(claims, "iat");
            long expiresAt = number(claims, "exp");
            long now = clock.instant().getEpochSecond();
            if (number(claims, "v") != 1
                    || issuedAt > now + 15 || expiresAt < now - 15
                    || expiresAt <= issuedAt || expiresAt - issuedAt > 120) {
                throw invalidToken();
            }
            String bodyDigest = text(claims, "bodySha256");
            if (!constantTime(text(claims, "iss"), "ratsnest-agent-runtime")
                    || !constantTime(text(claims, "aud"), "ratsnest-control-plane")
                    || !constantTime(text(claims, "method"), method.toUpperCase(java.util.Locale.ROOT))
                    || !constantTime(text(claims, "path"), path)
                    || !constantTime(text(claims, "runId"), expectedRunId)
                    || !SHA256.matcher(bodyDigest).matches()
                    || !constantTime(bodyDigest, sha256Hex(body))) {
                throw invalidToken();
            }
            return new RuntimeClaims(
                    text(claims, "sub"),
                    text(claims, "tenantId"),
                    text(claims, "projectId"),
                    text(claims, "runId"));
        } catch (IllegalArgumentException exception) {
            throw exception;
        } catch (Exception exception) {
            throw invalidToken();
        }
    }

    @Override
    public boolean verifyEvolutionResultAttestation(
            byte[] canonicalPayload,
            String payloadSha256,
            String signature) {
        if (payloadSha256 == null || signature == null
                || !SHA256.matcher(payloadSha256).matches()
                || !SHA256.matcher(signature).matches()
                || !constantTime(payloadSha256, sha256Hex(canonicalPayload))) {
            return false;
        }
        byte[] domain = "ratsnest-evolution-result-v1\0".getBytes(StandardCharsets.US_ASCII);
        byte[] signed = new byte[domain.length + canonicalPayload.length];
        System.arraycopy(domain, 0, signed, 0, domain.length);
        System.arraycopy(canonicalPayload, 0, signed, domain.length, canonicalPayload.length);
        return MessageDigest.isEqual(
                java.util.HexFormat.of().parseHex(signature),
                hmac(signed));
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

    private String text(Map<String, Object> claims, String name) {
        Object value = claims.get(name);
        if (!(value instanceof String text) || text.isBlank() || text.length() > 500) {
            throw invalidToken();
        }
        return text;
    }

    private long number(Map<String, Object> claims, String name) {
        Object value = claims.get(name);
        if (!(value instanceof Number number)) {
            throw invalidToken();
        }
        return number.longValue();
    }

    private boolean constantTime(String left, String right) {
        return MessageDigest.isEqual(
                left.getBytes(StandardCharsets.UTF_8),
                right.getBytes(StandardCharsets.UTF_8));
    }

    private IllegalArgumentException invalidToken() {
        return new IllegalArgumentException("Invalid Agent Runtime credential");
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

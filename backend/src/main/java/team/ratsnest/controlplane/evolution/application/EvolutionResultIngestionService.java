package team.ratsnest.controlplane.evolution.application;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import team.ratsnest.controlplane.agentgateway.domain.port.RuntimeCredentials;
import team.ratsnest.controlplane.agentgateway.domain.port.RuntimeCredentials.RuntimeClaims;
import team.ratsnest.controlplane.evolution.application.EvolutionTrialService.ResultProof;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.shared.web.ApiException;
import tools.jackson.databind.ObjectMapper;

@Service
public final class EvolutionResultIngestionService {

    private static final String SYSTEM_PROJECT_ID = "00000000-0000-0000-0000-000000000000";
    private static final int MAX_RESULT_BYTES = 1024 * 1024;
    private static final Set<String> RESULT_FIELDS = Set.of(
            "trial_id", "candidate_id", "candidate_digest",
            "base_harness_version_id", "base_manifest_digest", "input_digest",
            "temporal_workflow_id", "optimization_suite_digest",
            "holdout_suite_digest", "adversarial_suite_digest", "eval_suite_digest",
            "patch_digest", "report_digest", "verdict", "guardrail_passed",
            "authoritative_report", "completed_at", "attestation");
    private static final Set<String> ATTESTATION_FIELDS = Set.of(
            "algorithm", "key_id", "payload_sha256", "signature");

    private final RuntimeCredentials signer;
    private final EvolutionTrialService evolution;
    private final ObjectMapper objectMapper;

    public EvolutionResultIngestionService(
            RuntimeCredentials signer,
            EvolutionTrialService evolution,
            ObjectMapper objectMapper) {
        this.signer = signer;
        this.evolution = evolution;
        this.objectMapper = objectMapper;
    }

    public EvolutionTrial ingest(UUID trialId, String authorization, byte[] body) {
        if (body.length == 0 || body.length > MAX_RESULT_BYTES) {
            throw invalidProof();
        }
        String path = "/internal/v1/evolution/trials/" + trialId + "/result";
        RuntimeClaims claims;
        try {
            claims = signer.verifyRuntimeToken(
                    bearer(authorization), "POST", path, body, trialId.toString());
        } catch (IllegalArgumentException exception) {
            throw unauthorized();
        }
        if (!"evolution-worker".equals(claims.subject())
                || !SYSTEM_PROJECT_ID.equals(claims.projectId())) {
            throw unauthorized();
        }
        UUID tenantId;
        Map<String, Object> payload;
        try {
            tenantId = UUID.fromString(claims.tenantId());
            payload = jsonObject(body);
        } catch (IllegalArgumentException exception) {
            throw invalidProof();
        }
        if (!payload.keySet().equals(RESULT_FIELDS)) {
            throw invalidProof();
        }
        Map<String, Object> attestation = object(payload, "attestation");
        if (!attestation.keySet().equals(ATTESTATION_FIELDS)
                || !"HMAC-SHA256".equals(text(attestation, "algorithm", 32))
                || !"ratsnest-internal-hs256-v1".equals(text(attestation, "key_id", 80))) {
            throw invalidProof();
        }
        Map<String, Object> proofPayload = new LinkedHashMap<>(payload);
        proofPayload.remove("attestation");
        if (!signer.verifyEvolutionResultAttestation(
                evolution.canonicalBytes(proofPayload),
                text(attestation, "payload_sha256", 64),
                text(attestation, "signature", 64))) {
            throw invalidProof();
        }
        ResultProof proof;
        try {
            proof = new ResultProof(
                    UUID.fromString(text(payload, "trial_id", 36)),
                    text(payload, "candidate_id", 64),
                    text(payload, "candidate_digest", 64),
                    text(payload, "base_harness_version_id", 120),
                    text(payload, "base_manifest_digest", 64),
                    text(payload, "input_digest", 64),
                    text(payload, "temporal_workflow_id", 255),
                    text(payload, "optimization_suite_digest", 64),
                    text(payload, "holdout_suite_digest", 64),
                    text(payload, "adversarial_suite_digest", 64),
                    text(payload, "eval_suite_digest", 64),
                    text(payload, "patch_digest", 64),
                    text(payload, "report_digest", 64),
                    text(payload, "verdict", 32),
                    bool(payload, "guardrail_passed"),
                    object(payload, "authoritative_report"),
                    Instant.parse(text(payload, "completed_at", 80)));
        } catch (IllegalArgumentException exception) {
            throw invalidProof();
        }
        if (!trialId.equals(proof.trialId())) {
            throw invalidProof();
        }
        return evolution.completeTrial(tenantId, trialId, proof);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonObject(byte[] value) {
        try {
            Object decoded = objectMapper.readValue(value, Map.class);
            if (!(decoded instanceof Map<?, ?>)) {
                throw invalidProof();
            }
            return (Map<String, Object>) decoded;
        } catch (ApiException exception) {
            throw exception;
        } catch (Exception exception) {
            throw invalidProof();
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> object(Map<String, Object> value, String name) {
        Object item = value.get(name);
        if (!(item instanceof Map<?, ?>)) {
            throw invalidProof();
        }
        return (Map<String, Object>) item;
    }

    private String text(Map<String, Object> value, String name, int maxLength) {
        Object item = value.get(name);
        if (!(item instanceof String text) || text.isBlank() || text.length() > maxLength) {
            throw invalidProof();
        }
        return text;
    }

    private boolean bool(Map<String, Object> value, String name) {
        Object item = value.get(name);
        if (!(item instanceof Boolean result)) {
            throw invalidProof();
        }
        return result;
    }

    private String bearer(String authorization) {
        if (authorization == null || authorization.length() < 8
                || !authorization.regionMatches(true, 0, "Bearer ", 0, 7)) {
            throw unauthorized();
        }
        return authorization.substring(7);
    }

    private ApiException unauthorized() {
        return new ApiException(
                "INTERNAL_AUTHENTICATION_REQUIRED",
                HttpStatus.UNAUTHORIZED,
                "A valid Agent Runtime credential is required.");
    }

    private ApiException invalidProof() {
        return new ApiException(
                "EVOLUTION_RESULT_PROOF_INVALID",
                HttpStatus.UNPROCESSABLE_ENTITY,
                "The Agent Runtime result proof is invalid.");
    }
}

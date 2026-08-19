package team.ratsnest.controlplane.evolution;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.InternalTaskSigner;
import team.ratsnest.controlplane.shared.web.ApiException;
import tools.jackson.databind.ObjectMapper;

@Component
final class EvolutionRuntimeGateway {

    private static final String SYSTEM_PROJECT_ID = "00000000-0000-0000-0000-000000000000";
    private static final Duration TIMEOUT = Duration.ofSeconds(20);

    private final URI baseUri;
    private final InternalTaskSigner signer;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;

    EvolutionRuntimeGateway(
            @Value("${ratsnest.agent-runtime.base-url:}") String baseUrl,
            InternalTaskSigner signer,
            ObjectMapper objectMapper) {
        if (baseUrl == null || baseUrl.isBlank()) {
            throw new IllegalStateException("Agent Runtime base URL must be configured");
        }
        this.baseUri = URI.create(baseUrl.endsWith("/") ? baseUrl : baseUrl + "/");
        this.signer = signer;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(10))
                .build();
    }

    StartResult start(UUID tenantId, EvolutionTrial trial, Map<String, Object> trialInput) {
        String trialId = trial.trialId().toString();
        String path = "/internal/v1/evolution/trials/" + trialId + ":start";
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("trial_id", trialId);
        payload.put("candidate_id", trial.candidateId());
        payload.put("base_harness_version_id", baseHarnessVersionId(trialInput));
        payload.put("base_manifest_digest", trial.baseManifestDigest());
        payload.put("input_digest", trial.inputDigest());
        payload.put("optimization_suite_digest", trial.optimizationSuiteDigest());
        payload.put("holdout_suite_digest", trial.holdoutSuiteDigest());
        payload.put("adversarial_suite_digest", trial.adversarialSuiteDigest());
        payload.put("trial_input", trialInput);
        payload.put("callback_path", "/internal/v1/evolution/trials/" + trialId + "/result");
        byte[] body = json(payload);
        RuntimeIdentity identity = new RuntimeIdentity(
                "evolution-control-plane", tenantId.toString(), SYSTEM_PROJECT_ID);
        HttpRequest request = HttpRequest.newBuilder(baseUri.resolve(path))
                .timeout(TIMEOUT)
                .header("Accept", "application/json")
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + signer.token(
                        "POST", path, body, identity, trialId))
                .header("X-Request-ID", trialId)
                .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                .build();
        try {
            HttpResponse<byte[]> response = httpClient.send(
                    request, HttpResponse.BodyHandlers.ofByteArray());
            if (response.statusCode() < 200 || response.statusCode() >= 300) {
                throw unavailable("Agent Runtime rejected the evolution trial");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> value = objectMapper.readValue(response.body(), Map.class);
            UUID returnedTrialId = UUID.fromString(requiredText(value, "trial_id"));
            if (!trial.trialId().equals(returnedTrialId)) {
                throw unavailable("Agent Runtime returned a mismatched evolution trial");
            }
            return new StartResult(
                    returnedTrialId,
                    requiredText(value, "workflow_id"),
                    requiredText(value, "status"));
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw unavailable("Agent Runtime evolution request was interrupted");
        } catch (ApiException exception) {
            throw exception;
        } catch (Exception exception) {
            throw unavailable("Agent Runtime evolution endpoint is unavailable");
        }
    }

    private byte[] json(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsBytes(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to encode evolution trial", exception);
        }
    }

    private String baseHarnessVersionId(Map<String, Object> trialInput) {
        Object value = trialInput.get("candidate");
        if (!(value instanceof Map<?, ?> candidate)
                || !(candidate.get("baseHarnessVersionId") instanceof String versionId)
                || versionId.isBlank()) {
            throw new IllegalStateException("Evolution trial is missing its base harness version");
        }
        return versionId;
    }

    private String requiredText(Map<String, Object> value, String name) {
        Object item = value.get(name);
        if (!(item instanceof String text) || text.isBlank() || text.length() > 255) {
            throw unavailable("Agent Runtime returned an invalid evolution response");
        }
        return text;
    }

    private ApiException unavailable(String detail) {
        return new ApiException("EVOLUTION_RUNTIME_UNAVAILABLE", HttpStatus.SERVICE_UNAVAILABLE, detail);
    }

    record StartResult(UUID trialId, String workflowId, String status) {
    }
}

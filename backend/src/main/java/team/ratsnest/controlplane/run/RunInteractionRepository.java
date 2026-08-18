package team.ratsnest.controlplane.run;

import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import tools.jackson.databind.ObjectMapper;

@Repository
class RunInteractionRepository {

    private final JdbcClient jdbcClient;
    private final ObjectMapper objectMapper;

    RunInteractionRepository(JdbcClient jdbcClient, ObjectMapper objectMapper) {
        this.jdbcClient = jdbcClient;
        this.objectMapper = objectMapper;
    }

    boolean register(
            Run run,
            String interactionId,
            long stateVersion,
            Map<String, Object> request) {
        int inserted = jdbcClient.sql("""
                        insert into control_plane.run_interactions (
                            tenant_id, interaction_id, run_id, kind,
                            interaction_version, request_payload
                        ) values (
                            :tenantId, :interactionId, :runId, 'clarification',
                            :interactionVersion, cast(:requestPayload as jsonb)
                        )
                        on conflict (tenant_id, interaction_id) do nothing
                        """)
                .param("tenantId", run.tenantId())
                .param("interactionId", interactionId)
                .param("runId", run.runId())
                .param("interactionVersion", stateVersion)
                .param("requestPayload", json(request))
                .update();
        if (inserted == 1) {
            return true;
        }
        RunInteraction existing = findForUpdate(
                        run.tenantId(), run.runId(), interactionId)
                .orElseThrow(() -> new IllegalStateException(
                        "Interaction identifier belongs to another run"));
        if (existing.stateVersion() != stateVersion
                || !existing.request().equals(request)) {
            throw new IllegalStateException(
                    "Agent Runtime changed an immutable interaction request");
        }
        return false;
    }

    Optional<RunInteraction> findForUpdate(UUID tenantId, UUID runId, String interactionId) {
        return jdbcClient.sql("""
                        select tenant_id, interaction_id, run_id, kind,
                               interaction_version, request_payload, status,
                               response_idempotency_key, response_fingerprint,
                               response_request_id, answer
                        from control_plane.run_interactions
                        where tenant_id = :tenantId
                          and run_id = :runId
                          and interaction_id = :interactionId
                        for update
                        """)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .param("interactionId", interactionId)
                .query((resultSet, rowNumber) -> new RunInteraction(
                        resultSet.getObject("tenant_id", UUID.class),
                        resultSet.getString("interaction_id"),
                        resultSet.getObject("run_id", UUID.class),
                        resultSet.getString("kind"),
                        resultSet.getLong("interaction_version"),
                        jsonObject(resultSet.getString("request_payload")),
                        RunInteraction.Status.valueOf(resultSet.getString("status")),
                        resultSet.getString("response_idempotency_key"),
                        resultSet.getString("response_fingerprint"),
                        resultSet.getObject("response_request_id", UUID.class),
                        resultSet.getString("answer")))
                .optional();
    }

    boolean beginResponse(
            RunInteraction interaction,
            String idempotencyKey,
            String fingerprint,
            UUID responseRequestId,
            String answer,
            AuthenticatedActor actor) {
        return jdbcClient.sql("""
                        update control_plane.run_interactions
                        set status = 'RESPONDING',
                            response_idempotency_key = :idempotencyKey,
                            response_fingerprint = :fingerprint,
                            response_request_id = :responseRequestId,
                            answer = :answer,
                            responded_by_issuer = :issuer,
                            responded_by_subject = :subject
                        where tenant_id = :tenantId
                          and run_id = :runId
                          and interaction_id = :interactionId
                          and status = 'PENDING'
                          and interaction_version = :interactionVersion
                        """)
                .param("tenantId", interaction.tenantId())
                .param("runId", interaction.runId())
                .param("interactionId", interaction.interactionId())
                .param("interactionVersion", interaction.stateVersion())
                .param("idempotencyKey", idempotencyKey)
                .param("fingerprint", fingerprint)
                .param("responseRequestId", responseRequestId)
                .param("answer", answer)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .update() == 1;
    }

    void markResponded(RunInteraction interaction) {
        jdbcClient.sql("""
                        update control_plane.run_interactions
                        set status = 'RESPONDED', responded_at = now()
                        where tenant_id = :tenantId
                          and run_id = :runId
                          and interaction_id = :interactionId
                          and status = 'RESPONDING'
                          and response_request_id = :responseRequestId
                        """)
                .param("tenantId", interaction.tenantId())
                .param("runId", interaction.runId())
                .param("interactionId", interaction.interactionId())
                .param("responseRequestId", interaction.responseRequestId())
                .update();
    }

    private String json(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to serialize interaction request", exception);
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonObject(String value) {
        try {
            return (Map<String, Object>) objectMapper.readValue(value, Map.class);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to read interaction request", exception);
        }
    }
}

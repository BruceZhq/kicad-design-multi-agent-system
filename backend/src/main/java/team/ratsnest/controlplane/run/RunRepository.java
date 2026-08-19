package team.ratsnest.controlplane.run;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.identity.AuthenticatedActor;
import tools.jackson.databind.ObjectMapper;

@Repository
class RunRepository {

    private static final String COLUMNS = """
            tenant_id, run_id, project_id, root_run_id, parent_run_id, revision_number,
            thread_id, idempotency_key,
            request_fingerprint, message, model, runtime_config,
            profile_id, profile_version, profile_digest, runtime_principal_id,
            harness_version_id, harness_manifest_digest, harness_channel,
            created_by_issuer, created_by_subject, state, delivery_status,
            runtime_run_id, event_count, oldest_event_id, newest_event_id,
            error_code, error, created_at, started_at, finished_at
            """;

    private final JdbcClient jdbcClient;
    private final ObjectMapper objectMapper;

    RunRepository(JdbcClient jdbcClient, ObjectMapper objectMapper) {
        this.jdbcClient = jdbcClient;
        this.objectMapper = objectMapper;
    }

    void insert(Run run, AuthenticatedActor actor) {
        jdbcClient.sql("""
                        insert into control_plane.runs (
                            tenant_id, run_id, project_id, root_run_id, parent_run_id, revision_number,
                            thread_id,
                            idempotency_key, request_fingerprint, message, model,
                            runtime_config, profile_id, profile_version, profile_digest,
                            harness_version_id, harness_manifest_digest, harness_channel,
                            runtime_principal_id, state,
                            created_by_issuer, created_by_subject
                        ) values (
                            :tenantId, :runId, :projectId, :rootRunId, :parentRunId, :revisionNumber,
                            :threadId,
                            :idempotencyKey, :fingerprint, :message, :model,
                            cast(:runtimeConfig as jsonb), :profileId, :profileVersion, :profileDigest,
                            :harnessVersionId, :harnessManifestDigest, :harnessChannel,
                            :runtimePrincipalId, :state,
                            :issuer, :subject
                        )
                        """)
                .param("tenantId", run.tenantId())
                .param("runId", run.runId())
                .param("projectId", run.projectId())
                .param("rootRunId", run.rootRunId())
                .param("parentRunId", run.parentRunId())
                .param("revisionNumber", run.revisionNumber())
                .param("threadId", run.threadId())
                .param("idempotencyKey", run.idempotencyKey())
                .param("fingerprint", run.requestFingerprint())
                .param("message", run.message())
                .param("model", run.model())
                .param("runtimeConfig", json(run.runtimeConfig()))
                .param("profileId", run.profileId())
                .param("profileVersion", run.profileVersion())
                .param("profileDigest", run.profileDigest())
                .param("harnessVersionId", run.harnessVersionId())
                .param("harnessManifestDigest", run.harnessManifestDigest())
                .param("harnessChannel", run.harnessChannel())
                .param("runtimePrincipalId", run.runtimePrincipalId())
                .param("state", run.state().name())
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .update();
    }

    Optional<Run> find(UUID tenantId, UUID runId) {
        return jdbcClient.sql("select " + COLUMNS + " from control_plane.runs "
                        + "where tenant_id = :tenantId and run_id = :runId")
                .param("tenantId", tenantId)
                .param("runId", runId)
                .query(this::map)
                .optional();
    }

    Optional<Run> findForUpdate(UUID tenantId, UUID runId) {
        return jdbcClient.sql("select " + COLUMNS + " from control_plane.runs "
                        + "where tenant_id = :tenantId and run_id = :runId for update")
                .param("tenantId", tenantId)
                .param("runId", runId)
                .query(this::map)
                .optional();
    }

    Optional<Run> findByIdempotency(UUID tenantId, UUID projectId, String key) {
        return jdbcClient.sql("select " + COLUMNS + " from control_plane.runs "
                        + "where tenant_id = :tenantId and project_id = :projectId "
                        + "and idempotency_key = :idempotencyKey")
                .param("tenantId", tenantId)
                .param("projectId", projectId)
                .param("idempotencyKey", key)
                .query(this::map)
                .optional();
    }

    int nextRevisionNumber(UUID tenantId, UUID rootRunId) {
        return jdbcClient.sql("""
                        select coalesce(max(revision_number), 0) + 1
                        from control_plane.runs
                        where tenant_id = :tenantId and root_run_id = :rootRunId
                        """)
                .param("tenantId", tenantId)
                .param("rootRunId", rootRunId)
                .query(Integer.class)
                .single();
    }

    Optional<Run> findLatestRevision(UUID tenantId, UUID rootRunId) {
        return jdbcClient.sql("select " + COLUMNS + " from control_plane.runs "
                        + "where tenant_id = :tenantId and root_run_id = :rootRunId "
                        + "order by revision_number desc limit 1")
                .param("tenantId", tenantId)
                .param("rootRunId", rootRunId)
                .query(this::map)
                .optional();
    }

    List<ConversationSummary> listConversations(UUID tenantId, UUID projectId, int limit) {
        return jdbcClient.sql("""
                        with ranked as (
                            select r.*,
                                   row_number() over (
                                       partition by r.thread_id
                                       order by r.created_at desc, r.revision_number desc, r.run_id desc
                                   ) as latest_rank,
                                   row_number() over (
                                       partition by r.thread_id
                                       order by r.created_at asc, r.revision_number asc, r.run_id asc
                                   ) as first_rank
                            from control_plane.runs r
                            where r.tenant_id = :tenantId and r.project_id = :projectId
                        ),
                        first_messages as (
                            select thread_id, message, created_at
                            from ranked
                            where first_rank = 1
                        )
                        select latest.thread_id,
                               first_messages.message as first_message,
                               latest.run_id as latest_run_id,
                               latest.revision_number,
                               latest.state,
                               latest.delivery_status,
                               coalesce(latest.newest_event_id, 0) as last_event_id,
                               pending.request_payload as pending_interaction,
                               first_messages.created_at,
                               coalesce(latest.finished_at, latest.started_at, latest.created_at) as updated_at
                        from ranked latest
                        join first_messages using (thread_id)
                        left join lateral (
                            select interaction.request_payload
                            from control_plane.run_interactions interaction
                            where interaction.tenant_id = latest.tenant_id
                              and interaction.run_id = latest.run_id
                              and interaction.status = 'PENDING'
                            order by interaction.created_at desc
                            limit 1
                        ) pending on true
                        where latest.latest_rank = 1
                        order by updated_at desc, latest.run_id desc
                        limit :limit
                        """)
                .param("tenantId", tenantId)
                .param("projectId", projectId)
                .param("limit", limit)
                .query((resultSet, rowNumber) -> ConversationSummary.fromStoredMessage(
                        resultSet.getString("thread_id"),
                        resultSet.getString("first_message"),
                        resultSet.getObject("latest_run_id", UUID.class),
                        resultSet.getInt("revision_number"),
                        RunState.valueOf(resultSet.getString("state")),
                        DeliveryStatus.fromApiValue(resultSet.getString("delivery_status")),
                        resultSet.getLong("last_event_id"),
                        resultSet.getString("pending_interaction") == null
                                ? Map.of()
                                : jsonObject(resultSet.getString("pending_interaction")),
                        resultSet.getObject("created_at", OffsetDateTime.class).toInstant(),
                        resultSet.getObject("updated_at", OffsetDateTime.class).toInstant()))
                .list();
    }

    void setDeliveryStatus(UUID tenantId, UUID runId, DeliveryStatus status) {
        int updated = jdbcClient.sql("""
                        update control_plane.runs
                        set delivery_status = :deliveryStatus
                        where tenant_id = :tenantId and run_id = :runId
                          and delivery_status is null
                        """)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .param("deliveryStatus", status.apiValue())
                .update();
        if (updated == 0) {
            DeliveryStatus existing = find(tenantId, runId)
                    .map(Run::deliveryStatus)
                    .orElseThrow(() -> new IllegalStateException("Run disappeared while saving delivery status"));
            if (existing != status) {
                throw new IllegalStateException("Agent Runtime changed an immutable delivery status");
            }
        }
    }

    boolean updateFromRuntime(UUID tenantId, UUID runId, RuntimeRun runtime) {
        return jdbcClient.sql("""
                        update control_plane.runs
                        set state = :state,
                            runtime_run_id = coalesce(:runtimeRunId, runtime_run_id),
                            event_count = greatest(event_count, :eventCount),
                            oldest_event_id = case
                                when cast(:oldestEventId as bigint) is null then oldest_event_id
                                when oldest_event_id is null then cast(:oldestEventId as bigint)
                                else least(oldest_event_id, cast(:oldestEventId as bigint))
                            end,
                            newest_event_id = case
                                when cast(:newestEventId as bigint) is null then newest_event_id
                                when newest_event_id is null then cast(:newestEventId as bigint)
                                else greatest(newest_event_id, cast(:newestEventId as bigint))
                            end,
                            error_code = :errorCode,
                            error = :error,
                            started_at = coalesce(started_at, :startedAt),
                            finished_at = coalesce(:finishedAt, finished_at)
                        where tenant_id = :tenantId and run_id = :runId
                          and state not in ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT')
                          and (
                              (state = 'QUEUED' and :state in (
                                  'QUEUED', 'RUNNING', 'WAITING_FOR_INPUT', 'COMPLETED', 'FAILED',
                                  'CANCELLED', 'TIMED_OUT'))
                              or
                              (state = 'RUNNING' and :state in (
                                  'RUNNING', 'WAITING_FOR_INPUT', 'COMPLETED', 'FAILED',
                                  'CANCELLED', 'TIMED_OUT'))
                              or
                              (state = 'WAITING_FOR_INPUT' and :state in (
                                  'WAITING_FOR_INPUT', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED',
                                  'CANCELLED', 'TIMED_OUT'))
                          )
                        """)
                .param("state", runtime.state().name())
                .param("runtimeRunId", runtime.runId())
                .param("eventCount", runtime.eventCount())
                .param("oldestEventId", runtime.oldestEventId())
                .param("newestEventId", runtime.newestEventId())
                .param("errorCode", runtime.errorCode())
                .param("error", runtime.error())
                .param("startedAt", offset(runtime.startedAt()))
                .param("finishedAt", offset(runtime.finishedAt()))
                .param("tenantId", tenantId)
                .param("runId", runId)
                .update() == 1;
    }

    boolean markWaitingForInput(UUID tenantId, UUID runId) {
        return jdbcClient.sql("""
                        update control_plane.runs
                        set state = 'WAITING_FOR_INPUT'
                        where tenant_id = :tenantId and run_id = :runId
                          and state in ('QUEUED', 'RUNNING')
                        """)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .update() == 1;
    }

    boolean markFailed(UUID tenantId, UUID runId, String code, String error) {
        return jdbcClient.sql("""
                        update control_plane.runs
                        set state = 'FAILED', error_code = :code, error = :error,
                            finished_at = now()
                        where tenant_id = :tenantId and run_id = :runId
                          and state in ('QUEUED', 'RUNNING', 'WAITING_FOR_INPUT')
                        """)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .param("code", code)
                .param("error", error)
                .update() == 1;
    }

    List<ReconciliationClaim> claimForReconciliation(String workerId, int batchSize) {
        return jdbcClient.sql(
                        "select * from control_plane.claim_runs_for_reconciliation(:workerId, :batchSize)")
                .param("workerId", workerId)
                .param("batchSize", batchSize)
                .query((resultSet, rowNumber) -> new ReconciliationClaim(
                        resultSet.getObject("tenant_id", UUID.class),
                        resultSet.getObject("run_id", UUID.class),
                        resultSet.getInt("reconcile_attempts")))
                .list();
    }

    boolean releaseReconciliation(
            ReconciliationClaim claim,
            String workerId,
            int delaySeconds) {
        return Boolean.TRUE.equals(jdbcClient.sql("""
                        select control_plane.release_run_reconciliation(
                            :tenantId, :runId, :workerId, :delaySeconds)
                        """)
                .param("tenantId", claim.tenantId())
                .param("runId", claim.runId())
                .param("workerId", workerId)
                .param("delaySeconds", delaySeconds)
                .query(Boolean.class)
                .single());
    }

    private Run map(java.sql.ResultSet resultSet, int rowNumber) throws java.sql.SQLException {
        return new Run(
                resultSet.getObject("tenant_id", UUID.class),
                resultSet.getObject("run_id", UUID.class),
                resultSet.getObject("project_id", UUID.class),
                resultSet.getObject("root_run_id", UUID.class),
                resultSet.getObject("parent_run_id", UUID.class),
                resultSet.getInt("revision_number"),
                resultSet.getString("thread_id"),
                resultSet.getString("idempotency_key"),
                resultSet.getString("request_fingerprint"),
                resultSet.getString("message"),
                resultSet.getString("model"),
                jsonObject(resultSet.getString("runtime_config")),
                resultSet.getString("profile_id"),
                resultSet.getString("profile_version"),
                resultSet.getString("profile_digest"),
                resultSet.getString("harness_version_id"),
                resultSet.getString("harness_manifest_digest"),
                resultSet.getString("harness_channel"),
                resultSet.getString("runtime_principal_id"),
                resultSet.getString("created_by_issuer"),
                resultSet.getString("created_by_subject"),
                RunState.valueOf(resultSet.getString("state")),
                DeliveryStatus.fromApiValue(resultSet.getString("delivery_status")),
                resultSet.getString("runtime_run_id"),
                resultSet.getLong("event_count"),
                nullableLong(resultSet, "oldest_event_id"),
                nullableLong(resultSet, "newest_event_id"),
                resultSet.getString("error_code"),
                resultSet.getString("error"),
                instant(resultSet, "created_at"),
                nullableInstant(resultSet, "started_at"),
                nullableInstant(resultSet, "finished_at"));
    }

    private String json(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to serialize run configuration", exception);
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonObject(String value) {
        try {
            return (Map<String, Object>) objectMapper.readValue(value, Map.class);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to read run configuration", exception);
        }
    }

    private static Long nullableLong(java.sql.ResultSet resultSet, String column)
            throws java.sql.SQLException {
        long value = resultSet.getLong(column);
        return resultSet.wasNull() ? null : value;
    }

    private static Instant instant(java.sql.ResultSet resultSet, String column)
            throws java.sql.SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }

    private static Instant nullableInstant(java.sql.ResultSet resultSet, String column)
            throws java.sql.SQLException {
        OffsetDateTime value = resultSet.getObject(column, OffsetDateTime.class);
        return value == null ? null : value.toInstant();
    }

    private static OffsetDateTime offset(Instant value) {
        return value == null ? null : value.atOffset(java.time.ZoneOffset.UTC);
    }

    record ReconciliationClaim(UUID tenantId, UUID runId, int attempts) {
    }
}

package team.ratsnest.controlplane.evolution.infrastructure.persistence;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservation;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservationGovernance;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import tools.jackson.databind.ObjectMapper;

@Repository
public class JdbcEvolutionRepository implements EvolutionRepository {

    private static final String OBSERVATION_COLUMNS = """
            observation_id, run_id, source_event_seq, harness_version_id,
            harness_channel, harness_manifest_digest, profile_reference,
            profile_digest, scope_fingerprint, project_fingerprint,
            event_type, failure_signature, step, check_name, category,
            recoverability, strategy, required_capability, outcome, revision,
            evidence_digest, observed_at, recorded_at
            """;

    private static final String TRIAL_COLUMNS = """
            trial_id, candidate_id, attempt, input_digest, base_manifest_digest, candidate_digest,
            eval_suite_digest, temporal_workflow_id, patch_commit, patch_sha256,
            candidate_image_digest, optimization_suite_digest, holdout_suite_digest,
            adversarial_suite_digest, baseline_metrics, candidate_metrics,
            guardrail_results, verdict, report_digest, authoritative_report,
            report_object_key, llm_tokens, wall_clock_ms, row_version,
            created_at, updated_at, completed_at
            """;

    private final JdbcClient jdbcClient;
    private final ObjectMapper objectMapper;

    public JdbcEvolutionRepository(JdbcClient jdbcClient, ObjectMapper objectMapper) {
        this.jdbcClient = jdbcClient;
        this.objectMapper = objectMapper;
    }

    @Override
    public List<EvolutionObservation> findObservations(UUID tenantId) {
        return jdbcClient.sql("select " + OBSERVATION_COLUMNS + """
                        from control_plane.evolution_observations
                        where tenant_id = :tenantId
                        order by observed_at desc, observation_id
                        limit 200
                        """)
                .param("tenantId", tenantId)
                .query(this::mapObservation)
                .list();
    }

    @Override
    public boolean insertObservation(
            UUID tenantId,
            EvolutionObservation value,
            EvolutionObservationGovernance governance) {
        return jdbcClient.sql("""
                        insert into control_plane.evolution_observations (
                            tenant_id, observation_id, run_id, source_event_seq,
                            harness_version_id, harness_channel, harness_manifest_digest,
                            profile_reference, profile_digest, scope_fingerprint,
                            project_fingerprint, event_type, failure_signature, step,
                            check_name, category, recoverability, strategy,
                            required_capability, outcome, revision, evidence_digest,
                            failure_origin, attribution_action, attribution_reason_code,
                            attribution_origin, independent_project_count,
                            independent_run_count, observed_at
                        ) values (
                            :tenantId, :observationId, :runId, :sourceEventSeq,
                            :harnessVersionId, :harnessChannel, :harnessManifestDigest,
                            :profileReference, :profileDigest, :scopeFingerprint,
                            :projectFingerprint, :eventType, :failureSignature, :step,
                            :checkName, :category, :recoverability, :strategy,
                            :requiredCapability, :outcome, :revision, :evidenceDigest,
                            :failureOrigin, :attributionAction, :attributionReasonCode,
                            :attributionOrigin, :independentProjectCount,
                            :independentRunCount, :observedAt
                        )
                        on conflict do nothing
                        """)
                .param("tenantId", tenantId)
                .param("observationId", value.observationId())
                .param("runId", value.runId())
                .param("sourceEventSeq", value.sourceEventSeq())
                .param("harnessVersionId", value.harnessVersionId())
                .param("harnessChannel", value.harnessChannel())
                .param("harnessManifestDigest", value.harnessManifestDigest())
                .param("profileReference", value.profileReference())
                .param("profileDigest", value.profileDigest())
                .param("scopeFingerprint", value.scopeFingerprint())
                .param("projectFingerprint", value.projectFingerprint())
                .param("eventType", value.eventType())
                .param("failureSignature", value.failureSignature())
                .param("step", value.step())
                .param("checkName", value.checkName())
                .param("category", value.category())
                .param("recoverability", value.recoverability())
                .param("strategy", value.strategy())
                .param("requiredCapability", value.requiredCapability())
                .param("outcome", value.outcome())
                .param("revision", value.revision())
                .param("evidenceDigest", value.evidenceDigest())
                .param("failureOrigin", governance.failureOrigin())
                .param("attributionAction", governance.attributionAction())
                .param("attributionReasonCode", governance.attributionReasonCode())
                .param("attributionOrigin", governance.attributionOrigin())
                .param("independentProjectCount", governance.independentProjectCount())
                .param("independentRunCount", governance.independentRunCount())
                .param("observedAt", value.observedAt().atOffset(java.time.ZoneOffset.UTC))
                .update() == 1;
    }

    @Override
    public List<EvolutionObservation> findActiveGaps(
            UUID tenantId,
            String harnessVersionId,
            String harnessManifestDigest,
            String failureSignature) {
        return jdbcClient.sql("select " + OBSERVATION_COLUMNS + """
                        from control_plane.evolution_observations o
                        where o.tenant_id = :tenantId
                          and o.harness_version_id = :harnessVersionId
                          and o.harness_manifest_digest = :harnessManifestDigest
                          and o.failure_signature = :failureSignature
                          and (
                              (
                                  o.event_type = 'harness_defect_observed'
                                  and o.failure_origin = 'harness'
                                  and o.recoverability = 'harness_observation'
                                  and o.attribution_action = 'observe_harness'
                                  and o.attribution_reason_code =
                                      'harness_defect_not_yet_cross_run_reproducible'
                                  and o.attribution_origin = 'harness'
                              )
                              or (
                                  o.event_type = 'capability_gap'
                                  and o.failure_origin = 'harness'
                                  and o.recoverability = 'capability_gap'
                                  and o.attribution_action = 'capability_gap'
                                  and o.attribution_reason_code =
                                      'cross_run_reproducible_harness_defect'
                                  and o.attribution_origin = 'harness'
                                  and o.independent_project_count >= 2
                                  and o.independent_run_count >= 2
                              )
                          )
                          and not exists (
                              select 1
                              from control_plane.evolution_observations resolved
                              where resolved.tenant_id = o.tenant_id
                                and resolved.harness_version_id = o.harness_version_id
                                and resolved.harness_manifest_digest = o.harness_manifest_digest
                                and resolved.failure_signature = o.failure_signature
                                and resolved.project_fingerprint = o.project_fingerprint
                                and resolved.event_type = 'capability_gap_resolved'
                                and resolved.attribution_action = 'resolve_capability_gap'
                                and resolved.attribution_reason_code =
                                    'verified_harness_capability_gap_resolved'
                                and resolved.attribution_origin = 'harness'
                                and resolved.independent_project_count > 0
                                and resolved.independent_run_count > 0
                                and (
                                    resolved.recorded_at > o.recorded_at
                                    or (
                                        resolved.recorded_at = o.recorded_at
                                        and (
                                            resolved.run_id <> o.run_id
                                            or resolved.source_event_seq >= o.source_event_seq
                                        )
                                    )
                                )
                          )
                        order by o.observed_at, o.source_event_seq, o.observation_id
                        limit 10000
                        """)
                .param("tenantId", tenantId)
                .param("harnessVersionId", harnessVersionId)
                .param("harnessManifestDigest", harnessManifestDigest)
                .param("failureSignature", failureSignature)
                .query(this::mapObservation)
                .list();
    }

    @Override
    public void upsertAggregate(UUID tenantId, EvolutionCandidate candidate) {
        jdbcClient.sql("""
                        insert into control_plane.evolution_candidates (
                            tenant_id, candidate_id, base_harness_version_id,
                            base_manifest_digest, failure_signature, step, check_name,
                            category, required_capability, profile_references,
                            observation_ids, occurrence_count, project_count,
                            risk_tier, change_kind, status
                        ) values (
                            :tenantId, :candidateId, :baseHarnessVersionId,
                            :baseManifestDigest, :failureSignature, :step, :checkName,
                            :category, :requiredCapability, cast(:profileReferences as jsonb),
                            cast(:observationIds as jsonb), :occurrenceCount, :projectCount,
                            :riskTier, :changeKind, :status
                        )
                        on conflict (
                            tenant_id, base_harness_version_id,
                            base_manifest_digest, failure_signature
                        ) do update set
                            step = excluded.step,
                            check_name = excluded.check_name,
                            category = excluded.category,
                            required_capability = excluded.required_capability,
                            profile_references = excluded.profile_references,
                            observation_ids = excluded.observation_ids,
                            occurrence_count = excluded.occurrence_count,
                            project_count = excluded.project_count,
                            status = case
                                when evolution_candidates.status in ('observed', 'eligible')
                                    then excluded.status
                                else evolution_candidates.status
                            end,
                            row_version = evolution_candidates.row_version + 1,
                            updated_at = now()
                        """)
                .param("tenantId", tenantId)
                .param("candidateId", candidate.candidateId())
                .param("baseHarnessVersionId", candidate.baseHarnessVersionId())
                .param("baseManifestDigest", candidate.baseManifestDigest())
                .param("failureSignature", candidate.failureSignature())
                .param("step", candidate.step())
                .param("checkName", candidate.checkName())
                .param("category", candidate.category())
                .param("requiredCapability", candidate.requiredCapability())
                .param("profileReferences", json(candidate.profileReferences()))
                .param("observationIds", json(candidate.observationIds()))
                .param("occurrenceCount", candidate.occurrenceCount())
                .param("projectCount", candidate.projectCount())
                .param("riskTier", candidate.riskTier())
                .param("changeKind", candidate.changeKind())
                .param("status", candidate.status().wireValue())
                .update();
    }

    @Override
    public void markAggregateStale(
            UUID tenantId,
            String harnessVersionId,
            String harnessManifestDigest,
            String failureSignature) {
        jdbcClient.sql("""
                        update control_plane.evolution_candidates
                        set status = 'stale', row_version = row_version + 1,
                            updated_at = now()
                        where tenant_id = :tenantId
                          and base_harness_version_id = :harnessVersionId
                          and base_manifest_digest = :harnessManifestDigest
                          and failure_signature = :failureSignature
                          and status in ('observed', 'eligible')
                        """)
                .param("tenantId", tenantId)
                .param("harnessVersionId", harnessVersionId)
                .param("harnessManifestDigest", harnessManifestDigest)
                .param("failureSignature", failureSignature)
                .update();
    }

    @Override
    public List<EvolutionCandidate> findCandidates(UUID tenantId) {
        return jdbcClient.sql("""
                        select candidate_id, base_harness_version_id, base_manifest_digest,
                               failure_signature, step, check_name, category, required_capability,
                               profile_references, observation_ids, occurrence_count, project_count,
                               risk_tier, change_kind, status, transition_reason,
                               row_version, created_at, updated_at
                        from control_plane.evolution_candidates
                        where tenant_id = :tenantId
                        order by updated_at desc, candidate_id
                        limit 200
                        """)
                .param("tenantId", tenantId)
                .query(this::mapCandidate)
                .list();
    }

    @Override
    public Optional<EvolutionCandidate> findCandidate(UUID tenantId, String candidateId) {
        return jdbcClient.sql("""
                        select candidate_id, base_harness_version_id, base_manifest_digest,
                               failure_signature, step, check_name, category, required_capability,
                               profile_references, observation_ids, occurrence_count, project_count,
                               risk_tier, change_kind, status, transition_reason,
                               row_version, created_at, updated_at
                        from control_plane.evolution_candidates
                        where tenant_id = :tenantId and candidate_id = :candidateId
                        """)
                .param("tenantId", tenantId)
                .param("candidateId", candidateId)
                .query(this::mapCandidate)
                .optional();
    }

    @Override
    public List<EvolutionTrial> findTrials(UUID tenantId, String candidateId) {
        return jdbcClient.sql("select " + TRIAL_COLUMNS + """
                        from control_plane.evolution_trials
                        where tenant_id = :tenantId and candidate_id = :candidateId
                        order by attempt desc
                        """)
                .param("tenantId", tenantId)
                .param("candidateId", candidateId)
                .query(this::mapTrial)
                .list();
    }

    @Override
    public Optional<EvolutionTrial> findTrial(UUID tenantId, UUID trialId) {
        return jdbcClient.sql("select " + TRIAL_COLUMNS + """
                        from control_plane.evolution_trials
                        where tenant_id = :tenantId and trial_id = :trialId
                        """)
                .param("tenantId", tenantId)
                .param("trialId", trialId)
                .query(this::mapTrial)
                .optional();
    }

    @Override
    public Optional<EvolutionTrial> findPendingTrial(UUID tenantId, String candidateId) {
        return jdbcClient.sql("select " + TRIAL_COLUMNS + """
                        from control_plane.evolution_trials
                        where tenant_id = :tenantId
                          and candidate_id = :candidateId
                          and verdict = 'PENDING'
                        """)
                .param("tenantId", tenantId)
                .param("candidateId", candidateId)
                .query(this::mapTrial)
                .optional();
    }

    @Override
    public int nextAttempt(UUID tenantId, String candidateId) {
        return jdbcClient.sql("""
                        select coalesce(max(attempt), 0) + 1
                        from control_plane.evolution_trials
                        where tenant_id = :tenantId and candidate_id = :candidateId
                        """)
                .param("tenantId", tenantId)
                .param("candidateId", candidateId)
                .query(Integer.class)
                .single();
    }

    @Override
    public boolean insertTrial(UUID tenantId, EvolutionTrial trial) {
        return jdbcClient.sql("""
                        insert into control_plane.evolution_trials (
                            tenant_id, trial_id, candidate_id, attempt, input_digest,
                            base_manifest_digest, candidate_digest, eval_suite_digest,
                            optimization_suite_digest, holdout_suite_digest,
                            adversarial_suite_digest, verdict
                        ) values (
                            :tenantId, :trialId, :candidateId, :attempt, :inputDigest,
                            :baseManifestDigest, :candidateDigest, :evalSuiteDigest,
                            :optimizationSuiteDigest, :holdoutSuiteDigest,
                            :adversarialSuiteDigest, 'PENDING'
                        )
                        on conflict do nothing
                        """)
                .param("tenantId", tenantId)
                .param("trialId", trial.trialId())
                .param("candidateId", trial.candidateId())
                .param("attempt", trial.attempt())
                .param("inputDigest", trial.inputDigest())
                .param("baseManifestDigest", trial.baseManifestDigest())
                .param("candidateDigest", trial.candidateDigest())
                .param("evalSuiteDigest", trial.evalSuiteDigest())
                .param("optimizationSuiteDigest", trial.optimizationSuiteDigest())
                .param("holdoutSuiteDigest", trial.holdoutSuiteDigest())
                .param("adversarialSuiteDigest", trial.adversarialSuiteDigest())
                .update() == 1;
    }

    @Override
    public boolean bindWorkflow(UUID tenantId, EvolutionTrial trial, String workflowId) {
        return jdbcClient.sql("""
                        update control_plane.evolution_trials
                        set temporal_workflow_id = :workflowId,
                            row_version = row_version + 1,
                            updated_at = now()
                        where tenant_id = :tenantId
                          and trial_id = :trialId
                          and verdict = 'PENDING'
                          and row_version = :expectedVersion
                          and temporal_workflow_id is null
                        """)
                .param("workflowId", workflowId)
                .param("tenantId", tenantId)
                .param("trialId", trial.trialId())
                .param("expectedVersion", trial.rowVersion())
                .update() == 1;
    }

    @Override
    public boolean completeTrial(UUID tenantId, EvolutionTrial trial, TrialResult result) {
        return jdbcClient.sql("""
                        update control_plane.evolution_trials
                        set patch_commit = :patchCommit,
                            patch_sha256 = :patchSha256,
                            candidate_image_digest = :candidateImageDigest,
                            baseline_metrics = cast(:baselineMetrics as jsonb),
                            candidate_metrics = cast(:candidateMetrics as jsonb),
                            guardrail_results = cast(:guardrailResults as jsonb),
                            verdict = :verdict,
                            report_digest = :reportDigest,
                            authoritative_report = cast(:authoritativeReport as jsonb),
                            report_object_key = :reportObjectKey,
                            llm_tokens = :llmTokens,
                            wall_clock_ms = :wallClockMs,
                            completed_at = :completedAt,
                            row_version = row_version + 1,
                            updated_at = now()
                        where tenant_id = :tenantId
                          and trial_id = :trialId
                          and candidate_id = :candidateId
                          and verdict = 'PENDING'
                          and row_version = :expectedVersion
                          and input_digest = :inputDigest
                          and base_manifest_digest = :baseManifestDigest
                          and eval_suite_digest = :evalSuiteDigest
                          and temporal_workflow_id = :workflowId
                        """)
                .param("patchCommit", result.patchCommit())
                .param("patchSha256", result.patchSha256())
                .param("candidateImageDigest", result.candidateImageDigest())
                .param("baselineMetrics", json(result.baselineMetrics()))
                .param("candidateMetrics", json(result.candidateMetrics()))
                .param("guardrailResults", json(result.guardrailResults()))
                .param("verdict", result.verdict())
                .param("reportDigest", result.reportDigest())
                .param("authoritativeReport", json(result.authoritativeReport()))
                .param("reportObjectKey", result.reportObjectKey())
                .param("llmTokens", result.llmTokens())
                .param("wallClockMs", result.wallClockMs())
                .param("completedAt", result.completedAt().atOffset(java.time.ZoneOffset.UTC))
                .param("tenantId", tenantId)
                .param("trialId", trial.trialId())
                .param("candidateId", trial.candidateId())
                .param("expectedVersion", trial.rowVersion())
                .param("inputDigest", trial.inputDigest())
                .param("baseManifestDigest", trial.baseManifestDigest())
                .param("evalSuiteDigest", trial.evalSuiteDigest())
                .param("workflowId", result.temporalWorkflowId())
                .update() == 1;
    }

    @Override
    public boolean transition(
            UUID tenantId,
            EvolutionCandidate candidate,
            EvolutionCandidate.Status target,
            String reason,
            AuthenticatedActor actor) {
        return jdbcClient.sql("""
                        update control_plane.evolution_candidates
                        set status = :target,
                            transition_reason = :reason,
                            updated_by_issuer = :issuer,
                            updated_by_subject = :subject,
                            row_version = row_version + 1,
                            updated_at = now()
                        where tenant_id = :tenantId
                          and candidate_id = :candidateId
                          and status = :currentStatus
                          and row_version = :expectedVersion
                        """)
                .param("target", target.wireValue())
                .param("reason", reason)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .param("tenantId", tenantId)
                .param("candidateId", candidate.candidateId())
                .param("currentStatus", candidate.status().wireValue())
                .param("expectedVersion", candidate.rowVersion())
                .update() == 1;
    }

    private EvolutionObservation mapObservation(ResultSet resultSet, int rowNumber)
            throws SQLException {
        return new EvolutionObservation(
                resultSet.getString("observation_id"),
                resultSet.getObject("run_id", UUID.class),
                resultSet.getLong("source_event_seq"),
                resultSet.getString("harness_version_id"),
                resultSet.getString("harness_channel"),
                resultSet.getString("harness_manifest_digest"),
                resultSet.getString("profile_reference"),
                resultSet.getString("profile_digest"),
                resultSet.getString("scope_fingerprint"),
                resultSet.getString("project_fingerprint"),
                resultSet.getString("event_type"),
                resultSet.getString("failure_signature"),
                resultSet.getString("step"),
                resultSet.getString("check_name"),
                resultSet.getString("category"),
                resultSet.getString("recoverability"),
                resultSet.getString("strategy"),
                resultSet.getString("required_capability"),
                resultSet.getString("outcome"),
                resultSet.getInt("revision"),
                resultSet.getString("evidence_digest"),
                instant(resultSet, "observed_at"),
                instant(resultSet, "recorded_at"));
    }

    private EvolutionCandidate mapCandidate(ResultSet resultSet, int rowNumber)
            throws SQLException {
        return new EvolutionCandidate(
                resultSet.getString("candidate_id"),
                resultSet.getString("base_harness_version_id"),
                resultSet.getString("base_manifest_digest"),
                resultSet.getString("failure_signature"),
                resultSet.getString("step"),
                resultSet.getString("check_name"),
                resultSet.getString("category"),
                resultSet.getString("required_capability"),
                jsonList(resultSet.getString("profile_references")),
                jsonList(resultSet.getString("observation_ids")),
                resultSet.getInt("occurrence_count"),
                resultSet.getInt("project_count"),
                resultSet.getString("risk_tier"),
                resultSet.getString("change_kind"),
                EvolutionCandidate.Status.fromWireValue(resultSet.getString("status")),
                resultSet.getString("transition_reason"),
                resultSet.getLong("row_version"),
                instant(resultSet, "created_at"),
                instant(resultSet, "updated_at"));
    }

    private EvolutionTrial mapTrial(ResultSet resultSet, int rowNumber) throws SQLException {
        return new EvolutionTrial(
                resultSet.getObject("trial_id", UUID.class),
                resultSet.getString("candidate_id"),
                resultSet.getInt("attempt"),
                resultSet.getString("input_digest"),
                resultSet.getString("base_manifest_digest"),
                resultSet.getString("candidate_digest"),
                resultSet.getString("eval_suite_digest"),
                resultSet.getString("temporal_workflow_id"),
                resultSet.getString("patch_commit"),
                resultSet.getString("patch_sha256"),
                resultSet.getString("candidate_image_digest"),
                resultSet.getString("optimization_suite_digest"),
                resultSet.getString("holdout_suite_digest"),
                resultSet.getString("adversarial_suite_digest"),
                jsonObject(resultSet.getString("baseline_metrics")),
                jsonObject(resultSet.getString("candidate_metrics")),
                jsonObject(resultSet.getString("guardrail_results")),
                resultSet.getString("verdict"),
                resultSet.getString("report_digest"),
                jsonObject(resultSet.getString("authoritative_report")),
                resultSet.getString("report_object_key"),
                resultSet.getLong("llm_tokens"),
                resultSet.getLong("wall_clock_ms"),
                resultSet.getLong("row_version"),
                instant(resultSet, "created_at"),
                instant(resultSet, "updated_at"),
                nullableInstant(resultSet, "completed_at"));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonObject(String value) {
        try {
            return (Map<String, Object>) objectMapper.readValue(value, Map.class);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to read evolution metadata", exception);
        }
    }

    private String json(List<String> value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to write evolution metadata", exception);
        }
    }

    private String json(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to write evolution metadata", exception);
        }
    }

    @SuppressWarnings("unchecked")
    private List<String> jsonList(String value) {
        try {
            return List.copyOf((List<String>) objectMapper.readValue(value, List.class));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to read evolution metadata", exception);
        }
    }

    private static Instant instant(ResultSet resultSet, String column) throws SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }

    private static Instant nullableInstant(ResultSet resultSet, String column) throws SQLException {
        OffsetDateTime value = resultSet.getObject(column, OffsetDateTime.class);
        return value == null ? null : value.toInstant();
    }

}

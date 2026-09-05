package team.ratsnest.controlplane.harness.infrastructure.persistence;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.Optional;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.harness.domain.model.HarnessVersion;
import team.ratsnest.controlplane.harness.domain.port.HarnessVersionRepository;

@Repository
public class JdbcHarnessVersionRepository implements HarnessVersionRepository {

    private final JdbcClient jdbcClient;

    public JdbcHarnessVersionRepository(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    @Override
    public Optional<HarnessVersion> find(String harnessVersionId) {
        return jdbcClient.sql("""
                        select harness_version_id, version, parent_version_id, source_commit,
                               source_tree_digest, dirty, runtime_image_digest, toolchain_digest,
                               bundle_digest, contract_digest, policy_digest,
                               manifest_object_key, manifest_digest, release_status, attested,
                               created_by, created_at, activated_at, transition_reason,
                               updated_by, row_version, updated_at
                        from control_plane.harness_versions
                        where harness_version_id = :harnessVersionId
                        """)
                .param("harnessVersionId", harnessVersionId)
                .query(JdbcHarnessVersionRepository::map)
                .optional();
    }

    @Override
    public Optional<HarnessVersion> findByManifest(String manifestDigest) {
        return jdbcClient.sql("select harness_version_id from control_plane.harness_versions "
                        + "where manifest_digest = :digest")
                .param("digest", manifestDigest).query(String.class).optional().flatMap(this::find);
    }

    @Override
    public boolean insert(HarnessVersion value) {
        return jdbcClient.sql("""
                        insert into control_plane.harness_versions (
                            harness_version_id, version, parent_version_id, source_commit,
                            source_tree_digest, dirty, runtime_image_digest, toolchain_digest,
                            bundle_digest, contract_digest, policy_digest, manifest_object_key,
                            manifest_digest, release_status, attested, created_by,
                            transition_reason, updated_by
                        ) values (
                            :harnessVersionId, :version, :parentVersionId, :sourceCommit,
                            :sourceTreeDigest, :dirty, :runtimeImageDigest, :toolchainDigest,
                            :bundleDigest, :contractDigest, :policyDigest, :manifestObjectKey,
                            :manifestDigest, :releaseStatus, :attested, :createdBy,
                            :transitionReason, :updatedBy
                        )
                        on conflict do nothing
                        """)
                .param("harnessVersionId", value.harnessVersionId())
                .param("version", value.version())
                .param("parentVersionId", value.parentVersionId())
                .param("sourceCommit", value.sourceCommit())
                .param("sourceTreeDigest", value.sourceTreeDigest())
                .param("dirty", value.dirty())
                .param("runtimeImageDigest", value.runtimeImageDigest())
                .param("toolchainDigest", value.toolchainDigest())
                .param("bundleDigest", value.bundleDigest())
                .param("contractDigest", value.contractDigest())
                .param("policyDigest", value.policyDigest())
                .param("manifestObjectKey", value.manifestObjectKey())
                .param("manifestDigest", value.manifestDigest())
                .param("releaseStatus", value.releaseStatus().name())
                .param("attested", value.attested())
                .param("createdBy", value.createdBy())
                .param("transitionReason", value.transitionReason())
                .param("updatedBy", value.updatedBy())
                .update() == 1;
    }

    @Override
    public boolean transition(
            HarnessVersion current,
            HarnessVersion.ReleaseStatus target,
            String reason,
            String updatedBy) {
        return jdbcClient.sql("""
                        update control_plane.harness_versions
                        set release_status = :target,
                            transition_reason = :reason,
                            updated_by = :updatedBy,
                            activated_at = case
                                when :target in ('CANARY', 'STABLE')
                                    then coalesce(activated_at, now())
                                else activated_at
                            end,
                            row_version = row_version + 1,
                            updated_at = now()
                        where harness_version_id = :harnessVersionId
                          and release_status = :currentStatus
                          and row_version = :expectedVersion
                        """)
                .param("target", target.name())
                .param("reason", reason)
                .param("updatedBy", updatedBy)
                .param("harnessVersionId", current.harnessVersionId())
                .param("currentStatus", current.releaseStatus().name())
                .param("expectedVersion", current.rowVersion())
                .update() == 1;
    }

    private static HarnessVersion map(ResultSet resultSet, int rowNumber) throws SQLException {
        return new HarnessVersion(
                resultSet.getString("harness_version_id"),
                resultSet.getString("version"),
                resultSet.getString("parent_version_id"),
                resultSet.getString("source_commit"),
                resultSet.getString("source_tree_digest"),
                resultSet.getBoolean("dirty"),
                resultSet.getString("runtime_image_digest"),
                resultSet.getString("toolchain_digest"),
                resultSet.getString("bundle_digest"),
                resultSet.getString("contract_digest"),
                resultSet.getString("policy_digest"),
                resultSet.getString("manifest_object_key"),
                resultSet.getString("manifest_digest"),
                HarnessVersion.ReleaseStatus.valueOf(resultSet.getString("release_status")),
                resultSet.getBoolean("attested"),
                resultSet.getString("created_by"),
                instant(resultSet, "created_at"),
                nullableInstant(resultSet, "activated_at"),
                resultSet.getString("transition_reason"),
                resultSet.getString("updated_by"),
                resultSet.getLong("row_version"),
                instant(resultSet, "updated_at"));
    }

    private static Instant instant(ResultSet resultSet, String column) throws SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }

    private static Instant nullableInstant(ResultSet resultSet, String column) throws SQLException {
        OffsetDateTime value = resultSet.getObject(column, OffsetDateTime.class);
        return value == null ? null : value.toInstant();
    }
}

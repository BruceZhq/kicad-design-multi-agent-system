package team.ratsnest.controlplane.artifact.infrastructure.persistence;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.artifact.domain.model.Artifact;
import team.ratsnest.controlplane.artifact.domain.model.ArtifactManifest;
import team.ratsnest.controlplane.artifact.domain.port.ArtifactStore;

@Repository
public class JdbcArtifactStore implements ArtifactStore {

    private final JdbcClient jdbcClient;

    public JdbcArtifactStore(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    public boolean persist(UUID tenantId, UUID runId, ArtifactManifest manifest) {
        int inserted = jdbcClient.sql("""
                        insert into control_plane.artifact_manifests (
                            tenant_id, manifest_id, run_id, source_event_seq,
                            delivery_status, manifest_digest, trusted
                        ) values (
                            :tenantId, :manifestId, :runId, :sourceEventSeq,
                            :deliveryStatus, :digest, :trusted
                        )
                        on conflict (tenant_id, run_id) do nothing
                        """)
                .param("tenantId", tenantId)
                .param("manifestId", manifest.manifestId())
                .param("runId", runId)
                .param("sourceEventSeq", manifest.sourceEventSeq())
                .param("deliveryStatus", manifest.deliveryStatus().apiValue())
                .param("digest", manifest.digest())
                .param("trusted", manifest.trusted())
                .update();
        if (inserted == 0) {
            requireSameManifest(tenantId, runId, manifest);
            return false;
        }
        for (Artifact artifact : manifest.artifacts()) {
            jdbcClient.sql("""
                            insert into control_plane.artifacts (
                                tenant_id, artifact_id, manifest_id, run_id,
                                name, kind, media_type, size_bytes, sha256, object_key
                            ) values (
                                :tenantId, :artifactId, :manifestId, :runId,
                                :name, :kind, :mediaType, :sizeBytes, :sha256, :objectKey
                            )
                            """)
                    .param("tenantId", tenantId)
                    .param("artifactId", artifact.artifactId())
                    .param("manifestId", manifest.manifestId())
                    .param("runId", runId)
                    .param("name", artifact.name())
                    .param("kind", artifact.kind())
                    .param("mediaType", artifact.mediaType())
                    .param("sizeBytes", artifact.sizeBytes())
                    .param("sha256", artifact.sha256())
                    .param("objectKey", artifact.objectKey())
                    .update();
        }
        return true;
    }

    public List<Artifact> findByRun(UUID tenantId, UUID runId) {
        return jdbcClient.sql("""
                        select artifact_id, run_id, name, kind, media_type,
                               size_bytes, sha256, object_key, created_at
                        from control_plane.artifacts
                        where tenant_id = :tenantId and run_id = :runId
                        order by name, artifact_id
                        """)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .query(JdbcArtifactStore::map)
                .list();
    }

    public Optional<Artifact> find(UUID tenantId, UUID artifactId) {
        return jdbcClient.sql("""
                        select artifact_id, run_id, name, kind, media_type,
                               size_bytes, sha256, object_key, created_at
                        from control_plane.artifacts
                        where tenant_id = :tenantId and artifact_id = :artifactId
                        """)
                .param("tenantId", tenantId)
                .param("artifactId", artifactId)
                .query(JdbcArtifactStore::map)
                .optional();
    }

    public boolean hasManifest(UUID tenantId, UUID runId) {
        return Boolean.TRUE.equals(jdbcClient.sql("""
                        select exists (
                            select 1 from control_plane.artifact_manifests
                            where tenant_id = :tenantId and run_id = :runId
                        )
                        """)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .query(Boolean.class)
                .single());
    }

    public boolean isSuperseded(UUID tenantId, UUID runId) {
        return Boolean.TRUE.equals(jdbcClient.sql("""
                        select exists (
                            select 1
                            from control_plane.runs current_run
                            join control_plane.runs newer_run
                              on newer_run.tenant_id = current_run.tenant_id
                             and newer_run.root_run_id = current_run.root_run_id
                             and newer_run.revision_number > current_run.revision_number
                            where current_run.tenant_id = :tenantId
                              and current_run.run_id = :runId
                        )
                        """)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .query(Boolean.class)
                .single());
    }

    private void requireSameManifest(
            UUID tenantId,
            UUID runId,
            ArtifactManifest manifest) {
        boolean same = Boolean.TRUE.equals(jdbcClient.sql("""
                        select exists (
                            select 1 from control_plane.artifact_manifests
                            where tenant_id = :tenantId and run_id = :runId
                              and manifest_id = :manifestId
                              and manifest_digest = :digest
                              and delivery_status = :deliveryStatus
                        )
                        """)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .param("manifestId", manifest.manifestId())
                .param("digest", manifest.digest())
                .param("deliveryStatus", manifest.deliveryStatus().apiValue())
                .query(Boolean.class)
                .single());
        if (!same) {
            throw new IllegalStateException("Agent Runtime changed an immutable artifact manifest");
        }
    }

    private static Artifact map(java.sql.ResultSet resultSet, int rowNumber)
            throws java.sql.SQLException {
        return new Artifact(
                resultSet.getObject("artifact_id", UUID.class),
                resultSet.getObject("run_id", UUID.class),
                resultSet.getString("name"),
                resultSet.getString("kind"),
                resultSet.getString("media_type"),
                resultSet.getLong("size_bytes"),
                resultSet.getString("sha256"),
                resultSet.getString("object_key"),
                instant(resultSet, "created_at"));
    }

    private static Instant instant(java.sql.ResultSet resultSet, String column)
            throws java.sql.SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }
}

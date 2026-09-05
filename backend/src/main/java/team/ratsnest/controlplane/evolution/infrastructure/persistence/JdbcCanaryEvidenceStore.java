package team.ratsnest.controlplane.evolution.infrastructure.persistence;

import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import team.ratsnest.controlplane.evolution.domain.port.CanaryEvidenceStore;

@Repository
public class JdbcCanaryEvidenceStore implements CanaryEvidenceStore {
    private final JdbcClient jdbc;

    public JdbcCanaryEvidenceStore(JdbcClient jdbc) { this.jdbc = jdbc; }

    @Override
    public List<Map<String, Object>> observations(
            UUID tenantId, String version, String digest, Instant since) {
        // Include failures and unfinished runs, not only successful manifests.
        // Parent/root IDs are retained so resumed revisions cannot inflate N.
        return jdbc.sql("""
                select r.run_id::text, coalesce(r.root_run_id, r.run_id)::text as root_run_id, r.state,
                       coalesce(m.delivery_status, '') as delivery_status,
                       coalesce(m.manifest_digest, '') as manifest_digest,
                       coalesce(m.trusted, false) as trusted,
                       coalesce((select string_agg(a.name, '|' order by a.name)
                         from control_plane.artifacts a
                         where a.tenant_id=r.tenant_id and a.run_id=r.run_id), '') as files
                from control_plane.runs r
                left join control_plane.artifact_manifests m
                  on m.tenant_id=r.tenant_id and m.run_id=r.run_id
                where r.tenant_id=:tenant and r.harness_version_id=:version
                  and r.harness_manifest_digest=:digest and r.harness_channel='canary'
                  and r.created_at >= :since
                order by r.created_at, r.run_id limit 501
                """)
                .param("tenant", tenantId).param("version", version).param("digest", digest)
                .param("since", since.atOffset(ZoneOffset.UTC)).query((rs, row) -> Map.<String, Object>of(
                        "runId", rs.getString("run_id"), "rootRunId", rs.getString("root_run_id"),
                        "state", rs.getString("state"), "deliveryStatus", rs.getString("delivery_status"),
                        "manifestDigest", rs.getString("manifest_digest"), "trusted", rs.getBoolean("trusted"),
                        "files", rs.getString("files"))).list();
    }
}

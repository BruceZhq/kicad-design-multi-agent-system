package team.ratsnest.controlplane.evolution.domain.port;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/** Read only, server-persisted delivery evidence; never accepts caller metrics. */
public interface CanaryEvidenceStore {
    List<Map<String, Object>> observations(
            UUID tenantId, String harnessVersionId, String manifestDigest, Instant since);
}

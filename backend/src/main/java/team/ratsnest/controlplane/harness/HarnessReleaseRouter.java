package team.ratsnest.controlplane.harness;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.Set;
import java.util.UUID;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.shared.web.ApiException;

@Service
public class HarnessReleaseRouter {

    private final HarnessRolloutRepository rollouts;
    private final HarnessVersionService versions;
    private final String rolloutId;
    private final byte[] routingSecret;

    public HarnessReleaseRouter(
            HarnessRolloutRepository rollouts,
            HarnessVersionService versions,
            @Value("${ratsnest.harness.rollout-id:production}") String rolloutId,
            @Value("${ratsnest.harness.routing-secret:${ratsnest.agent-runtime.signing-secret:}}")
                    String routingSecret) {
        this.rollouts = rollouts;
        this.versions = versions;
        this.rolloutId = rolloutId.strip();
        this.routingSecret = routingSecret == null
                ? new byte[0]
                : routingSecret.getBytes(StandardCharsets.UTF_8);
        if (this.routingSecret.length < 32) {
            throw new IllegalStateException("Harness routing secret must contain at least 32 bytes");
        }
    }

    @Transactional(readOnly = true)
    public HarnessSelection route(
            UUID tenantId,
            UUID projectId,
            String idempotencyKey) {
        HarnessRollout rollout = requireRollout();
        boolean canary = rollout.canaryPercent() > 0
                && rollout.canaryVersionId() != null
                && bucket(tenantId, projectId, idempotencyKey) < rollout.canaryPercent();
        String channel = canary ? "canary" : "stable";
        String versionId = canary
                ? rollout.canaryVersionId()
                : rollout.stableVersionId();
        HarnessVersion version = versions.require(versionId);
        requireDeployable(version, channel);
        return new HarnessSelection(version, channel, rollout.rolloutId(), rollout.rowVersion());
    }

    @Transactional(readOnly = true)
    public HarnessSelection stable() {
        HarnessRollout rollout = requireRollout();
        HarnessVersion version = versions.require(rollout.stableVersionId());
        requireDeployable(version, "stable");
        return new HarnessSelection(version, "stable", rollout.rolloutId(), rollout.rowVersion());
    }

    private HarnessRollout requireRollout() {
        return rollouts.find(rolloutId).orElseThrow(() -> new ApiException(
                "HARNESS_ROLLOUT_NOT_FOUND",
                HttpStatus.SERVICE_UNAVAILABLE,
                "The configured harness rollout is not registered."));
    }

    private int bucket(UUID tenantId, UUID projectId, String idempotencyKey) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(routingSecret, "HmacSHA256"));
            byte[] digest = mac.doFinal(
                    (tenantId + "\u0000" + projectId + "\u0000" + idempotencyKey)
                            .getBytes(StandardCharsets.UTF_8));
            return Math.floorMod(ByteBuffer.wrap(digest).getLong(), 100);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to calculate harness rollout bucket", exception);
        }
    }

    private void requireDeployable(HarnessVersion version, String channel) {
        Set<HarnessVersion.ReleaseStatus> allowed = "canary".equals(channel)
                ? Set.of(HarnessVersion.ReleaseStatus.APPROVED, HarnessVersion.ReleaseStatus.CANARY)
                : Set.of(HarnessVersion.ReleaseStatus.STABLE);
        if (!allowed.contains(version.releaseStatus())) {
            throw new ApiException(
                    "HARNESS_VERSION_NOT_DEPLOYABLE",
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "The rollout references a harness version that is not deployable.");
        }
    }

    public record HarnessSelection(
            HarnessVersion version,
            String channel,
            String rolloutId,
            long rolloutVersion) {
    }
}

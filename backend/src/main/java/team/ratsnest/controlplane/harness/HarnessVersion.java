package team.ratsnest.controlplane.harness;

import java.time.Instant;

public record HarnessVersion(
        String harnessVersionId,
        String version,
        String parentVersionId,
        String sourceCommit,
        String sourceTreeDigest,
        boolean dirty,
        String runtimeImageDigest,
        String toolchainDigest,
        String bundleDigest,
        String contractDigest,
        String policyDigest,
        String manifestObjectKey,
        String manifestDigest,
        ReleaseStatus releaseStatus,
        boolean attested,
        String createdBy,
        Instant createdAt,
        Instant activatedAt,
        String transitionReason,
        String updatedBy,
        long rowVersion,
        Instant updatedAt) {

    public enum ReleaseStatus {
        CANDIDATE,
        APPROVED,
        CANARY,
        STABLE,
        RETIRED,
        ROLLED_BACK
    }
}

package team.ratsnest.controlplane.harness;

import java.time.Instant;
import java.util.Objects;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;

@Service
public class HarnessVersionService {

    private final HarnessVersionRepository versions;
    private final HarnessRolloutRepository rollouts;

    public HarnessVersionService(
            HarnessVersionRepository versions,
            HarnessRolloutRepository rollouts) {
        this.versions = versions;
        this.rollouts = rollouts;
    }

    @Transactional(readOnly = true)
    public HarnessVersion get(String harnessVersionId) {
        return require(harnessVersionId);
    }

    public HarnessVersion require(String harnessVersionId) {
        return versions.find(harnessVersionId).orElseThrow(() -> new ApiException(
                "HARNESS_VERSION_NOT_FOUND",
                HttpStatus.SERVICE_UNAVAILABLE,
                "The configured harness version is not registered."));
    }

    @Transactional(readOnly = true)
    public HarnessRollout rollout(String rolloutId) {
        return requireRollout(rolloutId);
    }

    @Transactional
    public HarnessVersion register(RegisterCommand command, AuthenticatedActor actor) {
        if (command.parentVersionId() != null) {
            require(command.parentVersionId());
        }
        Instant now = Instant.now();
        HarnessVersion candidate = new HarnessVersion(
                command.harnessVersionId(),
                command.version(),
                command.parentVersionId(),
                command.sourceCommit(),
                command.sourceTreeDigest(),
                command.dirty(),
                command.runtimeImageDigest(),
                command.toolchainDigest(),
                command.bundleDigest(),
                command.contractDigest(),
                command.policyDigest(),
                command.manifestObjectKey(),
                command.manifestDigest(),
                HarnessVersion.ReleaseStatus.CANDIDATE,
                !command.dirty()
                        && command.runtimeImageDigest() != null
                        && command.manifestObjectKey() != null,
                actor.subject(),
                now,
                null,
                "registered",
                actor.subject(),
                1,
                now);
        if (!candidate.attested()) {
            throw new ApiException(
                    "HARNESS_ATTESTATION_REQUIRED",
                    HttpStatus.BAD_REQUEST,
                    "A release candidate must be clean and include an image digest and manifest object key.");
        }
        if (versions.insert(candidate)) {
            return require(candidate.harnessVersionId());
        }
        HarnessVersion existing = versions.find(candidate.harnessVersionId())
                .orElseThrow(() -> new ApiException(
                        "HARNESS_VERSION_CONFLICT",
                        HttpStatus.CONFLICT,
                        "The harness version or display version is already registered."));
        if (!sameIdentity(existing, candidate)) {
            throw new ApiException(
                    "HARNESS_VERSION_CONFLICT",
                    HttpStatus.CONFLICT,
                    "The harness version ID is already registered with different immutable evidence.");
        }
        return existing;
    }

    @Transactional
    public HarnessVersion transition(
            String harnessVersionId,
            long expectedVersion,
            HarnessVersion.ReleaseStatus target,
            String reason,
            AuthenticatedActor actor) {
        HarnessVersion current = require(harnessVersionId);
        requireExpected(current.rowVersion(), expectedVersion);
        if (!(target == HarnessVersion.ReleaseStatus.APPROVED
                || target == HarnessVersion.ReleaseStatus.RETIRED)
                || !canTransition(current.releaseStatus(), target)) {
            throw invalidTransition();
        }
        transition(current, target, reason, actor);
        return require(harnessVersionId);
    }

    @Transactional
    public HarnessRollout configureCanary(
            String rolloutId,
            long expectedVersion,
            String canaryVersionId,
            int canaryPercent,
            AuthenticatedActor actor) {
        HarnessRollout rollout = requireRollout(rolloutId);
        requireExpected(rollout.rowVersion(), expectedVersion);
        if (canaryPercent < 0 || canaryPercent > 100
                || (canaryPercent > 0 && canaryVersionId == null)) {
            throw new ApiException(
                    "HARNESS_ROLLOUT_INVALID",
                    HttpStatus.BAD_REQUEST,
                    "Canary percent must be 0-100 and a positive percent requires a canary version.");
        }
        if (canaryVersionId != null) {
            if (canaryVersionId.equals(rollout.stableVersionId())) {
                throw new ApiException(
                        "HARNESS_ROLLOUT_INVALID",
                        HttpStatus.BAD_REQUEST,
                        "Stable and canary harness versions must differ.");
            }
            HarnessVersion canary = require(canaryVersionId);
            requireAttested(canary);
            if (canary.releaseStatus() == HarnessVersion.ReleaseStatus.APPROVED) {
                transition(canary, HarnessVersion.ReleaseStatus.CANARY, "canary rollout", actor);
            } else if (canary.releaseStatus() != HarnessVersion.ReleaseStatus.CANARY) {
                throw invalidTransition();
            }
        }
        if (!rollouts.configureCanary(
                rollout, canaryVersionId, canaryPercent, actor.subject())) {
            throw stale();
        }
        return requireRollout(rolloutId);
    }

    @Transactional
    public HarnessRollout promote(
            String rolloutId,
            long expectedVersion,
            String canaryVersionId,
            AuthenticatedActor actor) {
        HarnessRollout rollout = requireRollout(rolloutId);
        requireExpected(rollout.rowVersion(), expectedVersion);
        if (!Objects.equals(rollout.canaryVersionId(), canaryVersionId)) {
            throw stale();
        }
        HarnessVersion stable = require(rollout.stableVersionId());
        HarnessVersion canary = require(canaryVersionId);
        requireAttested(canary);
        if (stable.releaseStatus() != HarnessVersion.ReleaseStatus.STABLE
                || canary.releaseStatus() != HarnessVersion.ReleaseStatus.CANARY) {
            throw invalidTransition();
        }
        transition(stable, HarnessVersion.ReleaseStatus.RETIRED, "superseded", actor);
        transition(canary, HarnessVersion.ReleaseStatus.STABLE, "promoted", actor);
        String rollbackTarget = stable.attested() ? stable.harnessVersionId() : null;
        if (!rollouts.promote(
                rollout, canaryVersionId, rollbackTarget, actor.subject())) {
            throw stale();
        }
        return requireRollout(rolloutId);
    }

    @Transactional
    public HarnessRollout rollback(
            String rolloutId,
            long expectedVersion,
            String targetVersionId,
            AuthenticatedActor actor) {
        HarnessRollout rollout = requireRollout(rolloutId);
        requireExpected(rollout.rowVersion(), expectedVersion);
        if (!Objects.equals(rollout.previousStableVersionId(), targetVersionId)) {
            throw invalidRollbackTarget();
        }
        if (rollout.canaryVersionId() != null || rollout.canaryPercent() != 0) {
            throw new ApiException(
                    "HARNESS_ACTIVE_CANARY_CONFLICT",
                    HttpStatus.CONFLICT,
                    "Stop the active canary before rolling back the promoted stable version.");
        }
        HarnessVersion current = require(rollout.stableVersionId());
        HarnessVersion target = require(targetVersionId);
        requireAttested(target);
        if (current.releaseStatus() != HarnessVersion.ReleaseStatus.STABLE
                || target.releaseStatus() != HarnessVersion.ReleaseStatus.RETIRED) {
            throw invalidTransition();
        }
        transition(current, HarnessVersion.ReleaseStatus.ROLLED_BACK, "stable rollback", actor);
        transition(target, HarnessVersion.ReleaseStatus.STABLE, "restored stable", actor);
        if (!rollouts.rollback(rollout, targetVersionId, actor.subject())) {
            throw stale();
        }
        return requireRollout(rolloutId);
    }

    private HarnessRollout requireRollout(String rolloutId) {
        return rollouts.find(rolloutId).orElseThrow(() -> new ApiException(
                "HARNESS_ROLLOUT_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The harness rollout was not found."));
    }

    private void transition(
            HarnessVersion current,
            HarnessVersion.ReleaseStatus target,
            String reason,
            AuthenticatedActor actor) {
        if (!canTransition(current.releaseStatus(), target)
                || !versions.transition(current, target, reason, actor.subject())) {
            throw stale();
        }
    }

    private boolean canTransition(
            HarnessVersion.ReleaseStatus source,
            HarnessVersion.ReleaseStatus target) {
        return switch (source) {
            case CANDIDATE -> target == HarnessVersion.ReleaseStatus.APPROVED
                    || target == HarnessVersion.ReleaseStatus.RETIRED;
            case APPROVED -> target == HarnessVersion.ReleaseStatus.CANARY
                    || target == HarnessVersion.ReleaseStatus.RETIRED;
            case CANARY -> target == HarnessVersion.ReleaseStatus.STABLE
                    || target == HarnessVersion.ReleaseStatus.ROLLED_BACK;
            case STABLE -> target == HarnessVersion.ReleaseStatus.RETIRED
                    || target == HarnessVersion.ReleaseStatus.ROLLED_BACK;
            case RETIRED -> target == HarnessVersion.ReleaseStatus.STABLE;
            case ROLLED_BACK -> false;
        };
    }

    private void requireAttested(HarnessVersion version) {
        if (!version.attested() || version.dirty()
                || version.runtimeImageDigest() == null
                || version.manifestObjectKey() == null) {
            throw new ApiException(
                    "HARNESS_ATTESTATION_REQUIRED",
                    HttpStatus.CONFLICT,
                    "Only an attested clean harness version can enter a rollout.");
        }
    }

    private void requireExpected(long actual, long expected) {
        if (actual != expected) {
            throw stale();
        }
    }

    private boolean sameIdentity(HarnessVersion left, HarnessVersion right) {
        return Objects.equals(left.version(), right.version())
                && Objects.equals(left.parentVersionId(), right.parentVersionId())
                && Objects.equals(left.sourceCommit(), right.sourceCommit())
                && Objects.equals(left.sourceTreeDigest(), right.sourceTreeDigest())
                && left.dirty() == right.dirty()
                && Objects.equals(left.runtimeImageDigest(), right.runtimeImageDigest())
                && Objects.equals(left.toolchainDigest(), right.toolchainDigest())
                && Objects.equals(left.bundleDigest(), right.bundleDigest())
                && Objects.equals(left.contractDigest(), right.contractDigest())
                && Objects.equals(left.policyDigest(), right.policyDigest())
                && Objects.equals(left.manifestObjectKey(), right.manifestObjectKey())
                && Objects.equals(left.manifestDigest(), right.manifestDigest());
    }

    private ApiException stale() {
        return new ApiException(
                "HARNESS_RELEASE_STALE",
                HttpStatus.CONFLICT,
                "The harness release state changed; reload before retrying.");
    }

    private ApiException invalidTransition() {
        return new ApiException(
                "HARNESS_RELEASE_TRANSITION_INVALID",
                HttpStatus.CONFLICT,
                "The requested harness release transition is not allowed.");
    }

    private ApiException invalidRollbackTarget() {
        return new ApiException(
                "HARNESS_ROLLBACK_TARGET_INVALID",
                HttpStatus.CONFLICT,
                "The target is not the attested previous stable version recorded by this rollout.");
    }

    public record RegisterCommand(
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
            String manifestDigest) {
    }

}

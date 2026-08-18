package team.ratsnest.controlplane.harness;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.Optional;

import org.junit.jupiter.api.Test;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;

class HarnessVersionServiceTest {

    private static final String DIGEST = "a".repeat(64);
    private static final AuthenticatedActor ACTOR =
            new AuthenticatedActor("https://issuer.example", "release-admin");

    private final HarnessVersionRepository versions = mock(HarnessVersionRepository.class);
    private final HarnessRolloutRepository rollouts = mock(HarnessRolloutRepository.class);
    private final HarnessVersionService service = new HarnessVersionService(versions, rollouts);

    @Test
    void restoresOnlyTheRecordedAttestedPreviousStableWithCas() {
        HarnessRollout current = rollout("harness-v2", "harness-v1", 7);
        HarnessRollout updated = rollout("harness-v1", null, 8);
        HarnessVersion active = version("harness-v2", HarnessVersion.ReleaseStatus.STABLE, 4);
        HarnessVersion target = version("harness-v1", HarnessVersion.ReleaseStatus.RETIRED, 9);
        when(rollouts.find("production"))
                .thenReturn(Optional.of(current), Optional.of(updated));
        when(versions.find("harness-v2")).thenReturn(Optional.of(active));
        when(versions.find("harness-v1")).thenReturn(Optional.of(target));
        when(versions.transition(
                active,
                HarnessVersion.ReleaseStatus.ROLLED_BACK,
                "stable rollback",
                ACTOR.subject())).thenReturn(true);
        when(versions.transition(
                target,
                HarnessVersion.ReleaseStatus.STABLE,
                "restored stable",
                ACTOR.subject())).thenReturn(true);
        when(rollouts.rollback(current, "harness-v1", ACTOR.subject())).thenReturn(true);

        HarnessRollout result = service.rollback("production", 7, "harness-v1", ACTOR);

        assertThat(result.stableVersionId()).isEqualTo("harness-v1");
        assertThat(result.previousStableVersionId()).isNull();
        assertThat(result.canaryVersionId()).isNull();
        assertThat(result.canaryPercent()).isZero();
        verify(versions).transition(
                active,
                HarnessVersion.ReleaseStatus.ROLLED_BACK,
                "stable rollback",
                ACTOR.subject());
        verify(versions).transition(
                target,
                HarnessVersion.ReleaseStatus.STABLE,
                "restored stable",
                ACTOR.subject());
        verify(rollouts).rollback(current, "harness-v1", ACTOR.subject());
    }

    @Test
    void rejectsAnArbitraryRetiredRollbackTargetBeforeReadingVersions() {
        when(rollouts.find("production"))
                .thenReturn(Optional.of(rollout("harness-v2", "harness-v1", 7)));

        ApiException failure = assertThrows(
                ApiException.class,
                () -> service.rollback("production", 7, "unrelated-retired", ACTOR));

        assertThat(failure.code()).isEqualTo("HARNESS_ROLLBACK_TARGET_INVALID");
        verifyNoInteractions(versions);
    }

    private HarnessRollout rollout(
            String stableVersionId,
            String previousStableVersionId,
            long rowVersion) {
        return new HarnessRollout(
                "production",
                stableVersionId,
                previousStableVersionId,
                null,
                0,
                rowVersion,
                ACTOR.subject(),
                Instant.parse("2026-08-19T00:00:00Z"));
    }

    private HarnessVersion version(
            String versionId,
            HarnessVersion.ReleaseStatus status,
            long rowVersion) {
        Instant now = Instant.parse("2026-08-19T00:00:00Z");
        return new HarnessVersion(
                versionId,
                versionId,
                null,
                "b".repeat(40),
                DIGEST,
                false,
                "sha256:" + DIGEST,
                null,
                DIGEST,
                DIGEST,
                DIGEST,
                "harness/" + versionId + "/manifest.json",
                DIGEST,
                status,
                true,
                ACTOR.subject(),
                now,
                now,
                null,
                ACTOR.subject(),
                rowVersion,
                now);
    }
}

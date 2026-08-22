package team.ratsnest.controlplane.harness.api;

import java.time.Instant;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import team.ratsnest.controlplane.harness.api.HarnessVersionController.HarnessVersionResponse;
import team.ratsnest.controlplane.harness.application.HarnessVersionService;
import team.ratsnest.controlplane.harness.domain.model.HarnessRollout;
import team.ratsnest.controlplane.harness.domain.model.HarnessVersion;
import team.ratsnest.controlplane.identity.api.JwtPlatformPrincipal;
import team.ratsnest.controlplane.identity.application.PlatformAccess;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

@RestController
@Validated
@RequestMapping("/api/v1/platform")
public class HarnessReleaseAdminController {

    private static final String VERSION_ID = "[A-Za-z0-9._:-]{1,120}";
    private static final String DIGEST = "[0-9a-f]{64}";

    private final PlatformAccess platformAccess;
    private final HarnessVersionService versions;

    public HarnessReleaseAdminController(
            PlatformAccess platformAccess,
            HarnessVersionService versions) {
        this.platformAccess = platformAccess;
        this.versions = versions;
    }

    @PostMapping("/harness-versions")
    HarnessVersionResponse register(
            @Valid @RequestBody RegisterRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return HarnessVersionResponse.from(versions.register(
                new HarnessVersionService.RegisterCommand(
                        request.harnessVersionId(),
                        request.version().strip(),
                        request.parentVersionId(),
                        request.sourceCommit(),
                        request.sourceTreeDigest(),
                        request.dirty(),
                        request.runtimeImageDigest(),
                        request.toolchainDigest(),
                        request.bundleDigest(),
                        request.contractDigest(),
                        request.policyDigest(),
                        request.manifestObjectKey().strip(),
                        request.manifestDigest()),
                actor), null);
    }

    @PostMapping("/harness-versions/{harnessVersionId}:transition")
    HarnessVersionResponse transition(
            @PathVariable @Pattern(regexp = VERSION_ID) String harnessVersionId,
            @Valid @RequestBody VersionTransitionRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return HarnessVersionResponse.from(versions.transition(
                harnessVersionId,
                request.expectedVersion(),
                request.targetStatus(),
                request.reason().strip(),
                actor), null);
    }

    @GetMapping("/harness-rollouts/{rolloutId}")
    HarnessRolloutResponse rollout(
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,80}") String rolloutId,
            @AuthenticationPrincipal Jwt jwt) {
        platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return HarnessRolloutResponse.from(versions.rollout(rolloutId));
    }

    @PutMapping("/harness-rollouts/{rolloutId}/canary")
    HarnessRolloutResponse configureCanary(
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,80}") String rolloutId,
            @Valid @RequestBody ConfigureCanaryRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return HarnessRolloutResponse.from(versions.configureCanary(
                rolloutId,
                request.expectedVersion(),
                request.canaryVersionId(),
                request.canaryPercent(),
                actor));
    }

    @PostMapping("/harness-rollouts/{rolloutId}:promote")
    HarnessRolloutResponse promote(
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,80}") String rolloutId,
            @Valid @RequestBody RolloutTransitionRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return HarnessRolloutResponse.from(versions.promote(
                rolloutId,
                request.expectedVersion(),
                request.canaryVersionId(),
                actor));
    }

    @PostMapping("/harness-rollouts/{rolloutId}:rollback")
    HarnessRolloutResponse rollback(
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,80}") String rolloutId,
            @Valid @RequestBody RollbackRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return HarnessRolloutResponse.from(versions.rollback(
                rolloutId,
                request.expectedVersion(),
                request.targetVersionId(),
                actor));
    }

    record RegisterRequest(
            @NotBlank @Pattern(regexp = VERSION_ID) String harnessVersionId,
            @NotBlank @Size(max = 80) String version,
            @Pattern(regexp = VERSION_ID) String parentVersionId,
            @NotBlank @Pattern(regexp = "[0-9a-f]{40,64}") String sourceCommit,
            @NotBlank @Pattern(regexp = DIGEST) String sourceTreeDigest,
            boolean dirty,
            @NotBlank @Pattern(regexp = "sha256:[0-9a-f]{64}") String runtimeImageDigest,
            @Pattern(regexp = "(?:sha256:)?[0-9a-f]{64}") String toolchainDigest,
            @NotBlank @Pattern(regexp = DIGEST) String bundleDigest,
            @NotBlank @Pattern(regexp = DIGEST) String contractDigest,
            @NotBlank @Pattern(regexp = DIGEST) String policyDigest,
            @NotBlank @Size(max = 1024) String manifestObjectKey,
            @NotBlank @Pattern(regexp = DIGEST) String manifestDigest) {
    }

    record VersionTransitionRequest(
            @Positive long expectedVersion,
            @NotNull HarnessVersion.ReleaseStatus targetStatus,
            @NotBlank @Size(max = 2000) String reason) {
    }

    record ConfigureCanaryRequest(
            @Positive long expectedVersion,
            @Pattern(regexp = VERSION_ID) String canaryVersionId,
            @Min(0) @Max(100) int canaryPercent) {
    }

    record RolloutTransitionRequest(
            @Positive long expectedVersion,
            @NotBlank @Pattern(regexp = VERSION_ID) String canaryVersionId) {
    }

    record RollbackRequest(
            @Positive long expectedVersion,
            @NotBlank @Pattern(regexp = VERSION_ID) String targetVersionId) {
    }

    record HarnessRolloutResponse(
            String rolloutId,
            String stableVersionId,
            String previousStableVersionId,
            String canaryVersionId,
            int canaryPercent,
            long rowVersion,
            Instant updatedAt) {

        static HarnessRolloutResponse from(HarnessRollout rollout) {
            return new HarnessRolloutResponse(
                    rollout.rolloutId(),
                    rollout.stableVersionId(),
                    rollout.previousStableVersionId(),
                    rollout.canaryVersionId(),
                    rollout.canaryPercent(),
                    rollout.rowVersion(),
                    rollout.updatedAt());
        }
    }
}

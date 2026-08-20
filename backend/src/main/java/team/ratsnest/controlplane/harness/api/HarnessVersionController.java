package team.ratsnest.controlplane.harness.api;

import java.time.Instant;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.validation.annotation.Validated;

import jakarta.validation.constraints.Pattern;
import team.ratsnest.controlplane.harness.application.HarnessReleaseRouter;
import team.ratsnest.controlplane.harness.application.HarnessVersionService;
import team.ratsnest.controlplane.harness.domain.model.HarnessVersion;

@RestController
@Validated
@RequestMapping("/api/v1/harness-versions")
public class HarnessVersionController {

    private final HarnessVersionService versions;
    private final HarnessReleaseRouter releaseRouter;

    public HarnessVersionController(
            HarnessVersionService versions,
            HarnessReleaseRouter releaseRouter) {
        this.versions = versions;
        this.releaseRouter = releaseRouter;
    }

    @GetMapping("/current")
    HarnessVersionResponse current() {
        HarnessReleaseRouter.HarnessSelection selection = releaseRouter.stable();
        return HarnessVersionResponse.from(selection.version(), selection.channel());
    }

    @GetMapping("/{harnessVersionId}")
    HarnessVersionResponse get(
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,120}") String harnessVersionId) {
        return HarnessVersionResponse.from(versions.get(harnessVersionId), null);
    }

    record HarnessVersionResponse(
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
            String manifestDigest,
            String releaseStatus,
            boolean attested,
            String channel,
            String transitionReason,
            long rowVersion,
            Instant createdAt,
            Instant activatedAt,
            Instant updatedAt) {

        static HarnessVersionResponse from(HarnessVersion version, String channel) {
            return new HarnessVersionResponse(
                    version.harnessVersionId(),
                    version.version(),
                    version.parentVersionId(),
                    version.sourceCommit(),
                    version.sourceTreeDigest(),
                    version.dirty(),
                    version.runtimeImageDigest(),
                    version.toolchainDigest(),
                    version.bundleDigest(),
                    version.contractDigest(),
                    version.policyDigest(),
                    version.manifestDigest(),
                    version.releaseStatus().name(),
                    version.attested(),
                    channel,
                    version.transitionReason(),
                    version.rowVersion(),
                    version.createdAt(),
                    version.activatedAt(),
                    version.updatedAt());
        }
    }
}

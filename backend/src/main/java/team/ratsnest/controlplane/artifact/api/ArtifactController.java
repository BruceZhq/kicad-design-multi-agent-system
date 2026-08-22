package team.ratsnest.controlplane.artifact.api;

import static team.ratsnest.controlplane.shared.web.ApiHeaders.ORGANIZATION_HEADER;

import java.net.URI;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import team.ratsnest.controlplane.identity.api.JwtIdentity;
import team.ratsnest.controlplane.artifact.application.ArtifactService;
import team.ratsnest.controlplane.artifact.domain.model.Artifact;

@RestController
@RequestMapping("/api/v1")
public class ArtifactController {

    private final ArtifactService artifacts;

    public ArtifactController(ArtifactService artifacts) {
        this.artifacts = artifacts;
    }

    @GetMapping("/runs/{runId}/artifacts")
    ArtifactsResponse list(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @AuthenticationPrincipal Jwt jwt) {
        ArtifactService.ArtifactListing listing = artifacts.list(
                tenantId, runId, JwtIdentity.from(jwt));
        return new ArtifactsResponse(
                runId,
                listing.superseded(),
                listing.artifacts().stream()
                        .map(ArtifactResponse::from)
                        .toList());
    }

    @GetMapping("/artifacts/{artifactId}:download")
    ResponseEntity<Void> download(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID artifactId,
            @AuthenticationPrincipal Jwt jwt) {
        URI location = artifacts.download(
                tenantId, artifactId, JwtIdentity.from(jwt));
        return ResponseEntity.status(HttpStatus.SEE_OTHER)
                .header(HttpHeaders.LOCATION, location.toASCIIString())
                .build();
    }

    record ArtifactsResponse(
            UUID runId,
            boolean superseded,
            List<ArtifactResponse> artifacts) {
    }

    record ArtifactResponse(
            UUID artifactId,
            UUID runId,
            String fileName,
            String kind,
            String mediaType,
            long sizeBytes,
            String sha256,
            Instant createdAt) {

        static ArtifactResponse from(Artifact artifact) {
            return new ArtifactResponse(
                    artifact.artifactId(),
                    artifact.runId(),
                    artifact.name(),
                    artifact.kind(),
                    artifact.mediaType(),
                    artifact.sizeBytes(),
                    artifact.sha256(),
                    artifact.createdAt());
        }
    }
}

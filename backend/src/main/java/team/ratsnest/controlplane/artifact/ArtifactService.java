package team.ratsnest.controlplane.artifact;

import java.net.URI;
import java.time.Duration;
import java.util.List;
import java.util.UUID;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import jakarta.annotation.PreDestroy;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;
import software.amazon.awssdk.services.s3.presigner.model.GetObjectPresignRequest;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.run.RunService;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.TenantAccess;
import team.ratsnest.controlplane.tenancy.TenantContext;

@Service
public final class ArtifactService {

    private final TransactionTemplate transactions;
    private final TenantContext tenantContext;
    private final TenantAccess tenantAccess;
    private final RunService runs;
    private final ArtifactRepository artifacts;
    private final String bucket;
    private final Duration downloadTtl;
    private final S3Presigner presigner;

    public ArtifactService(
            TransactionTemplate transactions,
            TenantContext tenantContext,
            TenantAccess tenantAccess,
            RunService runs,
            ArtifactRepository artifacts,
            @Value("${ratsnest.artifacts.bucket:}") String bucket,
            @Value("${ratsnest.artifacts.region:us-east-1}") String region,
            @Value("${ratsnest.artifacts.endpoint:}") String endpoint,
            @Value("${ratsnest.artifacts.path-style:true}") boolean pathStyle,
            @Value("${ratsnest.artifacts.download-ttl:5m}") Duration downloadTtl) {
        this.transactions = transactions;
        this.tenantContext = tenantContext;
        this.tenantAccess = tenantAccess;
        this.runs = runs;
        this.artifacts = artifacts;
        this.bucket = bucket.strip();
        if (downloadTtl.isNegative() || downloadTtl.isZero()
                || downloadTtl.compareTo(Duration.ofMinutes(15)) > 0) {
            throw new IllegalArgumentException("Artifact download TTL must be between 1 second and 15 minutes");
        }
        this.downloadTtl = downloadTtl;
        S3Presigner.Builder builder = S3Presigner.builder()
                .region(Region.of(region))
                .serviceConfiguration(S3Configuration.builder()
                        .pathStyleAccessEnabled(pathStyle)
                        .build());
        if (!endpoint.isBlank()) {
            builder.endpointOverride(URI.create(endpoint));
        }
        this.presigner = builder.build();
    }

    public ArtifactListing list(
            UUID tenantId,
            UUID runId,
            AuthenticatedActor actor) {
        runs.get(tenantId, runId, actor);
        ArtifactListing result = transactions.execute(status -> {
            tenantContext.activate(tenantId);
            return new ArtifactListing(
                    artifacts.findByRun(tenantId, runId),
                    artifacts.isSuperseded(tenantId, runId));
        });
        return result == null ? new ArtifactListing(List.of(), false) : result;
    }

    public URI download(
            UUID tenantId,
            UUID artifactId,
            AuthenticatedActor actor) {
        Artifact artifact = transactions.execute(status -> {
            tenantAccess.requireMembership(tenantId, actor);
            return artifacts.find(tenantId, artifactId).orElseThrow(this::notFound);
        });
        if (artifact == null) {
            throw notFound();
        }
        runs.authorizeRead(tenantId, artifact.runId(), actor);
        if (bucket.isBlank()) {
            throw new ApiException(
                    "ARTIFACT_STORAGE_UNAVAILABLE",
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "Artifact downloads are not configured.");
        }
        GetObjectRequest objectRequest = GetObjectRequest.builder()
                .bucket(bucket)
                .key(artifact.objectKey())
                .responseContentDisposition("attachment; filename=\"" + artifact.name() + "\"")
                .build();
        return URI.create(presigner.presignGetObject(GetObjectPresignRequest.builder()
                        .signatureDuration(downloadTtl)
                        .getObjectRequest(objectRequest)
                        .build())
                .url()
                .toString());
    }

    @PreDestroy
    void close() {
        presigner.close();
    }

    private ApiException notFound() {
        return new ApiException(
                "ARTIFACT_NOT_FOUND", HttpStatus.NOT_FOUND, "The artifact was not found.");
    }

    public record ArtifactListing(List<Artifact> artifacts, boolean superseded) {

        public ArtifactListing {
            artifacts = List.copyOf(artifacts);
        }
    }
}

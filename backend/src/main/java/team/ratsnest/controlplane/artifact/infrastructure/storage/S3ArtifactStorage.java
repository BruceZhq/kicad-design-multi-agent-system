package team.ratsnest.controlplane.artifact.infrastructure.storage;

import java.net.URI;
import java.time.Duration;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import jakarta.annotation.PreDestroy;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;
import software.amazon.awssdk.services.s3.presigner.model.GetObjectPresignRequest;
import team.ratsnest.controlplane.artifact.domain.model.Artifact;
import team.ratsnest.controlplane.artifact.domain.port.ArtifactStorage;

@Component
public class S3ArtifactStorage implements ArtifactStorage {

    private final String bucket;
    private final Duration downloadTtl;
    private final S3Presigner presigner;

    public S3ArtifactStorage(
            @Value("${ratsnest.artifacts.bucket:}") String bucket,
            @Value("${ratsnest.artifacts.region:us-east-1}") String region,
            @Value("${ratsnest.artifacts.endpoint:}") String endpoint,
            @Value("${ratsnest.artifacts.path-style:true}") boolean pathStyle,
            @Value("${ratsnest.artifacts.download-ttl:5m}") Duration downloadTtl) {
        this.bucket = bucket.strip();
        if (downloadTtl.isNegative() || downloadTtl.isZero()
                || downloadTtl.compareTo(Duration.ofMinutes(15)) > 0) {
            throw new IllegalArgumentException(
                    "Artifact download TTL must be between 1 second and 15 minutes");
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

    @Override
    public boolean available() {
        return !bucket.isBlank();
    }

    @Override
    public URI downloadUrl(Artifact artifact) {
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
}

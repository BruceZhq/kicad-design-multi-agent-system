package dev.ratsnest.artifact;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "run_artifacts", uniqueConstraints =
        @UniqueConstraint(name = "uk_artifact_run_kind",
                columnNames = {"run_id", "kind"}))
public class RunArtifact {

    @Id
    private String id;

    @Column(name = "run_id", nullable = false)
    private String runId;

    @Column(name = "organization_id")
    private String organizationId;

    @Column(nullable = false, length = 40)
    private String kind;

    @Column(nullable = false, length = 240)
    private String filename;

    @Column(nullable = false, length = 120)
    private String contentType;

    @Column(nullable = false, unique = true, length = 600)
    private String storageKey;

    @Column(nullable = false)
    private long sizeBytes;

    @Column(nullable = false, length = 64)
    private String sha256;

    @Column(nullable = false)
    private Instant createdAt;

    public static RunArtifact create(String runId, String organizationId,
                                     String kind, String filename,
                                     String contentType, String storageKey,
                                     long sizeBytes, String sha256) {
        RunArtifact artifact = new RunArtifact();
        artifact.id = UUID.randomUUID().toString();
        artifact.runId = runId;
        artifact.organizationId = organizationId;
        artifact.kind = kind;
        artifact.filename = filename;
        artifact.contentType = contentType;
        artifact.storageKey = storageKey;
        artifact.sizeBytes = sizeBytes;
        artifact.sha256 = sha256;
        artifact.createdAt = Instant.now();
        return artifact;
    }

    public void replace(String filename, String contentType, long sizeBytes,
                        String sha256) {
        this.filename = filename;
        this.contentType = contentType;
        this.sizeBytes = sizeBytes;
        this.sha256 = sha256;
        this.createdAt = Instant.now();
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getRunId() { return runId; }
    public void setRunId(String runId) { this.runId = runId; }
    public String getOrganizationId() { return organizationId; }
    public void setOrganizationId(String organizationId) { this.organizationId = organizationId; }
    public String getKind() { return kind; }
    public void setKind(String kind) { this.kind = kind; }
    public String getFilename() { return filename; }
    public void setFilename(String filename) { this.filename = filename; }
    public String getContentType() { return contentType; }
    public void setContentType(String contentType) { this.contentType = contentType; }
    public String getStorageKey() { return storageKey; }
    public void setStorageKey(String storageKey) { this.storageKey = storageKey; }
    public long getSizeBytes() { return sizeBytes; }
    public void setSizeBytes(long sizeBytes) { this.sizeBytes = sizeBytes; }
    public String getSha256() { return sha256; }
    public void setSha256(String sha256) { this.sha256 = sha256; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
}

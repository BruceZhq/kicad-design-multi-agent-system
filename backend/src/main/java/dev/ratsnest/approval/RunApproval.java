package dev.ratsnest.approval;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import jakarta.persistence.Version;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "run_approvals", uniqueConstraints =
        @UniqueConstraint(name = "uk_approval_run_type",
                columnNames = {"run_id", "type"}))
public class RunApproval {

    @Id
    private String id;

    @Column(name = "run_id", nullable = false)
    private String runId;

    @Column(name = "organization_id")
    private String organizationId;

    @Column(nullable = false, length = 40)
    private String type;

    @Column(nullable = false, length = 20)
    private String status;

    @Column(nullable = false, length = 64)
    private String subjectSha256;

    @Column(nullable = false)
    private Instant requestedAt;

    private Instant decidedAt;
    private String decidedBy;

    @Column(length = 1000)
    private String comment;

    @Version
    private long version;

    public static RunApproval pending(String runId, String organizationId,
                                      String type, String subjectSha256) {
        RunApproval approval = new RunApproval();
        approval.id = UUID.randomUUID().toString();
        approval.runId = runId;
        approval.organizationId = organizationId;
        approval.type = type;
        approval.status = "pending";
        approval.subjectSha256 = subjectSha256;
        approval.requestedAt = Instant.now();
        return approval;
    }

    public void decide(String decision, String actor, String comment) {
        status = decision;
        decidedBy = actor;
        this.comment = comment;
        decidedAt = Instant.now();
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getRunId() { return runId; }
    public void setRunId(String runId) { this.runId = runId; }
    public String getOrganizationId() { return organizationId; }
    public void setOrganizationId(String organizationId) { this.organizationId = organizationId; }
    public String getType() { return type; }
    public void setType(String type) { this.type = type; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public String getSubjectSha256() { return subjectSha256; }
    public void setSubjectSha256(String subjectSha256) { this.subjectSha256 = subjectSha256; }
    public Instant getRequestedAt() { return requestedAt; }
    public void setRequestedAt(Instant requestedAt) { this.requestedAt = requestedAt; }
    public Instant getDecidedAt() { return decidedAt; }
    public void setDecidedAt(Instant decidedAt) { this.decidedAt = decidedAt; }
    public String getDecidedBy() { return decidedBy; }
    public void setDecidedBy(String decidedBy) { this.decidedBy = decidedBy; }
    public String getComment() { return comment; }
    public void setComment(String comment) { this.comment = comment; }
    public long getVersion() { return version; }
    public void setVersion(long version) { this.version = version; }
}

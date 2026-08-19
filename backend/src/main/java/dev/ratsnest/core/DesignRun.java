package dev.ratsnest.core;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Lob;
import jakarta.persistence.PrePersist;
import jakarta.persistence.PreUpdate;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import jakarta.persistence.Version;

import dev.ratsnest.tenant.HardwareProject;

import java.time.Instant;
import java.util.UUID;

/** Governance record of one design run. Payloads stay opaque JSON (typed by
 *  the shared contract schemas) — no domain logic in the control plane. */
@Entity
@Table(name = "design_runs", uniqueConstraints =
        @UniqueConstraint(name = "uk_run_org_idempotency",
                columnNames = {"organization_id", "idempotency_key"}))
public class DesignRun {

    @Id
    private String id;

    private String kind;            // "fix" | "design"
    private String backend;         // "template" | "crew" | "mcp" (design runs)
    private String owner;           // username that created the run (from JWT)
    private String ownerUserId;
    @Column(name = "organization_id")
    private String organizationId;
    private String workspaceId;
    private String projectId;
    @Column(name = "idempotency_key")
    private String idempotencyKey;
    private String requirement;     // natural-language requirement (design runs)
    private String projectDir;
    private int maxIterations;
    private String status;          // dispatched|running|converged|escalated|failed
    private String pythonRunId;     // run_id assigned by the agent runtime
    private String strategyVersionId;
    private Double initialScore;
    private Double finalScore;
    private Instant createdAt;
    private Instant updatedAt;
    private Instant startedAt;
    private Instant finishedAt;
    private int attempt;
    private String failureMessage;
    private String resultSha256;
    private String releaseStatus;
    private String dispatchPhase;
    private String planContractVersion;
    private String planSha256;
    private Instant planCreatedAt;
    private Instant planApprovedAt;

    @Lob
    @Column(columnDefinition = "TEXT")
    private String planJson;

    @Version
    private long version;

    @Lob
    @Column(columnDefinition = "CLOB")
    private String resultJson;      // full RunRecord contract payload

    public static DesignRun create(String projectDir, int maxIterations) {
        DesignRun run = new DesignRun();
        run.id = UUID.randomUUID().toString();
        run.kind = "fix";
        run.projectDir = projectDir;
        run.maxIterations = maxIterations;
        run.status = "dispatched";
        run.createdAt = Instant.now();
        run.updatedAt = run.createdAt;
        run.releaseStatus = "draft";
        run.dispatchPhase = "execute";
        return run;
    }

    public static DesignRun createDesign(String requirement, String projectDir,
                                         int maxIterations, String backend) {
        DesignRun run = create(projectDir, maxIterations);
        run.kind = "design";
        run.requirement = requirement;
        run.backend = backend;
        run.status = "planning";
        run.dispatchPhase = "plan";
        return run;
    }

    public void assignProject(HardwareProject project, String userId) {
        this.ownerUserId = userId;
        if (project != null) {
            this.organizationId = project.getOrganizationId();
            this.workspaceId = project.getWorkspaceId();
            this.projectId = project.getId();
        }
    }

    @PrePersist
    @PreUpdate
    void touch() {
        updatedAt = Instant.now();
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getKind() { return kind; }
    public void setKind(String kind) { this.kind = kind; }
    public String getBackend() { return backend; }
    public void setBackend(String backend) { this.backend = backend; }
    public String getOwner() { return owner; }
    public void setOwner(String owner) { this.owner = owner; }
    public String getOwnerUserId() { return ownerUserId; }
    public void setOwnerUserId(String ownerUserId) { this.ownerUserId = ownerUserId; }
    public String getOrganizationId() { return organizationId; }
    public void setOrganizationId(String organizationId) { this.organizationId = organizationId; }
    public String getWorkspaceId() { return workspaceId; }
    public void setWorkspaceId(String workspaceId) { this.workspaceId = workspaceId; }
    public String getProjectId() { return projectId; }
    public void setProjectId(String projectId) { this.projectId = projectId; }
    @com.fasterxml.jackson.annotation.JsonIgnore
    public String getIdempotencyKey() { return idempotencyKey; }
    public void setIdempotencyKey(String idempotencyKey) { this.idempotencyKey = idempotencyKey; }
    public String getRequirement() { return requirement; }
    public void setRequirement(String requirement) { this.requirement = requirement; }
    public String getProjectDir() { return projectDir; }
    public void setProjectDir(String projectDir) { this.projectDir = projectDir; }
    public int getMaxIterations() { return maxIterations; }
    public void setMaxIterations(int maxIterations) { this.maxIterations = maxIterations; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public String getPythonRunId() { return pythonRunId; }
    public void setPythonRunId(String pythonRunId) { this.pythonRunId = pythonRunId; }
    public String getStrategyVersionId() { return strategyVersionId; }
    public void setStrategyVersionId(String strategyVersionId) { this.strategyVersionId = strategyVersionId; }
    public Double getInitialScore() { return initialScore; }
    public void setInitialScore(Double initialScore) { this.initialScore = initialScore; }
    public Double getFinalScore() { return finalScore; }
    public void setFinalScore(Double finalScore) { this.finalScore = finalScore; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public void setUpdatedAt(Instant updatedAt) { this.updatedAt = updatedAt; }
    public Instant getStartedAt() { return startedAt; }
    public void setStartedAt(Instant startedAt) { this.startedAt = startedAt; }
    public Instant getFinishedAt() { return finishedAt; }
    public void setFinishedAt(Instant finishedAt) { this.finishedAt = finishedAt; }
    public int getAttempt() { return attempt; }
    public void setAttempt(int attempt) { this.attempt = attempt; }
    public String getFailureMessage() { return failureMessage; }
    public void setFailureMessage(String failureMessage) { this.failureMessage = failureMessage; }
    public String getResultSha256() { return resultSha256; }
    public void setResultSha256(String resultSha256) { this.resultSha256 = resultSha256; }
    public String getReleaseStatus() { return releaseStatus; }
    public void setReleaseStatus(String releaseStatus) { this.releaseStatus = releaseStatus; }
    public String getDispatchPhase() { return dispatchPhase; }
    public void setDispatchPhase(String dispatchPhase) { this.dispatchPhase = dispatchPhase; }
    public String getPlanContractVersion() { return planContractVersion; }
    public void setPlanContractVersion(String planContractVersion) { this.planContractVersion = planContractVersion; }
    public String getPlanSha256() { return planSha256; }
    public void setPlanSha256(String planSha256) { this.planSha256 = planSha256; }
    public Instant getPlanCreatedAt() { return planCreatedAt; }
    public void setPlanCreatedAt(Instant planCreatedAt) { this.planCreatedAt = planCreatedAt; }
    public Instant getPlanApprovedAt() { return planApprovedAt; }
    public void setPlanApprovedAt(Instant planApprovedAt) { this.planApprovedAt = planApprovedAt; }
    @com.fasterxml.jackson.annotation.JsonIgnore
    public String getPlanJson() { return planJson; }
    public void setPlanJson(String planJson) { this.planJson = planJson; }
    public long getVersion() { return version; }
    public void setVersion(long version) { this.version = version; }
    public String getResultJson() { return resultJson; }
    public void setResultJson(String resultJson) { this.resultJson = resultJson; }
}

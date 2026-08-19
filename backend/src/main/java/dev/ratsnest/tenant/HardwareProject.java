package dev.ratsnest.tenant;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "hardware_projects", uniqueConstraints =
        @UniqueConstraint(name = "uk_project_workspace_name",
                columnNames = {"workspace_id", "name"}))
public class HardwareProject {

    @Id
    private String id;

    @Column(name = "organization_id", nullable = false)
    private String organizationId;

    @Column(name = "workspace_id", nullable = false)
    private String workspaceId;

    @Column(nullable = false, length = 160)
    private String name;

    @Column(length = 1000)
    private String description;

    @Column(nullable = false)
    private String createdBy;

    @Column(nullable = false)
    private Instant createdAt;

    public static HardwareProject create(String organizationId,
                                         String workspaceId, String name,
                                         String description,
                                         String createdBy) {
        HardwareProject project = new HardwareProject();
        project.id = UUID.randomUUID().toString();
        project.organizationId = organizationId;
        project.workspaceId = workspaceId;
        project.name = name;
        project.description = description;
        project.createdBy = createdBy;
        project.createdAt = Instant.now();
        return project;
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getOrganizationId() { return organizationId; }
    public void setOrganizationId(String organizationId) { this.organizationId = organizationId; }
    public String getWorkspaceId() { return workspaceId; }
    public void setWorkspaceId(String workspaceId) { this.workspaceId = workspaceId; }
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public String getDescription() { return description; }
    public void setDescription(String description) { this.description = description; }
    public String getCreatedBy() { return createdBy; }
    public void setCreatedBy(String createdBy) { this.createdBy = createdBy; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
}

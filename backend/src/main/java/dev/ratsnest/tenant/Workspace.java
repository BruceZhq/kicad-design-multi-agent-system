package dev.ratsnest.tenant;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "tenant_workspaces", uniqueConstraints =
        @UniqueConstraint(name = "uk_workspace_org_slug",
                columnNames = {"organization_id", "slug"}))
public class Workspace {

    @Id
    private String id;

    @Column(name = "organization_id", nullable = false)
    private String organizationId;

    @Column(nullable = false, length = 120)
    private String name;

    @Column(nullable = false, length = 80)
    private String slug;

    @Column(nullable = false)
    private String createdBy;

    @Column(nullable = false)
    private Instant createdAt;

    public static Workspace create(String organizationId, String name,
                                   String slug, String createdBy) {
        Workspace workspace = new Workspace();
        workspace.id = UUID.randomUUID().toString();
        workspace.organizationId = organizationId;
        workspace.name = name;
        workspace.slug = slug;
        workspace.createdBy = createdBy;
        workspace.createdAt = Instant.now();
        return workspace;
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getOrganizationId() { return organizationId; }
    public void setOrganizationId(String organizationId) { this.organizationId = organizationId; }
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public String getSlug() { return slug; }
    public void setSlug(String slug) { this.slug = slug; }
    public String getCreatedBy() { return createdBy; }
    public void setCreatedBy(String createdBy) { this.createdBy = createdBy; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
}

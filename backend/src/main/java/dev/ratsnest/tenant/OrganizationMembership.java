package dev.ratsnest.tenant;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "organization_memberships", uniqueConstraints =
        @UniqueConstraint(name = "uk_membership_org_user",
                columnNames = {"organization_id", "user_id"}))
public class OrganizationMembership {

    @Id
    private String id;

    @Column(name = "organization_id", nullable = false)
    private String organizationId;

    @Column(name = "user_id", nullable = false)
    private String userId;

    @Column(nullable = false, length = 24)
    private String role;

    @Column(nullable = false)
    private Instant createdAt;

    public static OrganizationMembership create(String organizationId,
                                                  String userId,
                                                  String role) {
        OrganizationMembership membership = new OrganizationMembership();
        membership.id = UUID.randomUUID().toString();
        membership.organizationId = organizationId;
        membership.userId = userId;
        membership.role = role;
        membership.createdAt = Instant.now();
        return membership;
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getOrganizationId() { return organizationId; }
    public void setOrganizationId(String organizationId) { this.organizationId = organizationId; }
    public String getUserId() { return userId; }
    public void setUserId(String userId) { this.userId = userId; }
    public String getRole() { return role; }
    public void setRole(String role) { this.role = role; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
}

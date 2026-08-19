package dev.ratsnest.tenant;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface OrganizationMembershipRepository
        extends JpaRepository<OrganizationMembership, String> {
    List<OrganizationMembership> findByUserIdOrderByCreatedAtAsc(String userId);
    Optional<OrganizationMembership> findByOrganizationIdAndUserId(
            String organizationId, String userId);
    boolean existsByOrganizationIdAndUserId(String organizationId, String userId);
}

package dev.ratsnest.tenant;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Collection;
import java.util.List;

public interface OrganizationRepository extends JpaRepository<Organization, String> {
    List<Organization> findByIdInOrderByNameAsc(Collection<String> ids);
    boolean existsBySlug(String slug);
}

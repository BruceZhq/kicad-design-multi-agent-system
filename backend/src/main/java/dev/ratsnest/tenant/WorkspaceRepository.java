package dev.ratsnest.tenant;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Collection;
import java.util.List;

public interface WorkspaceRepository extends JpaRepository<Workspace, String> {
    List<Workspace> findByOrganizationIdOrderByNameAsc(String organizationId);
    List<Workspace> findByOrganizationIdInOrderByNameAsc(
            Collection<String> organizationIds);
}

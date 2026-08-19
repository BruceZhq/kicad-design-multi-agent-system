package dev.ratsnest.tenant;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Collection;
import java.util.List;

public interface HardwareProjectRepository extends JpaRepository<HardwareProject, String> {
    List<HardwareProject> findByWorkspaceIdOrderByNameAsc(String workspaceId);
    List<HardwareProject> findByOrganizationIdInOrderByCreatedAtAsc(
            Collection<String> organizationIds);
}

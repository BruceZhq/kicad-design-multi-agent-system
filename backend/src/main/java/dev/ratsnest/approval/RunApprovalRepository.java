package dev.ratsnest.approval;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;
import java.util.List;

public interface RunApprovalRepository extends JpaRepository<RunApproval, String> {
    Optional<RunApproval> findByRunIdAndType(String runId, String type);
    List<RunApproval> findByRunIdOrderByRequestedAtAsc(String runId);
}

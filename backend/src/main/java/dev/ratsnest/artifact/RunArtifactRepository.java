package dev.ratsnest.artifact;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface RunArtifactRepository extends JpaRepository<RunArtifact, String> {
    Optional<RunArtifact> findByRunIdAndKind(String runId, String kind);
    List<RunArtifact> findByRunIdOrderByCreatedAtAsc(String runId);
}

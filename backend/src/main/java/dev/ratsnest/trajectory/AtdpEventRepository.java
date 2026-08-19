package dev.ratsnest.trajectory;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface AtdpEventRepository extends JpaRepository<AtdpEvent, Long> {
    List<AtdpEvent> findByRunIdOrderByStepAsc(String runId);
}

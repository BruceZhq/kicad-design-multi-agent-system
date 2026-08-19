package dev.ratsnest.core;

import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

public interface DispatchOutboxRepository
        extends JpaRepository<DispatchOutbox, String> {

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select event from DispatchOutbox event where "
            + "event.status = 'pending' and event.availableAt <= :now "
            + "order by event.createdAt asc")
    List<DispatchOutbox> lockReady(@Param("now") Instant now,
                                   Pageable pageable);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select event from DispatchOutbox event where event.runId = :runId")
    Optional<DispatchOutbox> findByRunId(@Param("runId") String runId);
}

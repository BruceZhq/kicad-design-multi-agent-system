package dev.ratsnest.core;

import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.Optional;
import jakarta.persistence.LockModeType;

public interface DesignRunRepository extends JpaRepository<DesignRun, String> {
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select run from DesignRun run where run.id = :id")
    Optional<DesignRun> findLockedById(@Param("id") String id);

    Page<DesignRun> findByOwner(String owner, Pageable pageable);

    Optional<DesignRun> findByOrganizationIdAndIdempotencyKey(
            String organizationId, String idempotencyKey);

    Optional<DesignRun> findByOwnerAndIdempotencyKey(
            String owner, String idempotencyKey);

    @Query("select run from DesignRun run where "
            + "run.organizationId in :organizationIds or "
            + "(run.organizationId is null and run.owner = :owner)")
    Page<DesignRun> findVisibleToUser(
            @Param("organizationIds") Collection<String> organizationIds,
            @Param("owner") String owner,
            Pageable pageable);
}

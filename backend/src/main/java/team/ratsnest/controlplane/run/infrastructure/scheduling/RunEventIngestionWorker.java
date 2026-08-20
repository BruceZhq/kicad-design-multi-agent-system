package team.ratsnest.controlplane.run.infrastructure.scheduling;

import java.net.InetAddress;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import jakarta.annotation.PreDestroy;
import team.ratsnest.controlplane.run.application.RunEventIngestionService;
import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore;
import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore.IngestionClaim;

@Component
@ConditionalOnProperty(
        name = "ratsnest.run-event-ingestion.enabled",
        havingValue = "true",
        matchIfMissing = true)
final class RunEventIngestionWorker {

    private static final Logger logger = LoggerFactory.getLogger(RunEventIngestionWorker.class);

    private final RunEventIngestionStore ingestion;
    private final RunEventIngestionService service;
    private final int batchSize;
    private final int activeDelaySeconds;
    private final Duration timeBudget;
    private final Duration attemptTimeout;
    private final String workerId;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();

    RunEventIngestionWorker(
            RunEventIngestionStore ingestion,
            RunEventIngestionService service,
            @Value("${ratsnest.run-event-ingestion.batch-size:10}") int batchSize,
            @Value("${ratsnest.run-event-ingestion.active-delay:5s}") Duration activeDelay,
            @Value("${ratsnest.run-event-ingestion.time-budget:30s}") Duration timeBudget,
            @Value("${ratsnest.run-event-ingestion.attempt-timeout:20s}") Duration attemptTimeout) {
        this.ingestion = ingestion;
        this.service = service;
        this.batchSize = Math.max(1, Math.min(batchSize, 100));
        this.activeDelaySeconds = seconds(activeDelay);
        this.timeBudget = positive(timeBudget, "time budget");
        this.attemptTimeout = positive(attemptTimeout, "attempt timeout");
        this.workerId = hostName() + "-event-ingest-" + UUID.randomUUID();
    }

    @Scheduled(fixedDelayString = "${ratsnest.run-event-ingestion.poll-delay:2s}")
    void ingestPending() {
        List<IngestionClaim> claims = ingestion.claim(workerId, batchSize);
        Instant deadline = Instant.now().plus(timeBudget);
        for (int index = 0; index < claims.size(); index++) {
            IngestionClaim claim = claims.get(index);
            Duration remaining = Duration.between(Instant.now(), deadline);
            if (remaining.isZero() || remaining.isNegative()) {
                releaseRemaining(claims, index);
                return;
            }
            long timeoutMillis = Math.max(
                    1,
                    Math.min(remaining.toMillis(), attemptTimeout.toMillis()));
            Future<Boolean> attempt = executor.submit(
                    () -> service.ingest(claim, workerId));
            int delay = activeDelaySeconds;
            try {
                boolean active = attempt.get(timeoutMillis, TimeUnit.MILLISECONDS);
                delay = active ? activeDelaySeconds : 1;
            } catch (InterruptedException exception) {
                Thread.currentThread().interrupt();
                attempt.cancel(true);
                ingestion.release(claim, workerId, 1);
                releaseRemaining(claims, index + 1);
                return;
            } catch (TimeoutException exception) {
                attempt.cancel(true);
                delay = retryDelay(claim.attempts());
                logger.warn("Run event ingestion timed out runId={}", claim.runId());
            } catch (Exception exception) {
                delay = retryDelay(claim.attempts());
                Throwable cause = exception.getCause() == null
                        ? exception
                        : exception.getCause();
                logger.error(
                        "Run event ingestion failed closed runId={} cursor={} cause={}",
                        claim.runId(),
                        claim.lastEventSequence(),
                        cause.getClass().getSimpleName());
            }
            if (!ingestion.release(claim, workerId, delay)) {
                logger.warn("Run event ingestion release lost runId={}", claim.runId());
            }
        }
    }

    @PreDestroy
    void close() {
        executor.shutdownNow();
    }

    private void releaseRemaining(List<IngestionClaim> claims, int startIndex) {
        for (int index = startIndex; index < claims.size(); index++) {
            ingestion.release(claims.get(index), workerId, 1);
        }
    }

    private int retryDelay(int attempts) {
        int exponent = Math.min(Math.max(attempts - 1, 0), 8);
        return Math.min(1 << exponent, 300);
    }

    private static int seconds(Duration value) {
        return (int) Math.max(1, Math.min(value.toSeconds(), 3600));
    }

    private static Duration positive(Duration value, String name) {
        if (value.isZero() || value.isNegative()) {
            throw new IllegalArgumentException(
                    "Run event ingestion " + name + " must be positive");
        }
        return value;
    }

    private static String hostName() {
        try {
            return InetAddress.getLocalHost().getHostName();
        } catch (Exception ignored) {
            return "control-plane";
        }
    }
}

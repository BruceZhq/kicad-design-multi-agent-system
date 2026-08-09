package team.ratsnest.controlplane.run;

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

@Component
@ConditionalOnProperty(name = "ratsnest.run-reconciliation.enabled", havingValue = "true")
class RunReconciliationWorker {

    private static final Logger logger = LoggerFactory.getLogger(RunReconciliationWorker.class);

    private final RunRepository runs;
    private final RunService runService;
    private final int batchSize;
    private final int activeDelaySeconds;
    private final Duration timeBudget;
    private final Duration attemptTimeout;
    private final String workerId;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();

    RunReconciliationWorker(
            RunRepository runs,
            RunService runService,
            @Value("${ratsnest.run-reconciliation.batch-size:10}") int batchSize,
            @Value("${ratsnest.run-reconciliation.active-delay:15s}") Duration activeDelay,
            @Value("${ratsnest.run-reconciliation.time-budget:30s}") Duration timeBudget,
            @Value("${ratsnest.run-reconciliation.attempt-timeout:20s}") Duration attemptTimeout) {
        this.runs = runs;
        this.runService = runService;
        this.batchSize = Math.max(1, Math.min(batchSize, 100));
        this.activeDelaySeconds = seconds(activeDelay);
        this.timeBudget = positive(timeBudget, "time budget");
        this.attemptTimeout = positive(attemptTimeout, "attempt timeout");
        this.workerId = hostName() + "-" + UUID.randomUUID();
    }

    @Scheduled(fixedDelayString = "${ratsnest.run-reconciliation.poll-delay:5s}")
    void reconcilePending() {
        List<RunRepository.ReconciliationClaim> claims =
                runs.claimForReconciliation(workerId, batchSize);
        Instant deadline = Instant.now().plus(timeBudget);
        for (int index = 0; index < claims.size(); index++) {
            RunRepository.ReconciliationClaim claim = claims.get(index);
            Duration remaining = Duration.between(Instant.now(), deadline);
            if (remaining.isZero() || remaining.isNegative()) {
                releaseRemaining(claims, index);
                return;
            }
            long timeoutMillis = Math.max(
                    1,
                    Math.min(remaining.toMillis(), attemptTimeout.toMillis()));
            Future<Boolean> attempt = executor.submit(
                    () -> runService.reconcile(claim.tenantId(), claim.runId()));
            int delay = activeDelaySeconds;
            try {
                boolean stillActive = attempt.get(timeoutMillis, TimeUnit.MILLISECONDS);
                delay = stillActive ? activeDelaySeconds : 1;
            } catch (InterruptedException exception) {
                Thread.currentThread().interrupt();
                attempt.cancel(true);
                runs.releaseReconciliation(claim, workerId, 1);
                releaseRemaining(claims, index + 1);
                return;
            } catch (TimeoutException exception) {
                attempt.cancel(true);
                delay = retryDelay(claim.attempts());
                logger.warn("Run reconciliation timed out runId={}", claim.runId());
            } catch (Exception exception) {
                delay = retryDelay(claim.attempts());
                logger.warn(
                        "Run reconciliation deferred runId={} cause={}",
                        claim.runId(),
                        exception.getClass().getSimpleName());
            }
            if (!runs.releaseReconciliation(claim, workerId, delay)) {
                logger.warn("Run reconciliation release lost runId={}", claim.runId());
            }
        }
    }

    @PreDestroy
    void close() {
        executor.shutdownNow();
    }

    private void releaseRemaining(
            List<RunRepository.ReconciliationClaim> claims,
            int startIndex) {
        for (int index = startIndex; index < claims.size(); index++) {
            runs.releaseReconciliation(claims.get(index), workerId, 1);
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
            throw new IllegalArgumentException("Run reconciliation " + name + " must be positive");
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

package dev.ratsnest.core;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

@Service
public class RunSubmissionService {

    private final DesignRunRepository runs;
    private final DispatchOutboxRepository outbox;
    private final RunDispatchService dispatch;

    @Value("${ratsnest.dispatch:local}")
    private String dispatchMode;

    public RunSubmissionService(DesignRunRepository runs,
                                DispatchOutboxRepository outbox,
                                RunDispatchService dispatch) {
        this.runs = runs;
        this.outbox = outbox;
        this.dispatch = dispatch;
    }

    @Transactional
    public DesignRun submit(DesignRun run) {
        DesignRun saved = runs.save(run);
        if ("kafka".equalsIgnoreCase(dispatchMode)) {
            queueOutbox(saved.getId());
        } else {
            dispatchAfterCommit(saved.getId());
        }
        return saved;
    }

    @Transactional
    public DesignRun scheduleExecution(DesignRun run) {
        if (!"design".equals(run.getKind())
                || run.getPlanJson() == null || run.getPlanSha256() == null) {
            throw new IllegalStateException(
                    "design execution requires a persisted approved plan");
        }
        run.setDispatchPhase("execute");
        run.setStatus("queued");
        DesignRun saved = runs.save(run);
        if ("kafka".equalsIgnoreCase(dispatchMode)) {
            queueOutbox(saved.getId());
        } else {
            dispatchAfterCommit(saved.getId());
        }
        return saved;
    }

    private void queueOutbox(String runId) {
        DispatchOutbox event = outbox.findByRunId(runId)
                .orElseGet(() -> DispatchOutbox.pending(runId));
        event.requeue();
        outbox.save(event);
    }

    private void dispatchAfterCommit(String runId) {
        TransactionSynchronizationManager.registerSynchronization(
                new TransactionSynchronization() {
                    @Override
                    public void afterCommit() {
                        dispatch.dispatchLocal(runId);
                    }
                });
    }
}

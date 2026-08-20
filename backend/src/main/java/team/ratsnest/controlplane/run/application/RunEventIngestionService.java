package team.ratsnest.controlplane.run.application;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Flow;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.EventSubscription;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore;
import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore.IngestionClaim;
import team.ratsnest.controlplane.run.domain.port.RunStore;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

/**
 * Drains runtime events to a durable control-plane cursor independently of any
 * browser SSE connection.
 */
@Service
public final class RunEventIngestionService {

    private final TransactionTemplate transactions;
    private final TenantContext tenantContext;
    private final RunStore runs;
    private final RunEventIngestionStore ingestion;
    private final AgentRuntimeGateway runtime;
    private final RunAccessSupport access;
    private final RunLifecycleService lifecycle;

    public RunEventIngestionService(
            TransactionTemplate transactions,
            TenantContext tenantContext,
            RunStore runs,
            RunEventIngestionStore ingestion,
            AgentRuntimeGateway runtime,
            RunAccessSupport access,
            RunLifecycleService lifecycle) {
        this.transactions = transactions;
        this.tenantContext = tenantContext;
        this.runs = runs;
        this.ingestion = ingestion;
        this.runtime = runtime;
        this.access = access;
        this.lifecycle = lifecycle;
    }

    /**
     * @return true while the runtime is active and should be checked again soon
     */
    public boolean ingest(IngestionClaim claim, String workerId) {
        Run run = transactions.execute(status -> {
            tenantContext.activate(claim.tenantId());
            return runs.find(claim.tenantId(), claim.runId()).orElse(null);
        });
        if (run == null) {
            return false;
        }

        RuntimeRun snapshot = runtime.getRun(access.reference(run));
        lifecycle.updateFromRuntime(run, snapshot);
        long highWater = snapshot.newestEventId() == null ? 0 : snapshot.newestEventId();
        if (highWater > claim.lastEventSequence()) {
            drain(run, claim, workerId, highWater);
        }
        return snapshot.state() == RunState.QUEUED || snapshot.state() == RunState.RUNNING;
    }

    private void drain(
            Run run,
            IngestionClaim claim,
            String workerId,
            long highWater) {
        AtomicLong cursor = new AtomicLong(claim.lastEventSequence());
        AtomicReference<Flow.Subscription> subscription = new AtomicReference<>();
        CompletableFuture<Void> completion = new CompletableFuture<>();
        Flow.Publisher<RuntimeEvent> source = runtime.subscribeEvents(
                new EventSubscription(access.command(run), claim.lastEventSequence()));

        try {
            source.subscribe(new Flow.Subscriber<>() {
                @Override
                public void onSubscribe(Flow.Subscription value) {
                    if (!subscription.compareAndSet(null, value)) {
                        value.cancel();
                        return;
                    }
                    value.request(1);
                }

                @Override
                public void onNext(RuntimeEvent event) {
                    try {
                        Long eventSequence = event.eventId();
                        if (eventSequence == null) {
                            requestNext();
                            return;
                        }
                        long current = cursor.get();
                        if (eventSequence <= current) {
                            requestNext();
                            return;
                        }
                        if (eventSequence > highWater) {
                            throw new IllegalStateException(
                                    "Runtime event advanced beyond the authoritative ingestion high-water");
                        }
                        if (replayFailure(event)) {
                            throw new IllegalStateException(
                                    "Runtime event ingestion received an explicit replay-gap signal");
                        }
                        lifecycle.ingestRuntimeEvent(run, event);
                        if (!advance(claim, workerId, current, eventSequence)) {
                            throw new IllegalStateException(
                                    "Run event ingestion cursor lease was lost");
                        }
                        cursor.set(eventSequence);
                        if (eventSequence >= highWater) {
                            cancel();
                            completion.complete(null);
                        } else {
                            requestNext();
                        }
                    } catch (Throwable failure) {
                        cancel();
                        completion.completeExceptionally(failure);
                    }
                }

                @Override
                public void onError(Throwable failure) {
                    completion.completeExceptionally(failure);
                }

                @Override
                public void onComplete() {
                    if (cursor.get() >= highWater) {
                        completion.complete(null);
                    } else {
                        completion.completeExceptionally(new IllegalStateException(
                                "Runtime event stream ended before the ingestion high-water"));
                    }
                }

                private void requestNext() {
                    Flow.Subscription current = subscription.get();
                    if (current != null) current.request(1);
                }

                private void cancel() {
                    Flow.Subscription current = subscription.get();
                    if (current != null) current.cancel();
                }
            });
            completion.get();
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            Flow.Subscription current = subscription.get();
            if (current != null) current.cancel();
            throw new IllegalStateException("Run event ingestion was interrupted", exception);
        } catch (ExecutionException exception) {
            Throwable cause = exception.getCause();
            if (cause instanceof RuntimeException runtimeFailure) {
                throw runtimeFailure;
            }
            throw new IllegalStateException("Run event ingestion failed", cause);
        } catch (RuntimeException exception) {
            Flow.Subscription current = subscription.get();
            if (current != null) current.cancel();
            throw exception;
        }
    }

    private boolean advance(
            IngestionClaim claim,
            String workerId,
            long expectedEventSequence,
            long nextEventSequence) {
        return Boolean.TRUE.equals(transactions.execute(status -> {
            tenantContext.activate(claim.tenantId());
            return ingestion.advance(
                    claim, workerId, expectedEventSequence, nextEventSequence);
        }));
    }

    private boolean replayFailure(RuntimeEvent event) {
        if ("replay_gap".equals(event.type())) {
            return true;
        }
        Object code = event.data().get("code");
        return "error".equals(event.type())
                && ("replay_gap".equals(code)
                || "cursor_ahead".equals(code)
                || "event_cursor_ahead".equals(code));
    }
}

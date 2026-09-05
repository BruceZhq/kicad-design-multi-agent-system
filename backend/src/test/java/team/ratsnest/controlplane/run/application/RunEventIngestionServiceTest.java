package team.ratsnest.controlplane.run.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.Flow;

import org.junit.jupiter.api.Test;
import org.mockito.InOrder;
import org.springframework.transaction.TransactionStatus;
import org.springframework.transaction.support.TransactionCallback;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.EventSubscription;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunReference;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.StartRunCommand;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore;
import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore.IngestionClaim;
import team.ratsnest.controlplane.run.domain.port.RunStore;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

class RunEventIngestionServiceTest {

    private static final String WORKER = "worker-1";
    private static final String DIGEST = "a".repeat(64);

    @Test
    void ingestsWithoutABrowserSubscriberAndAdvancesOnlyAfterPersistence() {
        Fixture fixture = fixture(0, 1, event(1, "message", Map.of()));
        when(fixture.ingestion.advance(fixture.claim, WORKER, 0, 1)).thenReturn(true);

        boolean active = fixture.service.ingest(fixture.claim, WORKER);

        assertThat(active).isFalse();
        InOrder order = inOrder(fixture.lifecycle, fixture.ingestion);
        order.verify(fixture.lifecycle).ingestRuntimeEvent(fixture.run, fixture.event);
        order.verify(fixture.ingestion).advance(fixture.claim, WORKER, 0, 1);
        verify(fixture.runtime).subscribeEvents(any(EventSubscription.class));
    }

    @Test
    void allowsAnAuthoritativelyFilteredSequenceHoleWithoutInventingAReplayGap() {
        Fixture fixture = fixture(43, 45, event(45, "message", Map.of()));
        when(fixture.ingestion.advance(fixture.claim, WORKER, 43, 45)).thenReturn(true);

        fixture.service.ingest(fixture.claim, WORKER);

        verify(fixture.lifecycle).ingestRuntimeEvent(fixture.run, fixture.event);
        verify(fixture.ingestion).advance(fixture.claim, WORKER, 43, 45);
    }

    @Test
    void explicitReplayGapFailsClosedWithoutPersistenceOrCursorAdvance() {
        Fixture fixture = fixture(
                3,
                5,
                event(5, "error", Map.of("code", "replay_gap", "retryable", false)));

        assertThatThrownBy(() -> fixture.service.ingest(fixture.claim, WORKER))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("replay-gap");

        verify(fixture.lifecycle, never()).ingestRuntimeEvent(any(), any());
        verify(fixture.ingestion, never()).advance(any(), any(), any(Long.class), any(Long.class));
    }

    @Test
    void persistenceFailureLeavesTheDurableCursorForRetry() {
        Fixture fixture = fixture(0, 1, event(1, "message", Map.of()));
        org.mockito.Mockito.doThrow(new IllegalStateException("database unavailable"))
                .when(fixture.lifecycle).ingestRuntimeEvent(fixture.run, fixture.event);

        assertThatThrownBy(() -> fixture.service.ingest(fixture.claim, WORKER))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("database unavailable");

        verify(fixture.ingestion, never()).advance(any(), any(), any(Long.class), any(Long.class));
    }

    @Test
    void browserFirstHitlBacklogDrainsWhileTheRunIsWaitingForInput() {
        RuntimeEvent harnessObservation = event(1, "message", Map.of());
        RuntimeEvent interaction = event(2, "ag_ui", Map.of());
        Fixture fixture = fixture(
                0,
                2,
                List.of(harnessObservation, interaction),
                RunState.WAITING_FOR_INPUT,
                RunState.WAITING_FOR_INPUT);
        when(fixture.ingestion.advance(fixture.claim, WORKER, 0, 1)).thenReturn(true);
        when(fixture.ingestion.advance(fixture.claim, WORKER, 1, 2)).thenReturn(true);

        boolean active = fixture.service.ingest(fixture.claim, WORKER);

        assertThat(active).isFalse();
        InOrder order = inOrder(fixture.lifecycle, fixture.ingestion);
        order.verify(fixture.lifecycle).ingestRuntimeEvent(fixture.run, harnessObservation);
        order.verify(fixture.ingestion).advance(fixture.claim, WORKER, 0, 1);
        order.verify(fixture.lifecycle).ingestRuntimeEvent(fixture.run, interaction);
        order.verify(fixture.ingestion).advance(fixture.claim, WORKER, 1, 2);
    }

    @SuppressWarnings({"rawtypes", "unchecked"})
    private Fixture fixture(long cursor, long highWater, RuntimeEvent event) {
        return fixture(
                cursor, highWater, List.of(event), RunState.RUNNING, RunState.COMPLETED);
    }

    @SuppressWarnings({"rawtypes", "unchecked"})
    private Fixture fixture(
            long cursor,
            long highWater,
            List<RuntimeEvent> events,
            RunState storedState,
            RunState runtimeState) {
        TransactionTemplate transactions = mock(TransactionTemplate.class);
        when(transactions.execute(any())).thenAnswer(invocation -> {
            TransactionCallback callback = invocation.getArgument(0);
            return callback.doInTransaction(mock(TransactionStatus.class));
        });
        TenantContext tenants = mock(TenantContext.class);
        RunStore runs = mock(RunStore.class);
        RunEventIngestionStore ingestion = mock(RunEventIngestionStore.class);
        AgentRuntimeGateway runtime = mock(AgentRuntimeGateway.class);
        RunAccessSupport access = mock(RunAccessSupport.class);
        RunLifecycleService lifecycle = mock(RunLifecycleService.class);
        Run run = run(storedState);
        IngestionClaim claim = new IngestionClaim(run.tenantId(), run.runId(), cursor, 1);
        RuntimeIdentity identity = new RuntimeIdentity(
                "principal", run.tenantId().toString(), run.projectId().toString());
        StartRunCommand command = new StartRunCommand(
                run.runId().toString(), run.threadId(), identity,
                run.message(), run.model(), null, run.runtimeConfig(), true);
        RunReference reference = new RunReference(
                run.runId().toString(), identity, run.harnessChannel() + "@" + run.harnessVersionId());
        RuntimeRun snapshot = new RuntimeRun(
                run.runId().toString(), "runtime-run", "build", runtimeState,
                "agent", run.threadId(), Instant.now(), Instant.now(), Instant.now(),
                highWater, highWater == 0 ? null : 1L, highWater == 0 ? null : highWater,
                null, null, Map.of());

        when(runs.find(run.tenantId(), run.runId())).thenReturn(Optional.of(run));
        when(access.reference(run)).thenReturn(reference);
        when(access.command(run)).thenReturn(command);
        when(runtime.getRun(reference)).thenReturn(snapshot);
        when(runtime.subscribeEvents(any(EventSubscription.class))).thenReturn(publisher(events));

        RunEventIngestionService service = new RunEventIngestionService(
                transactions, tenants, runs, ingestion, runtime, access, lifecycle);
        return new Fixture(service, ingestion, runtime, lifecycle, run, claim, events.getFirst());
    }

    private Flow.Publisher<RuntimeEvent> publisher(RuntimeEvent event) {
        return publisher(List.of(event));
    }

    private Flow.Publisher<RuntimeEvent> publisher(List<RuntimeEvent> events) {
        return subscriber -> subscriber.onSubscribe(new Flow.Subscription() {
            private int index;
            private boolean cancelled;
            private boolean completed;

            @Override
            public void request(long count) {
                if (cancelled || completed || index >= events.size()) return;
                RuntimeEvent event = events.get(index++);
                subscriber.onNext(event);
                if (!cancelled && !completed && index >= events.size()) {
                    completed = true;
                    subscriber.onComplete();
                }
            }

            @Override
            public void cancel() {
                cancelled = true;
            }
        });
    }

    private RuntimeEvent event(long sequence, String type, Map<String, Object> data) {
        return new RuntimeEvent(sequence, type, null, null, null, data);
    }

    private Run run() {
        return run(RunState.RUNNING);
    }

    private Run run(RunState state) {
        return new Run(
                UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                "thread", "idempotency", DIGEST, "private requirement", "model",
                Map.of("harness_version", Map.of("manifest_digest", DIGEST)),
                "site-control-telemetry", "1.0", DIGEST,
                "harness-v1", DIGEST, "stable", "principal", "issuer", "subject",
                state, null, 0, null, null, null, null,
                Instant.now(), Instant.now(), null);
    }

    private record Fixture(
            RunEventIngestionService service,
            RunEventIngestionStore ingestion,
            AgentRuntimeGateway runtime,
            RunLifecycleService lifecycle,
            Run run,
            IngestionClaim claim,
            RuntimeEvent event) {
    }
}

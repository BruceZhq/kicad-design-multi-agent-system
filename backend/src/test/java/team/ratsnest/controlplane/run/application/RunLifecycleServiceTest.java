package team.ratsnest.controlplane.run.application;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyMap;
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
import java.util.function.Consumer;

import org.junit.jupiter.api.Test;
import org.mockito.InOrder;
import org.springframework.transaction.TransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.EventSubscription;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.artifact.application.ArtifactManifestParser;
import team.ratsnest.controlplane.artifact.domain.port.ArtifactStore;
import team.ratsnest.controlplane.evolution.application.EvolutionCollector;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore;
import team.ratsnest.controlplane.run.domain.port.RunInteractionStore;
import team.ratsnest.controlplane.run.domain.port.RunOutbox;
import team.ratsnest.controlplane.run.domain.port.RunStore;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

class RunLifecycleServiceTest {

    private static final String DIGEST = "a".repeat(64);

    @Test
    @SuppressWarnings("unchecked")
    void persistsHitlInteractionBeforeForwardingItToTheBrowser() {
        TransactionTemplate transactions = mock(TransactionTemplate.class);
        org.mockito.Mockito.doAnswer(invocation -> {
            Consumer<TransactionStatus> work = invocation.getArgument(0);
            work.accept(mock(TransactionStatus.class));
            return null;
        }).when(transactions).executeWithoutResult(any());
        TenantContext tenants = mock(TenantContext.class);
        RunStore runs = mock(RunStore.class);
        RunEventIngestionStore eventIngestion = mock(RunEventIngestionStore.class);
        RunInteractionStore interactions = mock(RunInteractionStore.class);
        RunOutbox outbox = mock(RunOutbox.class);
        ArtifactStore artifacts = mock(ArtifactStore.class);
        ArtifactManifestParser manifests = mock(ArtifactManifestParser.class);
        EvolutionCollector evolution = mock(EvolutionCollector.class);
        AgentRuntimeGateway runtime = mock(AgentRuntimeGateway.class);
        RunAccessSupport access = mock(RunAccessSupport.class);
        Run run = run();
        AuthenticatedActor actor = new AuthenticatedActor("issuer", "subject");
        RuntimeEvent event = hitlEvent();
        Runnable forwarded = mock(Runnable.class);

        when(access.requireRun(run.tenantId(), run.runId(), actor)).thenReturn(run);
        when(runtime.subscribeEvents(any(EventSubscription.class))).thenReturn(publisher(event));
        when(runs.findForUpdate(run.tenantId(), run.runId())).thenReturn(Optional.of(run));
        when(interactions.register(eq(run), eq("interaction-1"), eq(1L), anyMap()))
                .thenReturn(true);
        when(runs.markWaitingForInput(run.tenantId(), run.runId())).thenReturn(true);

        RunLifecycleService service = new RunLifecycleService(
                transactions, tenants, runs, eventIngestion, interactions, outbox, artifacts, manifests,
                evolution, runtime, access, false, false);
        service.events(run.tenantId(), run.runId(), 0, actor).subscribe(new Flow.Subscriber<>() {
            @Override
            public void onSubscribe(Flow.Subscription subscription) {
                subscription.request(1);
            }

            @Override
            public void onNext(RuntimeEvent ignored) {
                forwarded.run();
            }

            @Override
            public void onError(Throwable ignored) {
            }

            @Override
            public void onComplete() {
            }
        });

        InOrder order = inOrder(eventIngestion, interactions, forwarded);
        order.verify(eventIngestion).recordObservedHighWater(
                run.tenantId(), run.runId(), 1L);
        order.verify(interactions).register(eq(run), eq("interaction-1"), eq(1L), anyMap());
        order.verify(forwarded).run();
        verify(evolution, never()).collect(any(), any());
    }

    private Flow.Publisher<RuntimeEvent> publisher(RuntimeEvent event) {
        return subscriber -> subscriber.onSubscribe(new Flow.Subscription() {
            private boolean emitted;

            @Override
            public void request(long count) {
                if (emitted) return;
                emitted = true;
                subscriber.onNext(event);
            }

            @Override
            public void cancel() {
            }
        });
    }

    private RuntimeEvent hitlEvent() {
        return new RuntimeEvent(
                1L,
                "ag_ui",
                null,
                null,
                null,
                Map.of(
                        "type", "CUSTOM",
                        "name", "ratsnest.human-input-required.v1",
                        "value", Map.of(
                                "interactionId", "interaction-1",
                                "kind", "clarification",
                                "question", "Choose the verified component",
                                "requestedBy", "Parts Specialist",
                                "options", List.of(Map.of("id", "a", "label", "A")),
                                "allowFreeText", true,
                                "stateVersion", 1)));
    }

    private Run run() {
        return new Run(
                UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                "thread", "idempotency", DIGEST, "private requirement", "model",
                Map.of("harness_version", Map.of("manifest_digest", DIGEST)),
                "site-control-telemetry", "1.0", DIGEST,
                "harness-v1", DIGEST, "stable", "principal", "issuer", "subject",
                RunState.RUNNING, null, 0, null, null, null, null,
                Instant.now(), Instant.now(), null);
    }
}

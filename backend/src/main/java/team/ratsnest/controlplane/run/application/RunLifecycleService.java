package team.ratsnest.controlplane.run.application;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Flow;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.domain.model.AgentRuntimeException;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.ControlRunCommand;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.EventSubscription;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunControl;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.artifact.application.ArtifactManifestParser;
import team.ratsnest.controlplane.artifact.domain.model.ArtifactManifest;
import team.ratsnest.controlplane.artifact.domain.port.ArtifactStore;
import team.ratsnest.controlplane.evolution.application.EvolutionCollector;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore;
import team.ratsnest.controlplane.run.domain.port.RunInteractionStore;
import team.ratsnest.controlplane.run.domain.port.RunOutbox;
import team.ratsnest.controlplane.run.domain.port.RunStore;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

/** Runtime lifecycle, event ingestion, outbox and reconciliation use cases. */
@Service
public class RunLifecycleService {

    private final TransactionTemplate transactions;
    private final TenantContext tenantContext;
    private final RunStore runs;
    private final RunEventIngestionStore eventIngestion;
    private final RunInteractionStore interactions;
    private final RunOutbox outbox;
    private final ArtifactStore artifacts;
    private final ArtifactManifestParser artifactManifests;
    private final EvolutionCollector evolutionCollector;
    private final AgentRuntimeGateway runtime;
    private final RunAccessSupport access;
    private final boolean outboxEnabled;
    private final boolean reconciliationEnabled;

    public RunLifecycleService(
            TransactionTemplate transactions,
            TenantContext tenantContext,
            RunStore runs,
            RunEventIngestionStore eventIngestion,
            RunInteractionStore interactions,
            RunOutbox outbox,
            ArtifactStore artifacts,
            ArtifactManifestParser artifactManifests,
            EvolutionCollector evolutionCollector,
            AgentRuntimeGateway runtime,
            RunAccessSupport access,
            @Value("${ratsnest.run-outbox.enabled:false}") boolean outboxEnabled,
            @Value("${ratsnest.run-reconciliation.enabled:false}") boolean reconciliationEnabled) {
        this.transactions = transactions;
        this.tenantContext = tenantContext;
        this.runs = runs;
        this.eventIngestion = eventIngestion;
        this.interactions = interactions;
        this.outbox = outbox;
        this.artifacts = artifacts;
        this.artifactManifests = artifactManifests;
        this.evolutionCollector = evolutionCollector;
        this.runtime = runtime;
        this.access = access;
        this.outboxEnabled = outboxEnabled;
        this.reconciliationEnabled = reconciliationEnabled;
    }

    Run dispatchStart(Run run, AuthenticatedActor actor) {
        if (reconciliationEnabled) {
            return access.requireRun(run.tenantId(), run.runId(), actor);
        }
        try {
            updateFromRuntime(run, runtime.startRun(access.command(run)));
        } catch (AgentRuntimeException exception) {
            markFailed(run, "RUNTIME_START_FAILED", exception.getMessage());
        }
        return access.requireRun(run.tenantId(), run.runId(), actor);
    }

    public Run cancel(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        Run run = access.requireWritableRun(tenantId, runId, actor);
        if (RunAccessSupport.terminal(run.state())) {
            return run;
        }
        try {
            if (reconciliationEnabled && run.state() == RunState.QUEUED) {
                runtime.startRun(access.command(run));
            }
            RuntimeRun cancelled = runtime.controlRun(
                    new ControlRunCommand(access.reference(run), RunControl.CANCEL));
            updateFromRuntime(run, cancelled);
            return access.requireRun(tenantId, runId, actor);
        } catch (AgentRuntimeException exception) {
            throw access.runtimeFailure(exception);
        }
    }

    public Flow.Publisher<RuntimeEvent> events(
            UUID tenantId,
            UUID runId,
            long lastEventId,
            AuthenticatedActor actor) {
        Run run = access.requireRun(tenantId, runId, actor);
        Flow.Publisher<RuntimeEvent> source = runtime.subscribeEvents(
                new EventSubscription(access.command(run), lastEventId));
        return subscriber -> source.subscribe(new Flow.Subscriber<>() {

            @Override
            public void onSubscribe(Flow.Subscription subscription) {
                subscriber.onSubscribe(subscription);
            }

            @Override
            public void onNext(RuntimeEvent event) {
                persistOperationalRuntimeEvent(run, event);
                subscriber.onNext(event);
                if (streamEndSignal(event.type())) {
                    refreshAfterStreamSignal(run);
                }
            }

            @Override
            public void onError(Throwable throwable) {
                subscriber.onError(throwable);
            }

            @Override
            public void onComplete() {
                refreshAfterStreamSignal(run);
                subscriber.onComplete();
            }
        });
    }

    public boolean reconcile(UUID tenantId, UUID runId) {
        Run run = transactions.execute(status -> {
            tenantContext.activate(tenantId);
            return runs.find(tenantId, runId).orElse(null);
        });
        if (run == null
                || RunAccessSupport.terminal(run.state())
                || run.state() == RunState.WAITING_FOR_INPUT) {
            return false;
        }
        RuntimeRun current = runtime.startRun(access.command(run));
        updateFromRuntime(run, current);
        return !RunAccessSupport.terminal(current.state());
    }

    void updateFromRuntime(Run run, RuntimeRun runtimeRun) {
        transactions.executeWithoutResult(status -> {
            tenantContext.activate(run.tenantId());
            Run current = runs.findForUpdate(run.tenantId(), run.runId()).orElse(null);
            if (current == null) {
                return;
            }
            boolean stateChanged = runtimeRun.state() != current.state();
            if (runs.updateFromRuntime(run.tenantId(), run.runId(), runtimeRun) && stateChanged) {
                appendLifecycle(
                        current,
                        "run." + runtimeRun.state().name().toLowerCase(java.util.Locale.ROOT),
                        runtimeRun.state(),
                        runtimeRun.errorCode(),
                        runtimeRun.error());
            }
            persistRuntimeResult(current, runtimeRun);
        });
    }

    void synchronizeTerminalResult(Run run) {
        if (run.deliveryStatus() != null) {
            return;
        }
        try {
            updateFromRuntime(run, runtime.getRun(access.reference(run)));
        } catch (AgentRuntimeException ignored) {
            // A structured manifest, not a narrative state, determines delivery.
        }
    }

    void markFailed(Run run, String code, String error) {
        transactions.executeWithoutResult(status -> {
            tenantContext.activate(run.tenantId());
            Run current = runs.findForUpdate(run.tenantId(), run.runId()).orElse(null);
            if (current != null && runs.markFailed(run.tenantId(), run.runId(), code, error)) {
                appendLifecycle(current, "run.failed", RunState.FAILED, code, error);
            }
        });
    }

    void appendLifecycle(
            Run run,
            String eventType,
            RunState state,
            String errorCode,
            String error) {
        if (!outboxEnabled) {
            return;
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("requestId", run.runId().toString());
        payload.put("projectId", run.projectId().toString());
        payload.put("rootRunId", run.rootRunId().toString());
        payload.put("revisionNumber", run.revisionNumber());
        if (run.parentRunId() != null) {
            payload.put("parentRunId", run.parentRunId().toString());
        }
        if (run.forkedFromRunId() != null) {
            payload.put("forkedFromRunId", run.forkedFromRunId().toString());
        }
        payload.put("threadId", run.threadId());
        payload.put("state", state.name());
        if (run.profileId() != null) {
            payload.put("profileId", run.profileId());
            payload.put("profileVersion", run.profileVersion());
        }
        payload.put("harnessVersionId", run.harnessVersionId());
        payload.put("harnessManifestDigest", run.harnessManifestDigest());
        payload.put("harnessChannel", run.harnessChannel());
        if (errorCode != null) {
            payload.put("errorCode", errorCode);
        }
        if (error != null) {
            payload.put("error", error);
        }
        outbox.append(run.tenantId(), run.runId(), eventType, Map.copyOf(payload));
    }

    private void refreshAfterStreamSignal(Run run) {
        try {
            updateFromRuntime(run, runtime.getRun(access.reference(run)));
        } catch (AgentRuntimeException ignored) {
            // The bounded reconciliation worker retries when enabled.
        }
    }

    /**
     * Persists one runtime event for the control-plane-owned durable ingestion path.
     * Browser SSE subscribers never call this method and therefore cannot advance or
     * skip the governance ingestion sequence.
     */
    void ingestRuntimeEvent(Run run, RuntimeEvent event) {
        if (event.eventId() == null || event.eventId() <= 0) {
            return;
        }
        if (evolutionCollector.supports(event)) {
            // Deliberately propagate failures. The ingestion cursor advances only
            // after this call succeeds, so the worker can replay the same event.
            evolutionCollector.collect(run, event);
        }
        persistOperationalRuntimeEvent(run, event);
    }

    /** Immediate, idempotent UI-path persistence; never collects Evolution or advances its cursor. */
    private void persistOperationalRuntimeEvent(Run run, RuntimeEvent event) {
        if (event.eventId() == null || event.eventId() <= 0) {
            return;
        }
        boolean artifactManifest = "artifact_manifest".equals(event.type());
        boolean interactionRequest = "ag_ui".equals(event.type());
        boolean outboxEvent = outboxEnabled && persistableEvent(event.type());
        transactions.executeWithoutResult(status -> {
            tenantContext.activate(run.tenantId());
            Run current = runs.findForUpdate(run.tenantId(), run.runId()).orElse(null);
            if (current == null) {
                return;
            }
            // Browser delivery is only a wake-up hint. The independent ingestion
            // cursor remains authoritative and advances only after durable replay.
            eventIngestion.recordObservedHighWater(
                    run.tenantId(), run.runId(), event.eventId());
            if (artifactManifest) {
                persistManifest(current, event.eventId(), event.data());
            }
            if (interactionRequest) {
                persistInteraction(current, event.data());
            }
            if (outboxEvent) {
                outbox.appendSourceEvent(
                        run.tenantId(),
                        run.runId(),
                        event.eventId(),
                        "run.event." + event.type(),
                        runtimeEventPayload(event));
            }
        });
    }

    private void persistInteraction(Run run, Map<String, Object> event) {
        if (!"CUSTOM".equals(event.get("type"))
                || !"ratsnest.human-input-required.v1".equals(event.get("name"))
                || !(event.get("value") instanceof Map<?, ?> raw)) {
            return;
        }
        Map<String, Object> value = new LinkedHashMap<>();
        raw.forEach((key, item) -> {
            if (key instanceof String text) {
                value.put(text, item);
            }
        });
        String interactionId = stringValue(value.get("interactionId"));
        String kind = stringValue(value.get("kind"));
        String question = stringValue(value.get("question"));
        String requestedBy = stringValue(value.get("requestedBy"));
        Long stateVersion = positiveLong(value.get("stateVersion"));
        if (interactionId == null || !interactionId.matches("[A-Za-z0-9._:-]{1,200}")
                || !"clarification".equals(kind)
                || question == null
                || requestedBy == null
                || !(value.get("options") instanceof List<?>)
                || !(value.get("allowFreeText") instanceof Boolean)
                || stateVersion == null) {
            return;
        }
        boolean created = interactions.register(
                run, interactionId, stateVersion, Map.copyOf(value));
        if (created && runs.markWaitingForInput(run.tenantId(), run.runId())) {
            appendLifecycle(run, "run.waiting_for_input", RunState.WAITING_FOR_INPUT, null, null);
        }
    }

    private void persistRuntimeResult(Run run, RuntimeRun runtimeRun) {
        if (runtimeRun.result().containsKey("artifact_manifest")
                || runtimeRun.result().containsKey("manifest_id")) {
            persistManifest(run, runtimeRun.newestEventId(), runtimeRun.result());
        }
    }

    private void persistManifest(Run run, Long eventId, Map<String, Object> data) {
        ArtifactManifest manifest = artifactManifests.parse(run.runId(), eventId, data);
        boolean inserted = artifacts.persist(run.tenantId(), run.runId(), manifest);
        runs.setDeliveryStatus(run.tenantId(), run.runId(), manifest.deliveryStatus());
        if (inserted && outboxEnabled) {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("requestId", run.runId().toString());
            payload.put("manifestId", manifest.manifestId().toString());
            payload.put("manifestDigest", manifest.digest());
            payload.put("deliveryStatus", manifest.deliveryStatus().apiValue());
            payload.put("artifactCount", manifest.artifacts().size());
            outbox.append(
                    run.tenantId(),
                    run.runId(),
                    "run.delivery." + manifest.deliveryStatus().apiValue(),
                    Map.copyOf(payload));
        }
    }

    private Map<String, Object> runtimeEventPayload(RuntimeEvent event) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("sourceEventSeq", event.eventId());
        payload.put("type", event.type());
        if (event.message() != null) {
            payload.put("message", event.message());
        }
        if (event.content() != null) {
            payload.put("content", event.content());
        }
        if (event.error() != null) {
            payload.put("error", event.error());
        }
        if (!event.data().isEmpty()) {
            payload.put("data", event.data());
        }
        return Map.copyOf(payload);
    }

    private boolean persistableEvent(String type) {
        return "message".equals(type)
                || "ag_ui".equals(type)
                || "artifact_manifest".equals(type)
                || "error".equals(type)
                || terminalEvent(type);
    }

    private boolean streamEndSignal(String type) {
        return "done".equals(type) || terminalEvent(type);
    }

    private boolean terminalEvent(String type) {
        return "completed".equals(type)
                || "failed".equals(type)
                || "cancelled".equals(type)
                || "timed_out".equals(type);
    }

    private String stringValue(Object value) {
        return value instanceof String text && !text.isBlank() ? text : null;
    }

    private Long positiveLong(Object value) {
        if (!(value instanceof Number number)) {
            return null;
        }
        long result = number.longValue();
        return result > 0 ? result : null;
    }
}

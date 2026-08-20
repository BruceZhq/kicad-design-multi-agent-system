package team.ratsnest.controlplane.agentgateway.infrastructure.http;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.Test;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.EventSubscription;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunReference;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.StartRunCommand;
import team.ratsnest.controlplane.agentgateway.infrastructure.security.InternalTaskSigner;
import tools.jackson.databind.ObjectMapper;

class HttpAgentRuntimeGatewayTest {

    private static final String REQUEST_ID = "34275583-8c58-4d7c-81f0-c89e30bd8ba0";

    @Test
    void staleDoneMarkerDoesNotHideEventsFromTheRecoveredSegment() throws Exception {
        String stream = """
                id: 44
                data: [DONE]

                id: 45
                data: {"type":"message","content":{"type":"ai","content":"later"}}

                id: 46
                data: [DONE]

                """;
        try (Fixture fixture = new Fixture(stream, 21)) {
            CollectingSubscriber subscriber = fixture.subscribe();

            assertTrue(subscriber.finished.await(5, TimeUnit.SECONDS));
            assertNull(subscriber.error);
            assertEquals(List.of("message", "completed"), subscriber.events.stream()
                    .map(RuntimeEvent::type)
                    .toList());
            assertEquals("later", subscriber.events.getFirst().message().content());
            assertTrue(fixture.statusCalls.get() >= 22);
        }
    }

    @Test
    void terminalDoneMarkerStillCompletesTheRunStream() throws Exception {
        try (Fixture fixture = new Fixture("id: 8\ndata: [DONE]\n\n", 0)) {
            CollectingSubscriber subscriber = fixture.subscribe();

            assertTrue(subscriber.finished.await(5, TimeUnit.SECONDS));
            assertNull(subscriber.error);
            assertEquals(List.of("completed"), subscriber.events.stream()
                    .map(RuntimeEvent::type)
                    .toList());
        }
    }

    @Test
    void preservesTopLevelDurableUiSnapshotInRuntimeResult() throws Exception {
        try (Fixture fixture = new Fixture("", 1)) {
            RuntimeRun run = fixture.getRun();

            assertEquals(
                    46,
                    ((Map<?, ?>) run.result().get("ui_snapshot")).get("snapshot_cursor"));
        }
    }

    private static final class Fixture implements AutoCloseable {

        private final HttpServer server;
        private final HttpAgentRuntimeGateway gateway;
        private final String stream;
        private final int runningStatusResponses;
        private final AtomicInteger statusCalls = new AtomicInteger();

        private Fixture(String stream, int runningStatusResponses) throws IOException {
            this.stream = stream;
            this.runningStatusResponses = runningStatusResponses;
            this.server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext(
                    "/internal/v1/runs/ratsnestpro-multi-agent/stream",
                    this::stream);
            server.createContext("/internal/v1/runs/" + REQUEST_ID, this::status);
            server.start();

            ObjectMapper objectMapper = new ObjectMapper();
            InternalTaskSigner signer = new InternalTaskSigner(
                    "http-gateway-test-signing-secret-32-bytes",
                    objectMapper,
                    Clock.systemUTC());
            this.gateway = new HttpAgentRuntimeGateway(
                    "http://127.0.0.1:" + server.getAddress().getPort(),
                    "",
                    signer,
                    objectMapper);
        }

        private CollectingSubscriber subscribe() {
            StartRunCommand command = new StartRunCommand(
                    REQUEST_ID,
                    "thread-1",
                    new RuntimeIdentity("principal", "tenant", "project"),
                    "message",
                    "model",
                    null,
                    Map.of(),
                    true);
            CollectingSubscriber subscriber = new CollectingSubscriber();
            gateway.subscribeEvents(new EventSubscription(command, 0)).subscribe(subscriber);
            return subscriber;
        }

        private RuntimeRun getRun() {
            return gateway.getRun(new RunReference(
                    REQUEST_ID,
                    new RuntimeIdentity("principal", "tenant", "project"),
                    null));
        }

        private void stream(HttpExchange exchange) throws IOException {
            exchange.getRequestBody().readAllBytes();
            byte[] body = stream.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        }

        private void status(HttpExchange exchange) throws IOException {
            boolean running = statusCalls.incrementAndGet() <= runningStatusResponses;
            String state = running ? "running" : "completed";
            String body = """
                    {"request_id":"%s","run_id":"graph-run","kind":"stream",
                    "status":"%s","agent_id":"ratsnestpro-multi-agent","thread_id":"thread-1",
                    "created_at":"2026-08-20T00:00:00Z","event_count":46,
                    "oldest_event_id":1,"newest_event_id":46,
                    "execution_lease_active":%s,"recoverable":false,
                    "checked_at":"2026-08-20T00:01:00Z","result":{},
                    "ui_snapshot":{"snapshot_cursor":46,"coverage_complete":true}}
                    """.formatted(REQUEST_ID, state, running);
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        }

        @Override
        public void close() {
            gateway.close();
            server.stop(0);
        }
    }

    private static final class CollectingSubscriber implements Flow.Subscriber<RuntimeEvent> {

        private final List<RuntimeEvent> events = new CopyOnWriteArrayList<>();
        private final CountDownLatch finished = new CountDownLatch(1);
        private volatile Throwable error;

        @Override
        public void onSubscribe(Flow.Subscription subscription) {
            subscription.request(Long.MAX_VALUE);
        }

        @Override
        public void onNext(RuntimeEvent item) {
            events.add(item);
        }

        @Override
        public void onError(Throwable throwable) {
            error = throwable;
            finished.countDown();
        }

        @Override
        public void onComplete() {
            finished.countDown();
        }
    }
}

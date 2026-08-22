package team.ratsnest.controlplane.run.infrastructure.messaging;

import java.net.InetAddress;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import tools.jackson.databind.ObjectMapper;
import team.ratsnest.controlplane.run.domain.port.RunOutbox;
import team.ratsnest.controlplane.run.domain.port.RunOutbox.OutboxEvent;

@Component
@ConditionalOnProperty(name = "ratsnest.run-outbox.enabled", havingValue = "true")
class RunOutboxPublisher {

    private static final Logger logger = LoggerFactory.getLogger(RunOutboxPublisher.class);
    private static final Duration MAX_SEND_TIMEOUT = Duration.ofMinutes(4);

    private final RunOutbox outbox;
    private final KafkaTemplate<String, String> kafka;
    private final ObjectMapper objectMapper;
    private final String topic;
    private final int batchSize;
    private final Duration sendTimeout;
    private final String workerId;

    RunOutboxPublisher(
            RunOutbox outbox,
            KafkaTemplate<String, String> kafka,
            ObjectMapper objectMapper,
            @Value("${ratsnest.run-outbox.topic:ratsnest.runs.v1}") String topic,
            @Value("${ratsnest.run-outbox.batch-size:100}") int batchSize,
            @Value("${ratsnest.run-outbox.send-timeout:10s}") Duration sendTimeout) {
        this.outbox = outbox;
        this.kafka = kafka;
        this.objectMapper = objectMapper;
        this.topic = topic;
        this.batchSize = Math.max(1, Math.min(batchSize, 500));
        if (sendTimeout == null || sendTimeout.isZero() || sendTimeout.isNegative()) {
            throw new IllegalArgumentException("Run outbox send timeout must be positive");
        }
        this.sendTimeout = sendTimeout.compareTo(MAX_SEND_TIMEOUT) > 0
                ? MAX_SEND_TIMEOUT
                : sendTimeout;
        this.workerId = hostName() + "-" + UUID.randomUUID();
    }

    @Scheduled(fixedDelayString = "${ratsnest.run-outbox.poll-delay:1s}")
    void publishPending() {
        for (OutboxEvent event : outbox.claim(workerId, batchSize)) {
            try {
                kafka.send(topic, event.runId().toString(), envelope(event))
                        .get(sendTimeout.toMillis(), TimeUnit.MILLISECONDS);
                if (!outbox.acknowledge(event.eventId(), workerId)) {
                    logger.warn("Run outbox acknowledgement lost eventId={}", event.eventId());
                }
            } catch (InterruptedException exception) {
                Thread.currentThread().interrupt();
                outbox.retry(event.eventId(), workerId, 1);
                return;
            } catch (Exception exception) {
                outbox.retry(event.eventId(), workerId, retryDelay(event.publishAttempts()));
                logger.warn(
                        "Run outbox publish deferred eventId={} cause={}",
                        event.eventId(),
                        exception.getClass().getSimpleName());
            }
        }
    }

    private String envelope(OutboxEvent event) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("schemaVersion", "1.0");
        value.put("eventId", event.eventId());
        value.put("tenantId", event.tenantId());
        value.put("runId", event.runId());
        value.put("stateVersion", event.stateVersion());
        if (event.sourceEventSeq() != null) {
            value.put("sourceEventSeq", event.sourceEventSeq());
        }
        value.put("eventType", event.eventType());
        value.put("occurredAt", event.occurredAt());
        value.put("payload", event.payload());
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to serialize run outbox event", exception);
        }
    }

    private int retryDelay(int publishAttempts) {
        int exponent = Math.min(Math.max(publishAttempts - 1, 0), 8);
        return Math.min(1 << exponent, 300);
    }

    private static String hostName() {
        try {
            return InetAddress.getLocalHost().getHostName();
        } catch (Exception ignored) {
            return "control-plane";
        }
    }
}

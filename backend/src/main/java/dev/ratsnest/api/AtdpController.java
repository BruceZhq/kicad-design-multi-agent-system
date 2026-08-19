package dev.ratsnest.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.trajectory.AtdpEvent;
import dev.ratsnest.trajectory.AtdpEventRepository;
import dev.ratsnest.security.RunAccessPolicy;
import dev.ratsnest.security.ServiceAccessPolicy;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api")
public class AtdpController {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final AtdpEventRepository events;
    private final DesignRunRepository runs;
    private final RunAccessPolicy access;
    private final ServiceAccessPolicy serviceAccess;
    private final org.springframework.beans.factory.ObjectProvider<
            org.springframework.kafka.core.KafkaTemplate<String, String>> kafka;

    @org.springframework.beans.factory.annotation.Value("${ratsnest.dispatch:local}")
    private String dispatchMode;

    @org.springframework.beans.factory.annotation.Value("${ratsnest.topic.atdp-events:ratsnest.atdp-events}")
    private String atdpTopic;

    public AtdpController(AtdpEventRepository events, DesignRunRepository runs,
                          RunAccessPolicy access,
                          ServiceAccessPolicy serviceAccess,
                          org.springframework.beans.factory.ObjectProvider<
                                  org.springframework.kafka.core.KafkaTemplate<String, String>> kafka) {
        this.events = events;
        this.runs = runs;
        this.access = access;
        this.serviceAccess = serviceAccess;
        this.kafka = kafka;
    }

    /** Ingest one ATDP TrajectoryEvent from the agent runtime's data proxy. */
    @PostMapping("/atdp/events")
    public ResponseEntity<Map<String, String>> ingest(@RequestBody String body)
            throws Exception {
        serviceAccess.requireServiceOrOpenMode();
        JsonNode json = MAPPER.readTree(body);
        AtdpEvent event = new AtdpEvent();
        event.setEventId(json.path("event_id").asText(null));
        event.setRunId(json.path("run_id").asText(null));
        event.setIteration(json.path("iteration").asInt());
        event.setStep(json.path("step").asInt());
        event.setNode(json.path("node").asText(null));
        event.setReward(json.path("reward").isNumber()
                ? json.path("reward").asDouble() : null);
        event.setReceivedAt(Instant.now());
        event.setPayload(body);
        events.save(event);
        // cluster mode: the ATDP stream also flows onto Kafka, so evolution
        // consumers can subscribe without polling the trajectory store
        if ("kafka".equals(dispatchMode)) {
            try {
                kafka.getObject().send(atdpTopic, event.getRunId(), body);
            } catch (Exception ignored) {
                // trajectory capture must never break ingestion
            }
        }
        return ResponseEntity.status(HttpStatus.ACCEPTED)
                .body(Map.of("eventId", String.valueOf(event.getEventId())));
    }

    /** Events for a control-plane run id (joins via the python run id). */
    @GetMapping("/runs/{id}/events")
    public ResponseEntity<List<AtdpEvent>> eventsForRun(@PathVariable String id) {
        return runs.findById(id)
                .filter(access::canAccess)
                .map(run -> run.getPythonRunId() == null
                        ? ResponseEntity.ok(List.<AtdpEvent>of())
                        : ResponseEntity.ok(
                                events.findByRunIdOrderByStepAsc(run.getPythonRunId())))
                .orElse(ResponseEntity.notFound().build());
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok", "service", "ratsnest-control-plane");
    }
}

package dev.ratsnest.trajectory;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Index;
import jakarta.persistence.Lob;
import jakarta.persistence.Table;

import java.time.Instant;

/** ATDP trajectory store row (paper pillar 1). One event = one step record
 *  ⟨observation, agent_state, action, outcome, reward, metadata⟩, kept as the
 *  original JSON payload plus indexed columns for trigger statistics. */
@Entity
@Table(name = "atdp_events", indexes = {
        @Index(name = "idx_atdp_run", columnList = "runId"),
        @Index(name = "idx_atdp_node", columnList = "node"),
})
public class AtdpEvent {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    private String eventId;
    private String runId;
    private int iteration;
    private int step;
    private String node;
    private Double reward;          // nullable: late-bound (ATDP §3)
    private Instant receivedAt;

    @Lob
    @Column(columnDefinition = "CLOB")
    private String payload;         // full TrajectoryEvent JSON, untouched

    public Long getId() { return id; }
    public String getEventId() { return eventId; }
    public void setEventId(String eventId) { this.eventId = eventId; }
    public String getRunId() { return runId; }
    public void setRunId(String runId) { this.runId = runId; }
    public int getIteration() { return iteration; }
    public void setIteration(int iteration) { this.iteration = iteration; }
    public int getStep() { return step; }
    public void setStep(int step) { this.step = step; }
    public String getNode() { return node; }
    public void setNode(String node) { this.node = node; }
    public Double getReward() { return reward; }
    public void setReward(Double reward) { this.reward = reward; }
    public Instant getReceivedAt() { return receivedAt; }
    public void setReceivedAt(Instant receivedAt) { this.receivedAt = receivedAt; }
    public String getPayload() { return payload; }
    public void setPayload(String payload) { this.payload = payload; }
}

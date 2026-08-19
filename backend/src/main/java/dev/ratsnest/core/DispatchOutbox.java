package dev.ratsnest.core;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;

@Entity
@Table(name = "dispatch_outbox")
public class DispatchOutbox {

    @Id
    private String id;

    @Column(name = "run_id", nullable = false, unique = true)
    private String runId;

    @Column(nullable = false, length = 20)
    private String status;

    @Column(nullable = false)
    private int attempts;

    @Column(nullable = false)
    private Instant availableAt;

    @Column(nullable = false)
    private Instant createdAt;

    private Instant publishedAt;

    @Column(length = 1000)
    private String lastError;

    @Version
    private long version;

    public static DispatchOutbox pending(String runId) {
        DispatchOutbox event = new DispatchOutbox();
        event.id = UUID.randomUUID().toString();
        event.runId = runId;
        event.status = "pending";
        event.availableAt = Instant.now();
        event.createdAt = event.availableAt;
        return event;
    }

    public void markPublished() {
        status = "published";
        publishedAt = Instant.now();
        lastError = null;
    }

    public void requeue() {
        status = "pending";
        attempts = 0;
        availableAt = Instant.now();
        publishedAt = null;
        lastError = null;
    }

    public void retryAfter(Exception error) {
        attempts++;
        long delaySeconds = Math.min(300, 1L << Math.min(attempts, 8));
        availableAt = Instant.now().plus(delaySeconds, ChronoUnit.SECONDS);
        String message = error.getMessage() == null
                ? error.getClass().getSimpleName() : error.getMessage();
        lastError = message.substring(0, Math.min(1000, message.length()));
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getRunId() { return runId; }
    public void setRunId(String runId) { this.runId = runId; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public int getAttempts() { return attempts; }
    public void setAttempts(int attempts) { this.attempts = attempts; }
    public Instant getAvailableAt() { return availableAt; }
    public void setAvailableAt(Instant availableAt) { this.availableAt = availableAt; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
    public Instant getPublishedAt() { return publishedAt; }
    public void setPublishedAt(Instant publishedAt) { this.publishedAt = publishedAt; }
    public String getLastError() { return lastError; }
    public void setLastError(String lastError) { this.lastError = lastError; }
    public long getVersion() { return version; }
    public void setVersion(long version) { this.version = version; }
}

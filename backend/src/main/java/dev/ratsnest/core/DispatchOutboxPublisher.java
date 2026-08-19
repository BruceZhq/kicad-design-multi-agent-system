package dev.ratsnest.core;

import org.springframework.data.domain.PageRequest;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;

@Service
public class DispatchOutboxPublisher {

    private final DispatchOutboxRepository outbox;
    private final RunDispatchService dispatch;

    public DispatchOutboxPublisher(DispatchOutboxRepository outbox,
                                   RunDispatchService dispatch) {
        this.outbox = outbox;
        this.dispatch = dispatch;
    }

    @Scheduled(fixedDelayString = "${ratsnest.outbox.poll-ms:1000}")
    @Transactional
    public void publishReady() {
        for (DispatchOutbox event : outbox.lockReady(
                Instant.now(), PageRequest.of(0, 20))) {
            try {
                dispatch.publishKafka(event.getRunId());
                event.markPublished();
            } catch (Exception error) {
                event.retryAfter(error);
            }
            outbox.save(event);
        }
    }
}

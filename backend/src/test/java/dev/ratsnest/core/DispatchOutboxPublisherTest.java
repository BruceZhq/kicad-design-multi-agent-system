package dev.ratsnest.core;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class DispatchOutboxPublisherTest {

    @Test
    void marksAcknowledgedKafkaPublicationComplete() throws Exception {
        DispatchOutboxRepository repository = mock(DispatchOutboxRepository.class);
        RunDispatchService dispatch = mock(RunDispatchService.class);
        DispatchOutbox event = DispatchOutbox.pending("run-1");
        when(repository.lockReady(any(), any())).thenReturn(List.of(event));

        new DispatchOutboxPublisher(repository, dispatch).publishReady();

        verify(dispatch).publishKafka("run-1");
        verify(repository).save(event);
        assertThat(event.getStatus()).isEqualTo("published");
        assertThat(event.getPublishedAt()).isNotNull();
    }

    @Test
    void retainsFailedPublicationForBoundedRetry() throws Exception {
        DispatchOutboxRepository repository = mock(DispatchOutboxRepository.class);
        RunDispatchService dispatch = mock(RunDispatchService.class);
        DispatchOutbox event = DispatchOutbox.pending("run-2");
        when(repository.lockReady(any(), any())).thenReturn(List.of(event));
        doThrow(new IllegalStateException("broker unavailable"))
                .when(dispatch).publishKafka(eq("run-2"));

        new DispatchOutboxPublisher(repository, dispatch).publishReady();

        assertThat(event.getStatus()).isEqualTo("pending");
        assertThat(event.getAttempts()).isEqualTo(1);
        assertThat(event.getLastError()).contains("broker unavailable");
    }
}

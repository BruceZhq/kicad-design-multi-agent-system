package dev.ratsnest.core;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

import java.util.concurrent.Executor;
import java.util.concurrent.ThreadPoolExecutor;

/** Bounded dispatch pool: local runs are CPU/IO heavy Python processes, so
 *  concurrency is capped and overflow degrades gracefully (caller-runs)
 *  instead of exhausting memory. Kafka mode barely touches this pool. */
@Configuration
public class AsyncConfig {

    @Value("${ratsnest.dispatch.pool-size:4}")
    private int poolSize;

    @Value("${ratsnest.dispatch.queue-capacity:100}")
    private int queueCapacity;

    @Bean(name = "taskExecutor")
    public Executor taskExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(poolSize);
        executor.setMaxPoolSize(poolSize * 4);
        executor.setQueueCapacity(queueCapacity);
        executor.setThreadNamePrefix("dispatch-");
        executor.setRejectedExecutionHandler(
                new ThreadPoolExecutor.CallerRunsPolicy());
        executor.initialize();
        return executor;
    }
}

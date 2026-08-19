package dev.ratsnest.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/** Per-client rate limiting with two backends:
 *    memory (default) — token bucket per node, zero deps
 *    redis            — fixed-window INCR/EXPIRE shared across ALL nodes,
 *                       so horizontally scaled backends enforce one limit.
 *  Redis errors fail open to the in-memory bucket (availability over
 *  strictness — flip if your threat model differs). */
@Component
public class RateLimitFilter extends OncePerRequestFilter {

    @Value("${ratsnest.ratelimit.per-second:25}")
    private double refillPerSecond;

    @Value("${ratsnest.ratelimit.burst:50}")
    private double burst;

    @Value("${ratsnest.ratelimit.mode:memory}")
    private String mode;

    private final org.springframework.beans.factory.ObjectProvider<
            org.springframework.data.redis.core.StringRedisTemplate> redis;

    public RateLimitFilter(org.springframework.beans.factory.ObjectProvider<
            org.springframework.data.redis.core.StringRedisTemplate> redis) {
        this.redis = redis;
    }

    private static final class Bucket {
        volatile double tokens;
        final AtomicLong lastNanos = new AtomicLong(System.nanoTime());
        Bucket(double tokens) { this.tokens = tokens; }
    }

    private final Map<String, Bucket> buckets = new ConcurrentHashMap<>();

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain chain)
            throws ServletException, IOException {
        String path = request.getRequestURI();
        if (!path.startsWith("/api/") || path.equals("/api/health")) {
            chain.doFilter(request, response);
            return;
        }
        String client = clientKey(request);
        if ("redis".equalsIgnoreCase(mode)) {
            Boolean allowed = tryRedis(client);
            if (allowed != null) {           // redis reachable: verdict is final
                if (!allowed) { reject(response); }
                else { chain.doFilter(request, response); }
                return;
            }                                 // else fall through to memory
        }
        if (buckets.size() > 10_000) {
            buckets.clear(); // crude cap against key-space abuse
        }
        Bucket bucket = buckets.computeIfAbsent(client,
                k -> new Bucket(burst));
        synchronized (bucket) {
            long now = System.nanoTime();
            double elapsed = (now - bucket.lastNanos.getAndSet(now)) / 1e9;
            bucket.tokens = Math.min(burst,
                    bucket.tokens + elapsed * refillPerSecond);
            if (bucket.tokens < 1.0) {
                reject(response);
                return;
            }
            bucket.tokens -= 1.0;
        }
        chain.doFilter(request, response);
    }

    /** Fixed one-second window in Redis, shared by every backend node.
     *  Returns null when Redis is unreachable (caller falls back). */
    private Boolean tryRedis(String client) {
        try {
            var template = redis.getObject();
            String key = "rl:" + client + ":" + (System.currentTimeMillis() / 1000);
            Long count = template.opsForValue().increment(key);
            if (count != null && count == 1L) {
                template.expire(key, java.time.Duration.ofSeconds(2));
            }
            return count != null && count <= (long) Math.max(refillPerSecond, 1);
        } catch (Exception e) {
            return null;
        }
    }

    private void reject(HttpServletResponse response) throws IOException {
        response.setStatus(429);
        response.setContentType("application/json");
        response.getWriter().write(
                "{\"type\":\"about:blank\",\"title\":\"Too Many Requests\","
                + "\"status\":429,\"detail\":\"rate limit exceeded\"}");
    }

    private String clientKey(HttpServletRequest request) {
        String forwarded = request.getHeader("X-Forwarded-For");
        if (forwarded != null && !forwarded.isBlank()) {
            return forwarded.split(",")[0].trim();
        }
        return request.getRemoteAddr();
    }
}

package team.ratsnest.controlplane.agentgateway.application;

import java.net.URI;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import team.ratsnest.controlplane.agentgateway.domain.model.AgentRuntimeException;
import tools.jackson.databind.ObjectMapper;

/** Immutable version -> deployment mapping, independent of mutable release channels. */
@Component
public class RuntimeVersionRoutes {
    private final Map<String, Object> endpoints;

    @SuppressWarnings("unchecked")
    public RuntimeVersionRoutes(ObjectMapper mapper,
            @Value("${ratsnest.agent-runtime.version-endpoints:{}}") String value) {
        endpoints = Map.copyOf(mapper.readValue(value == null || value.isBlank() ? "{}" : value, Map.class));
        for (String id : endpoints.keySet()) {
            if (!id.matches("[A-Za-z0-9._:-]{1,120}")) { throw new IllegalArgumentException("Invalid runtime version ID"); }
            requireVersion(id);
        }
    }

    public Map<?, ?> requireVersion(String id) {
        if (!(endpoints.get(id) instanceof Map<?, ?> endpoint) || !(endpoint.get("http") instanceof String http)) {
            throw new AgentRuntimeException(503, "Version-pinned Runtime deployment is not configured: " + id);
        }
        URI uri = URI.create(http);
        if (!("http".equals(uri.getScheme()) || "https".equals(uri.getScheme()))
                || uri.getHost() == null || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null) {
            throw new IllegalArgumentException("Runtime endpoints require a plain HTTP(S) origin");
        }
        return endpoint;
    }

    public Map<?, ?> endpoint(String selector) {
        String[] parts = selector == null ? new String[0] : selector.split("@", 2);
        if (parts.length < 2 || !endpoints.containsKey(parts[1])) { return null; }
        return requireVersion(parts[1]);
    }

    public static String selector(Map<String, Object> config) {
        if (config.get("harness_version") instanceof Map<?, ?> values) {
            String channel = "canary".equals(values.get("channel")) ? "canary" : "stable";
            return values.get("id") instanceof String id ? channel + "@" + id : channel;
        }
        return "stable";
    }

    public static boolean canary(String selector) {
        return selector != null && (selector.equals("canary") || selector.startsWith("canary@"));
    }
}

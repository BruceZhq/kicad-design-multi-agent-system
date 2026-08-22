package team.ratsnest.controlplane.bootstrap;

import java.util.Locale;
import java.util.Set;

import org.springframework.beans.factory.InitializingBean;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(name = "ratsnest.run-outbox.enabled", havingValue = "true")
final class KafkaSecurityConfigurationGuard implements InitializingBean {

    private static final Set<String> PROTOCOLS = Set.of(
            "PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL");

    private final String protocol;
    private final String mechanism;
    private final String jaasConfig;
    private final String endpointIdentificationAlgorithm;

    KafkaSecurityConfigurationGuard(
            @Value("${spring.kafka.properties.security.protocol:PLAINTEXT}") String protocol,
            @Value("${spring.kafka.properties.sasl.mechanism:}") String mechanism,
            @Value("${spring.kafka.properties.sasl.jaas.config:}") String jaasConfig,
            @Value("${spring.kafka.properties.ssl.endpoint.identification.algorithm:HTTPS}")
                    String endpointIdentificationAlgorithm) {
        this.protocol = normalized(protocol);
        this.mechanism = mechanism == null ? "" : mechanism.trim();
        this.jaasConfig = jaasConfig == null ? "" : jaasConfig.trim();
        this.endpointIdentificationAlgorithm = normalized(endpointIdentificationAlgorithm);
    }

    @Override
    public void afterPropertiesSet() {
        if (!PROTOCOLS.contains(protocol)) {
            throw new IllegalStateException("Unsupported Kafka security protocol");
        }
        if (protocol.startsWith("SASL_") && (mechanism.isEmpty() || jaasConfig.isEmpty())) {
            throw new IllegalStateException(
                    "Kafka SASL requires a mechanism and JAAS credentials");
        }
        if (("SSL".equals(protocol) || "SASL_SSL".equals(protocol))
                && !"HTTPS".equals(endpointIdentificationAlgorithm)) {
            throw new IllegalStateException(
                    "Kafka TLS hostname verification must remain enabled");
        }
    }

    private static String normalized(String value) {
        return value == null ? "" : value.trim().toUpperCase(Locale.ROOT);
    }
}

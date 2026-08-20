package team.ratsnest.controlplane.run.infrastructure.messaging;

import java.util.LinkedHashMap;
import java.util.Map;

import org.apache.kafka.clients.CommonClientConfigs;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.config.SaslConfigs;
import org.apache.kafka.common.config.SslConfigs;
import org.apache.kafka.common.serialization.StringSerializer;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.DefaultKafkaProducerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.core.ProducerFactory;

@Configuration(proxyBeanMethods = false)
@ConditionalOnProperty(name = "ratsnest.run-outbox.enabled", havingValue = "true")
class RunOutboxKafkaConfiguration {

    @Bean
    ProducerFactory<String, String> runOutboxProducerFactory(
            @Value("${spring.kafka.bootstrap-servers}") String bootstrapServers,
            @Value("${spring.kafka.properties.security.protocol:PLAINTEXT}") String securityProtocol,
            @Value("${spring.kafka.properties.sasl.mechanism:}") String saslMechanism,
            @Value("${spring.kafka.properties.sasl.jaas.config:}") String saslJaasConfig,
            @Value("${spring.kafka.properties.ssl.endpoint.identification.algorithm:HTTPS}")
                    String endpointIdentificationAlgorithm) {
        Map<String, Object> properties = new LinkedHashMap<>();
        properties.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        properties.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        properties.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        properties.put(ProducerConfig.ACKS_CONFIG, "all");
        properties.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);
        properties.put(ProducerConfig.MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION, 5);
        properties.put(CommonClientConfigs.SECURITY_PROTOCOL_CONFIG, securityProtocol);
        if (securityProtocol.startsWith("SASL_")) {
            properties.put(SaslConfigs.SASL_MECHANISM, saslMechanism);
            properties.put(SaslConfigs.SASL_JAAS_CONFIG, saslJaasConfig);
        }
        if (securityProtocol.endsWith("SSL")) {
            properties.put(
                    SslConfigs.SSL_ENDPOINT_IDENTIFICATION_ALGORITHM_CONFIG,
                    endpointIdentificationAlgorithm);
        }
        return new DefaultKafkaProducerFactory<>(properties);
    }

    @Bean
    KafkaTemplate<String, String> runOutboxKafkaTemplate(
            ProducerFactory<String, String> runOutboxProducerFactory) {
        return new KafkaTemplate<>(runOutboxProducerFactory);
    }
}

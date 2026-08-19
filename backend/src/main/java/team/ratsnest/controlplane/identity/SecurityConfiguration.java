package team.ratsnest.controlplane.identity;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.oauth2.server.resource.web.access.BearerTokenAccessDeniedHandler;
import org.springframework.security.oauth2.server.resource.web.BearerTokenAuthenticationEntryPoint;
import org.springframework.security.web.SecurityFilterChain;

@Configuration(proxyBeanMethods = false)
public class SecurityConfiguration {

    @Bean
    SecurityFilterChain securityFilterChain(
            HttpSecurity http,
            SecurityProblemWriter problemWriter) throws Exception {
        BearerTokenAuthenticationEntryPoint bearerEntryPoint = new BearerTokenAuthenticationEntryPoint();
        BearerTokenAccessDeniedHandler bearerDeniedHandler = new BearerTokenAccessDeniedHandler();
        return http
                .csrf(AbstractHttpConfigurer::disable)
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(authorize -> authorize
                        .requestMatchers(
                                "/actuator/health/liveness",
                                "/actuator/health/readiness")
                        .permitAll()
                        .requestMatchers("/internal/v1/evolution/trials/*/result")
                        .permitAll()
                        .requestMatchers("/api/**")
                        .authenticated()
                        .anyRequest()
                        .denyAll())
                .exceptionHandling(exceptions -> exceptions
                        .authenticationEntryPoint((request, response, exception) -> {
                            bearerEntryPoint.commence(request, response, exception);
                            problemWriter.write(
                                    request,
                                    response,
                                    org.springframework.http.HttpStatus.UNAUTHORIZED,
                                    "AUTHENTICATION_REQUIRED",
                                    "A valid bearer token is required.");
                        })
                        .accessDeniedHandler((request, response, exception) -> {
                            bearerDeniedHandler.handle(request, response, exception);
                            problemWriter.write(
                                    request,
                                    response,
                                    org.springframework.http.HttpStatus.FORBIDDEN,
                                    "ACCESS_DENIED",
                                    "The authenticated principal is not allowed to perform this operation.");
                        }))
                .oauth2ResourceServer(resourceServer -> resourceServer
                        .jwt(Customizer.withDefaults())
                        .authenticationEntryPoint((request, response, exception) -> {
                            bearerEntryPoint.commence(request, response, exception);
                            problemWriter.write(
                                    request,
                                    response,
                                    org.springframework.http.HttpStatus.UNAUTHORIZED,
                                    "AUTHENTICATION_REQUIRED",
                                    "A valid bearer token is required.");
                        }))
                .build();
    }
}

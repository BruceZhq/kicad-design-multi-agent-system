package team.ratsnest.controlplane.tenancy.infrastructure.persistence;

import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

@Component
public final class PostgresTenantContext implements TenantContext {

    private final JdbcClient jdbcClient;

    public PostgresTenantContext(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    public void activate(UUID tenantId) {
        requireTransaction();
        jdbcClient.sql("select set_config('ratsnest.tenant_id', :tenantId, true)")
                .param("tenantId", tenantId.toString())
                .query(String.class)
                .single();
    }

    public void activatePrincipal(AuthenticatedActor actor) {
        requireTransaction();
        jdbcClient.sql("select set_config('ratsnest.principal_issuer', :issuer, true)")
                .param("issuer", actor.issuer())
                .query(String.class)
                .single();
        jdbcClient.sql("select set_config('ratsnest.principal_subject', :subject, true)")
                .param("subject", actor.subject())
                .query(String.class)
                .single();
    }

    private void requireTransaction() {
        if (!TransactionSynchronizationManager.isActualTransactionActive()) {
            throw new IllegalStateException("Tenant context requires an active transaction");
        }
    }
}

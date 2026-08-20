package team.ratsnest.controlplane.organization.infrastructure.persistence;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.Optional;
import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.organization.domain.model.Organization;
import team.ratsnest.controlplane.organization.domain.port.OrganizationStore;

@Repository
public class JdbcOrganizationStore implements OrganizationStore {

    private final JdbcClient jdbcClient;

    public JdbcOrganizationStore(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    public void insert(UUID tenantId, String name, AuthenticatedActor actor) {
        jdbcClient.sql("""
                        insert into control_plane.organizations (
                            tenant_id, name, created_by_issuer, created_by_subject
                        ) values (
                            :tenantId, :name, :issuer, :subject
                        )
                        """)
                .param("tenantId", tenantId)
                .param("name", name)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .update();
    }

    public Optional<Organization> find(UUID tenantId) {
        return jdbcClient.sql("""
                        select tenant_id, name, created_at, updated_at
                        from control_plane.organizations
                        where tenant_id = :tenantId
                        """)
                .param("tenantId", tenantId)
                .query((resultSet, rowNumber) -> new Organization(
                        resultSet.getObject("tenant_id", UUID.class),
                        resultSet.getString("name"),
                        instant(resultSet, "created_at"),
                        instant(resultSet, "updated_at")))
                .optional();
    }

    private static Instant instant(java.sql.ResultSet resultSet, String column) throws java.sql.SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }
}

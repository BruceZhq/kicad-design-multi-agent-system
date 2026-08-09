package team.ratsnest.controlplane.tenancy;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.identity.AuthenticatedActor;

@Repository
public class MembershipRepository {

    private final JdbcClient jdbcClient;

    public MembershipRepository(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    public Optional<MembershipRole> findRole(UUID tenantId, AuthenticatedActor actor) {
        return jdbcClient.sql("""
                        select membership_role
                        from control_plane.memberships
                        where tenant_id = :tenantId
                          and issuer = :issuer
                          and subject = :subject
                        """)
                .param("tenantId", tenantId)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .query(String.class)
                .optional()
                .map(MembershipRole::fromWireValue);
    }

    public Optional<MembershipRole> findRole(
            UUID tenantId,
            String issuer,
            String subject) {
        return findRole(tenantId, new AuthenticatedActor(issuer, subject));
    }

    public List<UUID> findTenantIds(AuthenticatedActor actor) {
        return jdbcClient.sql("""
                        select tenant_id
                        from control_plane.memberships
                        where issuer = :issuer and subject = :subject
                        order by created_at, tenant_id
                        """)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .query(UUID.class)
                .list();
    }

    public void upsert(UUID tenantId, AuthenticatedActor actor, MembershipRole role) {
        jdbcClient.sql("""
                        insert into control_plane.memberships (
                            tenant_id, issuer, subject, membership_role
                        ) values (
                            :tenantId, :issuer, :subject, :role
                        )
                        on conflict (tenant_id, issuer, subject)
                        do update set
                            membership_role = excluded.membership_role,
                            updated_at = now()
                        """)
                .param("tenantId", tenantId)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .param("role", role.wireValue())
                .update();
    }

    public List<Membership> findAll(UUID tenantId) {
        return jdbcClient.sql("""
                        select issuer, subject, membership_role, created_at, updated_at
                        from control_plane.memberships
                        where tenant_id = :tenantId
                        order by created_at, issuer, subject
                        """)
                .param("tenantId", tenantId)
                .query((resultSet, rowNumber) -> new Membership(
                        resultSet.getString("issuer"),
                        resultSet.getString("subject"),
                        MembershipRole.fromWireValue(resultSet.getString("membership_role")),
                        instant(resultSet, "created_at"),
                        instant(resultSet, "updated_at")))
                .list();
    }

    public record Membership(
            String issuer,
            String subject,
            MembershipRole role,
            Instant createdAt,
            Instant updatedAt) {
    }

    private static Instant instant(java.sql.ResultSet resultSet, String column) throws java.sql.SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }
}

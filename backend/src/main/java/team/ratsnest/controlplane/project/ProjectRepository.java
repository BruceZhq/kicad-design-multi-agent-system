package team.ratsnest.controlplane.project;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.identity.AuthenticatedActor;

@Repository
class ProjectRepository {

    private final JdbcClient jdbcClient;

    ProjectRepository(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    void insert(
            UUID tenantId,
            UUID projectId,
            String name,
            String description,
            AuthenticatedActor actor) {
        jdbcClient.sql("""
                        insert into control_plane.projects (
                            tenant_id, project_id, name, description,
                            created_by_issuer, created_by_subject
                        ) values (
                            :tenantId, :projectId, :name, :description,
                            :issuer, :subject
                        )
                        """)
                .param("tenantId", tenantId)
                .param("projectId", projectId)
                .param("name", name)
                .param("description", description)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .update();
    }

    List<Project> findAll(UUID tenantId) {
        return jdbcClient.sql("""
                        select tenant_id, project_id, name, description, created_at, updated_at
                        from control_plane.projects
                        where tenant_id = :tenantId
                        order by created_at, project_id
                        """)
                .param("tenantId", tenantId)
                .query(ProjectRepository::map)
                .list();
    }

    Optional<Project> find(UUID tenantId, UUID projectId) {
        return jdbcClient.sql("""
                        select tenant_id, project_id, name, description, created_at, updated_at
                        from control_plane.projects
                        where tenant_id = :tenantId and project_id = :projectId
                        """)
                .param("tenantId", tenantId)
                .param("projectId", projectId)
                .query(ProjectRepository::map)
                .optional();
    }

    int update(UUID tenantId, UUID projectId, String name, String description) {
        return jdbcClient.sql("""
                        update control_plane.projects
                        set name = :name,
                            description = :description,
                            updated_at = now()
                        where tenant_id = :tenantId and project_id = :projectId
                        """)
                .param("tenantId", tenantId)
                .param("projectId", projectId)
                .param("name", name)
                .param("description", description)
                .update();
    }

    private static Project map(java.sql.ResultSet resultSet, int rowNumber) throws java.sql.SQLException {
        return new Project(
                resultSet.getObject("tenant_id", UUID.class),
                resultSet.getObject("project_id", UUID.class),
                resultSet.getString("name"),
                resultSet.getString("description"),
                instant(resultSet, "created_at"),
                instant(resultSet, "updated_at"));
    }

    private static Instant instant(java.sql.ResultSet resultSet, String column) throws java.sql.SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }
}

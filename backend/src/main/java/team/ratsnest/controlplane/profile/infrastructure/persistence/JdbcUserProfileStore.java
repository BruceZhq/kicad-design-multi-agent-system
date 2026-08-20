package team.ratsnest.controlplane.profile.infrastructure.persistence;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.Optional;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.profile.domain.model.ProfileAvatar;
import team.ratsnest.controlplane.profile.domain.model.UserProfile;
import team.ratsnest.controlplane.profile.domain.port.UserProfileStore;

@Repository("userProfileRepository")
public class JdbcUserProfileStore implements UserProfileStore {

    private final JdbcClient jdbcClient;

    public JdbcUserProfileStore(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    @Override
    public Optional<UserProfile> find(AuthenticatedActor actor) {
        return jdbcClient.sql("""
                        select display_name, job_title, bio, locale, time_zone,
                               avatar_object_key, avatar_media_type, avatar_sha256,
                               avatar_size_bytes, version, created_at, updated_at
                        from control_plane.user_profiles
                        where issuer = :issuer and subject = :subject
                        """)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .query(JdbcUserProfileStore::map)
                .optional();
    }

    @Override
    public boolean insert(
            AuthenticatedActor actor,
            String displayName,
            String jobTitle,
            String bio,
            String locale,
            String timeZone) {
        return jdbcClient.sql("""
                        insert into control_plane.user_profiles (
                            issuer, subject, display_name, job_title, bio, locale, time_zone
                        ) values (
                            :issuer, :subject, :displayName, :jobTitle, :bio, :locale, :timeZone
                        )
                        on conflict (issuer, subject) do nothing
                        """)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .param("displayName", displayName)
                .param("jobTitle", jobTitle)
                .param("bio", bio)
                .param("locale", locale)
                .param("timeZone", timeZone)
                .update() == 1;
    }

    @Override
    public boolean update(
            AuthenticatedActor actor,
            long expectedVersion,
            String displayName,
            String jobTitle,
            String bio,
            String locale,
            String timeZone) {
        return jdbcClient.sql("""
                        update control_plane.user_profiles
                        set display_name = :displayName,
                            job_title = :jobTitle,
                            bio = :bio,
                            locale = :locale,
                            time_zone = :timeZone,
                            version = version + 1,
                            updated_at = now()
                        where issuer = :issuer
                          and subject = :subject
                          and version = :expectedVersion
                        """)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .param("expectedVersion", expectedVersion)
                .param("displayName", displayName)
                .param("jobTitle", jobTitle)
                .param("bio", bio)
                .param("locale", locale)
                .param("timeZone", timeZone)
                .update() == 1;
    }

    @Override
    public boolean updateAvatar(
            AuthenticatedActor actor,
            long expectedVersion,
            ProfileAvatar avatar) {
        return jdbcClient.sql("""
                        update control_plane.user_profiles
                        set avatar_object_key = :objectKey,
                            avatar_media_type = :mediaType,
                            avatar_sha256 = :sha256,
                            avatar_size_bytes = :sizeBytes,
                            version = version + 1,
                            updated_at = now()
                        where issuer = :issuer
                          and subject = :subject
                          and version = :expectedVersion
                        """)
                .param("issuer", actor.issuer())
                .param("subject", actor.subject())
                .param("expectedVersion", expectedVersion)
                .param("objectKey", avatar.objectKey())
                .param("mediaType", avatar.mediaType())
                .param("sha256", avatar.sha256())
                .param("sizeBytes", avatar.bytes().length)
                .update() == 1;
    }

    private static UserProfile map(ResultSet resultSet, int rowNumber) throws SQLException {
        Long avatarSize = resultSet.getObject("avatar_size_bytes", Long.class);
        return new UserProfile(
                resultSet.getString("display_name"),
                resultSet.getString("job_title"),
                resultSet.getString("bio"),
                resultSet.getString("locale"),
                resultSet.getString("time_zone"),
                resultSet.getString("avatar_object_key"),
                resultSet.getString("avatar_media_type"),
                resultSet.getString("avatar_sha256"),
                avatarSize,
                resultSet.getLong("version"),
                instant(resultSet, "created_at"),
                instant(resultSet, "updated_at"));
    }

    private static Instant instant(ResultSet resultSet, String column) throws SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }
}

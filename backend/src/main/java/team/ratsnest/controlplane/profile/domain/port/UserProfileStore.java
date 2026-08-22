package team.ratsnest.controlplane.profile.domain.port;

import java.util.Optional;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.profile.domain.model.ProfileAvatar;
import team.ratsnest.controlplane.profile.domain.model.UserProfile;

public interface UserProfileStore {

    Optional<UserProfile> find(AuthenticatedActor actor);

    boolean insert(
            AuthenticatedActor actor,
            String displayName,
            String jobTitle,
            String bio,
            String locale,
            String timeZone);

    boolean update(
            AuthenticatedActor actor,
            long expectedVersion,
            String displayName,
            String jobTitle,
            String bio,
            String locale,
            String timeZone);

    boolean updateAvatar(AuthenticatedActor actor, long expectedVersion, ProfileAvatar avatar);
}

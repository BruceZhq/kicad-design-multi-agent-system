package team.ratsnest.controlplane.profile.domain.port;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.profile.domain.model.AvatarContent;
import team.ratsnest.controlplane.profile.domain.model.ProfileAvatar;
import team.ratsnest.controlplane.profile.domain.model.UserProfile;

public interface ProfileAvatarStore {

    ProfileAvatar store(AuthenticatedActor actor, String mediaType, byte[] bytes);

    AvatarContent read(UserProfile profile);
}

package team.ratsnest.controlplane.profile.application.model;

import java.time.Instant;

import team.ratsnest.controlplane.profile.domain.model.UserProfile;

public record ProfileView(
        String displayName,
        String username,
        String email,
        String jobTitle,
        String bio,
        String locale,
        String timeZone,
        boolean hasAvatar,
        long version,
        Instant createdAt,
        Instant updatedAt) {

    public static ProfileView empty(ProfileIdentity identity) {
        return new ProfileView(
                identity.defaultDisplayName(), identity.username(), identity.email(),
                "", "", "zh-CN", "Asia/Shanghai", false, 0, null, null);
    }

    public static ProfileView from(ProfileIdentity identity, UserProfile profile) {
        return new ProfileView(
                profile.displayName(), identity.username(), identity.email(),
                profile.jobTitle(), profile.bio(), profile.locale(), profile.timeZone(),
                profile.hasAvatar(), profile.version(), profile.createdAt(), profile.updatedAt());
    }
}

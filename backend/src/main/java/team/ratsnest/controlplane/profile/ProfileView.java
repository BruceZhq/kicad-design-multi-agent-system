package team.ratsnest.controlplane.profile;

import java.time.Instant;

record ProfileView(
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

    static ProfileView empty(ProfileIdentity identity) {
        return new ProfileView(
                identity.defaultDisplayName(), identity.username(), identity.email(),
                "", "", "zh-CN", "Asia/Shanghai", false, 0, null, null);
    }

    static ProfileView from(ProfileIdentity identity, UserProfile profile) {
        return new ProfileView(
                profile.displayName(), identity.username(), identity.email(),
                profile.jobTitle(), profile.bio(), profile.locale(), profile.timeZone(),
                profile.hasAvatar(), profile.version(), profile.createdAt(), profile.updatedAt());
    }
}

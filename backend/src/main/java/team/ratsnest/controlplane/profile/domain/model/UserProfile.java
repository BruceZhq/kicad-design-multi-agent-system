package team.ratsnest.controlplane.profile.domain.model;

import java.time.Instant;

public record UserProfile(
        String displayName,
        String jobTitle,
        String bio,
        String locale,
        String timeZone,
        String avatarObjectKey,
        String avatarMediaType,
        String avatarSha256,
        Long avatarSizeBytes,
        long version,
        Instant createdAt,
        Instant updatedAt) {

    public boolean hasAvatar() {
        return avatarObjectKey != null;
    }
}

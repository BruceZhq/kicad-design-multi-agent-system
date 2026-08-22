package team.ratsnest.controlplane.profile.domain.model;

public record ProfileAvatar(
        String objectKey,
        String mediaType,
        String sha256,
        byte[] bytes) {

    public ProfileAvatar {
        bytes = bytes.clone();
    }

    @Override
    public byte[] bytes() {
        return bytes.clone();
    }
}

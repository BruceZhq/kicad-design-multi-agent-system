package team.ratsnest.controlplane.profile.domain.model;

public record AvatarContent(String mediaType, String sha256, byte[] bytes) {

    public AvatarContent {
        bytes = bytes.clone();
    }

    @Override
    public byte[] bytes() {
        return bytes.clone();
    }
}

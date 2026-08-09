package team.ratsnest.controlplane.profile;

record ProfileAvatar(
        String objectKey,
        String mediaType,
        String sha256,
        byte[] bytes) {

    ProfileAvatar {
        bytes = bytes.clone();
    }

    @Override
    public byte[] bytes() {
        return bytes.clone();
    }
}

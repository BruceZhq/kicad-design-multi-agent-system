package team.ratsnest.controlplane.profile.infrastructure.storage;

import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.Locale;
import java.util.Map;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import jakarta.annotation.PreDestroy;
import software.amazon.awssdk.core.ResponseBytes;
import software.amazon.awssdk.core.exception.SdkException;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3ClientBuilder;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.profile.domain.model.AvatarContent;
import team.ratsnest.controlplane.profile.domain.model.ProfileAvatar;
import team.ratsnest.controlplane.profile.domain.model.UserProfile;
import team.ratsnest.controlplane.profile.domain.port.ProfileAvatarStore;
import team.ratsnest.controlplane.shared.web.ApiException;

@Component("profileAvatarStorage")
public final class S3ProfileAvatarStore implements ProfileAvatarStore {

    static final int MAX_BYTES = 2 * 1024 * 1024;

    private static final Map<String, String> EXTENSIONS = Map.of(
            "image/jpeg", "jpg",
            "image/png", "png",
            "image/webp", "webp");

    private final String bucket;
    private final S3Client client;

    public S3ProfileAvatarStore(
            @Value("${ratsnest.artifacts.bucket:}") String bucket,
            @Value("${ratsnest.artifacts.region:us-east-1}") String region,
            @Value("${ratsnest.artifacts.internal-endpoint:${ratsnest.artifacts.endpoint:}}") String endpoint,
            @Value("${ratsnest.artifacts.path-style:true}") boolean pathStyle) {
        this.bucket = bucket.strip();
        S3ClientBuilder builder = S3Client.builder()
                .region(Region.of(region))
                .serviceConfiguration(S3Configuration.builder()
                        .pathStyleAccessEnabled(pathStyle)
                        .build());
        if (!endpoint.isBlank()) {
            builder.endpointOverride(URI.create(endpoint));
        }
        this.client = builder.build();
    }

    @Override
    public ProfileAvatar store(AuthenticatedActor actor, String mediaType, byte[] bytes) {
        String normalizedType = mediaType == null
                ? ""
                : mediaType.strip().toLowerCase(Locale.ROOT);
        String extension = EXTENSIONS.get(normalizedType);
        if (extension == null || bytes.length == 0 || bytes.length > MAX_BYTES
                || !matchesSignature(normalizedType, bytes)) {
            throw new ApiException(
                    "PROFILE_AVATAR_INVALID",
                    HttpStatus.BAD_REQUEST,
                    "Avatar must be a JPEG, PNG, or WebP image no larger than 2 MiB.");
        }
        requireConfigured();
        String sha256 = sha256(bytes);
        String principal = sha256((actor.issuer() + "\0" + actor.subject())
                .getBytes(StandardCharsets.UTF_8));
        String objectKey = "profiles/" + principal + "/avatars/" + sha256 + "." + extension;
        try {
            client.putObject(
                    PutObjectRequest.builder()
                            .bucket(bucket)
                            .key(objectKey)
                            .contentType(normalizedType)
                            .metadata(Map.of("sha256", sha256))
                            .build(),
                    RequestBody.fromBytes(bytes));
        } catch (SdkException exception) {
            throw unavailable();
        }
        return new ProfileAvatar(objectKey, normalizedType, sha256, bytes);
    }

    @Override
    public AvatarContent read(UserProfile profile) {
        if (!profile.hasAvatar()) {
            throw new ApiException(
                    "PROFILE_AVATAR_NOT_FOUND",
                    HttpStatus.NOT_FOUND,
                    "The user profile does not have an avatar.");
        }
        requireConfigured();
        try {
            ResponseBytes<GetObjectResponse> response = client.getObjectAsBytes(
                    GetObjectRequest.builder()
                            .bucket(bucket)
                            .key(profile.avatarObjectKey())
                            .build());
            byte[] bytes = response.asByteArray();
            if (bytes.length != profile.avatarSizeBytes() || !sha256(bytes).equals(profile.avatarSha256())) {
                throw new ApiException(
                        "PROFILE_AVATAR_CORRUPT",
                        HttpStatus.BAD_GATEWAY,
                        "The stored avatar failed integrity verification.");
            }
            return new AvatarContent(profile.avatarMediaType(), profile.avatarSha256(), bytes);
        } catch (SdkException exception) {
            throw unavailable();
        }
    }

    @PreDestroy
    void close() {
        client.close();
    }

    private void requireConfigured() {
        if (bucket.isBlank()) {
            throw unavailable();
        }
    }

    private ApiException unavailable() {
        return new ApiException(
                "PROFILE_AVATAR_STORAGE_UNAVAILABLE",
                HttpStatus.SERVICE_UNAVAILABLE,
                "Avatar storage is not configured or unavailable.");
    }

    static boolean matchesSignature(String mediaType, byte[] bytes) {
        return switch (mediaType) {
            case "image/jpeg" -> bytes.length >= 3
                    && unsigned(bytes[0]) == 0xff
                    && unsigned(bytes[1]) == 0xd8
                    && unsigned(bytes[2]) == 0xff;
            case "image/png" -> bytes.length >= 8
                    && unsigned(bytes[0]) == 0x89
                    && unsigned(bytes[1]) == 0x50
                    && unsigned(bytes[2]) == 0x4e
                    && unsigned(bytes[3]) == 0x47
                    && unsigned(bytes[4]) == 0x0d
                    && unsigned(bytes[5]) == 0x0a
                    && unsigned(bytes[6]) == 0x1a
                    && unsigned(bytes[7]) == 0x0a;
            case "image/webp" -> bytes.length >= 12
                    && ascii(bytes, 0, "RIFF")
                    && ascii(bytes, 8, "WEBP");
            default -> false;
        };
    }

    private static boolean ascii(byte[] bytes, int offset, String expected) {
        for (int index = 0; index < expected.length(); index++) {
            if (bytes[offset + index] != (byte) expected.charAt(index)) return false;
        }
        return true;
    }

    private static int unsigned(byte value) {
        return value & 0xff;
    }

    private static String sha256(byte[] value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to hash profile avatar", exception);
        }
    }
}

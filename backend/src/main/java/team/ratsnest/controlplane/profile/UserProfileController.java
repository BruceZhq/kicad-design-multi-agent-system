package team.ratsnest.controlplane.profile;

import java.io.IOException;
import java.time.Duration;
import java.time.Instant;

import org.springframework.http.CacheControl;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;
import team.ratsnest.controlplane.shared.web.ApiException;

@RestController
@Validated
@RequestMapping("/api/v1/me/profile")
public class UserProfileController {

    private final UserProfileService profiles;

    public UserProfileController(UserProfileService profiles) {
        this.profiles = profiles;
    }

    @GetMapping
    ProfileResponse get(@AuthenticationPrincipal Jwt jwt) {
        return ProfileResponse.from(profiles.get(ProfileIdentity.from(jwt)));
    }

    @PutMapping
    ProfileResponse update(
            @Valid @RequestBody UpdateProfileRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return ProfileResponse.from(profiles.update(
                ProfileIdentity.from(jwt),
                request.version(),
                request.displayName(),
                request.jobTitle(),
                request.bio(),
                request.locale(),
                request.timeZone()));
    }

    @PutMapping(value = "/avatar", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    ProfileResponse updateAvatar(
            @RequestParam("version") @PositiveOrZero long version,
            @RequestPart("file") MultipartFile file,
            @AuthenticationPrincipal Jwt jwt) {
        try {
            return ProfileResponse.from(profiles.updateAvatar(
                    ProfileIdentity.from(jwt), version, file.getContentType(), file.getBytes()));
        } catch (IOException exception) {
            throw new ApiException(
                    "PROFILE_AVATAR_INVALID",
                    HttpStatus.BAD_REQUEST,
                    "The avatar upload could not be read.");
        }
    }

    @GetMapping("/avatar")
    ResponseEntity<byte[]> avatar(@AuthenticationPrincipal Jwt jwt) {
        ProfileAvatarStorage.AvatarContent avatar = profiles.avatar(ProfileIdentity.from(jwt));
        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType(avatar.mediaType()))
                .cacheControl(CacheControl.maxAge(Duration.ofMinutes(5)).cachePrivate())
                .eTag("\"" + avatar.sha256() + "\"")
                .body(avatar.bytes());
    }

    record UpdateProfileRequest(
            @NotNull @PositiveOrZero Long version,
            @NotBlank @Size(max = 120) String displayName,
            @NotNull @Size(max = 120) String jobTitle,
            @NotNull @Size(max = 1000) String bio,
            @NotBlank @Size(max = 35) String locale,
            @NotBlank @Size(max = 64) String timeZone) {
    }

    record ProfileResponse(
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

        static ProfileResponse from(ProfileView profile) {
            return new ProfileResponse(
                    profile.displayName(), profile.username(), profile.email(),
                    profile.jobTitle(), profile.bio(), profile.locale(), profile.timeZone(),
                    profile.hasAvatar(), profile.version(), profile.createdAt(), profile.updatedAt());
        }
    }
}

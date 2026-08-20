package team.ratsnest.controlplane.profile.application;

import java.time.DateTimeException;
import java.time.ZoneId;
import java.util.IllformedLocaleException;
import java.util.Locale;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.profile.application.model.ProfileIdentity;
import team.ratsnest.controlplane.profile.application.model.ProfileView;
import team.ratsnest.controlplane.profile.domain.model.AvatarContent;
import team.ratsnest.controlplane.profile.domain.model.ProfileAvatar;
import team.ratsnest.controlplane.profile.domain.model.UserProfile;
import team.ratsnest.controlplane.profile.domain.port.ProfileAvatarStore;
import team.ratsnest.controlplane.profile.domain.port.UserProfileStore;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

@Service
public class UserProfileService {

    private final TenantContext tenantContext;
    private final UserProfileStore profiles;
    private final ProfileAvatarStore avatars;
    private final TransactionTemplate transactions;

    public UserProfileService(
            TenantContext tenantContext,
            UserProfileStore profiles,
            ProfileAvatarStore avatars,
            TransactionTemplate transactions) {
        this.tenantContext = tenantContext;
        this.profiles = profiles;
        this.avatars = avatars;
        this.transactions = transactions;
    }

    @Transactional(readOnly = true)
    public ProfileView get(ProfileIdentity identity) {
        tenantContext.activatePrincipal(identity.actor());
        return profiles.find(identity.actor())
                .map(profile -> ProfileView.from(identity, profile))
                .orElseGet(() -> ProfileView.empty(identity));
    }

    @Transactional
    public ProfileView update(
            ProfileIdentity identity,
            long expectedVersion,
            String displayName,
            String jobTitle,
            String bio,
            String locale,
            String timeZone) {
        tenantContext.activatePrincipal(identity.actor());
        String normalizedName = displayName.strip();
        String normalizedTitle = jobTitle.strip();
        String normalizedBio = bio.strip();
        String normalizedLocale = validateLocale(locale.strip());
        String normalizedTimeZone = validateTimeZone(timeZone.strip());

        boolean saved = expectedVersion == 0
                ? profiles.insert(
                        identity.actor(), normalizedName, normalizedTitle, normalizedBio,
                        normalizedLocale, normalizedTimeZone)
                : profiles.update(
                        identity.actor(), expectedVersion, normalizedName, normalizedTitle,
                        normalizedBio, normalizedLocale, normalizedTimeZone);
        if (!saved) throw versionConflict();
        return ProfileView.from(identity, profiles.find(identity.actor()).orElseThrow());
    }

    public ProfileView updateAvatar(
            ProfileIdentity identity,
            long expectedVersion,
            String mediaType,
            byte[] bytes) {
        ProfileAvatar avatar = avatars.store(identity.actor(), mediaType, bytes);
        ProfileView result = transactions.execute(status -> saveAvatar(identity, expectedVersion, avatar));
        if (result == null) throw new IllegalStateException("Profile avatar transaction returned no result");
        return result;
    }

    public AvatarContent avatar(ProfileIdentity identity) {
        UserProfile profile = transactions.execute(status -> {
            tenantContext.activatePrincipal(identity.actor());
            return profiles.find(identity.actor()).orElseThrow(() -> new ApiException(
                    "PROFILE_AVATAR_NOT_FOUND",
                    HttpStatus.NOT_FOUND,
                    "The user profile does not have an avatar."));
        });
        if (profile == null) throw new IllegalStateException("Profile lookup returned no result");
        return avatars.read(profile);
    }

    private ProfileView saveAvatar(
            ProfileIdentity identity,
            long expectedVersion,
            ProfileAvatar avatar) {
        tenantContext.activatePrincipal(identity.actor());
        long profileVersion = expectedVersion;
        if (expectedVersion == 0) {
            if (!profiles.insert(
                    identity.actor(), identity.defaultDisplayName(), "", "", "zh-CN", "Asia/Shanghai")) {
                throw versionConflict();
            }
            profileVersion = 1;
        }
        if (!profiles.updateAvatar(identity.actor(), profileVersion, avatar)) {
            throw versionConflict();
        }
        return ProfileView.from(identity, profiles.find(identity.actor()).orElseThrow());
    }

    private static String validateLocale(String value) {
        if (value.isEmpty() || value.length() > 35) {
            throw invalid("locale must be a valid IETF language tag.");
        }
        try {
            Locale locale = new Locale.Builder().setLanguageTag(value).build();
            if (locale.getLanguage().isEmpty()) {
                throw invalid("locale must be a valid IETF language tag.");
            }
            return locale.toLanguageTag();
        } catch (IllformedLocaleException exception) {
            throw invalid("locale must be a valid IETF language tag.");
        }
    }

    private static String validateTimeZone(String value) {
        try {
            return ZoneId.of(value).getId();
        } catch (DateTimeException exception) {
            throw invalid("timeZone must be a valid IANA time-zone identifier.");
        }
    }

    private static ApiException invalid(String detail) {
        return new ApiException("PROFILE_INVALID", HttpStatus.BAD_REQUEST, detail);
    }

    private static ApiException versionConflict() {
        return new ApiException(
                "PROFILE_VERSION_CONFLICT",
                HttpStatus.CONFLICT,
                "The user profile changed. Reload it before saving again.");
    }
}

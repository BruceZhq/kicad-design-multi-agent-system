package team.ratsnest.controlplane.tenancy;

import java.util.Locale;

import org.springframework.http.HttpStatus;

import team.ratsnest.controlplane.shared.web.ApiException;

public enum MembershipRole {
    OWNER,
    ADMIN,
    ENGINEER,
    REVIEWER,
    VIEWER;

    public static MembershipRole fromWireValue(String value) {
        try {
            return MembershipRole.valueOf(value.strip().toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException | NullPointerException exception) {
            throw new ApiException(
                    "INVALID_MEMBERSHIP_ROLE",
                    HttpStatus.BAD_REQUEST,
                    "Role must be owner, admin, engineer, reviewer, or viewer.");
        }
    }

    public String wireValue() {
        return name().toLowerCase(Locale.ROOT);
    }

    public boolean canWriteProjects() {
        return this == OWNER || this == ADMIN || this == ENGINEER;
    }

    public boolean canManageMemberships() {
        return this == OWNER || this == ADMIN;
    }

    public boolean canManageEvolution() {
        return this == OWNER || this == ADMIN;
    }
}

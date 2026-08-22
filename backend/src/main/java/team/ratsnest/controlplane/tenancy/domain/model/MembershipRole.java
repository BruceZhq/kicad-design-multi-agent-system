package team.ratsnest.controlplane.tenancy.domain.model;

import java.util.Locale;

public enum MembershipRole {
    OWNER,
    ADMIN,
    ENGINEER,
    REVIEWER,
    VIEWER;

    public static MembershipRole fromWireValue(String value) {
        return MembershipRole.valueOf(value.strip().toUpperCase(Locale.ROOT));
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

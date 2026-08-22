package team.ratsnest.controlplane.run.domain.model;

import java.util.Locale;

public enum DeliveryStatus {
    EXECUTION_BLOCKED,
    DELIVERED_WITH_ISSUES,
    RELEASE_READY;

    public String apiValue() {
        return name().toLowerCase(Locale.ROOT);
    }

    public static DeliveryStatus fromApiValue(String value) {
        return value == null ? null : valueOf(value.toUpperCase(Locale.ROOT));
    }
}

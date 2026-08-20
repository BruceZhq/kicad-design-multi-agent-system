package team.ratsnest.controlplane.tenancy.domain.model;

import java.time.Instant;

public record Membership(
        String issuer,
        String subject,
        MembershipRole role,
        Instant createdAt,
        Instant updatedAt) {
}

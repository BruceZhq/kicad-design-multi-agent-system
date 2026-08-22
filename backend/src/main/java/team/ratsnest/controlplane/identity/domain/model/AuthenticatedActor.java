package team.ratsnest.controlplane.identity.domain.model;

public record AuthenticatedActor(String issuer, String subject) {
}

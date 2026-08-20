package team.ratsnest.controlplane.profile.application.model;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

public record ProfileIdentity(
        AuthenticatedActor actor,
        String username,
        String email,
        String defaultDisplayName) {
}

package team.ratsnest.controlplane.identity.application.model;

import java.util.Set;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

public record PlatformPrincipal(
        AuthenticatedActor actor,
        Set<String> roles,
        Set<String> scopes) {

    public PlatformPrincipal {
        roles = Set.copyOf(roles);
        scopes = Set.copyOf(scopes);
    }
}

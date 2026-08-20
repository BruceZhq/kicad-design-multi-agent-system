package team.ratsnest.controlplane.run.application.model;

import java.util.List;

public record StartRequest(
        String message,
        String model,
        String threadId,
        ProfileSelector capabilityProfile,
        List<TeamMember> teamMembers) {

    public StartRequest {
        teamMembers = teamMembers == null ? List.of() : List.copyOf(teamMembers);
    }
}

package team.ratsnest.controlplane.run.application.model;

import java.util.List;
import java.util.Objects;

public record ForkRequest(
        ProfileSelector capabilityProfile,
        ForkReplayMode replayMode,
        String changeRequest,
        String model,
        String reasoningEffort,
        String visionModel,
        String visionReasoningEffort,
        List<TeamMember> teamMembers) {

    public ForkRequest {
        Objects.requireNonNull(capabilityProfile, "capabilityProfile");
        Objects.requireNonNull(replayMode, "replayMode");
        changeRequest = changeRequest == null || changeRequest.isBlank()
                ? null
                : changeRequest.strip();
        teamMembers = teamMembers == null ? List.of() : List.copyOf(teamMembers);
    }

    public ForkRequest(
            ProfileSelector capabilityProfile,
            ForkReplayMode replayMode,
            String changeRequest,
            String model,
            List<TeamMember> teamMembers) {
        this(capabilityProfile, replayMode, changeRequest, model, null, null, null, teamMembers);
    }
}

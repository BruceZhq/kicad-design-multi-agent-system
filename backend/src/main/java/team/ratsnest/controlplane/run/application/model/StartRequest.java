package team.ratsnest.controlplane.run.application.model;

import java.util.List;
import java.util.Map;

public record StartRequest(
        String message,
        String model,
        String reasoningEffort,
        String visionModel,
        String visionReasoningEffort,
        String threadId,
        ProfileSelector capabilityProfile,
        List<TeamMember> teamMembers,
        String agentId,
        Map<String, String> evaluationContext) {

    public StartRequest {
        teamMembers = teamMembers == null ? List.of() : List.copyOf(teamMembers);
        evaluationContext = evaluationContext == null
                ? Map.of()
                : Map.copyOf(evaluationContext);
    }

    public StartRequest(
            String message,
            String model,
            String threadId,
            ProfileSelector capabilityProfile,
            List<TeamMember> teamMembers) {
        this(message, model, null, null, null, threadId, capabilityProfile, teamMembers, null, Map.of());
    }
}

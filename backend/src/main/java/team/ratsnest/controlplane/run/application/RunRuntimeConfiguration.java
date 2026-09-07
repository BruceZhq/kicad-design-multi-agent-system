package team.ratsnest.controlplane.run.application;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.stereotype.Component;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.CapabilityProfile;
import team.ratsnest.controlplane.harness.application.HarnessReleaseRouter.HarnessSelection;
import team.ratsnest.controlplane.harness.domain.model.HarnessVersion;
import team.ratsnest.controlplane.run.application.model.TeamMember;

/** Builds the immutable, persisted Runtime configuration snapshot for a run. */
@Component
class RunRuntimeConfiguration {

    Map<String, Object> create(
            List<TeamMember> teamMembers,
            CapabilityProfile profile,
            HarnessSelection harness) {
        return create(teamMembers, profile, harness, null, Map.of());
    }

    Map<String, Object> create(
            List<TeamMember> teamMembers,
            CapabilityProfile profile,
            HarnessSelection harness,
            String agentId,
            Map<String, String> evaluationContext) {
        return create(
                teamMembers, profile, harness, agentId, evaluationContext,
                null, null, null);
    }

    Map<String, Object> create(
            List<TeamMember> teamMembers,
            CapabilityProfile profile,
            HarnessSelection harness,
            String agentId,
            Map<String, String> evaluationContext,
            String reasoningEffort,
            String visionModel,
            String visionReasoningEffort) {
        return create(
                teamMembers,
                profile.id(),
                profile.version(),
                profile.digest(),
                harness.version(),
                harness.channel(),
                agentId,
                evaluationContext,
                reasoningEffort,
                visionModel,
                visionReasoningEffort);
    }

    Map<String, Object> create(
            List<TeamMember> teamMembers,
            String profileId,
            String profileVersion,
            String profileDigest,
            HarnessVersion harness,
            String harnessChannel) {
        return create(
                teamMembers,
                profileId,
                profileVersion,
                profileDigest,
                harness,
                harnessChannel,
                null,
                Map.of(),
                null,
                null,
                null);
    }

    Map<String, Object> create(
            List<TeamMember> teamMembers,
            String profileId,
            String profileVersion,
            String profileDigest,
            HarnessVersion harness,
            String harnessChannel,
            String agentId,
            Map<String, String> evaluationContext) {
        return create(
                teamMembers, profileId, profileVersion, profileDigest, harness,
                harnessChannel, agentId, evaluationContext, null, null, null);
    }

    Map<String, Object> create(
            List<TeamMember> teamMembers,
            String profileId,
            String profileVersion,
            String profileDigest,
            HarnessVersion harness,
            String harnessChannel,
            String agentId,
            Map<String, String> evaluationContext,
            String reasoningEffort,
            String visionModel,
            String visionReasoningEffort) {
        List<Map<String, Object>> members = teamMembers.stream()
                .map(member -> Map.<String, Object>of(
                        "role_id", member.roleId(),
                        "name", member.name(),
                        "responsibility", member.responsibility()))
                .toList();
        Map<String, Object> config = new LinkedHashMap<>();
        config.put("team_members", members);
        config.put("capability_profile", Map.of(
                        "id", profileId,
                        "version", profileVersion,
                        "digest", profileDigest));
        config.put("harness_version", Map.of(
                        "id", harness.harnessVersionId(),
                        "version", harness.version(),
                        "manifest_digest", harness.manifestDigest(),
                        "source_commit", harness.sourceCommit(),
                        "source_tree_digest", harness.sourceTreeDigest(),
                        "bundle_digest", harness.bundleDigest(),
                        "contract_digest", harness.contractDigest(),
                        "policy_digest", harness.policyDigest(),
                        "channel", harnessChannel));
        if (agentId != null) {
            config.put("agent_id", agentId);
        }
        if (evaluationContext != null && !evaluationContext.isEmpty()) {
            config.put("evaluation_context", Map.copyOf(evaluationContext));
        }
        if (reasoningEffort != null) {
            config.put("reasoning_effort", reasoningEffort);
        }
        if (visionModel != null) {
            config.put("vision_model", visionModel);
        }
        if (visionReasoningEffort != null) {
            config.put("vision_reasoning_effort", visionReasoningEffort);
        }
        return Map.copyOf(config);
    }
}

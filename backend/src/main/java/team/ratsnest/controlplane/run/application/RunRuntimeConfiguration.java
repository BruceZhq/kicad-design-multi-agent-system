package team.ratsnest.controlplane.run.application;

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
        return create(
                teamMembers,
                profile.id(),
                profile.version(),
                profile.digest(),
                harness.version(),
                harness.channel());
    }

    Map<String, Object> create(
            List<TeamMember> teamMembers,
            String profileId,
            String profileVersion,
            String profileDigest,
            HarnessVersion harness,
            String harnessChannel) {
        List<Map<String, Object>> members = teamMembers.stream()
                .map(member -> Map.<String, Object>of(
                        "role_id", member.roleId(),
                        "name", member.name(),
                        "responsibility", member.responsibility()))
                .toList();
        return Map.of(
                "team_members", members,
                "capability_profile", Map.of(
                        "id", profileId,
                        "version", profileVersion,
                        "digest", profileDigest),
                "harness_version", Map.of(
                        "id", harness.harnessVersionId(),
                        "version", harness.version(),
                        "manifest_digest", harness.manifestDigest(),
                        "source_commit", harness.sourceCommit(),
                        "source_tree_digest", harness.sourceTreeDigest(),
                        "bundle_digest", harness.bundleDigest(),
                        "contract_digest", harness.contractDigest(),
                        "policy_digest", harness.policyDigest(),
                        "channel", harnessChannel));
    }
}

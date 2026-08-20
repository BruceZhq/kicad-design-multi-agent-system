package team.ratsnest.controlplane.artifact.domain.model;

import java.util.List;
import java.util.UUID;

import team.ratsnest.controlplane.run.domain.model.DeliveryStatus;

public record ArtifactManifest(
        UUID manifestId,
        Long sourceEventSeq,
        DeliveryStatus deliveryStatus,
        String digest,
        boolean trusted,
        List<Artifact> artifacts) {

    public ArtifactManifest {
        artifacts = List.copyOf(artifacts);
    }
}

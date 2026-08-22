package team.ratsnest.controlplane.artifact.domain.port;

import java.net.URI;

import team.ratsnest.controlplane.artifact.domain.model.Artifact;

public interface ArtifactStorage {

    boolean available();

    URI downloadUrl(Artifact artifact);
}

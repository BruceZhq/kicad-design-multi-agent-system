package team.ratsnest.controlplane.artifact.application;

import java.net.URI;
import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.artifact.domain.model.Artifact;
import team.ratsnest.controlplane.artifact.domain.port.ArtifactStorage;
import team.ratsnest.controlplane.artifact.domain.port.ArtifactStore;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.run.application.RunQueryService;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.application.TenantAccess;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

@Service
public final class ArtifactService {

    private final TransactionTemplate transactions;
    private final TenantContext tenantContext;
    private final TenantAccess tenantAccess;
    private final RunQueryService runs;
    private final ArtifactStore artifacts;
    private final ArtifactStorage storage;

    public ArtifactService(
            TransactionTemplate transactions,
            TenantContext tenantContext,
            TenantAccess tenantAccess,
            RunQueryService runs,
            ArtifactStore artifacts,
            ArtifactStorage storage) {
        this.transactions = transactions;
        this.tenantContext = tenantContext;
        this.tenantAccess = tenantAccess;
        this.runs = runs;
        this.artifacts = artifacts;
        this.storage = storage;
    }

    public ArtifactListing list(
            UUID tenantId,
            UUID runId,
            AuthenticatedActor actor) {
        runs.get(tenantId, runId, actor);
        ArtifactListing result = transactions.execute(status -> {
            tenantContext.activate(tenantId);
            return new ArtifactListing(
                    artifacts.findByRun(tenantId, runId),
                    artifacts.isSuperseded(tenantId, runId));
        });
        return result == null ? new ArtifactListing(List.of(), false) : result;
    }

    public URI download(
            UUID tenantId,
            UUID artifactId,
            AuthenticatedActor actor) {
        Artifact artifact = transactions.execute(status -> {
            tenantAccess.requireMembership(tenantId, actor);
            return artifacts.find(tenantId, artifactId).orElseThrow(this::notFound);
        });
        if (artifact == null) {
            throw notFound();
        }
        runs.authorizeRead(tenantId, artifact.runId(), actor);
        if (!storage.available()) {
            throw new ApiException(
                    "ARTIFACT_STORAGE_UNAVAILABLE",
                    HttpStatus.SERVICE_UNAVAILABLE,
                    "Artifact downloads are not configured.");
        }
        return storage.downloadUrl(artifact);
    }

    private ApiException notFound() {
        return new ApiException(
                "ARTIFACT_NOT_FOUND", HttpStatus.NOT_FOUND, "The artifact was not found.");
    }

    public record ArtifactListing(List<Artifact> artifacts, boolean superseded) {

        public ArtifactListing {
            artifacts = List.copyOf(artifacts);
        }
    }
}

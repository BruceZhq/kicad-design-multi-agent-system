package dev.ratsnest.artifact;

import dev.ratsnest.core.DesignRun;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.stream.Stream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;
import java.util.zip.ZipOutputStream;

@Service
public class RunArtifactService {

    public static final String PROJECT = "project";
    private static final long MAX_ENTRY_BYTES = 32L * 1024 * 1024;

    private final ArtifactStore store;
    private final RunArtifactRepository artifacts;

    public RunArtifactService(ArtifactStore store,
                              RunArtifactRepository artifacts) {
        this.store = store;
        this.artifacts = artifacts;
    }

    @Transactional
    public RunArtifact storeProject(DesignRun run, InputStream source,
                                    String requestedFilename) throws IOException {
        String filename = safeFilename(requestedFilename, run.getId());
        String tenant = run.getOrganizationId() == null
                ? "legacy" : run.getOrganizationId();
        String key = "organizations/" + tenant + "/runs/" + run.getId()
                + "/project.zip";
        ArtifactStore.StoredObject stored = store.put(key, source);
        RunArtifact artifact = artifacts.findByRunIdAndKind(run.getId(), PROJECT)
                .orElseGet(() -> RunArtifact.create(
                        run.getId(), run.getOrganizationId(), PROJECT, filename,
                        "application/zip", key, stored.size(), stored.sha256()));
        if (artifact.getId() != null) {
            artifact.replace(filename, "application/zip", stored.size(),
                    stored.sha256());
        }
        return artifacts.save(artifact);
    }

    public Optional<RunArtifact> projectFor(String runId) {
        return artifacts.findByRunIdAndKind(runId, PROJECT)
                .filter(artifact -> store.exists(artifact.getStorageKey()));
    }

    public InputStream open(RunArtifact artifact) throws IOException {
        return store.open(artifact.getStorageKey());
    }

    public List<RunArtifact> list(String runId) {
        return artifacts.findByRunIdOrderByCreatedAtAsc(runId);
    }

    public Optional<byte[]> readProjectEntry(String runId, String entryName)
            throws IOException {
        RunArtifact artifact = projectFor(runId).orElse(null);
        if (artifact == null) {
            return Optional.empty();
        }
        try (InputStream raw = store.open(artifact.getStorageKey());
             ZipInputStream zip = new ZipInputStream(raw)) {
            ZipEntry entry;
            while ((entry = zip.getNextEntry()) != null) {
                if (!entry.isDirectory() && entryName.equals(
                        entry.getName().replace('\\', '/'))) {
                    return Optional.of(readBounded(zip, MAX_ENTRY_BYTES));
                }
            }
        }
        return Optional.empty();
    }

    public List<String> listProjectEntries(String runId, String prefix,
                                           String suffix) throws IOException {
        RunArtifact artifact = projectFor(runId).orElse(null);
        if (artifact == null) {
            return List.of();
        }
        List<String> names = new ArrayList<>();
        try (InputStream raw = store.open(artifact.getStorageKey());
             ZipInputStream zip = new ZipInputStream(raw)) {
            ZipEntry entry;
            while ((entry = zip.getNextEntry()) != null) {
                String name = entry.getName().replace('\\', '/');
                if (!entry.isDirectory() && name.startsWith(prefix)
                        && name.endsWith(suffix)) {
                    names.add(name);
                }
            }
        }
        return names.stream().sorted().toList();
    }

    public RunArtifact captureProjectDirectory(DesignRun run) throws IOException {
        Path directory = Path.of(run.getProjectDir());
        if (!Files.isDirectory(directory)) {
            throw new IOException("project directory does not exist");
        }
        Path temp = Files.createTempFile("ratsnest-project-", ".zip");
        try {
            try (ZipOutputStream zip = new ZipOutputStream(
                    Files.newOutputStream(temp));
                 Stream<Path> files = Files.walk(directory)) {
                for (Path file : files.filter(Files::isRegularFile).toList()) {
                    String name = directory.relativize(file).toString()
                            .replace('\\', '/');
                    zip.putNextEntry(new ZipEntry(name));
                    Files.copy(file, zip);
                    zip.closeEntry();
                }
            }
            try (InputStream input = Files.newInputStream(temp)) {
                return storeProject(run, input,
                        directory.getFileName() + ".zip");
            }
        } finally {
            Files.deleteIfExists(temp);
        }
    }

    private static byte[] readBounded(InputStream input, long limit)
            throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[32 * 1024];
        long total = 0;
        int read;
        while ((read = input.read(buffer)) >= 0) {
            total += read;
            if (total > limit) {
                throw new IOException("artifact entry exceeds size limit");
            }
            output.write(buffer, 0, read);
        }
        return output.toByteArray();
    }

    private static String safeFilename(String value, String runId) {
        String fallback = "ratsnest-" + runId + ".zip";
        if (value == null || value.isBlank()) {
            return fallback;
        }
        String normalized = value.replace('\\', '/');
        String filename = normalized.substring(normalized.lastIndexOf('/') + 1)
                .replaceAll("[^A-Za-z0-9._-]", "_");
        return filename.toLowerCase().endsWith(".zip") ? filename : fallback;
    }
}

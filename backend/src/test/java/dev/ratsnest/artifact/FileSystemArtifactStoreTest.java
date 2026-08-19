package dev.ratsnest.artifact;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class FileSystemArtifactStoreTest {

    @TempDir Path tempDir;

    @Test
    void storesContentWithDigestAndRejectsEscapingKeys() throws Exception {
        FileSystemArtifactStore store = new FileSystemArtifactStore(
                tempDir.toString(), 1024);
        byte[] payload = "verified-project".getBytes(StandardCharsets.UTF_8);

        ArtifactStore.StoredObject stored = store.put(
                "organizations/acme/runs/run-1/project.zip",
                new ByteArrayInputStream(payload));

        assertThat(stored.size()).isEqualTo(payload.length);
        assertThat(stored.sha256()).hasSize(64);
        assertThat(store.open(stored.key()).readAllBytes()).isEqualTo(payload);
        assertThatThrownBy(() -> store.put(
                "../outside.zip", new ByteArrayInputStream(payload)))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void enforcesConfiguredMaximumSize() {
        FileSystemArtifactStore store = new FileSystemArtifactStore(
                tempDir.toString(), 4);
        assertThatThrownBy(() -> store.put(
                "safe/file.zip", new ByteArrayInputStream(new byte[5])))
                .hasMessageContaining("size limit");
    }
}

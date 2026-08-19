package dev.ratsnest.artifact;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

@Component
public class FileSystemArtifactStore implements ArtifactStore {

    private final Path root;
    private final long maxBytes;

    public FileSystemArtifactStore(
            @Value("${ratsnest.artifacts.root:./data/artifacts}") String root,
            @Value("${ratsnest.artifacts.max-bytes:268435456}") long maxBytes) {
        this.root = Path.of(root).toAbsolutePath().normalize();
        this.maxBytes = maxBytes;
    }

    @Override
    public StoredObject put(String key, InputStream source) throws IOException {
        Path target = resolve(key);
        Files.createDirectories(target.getParent());
        Path temp = Files.createTempFile(target.getParent(), ".upload-", ".tmp");
        MessageDigest digest = sha256();
        long size = 0;
        try (InputStream input = new BufferedInputStream(source);
             OutputStream output = new BufferedOutputStream(
                     Files.newOutputStream(temp))) {
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                size += read;
                if (size > maxBytes) {
                    throw new IOException("artifact exceeds configured size limit");
                }
                digest.update(buffer, 0, read);
                output.write(buffer, 0, read);
            }
        } catch (Exception e) {
            Files.deleteIfExists(temp);
            throw e;
        }
        try {
            Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING,
                    StandardCopyOption.ATOMIC_MOVE);
        } catch (java.nio.file.AtomicMoveNotSupportedException ignored) {
            Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING);
        }
        return new StoredObject(key, size,
                HexFormat.of().formatHex(digest.digest()));
    }

    @Override
    public InputStream open(String key) throws IOException {
        return Files.newInputStream(resolve(key));
    }

    @Override
    public boolean exists(String key) {
        return Files.isRegularFile(resolve(key));
    }

    private Path resolve(String key) {
        if (key == null || !key.matches("[A-Za-z0-9._/-]{1,500}")) {
            throw new IllegalArgumentException("invalid artifact key");
        }
        Path resolved = root.resolve(key).normalize();
        if (!resolved.startsWith(root)) {
            throw new IllegalArgumentException("artifact key escapes storage root");
        }
        return resolved;
    }

    private static MessageDigest sha256() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}

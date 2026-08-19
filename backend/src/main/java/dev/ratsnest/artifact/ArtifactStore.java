package dev.ratsnest.artifact;

import java.io.IOException;
import java.io.InputStream;

public interface ArtifactStore {

    record StoredObject(String key, long size, String sha256) {}

    StoredObject put(String key, InputStream source) throws IOException;

    InputStream open(String key) throws IOException;

    boolean exists(String key);
}

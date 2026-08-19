package dev.ratsnest.core;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ExecutionException;

/**
 * The single owner of "invoke the Python agent runtime": builds
 * `<python-exe> -m ratsnest <args>` in the configured runtime directory.
 * Both the Web-EDA bridge and local run dispatch go through here.
 */
@Service
public class PythonBridge {

    private static final int MAX_CAPTURE_BYTES = 4 * 1024 * 1024;

    @Value("${ratsnest.python-exe:python}")
    private String pythonExe;

    @Value("${ratsnest.agent-runtime-dir:.}")
    private String agentRuntimeDir;

    public record BridgeResult(boolean finished, String stdout, String stderr) {}

    public BridgeResult run(List<String> args, Duration timeout,
                            Map<String, String> extraEnv)
            throws IOException, InterruptedException {
        List<String> cmd = new ArrayList<>(List.of(pythonExe, "-m", "ratsnest"));
        cmd.addAll(args);
        ProcessBuilder pb = new ProcessBuilder(cmd)
                .directory(new File(agentRuntimeDir))
                .redirectErrorStream(false);
        pb.environment().putAll(extraEnv);
        Process proc = pb.start();
        ExecutorService drains = Executors.newFixedThreadPool(2);
        Future<String> stdout = drains.submit(() -> readBounded(
                proc.getInputStream()));
        Future<String> stderr = drains.submit(() -> readBounded(
                proc.getErrorStream()));
        boolean finished = false;
        try {
            finished = proc.waitFor(timeout.toMillis(), TimeUnit.MILLISECONDS);
            if (!finished) {
                proc.descendants().forEach(ProcessHandle::destroy);
                proc.destroy();
                if (!proc.waitFor(5, TimeUnit.SECONDS)) {
                    proc.descendants().forEach(ProcessHandle::destroyForcibly);
                    proc.destroyForcibly();
                    proc.waitFor(5, TimeUnit.SECONDS);
                }
            }
            return new BridgeResult(
                    finished, await(stdout), await(stderr));
        } finally {
            drains.shutdownNow();
        }
    }

    private static String await(Future<String> value) throws IOException,
            InterruptedException {
        try {
            return value.get(10, TimeUnit.SECONDS);
        } catch (java.util.concurrent.TimeoutException error) {
            throw new IOException("timed out draining agent runtime output", error);
        } catch (ExecutionException error) {
            Throwable cause = error.getCause();
            if (cause instanceof IOException io) {
                throw io;
            }
            throw new IOException("failed to drain agent runtime output", cause);
        }
    }

    private static String readBounded(InputStream input) throws IOException {
        ByteArrayOutputStream captured = new ByteArrayOutputStream();
        byte[] buffer = new byte[16 * 1024];
        int read;
        boolean truncated = false;
        while ((read = input.read(buffer)) >= 0) {
            int remaining = MAX_CAPTURE_BYTES - captured.size();
            if (remaining > 0) {
                captured.write(buffer, 0, Math.min(read, remaining));
            }
            truncated |= read > remaining;
        }
        String text = captured.toString(StandardCharsets.UTF_8);
        return truncated ? text + "\n[output truncated]" : text;
    }
}

package dev.ratsnest.api;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.core.PythonBridge;
import dev.ratsnest.security.RunAccessPolicy;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * Web-EDA bridge (Stage 3): the browser sends typed edit ops; the Python
 * runtime executes them through the trusted write paths and returns fresh
 * state. The browser never writes S-expressions. Local-dispatch mode only
 * (cluster mode needs artifact storage first — Phase 3 note applies).
 */
@RestController
@RequestMapping("/api")
public class EdaController {

    private final DesignRunRepository runs;
    private final RunAccessPolicy access;
    private final PythonBridge bridge;

    public EdaController(DesignRunRepository runs, RunAccessPolicy access,
                         PythonBridge bridge) {
        this.runs = runs;
        this.access = access;
        this.bridge = bridge;
    }

    @GetMapping("/runs/{id}/eda")
    public ResponseEntity<String> state(@PathVariable String id)
            throws Exception {
        return runEda(id, null);
    }

    @PostMapping("/runs/{id}/eda")
    public ResponseEntity<String> edit(@PathVariable String id,
                                       @RequestBody String opsJson)
            throws Exception {
        if (opsJson == null || opsJson.length() > 100_000) {
            return ResponseEntity.badRequest().build();
        }
        return runEda(id, opsJson);
    }

    private ResponseEntity<String> runEda(String id, String opsJson)
            throws Exception {
        DesignRun run = runs.findById(id).orElse(null);
        if (run == null || run.getProjectDir() == null
                || !Files.isDirectory(Path.of(run.getProjectDir()))) {
            return ResponseEntity.notFound().build();
        }
        if (!access.canAccess(run)) {
            return ResponseEntity.notFound().build();  // same policy as RunController
        }
        List<String> cmd = new java.util.ArrayList<>(List.of("eda", run.getProjectDir()));
        Path opsFile = null;
        if (opsJson != null) {
            opsFile = Files.createTempFile("ratsnest-eda", ".json");
            Files.writeString(opsFile, opsJson, StandardCharsets.UTF_8);
            cmd.add("--ops");
            cmd.add(opsFile.toString());
        }
        try {
            PythonBridge.BridgeResult result = bridge.run(
                    cmd, Duration.ofMinutes(3), Map.of());
            if (result.stdout().isBlank()) {
                return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
                        .body("{\"error\":\"eda bridge produced no output\"}");
            }
            return ResponseEntity.ok()
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(result.stdout());
        } finally {
            if (opsFile != null) {
                Files.deleteIfExists(opsFile);
            }
        }
    }
}

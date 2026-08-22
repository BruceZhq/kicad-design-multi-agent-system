package team.ratsnest.controlplane.evolution.api;

import java.util.UUID;

import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import team.ratsnest.controlplane.evolution.application.EvolutionResultIngestionService;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;

@RestController
@RequestMapping("/internal/v1/evolution/trials")
public final class EvolutionResultController {

    private final EvolutionResultIngestionService results;

    public EvolutionResultController(EvolutionResultIngestionService results) {
        this.results = results;
    }

    @PostMapping("/{trialId}/result")
    EvolutionTrial result(
            @PathVariable UUID trialId,
            @RequestHeader("Authorization") String authorization,
            @RequestBody byte[] body) {
        return results.ingest(trialId, authorization, body);
    }
}

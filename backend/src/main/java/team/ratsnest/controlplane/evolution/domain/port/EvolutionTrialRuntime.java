package team.ratsnest.controlplane.evolution.domain.port;

import java.util.Map;
import java.util.UUID;

import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;

/** Outbound port for starting a governed evaluation in the Agent Runtime. */
public interface EvolutionTrialRuntime {

    StartResult start(UUID tenantId, EvolutionTrial trial, Map<String, Object> trialInput);

    record StartResult(UUID trialId, String workflowId, String status) {
    }
}

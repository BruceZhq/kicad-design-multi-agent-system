package team.ratsnest.controlplane.evolution.domain.model;

/** Trusted attribution retained separately from the public observation DTO. */
public record EvolutionObservationGovernance(
        String failureOrigin,
        String attributionAction,
        String attributionReasonCode,
        String attributionOrigin,
        Integer independentProjectCount,
        Integer independentRunCount) {

    public static EvolutionObservationGovernance none() {
        return new EvolutionObservationGovernance(null, null, null, null, null, null);
    }
}

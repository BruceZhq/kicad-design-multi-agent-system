package dev.ratsnest.approval;

import dev.ratsnest.artifact.RunArtifactService;
import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

class RunApprovalServiceProductionGateTest {

    private final RunApprovalService service = new RunApprovalService(
            mock(RunApprovalRepository.class),
            mock(DesignRunRepository.class),
            mock(RunArtifactService.class));

    @Test
    void releaseRequiresCrewV2ConvergenceAndEveryProductionGate() {
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        run.setStatus("converged");
        run.setPlanContractVersion("ratsnest.design-plan.v2");
        run.setResultJson(resultWithGate("emc", "passed"));

        assertThat(service.isReleaseEligible(run)).isTrue();

        run.setResultJson(resultWithGate("emc", "failed"));
        assertThat(service.isReleaseEligible(run)).isFalse();

        run.setResultJson(resultWithGate("emc", "passed")
                .replace("\"thermal\": {\"status\": \"passed\", \"required\": true},", ""));
        assertThat(service.isReleaseEligible(run)).isFalse();

        run.setResultJson(resultWithGate("emc", "passed"));
        run.setBackend("template");
        assertThat(service.isReleaseEligible(run)).isFalse();
    }

    @Test
    void malformedOrIncompleteGateEvidenceFailsClosed() {
        assertThat(service.hasPassedProductionGates(null)).isFalse();
        assertThat(service.hasPassedProductionGates("{}" )).isFalse();
        assertThat(service.hasPassedProductionGates("not-json")).isFalse();
        assertThat(service.hasPassedProductionGates(
                "{\"iterations\":[{\"scorecard\":{" +
                        "\"required_gates_passed\":false}}]}"))
                .isFalse();
    }

    private static String resultWithGate(String gate, String status) {
        return """
                {
                  "status": "converged",
                  "iterations": [{
                    "scorecard": {
                      "required_gates_passed": true,
                      "gate_results": {
                        "catalog": {"status": "passed", "required": true},
                        "bom": {"status": "passed", "required": true},
                        "erc": {"status": "passed", "required": true},
                        "drc": {"status": "passed", "required": true},
                        "spice": {"status": "passed", "required": true},
                        "thermal": {"status": "passed", "required": true},
                        "%s": {"status": "%s", "required": true}
                      }
                    }
                  }]
                }
                """.formatted(gate, status);
    }
}

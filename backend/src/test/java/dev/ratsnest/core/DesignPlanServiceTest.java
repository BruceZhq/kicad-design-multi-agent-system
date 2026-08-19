package dev.ratsnest.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.ratsnest.approval.RunApprovalService;
import org.junit.jupiter.api.Test;
import org.springframework.web.server.ResponseStatusException;

import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class DesignPlanServiceTest {

    @Test
    void firstValidatedPlanIsImmutableAndBoundToTheRun() {
        DesignRunRepository runs = mock(DesignRunRepository.class);
        RunApprovalService approvals = mock(RunApprovalService.class);
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        run.setPythonRunId("run_controlled");
        when(runs.findLockedById(run.getId())).thenReturn(Optional.of(run));
        when(runs.save(any(DesignRun.class))).thenAnswer(call -> call.getArgument(0));
        DesignPlanService service = new DesignPlanService(
                runs, approvals, new ObjectMapper());
        String first = planJson("run_controlled", "12V to 5V", "crew");

        DesignRun saved = service.apply(run.getId(), first);
        String firstHash = saved.getPlanSha256();
        service.apply(run.getId(), planJson(
                "run_controlled", "12V to 5V", "template"));

        assertThat(saved.getStatus()).isEqualTo("awaiting_plan_approval");
        assertThat(saved.getPlanJson()).isEqualTo(first);
        assertThat(saved.getPlanSha256()).isEqualTo(firstHash).hasSize(64);
        assertThat(saved.getPlanContractVersion())
                .isEqualTo("ratsnest.design-plan.v2");
        verify(approvals).ensurePlanReview(saved);
    }

    @Test
    void acceptsLegacyV1ForReadCompatibility() {
        DesignRunRepository runs = mock(DesignRunRepository.class);
        RunApprovalService approvals = mock(RunApprovalService.class);
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        run.setPythonRunId("run_legacy");
        when(runs.findLockedById(run.getId())).thenReturn(Optional.of(run));
        when(runs.save(any(DesignRun.class))).thenAnswer(call -> call.getArgument(0));
        DesignPlanService service = new DesignPlanService(
                runs, approvals, new ObjectMapper());

        DesignRun saved = service.apply(run.getId(), planJson(
                "run_legacy", "12V to 5V", "crew",
                "ratsnest.design-plan.v1"));

        assertThat(saved.getPlanContractVersion())
                .isEqualTo("ratsnest.design-plan.v1");
    }

    @Test
    void rejectsAPlanForAnotherRunBeforePersistingIt() {
        DesignRunRepository runs = mock(DesignRunRepository.class);
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        run.setPythonRunId("run_expected");
        when(runs.findLockedById(run.getId())).thenReturn(Optional.of(run));
        DesignPlanService service = new DesignPlanService(
                runs, mock(RunApprovalService.class), new ObjectMapper());

        assertThatThrownBy(() -> service.apply(
                run.getId(), planJson("run_other", "12V to 5V", "crew")))
                .isInstanceOf(ResponseStatusException.class)
                .hasMessageContaining("another run");
        assertThat(run.getPlanJson()).isNull();
    }

    static String planJson(String runId, String requirement, String backend) {
        return planJson(runId, requirement, backend,
                "ratsnest.design-plan.v2");
    }

    static String planJson(String runId, String requirement, String backend,
                           String contractVersion) {
        boolean production = "ratsnest.design-plan.v2".equals(contractVersion);
        String component = production
                ? "{\"ref\":\"U1\",\"catalog_id\":\"ti.lm2596s-adj.ktt\"," +
                "\"footprint\":\"Package_TO_SOT_SMD:TO-263-5_TabPin3\"," +
                "\"on_board\":true}"
                : "{\"ref\":\"U1\"}";
        String productionFields = production
                ? "\"catalog_version\":\"stage3.1.2\"," +
                "\"design_limits\":{}," +
                "\"required_gates\":[\"catalog\",\"bom\"],"
                : "";
        return "{" +
                "\"contract_version\":\"" + contractVersion + "\"," +
                "\"run_id\":\"" + runId + "\"," +
                "\"requirement\":\"" + requirement + "\"," +
                "\"backend\":\"" + backend + "\"," +
                "\"design_spec\":{\"requirement_text\":\"" +
                requirement + "\"}," +
                "\"board_plan\":{\"topology\":\"ldo\"," +
                productionFields +
                "\"components\":[" + component + "]," +
                "\"connections\":[]}," +
                "\"strategy_name\":\"v0\"," +
                "\"strategy_version_id\":\"strat_0123456789abcdef\"," +
                "\"trajectory_step\":2," +
                "\"created_at\":\"2026-07-15T00:00:00Z\"}";
    }
}

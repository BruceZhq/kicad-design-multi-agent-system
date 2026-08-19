package dev.ratsnest.core;

import dev.ratsnest.approval.RunApprovalService;
import dev.ratsnest.artifact.RunArtifactService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.kafka.core.KafkaTemplate;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class RunDispatchServiceTest {

    @Test
    void identicalResultCallbacksAreIdempotentButConflictsAreRejected()
            throws Exception {
        @SuppressWarnings("unchecked")
        ObjectProvider<KafkaTemplate<String, String>> kafka =
                mock(ObjectProvider.class);
        RunApprovalService approvals = mock(RunApprovalService.class);
        RunResultService results = new RunResultService(
                mock(DesignRunRepository.class), approvals);
        RunDispatchService service = new RunDispatchService(
                mock(DesignRunRepository.class), kafka,
                mock(PythonBridge.class), mock(RunArtifactService.class),
                approvals, mock(DesignPlanService.class), results);
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        run.setPythonRunId("run_python");
        run.setPlanJson("{\"approved\":true}");
        run.setPlanSha256(DesignPlanService.sha256(run.getPlanJson()));
        when(approvals.isApproved(
                run.getId(), RunApprovalService.BOARD_PLAN)).thenReturn(true);
        String result = "{\"run_id\":\"run_python\","
                + "\"status\":\"converged\",\"iterations\":[]}";

        service.applyResult(run, result);
        service.applyResult(run, result);

        assertThat(run.getStatus()).isEqualTo("converged");
        assertThat(run.getResultSha256()).hasSize(64);
        assertThatThrownBy(() -> service.applyResult(
                run, "{\"status\":\"failed\"}"))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("conflicting result");
    }

    @Test
    void unapprovedDesignNeverStartsThePythonExecutionBridge() throws Exception {
        @SuppressWarnings("unchecked")
        ObjectProvider<KafkaTemplate<String, String>> kafka =
                mock(ObjectProvider.class);
        DesignRunRepository runs = mock(DesignRunRepository.class);
        PythonBridge bridge = mock(PythonBridge.class);
        RunApprovalService approvals = mock(RunApprovalService.class);
        RunResultService results = new RunResultService(runs, approvals);
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        run.setDispatchPhase("execute");
        run.setPlanJson("{\"approved\":false}");
        run.setPlanSha256(DesignPlanService.sha256(run.getPlanJson()));
        when(runs.findById(run.getId())).thenReturn(java.util.Optional.of(run));
        when(runs.findLockedById(run.getId()))
                .thenReturn(java.util.Optional.of(run));
        when(runs.save(any(DesignRun.class))).thenAnswer(call -> call.getArgument(0));
        RunDispatchService service = new RunDispatchService(
                runs, kafka, bridge, mock(RunArtifactService.class),
                approvals, mock(DesignPlanService.class), results);

        service.dispatchLocal(run.getId());

        verify(bridge, never()).run(any(), any(), any());
        assertThat(run.getStatus()).isEqualTo("failed");
        assertThat(run.getFailureMessage()).contains("approved immutable");
    }

    @Test
    void commitsResultBeforeArtifactAndReleaseReview() throws Exception {
        @SuppressWarnings("unchecked")
        ObjectProvider<KafkaTemplate<String, String>> kafka =
                mock(ObjectProvider.class);
        DesignRunRepository runs = mock(DesignRunRepository.class);
        PythonBridge bridge = mock(PythonBridge.class);
        RunArtifactService artifacts = mock(RunArtifactService.class);
        RunApprovalService approvals = mock(RunApprovalService.class);
        RunResultService results = mock(RunResultService.class);
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        run.setDispatchPhase("execute");
        run.setPythonRunId("run_python");
        run.setPlanJson("{\"approved\":true}");
        run.setPlanSha256(DesignPlanService.sha256(run.getPlanJson()));
        when(runs.findById(run.getId())).thenReturn(java.util.Optional.of(run));
        when(runs.save(any(DesignRun.class))).thenAnswer(call -> call.getArgument(0));
        when(approvals.isApproved(
                run.getId(), RunApprovalService.BOARD_PLAN)).thenReturn(true);
        when(bridge.run(any(), any(), any())).thenReturn(
                new PythonBridge.BridgeResult(true,
                        "{\"run_id\":\"run_python\","
                                + "\"status\":\"converged\","
                                + "\"iterations\":[]}", ""));
        run.setStatus("converged");
        when(results.accept(any(), any())).thenReturn(run);

        RunDispatchService service = new RunDispatchService(
                runs, kafka, bridge, artifacts, approvals,
                mock(DesignPlanService.class), results);
        service.dispatchLocal(run.getId());

        var order = inOrder(results, artifacts);
        order.verify(results).accept(any(), any());
        order.verify(artifacts).captureProjectDirectory(run);
        order.verify(results).requestReleaseReview(run.getId());
    }
}

package dev.ratsnest.approval;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.RunSubmissionService;
import org.junit.jupiter.api.Test;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class RunApprovalWorkflowServiceTest {

    @Test
    void newlyApprovedBoardPlanSchedulesExecutionExactlyOnce() {
        RunApprovalService approvals = mock(RunApprovalService.class);
        RunSubmissionService submission = mock(RunSubmissionService.class);
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        RunApproval approval = RunApproval.pending(
                run.getId(), null, RunApprovalService.BOARD_PLAN,
                "a".repeat(64));
        approval.decide("approved", "reviewer", "checked");
        when(approvals.decide(
                run.getId(), RunApprovalService.BOARD_PLAN,
                "approved", "reviewer", "checked"))
                .thenReturn(new RunApprovalService.DecisionResult(
                        approval, run, true));
        RunApprovalWorkflowService workflow =
                new RunApprovalWorkflowService(approvals, submission);

        workflow.decide(run.getId(), RunApprovalService.BOARD_PLAN,
                "approved", "reviewer", "checked");

        verify(submission).scheduleExecution(run);
    }

    @Test
    void idempotentApprovalRetryDoesNotRedispatch() {
        RunApprovalService approvals = mock(RunApprovalService.class);
        RunSubmissionService submission = mock(RunSubmissionService.class);
        DesignRun run = DesignRun.createDesign(
                "12V to 5V", "project", 4, "crew");
        RunApproval approval = RunApproval.pending(
                run.getId(), null, RunApprovalService.BOARD_PLAN,
                "a".repeat(64));
        when(approvals.decide(
                run.getId(), RunApprovalService.BOARD_PLAN,
                "approved", "reviewer", null))
                .thenReturn(new RunApprovalService.DecisionResult(
                        approval, run, false));

        new RunApprovalWorkflowService(approvals, submission).decide(
                run.getId(), RunApprovalService.BOARD_PLAN,
                "approved", "reviewer", null);

        verify(submission, never()).scheduleExecution(run);
    }
}

package dev.ratsnest.approval;

import dev.ratsnest.core.RunSubmissionService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class RunApprovalWorkflowService {

    private final RunApprovalService approvals;
    private final RunSubmissionService submission;

    public RunApprovalWorkflowService(RunApprovalService approvals,
                                      RunSubmissionService submission) {
        this.approvals = approvals;
        this.submission = submission;
    }

    @Transactional
    public RunApproval decide(String runId, String type, String decision,
                              String actor, String comment) {
        RunApprovalService.DecisionResult result = approvals.decide(
                runId, type, decision, actor, comment);
        if (result.changed()
                && RunApprovalService.BOARD_PLAN.equals(type)
                && "approved".equals(decision)) {
            submission.scheduleExecution(result.run());
        }
        return result.approval();
    }
}

"""Serialization-only constants shared by the evolution worker and workflow."""

EVOLUTION_WORKFLOW_NAME = "ratsnest.harness-evolution.v1"
EVOLUTION_TASK_QUEUE = "ratsnest-evolution"
EVOLUTION_SANDBOX_TASK_QUEUE = "ratsnest-evolution-sandbox"
EVALUATE_CANDIDATE_ACTIVITY = "ratsnest.evolution.materialize-and-evaluate-candidate"
BUILD_FAILURE_REPORT_ACTIVITY = "ratsnest.evolution.build-failure-report"
ATTEST_RESULT_ACTIVITY = "ratsnest.evolution.attest-result"
DELIVER_RESULT_ACTIVITY = "ratsnest.evolution.deliver-result"
FIXED_EVAL_IDS = (
    "python-compile",
    "evolution-core",
    "optimization-suite",
    "holdout-suite",
    "adversarial-suite",
)
FIXED_SUITE_MANIFESTS = (
    ("optimization-suite", "optimization", "evals/regression/optimization.v1.json", False),
    ("holdout-suite", "holdout", "evals/sealed/regression/holdout.v1.json", True),
    (
        "adversarial-suite",
        "adversarial",
        "evals/sealed/regression/adversarial.v1.json",
        True,
    ),
)

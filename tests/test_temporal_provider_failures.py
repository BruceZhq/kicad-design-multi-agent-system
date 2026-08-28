from __future__ import annotations

import pytest
from pydantic import BaseModel
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    RetryState,
)

from agents.ratsnestpro.temporal.workflow import _activity_failure_message
from agents.ratsnestpro.tools import (
    _non_retryable_provider_failure,
    _provider_failure_message,
)
from ratsnestpro.agents import LlmMode, NonRetryableLlmError
from ratsnestpro.orchestration.pipeline import PipelineContext, propose_structured


class _Proposal(BaseModel):
    value: int


class _FatalClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, _system: str, _user: str) -> str:
        self.calls += 1
        raise NonRetryableLlmError("provider balance is exhausted")


class _ProviderError(RuntimeError):
    status_code = 402


def test_provider_balance_failure_is_non_retryable_and_sanitized() -> None:
    error = _ProviderError("Error code: 402 - Insufficient Balance")

    assert _non_retryable_provider_failure(error) is True
    assert _provider_failure_message(error, "deepseek-v4-flash") == (
        "LLM model deepseek-v4-flash: provider balance or quota is exhausted "
        "(HTTP 402)"
    )


def test_structured_proposal_does_not_retry_fatal_provider_failure() -> None:
    client = _FatalClient()
    context = PipelineContext(mode=LlmMode.REQUIRED, client=client)

    with pytest.raises(NonRetryableLlmError, match="balance is exhausted"):
        propose_structured(
            context,
            model=_Proposal,
            system="system",
            user="user",
            fallback=lambda: _Proposal(value=0),
        )

    assert client.calls == 1


def test_temporal_activity_message_unwraps_the_application_error() -> None:
    activity_error = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="ratsnest.execute_pipeline_step",
        activity_id="9",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )
    activity_error.__cause__ = ApplicationError(
        "LLM model deepseek-v4-flash: provider balance or quota is exhausted (HTTP 402)",
        type="PermanentPipelineError",
        non_retryable=True,
    )

    assert _activity_failure_message(activity_error) == (
        "Temporal Activity failed without retry: PermanentPipelineError: "
        "LLM model deepseek-v4-flash: provider balance or quota is exhausted "
        "(HTTP 402)"
    )

"""Governed, offline improvement primitives for the RatsNestPro harness.

The runtime AHE remains responsible for one run.  This package consumes its
events, evaluates immutable harness versions, and prepares reviewable change
candidates.  Nothing in this package promotes or deploys production code.
"""

from evolution.contracts import (
    EvalCaseManifest,
    EvolutionCandidate,
    EvolutionObservation,
    HarnessIdentity,
    HarnessManifest,
)

__all__ = [
    "EvalCaseManifest",
    "EvolutionCandidate",
    "EvolutionObservation",
    "HarnessIdentity",
    "HarnessManifest",
]

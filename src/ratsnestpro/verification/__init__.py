"""Deterministic verification: IR-level rules, parametric checks, and gates.

The verifiers here are the authority. The LLM may propose an IR and params,
but whether a design passes is decided by these deterministic rules plus
kicad-cli ERC — never by model prose.
"""

from ratsnestpro.verification.expectations import Expectations
from ratsnestpro.verification.verify import verify_design

__all__ = ["Expectations", "verify_design"]

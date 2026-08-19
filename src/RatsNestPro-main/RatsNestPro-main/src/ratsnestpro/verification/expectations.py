"""Expectations: the deterministic, machine-checkable facts a materialized
design must satisfy, derived from the validated family parameters.

This is *hard fact* territory — it lives with the verification layer, not in
the fuzzy knowledge base. The Architect derives an Expectations object from
the chosen parameters and the verifier checks the Circuit IR against it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: str = "generic-design"
    supply_net: str = "3V3"
    supply_voltage_v: float = 3.3
    gnd_net: str = "GND"

    decoupling_count: int = Field(default=6, ge=1, le=16)
    decoupling_value: str = "100nF"

    crystal_freq_mhz: int = 16
    crystal_load_cap: str = "18pF"

    ldo_input_cap: str = "1uF"
    ldo_output_cap: str = "1uF"

    power_led: bool = True
    header_signal_pins: int = Field(default=12, ge=0, le=64)
    mounting_holes: int = Field(default=4, ge=0, le=8)

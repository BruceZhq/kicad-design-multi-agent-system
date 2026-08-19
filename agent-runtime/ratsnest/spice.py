"""Headless ngspice gate for the two qualified Stage 3 power families."""

from __future__ import annotations

import ctypes
import json
import math
import threading
from pathlib import Path

from ratsnest.circuit_math import BUCK_TOPOLOGY, parse_si_value
from ratsnest.config import Config
from ratsnest.crews.contracts import BoardPlan
from ratsnest.schemas import DesignSpec, GateStatus, VerificationGate

_NGSPICE_LOCK = threading.Lock()


def _value(plan: BoardPlan, ref: str) -> float:
    return parse_si_value(plan.component(ref).value)


def _deck(plan: BoardPlan, spec: DesignSpec) -> tuple[str, float]:
    load_ohm = spec.output_voltage / spec.output_current_a
    if plan.topology != BUCK_TOPOLOGY:
        dropout = 1.4
        text = f"""* RatsNest TLV1117 release model
VINPUT vin 0 DC {spec.input_voltage:g}
BREG vraw 0 V=min(V(vin)-{dropout:g},{spec.output_voltage:g})
RREG vraw vout 0.05
CIN vin 0 {_value(plan, 'C1'):g}
COUT vout 0 {_value(plan, 'C2'):g} IC=0
RLOAD vout 0 {load_ohm:g}
.tran 10u 20m uic
.end
"""
        return text, 0.010

    limits = plan.design_limits
    assert limits is not None and limits.duty_cycle is not None
    frequency = limits.switching_frequency_hz or 150000.0
    period = 1.0 / frequency
    on_time = period * limits.duty_cycle
    text = f"""* RatsNest LM2596 asynchronous Buck release model
VINPUT vin 0 DC {spec.input_voltage:g}
VPWM pwm 0 PULSE(0 5 0 10n 10n {on_time:g} {period:g})
SMAIN vin sw pwm 0 SWMOD
DREC 0 sw DMOD
LPOWER sw vout {_value(plan, 'L1'):g} IC=0
CIN vin 0 {_value(plan, 'C1'):g}
COUT vout 0 {_value(plan, 'C2'):g} IC=0
RLOAD vout 0 {load_ohm:g}
.model SWMOD SW(Ron=0.05 Roff=1e9 Vt=2.5 Vh=0.1)
.model DMOD D(Is=1n Rs=0.02 N=1.05 Cjo=50p)
.tran 0.5u 30m 20m uic
.end
"""
    return text, 0.020


def _simulate(library: Path, deck: Path, data: Path) -> list[str]:
    messages: list[str] = []
    send_char_t = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
    exit_t = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool,
        ctypes.c_int, ctypes.c_void_p)
    data_t = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p)
    init_data_t = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
    bg_t = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)

    @send_char_t
    def send_char(message, _ident, _user):
        if message:
            messages.append(message.decode("utf-8", errors="replace"))
        return 0

    @exit_t
    def controlled_exit(_status, _immediate, _quit, _ident, _user):
        return 0

    @data_t
    def send_data(_values, _count, _ident, _user):
        return 0

    @init_data_t
    def send_init(_values, _ident, _user):
        return 0

    @bg_t
    def bg_thread(_running, _ident, _user):
        return 0

    dll = ctypes.CDLL(str(library))
    dll.ngSpice_Init.argtypes = [
        send_char_t, send_char_t, exit_t, data_t, init_data_t, bg_t,
        ctypes.c_void_p]
    dll.ngSpice_Init.restype = ctypes.c_int
    dll.ngSpice_Command.argtypes = [ctypes.c_char_p]
    dll.ngSpice_Command.restype = ctypes.c_int
    if dll.ngSpice_Init(
            send_char, send_char, controlled_exit, send_data, send_init,
            bg_thread, None) != 0:
        raise RuntimeError("ngSpice_Init failed")

    def command(value: str) -> None:
        if dll.ngSpice_Command(value.encode("utf-8")) != 0:
            raise RuntimeError(f"ngspice command failed: {value}")

    command("set noaskquit")
    command(f'source {deck.as_posix()}')
    command("run")
    command(f'wrdata {data.as_posix()} time v(vout)')
    command("destroy all")
    return messages


def run_spice_gate(project_dir: Path, plan: BoardPlan, spec: DesignSpec,
                   config: Config | None = None) -> VerificationGate:
    config = config or Config.load()
    output = Path(project_dir) / "verification"
    output.mkdir(parents=True, exist_ok=True)
    deck_path = output / "spice.cir"
    data_path = output / "spice.dat"
    result_path = output / "spice.json"
    deck, steady_start = _deck(plan, spec)
    deck_path.write_text(deck, encoding="ascii")
    library = config.ngspice_library
    if library is None or not Path(library).is_file():
        return VerificationGate(
            name="spice", status=GateStatus.unavailable,
            summary="ngspice shared library is unavailable", tool="ngspice",
            evidence=[str(deck_path.relative_to(project_dir))])
    try:
        with _NGSPICE_LOCK:
            messages = _simulate(Path(library), deck_path, data_path)
        rows: list[tuple[float, float]] = []
        for line in data_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                values = [float(value) for value in line.split()]
            except ValueError:
                continue
            if len(values) >= 2 and math.isfinite(values[0]) and math.isfinite(values[-1]):
                rows.append((values[0], values[-1]))
        steady = [voltage for time, voltage in rows if time >= steady_start]
        if len(steady) < 20:
            raise RuntimeError("ngspice produced too few steady-state samples")
        average = sum(steady) / len(steady)
        ripple_mv = (max(steady) - min(steady)) * 1000.0
        error_pct = abs(average - spec.output_voltage) / spec.output_voltage * 100.0
        passed = error_pct <= 3.0 and ripple_mv <= spec.max_output_ripple_mv
        metrics = {
            "average_output_v": round(average, 6),
            "target_output_v": spec.output_voltage,
            "output_error_pct": round(error_pct, 4),
            "peak_to_peak_ripple_mv": round(ripple_mv, 4),
            "ripple_limit_mv": spec.max_output_ripple_mv,
            "samples": len(steady),
        }
        result_path.write_text(
            json.dumps({"metrics": metrics, "messages": messages[-20:]}, indent=2),
            encoding="utf-8")
        return VerificationGate(
            name="spice",
            status=GateStatus.passed if passed else GateStatus.failed,
            summary=("transient output and ripple are within limits" if passed
                     else "transient output or ripple exceeds the approved limit"),
            tool="ngspice",
            evidence=[str(path.relative_to(project_dir))
                      for path in (deck_path, data_path, result_path)],
            metrics=metrics)
    except Exception as exc:
        return VerificationGate(
            name="spice", status=GateStatus.error,
            summary=f"ngspice verification failed: {exc}", tool="ngspice",
            evidence=[str(deck_path.relative_to(project_dir))])

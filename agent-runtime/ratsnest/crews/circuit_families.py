"""Canonical Stage 3 BoardPlan recipes for the qualified circuit families."""

from __future__ import annotations

from ratsnest.catalog import load_catalog
from ratsnest.circuit_math import BUCK_TOPOLOGY, LDO_TOPOLOGY, SolvedCircuit
from ratsnest.crews.contracts import (
    BoardComponent,
    BoardConnection,
    BoardNetClass,
    BoardOutline,
    BoardPlan,
    DesignLimits,
    PlacementHint,
)
from ratsnest.design_gen.templates import rail_name
from ratsnest.schemas import DesignSpec


def _component(ref: str, solved: SolvedCircuit) -> BoardComponent:
    catalog = load_catalog()
    catalog_id = solved.catalog_ids[ref]
    entry = catalog.entry(catalog_id)
    mpn = solved.mpns[ref]
    properties = {
        "Catalog ID": catalog_id,
        "Manufacturer": entry.manufacturer,
        "MPN": mpn,
        "Lifecycle": entry.lifecycle,
        "Role": solved.roles[ref],
        "Datasheet": entry.datasheet_url,
    }
    return BoardComponent(
        ref=ref,
        symbol=entry.symbol,
        value=solved.values[ref],
        footprint=entry.footprint,
        catalog_id=catalog_id,
        role=solved.roles[ref],
        in_bom=entry.in_bom,
        on_board=entry.on_board,
        properties=properties,
    )


def _limits(spec: DesignSpec, solved: SolvedCircuit) -> DesignLimits:
    metrics = solved.metrics
    return DesignLimits(
        input_voltage_v=spec.input_voltage,
        output_voltage_v=spec.output_voltage,
        output_current_a=spec.output_current_a,
        ambient_temperature_c=spec.ambient_temperature_c,
        controller_loss_w=metrics["controller_loss_w"],
        estimated_junction_c=metrics["estimated_junction_c"],
        max_junction_c=metrics["max_junction_c"],
        estimated_efficiency_pct=metrics["estimated_efficiency_pct"],
        dropout_margin_v=metrics.get("dropout_margin_v"),
        duty_cycle=metrics.get("duty_cycle"),
        switching_frequency_hz=metrics.get("switching_frequency_hz"),
        max_output_ripple_mv=spec.max_output_ripple_mv,
    )


def _hints(rows: dict[str, tuple[float, float]]) -> list[PlacementHint]:
    return [PlacementHint(ref=ref, x=position[0], y=position[1])
            for ref, position in rows.items()]


def _common_connections(vin: str, vout: str,
                        feedback: str) -> list[BoardConnection]:
    return [
        BoardConnection(net=vin, ref="J1", pin="1"),
        BoardConnection(net="GND", ref="J1", pin="2"),
        BoardConnection(net=vout, ref="J2", pin="1"),
        BoardConnection(net="GND", ref="J2", pin="2"),
        BoardConnection(net=vin, ref="TP1", pin="1"),
        BoardConnection(net=vout, ref="TP2", pin="1"),
        BoardConnection(net="GND", ref="TP3", pin="1"),
        BoardConnection(net=feedback, ref="TP4", pin="1"),
        BoardConnection(net=vin, ref="#FLG01", pin="1"),
        BoardConnection(net="GND", ref="#FLG02", pin="1"),
    ]


def _append_indicator(connections: list[BoardConnection], vout: str,
                      diode_ref: str) -> None:
    connections.extend([
        BoardConnection(net=vout, ref="R3", pin="1"),
        BoardConnection(net="LED_A", ref="R3", pin="2"),
        BoardConnection(net="LED_A", ref=diode_ref, pin="2"),
        BoardConnection(net="LED_A", ref="TP5", pin="1"),
        BoardConnection(net="GND", ref=diode_ref, pin="1"),
    ])


def build_canonical_plan(spec: DesignSpec, solved: SolvedCircuit) -> BoardPlan:
    vin = rail_name(spec.input_voltage)
    vout = rail_name(spec.output_voltage)
    if solved.topology == LDO_TOPOLOGY:
        refs = [
            "J1", "C1", "U1", "R1", "R2", "C2", "J2",
            "TP1", "TP2", "TP3", "TP4", "#FLG01", "#FLG02",
        ]
        if solved.include_led:
            refs.extend(["R3", "D1", "TP5"])
        connections = _common_connections(vin, vout, "ADJ") + [
            BoardConnection(net=vin, ref="C1", pin="1"),
            BoardConnection(net="GND", ref="C1", pin="2"),
            BoardConnection(net=vin, ref="U1", pin="3"),
            BoardConnection(net=vout, ref="U1", pin="2"),
            BoardConnection(net=vout, ref="R1", pin="1"),
            BoardConnection(net="ADJ", ref="R1", pin="2"),
            BoardConnection(net="ADJ", ref="U1", pin="1"),
            BoardConnection(net="ADJ", ref="R2", pin="1"),
            BoardConnection(net="GND", ref="R2", pin="2"),
            BoardConnection(net=vout, ref="C2", pin="1"),
            BoardConnection(net="GND", ref="C2", pin="2"),
        ]
        if solved.include_led:
            _append_indicator(connections, vout, "D1")
        positions = {
            "J1": (6, 15), "C1": (16, 15), "U1": (29, 15),
            "R1": (34, 27), "R2": (42, 27), "C2": (47, 15),
            "J2": (62, 15), "TP1": (12, 36), "TP2": (52, 36),
            "TP3": (26, 36), "TP4": (40, 36),
        }
        if solved.include_led:
            positions.update({
                "R3": (54, 27), "D1": (62, 27), "TP5": (62, 36)})
        return BoardPlan(
            topology=LDO_TOPOLOGY,
            family_version=solved.family_version,
            catalog_version=solved.catalog_version,
            components=[_component(ref, solved) for ref in refs],
            connections=connections,
            outline=BoardOutline(width=70, height=45),
            design_limits=_limits(spec, solved),
            placement_hints=_hints(positions),
            net_classes={
                vin: BoardNetClass(track_width_mm=1.0, clearance_mm=0.25),
                vout: BoardNetClass(track_width_mm=1.0, clearance_mm=0.25),
                "GND": BoardNetClass(track_width_mm=1.0, clearance_mm=0.25),
            },
            required_gates=list(solved.required_gates),
            constraints=[
                "TLV1117 output-to-ADJ and ADJ-to-ground values are solver-authoritative",
                "C1 and C2 must remain close to U1",
                "estimated junction temperature must remain below the design limit",
                "input and output rails require accessible test points",
            ],
            rationale="Qualified low-loss TLV1117 adjustable LDO family",
        )

    if solved.topology != BUCK_TOPOLOGY:
        raise ValueError(f"unknown solved topology {solved.topology!r}")
    refs = [
        "J1", "C1", "U1", "D1", "L1", "C2", "J2", "R1", "R2",
        "TP1", "TP2", "TP3", "TP4", "#FLG01", "#FLG02",
    ]
    if solved.include_led:
        refs.extend(["R3", "D2", "TP5"])
    connections = _common_connections(vin, vout, "FB") + [
        BoardConnection(net=vin, ref="C1", pin="1"),
        BoardConnection(net="GND", ref="C1", pin="2"),
        BoardConnection(net=vin, ref="U1", pin="1"),
        BoardConnection(net="SW", ref="U1", pin="2"),
        BoardConnection(net="GND", ref="U1", pin="3"),
        BoardConnection(net="FB", ref="U1", pin="4"),
        BoardConnection(net="GND", ref="U1", pin="5"),
        BoardConnection(net="SW", ref="D1", pin="1"),
        BoardConnection(net="GND", ref="D1", pin="2"),
        BoardConnection(net="SW", ref="L1", pin="1"),
        BoardConnection(net=vout, ref="L1", pin="2"),
        BoardConnection(net=vout, ref="C2", pin="1"),
        BoardConnection(net="GND", ref="C2", pin="2"),
        BoardConnection(net="FB", ref="R1", pin="1"),
        BoardConnection(net="GND", ref="R1", pin="2"),
        BoardConnection(net=vout, ref="R2", pin="1"),
        BoardConnection(net="FB", ref="R2", pin="2"),
    ]
    if solved.include_led:
        _append_indicator(connections, vout, "D2")
    positions = {
        "J1": (7, 20), "C1": (18, 20), "U1": (38, 20),
        "D1": (45, 34), "L1": (54, 20), "C2": (70, 20),
        "J2": (82, 20), "R1": (31, 39), "R2": (37, 39),
        "TP1": (13, 50), "TP2": (72, 50), "TP3": (24, 50),
        "TP4": (42, 43),
    }
    if solved.include_led:
        positions.update({
            "R3": (58, 43), "D2": (67, 43), "TP5": (62, 50)})
    return BoardPlan(
        topology=BUCK_TOPOLOGY,
        family_version=solved.family_version,
        catalog_version=solved.catalog_version,
        components=[_component(ref, solved) for ref in refs],
        connections=connections,
        outline=BoardOutline(width=90, height=60),
        design_limits=_limits(spec, solved),
        placement_hints=_hints(positions),
        net_classes={
            vin: BoardNetClass(track_width_mm=1.5, clearance_mm=0.3),
            vout: BoardNetClass(track_width_mm=1.5, clearance_mm=0.3),
            # The LM2596 TO-263 pins use 1.7 mm pitch. This still carries
            # the qualified 0.5 A load while leaving a manufacturable fanout.
            "SW": BoardNetClass(track_width_mm=1.2, clearance_mm=0.3),
            "GND": BoardNetClass(track_width_mm=1.5, clearance_mm=0.3),
            "FB": BoardNetClass(track_width_mm=0.25, clearance_mm=0.3),
        },
        required_gates=list(solved.required_gates),
        constraints=[
            "LM2596 feedback divider and power-stage values are solver-authoritative",
            "U1, D1, L1, C1, and C2 form a compact power loop",
            "the SW copper extent must remain bounded",
            "feedback components must stay close to U1 and outside the SW loop",
            "the shielded inductor peak current must satisfy catalog derating",
        ],
        rationale="Qualified LM2596 asynchronous Buck family",
    )

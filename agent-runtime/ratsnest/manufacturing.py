"""Manufacturing outputs and trusted-catalog reconciliation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ratsnest.catalog import ComponentCatalog, load_catalog
from ratsnest.crews.contracts import BoardComponent, BoardPlan
from ratsnest.schemas import DesignSpec


class BomLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refs: list[str]
    quantity: int = Field(gt=0)
    role: str
    value: str
    manufacturer: str
    mpn: str
    catalog_id: str
    footprint: str
    lifecycle: str
    datasheet: str


class ManufacturingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "ratsnest.manufacturing.v1"
    catalog_version: str
    topology: str
    family_version: str
    design_spec: dict[str, Any]
    design_limits: dict[str, Any]
    bom: list[BomLine]
    non_bom_components: list[str] = Field(default_factory=list)


def _property(component: BoardComponent, name: str) -> str:
    return str(component.properties.get(name, "")).strip()


def catalog_issues(plan: BoardPlan,
                   catalog: ComponentCatalog | None = None) -> list[str]:
    catalog = catalog or load_catalog()
    issues: list[str] = []
    if plan.catalog_version != catalog.version:
        issues.append(
            f"plan catalog {plan.catalog_version!r} != runtime {catalog.version!r}")
    for component in plan.components:
        try:
            entry = catalog.entry(component.catalog_id)
        except Exception as exc:
            issues.append(f"{component.ref}: {exc}")
            continue
        comparisons = {
            "symbol": (component.symbol, entry.symbol),
            "footprint": (component.footprint, entry.footprint),
            "in_bom": (component.in_bom, entry.in_bom),
            "on_board": (component.on_board, entry.on_board),
            "manufacturer": (_property(component, "Manufacturer"),
                             entry.manufacturer),
            "lifecycle": (_property(component, "Lifecycle"), entry.lifecycle),
            "datasheet": (_property(component, "Datasheet"),
                          entry.datasheet_url),
        }
        for field, (actual, expected) in comparisons.items():
            if actual != expected:
                issues.append(
                    f"{component.ref}: catalog {field} mismatch "
                    f"({actual!r} != {expected!r})")
        mpn = _property(component, "MPN")
        if component.in_bom and (not mpn or mpn in {"DNP", "value-coded"}):
            issues.append(f"{component.ref}: BOM component has no exact MPN")
        if entry.mpn != "value-coded" and mpn != entry.mpn:
            issues.append(f"{component.ref}: MPN differs from trusted catalog")

    limits = plan.design_limits
    if limits is None:
        return issues + ["plan has no typed design limits"]
    by_role = {component.role: component for component in plan.components}

    def rating(role: str, key: str) -> float | None:
        component = by_role.get(role)
        if component is None:
            issues.append(f"required role {role!r} is missing")
            return None
        return catalog.entry(component.catalog_id).ratings.get(key)

    controller_role = (
        "linear_regulator" if plan.topology == "adjustable_ldo"
        else "buck_regulator")
    vin_max = rating(controller_role, "vin_max_v")
    iout_max = rating(controller_role, "iout_max_a")
    if vin_max is not None and limits.input_voltage_v > 0.875 * vin_max:
        issues.append("controller input voltage violates 87.5% derating")
    if iout_max is not None and limits.output_current_a > 0.75 * iout_max:
        issues.append("controller output current violates 75% derating")

    connector_rating = rating("input_connector", "current_a")
    if (connector_rating is not None
            and limits.output_current_a > 0.8 * connector_rating):
        issues.append("input connector current violates 80% derating")

    if plan.topology == "asynchronous_buck":
        reverse = rating("catch_diode", "reverse_voltage_v")
        diode_current = rating("catch_diode", "average_current_a")
        inductor_current = rating("power_inductor", "isat_10pct_a")
        cin_voltage = rating("input_bulk", "voltage_v")
        cout_voltage = rating("output_bulk", "voltage_v")
        if reverse is not None and reverse < 1.1 * limits.input_voltage_v:
            issues.append("catch diode reverse-voltage derating failed")
        if diode_current is not None and diode_current < 1.5 * limits.output_current_a:
            issues.append("catch diode current derating failed")
        if inductor_current is not None and inductor_current < 1.25 * limits.output_current_a:
            issues.append("inductor current derating failed")
        if cin_voltage is not None and cin_voltage < 1.25 * limits.input_voltage_v:
            issues.append("input capacitor voltage derating failed")
        if cout_voltage is not None and cout_voltage < 1.25 * limits.output_voltage_v:
            issues.append("output capacitor voltage derating failed")
    else:
        cin_voltage = rating("input_stability", "voltage_v")
        cout_voltage = rating("output_stability", "voltage_v")
        if cin_voltage is not None and cin_voltage < 1.25 * limits.input_voltage_v:
            issues.append("LDO input capacitor voltage derating failed")
        if cout_voltage is not None and cout_voltage < 1.25 * limits.output_voltage_v:
            issues.append("LDO output capacitor voltage derating failed")
    return issues


def build_manifest(plan: BoardPlan, spec: DesignSpec,
                   catalog: ComponentCatalog | None = None,
                   ) -> ManufacturingManifest:
    catalog = catalog or load_catalog()
    issues = catalog_issues(plan, catalog)
    if issues:
        raise ValueError("catalog validation failed: " + "; ".join(issues))
    lines: list[BomLine] = []
    for component in plan.components:
        if not component.in_bom:
            continue
        entry = catalog.entry(component.catalog_id)
        lines.append(BomLine(
            refs=[component.ref], quantity=1, role=component.role,
            value=component.value, manufacturer=entry.manufacturer,
            mpn=_property(component, "MPN"), catalog_id=component.catalog_id,
            footprint=component.footprint, lifecycle=entry.lifecycle,
            datasheet=entry.datasheet_url))
    return ManufacturingManifest(
        catalog_version=catalog.version,
        topology=plan.topology,
        family_version=plan.family_version,
        design_spec=spec.model_dump(mode="json"),
        design_limits=(plan.design_limits.model_dump(mode="json")
                       if plan.design_limits else {}),
        bom=lines,
        non_bom_components=[component.ref for component in plan.components
                            if not component.in_bom],
    )


def write_manufacturing_outputs(project_dir: Path, plan: BoardPlan,
                                spec: DesignSpec) -> dict[str, Path]:
    project_dir = Path(project_dir)
    manifest = build_manifest(plan, spec)
    manifest_path = project_dir / "manufacturing_manifest.json"
    manifest_path.write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8")

    bom_path = project_dir / "bom.csv"
    with bom_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "refs", "quantity", "role", "value", "manufacturer", "mpn",
            "catalog_id", "footprint", "lifecycle", "datasheet"])
        writer.writeheader()
        for line in manifest.bom:
            row = line.model_dump(mode="json")
            row["refs"] = ",".join(line.refs)
            writer.writerow(row)
    return {"manifest": manifest_path, "bom": bom_path}


def read_manifest(project_dir: Path) -> ManufacturingManifest:
    path = Path(project_dir) / "manufacturing_manifest.json"
    return ManufacturingManifest.model_validate_json(
        path.read_text(encoding="utf-8"))

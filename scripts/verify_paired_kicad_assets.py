"""Read-only verification for the content-addressed paired-eval KiCad assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ratsnestpro.eda.footprints import footprint_pad_numbers, resolve_footprint  # noqa: E402
from ratsnestpro.eda.symbols import symbol_info  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _electrical_numbers(values: Any) -> set[str]:
    return {
        value
        for raw in values or ()
        if (value := str(raw or "").strip())
    }


def _observe_binding(binding: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    asset_id = str(binding.get("assetId", ""))
    symbol_id = str(binding.get("symbol", ""))
    footprint_id = str(binding.get("footprint", ""))
    info = symbol_info(symbol_id)
    footprint_path = resolve_footprint(footprint_id)
    pads = footprint_pad_numbers(footprint_id)
    if info is None or not info.get("path"):
        return None, [f"{asset_id}: symbol is not installed: {symbol_id}"]
    if footprint_path is None or pads is None:
        return None, [f"{asset_id}: footprint is not installed: {footprint_id}"]
    pins = _electrical_numbers(
        pin.get("number")
        for pin in info.get("pins", [])
        if isinstance(pin, dict)
    )
    pad_numbers = _electrical_numbers(pads)
    zero_pin_mechanical = (
        not pins
        and not pad_numbers
        and symbol_id.startswith("Mechanical:")
        and footprint_id.startswith("MountingHole:")
    )
    return {
        "symbolPinCount": len(pins),
        "footprintPadCount": len(pad_numbers),
        "pinPadCompatible": pins == pad_numbers and (bool(pins) or zero_pin_mechanical),
        "symbolLibrarySha256": _sha256(Path(str(info["path"]))),
        "footprintFileSha256": _sha256(footprint_path),
    }, []


def verify(manifest_path: Path) -> dict[str, Any]:
    raw = manifest_path.read_bytes()
    document = json.loads(raw)
    bindings = document.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise ValueError("manifest bindings must be a non-empty array")
    seen: set[str] = set()
    failures: list[str] = []
    for binding in bindings:
        asset_id = str(binding.get("assetId", ""))
        if not asset_id or asset_id in seen:
            failures.append(f"invalid or duplicate assetId: {asset_id!r}")
            continue
        seen.add(asset_id)
        checks, binding_failures = _observe_binding(binding)
        failures.extend(binding_failures)
        if checks is None:
            continue
        for key, actual in checks.items():
            if binding.get(key) != actual:
                failures.append(
                    f"{asset_id}: {key} expected {binding.get(key)!r}, got {actual!r}"
                )
    result = {
        "status": "ok" if not failures else "blocked",
        "manifestDigest": hashlib.sha256(raw).hexdigest(),
        "verifiedAssetCount": len(seen) - len({item.split(":", 1)[0] for item in failures}),
        "assetCount": len(bindings),
        "failures": failures,
    }
    return result


def freeze(
    manifest_path: Path,
    *,
    config_path: Path,
    plan_path: Path,
    blind_template_path: Path,
    runtime_image_id: str,
    kicad_version: str,
) -> dict[str, str]:
    """Explicitly refresh deployment-bound hashes; never called by evaluation."""

    manifest = json.loads(manifest_path.read_bytes())
    for binding in manifest["bindings"]:
        observed, failures = _observe_binding(binding)
        if failures or observed is None:
            raise ValueError("; ".join(failures))
        binding.update(observed)
    manifest["checkedAt"] = datetime.now(UTC).isoformat()
    manifest["deployedRuntimeImageId"] = runtime_image_id
    manifest["kicadVersion"] = kicad_version
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    asset_digest = _sha256(manifest_path)

    config = json.loads(config_path.read_bytes())
    config["assetSnapshotDigest"] = asset_digest
    config["deployedRuntimeImageId"] = runtime_image_id
    config["kicadVersion"] = kicad_version
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    config_digest = _sha256(config_path)

    plan = json.loads(plan_path.read_bytes())
    plan["assetManifestDigest"] = asset_digest
    plan["frozenExecution"]["environmentDigest"] = asset_digest
    plan["frozenExecution"]["configDigest"] = config_digest
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    plan_digest = _sha256(plan_path)

    blind = json.loads(blind_template_path.read_bytes())
    blind["planDigest"] = plan_digest
    blind_template_path.write_text(
        json.dumps(blind, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "assetManifestDigest": asset_digest,
        "frozenConfigDigest": config_digest,
        "planDigest": plan_digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "evals" / "paired" / "kicad-assets.v1.json",
    )
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--runtime-image-id")
    parser.add_argument("--kicad-version")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "evals" / "paired" / "frozen-config.v1.json",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=ROOT / "frontend" / "public" / "evals" / "paired-kicad-golden.v1.json",
    )
    parser.add_argument(
        "--blind-template",
        type=Path,
        default=ROOT / "evals" / "paired" / "blind-review-template.v1.json",
    )
    args = parser.parse_args()
    if args.freeze:
        if not args.runtime_image_id or not args.kicad_version:
            parser.error("--freeze requires --runtime-image-id and --kicad-version")
        digests = freeze(
            args.manifest.resolve(),
            config_path=args.config.resolve(),
            plan_path=args.plan.resolve(),
            blind_template_path=args.blind_template.resolve(),
            runtime_image_id=args.runtime_image_id,
            kicad_version=args.kicad_version,
        )
        print(json.dumps({"status": "frozen", **digests}, indent=2))
        return 0
    result = verify(args.manifest.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] != "ok")


if __name__ == "__main__":
    raise SystemExit(main())

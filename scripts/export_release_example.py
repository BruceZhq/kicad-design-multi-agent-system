"""Export explicit final engineering artifacts, never a raw runtime directory."""

import argparse
import hashlib
import json
import shutil
import re
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact_libraries(destination):
    """Keep referenced symbol definitions and their parents, not whole system libraries."""
    from ratsnestpro.eda.vendor.sexpr import loads, dumps, find_all, find_first, tag_of

    destination = Path(destination)
    evidence_path = destination / "release-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    schematic = destination / (evidence["release_identity"]["project_name"] + ".kicad_sch")
    identifiers = set(re.findall(r'\(lib_id\s+"([^\"]+)"\)', schematic.read_text(encoding="utf-8")))
    for path in (destination / ".ratsnest-libs" / "symbols").glob("*.kicad_sym"):
        root = loads(path.read_text(encoding="utf-8"))
        definitions = {str(node[1]): node for node in find_all(root, "symbol")}
        required = {
            identity.split(":", 1)[1]
            for identity in identifiers
            if identity.startswith(path.stem + ":")
        }
        pending = list(required)
        while pending:
            name = pending.pop()
            if name not in definitions:
                raise ValueError(f"missing symbol definition: {path.stem}:{name}")
            parent = find_first(definitions[name], "extends")
            if parent and str(parent[1]) not in required:
                required.add(str(parent[1]))
                pending.append(str(parent[1]))
        subset = [node for node in root if tag_of(node) != "symbol" or str(node[1]) in required]
        source_hash = digest(path)
        path.write_text(dumps(subset) + "\n", encoding="utf-8")
        relative = path.relative_to(destination).as_posix()
        for entry in evidence["files"]:
            if entry["path"] == relative:
                entry.setdefault("source_sha256", source_hash)
                entry.update(
                    sha256=digest(path),
                    bytes=path.stat().st_size,
                    transformation="selected symbol definitions and inherited parents; semantic content unchanged",
                )
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def export(source: Path, destination: Path):
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("export destination already exists; refusing to overwrite")
    result = json.loads((source / "pipeline_result.json").read_text(encoding="utf-8"))
    if result.get("release_ready") is not True or result.get("release_blockers"):
        raise ValueError("only a recorded release-ready result can be exported")
    identity = result["release_identity"]
    name = identity["project_name"]
    pcb = source / identity["pcb_relpath"]
    if pcb.parent != source or digest(pcb) != identity["pcb_sha256"]:
        raise ValueError("final PCB no longer matches release identity")
    paths = [
        source / (name + suffix)
        for suffix in (
            ".kicad_pcb",
            ".kicad_sch",
            ".kicad_pro",
            ".placement_constraints.json",
            ".erc.json",
            ".drc.json",
            "_production_bom.csv",
            "_procurement_bom.csv",
            "_cpl.csv",
        )
    ]
    paths += [source / "fp-lib-table", source / "sym-lib-table"]
    paths += list((source / "gerber").glob("*"))
    paths += list((source / ".ratsnest-libs").rglob("*.kicad_mod"))
    paths += list((source / ".ratsnest-libs").rglob("*.kicad_sym"))
    # Validate every input before copying anything; final CAD remains byte-identical.
    for path in paths:
        resolved = path.resolve()
        installed_library = path.is_relative_to(source / ".ratsnest-libs") and any(
            resolved.is_relative_to(root)
            for root in (Path("/usr/share/kicad/footprints"), Path("/usr/share/kicad/symbols"))
        )
        if not path.is_file() or not (resolved.is_relative_to(source) or installed_library):
            raise ValueError(f"missing or unsafe final artifact: {path.name}")
    destination.mkdir(parents=True)
    files = []
    for path in paths:
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files.append(
            {"path": relative.as_posix(), "sha256": digest(target), "bytes": target.stat().st_size}
        )

    def portable(value):
        if isinstance(value, dict):
            return {key: portable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [portable(item) for item in value]
        if isinstance(value, str):
            return value.replace(str(result["run_directory"]).rstrip("/"), ".")
        return value

    evidence = {
        "example": "stm32g070-assisted-release-20260908",
        "provenance": "agent workflow with Codex-assisted physical repair; not an autonomous benchmark",
        "release_ready": True,
        "completed_steps": result["completed_steps"],
        "total_steps": result["total_steps"],
        "release_identity": identity,
        "verification": portable(result["verification"]),
        "routing": {
            key: result["routing"].get(key)
            for key in (
                "method",
                "unconnected",
                "routed_nets",
                "total_nets",
                "routed_connections",
                "total_connections",
            )
        },
        "files": files,
        "excluded": "private checkpoints, conversations, credentials, raw logs, manufacturer PDFs and stale pre-repair router exchange files",
    }
    (destination / "release-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    compact_libraries(destination)
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(f"Exported {export(args.source, args.destination)} final artifacts with SHA-256 evidence")

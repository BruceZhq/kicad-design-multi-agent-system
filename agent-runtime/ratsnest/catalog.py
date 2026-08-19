"""Typed access to the immutable Stage 3 component catalog."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CatalogError(ValueError):
    pass


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_id: str
    kind: str
    manufacturer: str
    mpn: str
    value: str
    symbol: str
    footprint: str = ""
    lifecycle: str
    source_url: str
    datasheet_url: str
    ratings: dict[str, float] = Field(default_factory=dict)
    pin_roles: dict[str, str] = Field(default_factory=dict)
    in_bom: bool = True
    on_board: bool = True

    @model_validator(mode="after")
    def validate_trusted_entry(self) -> "CatalogEntry":
        if self.lifecycle not in {"active", "virtual"}:
            raise ValueError("catalog only accepts active or virtual entries")
        if self.in_bom and (not self.mpn or self.mpn == "DNP"):
            raise ValueError("BOM catalog entries require an MPN")
        if self.on_board and not self.footprint:
            raise ValueError("physical catalog entries require a footprint")
        if not self.source_url.startswith("https://"):
            raise ValueError("catalog source must be HTTPS")
        return self


class ComponentCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    entries: dict[str, CatalogEntry]

    def entry(self, catalog_id: str) -> CatalogEntry:
        try:
            return self.entries[catalog_id]
        except KeyError as exc:
            raise CatalogError(f"unknown trusted catalog id {catalog_id!r}") from exc


@lru_cache(maxsize=1)
def load_catalog() -> ComponentCatalog:
    path = files("ratsnest.catalogs").joinpath("stage3.yaml")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = {
        catalog_id: CatalogEntry.model_validate({
            "catalog_id": catalog_id,
            **payload,
        })
        for catalog_id, payload in raw.get("entries", {}).items()
    }
    if not entries:
        raise CatalogError("trusted component catalog is empty")
    return ComponentCatalog(version=str(raw.get("version", "")), entries=entries)


def led_catalog_id(color: str) -> str:
    ids = {
        "red": "liteon.ltst-c170krkt",
        "green": "liteon.ltst-c170gkt",
        "blue": "liteon.ltst-c170tbkt",
    }
    try:
        return ids[color.lower()]
    except KeyError as exc:
        raise CatalogError(
            f"Stage 3 catalog supports red, green, or blue LEDs, got {color!r}") from exc

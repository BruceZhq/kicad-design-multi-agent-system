"""Strategy registry: versioned YAML bundles, ACTIVE pointer, promote/rollback.

Layout:
    strategies/
      ACTIVE            first line = active version dir name; older lines = history
      v0/strategy.yaml
      v1/strategy.yaml  (promoted candidates)

Every run stamps the content-hash version_id of the bundle it used; the dir
name is a human handle, the hash is the identity.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ratsnest.config import Config
from ratsnest.schemas import StrategyBundle


def load_strategy(version_dir: Path) -> StrategyBundle:
    data = yaml.safe_load((version_dir / "strategy.yaml").read_text(encoding="utf-8"))
    return StrategyBundle.model_validate(data)


class StrategyRegistry:
    def __init__(self, strategies_dir: Path | None = None):
        self.dir = Path(strategies_dir or Config.load().strategies_dir)
        self._active_file = self.dir / "ACTIVE"

    # -- read ---------------------------------------------------------------
    def list_versions(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir()
                      if p.is_dir() and (p / "strategy.yaml").exists())

    def active_name(self) -> str:
        if self._active_file.exists():
            lines = self._active_file.read_text(encoding="utf-8").split()
            if lines:
                return lines[0]
        versions = self.list_versions()
        if not versions:
            raise FileNotFoundError(f"no strategy versions in {self.dir}")
        return versions[0]

    def load(self, name: str) -> StrategyBundle:
        return load_strategy(self.dir / name)

    def load_active(self) -> tuple[str, StrategyBundle]:
        name = self.active_name()
        return name, self.load(name)

    def load_exact(self, name: str, version_id: str) -> StrategyBundle:
        """Load a named strategy and prove its content identity."""
        strategy = self.load(name)
        actual = strategy.version_id()
        if actual != version_id:
            raise ValueError(
                f"strategy {name!r} changed: expected {version_id}, got {actual}")
        return strategy

    # -- write (control-plane actions; rollback is first-class) --------------
    def save_candidate(self, bundle: StrategyBundle, name: str) -> Path:
        vdir = self.dir / name
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "strategy.yaml").write_text(
            yaml.safe_dump(bundle.model_dump(mode="json"), sort_keys=False,
                           allow_unicode=True),
            encoding="utf-8",
        )
        return vdir

    def promote(self, name: str) -> None:
        """Make `name` the active version, pushing the old one onto history."""
        if not (self.dir / name / "strategy.yaml").exists():
            raise FileNotFoundError(f"strategy version {name!r} not found")
        history = []
        if self._active_file.exists():
            history = self._active_file.read_text(encoding="utf-8").split()
        history = [name] + [h for h in history if h != name]
        self._active_file.write_text("\n".join(history), encoding="utf-8")

    def rollback(self) -> str:
        """One-command rollback to the previous active version."""
        history = self._active_file.read_text(encoding="utf-8").split()
        if len(history) < 2:
            raise RuntimeError("no previous strategy version to roll back to")
        history = history[1:]
        self._active_file.write_text("\n".join(history), encoding="utf-8")
        return history[0]

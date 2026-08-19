"""Runtime configuration, resolved from environment variables with local defaults."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# agent-runtime/ratsnest/config.py -> repo root is two levels up from package dir
REPO_ROOT = Path(__file__).resolve().parents[2]

_KICAD_HAPPY_CANDIDATES = (
    REPO_ROOT.parent,
    REPO_ROOT.parent / "kicad-happy-main",
)
_DEFAULT_KICAD_HAPPY_ROOT = next(
    (candidate for candidate in _KICAD_HAPPY_CANDIDATES
     if (candidate / "skills" / "kicad").is_dir()),
    _KICAD_HAPPY_CANDIDATES[-1],
)
_PROGRAM_FILES = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
_KICAD_BIN_DIRS = tuple(
    _PROGRAM_FILES / "KiCad" / version / "bin"
    for version in ("10.0", "9.0", "8.0")
)
_DEFAULT_FREEROUTING_JAR = Path.home() / ".kicad-mcp" / "freerouting.jar"
_DEFAULT_FREEROUTING_JAVA = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "RatsNest" / "tools" / "temurin-25-jre" / "bin" / "java.exe")


def _apply_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE defaults from .env (repo root). Real environment
    variables always win — docker-compose / the control plane inject vars
    that must never be overridden. utf-8-sig tolerates PowerShell BOMs."""
    path = path or REPO_ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass
class Config:
    kicad_happy_root: Path
    kicad_cli: Path | None
    runs_dir: Path
    strategies_dir: Path
    benchmarks_dir: Path
    control_plane_url: str | None
    mcp_server_dir: Path | None = None
    kicad_python: Path | None = None
    llm_api_key: str | None = None
    llm_provider: str = "anthropic"  # anthropic|openai|deepseek|qwen|moonshot|zhipu|ollama
    llm_base_url: str = ""           # empty -> provider preset
    llm_model: str = ""              # empty -> provider preset
    llm_enabled: bool = True         # RATSNEST_LLM=off disables
    llm_required: bool = False       # RATSNEST_LLM=require forbids fallback
    llm_timeout_seconds: float = 60.0
    llm_retries: int = 2
    llm_max_calls: int = 32
    llm_max_tokens_per_call: int = 3000
    llm_max_total_tokens: int = 40000
    llm_model_routes: dict[str, str] = field(default_factory=dict)
    routing_mode: str = "freerouting"  # freerouting|direct|none
    freerouting_jar: Path | None = None
    freerouting_java: Path | None = None
    freerouting_max_passes: int = 5
    freerouting_timeout_seconds: int = 120
    ngspice_library: Path | None = None

    @property
    def kicad_scripts(self) -> Path:
        return self.kicad_happy_root / "skills" / "kicad" / "scripts"

    @property
    def bom_scripts(self) -> Path:
        return self.kicad_happy_root / "skills" / "bom" / "scripts"

    @classmethod
    def load(cls) -> "Config":
        _apply_dotenv()
        kh_root = Path(
            os.environ.get(
                "RATSNEST_KICAD_HAPPY_ROOT",
                str(_DEFAULT_KICAD_HAPPY_ROOT),
            )
        )
        kicad_cli = _first_existing(
            os.environ.get("RATSNEST_KICAD_CLI"),
            shutil.which("kicad-cli"),
            *(_dir / "kicad-cli.exe" for _dir in _KICAD_BIN_DIRS),
        )
        return cls(
            kicad_happy_root=kh_root,
            kicad_cli=kicad_cli,
            runs_dir=Path(os.environ.get("RATSNEST_RUNS_DIR", str(REPO_ROOT / "runs"))),
            strategies_dir=Path(
                os.environ.get(
                    "RATSNEST_STRATEGIES_DIR",
                    str(REPO_ROOT / "agent-runtime" / "strategies"),
                )
            ),
            benchmarks_dir=Path(
                os.environ.get("RATSNEST_BENCHMARKS_DIR", str(REPO_ROOT / "benchmarks"))
            ),
            control_plane_url=os.environ.get("RATSNEST_CONTROL_PLANE_URL"),
            mcp_server_dir=_first_existing(
                os.environ.get("RATSNEST_MCP_SERVER"),
                REPO_ROOT.parent / "KiCAD-MCP-Server-main",
            ),
            kicad_python=_first_existing(
                os.environ.get("RATSNEST_KICAD_PYTHON"),
                *(_dir / "python.exe" for _dir in _KICAD_BIN_DIRS),
            ),
            llm_api_key=(os.environ.get("RATSNEST_LLM_API_KEY")
                         or os.environ.get("ANTHROPIC_API_KEY")
                         or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                         or os.environ.get("OPENAI_API_KEY")),
            llm_provider=os.environ.get(
                "RATSNEST_LLM_PROVIDER", "anthropic").lower(),
            llm_base_url=(os.environ.get("RATSNEST_LLM_BASE_URL")
                          or (os.environ.get("ANTHROPIC_BASE_URL")
                              if os.environ.get("RATSNEST_LLM_PROVIDER",
                                                "anthropic") == "anthropic"
                              else "")
                          or ""),
            llm_model=os.environ.get("RATSNEST_LLM_MODEL", ""),
            llm_enabled=os.environ.get("RATSNEST_LLM", "auto") != "off",
            llm_required=os.environ.get("RATSNEST_LLM", "auto") == "require",
            llm_timeout_seconds=_bounded_float(
                "RATSNEST_LLM_TIMEOUT_SECONDS", 60.0, 5.0, 300.0),
            llm_retries=_bounded_int("RATSNEST_LLM_RETRIES", 2, 0, 5),
            llm_max_calls=_bounded_int(
                "RATSNEST_LLM_MAX_CALLS", 32, 1, 200),
            llm_max_tokens_per_call=_bounded_int(
                "RATSNEST_LLM_MAX_TOKENS_PER_CALL", 3000, 100, 16000),
            llm_max_total_tokens=_bounded_int(
                "RATSNEST_LLM_MAX_TOTAL_TOKENS", 40000, 500, 500000),
            llm_model_routes=_model_routes(),
            routing_mode=_choice(
                "RATSNEST_ROUTING_MODE", "freerouting",
                {"freerouting", "direct", "none"}),
            freerouting_jar=_first_existing(
                os.environ.get("RATSNEST_FREEROUTING_JAR"),
                _DEFAULT_FREEROUTING_JAR),
            freerouting_java=_first_existing(
                os.environ.get("RATSNEST_FREEROUTING_JAVA"),
                _DEFAULT_FREEROUTING_JAVA),
            freerouting_max_passes=_bounded_int(
                "RATSNEST_FREEROUTING_MAX_PASSES", 5, 1, 100),
            freerouting_timeout_seconds=_bounded_int(
                "RATSNEST_FREEROUTING_TIMEOUT_SECONDS", 120, 30, 1800),
            ngspice_library=_first_existing(
                os.environ.get("RATSNEST_NGSPICE_LIBRARY"),
                *(_dir / "ngspice.dll" for _dir in _KICAD_BIN_DIRS)),
        )


def _first_existing(*candidates) -> Path | None:
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return None


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _bounded_float(name: str, default: float,
                   minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _choice(name: str, default: str, choices: set[str]) -> str:
    value = os.environ.get(name, default).strip().lower()
    return value if value in choices else default


def _model_routes() -> dict[str, str]:
    raw = os.environ.get("RATSNEST_LLM_MODEL_ROUTES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(agent): str(model)
        for agent, model in parsed.items()
        if isinstance(agent, str) and isinstance(model, str)
        and agent and model
    }

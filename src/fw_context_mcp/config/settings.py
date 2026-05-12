"""fw-context configuration loader.

Hierarchy (later overrides earlier):
  1. built-in defaults
  2. ~/.fw-context/config.toml      (global, per-user)
  3. <project>/.fw-context/config.toml  (project, committable)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

_GLOBAL_CONFIG_PATH = Path.home() / ".fw-context" / "config.toml"
_PROJECT_CONFIG_DIR = ".fw-context"
_PROJECT_CONFIG_NAME = "config.toml"

_GLOBAL_DEFAULTS = """\
[index]
db_dir = "~/.fw-context/index"

[llm]
ollama_url = "http://localhost:11434"
model = "codestral:latest"
num_ctx = 8192
cloud_model = "devstral-small-2:cloud"
"""

_PROJECT_DEFAULTS_TEMPLATE = """\
[project]
# name = "my-project"   # defaults to directory name

[index]
compile_commands = "compile_commands.json"
source_roots = ["src", "lib"]

# [llm]
# model = "qwen2.5-coder:7b-q4_K_M"   # override global model for this project
"""


@dataclass
class LLMConfig:
    ollama_url: str = "http://localhost:11434"
    model: str = "codestral:latest"
    cloud_model: str = "devstral-small-2:cloud"
    num_ctx: int = 8192
    debug_log: Path | None = None


@dataclass
class IndexConfig:
    db_dir: Path = field(default_factory=lambda: Path.home() / ".fw-context" / "index")
    compile_commands: Path = field(default_factory=lambda: Path("compile_commands.json"))
    source_roots: list[str] = field(default_factory=lambda: ["src", "lib"])
    exclude_paths: list[str] = field(default_factory=list)


@dataclass
class ProjectMeta:
    name: str | None = None


@dataclass
class Config:
    project: ProjectMeta = field(default_factory=ProjectMeta)
    index: IndexConfig = field(default_factory=IndexConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    def source_root_paths(self, project_root: Path) -> list[Path]:
        return [
            (project_root / r).resolve()
            for r in self.index.source_roots
            if (project_root / r).exists()
        ]

    def exclude_root_paths(self, project_root: Path) -> list[Path]:
        return [(project_root / p).resolve() for p in self.index.exclude_paths]


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _from_dict(data: dict) -> Config:
    cfg = Config()

    if proj := data.get("project", {}):
        if name := proj.get("name"):
            cfg.project.name = name

    if idx := data.get("index", {}):
        if db_dir := idx.get("db_dir"):
            cfg.index.db_dir = Path(db_dir).expanduser()
        if cc := idx.get("compile_commands"):
            cfg.index.compile_commands = Path(cc)
        if roots := idx.get("source_roots"):
            cfg.index.source_roots = roots
        if excludes := idx.get("exclude_paths"):
            cfg.index.exclude_paths = excludes

    if llm := data.get("llm", {}):
        if url := llm.get("ollama_url"):
            cfg.llm.ollama_url = url
        if model := llm.get("model"):
            cfg.llm.model = model
        if cloud := llm.get("cloud_model"):
            cfg.llm.cloud_model = cloud
        if num_ctx := llm.get("num_ctx"):
            cfg.llm.num_ctx = int(num_ctx)
        if debug_log := llm.get("debug_log"):
            cfg.llm.debug_log = Path(debug_log).expanduser()

    return cfg


def _ensure_global_config() -> Path:
    path = _GLOBAL_CONFIG_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_GLOBAL_DEFAULTS)
    return path


def _ensure_project_config(project_root: Path) -> Path:
    config_dir = project_root / _PROJECT_CONFIG_DIR
    path = config_dir / _PROJECT_CONFIG_NAME
    if not path.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(_PROJECT_DEFAULTS_TEMPLATE)
    return path


def load(project_root: Path | None = None) -> Config:
    """Load merged config for the given project root.

    Creates default config files on first use.
    """
    global_path = _ensure_global_config()
    data = tomllib.loads(global_path.read_text())

    if project_root is not None:
        proj_path = _ensure_project_config(project_root)
        proj_data = tomllib.loads(proj_path.read_text())
        data = _deep_merge(data, proj_data)

    cfg = _from_dict(data)
    cfg.index.db_dir = cfg.index.db_dir.expanduser().resolve()

    return cfg


def derive_project_id(root: Path) -> str:
    """Derive a stable project_id from git remote URL or directory path."""
    import hashlib
    import subprocess

    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return hashlib.sha256(url.encode()).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]

"""fw-context configuration loader.

Hierarchy (later overrides earlier):
  1. built-in defaults
  2. ~/.fw-context/config.toml      (global, per-user)
  3. <project>/.fw-context/config.toml  (project, committable)
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..indexer.build import BuildConfig

_GLOBAL_CONFIG_PATH = Path.home() / ".fw-context" / "config.toml"
_PROJECT_CONFIG_DIR = ".fw-context"
_PROJECT_CONFIG_NAME = "config.toml"

_GLOBAL_DEFAULTS = """\
[index]
db_dir = "~/.fw-context/index"

[llm]
# enabled = true   # set to false to disable Ollama and return raw prompts for the agent
ollama_url = "http://localhost:11434"
model = "qwen2.5-coder:14b"
num_ctx = 8192
# debug_log = "~/.fw-context/llm-debug.jsonl"   # write LLM prompts/responses to JSONL
"""

_PROJECT_DEFAULTS_TEMPLATE = """\
[project]
# name = "my-project"   # defaults to directory name

[build]
# system = "mbed-os"        # auto-detected: mbed-os, zephyr, platformio
# clean = true              # always clean build before indexing (recommended)
# command = "bear -- make"  # full override — bypasses all detection
#
# Mbed OS overrides (auto-detected from .mbed):
# target = "P_ECB_BOARD"
# toolchain = "GCC_ARM"
# profile = "Develop"
# app_config = "mbed_app.json"
# extra_profiles = ["lto.json"]
#
# Zephyr override:
# board = "nrf52840dk_nrf52840"

[index]
compile_commands = "compile_commands.json"
# Generate it with:  fw-context index --build  (auto-detects build system)
# source_roots: directories to index symbols from.
#   Empty list = auto-detect (scans for src, lib, app, include, zephyr, mbed-os,
#   modules + top-level directories from compile_commands.json).
#   Set explicitly to narrow indexing to specific directories.
source_roots = []
exclude_paths = ["build", "BUILD"]

# index_refs: build a cross-reference / call graph.
#   On by default — enables find_callers, find_call_path, find_dead_code, etc.
#   Set false to disable for faster indexing on very large projects.
# index_refs = false

# [llm]
# enabled = false   # disable Ollama, return raw prompts for the agent to answer
# ollama_url = "http://localhost:11434"
# model = "qwen2.5-coder:7b-q4_K_M"   # override global model for this project
# num_ctx = 8192
# debug_log = "~/.fw-context/llm-debug.jsonl"   # write LLM prompts/responses to JSONL
"""


@dataclass
class LLMConfig:
    enabled: bool = True
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen2.5-coder:14b"
    embed_model: str = "mxbai-embed-large:latest"
    num_ctx: int = 8192
    debug_log: Path | None = None


@dataclass
class IndexConfig:
    db_dir: Path = field(default_factory=lambda: Path.home() / ".fw-context" / "index")
    compile_commands: Path = field(default_factory=lambda: Path("compile_commands.json"))
    source_roots: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=lambda: ["build", "BUILD"])
    index_refs: bool = True
    index_embeddings: bool = True


@dataclass
class ProjectMeta:
    name: str | None = None


@dataclass
class Config:
    project: ProjectMeta = field(default_factory=ProjectMeta)
    build: BuildConfig = field(default_factory=BuildConfig)
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

    if build := data.get("build", {}):
        if system := build.get("system"):
            cfg.build.system = system
        if "clean" in build:
            cfg.build.clean = bool(build["clean"])
        if command := build.get("command"):
            cfg.build.command = command
        if target := build.get("target"):
            cfg.build.target = target
        if toolchain := build.get("toolchain"):
            cfg.build.toolchain = toolchain
        if profile := build.get("profile"):
            cfg.build.profile = profile
        if app_config := build.get("app_config"):
            cfg.build.app_config = app_config
        if extra := build.get("extra_profiles"):
            cfg.build.extra_profiles = list(extra)
        if board := build.get("board"):
            cfg.build.board = board

    if idx := data.get("index", {}):
        if db_dir := idx.get("db_dir"):
            cfg.index.db_dir = Path(db_dir).expanduser()
        if cc := idx.get("compile_commands"):
            cfg.index.compile_commands = Path(cc)
        if roots := idx.get("source_roots"):
            cfg.index.source_roots = roots
        if excludes := idx.get("exclude_paths"):
            cfg.index.exclude_paths = excludes
        if "index_refs" in idx:
            cfg.index.index_refs = bool(idx["index_refs"])
        if "index_embeddings" in idx:
            cfg.index.index_embeddings = bool(idx["index_embeddings"])

    if llm := data.get("llm", {}):
        if "enabled" in llm:
            cfg.llm.enabled = bool(llm["enabled"])
        if url := llm.get("ollama_url"):
            cfg.llm.ollama_url = url
        if model := llm.get("model"):
            cfg.llm.model = model
        if embed_model := llm.get("embed_model"):
            cfg.llm.embed_model = embed_model
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


def _is_loopback_url(url: str) -> bool:
    """True if *url*'s host is a loopback address (localhost / 127.0.0.0/8 / ::1)."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "::1"} or host.startswith("127.")


def load(project_root: Path | None = None) -> Config:
    """Load merged config for the given project root.

    Creates default config files on first use.  TOML parse errors in user
    configs are logged and the built-in defaults are used instead — a broken
    config file must not prevent the MCP tools from loading.
    """
    import logging

    log = logging.getLogger(__name__)
    data: dict = {}

    global_path = _ensure_global_config()
    try:
        data = tomllib.loads(global_path.read_text())
    except Exception:
        log.exception("Failed to parse %s — using defaults", global_path)

    if project_root is not None:
        proj_path = _ensure_project_config(project_root)
        try:
            proj_data = tomllib.loads(proj_path.read_text())
            data = _deep_merge(data, proj_data)
            # Security: a committed project config can redirect LLM calls (which
            # include source-code snippets) to an arbitrary host. Warn loudly.
            proj_url = (proj_data.get("llm") or {}).get("ollama_url")
            if proj_url and not _is_loopback_url(proj_url):
                log.warning(
                    "Project config %s sets a non-local ollama_url (%s). "
                    "Source-code snippets in LLM prompts will be sent to that host.",
                    proj_path, proj_url,
                )
        except Exception:
            log.exception("Failed to parse %s — ignoring project config", proj_path)

    cfg = _from_dict(data)
    cfg.index.db_dir = cfg.index.db_dir.expanduser().resolve()

    return cfg


def derive_project_id(root: Path) -> str:
    """Derive a stable project_id from git remote URL or directory path."""
    import hashlib
    import logging
    import subprocess

    log = logging.getLogger(__name__)

    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return hashlib.sha256(url.encode()).hexdigest()[:16]
    except Exception:
        log.debug("git remote origin not found for %s, using path hash", root)
        return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]

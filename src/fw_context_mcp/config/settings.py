"""fw-context configuration loader.

Hierarchy (later overrides earlier):
  1. built-in defaults
  2. ~/.fw-context/config.toml           (global, per-user)
  3. <project>/.fw-context/config.toml   (project, shared — commit to git)
  4. <project>/.fw-context/local.toml    (project, local — gitignore)
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..indexer.build import BuildConfig

__all__ = ["CacheServerConfig", "Config", "LLMConfig", "IndexConfig", "ProjectMeta", "load", "derive_project_id"]

_GLOBAL_CONFIG_PATH = Path.home() / ".fw-context" / "config.toml"
_PROJECT_CONFIG_DIR = ".fw-context"
_PROJECT_CONFIG_NAME = "config.toml"
_PROJECT_LOCAL_CONFIG_NAME = "local.toml"

_GLOBAL_DEFAULTS = """\
[index]
db_dir = "~/.fw-context/index"

[llm]
# enabled = true   # set to false to disable Ollama and return raw prompts for the agent
ollama_url = "http://localhost:11434"

# Chat model for symbolic analysis and search query generation.
# Minimum recommended: 14B parameters — smaller models produce inconsistent JSON
# and poor-quality descriptions for C++ embedded code.
#
# Recommended models (min. 14B parameters):
#   Local:
#     qwen2.5-coder:14b        (4.8 GB VRAM, balanced speed/quality)
#     deepseek-coder-v2:16b    (10 GB VRAM, strong code comprehension)
#     qwen2.5-coder:32b        (19 GB VRAM, best local quality)
#   Cloud (Ollama proxies to cloud APIs — "ollama pull <model>"):
#     qwen3-coder:480b-cloud         (coding-focused, best quality)
#     deepseek-v4-flash:cloud        (284B MoE, 13B active — great value)
#     gemini-3-flash-preview:latest-cloud   (fast, affordable)
#   See: https://ollama.com/search?c=cloud
model = "qwen2.5-coder:14b"
num_ctx = 16384
# keep_alive = "10m"   # how long to keep model loaded in VRAM after each request
# timeout = 600.0   # HTTP timeout for Ollama requests in seconds (default 600)
# debug_log = "~/.fw-context/llm-debug.jsonl"   # write LLM prompts/responses to JSONL

# Embedding model for semantic_search.
# Pull with:  ollama pull <model>
# Uncomment exactly ONE of the sections below.
#
# ── Default: mxbai-embed-large (~670 MB VRAM, no GPU needed) ──
# embed_model = "mxbai-embed-large:latest"
# embed_query_prompt = "Represent this sentence for searching relevant passages: "
# embed_doc_prompt = ""
#
# ── Higher quality: qwen3-embedding:8b (~4.7 GB VRAM, GPU recommended) ──
# embed_model = "qwen3-embedding:8b"
# embed_query_prompt = "Retrieve C/C++ functions, types, symbols, and implementation code relevant to the query."
# embed_doc_prompt = ""

analyze_symbols = true
"""

_PROJECT_DEFAULTS_TEMPLATE = """\
[project]
# name = "my-project"   # defaults to directory name

[build]
# Build system auto-detection: scans for .mbed, west.yml, or platformio.ini.
# Set system explicitly when auto-detection fails or you need to force a specific one.
# system = "mbed-os"        # choices: mbed-os, zephyr, platformio
# clean = true              # always clean build before indexing (recommended)
# command = "bear -- make"  # full override — bypasses all detection

# ── Mbed OS ──────────────────────────────────────────────────────────────
# Most values are auto-detected from .mbed and custom_targets.json.
# Override them here when auto-detection is wrong or incomplete.
#
# target = "P_ECB_BOARD"
# toolchain = "GCC_ARM"
# profile = "develop"
# app_config = "mbed_app.json"
# extra_profiles = ["lto.json"]
# defines = ["VERSION_FW_MAJOR=4", "DEV"]  # extra -D macros passed to compiler

# ── Zephyr ───────────────────────────────────────────────────────────────
# board is REQUIRED — there is no safe auto-detection for Zephyr boards.
# Set system = "zephyr" if west.yml coexists with another build system marker.
#
# system = "zephyr"
# board = "nrf52840dk_nrf52840"

# ── PlatformIO ───────────────────────────────────────────────────────────
# Usually needs no extra config — pio run --target compiledb is enough.
# system = "platformio"

[index]
compile_commands = "compile_commands.json"
# Generate it with:  fw-context index --build  (auto-detects build system)
# config_header = "config/my_build_config.h"   # path to config.h for custom build systems
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

# index_embeddings: generate vector embeddings for semantic search.
#   On by default. Set false to disable for faster indexing.
# index_embeddings = false

# LLM and local-path settings belong in local.toml (gitignored).
# See local.toml for ollama_url, model, db_dir, etc.
"""

_PROJECT_LOCAL_DEFAULTS_TEMPLATE = """\
# Local developer settings — NOT committed to git.
# Overrides values from config.toml where both define the same key.

[llm]
# enabled = false   # disable Ollama, return raw prompts for the agent to answer
# ollama_url = "http://localhost:11434"
# model = "qwen2.5-coder:14b"   # minimum 14B recommended — see global config for options
# num_ctx = 16384
# keep_alive = "10m"   # how long to keep model loaded in VRAM after each request
# timeout = 600.0   # HTTP timeout for Ollama requests in seconds (default 600)
# debug_log = "~/.fw-context/llm-debug.jsonl"   # write LLM prompts/responses to JSONL
# Embedding model for semantic_search.
# Pull with:  ollama pull <model>
# Uncomment exactly ONE of the sections below.
#
# ── Default: mxbai-embed-large (~670 MB VRAM, no GPU needed) ──
# embed_model = "mxbai-embed-large:latest"
# embed_query_prompt = "Represent this sentence for searching relevant passages: "
# embed_doc_prompt = ""
#
# ── Higher quality: qwen3-embedding:8b (~4.7 GB VRAM, GPU recommended) ──
# embed_model = "qwen3-embedding:8b"
# embed_query_prompt = "Retrieve C/C++ functions, types, symbols, and implementation code relevant to the query."
# embed_doc_prompt = ""
# analyze_symbols = true    # generate structured symbol descriptions (summary, inputs, outputs) — enabled by default
# analyze_vendor = false    # when true, also analyze vendor/SDK code (mbed-os, Zephyr, etc.) — can add hours

[index]
# db_dir = "~/.fw-context/index"   # where to store the SQLite index database
"""


@dataclass
class LLMConfig:
    """LLM (Ollama) configuration for symbol analysis, search query generation, and embeddings.

    Attributes:
        enabled: Set False to disable all Ollama calls — tools return raw prompts
            for the agent to answer instead.
        ollama_url: Ollama API endpoint (must be a loopback address unless you
            accept that source-code snippets will be sent externally).
        model: Chat model for analysis and search query generation.
            Minimum 14B parameters recommended for C++ embedded code.
        embed_model: Embedding model for semantic search.
            Default ``mxbai-embed-large:latest`` (~670 MB VRAM, no GPU needed).
            For better quality with a GPU use ``qwen3-embedding:8b`` (~4.7 GB).
        embed_query_prompt: Instruction prepended to query text before embedding.
            Auto-detected from model prefix when empty.  ``mxbai-*`` uses
            ``"Represent this sentence for searching relevant passages: "``,
            ``qwen3-embedding*`` uses a code-specific retrieval instruction.
            Set explicitly in TOML to override or disable (empty string).
        embed_doc_prompt: Instruction prepended to symbol descriptions during
            indexing.  Most models work best with an empty doc prompt — only
            set when the model's training expects a per-document instruction.
        num_ctx: Context window size in tokens.
        timeout: HTTP request timeout in seconds for Ollama API calls.
            Default 600 s (10 min) — analysis batches with large prompts and
            ``num_predict=3000`` can take several minutes, especially when
            the model runs on CPU. Embed requests use ``timeout * 2``.
        debug_log: Optional path to a JSONL file for debugging LLM prompts/responses.
        keep_alive: How long Ollama keeps the model loaded in VRAM after a request.
            ``"10m"`` (default) means the model stays loaded for 10 minutes of
            inactivity before unloading.  During indexing (~40 back-to-back
            requests), this prevents per-request model loading (~2-5 s each).
            Set ``"0"`` to unload immediately (saves VRAM, adds latency).
            Set ``"-1"`` to keep loaded indefinitely.
        analyze_symbols: Generate per-symbol summaries, inputs, and outputs during indexing.
        analyze_vendor: When False (default), skip LLM analysis for vendor/SDK code
            (mbed-os, Zephyr, PlatformIO, etc.) — only project code is analyzed.
            Set True to analyze every indexed symbol regardless of origin.
    """
    enabled: bool = True
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen2.5-coder:14b"
    embed_model: str = "mxbai-embed-large:latest"
    embed_query_prompt: str = ""
    embed_doc_prompt: str = ""
    num_ctx: int = 16384
    timeout: float = 600.0
    keep_alive: str = "10m"
    debug_log: Path | None = None
    analyze_symbols: bool = True
    analyze_vendor: bool = False


def _apply_embed_prompt_defaults(llm_cfg: LLMConfig) -> None:
    """Set embed prompt defaults based on model prefix when the field is empty.

    Called by ``_from_dict`` after loading TOML — only fills defaults when
    the user hasn't explicitly configured a prompt value.
    """
    model_lower = llm_cfg.embed_model.lower()
    if llm_cfg.embed_query_prompt == "":
        if model_lower.startswith("qwen3-embedding"):
            llm_cfg.embed_query_prompt = (
                "Retrieve C/C++ functions, types, symbols, and implementation "
                "code relevant to the query."
            )
        elif model_lower.startswith("mxbai"):
            llm_cfg.embed_query_prompt = (
                "Represent this sentence for searching relevant passages: "
            )
    if llm_cfg.embed_doc_prompt == "":
        if model_lower.startswith("qwen3-embedding"):
            llm_cfg.embed_doc_prompt = ""
        elif model_lower.startswith("mxbai"):
            llm_cfg.embed_doc_prompt = ""


@dataclass
class IndexConfig:
    """Index storage and scoping configuration.

    Attributes:
        db_dir: Directory where per-project SQLite databases are stored.
        compile_commands: Path to compile_commands.json (relative to project root).
        config_header: Path to a build-generated configuration header (e.g.
            ``config.h``) for projects whose build system does not emit
            ``-include`` flags in compile_commands.json.  When set, the file
            is force-included in every translation unit via ``-include``.
            Useful for custom build systems where auto-detection fails.
        source_roots: Directories to index symbols from. Empty = auto-detect.
        exclude_paths: Directories to exclude from indexing (LIKE patterns).
        index_refs: Build cross-reference index for call-graph tools
            (find_callers, find_call_path, etc.). Enabled by default.
        index_embeddings: Generate vector embeddings during indexing for
            semantic search. Enabled by default.
    """
    db_dir: Path = field(default_factory=lambda: Path.home() / ".fw-context" / "index")
    compile_commands: Path = field(default_factory=lambda: Path("compile_commands.json"))
    config_header: str = ""  # path to build-generated config.h for custom build systems
    source_roots: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=lambda: ["build", "BUILD"])
    index_refs: bool = True
    index_embeddings: bool = True


@dataclass
class ProjectMeta:
    """Optional project-level metadata.

    Attributes:
        name: Human-readable name for the project. Defaults to the directory
            name when not set.
    """
    name: str | None = None


@dataclass
class CacheServerConfig:
    """Optional centralized LLM analysis cache server.

    When ``url`` is set, the ``CacheClient`` is created and used during
    ``fw-context index --analyze`` to check remote cache before calling
    Ollama and to upload results after analysis.

    Attributes:
        url: Base URL of the cache server (e.g. ``https://fw-cache.example.com``).
        token: Bearer token for authentication (read+write or read-only).
        batch_size: Maximum hashes/entries per HTTP request (default 100).
        force: When ``True``, sends ``X-Cache-Overwrite`` header on write
            requests (requires ``can_overwrite`` on the server).
    """
    url: str = ""
    token: str = ""
    batch_size: int = 100
    force: bool = False


@dataclass
class Config:
    """Top-level configuration container aggregating all sub-configs.

    Loaded from TOML files with hierarchy: built-in defaults → global
    (``~/.fw-context/config.toml``) → shared project (``.fw-context/config.toml``)
    → local project (``.fw-context/local.toml``).
    Later levels override earlier ones via deep merge.

    Attributes:
        project: Optional project metadata (name override).
        build: Build system settings for compile_commands.json generation.
        index: Index storage, scoping, and feature flags.
        llm: Ollama configuration for analysis and search.
    """
    project: ProjectMeta = field(default_factory=ProjectMeta)
    build: BuildConfig = field(default_factory=BuildConfig)
    index: IndexConfig = field(default_factory=IndexConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    cache_server: CacheServerConfig | None = None

    def source_root_paths(self, project_root: Path) -> list[Path]:
        """Resolve and validate source root directories against *project_root*.

        Only directories that actually exist on disk are returned —
        configured paths that don't exist are logged as warnings and skipped.
        """
        import logging
        log = logging.getLogger(__name__)
        result: list[Path] = []
        for r in self.index.source_roots:
            p = (project_root / r).resolve()
            if p.exists():
                result.append(p)
            else:
                log.warning("source_root %r does not exist — skipping", r)
        return result

    def exclude_root_paths(self, project_root: Path) -> list[Path]:
        """Resolve exclude paths against *project_root*.

        Note: This does NOT resolve glob patterns or validate existence —
        the paths are used as SQL LIKE patterns downstream.
        """
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
        if defines := build.get("defines"):
            cfg.build.defines = list(defines)
        if board := build.get("board"):
            cfg.build.board = board

    if idx := data.get("index", {}):
        if db_dir := idx.get("db_dir"):
            cfg.index.db_dir = Path(db_dir).expanduser()
        if cc := idx.get("compile_commands"):
            cfg.index.compile_commands = Path(cc)
        if "source_roots" in idx:
            cfg.index.source_roots = idx["source_roots"]
        if "exclude_paths" in idx:
            cfg.index.exclude_paths = idx["exclude_paths"]
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
        if embed_qp := llm.get("embed_query_prompt"):
            cfg.llm.embed_query_prompt = embed_qp
        if embed_dp := llm.get("embed_doc_prompt"):
            cfg.llm.embed_doc_prompt = embed_dp
        _apply_embed_prompt_defaults(cfg.llm)
        if num_ctx := llm.get("num_ctx"):
            cfg.llm.num_ctx = int(num_ctx)
        if timeout := llm.get("timeout"):
            cfg.llm.timeout = float(timeout)
        if keep_alive := llm.get("keep_alive"):
            cfg.llm.keep_alive = keep_alive
        if debug_log := llm.get("debug_log"):
            cfg.llm.debug_log = Path(debug_log).expanduser()
        if "analyze_symbols" in llm:
            cfg.llm.analyze_symbols = bool(llm["analyze_symbols"])
        if "analyze_vendor" in llm:
            cfg.llm.analyze_vendor = bool(llm["analyze_vendor"])

    if cs := data.get("cache_server", {}):
        cfg.cache_server = CacheServerConfig(
            url=cs.get("url", ""),
            token=cs.get("token", ""),
            batch_size=cs.get("batch_size", 100),
            force=cs.get("force", False),
        )

    return cfg


def _ensure_global_config() -> Path:
    path = _GLOBAL_CONFIG_PATH
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_GLOBAL_DEFAULTS)
        except (OSError, PermissionError) as e:
            import logging
            logging.getLogger(__name__).warning("Could not create global config %s: %s", path, e)
    return path


def _ensure_project_config(project_root: Path) -> Path:
    config_dir = project_root / _PROJECT_CONFIG_DIR
    path = config_dir / _PROJECT_CONFIG_NAME
    if not path.exists():
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(_PROJECT_DEFAULTS_TEMPLATE)
        except (OSError, PermissionError) as e:
            import logging
            logging.getLogger(__name__).warning("Could not create project config %s: %s", path, e)
    return path


def _ensure_project_local_config(project_root: Path) -> Path:
    config_dir = project_root / _PROJECT_CONFIG_DIR
    path = config_dir / _PROJECT_LOCAL_CONFIG_NAME
    if not path.exists():
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(_PROJECT_LOCAL_DEFAULTS_TEMPLATE)
        except (OSError, PermissionError) as e:
            import logging
            logging.getLogger(__name__).warning("Could not create local config %s: %s", path, e)
    return path


def _is_loopback_url(url: str) -> bool:
    """True if *url*'s host is a loopback address.

    Uses ``ipaddress`` for robust IPv4/IPv6 detection, including shortened
    IPv6 formats (``0:0:0:0:0:0:0:1``, ``::1``) and IPv4-mapped IPv6.
    """
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        return False


# Module-level config cache with mtime-based invalidation.
# load_config() is called 20+ times per MCP session; caching avoids redundant
# TOML parsing when config files haven't changed.
_config_cache: dict[tuple, tuple[float, Config]] = {}


def load(project_root: Path | None = None) -> Config:
    """Load merged config for the given project root.

    Creates default config files on first use.  TOML parse errors in user
    configs are logged and the built-in defaults are used instead — a broken
    config file must not prevent the MCP tools from loading.

    Results are cached per (global_path, proj_path, local_path) tuple and
    invalidated when any source file's mtime changes.
    """
    import logging

    log = logging.getLogger(__name__)
    data: dict = {}

    global_path = _ensure_global_config()
    proj_path = _ensure_project_config(project_root) if project_root is not None else None
    local_path = _ensure_project_local_config(project_root) if project_root is not None else None

    # Check mtime-based cache
    cache_key = (global_path, proj_path, local_path)
    if cache_key in _config_cache:
        cached_ts, cached_cfg = _config_cache[cache_key]
        # Check if any source file changed
        try:
            newest = max(
                p.stat().st_mtime for p in cache_key if p is not None
            )
            if newest <= cached_ts:
                return cached_cfg
        except OSError:
            pass  # File missing → reload

    try:
        data = tomllib.loads(global_path.read_text())
    except Exception:
        log.exception("Failed to parse %s — using defaults", global_path)

    if project_root is not None:
        assert proj_path is not None
        try:
            proj_data = tomllib.loads(proj_path.read_text())
            data = _deep_merge(data, proj_data)
        except Exception:
            log.exception("Failed to parse %s — ignoring project config", proj_path)

        assert local_path is not None
        try:
            local_data = tomllib.loads(local_path.read_text())
            data = _deep_merge(data, local_data)
        except Exception:
            log.exception("Failed to parse %s — ignoring local config", local_path)

        # Security: a committed or local config can redirect LLM calls (which
        # include source-code snippets) to an arbitrary host. Warn loudly.
        final_url = (data.get("llm") or {}).get("ollama_url")
        if final_url and not _is_loopback_url(final_url):
            log.warning(
                "Config sets a non-local ollama_url (%s). "
                "Source-code snippets in LLM prompts will be sent to that host.",
                final_url,
            )
            print(
                f"⚠ WARNING: non-local ollama_url ({final_url}) — "
                "source code will be sent to an external host.",
                file=sys.stderr,
            )

    cfg = _from_dict(data)
    cfg.index.db_dir = cfg.index.db_dir.expanduser().resolve()

    # Cache with current mtime
    try:
        newest_mtime = max(
            p.stat().st_mtime for p in cache_key if p is not None
        )
        _config_cache[cache_key] = (newest_mtime, cfg)
    except OSError:
        pass  # Can't cache if we can't stat files

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

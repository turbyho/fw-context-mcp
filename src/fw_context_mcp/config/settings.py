"""fw-context configuration loader.

Hierarchy (later overrides earlier):
  1. built-in defaults
  2. ~/.fw-context/config.toml           (global, per-user)
  3. <project>/.fw-context/config.toml   (project, shared — commit to git)
  4. <project>/.fw-context/local.toml    (project, local — gitignore)

**Why four levels?**  Configuration has three owners:
- Tool authors define safe defaults (built-in).
- Each developer sets their own machine preferences (global — Ollama URL,
  model choice, VRAM limits).  This file must NEVER be committed.
- The team agrees on project-level settings (build system, index paths,
  vendor directories) and shares them via git.  Third-party contributors
  get a working config out of the box.
- Machine-specific overrides (local Python path, virtualenv activation,
  API keys) are stored in ``local.toml`` (gitignored).  A broken
  ``local.toml`` must not block other developers.

**Config class design — dataclasses vs dicts:**  Dataclasses provide
type-safe field access, IDE autocompletion, and default values directly
in the attribute definitions.  The TOML-to-dataclass bridge uses
field-mapping tables (_PROJECT_FIELDS, _BUILD_FIELDS, etc.) instead of
reflection — this avoids fragile naming conventions, gives explicit
control over type coercion per field, and makes the TOML schema
self-documenting through the mapping tables themselves.

**Safety — committed config validation:**  ``config.toml`` is shared
via git, so anyone with commit access could set ``pre_build`` or
``command`` to arbitrary shell commands.  The config loader warns
loudly when dangerous settings appear in committed config.  These
settings must live in ``local.toml`` (gitignored, per-developer).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import sys
import tomllib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..indexer.build import BuildConfig, BuildImage, BuildVariant

log = logging.getLogger(__name__)

__all__ = [
    "CacheServerConfig",
    "Config",
    "IndexConfig",
    "LLMConfig",
    "ProjectMeta",
    "ProjectNotInitializedError",
    "derive_project_id",
    "generate_project_id",
    "load",
    "update_global_config",
]

# ── Embedding description format version — bump when the text changes ──
# Each version change invalidates all cached embeddings because the vector
# for "the same symbol" differs if the description text format changed.
# The version tag is embedded in the embed_key() cache key so stale vectors
# from a previous index are never reused — semantic_search sees only vectors
# produced by the current description format.
#
# desc-v1: original — "kind name : in class : in path : sig : doc : llm"
# desc-v2: body text appended for function/method/constructor/destructor
# desc-v3: file name prefix added
# desc-v4: contextual sentence format (flowing text vs token-boundary lists)
# desc-v5: multi-chunk embeddings with semantic body splitting (chunk_index,
#   Body [part N/M] markers), composite PK (symbol_id, chunk_index)
DESCRIPTION_VERSION = "desc-v5"


class ProjectNotInitializedError(RuntimeError):
    """Raised when a project has not been initialized with ``fw-context init``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        super().__init__(
            f"Project at {root} is not initialized.\n"
            "Run 'fw-context init' in the project root to generate a project ID."
        )


_GLOBAL_CONFIG_PATH = Path.home() / ".fw-context" / "config.toml"
_PROJECT_CONFIG_DIR = ".fw-context"
_PROJECT_CONFIG_NAME = "config.toml"
_PROJECT_LOCAL_CONFIG_NAME = "local.toml"

# Why separate global vs project vs local?
# - Global: per-machine, per-developer — never committed.  Holds Ollama
#   URL, model preference, VRAM budgets.
# - Project (config.toml): team-shared via git — build system, index
#   paths, vendor directories.  Third-party contributors get a working
#   config immediately.
# - Local (local.toml): machine-specific overrides — Python path,
#   virtualenv activation scripts, API keys.  Gitignored.  Must not
#   break other developers if missing or broken.

_GLOBAL_DEFAULTS = """\
[index]
db_dir = "~/.fw-context/index"

[llm]
# enabled = false   # uncomment to disable Ollama — LLM-calling tools will return raw prompts
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

# Embedding model for semantic_search (auto-detected when not set).
# Auto-detect: GPU available → qwen3-embedding:8b, CPU-only → qwen3-embedding:0.6b.
# Set explicitly below to override.
# Pull with:  ollama pull <model>
#
# ── CPU-only, 32K context (~400 MB), 4096-d vectors ──
# embed_model = "qwen3-embedding:0.6b"
#
# ── CPU/GPU, 32K context (~2.4 GB), 5120-d vectors ──
# embed_model = "qwen3-embedding:4b"
#
# ── GPU, 8K context (~4.7 GB), 4096-d vectors ──
# embed_model = "qwen3-embedding:8b"
#
# ── Custom query/doc prompts (auto-detected per model, override if needed) ──
# embed_query_prompt = "Retrieve C/C++ functions, types, symbols, and implementation code relevant to the query."
# embed_doc_prompt = ""

analyze_symbols = true
"""

_PROJECT_DEFAULTS_TEMPLATE = """\
[project]
# name = "my-project"   # defaults to directory name
# id = ""               # auto-generated by 'fw-context init' — do not edit

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
# target = "BOARD_V2_BOARD"
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

# ── Multi-project / multi-device ([[build.variants]]) ─────────────────────
# One workspace may build several boards/images (Zephyr sysbuild, multiple
# mbed targets, PlatformIO environments, …).  Declare each build as a variant;
# each (variant, image) pair becomes one indexed build with its own config_hash.
#
# [[build.variants]]
# name   = "nrf52840-dev"                     # unique key, referenced by tools
# board  = "nrf52840dk/nrf52840"              # build-system-specific label
# build_dir = "build/nrf52840_sysbuild"       # per-variant output (default build/<name>)
# env    = { BOARD_ENV = "DEV" }               # build env vars (folded into config_hash)
# images = [                                  # sysbuild images (Zephyr only)
#   { name = "app",      dir = "proj/app",      type = "project" },
#   { name = "mcuboot",  dir = "${NCS}/bootloader/mcuboot", type = "sdk" },
# ]
#
# Optional shared defaults:
# source_dir = "proj/app"        # sysbuild input app (Zephyr)
# sysbuild   = true              # use `west build --sysbuild`
# default_variant = "nrf52840-dev"  # default when a query omits variant
# default_image   = "app"           # default image within default_variant

# ── PlatformIO ───────────────────────────────────────────────────────────
# Usually needs no extra config — pio run --target compiledb is enough.
# system = "platformio"

[index]
compile_commands = ".fw-context/build/compile_commands.json"
# Generate it with:  fw-context index --build  (auto-detects build system)
# config_header = "config/my_build_config.h"   # path to config.h for custom build systems
#vendor_paths = ["third_party", "vendor_libs"]
#project_paths = ["src/old_hal", "lib/muj_modul"]
#  Pro cesty MIMO project_root používejte ABSOLUTNÍ cesty:
#  project_paths = ["/home/user/esp/components/muj_fork"]

# index_refs: build a cross-reference / call graph.
#   On by default — enables find_callers, find_call_path, find_dead_code, etc.
#   Set false to disable for faster indexing on very large projects.
# index_refs = false

# index_embeddings: generate vector embeddings for semantic search.
#   On by default. Set false to disable for faster indexing.
# index_embeddings = false

[call_graph]
# dispatch_bridges: map dispatch-registration APIs to their event-loop
#   entry points for call-path tracing through async dispatch
#   (EventQueue, work queues, thread starts, timers).
#   Built-in defaults cover mbed-os, Zephyr, FreeRTOS.
#   Add custom bridges for other RTOS/dispatch frameworks here.
#
# [call_graph.dispatch_bridges]
# "my_rtos::Task::start" = "my_rtos::Task::_thread_entry"
# "my_rtos::Timer::attach" = "my_rtos::Timer::_on_timeout"

# LLM and local-path settings belong in local.toml (gitignored).
# See local.toml for ollama_url, model, db_dir, etc.
"""

_PROJECT_LOCAL_DEFAULTS_TEMPLATE = """\
# Local developer settings — NOT committed to git.
# Overrides values from config.toml where both define the same key.

# ── Build environment (machine-specific paths to build tools) ──
# Detected automatically by 'fw-context init'.
# Set manually only if auto-detection fails or you need a different toolchain.
#
# python = "/path/to/python"        # Python interpreter for pip-based CLI tools
#                                    #   (mbed-cli, platformio, keil2clangd, compiledb)
# activate = "/path/to/setup.sh"    # shell script sourced before build
#                                    #   (auto-detected for Zephyr/NCS/ESP-IDF; override for custom)

[build]
[llm]
# enabled = false   # disable Ollama, return raw prompts for the agent to answer
# ollama_url = "http://localhost:11434"
# model = "qwen2.5-coder:14b"   # minimum 14B recommended — see global config for options
# num_ctx = 16384
# keep_alive = "10m"   # how long to keep model loaded in VRAM after each request
# timeout = 600.0   # HTTP timeout for Ollama requests in seconds (default 600)
# debug_log = "~/.fw-context/llm-debug.jsonl"   # write LLM prompts/responses to JSONL
# Embedding model for semantic_search (auto-detected when not set).
# Auto-detect: GPU available → qwen3-embedding:8b, CPU-only → qwen3-embedding:0.6b.
# Set explicitly below to override.
# Pull with:  ollama pull <model>
#
# ── CPU-only, 32K context (~400 MB), 4096-d vectors ──
# embed_model = "qwen3-embedding:0.6b"
#
# ── CPU/GPU, 32K context (~2.4 GB), 5120-d vectors ──
# embed_model = "qwen3-embedding:4b"
#
# ── GPU, 8K context (~4.7 GB), 4096-d vectors ──
# embed_model = "qwen3-embedding:8b"
#
# ── Custom query/doc prompts (auto-detected per model, override if needed) ──
# embed_query_prompt = "Retrieve C/C++ functions, types, symbols, and implementation code relevant to the query."
# embed_doc_prompt = ""
# analyze_symbols = true    # generate structured symbol descriptions (summary, inputs, outputs) — enabled by default
# analyze_vendor = false    # when true, also analyze vendor/SDK code (mbed-os, Zephyr, etc.) — can add hours

# ── Chat API — use OpenAI-compatible API for chat instead of local Ollama ──
# When set, chat calls are routed to this endpoint. Embedding still uses
# local Ollama (embed_model above). Format is auto-detected from the URL:
#   :11434 or /api/generate → Ollama native (keeps keep_alive, num_ctx, auto_pull)
#   /v1 or bare host       → OpenAI-compatible (DeepSeek, OpenAI, LiteLLM, vLLM, etc.)
# Examples:
#   chat_api_base = "https://api.deepseek.com/v1"   # DeepSeek
#   chat_api_base = "https://api.openai.com/v1"     # OpenAI
#   chat_api_base = "http://localhost:4000"          # LiteLLM proxy
#   chat_api_base = "http://localhost:8080/v1"       # llama.cpp
# chat_api_base = ""
# chat_api_key = ""   # Bearer token — required by most cloud APIs
# chat_api_format = "auto"   # "auto" | "ollama" | "openai" (override detection)

# Auto-pull models on 404 (Ollama only). Set true for auto-recovery in
# online environments. Set false (default) for intranet/offline — model
# pulls must be done explicitly via `ollama pull` or the agent flow.
# auto_pull = false

# Stream chat responses (both OpenAI-compatible and Ollama-native).
# When true, sends stream:true and consumes SSE chunks — keeps the HTTP
# connection alive with continuous data flow, avoiding reverse-proxy idle
# timeouts (nginx 60s, Cloudflare 100s). Default false — non-streaming is
# sufficient for local Ollama. When false, SSE responses are still
# auto-detected and parsed as a fallback.
# stream = false

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
            Empty (default, ``""``) auto-detects: GPU available →
            ``qwen3-embedding:8b``, CPU-only → ``qwen3-embedding:0.6b``.
            Set explicitly to override (see config template for options).
        embed_query_prompt: Instruction prepended to query text before embedding.
            Auto-detected from model prefix when empty.  ``qwen3-embedding*`` uses
            a code-specific retrieval instruction, ``mxbai-*`` and ``ibm-granite/*``
            use ``"Represent this sentence for searching relevant passages: "``.
            Set explicitly in TOML to override or disable (empty string).
        embed_doc_prompt: Instruction prepended to symbol descriptions during
            indexing.  Most models work best with an empty doc prompt — only
            set when the model's training expects a per-document instruction.
        embed_dim: Override output embedding dimension for Matryoshka models.
            When set and smaller than the model's native dimension, vectors
            are truncated to this size and re-normalized.  E.g. set to 128
            for granite-embedding-r2 to get 6× smaller storage with only -1.6
            MTEB Code points loss.
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
        reranker_model: Optional cross-encoder model name for result reranking.
            When set (e.g. ``"cross-encoder/ms-marco-MiniLM-L6-v2"``), search
            results are rescored by the cross-encoder for higher precision.
            Default ``None`` — no reranking.
        ollama_max_concurrent: Maximum concurrent in-flight Ollama HTTP calls per
            process.  Default 1 (serial, identical to a Lock).  Increase to 2–4
            for multi-client MCP transports (SSE/streamable) where embedding
            requests (small model, ~100 ms) can overlap without saturating the
            GPU.  Chat requests are GPU-heavy — keep this low.
        chat_api_base: Chat API URL for OpenAI-compatible cloud/proxy endpoints
            (DeepSeek, LiteLLM, vLLM, llama.cpp).  ``None`` (default) uses local
            Ollama for chat.  External URLs send source code to that host.
        chat_api_key: Bearer token for cloud/proxy APIs.  ``None`` for local/no-auth.
        chat_api_format: Format override: ``"auto"`` (default), ``"ollama"``, or
            ``"openai"``.  ``"auto"`` detects format from the URL.
        auto_pull: Auto-pull models on 404 (Ollama only).  Default ``False`` —
            all model pulls must be explicit (safer for intranet/offline envs).
        stream: When True, send ``stream: true`` and consume SSE chunks for chat
            requests (both OpenAI-compatible and Ollama-native paths).  Keeps the
            HTTP connection alive with continuous data flow, avoiding reverse-proxy
            idle timeouts (nginx default 60s, Cloudflare 100s).  Default False —
            non-streaming is simpler and sufficient for local Ollama.  When False,
            SSE responses are still auto-detected and parsed (Plan A fallback).
    """

    enabled: bool = True
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen2.5-coder:14b"
    embed_model: str = ""  # empty = auto-detect (GPU → qwen3:8b, CPU → qwen3:0.6b)
    embed_query_prompt: str = ""
    embed_doc_prompt: str = ""
    embed_dim: int | None = None
    num_ctx: int = 16384
    timeout: float = 600.0
    keep_alive: str = "10m"
    debug_log: Path | None = None
    analyze_symbols: bool = True
    analyze_vendor: bool = False
    reranker_model: str | None = None
    """SentenceTransformers cross-encoder model for re-ranking search results.
    Set to e.g. ``"cross-encoder/ms-marco-MiniLM-L6-v2"`` to enable re-ranking.
    ``None`` (default) skips re-ranking — results are returned in FTS5 order."""
    ollama_max_concurrent: int = 1  # max concurrent Ollama HTTP calls (1=serial, 2–4 for SSE transport)
    chat_api_base: str | None = None
    chat_api_key: str | None = None
    chat_api_format: str = "auto"
    auto_pull: bool = False
    stream: bool = False

    def embed_key(self) -> str:
        """Return ``embed_model`` with description version for cache disambiguation.

        Returns ``"auto:desc-v5"`` when *embed_model* is empty (auto-detect not
        yet resolved — ``resolve_embed_model`` fills the field at config load time).
        """
        model = self.embed_model or "auto"
        return f"{model}:{DESCRIPTION_VERSION}"


def _apply_embed_prompt_defaults(llm_cfg: LLMConfig) -> None:
    """Set embed prompt defaults based on model prefix when the field is empty.

    Called by ``_from_dict`` after loading TOML — only fills defaults when
    the user hasn't explicitly configured a prompt value.

    **Why per-model prompts?**  Embedding models are trained with specific
    instruction formats.  ``qwen3-embedding`` models expect a task
    description prepended to the query (e.g. "Retrieve C/C++ functions…").
    ``mxbai`` and ``ibm-granite`` models use the standard "Represent this
    sentence…" format.  Using the wrong format reduces retrieval quality
    by 10-15% MTEB score — auto-detection prevents silent degradation.
    """
    model_lower = llm_cfg.embed_model.lower()
    if llm_cfg.embed_query_prompt == "":
        if model_lower.startswith("qwen3-embedding"):
            llm_cfg.embed_query_prompt = (
                "Retrieve C/C++ functions, types, symbols, and implementation code relevant to the query."
            )
        elif model_lower.startswith("mxbai"):
            llm_cfg.embed_query_prompt = "Represent this sentence for searching relevant passages: "
        elif model_lower.startswith("ibm-granite"):
            llm_cfg.embed_query_prompt = "Represent this sentence for searching relevant passages: "
    if llm_cfg.embed_doc_prompt == "":
        if model_lower.startswith("qwen3-embedding"):
            llm_cfg.embed_doc_prompt = ""
        elif model_lower.startswith("mxbai"):
            llm_cfg.embed_doc_prompt = ""
        elif model_lower.startswith("ibm-granite"):
            llm_cfg.embed_doc_prompt = ""


@dataclass
class IndexConfig:
    """Index storage and scoping configuration.

    Attributes:
        db_dir: Directory where per-project SQLite databases are stored.
        compile_commands: Path to compile_commands.json (relative to project root).
            Defaults to ``.fw-context/build/compile_commands.json`` — a gitignored
            subdirectory of the project config dir.
        config_header: Path to a build-generated configuration header (e.g.
            ``config.h``) for projects whose build system does not emit
            ``-include`` flags in compile_commands.json.  When set, the file
            is force-included in every translation unit via ``-include``.
            Useful for custom build systems where auto-detection fails.
        vendor_paths: Additional vendor/SDK directories (additive to auto-detection).
            Paths matching these patterns get ``is_project=0``.
        project_paths: Manual project directories that override auto-detection.
            Paths matching these patterns get ``is_project=1``, even if they
            would otherwise be detected as vendor/SDK.  Use absolute paths for
            directories outside the project root.
        index_refs: Build cross-reference index for call-graph tools
            (find_callers, find_call_path, etc.). Enabled by default.
        index_embeddings: Generate vector embeddings during indexing for
            semantic search. Enabled by default.
        rerank_top_k: Number of candidates to feed to the cross-encoder
            reranker (default 50).
        min_dense_count: Minimum number of embedding results required to
            trust dense-only routing in ``AdaptiveFusionPhase``.  Below this
            threshold FTS5 is used as fallback.  Set higher for projects
            with immature embedding quality (default 3, tuned on the Mbed project).
        max_symbol_body_lines: Maximum number of source lines stored per
            function/method body in the index.  Bodies longer than this are
            truncated.  Default 1000.
    """

    db_dir: Path = field(default_factory=lambda: Path.home() / ".fw-context" / "index")
    compile_commands: Path = field(
        default_factory=lambda: Path(".fw-context") / "build" / "compile_commands.json"
    )
    config_header: str = ""  # path to build-generated config.h for custom build systems
    vendor_paths: list[str] = field(default_factory=list)
    project_paths: list[str] = field(default_factory=list)
    index_refs: bool = True
    index_embeddings: bool = True
    rerank_top_k: int = 50
    min_dense_count: int = 3
    max_symbol_body_lines: int = 1000
    purge_max_missing_percent: int = 20
    """Abort the automatic ghost-file purge when more than this percent of
    indexed files are missing from disk — guards against an offline network
    mount or a misconfigured project root deleting the whole vendor index."""


@dataclass
class ProjectMeta:
    """Optional project-level metadata.

    Attributes:
        name: Human-readable name for the project. Defaults to the directory
            name when not set.
        id: Stable unique project identifier (UUID4 hex, 32 chars).
            Generated by ``fw-context init``, stored in
            ``.fw-context/config.toml``.  Shared across the team via git.
    """

    name: str | None = None
    id: str | None = None


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


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* — nested dicts are merged,
    not replaced.

    **Why recursive?**  TOML sections are nested dicts.  A shallow merge
    (``{**base, **override}``) would replace the entire ``[llm]`` dict
    when ``local.toml`` sets ``ollama_url`` — erasing all other LLM keys
    set by ``config.toml``.  Deep merge walks into sub-dicts, so each
    key is overridden individually.
    """
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _safe_int(val, default: int = 0) -> int:
    """Coerce *val* to int, returning *default* on failure.

    **Why safe?**  TOML values come from user-edited files — typos like
    ``num_ctx = "auto"`` or ``timeout = "inf"`` crash the server if not
    caught.  ``_safe_int`` logs a warning and returns the default so the
    server starts with a working (but potentially wrong) value.
    """
    try:
        return int(val)
    except (ValueError, TypeError):
        log.warning("Invalid int value %r — using default %d", val, default)
        return default


def _safe_float(val, default: float = 0.0) -> float:
    """Coerce *val* to float, returning *default* on failure."""
    try:
        return float(val)
    except (ValueError, TypeError):
        log.warning("Invalid float value %r — using default %.3f", val, default)
        return default


def _apply_section(
    data: dict,
    section_name: str,
    fields: list[tuple[str, str, str]],
    target: object,
) -> None:
    """Apply a TOML section's key-value pairs to a config dataclass.

    Iterates over *fields* — each is a ``(toml_key, attr_name, converter)``
    tuple — and calls ``_convert`` + ``setattr`` for each key present in
    the TOML section.

    **Why separate ``_convert`` dispatch?**  TOML types are limited (str,
    int, float, bool, array, table).  Our config types are richer (Path,
    list[str], Optional[int] with sentinel values).  The converter strings
    provide a compact, self-documenting mapping from TOML types to Python
    dataclass attribute types — no per-field handler functions needed.
    """
    if section := data.get(section_name, {}):
        for key, attr, conv in fields:
            if key in section:
                setattr(target, attr, _convert(section[key], conv))


def _build_cache_server_config(data: dict) -> CacheServerConfig | None:
    """Build a ``CacheServerConfig`` from the ``[cache_server]`` TOML section.

    Reads the bearer token from the config file, falling back to
    ``~/.fw-context/.cache_token`` when not set.

    **Why token file fallback?**  API tokens must never appear in
    committed config files.  The ``.cache_token`` file lives outside any
    git repository and follows the same pattern as ``.netrc`` — machine
    credentials in a protected dotfile.  Multiple projects on the same
    machine share one cache server, so a single token file suffices.

    Args:
        data: Merged TOML dictionary.

    Returns:
        ``CacheServerConfig`` when ``[cache_server]`` section is present and
        has a non-empty URL, ``None`` otherwise.
    """
    cs = data.get("cache_server", {})
    if not cs:
        return None

    token = cs.get("token", "")
    if not token:
        token_file = Path.home() / ".fw-context" / ".cache_token"
        if token_file.exists():
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass

    return CacheServerConfig(
        url=cs.get("url", ""),
        token=token,
        batch_size=cs.get("batch_size", 100),
        force=cs.get("force", False),
    )

def _from_dict(data: dict) -> Config:
    """Convert merged TOML dict to Config with field mapping tables.

    Each mapping entry: (toml_key, attr_name, converter).
    Converters: "str" | "bool" | "list" | "dict" | "path" | "int(N)" | "float(N)"

    Why table-driven instead of reflection-based?
    - Reflection (``setattr(cfg.__dict__[k], ...)``) couples TOML key names
      to Python attribute names, forcing identical naming.  Tables let them
      diverge — e.g. TOML ``compile_commands`` → Python ``compile_commands``,
      but could map TOML ``db-path`` → Python ``db_dir`` if desired.
    - Tables self-document the full TOML schema in one place.  A new developer
      can see every supported key without reading dataclass field definitions.
    - Type coercion is explicit per field — no guessing whether a TOML string
      "true" should be bool or str.
    """
    cfg = Config()

    # ── Core sections ──
    _apply_section(data, "project", _PROJECT_FIELDS, cfg.project)
    _apply_section(data, "build", _BUILD_FIELDS, cfg.build)
    _apply_section(data, "index", _INDEX_FIELDS, cfg.index)
    _apply_section(data, "llm", _LLM_FIELDS, cfg.llm)

    # [[build.variants]] is an array-of-tables, not a scalar — parsed
    # separately from the flat _BUILD_FIELDS mapping.
    cfg.build.variants = _build_variants(data)
    _validate_variant_names(cfg.build)

    # Normalize sentinel values: -1 means "not set" for embed_dim.
    # The TOML file cannot express None, so we use -1 as a sentinel that
    # resolve_embed_model later replaces with the auto-detected dimension.
    if cfg.llm.embed_dim is not None and cfg.llm.embed_dim < 0:
        cfg.llm.embed_dim = None
    _apply_embed_prompt_defaults(cfg.llm)


    cfg.cache_server = _build_cache_server_config(data)

    # Warn about unknown top-level keys — a typo like [indexx] goes unnoticed
    # without this check.  The user would otherwise get silent defaults.
    for key in data:
        if key not in _KNOWN_SECTIONS:
            log.warning(
                "Unknown config section [%s] — check for typos (valid: %s)",
                key, ", ".join(sorted(_KNOWN_SECTIONS)),
            )

    return cfg


def _convert(value, conv: str):
    """Convert a TOML value using the converter specification.

    Converter specs:
        ``"str"`` — return as-is
        ``"bool"`` — coerce to bool (handles strings "true"/"false"/"yes"/"no"/"1"/"0")
        ``"list"`` — coerce to list (wraps scalar in list)
        ``"dict"`` — coerce to dict
        ``"path"`` — wrap in ``Path`` and expand ``~``
        ``"int(N)"`` — coerce to int, default N on failure
        ``"float(N)"`` — coerce to float, default N on failure
    """
    # Exact-match converters (fast path) — dict lookup instead of if/elif chain.
    # Each converter is a lambda or builtin, stored once at module load.
    if conv in _CONVERTERS:
        return _CONVERTERS[conv](value)

    # Prefix-match converters: "int(N)" / "float(N)".
    # Format: "int(16384)" → int conversion, default 16384 on failure.
    if conv.startswith("int("):
        return _safe_int(value, int(conv[4:-1]))
    if conv.startswith("float("):
        return _safe_float(value, float(conv[6:-1]))

    return value  # fallback — unknown converter, pass through


def _convert_bool(value) -> bool:
    """Coerce *value* to bool, handling string representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1", "on"):
            return True
        if v in ("false", "no", "0", "off", ""):
            return False
    return bool(value)


def _convert_list(value) -> list:
    """Coerce *value* to list — wraps scalar values."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return list(value)


# Dispatch table for exact-match converters — dict lookup is O(1) and
# extensible (add a new converter string + lambda without touching _convert logic).
_CONVERTERS: dict[str, Callable[..., Any]] = {
    "str": lambda v: v,
    "bool": _convert_bool,
    "list": _convert_list,
    "dict": dict,
    "path": lambda v: Path(v).expanduser(),
}


# ── Field mapping tables ──

_PROJECT_FIELDS: list[tuple[str, str, str]] = [
    ("name", "name", "str"),
    ("id", "id", "str"),
]

_BUILD_FIELDS: list[tuple[str, str, str]] = [
    ("system", "system", "str"),
    ("clean", "clean", "bool"),
    ("command", "command", "str"),
    ("target", "target", "str"),
    ("toolchain", "toolchain", "str"),
    ("profile", "profile", "str"),
    ("app_config", "app_config", "str"),
    ("extra_profiles", "extra_profiles", "list"),
    ("defines", "defines", "list"),
    ("board", "board", "str"),
    ("idf_path", "idf_path", "str"),
    ("fqbn", "fqbn", "str"),
    ("cmake_generator", "cmake_generator", "str"),
    ("keil_project", "keil_project", "str"),
    ("keil_target", "keil_target", "str"),
    ("keil_cmsis_path", "keil_cmsis_path", "str"),
    ("iar_project", "iar_project", "str"),
    ("iar_target", "iar_target", "str"),
    ("makefile", "makefile", "str"),
    ("make_target", "make_target", "str"),
    ("make_vars", "make_vars", "dict"),
    ("make_dry_run", "make_dry_run", "bool"),
    ("toolchain_path", "toolchain_path", "str"),
    ("toolchain_prefix", "toolchain_prefix", "str"),
    ("include_dirs", "include_dirs", "list"),
    ("system_include_dirs", "system_include_dirs", "list"),
    ("extra_flags", "extra_flags", "list"),
    ("source_dirs", "source_dirs", "list"),
    ("compiler", "compiler", "str"),
    ("pre_build", "pre_build", "str"),
    ("activate", "activate", "str"),
    ("python", "python", "str"),
    ("timeout", "timeout", "float(7200)"),
    ("source_dir", "source_dir", "str"),
    ("sysbuild", "sysbuild", "bool"),
    ("build_dir", "build_dir", "str"),
    ("default_variant", "default_variant", "str"),
    ("default_image", "default_image", "str"),
    ("env", "env", "dict"),
]

_INDEX_FIELDS: list[tuple[str, str, str]] = [
    ("db_dir", "db_dir", "path"),
    ("compile_commands", "compile_commands", "path"),
    ("config_header", "config_header", "str"),
    ("vendor_paths", "vendor_paths", "list"),
    ("project_paths", "project_paths", "list"),
    ("index_refs", "index_refs", "bool"),
    ("index_embeddings", "index_embeddings", "bool"),
    ("rerank_top_k", "rerank_top_k", "int(50)"),
    ("min_dense_count", "min_dense_count", "int(3)"),
    ("max_symbol_body_lines", "max_symbol_body_lines", "int(1000)"),
    ("purge_max_missing_percent", "purge_max_missing_percent", "int(20)"),
]

_LLM_FIELDS: list[tuple[str, str, str]] = [
    ("enabled", "enabled", "bool"),
    ("ollama_url", "ollama_url", "str"),
    ("model", "model", "str"),
    ("embed_model", "embed_model", "str"),
    ("embed_query_prompt", "embed_query_prompt", "str"),
    ("embed_doc_prompt", "embed_doc_prompt", "str"),
    ("embed_dim", "embed_dim", "int(-1)"),  # -1 = auto-detect (maps to None)
    ("num_ctx", "num_ctx", "int(16384)"),
    ("timeout", "timeout", "float(600.0)"),
    ("keep_alive", "keep_alive", "str"),
    ("debug_log", "debug_log", "path"),
    ("analyze_symbols", "analyze_symbols", "bool"),
    ("analyze_vendor", "analyze_vendor", "bool"),
    ("reranker_model", "reranker_model", "str"),
    ("ollama_max_concurrent", "ollama_max_concurrent", "int(1)"),
    ("chat_api_base", "chat_api_base", "str"),
    ("chat_api_key", "chat_api_key", "str"),
    ("chat_api_format", "chat_api_format", "str"),
    ("auto_pull", "auto_pull", "bool"),
    ("stream", "stream", "bool"),
]

_KNOWN_SECTIONS: set[str] = {"project", "build", "index", "llm", "cache_server", "call_graph"}

# BuildConfig attribute name → merge semantics classification lives in
# indexer/build.py; this module maps TOML keys → attribute names for the
# variant-override table.
_BUILD_KEY_TO_ATTR: dict[str, str] = {key: attr for key, attr, _ in _BUILD_FIELDS}
_VARIANT_SPECIFIC_KEYS: frozenset[str] = frozenset({
    "name", "description", "board", "build_dir", "env", "images",
})


def _build_variants(data: dict) -> list[BuildVariant]:
    """Parse ``[[build.variants]]`` from the merged TOML dict.

    Each variant is a partial ``[build]`` override keyed by ``name``.  Variant-
    specific keys (name/description/board/build_dir/env/images) map to
    BuildVariant fields; every other key is treated as a ``[build]`` override
    keyed by the BuildConfig attribute name.

    WHY attribute-keyed overrides: ``build_variant_config`` (build.py) applies
    the merge by field type (scalar/list/dict).  Keying overrides by attribute
    name keeps the TOML→attribute mapping in this module and the merge logic in
    build.py — no duplicated key tables.

    An ``[index]`` key goes to ``index_overrides`` instead.
    ``build_variant_config`` applies BuildConfig fields only, so such a key
    used to be dropped without a word — and since the unknown-key warning it
    would be reported as a typo.
    """
    raw = data.get("build", {}).get("variants")
    if not raw:
        return []

    variants: list[BuildVariant] = []
    for table in raw:
        if not isinstance(table, dict):
            log.warning("[[build.variants]] entry is not a table — skipping %r", table)
            continue
        name = table.get("name")
        if not name:
            log.warning("[[build.variants]] entry missing required 'name' — skipping")
            continue

        images: list[BuildImage] = []
        for img in table.get("images") or []:
            if not isinstance(img, dict):
                continue
            images.append(
                BuildImage(
                    name=img.get("name", ""),
                    description=img.get("description", ""),
                    dir=img.get("dir", ""),
                    type=img.get("type", "project"),
                    board=img.get("board"),
                )
            )

        # Split by section.  The three key sets are disjoint — verified:
        # _BUILD_FIELDS n _INDEX_FIELDS, _VARIANT_SPECIFIC_KEYS n
        # _INDEX_FIELDS and _INDEX_FIELDS n _LLM_FIELDS are all empty — so a
        # key cannot belong to two of them.
        index_keys = {toml_key for toml_key, _, _ in _INDEX_FIELDS}
        overrides: dict = {}
        index_overrides: dict = {}
        for key, value in table.items():
            if key in _VARIANT_SPECIFIC_KEYS:
                continue
            if key in index_keys:
                index_overrides[key] = value
                continue
            overrides[_BUILD_KEY_TO_ATTR.get(key, key)] = value

        env = table.get("env") or {}
        variants.append(
            BuildVariant(
                name=name,
                description=table.get("description", ""),
                board=table.get("board"),
                build_dir=table.get("build_dir"),
                env=dict(env) if isinstance(env, dict) else {},
                images=images,
                overrides=overrides,
                index_overrides=index_overrides,
            )
        )

    return variants


def _validate_variant_names(build: BuildConfig) -> None:
    """Warn when default_variant/default_image reference unknown names.

    WHY a warning, not an error: the config may declare a default_variant that
    has not been indexed yet — the choice is owned by the project author, so a
    typo must be surfaced but must not block startup.
    """
    if not build.variants:
        return
    names = {v.name for v in build.variants}
    if build.default_variant and build.default_variant not in names:
        log.warning(
            "[build] default_variant '%s' not found in [[build.variants]] "
            "(valid: %s)",
            build.default_variant, ", ".join(sorted(names)),
        )
    if build.default_image and build.default_variant in names:
        dv = next(v for v in build.variants if v.name == build.default_variant)
        img_names = {i.name for i in dv.images}
        if build.default_image not in img_names:
            log.warning(
                "[build] default_image '%s' not found in images of variant '%s'",
                build.default_image, build.default_variant,
            )


def _ensure_config_file(path: Path, template: str, label: str) -> Path:
    """Create *path* from *template* if it does not exist.

    Args:
        path: Absolute path to the config file.
        template: TOML template string to write when the file is missing.
        label: Human-readable label for log messages (e.g. ``"global config"``).

    Returns:
        *path* unchanged.
    """
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(template, encoding="utf-8")
        except (OSError, PermissionError) as e:
            log.warning("Could not create %s %s: %s", label, path, e)
    return path


def _ensure_global_config() -> Path:
    return _ensure_config_file(_GLOBAL_CONFIG_PATH, _GLOBAL_DEFAULTS, "global config")


def update_global_config(fix: bool = False) -> None:
    """Check ``~/.fw-context/config.toml`` for missing template keys.

    When *fix* is True, merge missing keys from the template into the file.
    Keys are placed in their correct TOML sections.  Does NOT modify
    existing keys or their values.

    Returns nothing; prints status to stdout.
    """
    path = _GLOBAL_CONFIG_PATH
    if not path.exists():
        if fix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_GLOBAL_DEFAULTS)
            print(f"  [fix] created {path}")
        else:
            print(f"  [info] {path} missing — run with --force to create")
        return

    from fw_context_mcp.config._toml_editor import merge_template

    added = merge_template(path, _GLOBAL_DEFAULTS)
    if added:
        if fix:
            print(f"  [fix] {path}: added {', '.join(added)}")
        else:
            print(f"  [info] {path}: missing options: {', '.join(added)}")
    else:
        print(f"  [ok] {path}")


def _ensure_project_config(project_root: Path) -> Path:
    return _ensure_config_file(
        project_root / _PROJECT_CONFIG_DIR / _PROJECT_CONFIG_NAME,
        _PROJECT_DEFAULTS_TEMPLATE,
        "project config",
    )


def _ensure_project_local_config(project_root: Path) -> Path:
    return _ensure_config_file(
        project_root / _PROJECT_CONFIG_DIR / _PROJECT_LOCAL_CONFIG_NAME,
        _PROJECT_LOCAL_DEFAULTS_TEMPLATE,
        "local config",
    )


def _validate_config_safety(proj_data: dict, proj_path: Path) -> None:
    """Warn if committed config contains security-sensitive settings.

    Committed config (.fw-context/config.toml) is shared via git.  Dangerous
    settings should only be in local.toml (gitignored).

    **Why separate validation?**  The deep merge in ``load()`` would silently
    execute a malicious ``pre_build`` from a committed config.  Separate
    validation (checking the committed layer BEFORE merge with local)
    catches this.  A local.toml override for ``pre_build`` is fine — it's
    per-developer and not shared.

    .. versionadded:: v0.18
    """
    build = proj_data.get("build") or {}
    for key in ("pre_build", "command"):
        if build.get(key):
            msg = (
                f"SECURITY: {key} is set in .fw-context/config.toml "
                f"(committed).  This allows anyone with commit access to run "
                f"arbitrary shell commands on other developers' machines.  "
                f"Move {key} to .fw-context/local.toml (gitignored) instead."
            )
            log.warning(msg)
            print(f"⚠ {msg}", file=sys.stderr)

    ollama_url = (proj_data.get("llm") or {}).get("ollama_url")
    if ollama_url and not _is_loopback_url(ollama_url):
        msg = (
            f"SECURITY: ollama_url ({ollama_url}) in .fw-context/config.toml "
            f"(committed) points to a non-local host.  Source code snippets in "
            f"LLM prompts would be sent to that host.  Use localhost or move "
            f"ollama_url to .fw-context/local.toml (gitignored) instead."
        )
        log.warning(msg)
        print(f"⚠ {msg}", file=sys.stderr)

def _is_loopback_url(url: str) -> bool:
    """True if *url*'s host is a loopback address.

    Uses ``ipaddress`` for robust IPv4/IPv6 detection, including shortened
    IPv6 formats (``0:0:0:0:0:0:0:1``, ``::1``) and IPv4-mapped IPv6.
    """

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
#
# Why mtime instead of inotify?  Simplicity — the MCP server is short-lived
# (one session), config files change rarely, and mtime comparison costs ~0.
# An inotify watcher would need threads and cleanup logic for no real benefit.
_config_cache: OrderedDict[tuple, tuple[float, Config]] = OrderedDict()
_CONFIG_CACHE_MAX = 50


def load(project_root: Path | None = None) -> Config:
    """Load merged config for the given project root.

    Creates default config files on first use.  TOML parse errors in user
    configs are logged and the built-in defaults are used instead — a broken
    config file must not prevent the MCP tools from loading.

    Results are cached per (global_path, proj_path, local_path) tuple and
    invalidated when any source file's mtime changes.

    **Why deep merge?**  Shallow merge loses nested keys —
    ``config.toml`` sets ``[llm] embed_model = "foo"``, ``local.toml`` sets
    ``[llm] num_ctx = 8192`` — shallow merge would overwrite the entire
    ``[llm]`` dict, discarding ``embed_model``.  Deep merge preserves both.

    **Why graceful degradation?**  The MCP server is a plumbing tool — if
    it fails to start because of a typo in ``local.toml``, the developer
    loses all fw-context tools in their AI assistant.  A more frustrating
    failure mode: the bug is in the tool they'd use to debug the config.
    Log loudly, continue with defaults.
    """
    data: dict = {}

    global_path = _ensure_global_config()
    proj_path = _ensure_project_config(project_root) if project_root is not None else None
    local_path = _ensure_project_local_config(project_root) if project_root is not None else None

    # Check mtime-based cache — if no config file has changed since last
    # load, return the cached Config object directly.  TOML parsing and
    # dataclass construction are cheap (sub-ms), but resolve_embed_model
    # runs subprocess checks (ollama list, nvidia-smi) that cost ~200 ms.
    cache_key = (global_path, proj_path, local_path)
    if cache_key in _config_cache:
        cached_ts, cached_cfg = _config_cache[cache_key]
        # Check if any source file changed
        try:
            newest = max(p.stat().st_mtime for p in cache_key if p is not None)
            if newest <= cached_ts:
                return cached_cfg
        except OSError:
            pass  # File missing → reload

    # Phase 1: load global config (always present — _ensure_global_config
    # creates it from template if missing).
    try:
        data = tomllib.loads(global_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("Failed to parse %s — using defaults", global_path)

    if project_root is not None:
        assert proj_path is not None

        # Phase 2: deep-merge project config (shared via git).
        # Uses _deep_merge so nested sections accumulate — [index] keys
        # from global and project config coexist.
        try:
            proj_data = tomllib.loads(proj_path.read_text(encoding="utf-8"))
            data = _deep_merge(data, proj_data)
            # Security: validate committed config doesn't contain dangerous settings.
            # pre_build, command, and non-loopback ollama_url in committed config.toml
            # are potential RCE / data exfiltration vectors.
            _validate_config_safety(proj_data, proj_path)
        except (OSError, ValueError):
            log.exception("Failed to parse %s — ignoring project config", proj_path)

        assert local_path is not None

        # Phase 3: deep-merge local config (gitignored, per-developer).
        # This is the last merge — local.toml values have highest precedence.
        try:
            local_data = tomllib.loads(local_path.read_text(encoding="utf-8"))
            data = _deep_merge(data, local_data)
        except (OSError, ValueError):
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
                f"⚠ WARNING: non-local ollama_url ({final_url}) — source code will be sent to an external host.",
                file=sys.stderr,
            )

        # Security: chat_api_base may point to an external cloud API.
        # Unlike ollama_url (which is typically local), chat_api_base is
        # explicitly chosen by the user for cloud access — so this is an
        # informational notice, not a loud warning.
        chat_base = (data.get("llm") or {}).get("chat_api_base")
        if chat_base and not _is_loopback_url(chat_base):
            print(
                f"ℹ Chat API set to external host ({chat_base}). "
                "Source code snippets in chat prompts will be sent to this endpoint.",
                file=sys.stderr,
            )

    cfg = _from_dict(data)
    from ..llm.auto_model import resolve_embed_model
    resolve_embed_model(cfg.llm)
    cfg.index.db_dir = cfg.index.db_dir.expanduser().resolve()

    # Test isolation: FW_CONTEXT_INDEX_DIR redirects the DEFAULT index DB
    # directory (~/.fw-context/index) for the whole process.  Tests set this
    # to a temp dir so no test writes to the user's real index.  Inherited by
    # subprocesses, so CLI invocations spawned from tests are isolated too.
    # An explicit db_dir in config.toml/local.toml still wins — this only
    # catches tests that forget to set db_dir and would fall back to the home.
    _default_db_dir = (Path.home() / ".fw-context" / "index").resolve()
    _env_index_dir = os.environ.get("FW_CONTEXT_INDEX_DIR")
    if _env_index_dir and cfg.index.db_dir == _default_db_dir:
        cfg.index.db_dir = Path(_env_index_dir).expanduser().resolve()

    # Cache with current mtime
    try:
        newest_mtime = max(
            p.stat().st_mtime for p in cache_key if p is not None
        )
        if len(_config_cache) >= _CONFIG_CACHE_MAX:
            _config_cache.popitem(last=False)
        _config_cache[cache_key] = (newest_mtime, cfg)
    except OSError:
        pass  # Can't cache if we can't stat files

    return cfg


def generate_project_id() -> str:
    """Generate a new project ID — UUID4 without hyphens (32 hex chars)."""
    import uuid

    return uuid.uuid4().hex


def derive_project_id(root: Path) -> str:
    """Return the project ID from ``.fw-context/config.toml``.

    The ID is generated by ``fw-context init`` and stored in the
    ``[project]`` section of the shared project config.  It is stable,
    team-shared via git, and independent of filesystem location.

    Raises:
        ProjectNotInitializedError: When the project has not been
            initialized — ``fw-context init`` has not been run.
    """
    cfg = load(project_root=root)
    if cfg.project.id:
        return cfg.project.id

    raise ProjectNotInitializedError(root)


def _write_project_id(project_root: Path, project_id: str) -> None:
    """Write ``id = "<project_id>"`` into ``[project]`` section of config.toml.

    Uses :func:`fw_context_mcp.config._toml_editor.set_key` for atomic,
    section-aware writes.  Creates the config file from the template if it
    doesn't exist yet.
    """
    config_path = project_root / _PROJECT_CONFIG_DIR / _PROJECT_CONFIG_NAME
    if not config_path.exists():
        _ensure_project_config(project_root)

    from fw_context_mcp.config._toml_editor import set_key

    set_key(config_path, "project", "id", project_id)


def _format_toml_value(key: str, value: object) -> str:
    """Format a key-value pair as a TOML line.

    Args:
        key: TOML key name.
        value: Value to format (str → quoted, bool → true/false,
            int/float → unquoted number).

    Returns:
        ``"key = value\\n"`` string suitable for insertion into a TOML file.
    """
    if isinstance(value, bool):
        val = "true" if value else "false"
    elif isinstance(value, int):
        val = str(value)
    elif isinstance(value, float):
        val = str(value)
    else:
        val = f'"{value}"'
    return f"{key} = {val}\n"


def _update_local_toml(
    project_root: Path,
    updates: dict[str, object],
    *,
    clear_keys: list[str] | None = None,
) -> None:
    """Update the ``[llm]`` section of ``<project>/.fw-context/local.toml``.

    Uses line-level editing to preserve comments and other sections.
    Creates the file from the template if it doesn't exist yet.

    Only writes to ``local.toml`` (gitignored, per-developer) — never
    touches the global config or the shared project ``config.toml``.

    **Why line-level editing instead of TOML round-trip?**  ``tomllib``
    can parse but Python has no standard TOML writer in the stdlib.
    Third-party writers (``toml``, ``tomli-w``, ``tomlkit``) lose comments
    and formatting.  Line-level editing with regex preserves everything
    — existing comments, blank lines, key ordering, trailing commas.

    Args:
        project_root: Project root directory.
        updates: Dict of ``{key: value}`` pairs to write.  ``None`` values
            are skipped (key is not written).  Supported value types:
            ``str``, ``bool``, ``int``, ``float``.
        clear_keys: Keys to REMOVE from the ``[llm]`` section (both active
            and commented-out forms).  Used when re-configuring away from a
            value that must not merely be overwritten — e.g. switching from
            a cloud API back to local Ollama must clear ``chat_api_base``/
            ``chat_api_key`` so chat stops routing externally.
    """
    local_path = project_root / _PROJECT_CONFIG_DIR / _PROJECT_LOCAL_CONFIG_NAME
    if not local_path.exists():
        _ensure_project_local_config(project_root)

    lines = local_path.read_text(encoding="utf-8").splitlines(keepends=True)

    to_write = {k: v for k, v in updates.items() if v is not None}
    to_clear = set(clear_keys or ())
    if not to_write and not to_clear:
        return

    # Remove requested keys from the [llm] section before writing, so a
    # cleared key cannot reappear from an earlier config.  Only lines inside
    # [llm] are considered — other sections are never touched.
    if to_clear:
        in_llm = False
        kept: list[str] = []
        for line in lines:
            s = line.strip()
            if s == "[llm]":
                in_llm = True
                kept.append(line)
                continue
            if in_llm and s.startswith("["):
                in_llm = False
                kept.append(line)
                continue
            if in_llm and any(re.match(rf"^#?\s*{re.escape(k)}\s*=", s) for k in to_clear):
                continue
            kept.append(line)
        lines = kept

    # Find [llm] section boundaries — we need to insert/replace keys
    # within this section while leaving other sections untouched.
    in_llm = False
    llm_start = -1
    llm_end = len(lines)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[llm]":
            in_llm = True
            llm_start = i + 1  # First line after [llm] header
            continue
        if in_llm and stripped.startswith("[") and stripped != "[llm]":
            llm_end = i  # Next section starts — stop here
            break

    if llm_start == -1 and to_write:
        # No [llm] section — append one at the end of the file
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("\n[llm]\n")
        llm_start = len(lines)
        llm_end = len(lines)

    # For each key: find and replace existing line, or insert before llm_end.
    # Regex matches both active and commented-out keys (``# key = value``)
    # so the user can uncomment a template default by running configure_llm.
    for key, value in to_write.items():
        found = False
        for i in range(llm_start, llm_end):
            stripped = lines[i].strip()
            if re.match(rf"^#?\s*{re.escape(key)}\s*=", stripped):
                lines[i] = _format_toml_value(key, value)
                found = True
                break
        if not found:
            lines.insert(llm_end, _format_toml_value(key, value))
            llm_end += 1

    local_path.write_text("".join(lines), encoding="utf-8")

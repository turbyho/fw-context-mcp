"""Interactive fallback prompts for ``fw-context init``.

Invoked only when auto-detection fails or a required parameter is missing
(e.g. Zephyr board, Arduino FQBN, LLM backend).  Every prompt has a
default (Enter = default).  ``non_interactive`` mode short-circuits all
prompts — no questions are asked in CI/pipe mode.

Idempotence: when a value is already configured, the flow shows the current
value with a "Change? [y/N]" prompt (default N = keep).  Re-running ``init``
and holding Enter therefore walks every section without modifying anything.
"""

from __future__ import annotations

from pathlib import Path

_BUILD_SYSTEM_CHOICES: list[tuple[str, str]] = [
    ("mbed-os", "Mbed OS 5/6"),
    ("zephyr", "Zephyr RTOS"),
    ("platformio", "PlatformIO"),
    ("cmake", "generic CMake"),
    ("esp-idf", "ESP-IDF"),
    ("arduino", "Arduino CLI"),
    ("keil-mdk", "Keil MDK"),
    ("iar-ewarm", "IAR EWARM"),
    ("makefile", "bare Makefile"),
    ("stm32cubeide", "STM32CubeIDE — detect-only"),
    ("ti-ccs", "TI Code Composer Studio — detect-only"),
]

# Chat model fixed default — the same default as ``LLMConfig.model``.
_DEFAULT_CHAT_MODEL = "qwen2.5-coder:14b"


def _input(prompt: str) -> str:
    """Read a trimmed line from stdin, swallowing EOF/Ctrl-C."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def prompt_build_system(non_interactive: bool, *, current: str | None = None) -> str | None:
    """Ask which build system to use (numbered list). Returns key or None.

    When *current* is given, it is marked in the menu and an empty answer
    keeps it — a re-run of ``init`` can walk the section with Enter only.
    """
    if non_interactive:
        return None
    print("\n  What build system does this project use?")
    for i, (key, label) in enumerate(_BUILD_SYSTEM_CHOICES, start=1):
        marker = "  (current)" if key == current else ""
        print(f"  {i:>2}. {key} ({label}){marker}")
    print("   0. none / manual")
    answer = _input("  > ")
    try:
        n = int(answer)
    except ValueError:
        return current
    if n == 0:
        return None
    if 1 <= n <= len(_BUILD_SYSTEM_CHOICES):
        return _BUILD_SYSTEM_CHOICES[n - 1][0]
    return current


# Sentinel for the picker's "I have my own script" entry.  A version string
# can never collide with it.
_OWN_SCRIPT = "\0own-script"


def _prompt_own_env_script(project_root: Path, *, dry_run: bool) -> str | None:
    """Ask for a hand-written environment script and use it as-is.

    The generated script is the default because it works for anyone with an
    SDK installed.  A project with its own arrangement — a shared team script
    that pins the version, sets extra variables, or activates a venv — says
    so here instead of having the generated one imposed on it.
    """
    val = prompt_text("Path to your SDK environment script", non_interactive=False)
    if not val:
        return None
    path = Path(val).expanduser()
    if not path.is_file():
        print(f"  [warn] {path} does not exist — writing it anyway, fix it later.")
    if dry_run:
        print(f"  [dry-run] Would use {path}")
        return None
    return str(path)


def _configure_ncs_version(
    project_root: Path, *, dry_run: bool, non_interactive: bool
) -> str | None:
    """Pick an NCS version and write the activation script for it.

    Returns the script path, or None when there is nothing to configure —
    no NCS installs, or no answer in a non-interactive run.  The caller then
    falls back to the generic detection and prompt.
    """
    from ..indexer.builders.zephyr import ZephyrBuildSystem

    # Upstream Zephyr sets its own paths — sourcing zephyr-env.sh or
    # activating the workspace exports ZEPHYR_BASE and
    # ZEPHYR_SDK_INSTALL_DIR.  When they are already there, use them; there
    # is nothing to choose between.  Only NCS needs asking, because its
    # environment does not exist until nrfutil is told which version to make.
    try:
        zephyr_sdk = ZephyrBuildSystem.zephyr_sdk_from_environment()
    except Exception:  # noqa: BLE001 — detection is best-effort at init
        zephyr_sdk = None
    if zephyr_sdk is not None and zephyr_sdk.usable and zephyr_sdk.env_script:
        print(f"  [ok] Zephyr SDK from the environment: {zephyr_sdk.path}")
        return str(zephyr_sdk.env_script)

    try:
        installs = ZephyrBuildSystem.list_installed_ncs()
    except Exception:  # noqa: BLE001 — detection is best-effort at init
        return None
    if not installs:
        return None

    preferred = ZephyrBuildSystem.preferred_ncs_version(project_root)
    version = prompt_ncs_version(
        installs, default=preferred, non_interactive=non_interactive
    )
    if version == _OWN_SCRIPT:
        return _prompt_own_env_script(project_root, dry_run=dry_run)
    if version is None:
        print(
            "  No SDK version chosen — set [build] activate yourself, or "
            "re-run init interactively."
        )
        return None

    chosen = next((i for i in installs if i.version == version), None)
    if chosen is None:
        return None

    nrfutil = ZephyrBuildSystem._find_nrfutil()
    if nrfutil is None:
        print("  nrfutil with 'sdk-manager' not found — cannot write the env script.")
        return None

    if dry_run:
        print(f"  [dry-run] Would use nRF Connect SDK {version} ({chosen.path})")
        return None

    script = ZephyrBuildSystem._write_ncs_env_script(
        project_root, nrfutil, version, str(chosen.path.parent)
    )
    print(f"  [ok] nRF Connect SDK: {version} ({chosen.path})")
    return script


def prompt_ncs_version(
    installs: list, *, default: str | None, non_interactive: bool
) -> str | None:
    """Ask which nRF Connect SDK version to build against.

    WHY this is asked rather than detected: the SDK version is an argument to
    ``nrfutil sdk-manager toolchain env``, not something discoverable from the
    project.  Before that environment is set up there is no ZEPHYR_BASE and
    no toolchain on PATH, so a machine with several SDKs installed offers no
    signal about which one this project wants.  Guessing "the newest" builds
    the index against different headers and macros than the developer
    compiles with.

    *default* is the version the project's own environment points at, when
    there is one; Enter keeps it.  Non-interactive returns the default, which
    may be None — the caller then declines to configure rather than pick.
    """
    usable = [i for i in installs if i.usable]
    if not usable:
        return None
    if len(usable) == 1:
        # Nothing to choose.
        return usable[0].version
    if non_interactive:
        return default

    print("\n  Which nRF Connect SDK should this project build with?")
    for index, install in enumerate(usable, start=1):
        marker = "  (matches this project's environment)" if install.version == default else ""
        print(f"  {index:>2}. {install.describe()}{marker}")
    own = len(usable) + 1
    print(f"  {own:>2}. I have my own environment script")
    suffix = f" [{default}]" if default else ""
    answer = _input(f"  >{suffix} ")
    if not answer:
        return default
    try:
        choice = int(answer)
    except ValueError:
        # Accept the version string too — "v3.2.3" is what the user sees.
        match = [i for i in usable if i.version == answer.strip()]
        return match[0].version if match else default
    if choice == own:
        return _OWN_SCRIPT
    if 1 <= choice <= len(usable):
        return usable[choice - 1].version
    return default


def prompt_text(
    label: str,
    *,
    default: str | None = None,
    non_interactive: bool = False,
) -> str | None:
    """Ask a free-text question. Returns the answer (or default on empty)."""
    if non_interactive:
        return default
    suffix = f" (default: {default})" if default else ""
    answer = _input(f"  {label}{suffix} > ")
    return answer or default


def confirm_change(label: str, current: str, *, non_interactive: bool = False) -> bool:
    """Ask whether to change an already-configured value. Default N (keep)."""
    print(f"  {label}: {current} (already configured)")
    if non_interactive:
        return False
    return _input("  Change? [y/N] > ").lower() in ("y", "yes")


def _confirm(question: str, *, default: bool = True) -> bool:
    """Yes/no prompt with a default.  Returns the boolean answer."""
    yn = "Y/n" if default else "y/N"
    answer = _input(f"  {question} [{yn}] > ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _llm_configured(project_root: Path) -> bool:
    """Return True when local.toml has an explicit (uncommented) LLM key."""
    import re

    local = project_root / ".fw-context" / "local.toml"
    if not local.exists():
        return False
    in_llm = False
    for line in local.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s == "[llm]":
            in_llm = True
            continue
        if in_llm and s.startswith("["):
            in_llm = False
            continue
        if in_llm and re.match(r"^(model|chat_api_base|embed_model|enabled)\s*=", s):
            return True
    return False


def _llm_summary(cfg) -> str:
    """Summarize the LLM config as a one-line display string.

    Used in the "already configured" line so the user can recognize the
    current backend at a glance before deciding whether to change it.
    """
    llm = cfg.llm
    if not llm.enabled:
        return "disabled"
    if llm.chat_api_base:
        return f"cloud API {llm.model} @ {llm.chat_api_base}"
    return f"ollama + {llm.model} + {llm.embed_model or 'auto'}"


def prompt_llm_config(project_root: Path, cfg, *, non_interactive: bool) -> None:
    """Configure the LLM backend interactively.  Idempotent.

    Non-interactive mode prints manual-edit instructions (when unconfigured)
    and returns.  Interactive mode shows the current config (when present)
    with a "Change? [y/N]" prompt, otherwise runs the Local Ollama / Cloud
    API / Disable menu.
    """
    configured = _llm_configured(project_root)

    if non_interactive:
        if not configured:
            print("  LLM: not configured")
            print("    Configure it by running `fw-context init` interactively,")
            print("    or edit .fw-context/local.toml manually (see template).")
        return

    if configured and not confirm_change("LLM", _llm_summary(cfg)):
        return

    print(f"  LLM: {'re-configure' if configured else 'not configured'}")
    print("  fw-context can use an LLM for symbol analysis and smart search.")
    print("  How would you like to configure it?\n")
    print("    1. Local Ollama (recommended for offline use)")
    print("    2. Cloud API (DeepSeek, OpenAI, LiteLLM, ...)")
    print("    3. Disable LLM analysis (embedding still works)")
    choice = _input("  > ")

    if choice == "1":
        _prompt_ollama_setup(project_root, cfg)
    elif choice == "2":
        _prompt_cloud_setup(project_root)
    elif choice == "3":
        from ..llm.configure import configure_llm_core

        configure_llm_core(project_root, {"enabled": False})
        print("  [ok] LLM disabled — embedding still works")
    else:
        print("  [skip] LLM configuration skipped")


def _prompt_ollama_setup(project_root: Path, cfg) -> None:
    """Interactive local-Ollama setup: recommend models, pull with consent."""
    from ..llm.auto_model import _gpu_available
    from ..llm.configure import configure_llm_core
    from ..llm.ollama import check_setup

    gpu = _gpu_available()
    chat_default = _DEFAULT_CHAT_MODEL
    embed_default = "qwen3-embedding:8b" if gpu else "qwen3-embedding:0.6b"

    result = check_setup(cfg.llm)
    status = result.get("status")
    # not_configured / embedding_unavailable also mean Ollama is down — only
    # "ok" and "model_missing" imply a reachable server.
    if status in ("error", "not_configured", "embedding_unavailable"):
        print(f"  [warn] Ollama not reachable: {result.get('message', '')}")
    else:
        print(f"  [ok] Ollama running at {cfg.llm.ollama_url}")
        installed = result.get("installed_models")
        if installed:
            print(f"  Installed models: {', '.join(installed)}")
        if status == "model_missing":
            print(f"  [info] {result.get('message', '')}")

    print(f"  Hardware: {'NVIDIA GPU detected' if gpu else 'CPU only (no GPU detected)'}")
    print("  Recommended:")
    print(f"    chat:      {chat_default} (fixed default)")
    print(f"    embedding: {embed_default}")

    chat = prompt_text("Chat model", default=chat_default) or chat_default
    embed = prompt_text("Embedding model", default=embed_default) or embed_default

    from ..deps._fixes import _ollama_pull

    if _confirm(f"Pull {chat} now? (multi-GB)"):
        ok, msg = _ollama_pull(chat)
        print(f"  [{'ok' if ok else 'warn'}] {msg}")
    if _confirm(f"Pull {embed} now? (multi-GB)"):
        ok, msg = _ollama_pull(embed)
        print(f"  [{'ok' if ok else 'warn'}] {msg}")

    res = configure_llm_core(
        project_root,
        {"enabled": True, "model": chat, "embed_model": embed},
        # Switching from cloud → local Ollama must clear any previously
        # configured cloud endpoint, otherwise chat keeps routing externally.
        clear_keys=["chat_api_base", "chat_api_key", "chat_api_format"],
    )
    if res.get("status") != "ok":
        print(f"  [warn] {res.get('message', 'LLM test call failed')}")
    print(f"  [ok] LLM configured: ollama + {chat} + {embed}")
    print("  [ok] Saved to .fw-context/local.toml")


def _prompt_cloud_setup(project_root: Path) -> None:
    """Interactive cloud-API setup: URL, key, model."""
    from ..config.settings import _is_loopback_url
    from ..llm.configure import configure_llm_core

    url = prompt_text("API URL") or ""
    if not url:
        print("  [skip] Cloud API setup skipped")
        return
    key = prompt_text("API key") or ""
    model = prompt_text("Model", default="deepseek-chat") or "deepseek-chat"

    if not _is_loopback_url(url):
        print(f"  ⚠ Source code will be sent to {url}.")
        print("    Ensure this complies with your data security policy.")

    updates: dict[str, object] = {"enabled": True, "chat_api_base": url, "model": model}
    if key:
        updates["chat_api_key"] = key
    configure_llm_core(project_root, updates, chat_api_base=url)
    print(f"  [ok] LLM configured: cloud API {model} @ {url}")
    print("  [ok] Saved to .fw-context/local.toml")


# ── Config-first build resolution ───────────────────────────────────────


def resolve_build_system(
    project_root: Path,
    cfg,
    *,
    non_interactive: bool,
    dry_run: bool,
) -> str | None:
    """Determine the build system, config-first.  Returns the system key or None.

    Reads ``cfg.build.system`` first (already configured → shows
    "already configured" + ``Change? [y/N]``).  Falls back to marker-based
    detection; when that also fails, prompts interactively and writes the
    answer to ``config.toml``.  In non-interactive mode, a failed detection
    returns ``None`` (falls to the checklist).
    """
    from ..indexer.build import detect_build_system
    from ._init_build import _write_build_config_key

    system = cfg.build.system
    if system:
        if dry_run:
            print(f"  Build system: {system} (already configured)")
        elif confirm_change("Build system", system, non_interactive=non_interactive):
            system = prompt_build_system(non_interactive, current=system) or system
            if system and system != cfg.build.system:
                _write_build_config_key(project_root, "system", system)
        return system

    detected = detect_build_system(project_root)
    if detected:
        print(f"  Build system: {detected} (auto-detected)")
        if not dry_run:
            _write_build_config_key(project_root, "system", detected)
        return detected

    print("  Build system: not detected")
    print("  No standard project markers found (.mbed, platformio.ini, CMakeLists.txt, ...)")
    system = prompt_build_system(non_interactive)
    if system and not dry_run:
        _write_build_config_key(project_root, "system", system)
        print(f"  [ok] Build system: {system}")
        print("  [ok] Saved to .fw-context/config.toml")
    return system


def resolve_build_params(
    project_root: Path,
    cfg,
    system: str | None,
    *,
    non_interactive: bool,
    dry_run: bool,
) -> None:
    """Prompt for system-specific required parameters (config-first).

    Only runs for the detected/configured *system*.  Missing required
    values are prompted (and written to ``config.toml``); already-set
    values show "already configured" with a ``Change? [y/N]`` prompt.
    """
    if not system:
        return
    from ._init_build import _write_build_config_key

    b = cfg.build
    if b.variants:
        # Multi-variant: board/target/fqbn/etc. live per-variant in
        # [[build.variants]] — the top-level singleton is not prompted.
        print("  Build variants configured — per-variant board/target lives in [[build.variants]]")
        return

    def _ensure(key: str, label: str, current: str | None) -> None:
        if current:
            if dry_run:
                print(f"  {label}: {current} (already configured)")
            elif confirm_change(label, current, non_interactive=non_interactive):
                val = prompt_text(label, default=current, non_interactive=non_interactive)
                if val and val != current:
                    _write_build_config_key(project_root, key, val)
            return
        val = prompt_text(label, non_interactive=non_interactive)
        if val:
            if not dry_run:
                _write_build_config_key(project_root, key, val)
            print(f"  [ok] {label}: {val}")

    if system == "zephyr":
        _ensure("board", "Board", b.board)
    elif system == "arduino":
        _ensure("fqbn", "Board FQBN", b.fqbn)
    elif system == "keil-mdk":
        _ensure("keil_project", "Path to .uvprojx", b.keil_project)
    elif system == "iar-ewarm":
        _ensure("iar_project", "Path to .ewp", b.iar_project)
    elif system == "bare":
        cur = ",".join(b.source_dirs) if b.source_dirs else None
        if cur:
            if dry_run:
                print(f"  Source dirs: {cur} (already configured)")
            elif confirm_change("Source dirs", cur, non_interactive=non_interactive):
                val = prompt_text("Source dirs (comma-separated)", non_interactive=non_interactive)
                if val:
                    _write_build_config_key(project_root, "source_dirs", [s.strip() for s in val.split(",") if s.strip()])
            return
        val = prompt_text("Source dirs (comma-separated)", non_interactive=non_interactive)
        if val:
            if not dry_run:
                _write_build_config_key(project_root, "source_dirs", [s.strip() for s in val.split(",") if s.strip()])
            print(f"  [ok] Source dirs: {val}")
    elif system == "mbed-os":
        if b.target:
            if dry_run:
                print(f"  Target: {b.target} (already configured)")
            elif confirm_change("Target", b.target, non_interactive=non_interactive):
                val = prompt_text("Target", default=b.target, non_interactive=non_interactive)
                if val and val != b.target:
                    _write_build_config_key(project_root, "target", val)
        else:
            try:
                from ..indexer.build import _parse_mbed_dotfile
                dotfile = _parse_mbed_dotfile(project_root)
            except Exception:
                dotfile = {}
            if dotfile.get("TARGET"):
                print(f"  Target: {dotfile['TARGET']} (from .mbed)")
            else:
                val = prompt_text("Target", non_interactive=non_interactive)
                if val:
                    if not dry_run:
                        _write_build_config_key(project_root, "target", val)
                    print(f"  [ok] Target: {val}")


def resolve_build_env(
    project_root: Path,
    cfg,
    system: str | None,
    *,
    non_interactive: bool,
    dry_run: bool,
) -> None:
    """Auto-detect or prompt for the build environment (config-first).

    Reads ``local.toml [build] python``/``activate``/``idf_path`` first —
    already-configured values are shown and left untouched (idempotent).
    Only when the key is missing is ``detect_environment`` run (or the user
    prompted).  Machine-specific → written to ``local.toml``.
    """
    b = cfg.build
    if b.python or b.activate or b.idf_path:
        if b.python:
            print(f"  Build python: {b.python} (already configured)")
        if b.activate:
            print(f"  Build activate: {b.activate} (already configured)")
        if b.idf_path:
            print(f"  Build idf_path: {b.idf_path} (already configured)")
        return

    if not system:
        return

    from ..indexer.builders import registry as _bregistry
    from ._init import _write_build_key

    builder_cls = _bregistry.get(system)
    detected: dict[str, str | None] = {"python": None, "activate": None}

    # Zephyr/NCS: the SDK version is a choice, not a detectable fact — see
    # prompt_ncs_version.  Ask before falling back to detect_environment,
    # which can only guess when several SDKs are installed.
    if system == "zephyr":
        chosen = _configure_ncs_version(
            project_root, dry_run=dry_run, non_interactive=non_interactive
        )
        if chosen is not None:
            detected["activate"] = chosen

    if detected["activate"] is None and builder_cls and hasattr(builder_cls, "detect_environment"):
        try:
            detected = builder_cls.detect_environment(project_root)
        except Exception:
            detected = {"python": None, "activate": None}

    detected_python = detected.get("python")
    if detected_python:
        if not dry_run:
            _write_build_key(project_root, "python", detected_python)
        print(f"  [ok] Build python: {detected_python} (auto-detected)")
    detected_activate = detected.get("activate")
    if detected_activate:
        if not dry_run:
            _write_build_key(project_root, "activate", detected_activate)
        print(f"  [ok] Build activate: {detected_activate} (auto-detected)")

    if system == "esp-idf" and not detected.get("activate"):
        val = prompt_text(
            "Path to ESP-IDF (directory containing export.sh) (or Enter to skip)",
            non_interactive=non_interactive,
        )
        if val:
            idf_dir = Path(val).expanduser()
            export_sh = idf_dir / "export.sh"
            if not dry_run:
                # idf_path sets IDF_PATH for the build; activate sources the
                # export.sh so idf.py/ninja/toolchain are on PATH.  Both are
                # machine-specific and belong in local.toml.
                _write_build_key(project_root, "idf_path", str(idf_dir))
                if export_sh.exists():
                    _write_build_key(project_root, "activate", str(export_sh))
                    detected["activate"] = str(export_sh)
                else:
                    detected["activate"] = val
            print(f"  [ok] Build idf_path: {idf_dir}")
            if export_sh.exists():
                print(f"  [ok] Build activate: {export_sh}")

    if not detected.get("python") and not detected.get("activate") and system in ("zephyr", "esp-idf"):
        print("  SDK environment script not found.")
        val = prompt_text(
            "Path to your shell init script for the SDK environment (or Enter to skip)",
            non_interactive=non_interactive,
        )
        if val:
            if not dry_run:
                _write_build_key(project_root, "activate", val)
            print(f"  [ok] Build activate: {val}")

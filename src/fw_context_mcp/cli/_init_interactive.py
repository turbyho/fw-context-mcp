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


def _input(prompt: str) -> str:
    """Read a trimmed line from stdin; EOF is an empty answer, Ctrl-C stops the command.

    WHY Ctrl-C stops: before, it was an empty answer, thus the default of
    the question ran — "Keep the profile?" removed it, and the profile of
    the project removed the keys of ``local.toml``.  The CLI turns the
    interrupt into exit code 130 (``cli.main``).
    """
    try:
        return input(prompt).strip()
    except EOFError:
        return ""
    except KeyboardInterrupt:
        print()
        raise


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


def _configure_ncs_version(
    project_root: Path, *, dry_run: bool, non_interactive: bool,
    current: str | None = None,
) -> str | None:
    """Pick an NCS version and write the activation script for it.

    *current* is the ``[build] activate`` already in the config, when there is
    one.  It is offered first and kept by default, so a re-run of init shows
    what the project uses — a hand-written script included — and only moves
    off it when asked.

    Returns the script path, or None when there is nothing to configure —
    no NCS installs, or no answer in a non-interactive run.  The caller then
    falls back to the generic detection and prompt.
    """
    from ..indexer.builders.zephyr import ZephyrBuildSystem

    if current:
        # A configured environment is the answer until someone changes it;
        # do not let the environment scan below silently override it.
        installs = ZephyrBuildSystem.list_installed_ncs()
        version = prompt_ncs_version(
            installs, default=None, non_interactive=non_interactive, current=current
        )
        if version is None or version == current:
            print(f"  Build activate: {current} (already configured)")
            return current
        return _write_ncs_script(project_root, installs, version, dry_run=dry_run)

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
    if version is None:
        print(
            "  No SDK version chosen — set [build] activate yourself, or "
            "re-run init interactively."
        )
        return None

    return _write_ncs_script(project_root, installs, version, dry_run=dry_run)


def _write_ncs_script(
    project_root: Path, installs: list, version: str, *, dry_run: bool
) -> str | None:
    """Generate the activation script for *version* and return its path."""
    from ..indexer.builders.zephyr import ZephyrBuildSystem

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
    installs: list, *, default: str | None, non_interactive: bool,
    current: str | None = None,
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
    if current is None:
        if not usable:
            return None
        if len(usable) == 1:
            # Nothing to choose.
            return usable[0].version
    if non_interactive:
        # Never switch an unattended run off what is configured.
        return current or default

    print("\n  Which nRF Connect SDK should this project build with?")
    entries: list[tuple[str, str]] = []
    if current is not None:
        entries.append((current, f"{current}  (current)"))
    for install in usable:
        marker = "  (matches this project's environment)" if install.version == default else ""
        entries.append((install.version, f"{install.describe()}{marker}"))
    for index, (_value, line) in enumerate(entries, start=1):
        print(f"  {index:>2}. {line}")

    keep = current or default
    suffix = f" [{keep}]" if keep else ""
    answer = _input(f"  >{suffix} ")
    if not answer:
        return keep
    try:
        choice = int(answer)
    except ValueError:
        # Accept the version string too — "v3.2.3" is what the user sees.
        match = [v for v, _ in entries if v == answer.strip()]
        return match[0] if match else keep
    if 1 <= choice <= len(entries):
        return entries[choice - 1][0]
    return keep


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


# The name of each config layer, as the LLM step of init gives it.
_LAYER_FILES = {
    "global": "the global config",
    "project": ".fw-context/config.toml",
    "local": ".fw-context/local.toml",
}


def _llm_layers(project_root: Path) -> list[str]:
    """Return the config layers that select the LLM backend, in merge order.

    A layer selects the backend when it sets a key of ``LLM_BACKEND_KEYS``.
    Before, init read only ``local.toml``, thus a project that used a
    complete ``[llm]`` table of the global config got "LLM: not
    configured", and the menu gave no way to keep the global settings.
    """
    from ..config.settings import llm_key_sources

    found = set(llm_key_sources(project_root).values())
    return [layer for layer in _LAYER_FILES if layer in found]


def _llm_summary(cfg) -> str:
    """Summarize the LLM config as a one-line display string.

    Used in the "already configured" line so the user can recognize the
    current backend at a glance before deciding whether to change it.
    """
    llm = cfg.llm
    if not llm.enabled:
        return "disabled"
    server = llm.chat_api_base or llm.ollama_url
    return (f"profile {llm.profile or '(default)'}: {llm.provider} {llm.model} @ {server}, "
            f"embedding {llm.embed_model or 'auto'}")


def prompt_llm_config(project_root: Path, cfg, *, non_interactive: bool) -> None:
    """Configure the LLM backend interactively.  Idempotent.

    The step tells where the backend comes from: the project
    (``config.toml`` or ``local.toml``), the global config, or nowhere.

    * The project selects the backend: a "Change? [y/N]" prompt, with the
      file named.  On "y", the menu.
    * Only the global config selects it: the menu, with option 0 "Use the
      global settings" as the default (Enter), which writes nothing.
    * Nothing selects it: the menu, and Enter skips the step.

    In a project that selects the backend in ``local.toml`` and not in
    ``config.toml``, option 0 removes the keys of ``LLM_BACKEND_KEYS``
    from ``local.toml``, and the global settings then apply.  Enter keeps
    the settings there: a removal must be a choice.  Option 0 is not given
    when ``config.toml`` selects the backend: init writes only
    ``local.toml``, and a committed setting applies to each user.

    Non-interactive mode writes nothing.  It prints where the settings come
    from, or the manual-edit instructions when nothing selects the backend.
    """
    layers = _llm_layers(project_root)
    project_layers = [layer for layer in layers if layer != "global"]
    from_global = "global" in layers
    use_global_offered = from_global and "project" not in project_layers

    if non_interactive:
        if not project_layers and from_global:
            print(f"  LLM: {_llm_summary(cfg)} (from the global config)")
        elif not project_layers:
            print("  LLM: not configured")
            print("    Configure it by running `fw-context init` interactively,")
            print("    or edit .fw-context/local.toml manually (see template).")
        return

    if project_layers:
        where = " and ".join(_LAYER_FILES[layer] for layer in project_layers)
        if not confirm_change("LLM", f"{_llm_summary(cfg)} (set in {where})"):
            return
        print("  LLM: re-configure")
    elif from_global:
        print(f"  LLM: {_llm_summary(cfg)} (from the global config)")
    else:
        print("  LLM: not configured")
    print("  fw-context can use an LLM for symbol analysis and smart search.")
    print("  How would you like to configure it?\n")
    if use_global_offered and "local" in project_layers:
        print("    0. Use the global settings (removes the [llm] backend keys of .fw-context/local.toml)")
    elif use_global_offered:
        print("    0. Use the global settings — Enter")
    print("    1. Local Ollama (recommended for offline use)")
    print("    2. LLM services: ChatGPT plan, OpenRouter, DeepSeek, Ollama Cloud, Ollama on another computer,")
    print("       another OpenAI-compatible API (more profiles; the source code goes to their host)")
    print("    3. Disable the LLM analysis (the embeddings stay)")
    choice = _input("  > ")

    if use_global_offered and (choice == "0" or (choice == "" and not project_layers)):
        _use_global_llm(
            project_root, remove_local="local" in project_layers, old_embed_model=cfg.llm.embed_model,
        )
    elif choice == "1":
        _prompt_ollama_setup(project_root, cfg)
    elif choice == "2":
        from ._llm_wizard import run_wizard

        run_wizard(project_root, cfg)
    elif choice == "3":
        _disable_llm_analysis(project_root, cfg)
    else:
        print("  [skip] LLM configuration skipped")


def _disable_llm_analysis(project_root: Path, cfg) -> None:
    """Turn off the LLM analysis of the project, and keep the embeddings (option 3 of the menu).

    Writes ``[llm] analyze_symbols = false`` to ``local.toml``.  WHY not
    ``enabled = false``, as before: that also turns off the embeddings and
    the chat of ``smart_search``, but the menu said "embedding still
    works".  WHY no test call (``configure_llm_core``): the option needs
    no chat model, and a test of the chat model failed on a machine
    without one, and put the old settings back.
    """
    from ..config.settings import ConfigError, _update_local_toml

    try:
        _update_local_toml(project_root, {"analyze_symbols": False})
    except (ConfigError, OSError) as exc:
        print(f"  [warn] .fw-context/local.toml cannot be changed ({exc}). Nothing was changed.")
        return
    print("  [ok] LLM analysis off ([llm] analyze_symbols = false) — the embeddings stay")
    if not cfg.llm.enabled:
        print("  [note] The LLM is off ([llm] enabled = false): the embeddings need enabled = true")


def _use_global_llm(project_root: Path, *, remove_local: bool, old_embed_model: str) -> None:
    """Let the global LLM settings apply to the project (option 0 of the menu).

    *remove_local*: remove the keys of ``LLM_BACKEND_KEYS`` from
    ``local.toml``.  Else nothing is written: no project file selects the
    backend.  No test call: the global settings are the ones that the
    other projects use, and ``fw-context doctor`` does the check.
    *old_embed_model* is the model that applied before: a note tells the
    user when the removal changes it.
    """
    from ..config.settings import LLM_BACKEND_KEYS, ConfigError, _update_local_toml, llm_key_sources

    if not remove_local:
        print("  [ok] LLM: the global settings apply")
        return
    from ..config import load

    local = project_root / ".fw-context" / "local.toml"
    try:
        previous = local.read_bytes()
        _update_local_toml(project_root, {}, clear_keys=list(LLM_BACKEND_KEYS))
        # The editor finds only the line "[llm]".  For another form of the
        # table (`[llm]  # note`, an inline table) it removed nothing, and
        # the step said "[ok]".  Thus read the file again.
        left = sorted(key for key, layer in llm_key_sources(project_root).items() if layer == "local")
    except (ConfigError, OSError) as exc:
        print(f"  [warn] .fw-context/local.toml cannot be changed ({exc}). Nothing was changed.")
        return
    if left:
        try:
            # Only when the editor removed a part: an unchanged file is
            # not written again.  Atomic, as the edit itself.
            if local.read_bytes() != previous:
                from ..config._toml_editor import _atomic_write

                _atomic_write(local.resolve(), previous.decode("utf-8"))
        except OSError as exc:
            print(
                f"  [warn] init cannot remove {', '.join(left)} from .fw-context/local.toml, and cannot "
                f"write its old text back ({exc}). Read the file before the next command."
            )
            return
        print(
            f"  [warn] init cannot remove {', '.join(left)} from .fw-context/local.toml: its [llm] table "
            "has a form that init does not edit. Nothing was changed. Remove the keys yourself."
        )
        return
    print("  [ok] LLM: removed the [llm] backend keys of .fw-context/local.toml — the global settings apply")
    # A different model gives vectors of a different meaning, and often of
    # a different dimension: the next index run embeds each definition
    # again.  The user must know that before the run, not after it.
    try:
        new_embed_model = load(project_root).llm.embed_model
    except ConfigError:
        return
    if old_embed_model and new_embed_model and new_embed_model != old_embed_model:
        print(
            f"  [note] The embedding model changes from {old_embed_model} to {new_embed_model}: "
            "the next index run embeds each definition again"
        )


def _save_profile(project_root: Path, name: str, values: dict[str, object], updates: dict[str, object], *,
                  clear_keys: list[str] | None = None, global_embed: str | None = None) -> bool:
    """Write the profile *name*, select it for the project with *updates* and *clear_keys*, and do a test call.

    *global_embed* is an embedding model for the global config: it is
    written with the profile, and the restore after a failed test call
    removes it too.

    Returns True when the two files changed and the test call passed.
    When the test call fails, the global config gets its earlier text
    back.  WHY: ``configure_llm_core`` puts back only ``local.toml``.
    Before, a profile that failed its test stayed in the global config,
    and a profile of the same name that worked before was replaced by it.
    """
    from ..config import settings
    from ..config._profiles import set_profile
    from ..llm.configure import configure_llm_core

    try:
        global_path = settings._ensure_global_config()
        previous = global_path.read_text(encoding="utf-8")
    except (ValueError, OSError, settings.ConfigError) as e:
        print(f"  [warn] The profile {name!r} is not written: {e}")
        return False
    try:
        set_profile(name, values)
        if global_embed is not None:
            settings.update_global_llm_key("embed_model", global_embed)
    except (ValueError, OSError, settings.ConfigError) as e:
        print(f"  [warn] The global config is not changed: {e}")
        _put_back(global_path, previous, name)
        return False
    res = configure_llm_core(project_root, updates, clear_keys=clear_keys)
    # An error means that no new setting is saved: the core puts the old
    # settings of local.toml back, or the write itself failed.  When the
    # core cannot put the old settings back, its message says so.
    if res.get("status") == "ok":
        return True
    print(f"  [warn] {res.get('message', 'LLM test call failed')}")
    _put_back(global_path, previous, name)
    return False


def _put_back(global_path: Path, previous: str, name: str) -> None:
    """Write the earlier text of the global config back; a failure names the profile that stays."""
    from ..config._toml_editor import _atomic_write

    try:
        _atomic_write(global_path.resolve(), previous)
    except OSError as e:
        print(
            f"  [warn] init cannot write the earlier text of {global_path} back ({e}): "
            f"the profile {name!r} stays in it. Read the file before the next command."
        )


def _prompt_ollama_setup(project_root: Path, cfg) -> None:
    """Option 1 of the menu: a local Ollama profile, selected for the project with its embedding model.

    The values come from ``_local_ollama_profile``; ``_save_profile`` writes
    the profile and tests it.  ``local.toml`` selects the profile only
    when it is not the global default.  The embedding model is a global
    default (decision of the user, 2026-10-21): the global config gets it,
    and ``local.toml`` only when the user wants it for this project only.
    """
    from ..config import settings

    found = _local_ollama_profile(project_root, cfg)
    if found is None:
        return
    name, values, embed = found
    try:
        global_llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm") or {}
    except settings.ConfigError as e:
        print(f"  [warn] {e}")
        return
    # A key that local.toml does not hold comes from the file below it:
    # config.toml of the project, else the global config.  WHY: before,
    # the key went when the global value was the same, and a config.toml
    # that selects an external profile then sent the code out.
    try:
        committed = settings.read_config_file(project_root / ".fw-context" / "config.toml").get("llm") or {}
    except settings.ConfigError as e:
        print(f"  [warn] {e}")
        return
    updates: dict[str, object] = {"enabled": True}
    clear = []
    if (committed.get("profile") or global_llm.get("profile")) == name:
        clear.append("profile")
    else:
        updates["profile"] = name
    global_embed: str | None = None
    committed_embed = committed.get("embed_model")
    if embed == (committed_embed or global_llm.get("embed_model")):
        clear.append("embed_model")
    elif committed_embed:
        # config.toml (in git) sets another model: only local.toml can
        # select this one over it.
        updates["embed_model"] = embed
    elif _confirm(f"Use the embedding model {embed} for each project (the global config)? A project whose index "
                  "holds another model embeds each symbol again at its next index run", default=True):
        global_embed = embed
        clear.append("embed_model")
    else:
        updates["embed_model"] = embed
    if not _save_profile(project_root, name, values, updates, clear_keys=clear, global_embed=global_embed):
        return
    make_machine_profile(name)
    print(f"  [ok] LLM configured: profile {name} (ollama + {values['model']}), embedding {embed}")
    where = ".fw-context/local.toml selects it" if "profile" in updates else (
        ".fw-context/config.toml selects it" if committed.get("profile") else "it is the global default")
    print(f"  [ok] Profile saved to {settings._GLOBAL_CONFIG_PATH}; {where}")
    if "embed_model" in updates:
        print(f"  [note] The embedding model {embed} is of this project only (.fw-context/local.toml); the "
              f"global config has {global_llm.get('embed_model') or 'none'}. 'fw-context doctor' reports the "
              "difference")


def _local_ollama_profile(
    project_root: Path | None, cfg, *, cloud: dict[str, str] | None = None, ask_embed: bool = True,
) -> tuple[str, dict[str, object], str] | None:
    """Ask for the models of a local Ollama profile, pull them with consent; return (name, values, embedding model).

    The default of each prompt is the model that a config file sets now,
    else the recommendation.  WHY: before, the defaults were always the
    recommendation, thus Enter replaced a model of the global config, for
    example a larger chat model, with the recommended one.  Not a model
    that Ollama does not serve: the chat model of an API profile, or an
    embedding model of sentence-transformers or ``ft://``.  The step would
    offer to pull it from Ollama.

    The chat model goes to the Ollama profile of this machine
    (``_machine_profile_for``), never to a profile of one project.  Its
    other keys stay; the caller writes it.  The recommended models come from the rule of two bands
    (``llm.recommend``): the measured speed of each installed model of the
    list, after a question, else an estimate from the memory of the
    machine; the table shows each model.  *cloud* names the cloud models
    of the local Ollama, which the list leaves out: they do not run on
    this machine.  Without *ask_embed* the embedding model is not asked
    (the setup of the LLM services: the embedding belongs to the project),
    and the third value is "".

    Returns None when the user stops at the question about the Ollama
    profile of this machine (``_machine_profile_for``).
    """
    from ..config.settings import _DEFAULT_OLLAMA_URL, _is_loopback_url, llm_key_sources
    from ..llm.auto_model import _gpu_available
    from ..llm.chat_router import chat_ollama_server
    from ..llm.embedder_factory import uses_ollama
    from ..llm.ollama import check_setup

    gpu = _gpu_available()
    sources = llm_key_sources(project_root) if project_root is not None else {}
    chat_server = chat_ollama_server(cfg.llm) if cfg.llm.provider == "ollama" else None
    ollama_profile = (
        bool(cfg.llm.profile) and chat_server is not None and _is_loopback_url(chat_server)
        and cfg.llm.model not in (cloud or {})
    )
    server = chat_server if ollama_profile and chat_server else _DEFAULT_OLLAMA_URL

    result = check_setup(cfg.llm)
    status = result.get("status")
    # `ollama_running`, not the status: "ok" also comes for a setup that
    # needs no Ollama (chat on an API, local embeddings), and it then said
    # "Ollama running" for a server that does not run.
    if not result.get("ollama_running"):
        print(f"  [warn] Ollama cannot be used: {result.get('message') or 'no Ollama server at ' + cfg.llm.ollama_url}")
    else:
        print(f"  [ok] Ollama running at {cfg.llm.ollama_url}")
        installed = [model for model in result.get("installed_models") or [] if model not in (cloud or {})]
        if installed:
            print(f"  Installed models: {', '.join(installed)}")
        # "error" with a running server: the Ollama server of chat_api_base
        # gives no model list.  Its message also names a missing embedding
        # model.
        if status in ("model_missing", "error") and result.get("message"):
            print(f"  [info] {result['message']}")

    print(f"  Hardware: {'GPU detected' if gpu else 'CPU only (no GPU detected)'}")
    from ._recommend import table_lines

    chat_from_config = ollama_profile and bool(cfg.llm.model)
    embed_from_config = "embed_model" in sources and cfg.llm.embed_model and uses_ollama(cfg.llm)
    found = _recommendations(server, cfg.llm.ollama_url, ask_embed=ask_embed, keep_alive=cfg.llm.keep_alive,
                             timeout=cfg.llm.timeout)
    chat_recommended = found["chat"].model.tag if found["chat"].model else _FALLBACK_CHAT_MODEL
    embed_recommended = (found["embedding"].model.tag if "embedding" in found and found["embedding"].model
                         else _FALLBACK_EMBED_MODEL if gpu else _FALLBACK_CPU_EMBED_MODEL)
    chat_default = cfg.llm.model if chat_from_config else chat_recommended
    embed_default = cfg.llm.embed_model if embed_from_config else embed_recommended
    for name, recommendation in found.items():
        current = (chat_default if chat_from_config else None) if name == "chat" else (
            embed_default if embed_from_config else None)
        print("\n".join(table_lines(recommendation, current)))
    fallback = " (no recommendation: a small model that runs on each machine)"
    print("  Recommended:")
    print(f"    chat:      {chat_recommended}" + ("" if found["chat"].model else fallback))
    if ask_embed:
        print(f"    embedding: {embed_recommended}" + ("" if found["embedding"].model else fallback))
    if (chat_default, embed_default if ask_embed else embed_recommended) != (chat_recommended, embed_recommended):
        print(f"  Current config: chat {chat_default}" + (f", embedding {embed_default}" if ask_embed else "")
              + " — Enter keeps them")

    chat = prompt_text("Chat model", default=chat_default) or chat_default
    found_profile = _machine_profile_for(server, chat)
    if found_profile is None:
        return None
    name, values = found_profile
    chat = str(values["model"])
    embed = (prompt_text("Embedding model", default=embed_default) or embed_default) if ask_embed else ""

    from ..deps._fixes import _ollama_pull

    # The chat model to the server of the profile, the embedding model to
    # the server of the embeddings: the checks ask each one for its model.
    if _confirm(f"Pull {chat} now? (multi-GB)"):
        ok, msg = _ollama_pull(chat, server)
        print(f"  [{'ok' if ok else 'warn'}] {msg}")
    if embed and _confirm(f"Pull {embed} now? (multi-GB)"):
        ok, msg = _ollama_pull(embed, cfg.llm.ollama_url)
        print(f"  [{'ok' if ok else 'warn'}] {msg}")
    return name, values, embed


def _machine_profile_for(server: str, chat: str) -> tuple[str, dict[str, object]] | None:
    """Return the Ollama profile of this machine for the local Ollama setup, with the chat model *chat*.

    One Ollama profile for each machine (``llm.profile_state``): the setup
    writes the chat model into it, and never makes a profile for one
    project.  The profile is the Ollama profile of this machine when it
    is of the local Ollama, else ``local``.  When the Ollama profile of
    this machine is of another computer, the user makes ``local`` the new
    one, or stops.  The profile is shared: a new chat model in it is the
    chat model of each project, thus a "no" keeps its model.  A profile
    ``local`` that is not of the local Ollama (an API, another computer)
    is never changed: the setup takes a free name.  While a local Ollama
    gives no model list, only ``[llm] ollama_profile`` names the profile
    of this machine: a cloud model would look like a local one.

    Returns None when the user stops.
    """
    from ..config import settings
    from ..config.settings import _DEFAULT_OLLAMA_URL, _is_loopback_url, read_config_file
    from ..llm.profile_state import cloud_lists, is_cloud_model, known, ollama_profile_name, unknown_servers

    llm = read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm") or {}
    raw = llm.get("profiles")
    profiles: dict = raw if isinstance(raw, dict) else {}
    lists = cloud_lists(profiles)
    cloud = known(lists)
    if unknown_servers(profiles, lists):
        named = llm.get("ollama_profile")
        machine = named if isinstance(named, str) and isinstance(profiles.get(named), dict) else None
    else:
        machine = ollama_profile_name(llm, cloud)

    def own(profile: object) -> bool:
        # A cloud model is not of the local Ollama: before, a profile
        # "local" of a cloud model became the Ollama profile of this
        # machine, and its code went to the cloud.  Without a model list a
        # cloud model is not known; the test call of the setup then fails
        # (a cloud model runs through the local Ollama), and the global
        # config gets its earlier text back.
        return (isinstance(profile, dict) and profile.get("provider") == "ollama"
                and _is_loopback_url(str(profile.get("url") or _DEFAULT_OLLAMA_URL))
                and not is_cloud_model(profile, cloud))

    machine_url = str((profiles.get(machine) or {}).get("url") or _DEFAULT_OLLAMA_URL) if machine else ""
    if machine and _is_loopback_url(machine_url):
        name = machine
    else:
        name, number = "local", 1
        while name in profiles and not own(profiles[name]):
            number += 1
            name = f"local-{number}"
        if machine and not _confirm(
            f"The Ollama profile of this machine is {machine!r} ({machine_url}). Make {name!r} (the local Ollama) "
            "the Ollama profile of each project?", default=True,
        ):
            print("  [skip] The Ollama profile of this machine is not changed: 'fw-context llm default <profile> "
                  "--ollama' changes it")
            return None
    earlier = profiles.get(name)
    keep: dict = earlier if own(earlier) and isinstance(earlier, dict) else {"url": server}
    values: dict[str, object] = {**keep, "provider": "ollama", "model": chat}
    from ..config._profiles import replaces_other_values

    if replaces_other_values(name, values) and not _confirm(
        f"The profile {name!r} is shared: each project that uses Ollama gets the chat model {chat}. Change it?",
        default=False,
    ):
        values["model"] = keep.get("model") or chat
        print(f"  [info] The profile {name!r} keeps the chat model {values['model']}")
    return name, values


def make_machine_profile(name: str) -> None:
    """Make *name* the Ollama profile of this machine after the local Ollama setup wrote it, when it is not yet.

    The key ``[llm] ollama_profile`` is written only when the rule without
    it gives another profile (``ollama_profile_name``): a global config
    with one Ollama profile needs no key.
    """
    from ..config import settings
    from ..config._profiles import select_global_profile
    from ..llm.profile_state import cloud_lists, known, ollama_profile_name, unknown_servers

    try:
        llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm") or {}
        raw = llm.get("profiles")
        profiles: dict = raw if isinstance(raw, dict) else {}
        lists = cloud_lists(profiles)
        # Without a list, the rule can give a cloud model: the key then
        # names the profile without a guess.
        if not unknown_servers(profiles, lists) and ollama_profile_name(llm, known(lists)) == name:
            return
        if llm.get("ollama_profile") == name:
            return
        select_global_profile(None, ollama_profile=name)
    except (OSError, settings.ConfigError) as exc:
        print(f"  [warn] {name!r} is not the Ollama profile of this machine ({exc}): run "
              f"'fw-context llm default {name} --ollama'")
        return
    print(f"  [ok] Profile {name!r} is the Ollama profile of this machine: each project that uses Ollama uses it")


#: The models when the rule has no candidate: the memory of the machine is
#: not known (Windows gives no RAM), and no installed model of the list is
#: measured.  A small model runs on each machine; the user selects another.
_FALLBACK_CHAT_MODEL = "qwen2.5-coder:7b"
#: 4b and not the automatic 8b: the same quality, less VRAM and smaller
#: vectors (measured 2026-10-21).  On the CPU, 0.6b: 4b was 4 times slower
#: on the GPU of the measurement (22 against 92 symbols/s).  The fallback
#: only: with a known memory, the rule of two bands decides, and it puts
#: the quality first (decision of the user, 2026-10-21).
_FALLBACK_EMBED_MODEL = "qwen3-embedding:4b"
_FALLBACK_CPU_EMBED_MODEL = "qwen3-embedding:0.6b"


def _recommendations(chat_server: str, embed_server: str, *, ask_embed: bool, keep_alive: str,
                     timeout: float) -> dict:
    """Return the recommendation of each role (``llm.recommend.gather``); measure the installed models after a question.

    WHY a question: each installed model of the list without a stored
    speed loads once (5 to 70 s each), and the setup of a machine with
    many models then takes minutes.  Without the measurement, the
    estimate from the memory of the machine applies to them too.
    """
    from ..config.settings import _is_loopback_url
    from ..llm.hardware import machine_memory
    from ..llm.recommend import CHAT, EMBEDDING, gather

    memory = machine_memory()
    roles = [(CHAT, chat_server)] + ([(EMBEDDING, embed_server)] if ask_embed else [])

    def run(measure: bool) -> dict:
        return {role.name: gather(role, server, memory if _is_loopback_url(server) else None, measure=measure,
                                  keep_alive=keep_alive, timeout=timeout,
                                  progress=lambda text: print(f"  [measure] {text}", flush=True))
                for role, server in roles}

    found = run(False)
    waiting = [c.tag for recommendation in found.values() for c in recommendation.candidates if c.can_measure]
    if waiting and _confirm(f"Measure the speed of {len(waiting)} installed models now? Each one loads one time "
                            "(5-70 s each); the recommendation then compares the measured speeds", default=True):
        found = run(True)
    return found


def _read_secret(prompt: str) -> str:
    """Read a secret from the terminal without an echo (a seam for the tests)."""
    import getpass

    return getpass.getpass(prompt)


def _store_api_key(profile: str) -> bool:
    """Ask for the API key of *profile* and store it; return False when the setup must stop.

    An empty answer stores nothing: a server without a key (a proxy on the
    local network) is valid.  The messages never hold the key.
    """
    from ..llm.secrets import SecretStoreError, secrets_path, set_api_key

    while True:
        value = _read_secret("  API key (no echo; empty: no key) > ").strip()
        if not value:
            return True
        try:
            place = set_api_key(profile, value)
            break
        except ValueError as exc:
            # A space, a control character, a character that is not ASCII:
            # the message holds no key.  Before, the error stopped init.
            print(f"  [warn] {exc}: give the key again")
        except SecretStoreError as exc:
            print(f"  [warn] {exc}. Cloud API setup stopped")
            return False
    where = "the keyring of the system" if place == "keyring" else f"{secrets_path()} (no keyring can be used)"
    print(f"  [ok] The API key is stored in {where}")
    return True


def _custom_api_profile() -> str | None:
    """Write a profile of another OpenAI-compatible API to the global config (the setup of the LLM services).

    Returns its name, or None when nothing is written.
    """
    from ..config import settings
    from ..config._profiles import set_profile

    found = _custom_api_values()
    if found is None:
        return None
    name, values = found
    try:
        set_profile(name, values)
    except (ValueError, OSError, settings.ConfigError) as exc:
        print(f"  [warn] The profile {name!r} is not written: {exc}")
        return None
    print(f"  [ok] Profile {name!r} saved to {settings._GLOBAL_CONFIG_PATH}")
    return name


def _custom_api_values() -> tuple[str, dict[str, object]] | None:
    """Ask for a profile of provider openai_compatible (name, URL, key, model); store its key; return (name, values).

    The profile never gets the API key: a key in a TOML file is a secret in
    plain text.  The profile gets the NAME of the environment variable of
    the key, or, without one, the step reads the key without an echo and
    stores it for the profile (``llm.secrets``: the keyring, else
    ``secrets.toml``).
    """
    from ..config._profiles import replaces_other_values
    from ..config.settings import _is_loopback_url, url_problem

    name = prompt_text("Profile name", default="cloud") or "cloud"
    url = prompt_text("API URL") or ""
    if not url:
        print("  [skip] Cloud API setup skipped")
        return None
    # Before `_is_loopback_url`: it raised ValueError for "http://[::1",
    # and init stopped with a traceback.
    problem = url_problem(url)
    if problem is not None:
        print(f"  [warn] {url} is not a valid URL ({problem}). Cloud API setup skipped")
        return None
    key_env = prompt_text("Environment variable with the API key (empty: store the key in the keyring)") or ""
    model = prompt_text("Model", default="deepseek-chat") or "deepseek-chat"

    if not _is_loopback_url(url):
        print(f"  ⚠ Source code will be sent to {url}.")
        print("    Ensure this complies with your data security policy.")

    values: dict[str, object] = {"provider": "openai_compatible", "url": url, "model": model, "api_key_env": key_env or None}
    # Each project that selects the profile uses it: a replaced "local"
    # profile sent the source code of each project to this API.
    while replaces_other_values(name, values) and not _confirm(
        f"The profile {name!r} exists, and each project that selects it uses it. Replace it?", default=False,
    ):
        name = prompt_text("Another profile name (empty: skip)") or ""
        if not name:
            print("  [skip] Cloud API setup skipped")
            return None
    # The key before the test call: the call needs it.
    if not key_env and not _store_api_key(name):
        return None
    return name, values


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
        if b.idf_path:
            print(f"  Build idf_path: {b.idf_path} (already configured)")
        if b.activate and system == "zephyr":
            # Show what is configured — a hand-written script included — as
            # the standing choice, with the installed SDKs behind it so a
            # re-run can switch.  init never asks for a script path: a custom
            # one is set by hand in the config, and this only offers to move
            # off it.
            chosen = _configure_ncs_version(
                project_root, dry_run=dry_run, non_interactive=non_interactive,
                current=b.activate,
            )
            if chosen is not None and chosen != b.activate and not dry_run:
                from ._init import _write_build_key

                _write_build_key(project_root, "activate", chosen)
                print(f"  [ok] Build activate: {chosen}")
            return
        if b.activate:
            print(f"  Build activate: {b.activate} (already configured)")
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

"""Convert the old ``[llm]`` backend keys of the config files into LLM profiles.

Before 2026-10-20, ``[llm]`` named the backend directly (``ollama_url``,
``model``, ``num_ctx``, ``chat_api_base``, ...), in each of the three
config files.  Now a profile of the global config names it
(``[llm.profiles.<name>]``), and a project selects one (``[llm] profile``).
``load`` refuses an old key with a sentence that says where it goes
(``settings._MOVED_LLM_KEYS``); ``fw-context init`` and ``fw-context
doctor --fix --only config`` run this conversion (decision of the user,
2026-10-20: convert the active keys too, and correct the commented lines).

* The global config: its old keys become a profile, and ``[llm] profile``
  selects it.
* A project: its old keys (``config.toml``, then ``local.toml``, as
  ``load`` merged them) apply over the profile that the project selects.
  When the result is the same profile, the keys go away; else the result
  is a new profile (named after the project directory, or an existing
  profile with the same values), and ``local.toml`` selects it.  The
  embeddings server of the project goes the same way (``embed_profile``).
* The commented lines of the old templates in ``[llm]``
  (``# ollama_url = ...``, the text about the chat API, ...) go away, and
  the ``local.toml`` of the project gets the commented ``profile`` line.

A file with ``chat_api_key`` is not converted: a profile has no place for
the value of an API key (a key in a TOML file is a secret in plain text).
The report says so; the user moves the key to an environment variable and
names it with ``api_key_env``.

Each changed file gets a ``.bak`` copy first (not ``config.toml`` of a
project: git holds its old text), through ``housekeeping``.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from pathlib import Path

import tomlkit

from .config import settings
from .config._profiles import project_profile_name, put_profile
from .housekeeping import CleanupReport, _config_files, _parse_config_file, _write_config_file

#: The default chat model of an old config that named none (the default of ``LLMConfig``).
_OLD_DEFAULT_MODEL = "qwen2.5-coder:14b"

#: The old ``[llm]`` keys of an Ollama backend, and their key in a profile.
_OLLAMA_KEYS = {
    "ollama_url": "url", "model": "model", "num_ctx": "num_ctx", "keep_alive": "keep_alive",
    "timeout": "timeout", "stream": "stream", "auto_pull": "auto_pull", "ollama_max_concurrent": "max_concurrent",
}

#: A commented line of an old key of ``[llm]`` (``# model = ...``, ``#   chat_api_base = ...``).
_OLD_KEY_COMMENT = re.compile(r"^#\s*(" + "|".join(settings._MOVED_LLM_KEYS) + r")\s*=")

#: The other commented lines of the old templates about the old keys: the
#: text about the chat API, the auto-pull and the streaming, and the list
#: of chat models (that list is now above ``model`` of the profile).
_OLD_TEMPLATE_COMMENTS = frozenset({
    "# Chat model for symbolic analysis and search query generation.",
    "# Minimum recommended: 14B parameters — smaller models produce inconsistent JSON",
    "# and poor-quality descriptions for C++ embedded code.",
    "# Recommended models (min. 14B parameters):",
    "#   Local:",
    "#     qwen2.5-coder:14b        (4.8 GB VRAM, balanced speed/quality)",
    "#     deepseek-coder-v2:16b    (10 GB VRAM, strong code comprehension)",
    "#     qwen2.5-coder:32b        (19 GB VRAM, best local quality)",
    '#   Cloud (Ollama proxies to cloud APIs — "ollama pull <model>"):',
    "#     qwen3-coder:480b-cloud         (coding-focused, best quality)",
    "#     deepseek-v4-flash:cloud        (284B MoE, 13B active — great value)",
    "#     gemini-3-flash-preview:latest-cloud   (fast, affordable)",
    "#   See: https://ollama.com/search?c=cloud",
    "# ── Chat API — use OpenAI-compatible API for chat instead of local Ollama ──",
    "# When set, chat calls are routed to this endpoint. Embedding still uses",
    "# local Ollama (embed_model above). Format is auto-detected from the URL:",
    "#   :11434 or /api/generate → Ollama native (keeps keep_alive, num_ctx, auto_pull)",
    "#   /v1 or bare host       → OpenAI-compatible (DeepSeek, OpenAI, LiteLLM, vLLM, etc.)",
    "# Examples:",
    "# Auto-pull models on 404 (Ollama only). Set true for auto-recovery in",
    "# online environments. Set false (default) for intranet/offline — model",
    "# pulls must be done explicitly via `ollama pull` or the agent flow.",
    "# Stream chat responses (both OpenAI-compatible and Ollama-native).",
    "# When true, sends stream:true and consumes SSE chunks — keeps the HTTP",
    "# connection alive with continuous data flow, avoiding reverse-proxy idle",
    "# timeouts (nginx 60s, Cloudflare 100s). Default false — non-streaming is",
    "# sufficient for local Ollama. When false, SSE responses are still",
    "# auto-detected and parsed as a fallback.",
})

#: The commented ``profile`` line that a project ``local.toml`` gets.
_PROFILE_COMMENT = '# profile = "local"   # the LLM profile of this project: a profile of the global config'


def migrate_llm_profiles(project_root: Path | None, *, dry_run: bool = False) -> CleanupReport:
    """Convert the old ``[llm]`` keys of the global config and of the project into profiles.

    Returns the report: ``removed_keys`` names each change (with
    *dry_run*, each change that the run would make), ``failures`` each
    file that it did not convert, and why.

    The files change as one unit: each new text is made and checked
    first, and each file must be writable (``_write_problem``), else no
    file changes.  Then the files are written in the order global config,
    ``local.toml``, ``config.toml``, and the first write that fails stops
    the rest.  WHY this order: a stop after the global config leaves only
    a profile that nothing selects, and a stop after ``local.toml`` leaves
    the old keys in ``config.toml``, which a second run converts again.
    The reverse order removed the old keys from ``config.toml`` first, and
    a failed write of ``local.toml`` lost them.
    """
    report = CleanupReport()
    files = _config_files(project_root)
    global_path = files[0][0]
    # Without the global config, a project has no profile to select: make
    # it from the template first, as ``load`` does.  Before, the run did
    # nothing, and the next ``load`` refused the old keys of the project.
    # A dry run uses the template text and makes no file.
    template: tomlkit.TOMLDocument | None = None
    if not global_path.exists():
        if dry_run:
            template = tomlkit.parse(settings._GLOBAL_DEFAULTS)
        else:
            try:
                settings._ensure_global_config()
            except (OSError, settings.ConfigError) as exc:
                report.failures.append((global_path, f"the global config cannot be made ({exc})"))
                return report
    documents: dict[Path, tuple[tomlkit.TOMLDocument, str, bool]] = {}
    for path, _of_project, backup in files:
        document, _unreadable = _parse_config_file(path, report)
        if document is None and path == global_path and template is not None:
            document = template
        if document is not None:
            documents[path] = (document, document.as_string(), backup)
    if global_path not in documents:
        return report
    global_doc = documents[global_path][0]
    changes: dict[Path, list[str]] = {path: [] for path in documents}
    project_files = [(path, documents[path][0]) for path, of_project, _ in files if of_project and path in documents]

    # ── The global config ──
    global_llm = global_doc.get("llm")
    old = _old_keys(global_llm)
    if old and "chat_api_key" in old:
        # The project selects a profile of the global config: without the
        # conversion of the global config, no profile is there to select.
        report.failures.append((global_path, _KEY_FAILURE))
        report.failures += [
            (path, "not converted: the global config is not converted (see its message)")
            for path, document in project_files if _old_keys(document.get("llm"))
        ]
        return report
    global_old: dict[str, object] | None = None
    if old and isinstance(global_llm, dict):
        global_old = old
        values, embed = _profile_from_old(old)
        name = _place_profile(global_doc, values)
        for key in old:
            del global_llm[key]
        if "profile" not in global_llm:
            global_llm["profile"] = name
        if embed is not None:
            global_llm["embed_profile"] = _place_profile(global_doc, embed, preferred="embeddings")
        changes[global_path].append(f"moved [llm] {', '.join(sorted(old))} to [llm.profiles.{name}]{_not_kept(old, values)}")

    # ── The project ──
    with_key = [path for path, document in project_files if "chat_api_key" in _old_keys(document.get("llm"))]
    if with_key:
        report.failures += [(path, _KEY_FAILURE) for path in with_key]
        return report
    project_old: dict[str, object] = {}
    for _path, document in project_files:
        project_old.update(_old_keys(document.get("llm")))
    local_path = (
        project_root / settings._PROJECT_CONFIG_DIR / settings._PROJECT_LOCAL_CONFIG_NAME
        if project_root is not None else None
    )
    if project_old and project_root is not None and local_path is not None:
        _pin_global_selection(global_doc, changes[global_path])
        _convert_project(global_doc, project_files, project_old, project_root, local_path, documents, changes,
                         global_old=global_old)

    # ── The commented lines, and the check of each new text ──
    texts: dict[Path, str] = {}
    for path, (document, _original, _backup) in documents.items():
        text = tomlkit.dumps(document)
        new_text, dropped, added = _clean_comments(text, profile_line=path == local_path)
        if added and not dropped and not changes[path]:
            # A valid local.toml is not written for a comment alone: doctor
            # reported such a file as a file to correct.
            new_text, added = text, False
        if dropped:
            changes[path].append(f"removed {dropped} old commented line(s) of [llm]")
        if added:
            changes[path].append("added the commented [llm] profile line")
        if not changes[path]:
            continue
        try:
            tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as exc:
            report.failures.append((path, f"the conversion gives text that is not valid TOML ({exc}); no file changed"))
            return report
        texts[path] = new_text

    # ── The write: global config, local.toml, config.toml ──
    order = sorted(texts, key=lambda path: (path != global_path, path != local_path))
    # A file that cannot be written stops the run before the first write.
    # WHY: a write that failed after local.toml selected the new profile
    # left the old keys in config.toml, and a second run then applied them
    # over that profile, thus over the keys of local.toml.  Only a change
    # between this check and the write can stop the run after a write now.
    blocked = [(path, reason) for path in order if (reason := _write_problem(path)) is not None]
    if blocked:
        report.failures += blocked
        report.failures += [
            (path, "not written: another file of the conversion cannot be written (see its message)")
            for path in order if path not in dict(blocked)
        ]
        return report
    for number, path in enumerate(order):
        document, original, backup = documents[path]
        failures = len(report.failures)
        _write_config_file(path, tomlkit.parse(texts[path]), original, changes[path], report, backup=backup,
                           dry_run=dry_run)
        if len(report.failures) > failures:
            report.failures += [
                (rest, f"not written, because the write of {path} failed: run the conversion again after the repair")
                for rest in order[number + 1:]
            ]
            break
    return report


def _convert_project(global_doc, project_files, project_old: dict[str, object], project_root: Path,
                     local_path: Path, documents: dict, changes: dict[Path, list[str]], *,
                     global_old: dict[str, object] | None) -> None:
    """Convert the old keys of the project files (*project_old*, merged as ``load`` merged them).

    The old keys of the project applied over the values of the layer
    below: thus the profile that the project selects now is converted back
    to old keys (``_old_from_profiles``), the old keys of the project go
    over them, and the result is converted again.  A project with only
    ``num_ctx`` thus keeps the API and the embeddings server of the global
    config.  The project selects a new profile only when its chat or its
    embeddings differ from the selected ones; it selects them in
    ``local.toml``, which is made when it is missing.

    *global_old* holds the old keys of the global config when this run
    converted them.  When the project selects no profile of its own, they
    are the layer below, as they were for ``load``: the profile cannot
    tell an explicit ``chat_api_format`` from a detected one, and the
    format of the global config applied to a ``chat_api_base`` of the
    project.  Else the format comes from the URL of the project, because
    the old format of the layer below is not known.
    """
    base, base_embed = _selected_profiles(global_doc, project_files)
    own_selection = any(
        key in (document.get("llm") or {}) for _path, document in project_files for key in ("profile", "embed_profile")
    )
    if global_old is not None and not own_selection:
        base_old = dict(global_old)
    else:
        base_old = _old_from_profiles(base, base_embed) if base is not None else {}
        if "chat_api_base" in project_old and "chat_api_format" not in project_old:
            base_old.pop("chat_api_format", None)
    effective = {**base_old, **project_old}
    values, embed = _profile_from_old(effective)
    base_values, base_embed_values = _profile_from_old(base_old) if base is not None else (None, None)
    if base is not None and "api_key_env" in base:
        for converted in (values, base_values):
            if converted is not None and converted.get("url") == base.get("url"):
                converted["api_key_env"] = base["api_key_env"]

    for path, document in project_files:
        moved = _old_keys(document.get("llm"))
        for key in moved:
            del document["llm"][key]
        if moved:
            changes[path].append(f"removed [llm] {', '.join(sorted(moved))} (now in an LLM profile){_not_kept(moved, values)}")
    profiles_before = set(_profile_names(global_doc))
    selected: dict[str, str] = {}
    chat_name = None
    if values != base_values:
        chat_name = _place_profile(global_doc, values, preferred=project_profile_name(project_root))
        selected["profile"] = chat_name
    if embed != base_embed_values:
        if embed is not None:
            selected["embed_profile"] = _place_profile(global_doc, embed, preferred="embeddings")
        elif values["provider"] == "ollama":
            # The embeddings go to the chat server: name it, because a
            # layer below selects another embeddings profile.
            selected["embed_profile"] = chat_name or _place_profile(global_doc, values)
        else:
            selected["embed_profile"] = _place_profile(global_doc, _DEFAULT_EMBED, preferred="embeddings")
    if not selected:
        return
    if local_path not in documents:
        documents[local_path] = (tomlkit.document(), "", True)
        changes[local_path] = []
    local = documents[local_path][0]
    llm = local.setdefault("llm", tomlkit.table())
    for key, name in selected.items():
        llm[key] = name
        changes[local_path].append(f'selected [llm] {key} = "{name}"')
    added = [name for name in _profile_names(global_doc) if name not in profiles_before]
    if added:
        global_path = next(iter(documents))
        changes[global_path].append(f"added the LLM profiles of the project ({', '.join(added)})")


def _pin_global_selection(global_doc, changes: list[str]) -> None:
    """Make ``[llm] profile`` of the global config explicit before the conversion of a project adds profiles.

    ``load`` selects the only profile when ``[llm] profile`` is not set.
    WHY: a global config without profiles got only the profile of the
    project, and that profile became the profile of each other project
    (their source code went to the API of this project); a second profile
    gave each other project a ConfigError.  Without a profile, the global
    config gets the profile of the old defaults (a local Ollama), which is
    the backend that the other projects used.
    """
    llm = global_doc.get("llm")
    if llm is None:
        llm = tomlkit.table()
        global_doc["llm"] = llm
    if not isinstance(llm, dict) or _plain(llm.get("profile")):
        return
    names = _profile_names(global_doc)
    if len(names) > 1:
        return  # load refuses this config already, and names the key to set
    name = names[0] if names else _place_profile(global_doc, _profile_from_old({})[0])
    llm["profile"] = name
    changes.append(f'selected [llm] profile = "{name}" (the profile of each project that selects none)')


def _profile_names(document) -> list[str]:
    """Return the names of the profiles of *document*."""
    llm = document.get("llm")
    profiles = llm.get("profiles") if isinstance(llm, dict) else None
    return list(profiles) if isinstance(profiles, dict) else []


def _not_kept(old: dict[str, object], values: dict) -> str:
    """Return a note for the report when *old* has ``num_ctx`` and the chat goes to an API: no profile key holds it.

    The old OpenAI-compatible path did not send ``num_ctx``, thus the
    backend stays the same; the note says why the value is not in the profile.
    """
    if "num_ctx" in old and values.get("provider") == "openai_compatible":
        return "; num_ctx is not kept: the chat goes to an OpenAI-compatible API, which did not get it"
    return ""


def _write_problem(path: Path) -> str | None:
    """Return why *path* cannot be written, or None; a missing file needs a directory that can be written."""
    try:
        target = path.resolve()
        if target.exists():
            if not stat.S_IMODE(target.stat().st_mode) & stat.S_IWUSR or not os.access(target, os.W_OK):
                return "the file cannot be written (no write permission); no file of the conversion changed"
            return None
        if not os.access(target.parent, os.W_OK):
            return f"the directory {target.parent} cannot be written; no file of the conversion changed"
        return None
    except (OSError, RuntimeError) as exc:
        return f"{exc}; no file of the conversion changed"


_KEY_FAILURE = (
    "[llm] chat_api_key is not converted: a profile has no place for the value of an API key. Put the key into "
    "an environment variable, write the profile by hand ([llm.profiles.<name>] with api_key_env = \"<variable>\", "
    "see docs/configuration.md), and remove the old [llm] keys"
)

#: The embeddings profile that ``load`` uses when none is selected and the
#: chat goes to an API.  Its ``model`` is not used (the embeddings use
#: ``embed_model``), but an Ollama profile needs one: it is the old
#: default chat model, the model that the old config gave that server.
_DEFAULT_EMBED: dict[str, object] = {"provider": "ollama", "url": settings._DEFAULT_OLLAMA_URL, "model": _OLD_DEFAULT_MODEL}

#: The old keys of the Ollama tuning, and their key in a profile: with the
#: chat on an API, they applied to the embeddings only.
_EMBED_TUNING_KEYS = {"keep_alive": "keep_alive", "auto_pull": "auto_pull", "ollama_max_concurrent": "max_concurrent"}


def _old_keys(llm: object) -> dict[str, object]:
    """Return the old backend keys of an ``[llm]`` table, with their values."""
    if not isinstance(llm, dict):
        return {}
    return {key: _plain(llm[key]) for key in settings._MOVED_LLM_KEYS if key in llm}


def _plain(value: object) -> object:
    """Return the Python value of a tomlkit item (``unwrap``), or the value as it is."""
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def _profile_from_old(old: dict[str, object]) -> tuple[dict, dict | None]:
    """Return the chat profile of the old keys *old*, and the embeddings profile.

    The embeddings profile is None when ``load`` gives the same
    embeddings without one: the chat server of an Ollama profile, or the
    default server with the default tuning for an API profile.  An old
    key that *old* does not hold has its old default.
    """
    from .config.settings import LLMConfig
    from .llm.chat_router import chat_ollama_server, detect_chat_endpoint

    model = old.get("model") or _OLD_DEFAULT_MODEL
    embed_url = str(old.get("ollama_url") or settings._DEFAULT_OLLAMA_URL)
    tuning = {_OLLAMA_KEYS[key]: old[key] for key in _OLLAMA_KEYS if key in old and key not in ("ollama_url", "model")}
    if old.get("chat_api_base"):
        probe = LLMConfig(chat_api_base=str(old["chat_api_base"]), chat_api_format=str(old.get("chat_api_format", "auto")))
        fmt, _endpoint = detect_chat_endpoint(probe)
        if fmt == "openai":
            values = {"provider": "openai_compatible", "url": old["chat_api_base"], "model": model}
            values.update({key: old[key] for key in ("timeout", "stream") if key in old})
            # The old OpenAI path did not send num_ctx; the Ollama tuning
            # applied to the embeddings only.
            embed = {**_DEFAULT_EMBED, "url": embed_url}
            embed.update({_EMBED_TUNING_KEYS[key]: old[key] for key in _EMBED_TUNING_KEYS if key in old})
            return values, None if embed == _DEFAULT_EMBED else embed
        chat_server = chat_ollama_server(probe) or str(old["chat_api_base"])
        values = {"provider": "ollama", "url": chat_server, "model": model, **tuning}
        if embed_url.rstrip("/") == chat_server.rstrip("/"):
            return values, None
        return values, {**_DEFAULT_EMBED, "url": embed_url}
    return {"provider": "ollama", "url": embed_url, "model": model, **tuning}, None


def _old_from_profiles(chat: dict, embed: dict | None) -> dict[str, object]:
    """Return the old keys that give the backend of the profile *chat* and the embeddings profile *embed*.

    The reverse of ``_profile_from_old``, as ``load`` resolves the two
    profiles (``settings._resolve_llm_profile``): with an Ollama chat, the
    tuning of the chat profile applies; with an API chat, the tuning of
    the embeddings profile.  ``api_key_env`` has no old key: the caller
    copies it.
    """
    old: dict[str, object] = {"model": chat.get("model")}
    if chat.get("provider") == "openai_compatible":
        old.update({"chat_api_base": chat.get("url"), "chat_api_format": "openai"})
        old.update({key: chat[key] for key in ("timeout", "stream") if key in chat})
        if embed is not None:
            old["ollama_url"] = embed.get("url", settings._DEFAULT_OLLAMA_URL)
            old.update({key: embed[name] for key, name in _EMBED_TUNING_KEYS.items() if name in embed})
        return old
    old.update({key: chat[name] for key, name in _OLLAMA_KEYS.items() if name in chat and key not in ("ollama_url", "model")})
    chat_url = str(chat.get("url", settings._DEFAULT_OLLAMA_URL))
    embed_url = str(embed.get("url", settings._DEFAULT_OLLAMA_URL)) if embed is not None else chat_url
    old["ollama_url"] = embed_url
    if chat_url.rstrip("/") != embed_url.rstrip("/"):
        old.update({"chat_api_base": chat_url, "chat_api_format": "ollama"})
    return old


def _place_profile(document, values: dict, *, preferred: str | None = None) -> str:
    """Put *values* into the profiles of *document* and return the name of the profile.

    A profile with the same values is reused.  A new one gets *preferred*,
    or a name from the provider (``local``, ``remote``, ``api``), with
    ``-2``, ``-3``, ... when the name is taken.
    """
    profiles = (document.get("llm") or {}).get("profiles") or {}
    existing = {name: _plain(table) for name, table in profiles.items()}
    for name, table in existing.items():
        if table == values:
            chosen = name
            break
    else:
        if preferred:
            stem = preferred
        elif values["provider"] == "openai_compatible":
            stem = "api"
        else:
            stem = "local" if settings._is_loopback_url(str(values.get("url", ""))) else "remote"
        chosen, number = stem, 1
        while chosen in existing:
            number += 1
            chosen = f"{stem}-{number}"
        put_profile(document, chosen, values)
    return chosen


def _selected_profiles(global_doc, project_files) -> tuple[dict | None, dict | None]:
    """Return the chat profile and the embeddings profile that the project selects now, as ``load`` selects them.

    The project files select over the global config.  The embeddings
    profile is None when ``load`` uses no profile for them.
    """
    global_llm = global_doc.get("llm") or {}
    name = _plain(global_llm.get("profile")) or ""
    embed_name = _plain(global_llm.get("embed_profile")) or ""
    for _path, document in project_files:
        llm = document.get("llm") or {}
        name = _plain(llm.get("profile")) or name
        embed_name = _plain(llm.get("embed_profile")) or embed_name
    profiles = {key: _plain(table) for key, table in (global_llm.get("profiles") or {}).items()}
    if not name and len(profiles) == 1:
        name = next(iter(profiles))
    chat = profiles.get(name)
    if not isinstance(chat, dict):
        return None, None
    if not embed_name and chat.get("provider") == "ollama":
        embed_name = name
    embed = profiles.get(embed_name) if embed_name else None
    return chat, embed if isinstance(embed, dict) else None


def _clean_comments(text: str, *, profile_line: bool) -> tuple[str, int, bool]:
    """Remove the commented lines of the old ``[llm]`` keys; with *profile_line*, add the ``profile`` line.

    Only ``local.toml`` gets the ``profile`` line: ``config.toml`` is in
    git, and a line that only a comment adds there is a change of a valid
    file (``doctor`` reported it).

    Returns the new text, the number of removed lines, and if the commented
    ``profile`` line was added.  A file that has a ``profile`` line (active
    or commented) does not get a second one, thus a second run changes
    nothing.  Only the lines
    of the table ``[llm]`` change (up to the next table header); two blank
    lines that a removal leaves next to each other become one.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_llm = False
    header: int | None = None
    dropped = 0
    has_profile_line = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_llm = stripped == "[llm]"
            if in_llm:
                header = len(out)
        elif in_llm:
            if _OLD_KEY_COMMENT.match(stripped) or stripped in _OLD_TEMPLATE_COMMENTS:
                dropped += 1
                continue
            if re.match(r"^#?\s*profile\s*=", stripped):
                has_profile_line = True
            if stripped == "" and out and out[-1].strip() == "" and dropped:
                continue
        out.append(line)
    added = profile_line and header is not None and not has_profile_line
    if added and header is not None:
        out.insert(header + 1, _PROFILE_COMMENT)
    return "\n".join(out), dropped, added

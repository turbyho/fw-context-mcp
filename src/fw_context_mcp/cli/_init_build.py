"""Compile_commands.json generation + shared-build-config write helpers.

Extracted from ``_init.py`` so ``cmd_init`` stays a thin orchestrator.
Two responsibilities:

1. ``_auto_build_if_possible`` — generate ``compile_commands.json`` when a
   build is feasible, preserving a pre-existing good file on failure.
2. ``_write_build_config_key`` — write team-shared build settings into the
   committed ``config.toml`` without stripping its comments.
"""

from __future__ import annotations

from pathlib import Path


def _write_build_config_key(project_root: Path, key: str, value: str | list[str]) -> None:
    """Write *key* into the ``[build]`` section of the SHARED ``config.toml``.

    Uses line-level editing (mirrors ``_update_local_toml``) so comments,
    blank lines, and other sections in the committed ``config.toml`` are
    preserved.  Unlike ``set_key``, this does NOT round-trip through
    ``tomli_w`` (which strips comments).

    Used only for team-shared build settings (``system``, ``board``,
    ``target``, ``fqbn``, ``keil_project``, ``iar_project``,
    ``source_dirs``).  Machine-specific paths (``python``, ``activate``,
    ``idf_path``) belong in ``local.toml`` via ``_write_build_key`` — mixing
    them here would commit machine paths into the shared config.

    *value* may be a string or a list of strings (written as a TOML array
    — used for ``source_dirs``).
    """
    import re

    config_path = project_root / ".fw-context" / "config.toml"
    if not config_path.exists():
        from ..config.settings import _ensure_project_config

        _ensure_project_config(project_root)

    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)

    in_build = False
    build_start = -1
    build_end = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[build]":
            in_build = True
            build_start = i + 1
            continue
        if in_build and stripped.startswith("[") and stripped != "[build]":
            build_end = i
            break

    if build_start == -1:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("\n[build]\n")
        build_start = len(lines)
        build_end = len(lines)

    value_line = f"{key} = {_format_build_value(value)}\n"
    found = False
    for i in range(build_start, build_end):
        stripped = lines[i].strip()
        # Match only ACTIVE keys — a commented template example
        # (``# board = ""``) is documentation and must be preserved, so it is
        # left untouched and a new active line is inserted instead.
        if re.match(rf"^{re.escape(key)}\s*=", stripped):
            lines[i] = value_line
            found = True
            break
    if not found:
        lines.insert(build_end, value_line)

    config_path.write_text("".join(lines), encoding="utf-8")


def _toml_str(value: str) -> str:
    """Escape *value* as a TOML basic string.

    Backslashes and double quotes must be escaped — otherwise a path or
    board name containing them produces an invalid ``config.toml``.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _format_build_value(value: str | list[str]) -> str:
    """Format a build value as a TOML value (string or array of strings).

    ``source_dirs`` is written as a TOML array; scalar keys (``system``,
    ``board``, ...) as basic strings.  Values are TOML-escaped so a path or
    board name containing backslashes or quotes stays valid in the file.
    """
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_str(v) for v in value) + "]"
    return _toml_str(value)


def _count_cc_entries(cc_path: Path) -> int:
    """Count entries in a compile_commands.json (0 on parse error).

    A partial or corrupt file from a failed build must not abort init —
    the count is only cosmetic, so 0 is returned instead of raising.
    """
    import json

    try:
        return len(json.loads(cc_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return 0


def _remove_partial_cc(cc: Path) -> None:
    """Delete a compile_commands.json that a failed build just created.

    Only reachable when *cc* did not exist before the build — the early
    return in ``_auto_build_if_possible`` guarantees a pre-existing good
    file is never rebuilt, so this unlink removes only a partial result
    left behind by the failed build.
    """
    try:
        if cc.exists():
            cc.unlink()
    except OSError:
        pass


def _auto_build_if_possible(
    project_root: Path,
    build_system: str | None,
    cfg,
) -> tuple[bool, Path | None, str | None]:
    """Attempt to generate compile_commands.json. Returns (ok, path, error).

    Never raises — catches ``RuntimeError``, ``KeyboardInterrupt``,
    ``FileNotFoundError``, and ``OSError`` internally.

    Thin wrapper over ``generate_compile_commands`` — the same
    "reuse cc.json, build if missing, catch RuntimeError" pattern that
    ``cli/_index.py:_resolve_compile_commands`` already uses.

    A pre-existing cc.json is reused (never rebuilt) and preserved on a
    failed build: only a file that did not exist before the failed build is
    deleted.  ``cc_path`` resolves against ``cfg.index.compile_commands``
    (identical to ``_resolve_compile_commands`` — not hardcoded).
    """
    from ..indexer.build import check_completeness, generate_compile_commands

    cc = cfg.index.compile_commands
    if not cc.is_absolute():
        cc = (project_root / cc).resolve()

    if cc.exists():
        print(f"  [ok] compile_commands.json already exists ({_count_cc_entries(cc)} entries)")
        return True, cc, None

    print("  [build] Generating compile_commands.json...")
    try:
        result_path = generate_compile_commands(project_root, cfg.build)
    except KeyboardInterrupt:
        _remove_partial_cc(cc)
        return False, None, "build interrupted (Ctrl-C)"
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        _remove_partial_cc(cc)
        print(
            "  [warn] the failed build may have left a partial compile_commands.json "
            "in a build-system output directory (e.g. build/) — remove it before re-running"
        )
        return False, None, str(exc)

    path = Path(result_path)
    if not path.exists():
        return False, None, "compile_commands.json was not generated"

    for warning in check_completeness(path, project_root):
        print(f"  [warn] {warning}")

    print(f"  [ok] compile_commands.json created ({_count_cc_entries(path)} entries)")
    return True, path, None

"""Decide what the linker script pass does in one index run.

The backend gives one of three answers, see `builders.link_record`:

* None — the backend does not know the link of this build.  The pass keeps
  every row that an earlier run wrote for this `config_hash`.  Measured on
  a Zephyr sysbuild project: an index of the copy of the database in
  `.fw-context/build/` finds no `build.ninja` next to it, and a removal
  there deleted a correct memory map on each `--build`.
* A record with no script — the backend knows the link, and the link names
  no script.  The pass removes the rows of an earlier run, because an old
  map is a wrong map for this build.
* A record with scripts — the pass reads them and stores what they define.

The third case can also do nothing.  The pass deletes and inserts each
`ld:` symbol, and the delete removes the embedding of the symbol too.
Measured on an ESP32 project: 1805 symbols, about one minute of embedding
work on each run that changed nothing.  Thus the pass keeps a fingerprint of
its inputs in a state file next to the database, and it does not run when
the fingerprint and the rows in the database did not change.

The state file is not in the database, so that the schema stays as it is.
It is only a hint: the pass compares its counts with the database before it
trusts it, thus a database that was reset or replaced gives a full run.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fw_context_mcp.utils import clear_dead_temporaries, owner_token

from .builders._linker import LinkRecord
from .linker_script import LinkerResult, LinkerScript, parse, store_scripts

log = logging.getLogger(__name__)

def code_files() -> list[Path]:
    """Return the source files of the code that makes the rows of the pass.

    That code is part of the fingerprint.  A change to it can change the
    rows for the same scripts, and a hand-kept version number is easy to
    forget.  The code is the reader, the pass, and each function that
    `store_scripts` gives a row to: the path and the USR come from
    `ops._normalize_file_path`, `is_project` from `sdk_detect._path_matches`,
    and the rows from the `db` writers.

    The list comes from the functions, not from file names, thus a function
    that moves to another module takes its new file with it.
    """
    from . import linker_script, ops, sdk_detect
    from .db import insert_symbols_batch, replace_memory_regions, set_entry_point, upsert_file

    functions = (
        store_scripts, linker_script.parse, ops._normalize_file_path, sdk_detect._path_matches,
        insert_symbols_batch, replace_memory_regions, set_entry_point, upsert_file,
    )
    files = {Path(__file__).resolve()}
    files.update(Path(inspect.getfile(function)).resolve() for function in functions)
    return sorted(files)


@dataclass(frozen=True)
class PassState:
    """What the last full run of the pass stored for one `config_hash`."""

    fingerprint: str
    files: int
    symbols: int
    regions: int
    entry: str
    paths: list[str]


def state_path(db_dir: Path, config_hash: str) -> Path:
    """Return the state file of the pass for *config_hash* in *db_dir*."""
    return db_dir / f"linker_pass.{config_hash}.json"


def _code_digest() -> str:
    digest = hashlib.sha256()
    for path in code_files():
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fingerprint(
    conn: sqlite3.Connection,
    config_hash: str,
    scripts: list[LinkerScript],
    project_root: Path,
    vendor_patterns: list[str],
    project_patterns: list[str],
    defsyms: dict[str, str | None],
) -> str | None:
    """Return a digest of every input that changes the rows of the pass, or None.

    The inputs are the code, the project root, the vendor and project
    patterns, the `--defsym` definitions that the scripts use, in order,
    the path and content of each script, and the script names that a C or
    an assembly unit defines.  The last input matters because
    `store_scripts` does not store a name that the compiled code defines.
    The `ld:` rows of the earlier run are excluded from that query, because
    they define every name of the scripts.

    None when a file cannot be read: the code of a zipimport or frozen
    install, or a script that went away after the parse.  Without every
    input the pass cannot know that nothing changed, thus it runs, and the
    index run does not stop.
    """
    try:
        code = _code_digest()
        contents = [script.path.read_bytes() for script in scripts]
    except OSError as exc:
        log.info("linker script: no fingerprint, thus the pass runs: %s", exc)
        return None
    texts = [content.decode("utf-8", errors="replace") for content in contents]
    digest = hashlib.sha256()
    digest.update(code.encode())
    digest.update(json.dumps([
        str(project_root), vendor_patterns, project_patterns, _used_defsyms(texts, defsyms),
    ]).encode())
    names: set[str] = set()
    for script, content in zip(scripts, contents, strict=True):
        digest.update(str(script.path).encode())
        digest.update(hashlib.sha256(content).hexdigest().encode())
        names.update(symbol.name for symbol in script.symbols)
    digest.update(json.dumps(sorted(_defined_by_units(conn, config_hash, sorted(names)))).encode())
    return digest.hexdigest()


def _used_defsyms(texts: list[str], defsyms: dict[str, str | None]) -> list[tuple[str, str | None]]:
    """Return the `--defsym` definitions that can change the rows, in order.

    A definition changes the rows only through a script: a region value, a
    `PROVIDE` that it replaces, or an assignment that blocks it.  Each needs
    the name in the text of a script, or in the expression of another
    definition that a script uses.  WHY filter: the Teensy platform passes
    `--defsym=__rtc_localtime=$UNIX_TIME`, which changes on every build.  No
    script names it, and with it in the fingerprint the pass never skips.

    The test is a plain substring test.  It can keep a definition that a
    script does not use, which costs one run, and never drops one that a
    script uses.
    """
    used = {name for name in defsyms if any(name in text for text in texts)}
    pending = list(used)
    while pending:
        expression = defsyms[pending.pop()] or ""
        for name in defsyms:
            if name not in used and name in expression:
                used.add(name)
                pending.append(name)
    return [(name, expression) for name, expression in defsyms.items() if name in used]


def _defined_by_units(conn: sqlite3.Connection, config_hash: str, names: list[str]) -> set[str]:
    """Return the names of *names* that a row outside the `ld:` namespace defines."""
    found: set[str] = set()
    for start in range(0, len(names), 500):
        chunk = names[start:start + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT DISTINCT name FROM symbols "  # noqa: S608
            f"WHERE config_hash=? AND is_definition=1 AND usr NOT LIKE 'ld:%' "
            f"AND name IN ({marks})",
            (config_hash, *chunk),
        )
        found.update(row[0] for row in rows)
    return found


def read_state(path: Path) -> PassState | None:
    """Return the state in *path*, or None when it is missing or unreadable.

    Each field must have its exact type, and no conversion is made.  A
    conversion would accept a wrong file: `list("abc")` gives three paths,
    and the coverage purge then deletes the rows of the real script as rows
    of a file that no unit covers.  None gives a full run, which is always
    correct.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.debug("cannot read %s", path, exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None
    texts = (raw.get("fingerprint"), raw.get("entry"))
    counts = (raw.get("files"), raw.get("symbols"), raw.get("regions"))
    paths = raw.get("paths")
    if not (
        all(isinstance(value, str) for value in texts)
        # A bool is an int in Python, and no count is a bool.
        and all(isinstance(value, int) and not isinstance(value, bool) for value in counts)
        and isinstance(paths, list)
        and all(isinstance(item, str) for item in paths)
    ):
        log.debug("%s has a field of a wrong type", path)
        return None
    return PassState(
        fingerprint=raw["fingerprint"],
        files=raw["files"],
        symbols=raw["symbols"],
        regions=raw["regions"],
        entry=raw["entry"],
        paths=list(paths),
    )


def write_state(path: Path, state: PassState) -> None:
    """Write *state* to *path* atomically.  A failure costs one full run later.

    The temporary files that SIGKILL left, of every `config_hash`, go first:
    each holds the owner token in its name, thus no later write reuses it.
    """
    temporary = path.with_name(f".{path.stem}.{owner_token()}{path.suffix}")
    clear_dead_temporaries(path.parent, "linker_pass", path.suffix)
    try:
        temporary.write_text(json.dumps(state.__dict__), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        log.warning("Cannot write %s: %s", path, exc)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.debug("cannot remove %s", temporary, exc_info=True)


def remove_state(path: Path) -> None:
    """Remove the state file, so that the next run does the full pass."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Cannot remove %s: %s", path, exc)


def rows_match(conn: sqlite3.Connection, config_hash: str, state: PassState) -> bool:
    """Say if the database still holds the rows that *state* describes."""
    symbols = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND usr LIKE 'ld:%'",
        (config_hash,),
    ).fetchone()[0]
    regions = conn.execute(
        "SELECT COUNT(*) FROM memory_regions WHERE config_hash=?", (config_hash,),
    ).fetchone()[0]
    row = conn.execute(
        "SELECT entry_point FROM build_configs WHERE config_hash=?", (config_hash,),
    ).fetchone()
    entry = row[0] if row else None
    return (symbols, regions, entry) == (state.symbols, state.regions, state.entry)


def parse_scripts(record: LinkRecord) -> list[LinkerScript]:
    """Return the scripts of *record* that the reader can read."""
    return [script for script in (parse(path) for path in record.scripts) if script is not None]


def result_from_state(state: PassState) -> LinkerResult:
    """Return a result that describes the rows of the run that wrote *state*.

    The coverage purge of the runner needs the paths: without them it counts
    each script as a file that no unit covers, and it deletes its rows.
    """
    return LinkerResult(
        files=state.files,
        symbols=state.symbols,
        entry=state.entry,
        regions=state.regions,
        paths=set(state.paths),
    )


def store(
    conn: sqlite3.Connection,
    config_hash: str,
    scripts: list[LinkerScript],
    project_root: Path,
    vendor_patterns: list[str],
    project_patterns: list[str],
    defsyms: dict[str, str | None],
) -> LinkerResult:
    """Store *scripts*.  An empty list removes the rows of an earlier run."""
    return store_scripts(
        conn, config_hash, [script.path for script in scripts], project_root,
        vendor_patterns, project_patterns, defsyms, parsed=scripts,
    )

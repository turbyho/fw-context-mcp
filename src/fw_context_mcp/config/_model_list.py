"""The list of the models of Ollama in the global config: ``[llm.models]`` (chat) and ``[llm.embed_models]``.

The models change with time, and the user adds models of their own
(decision of the user, 2026-10-21).  Thus the list is not a constant of
the code: ``data/ollama_models.toml`` of each release holds the entries of
fw-context, and ``fw-context init`` and ``fw-context doctor --fix`` copy
them into the global config, where the user reads and changes them.

An entry of fw-context has ``source = "fw-context <version>"`` and the
``fingerprint`` of the values that fw-context wrote.  The fingerprint
tells an entry that the user changed from one that a new release only
has to update:

- an entry that is not in the config is added;
- an entry of fw-context that the user did not change gets the values of
  the new release;
- an entry that the user changed stays, and the report names the new
  value;
- an entry without ``source`` is of the user, and fw-context never
  changes it;
- an entry of fw-context that a new release does not have is removed
  when the user did not change it, else it stays and the report names it.

A removed entry comes back with the next sync: a removal cannot be told
from an entry that is not added yet.  ``enabled = false`` takes a model
out of each recommendation, and fw-context keeps that key.  Thus a removal
is also the way back to the entry of fw-context, for an entry that the
user changed: the next sync adds it with the values of the release.

A wrong type or an unknown key gives a warning, never an error: the list
selects no backend (rule "the strictness follows the harm").
"""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import tomlkit

#: The table of the global config for each section of ``data/ollama_models.toml``.
TABLES = {"chat": "models", "embedding": "embed_models"}

#: The keys of an entry, with their converter of ``settings._expected_type``.
_FIELDS: dict[str, dict[str, str]] = {
    "models": {"quality": "float", "quality_source": "str", "weights_gb": "float", "moe": "bool",
               "remote": "bool", "usd_per_1k_symbols": "float", "usd_per_1k_symbols_off_peak": "float",
               "price_source": "str", "enabled": "bool",
               "source": "str", "fingerprint": "str"},
    "embed_models": {"quality": "float", "quality_source": "str", "weights_gb": "float", "dimension": "int",
                     "remote": "bool", "provider": "str", "usd_per_mtok": "float", "price_source": "str",
                     "enabled": "bool", "source": "str", "fingerprint": "str"},
}

#: The keys that are not values of fw-context: the fingerprint leaves them
#: out, and a sync keeps ``enabled`` of the user.
_NOT_VALUES = ("source", "fingerprint", "enabled")


def shipped() -> dict[str, dict[str, dict]]:
    """Return the entries of this release: the table of the global config → tag → values."""
    text = resources.files("fw_context_mcp.data").joinpath("ollama_models.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    return {table: dict(data.get(section) or {}) for section, table in TABLES.items()}


def fingerprint(values: dict) -> str:
    """Return the fingerprint of the values of an entry: the keys that fw-context writes, in a fixed order."""
    kept = {key: values[key] for key in sorted(values) if key not in _NOT_VALUES}
    # default=str: a TOML date or time of the user is no JSON value.
    return hashlib.sha256(json.dumps(kept, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class SyncReport:
    """What a sync of the list changed, or would change: a label ``<table> <tag>`` for each entry."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Entries that the user changed, with a new value of fw-context, or
    #: that the release does not have any more: the sync keeps them.
    kept: list[str] = field(default_factory=list)

    #: Tables that the sync cannot change, with the reason: the user
    #: writes them in another shape.
    skipped: list[str] = field(default_factory=list)
    #: The values of each entry that the sync adds or updates, for the
    #: check of the result (``_misplaced``): label → values.
    written: dict[str, dict] = field(default_factory=dict, repr=False)

    @property
    def changes(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def lines(self) -> list[str]:
        return ([f"added {label}" for label in self.added] + [f"updated {label}" for label in self.updated]
                + [f"removed {label}" for label in self.removed]
                + [f"kept {label} (changed by the user)" for label in self.kept]
                + [f"not changed: {label}" for label in self.skipped])


def sync(document: tomlkit.TOMLDocument, *, version: str) -> SyncReport:
    """Bring the list of *document* (a global config) to the entries of this release; see the module.

    The caller checks the result (``sync_global_config``): tomlkit can
    write an entry outside [llm] for a shape that the user wrote by hand.

    Raises:
        ValueError: tomlkit cannot put a table into a part of *document*
            (an inline table).
    """
    report = SyncReport()
    llm = document.get("llm")
    if llm is None:
        llm = tomlkit.table()
        document["llm"] = llm
    if not isinstance(llm, dict):
        report.skipped.append("[llm] is not a table")
        return report
    for table, entries in shipped().items():
        current = llm.get(table)
        if current is None:
            current = tomlkit.table(is_super_table=True)
            llm[table] = current
        if not isinstance(current, dict):
            report.skipped.append(f'[llm.{table}] is not a table of [llm.{table}."<tag>"] entries')
            continue
        for tag, values in entries.items():
            label = f"{table} {tag}"
            entry = current.get(tag)
            if entry is None:
                current[tag] = _entry(values, version)
                report.added.append(label)
                report.written[label] = _written(values, version)
                continue
            if not isinstance(entry, dict) or "source" not in entry:
                continue  # of the user
            plain = _plain(entry)
            if plain.get("fingerprint") != fingerprint(plain):
                # Changed by the user: it stays.  The report names a value
                # of this release only when the release changed it since
                # fw-context wrote the entry.
                if fingerprint(values) != plain.get("fingerprint"):
                    report.kept.append(f"{label}: a new value of fw-context is {_short(values)}")
                continue
            if fingerprint(values) != plain["fingerprint"]:
                _update(entry, values, version)
                report.updated.append(label)
                report.written[label] = _written(values, version, enabled=plain.get("enabled"))
        for tag in [tag for tag in current if tag not in entries]:
            entry = current[tag]
            if not isinstance(entry, dict) or "source" not in entry:
                continue
            plain = _plain(entry)
            if plain.get("fingerprint") == fingerprint(plain):
                del current[tag]
                report.removed.append(f"{table} {tag}")
            else:
                report.kept.append(f"{table} {tag}: fw-context does not list it any more")
    return report


def _written(values: dict, version: str, *, enabled: object = None) -> dict:
    """Return the values that an entry must hold after the sync."""
    expected = {**values, "source": f"fw-context {version}", "fingerprint": fingerprint(values)}
    if enabled is not None:
        expected["enabled"] = enabled
    return expected


def _entry(values: dict, version: str) -> tomlkit.items.Table:
    entry = tomlkit.table()
    _update(entry, values, version)
    return entry


def _update(entry: dict, values: dict, version: str) -> None:
    """Give *entry* the values of this release in place: its comments and ``enabled`` of the user stay.

    An entry whose fingerprint matches holds only keys of fw-context and
    ``enabled``, thus a key that the release does not have any more goes.
    """
    # Not source and fingerprint: they are written again below, and a
    # delete from a dotted table raises in tomlkit.
    for key in [key for key in entry if key not in values and key not in _NOT_VALUES]:
        del entry[key]
    for key, value in values.items():
        entry[key] = value
    entry["source"] = f"fw-context {version}"
    entry["fingerprint"] = fingerprint(values)


def _plain(entry: dict) -> dict:
    """Return the values of a tomlkit table as plain Python values."""
    unwrap = getattr(entry, "unwrap", None)
    return dict(unwrap() if unwrap else entry)


def _short(values: dict) -> str:
    named = [f"{key} = {values[key]}" for key in ("quality", "weights_gb", "dimension", "usd_per_1k_symbols",
                                                   "usd_per_1k_symbols_off_peak") if key in values]
    return ", ".join(named) or "of other keys"


def _misplaced(before: dict, document: tomlkit.TOMLDocument, report: SyncReport) -> str | None:
    """Return why the synced *document* is not right, or None.

    Right means: valid TOML; each entry that the report adds or updates is
    in its table; and each other value is the value of *before* (the file
    as it was).  WHY a check of the result, not of each shape: the shapes
    that a user writes by hand are many.  tomlkit writes a new entry of a
    dotted table at the top level (``[models."a:1b"]``), and after a dotted
    key at the top level (``llm.profile = "local"``) it puts the new tables
    before the next lines of the file, which then belong to the last new
    entry: a value of the user moves and loses its meaning.
    """
    try:
        after = tomllib.loads(tomlkit.dumps(document))
    except tomllib.TOMLDecodeError as e:
        return f"the result is not TOML ({e})"
    llm = after.get("llm")
    for label in report.added + report.updated:
        table, tag = label.split(" ", 1)
        entries = llm.get(table) if isinstance(llm, dict) else None
        if not isinstance(entries, dict) or tag not in entries:
            return f"{label} is not under [llm.{table}]"
        if entries[tag] != report.written[label]:
            # tomlkit wrote a part of the entry elsewhere (a dotted table).
            return f"{label} does not hold the values that fw-context wrote"
    named = report.added + report.updated + report.removed
    if _without(before, named) != _without(after, named):
        return "a value outside the entries of fw-context changes"
    return None


def _without(data: dict, labels: list[str]) -> dict:
    """Return *data* without the entries of *labels*, and without a list table that is then empty."""
    data = copy.deepcopy(data)
    llm = data.get("llm")
    if not isinstance(llm, dict):
        return data
    for label in labels:
        table, tag = label.split(" ", 1)
        entries = llm.get(table)
        if isinstance(entries, dict):
            entries.pop(tag, None)
    for table in TABLES.values():
        if llm.get(table) == {}:
            del llm[table]
    return data


def sync_global_config(path: Path, *, version: str, dry_run: bool = False) -> SyncReport:
    """Sync the list of the global config at *path*, and write it unless *dry_run*; comments stay.

    A result that is not right (``_misplaced``), or a shape that tomlkit
    cannot change, writes nothing: the report names it in ``skipped``.

    Raises:
        OSError: The file cannot be read or written.
        tomlkit.exceptions.ParseError: The file is not valid TOML: it gets
            no more text.
    """
    from ._toml_editor import _atomic_write

    text = path.read_text(encoding="utf-8")
    document = tomlkit.parse(text)
    how = 'write [llm] as a table with a header, and each entry as a [llm.models."<tag>"] table'
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return SyncReport(skipped=[f"the file is not TOML for Python ({e})"])
    try:
        report = sync(document, version=version)
    except (ValueError, TypeError, IndexError, KeyError) as e:
        # tomlkit cannot change some shapes: a table into an inline table
        # (ValueError), a key out of a dotted table (IndexError).
        return SyncReport(skipped=[f"tomlkit cannot change this shape ({type(e).__name__}: {e}): {how}"])
    if report.changes:
        problem = _misplaced(before, document, report)
        if problem is not None:
            return SyncReport(skipped=[f"{problem}: {how}"])
        if not dry_run:
            _atomic_write(path.resolve(), tomlkit.dumps(document))
    return report


def problems(llm: dict, *, global_file: bool) -> list:
    """Return the values of the list in *llm* that fw-context cannot use, as warnings (``settings.ValueProblem``).

    Each problem is not critical: a warning, not a ConfigError, because the
    list selects no backend.  ``doctor --fix --only config`` can remove a
    value of the wrong type, as a tuning key.  An unknown key is not
    removable: it is likely a typo of the user, whose value the user wants.
    """
    from .settings import ValueProblem, _expected_type, _toml_type

    found: list = []
    for table, fields in _FIELDS.items():
        if table not in llm:
            continue
        keys: tuple[str | int, ...] = ("llm", table)
        label = f"[llm.{table}]"
        value = llm[table]
        if not global_file:
            # Not removable: a committed config.toml of a project would lose
            # it without a word; the user moves it.
            found.append(ValueProblem(keys, f"{label} is in the global config only", removable=False,
                                      critical=False))
            continue
        if not isinstance(value, dict):
            # Not removable: an array of tables is a list of the user in
            # another shape.
            found.append(ValueProblem(keys, f"{label} must be a table, not {_toml_type(value)}", removable=False,
                                      critical=False))
            continue
        for tag, entry in value.items():
            entry_label = f'[llm.{table}."{tag}"]'
            if not isinstance(entry, dict):
                found.append(ValueProblem((*keys, tag), f"{entry_label} must be a table, not {_toml_type(entry)}",
                                          critical=False))
                continue
            for key, item in entry.items():
                conv = fields.get(key)
                if conv is None:
                    found.append(ValueProblem((*keys, tag, key), f"{entry_label} {key} is not a key of the list "
                                              f"(keys: {', '.join(sorted(fields))})", removable=False,
                                              critical=False))
                    continue
                expected = _expected_type(item, conv, strict=True)
                if expected is not None:
                    found.append(ValueProblem((*keys, tag, key), f"{entry_label} {key} must be {expected}, not "
                                              f"{_toml_type(item, conv, strict=True)}", critical=False))
    return found

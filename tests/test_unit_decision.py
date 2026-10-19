"""The decision of the index run about each unit: ``decide_units``.

A unit is unchanged only when the manifest holds an entry for its listing
(the same flags hash), the source and each header have the hash of that
entry, and no other listing of the same file must be parsed.  No file time
takes part.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.indexer._unit_processor import decide_units
from fw_context_mcp.indexer.config_hash import DEFAULT_TRANSIENT_DEFINES, compute_flags_hash
from fw_context_mcp.indexer.db import FileHashRecord, number_listings


def _unit(root: Path, rel: str, define: str = "-DA"):
    src = root / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        src.write_text("int x;\n", encoding="utf-8")
    return SimpleNamespace(
        file=src,
        raw_entry={"file": rel, "directory": str(root), "arguments": ["gcc", define, "-c", rel]},
    )


def _flags(unit) -> str:
    return compute_flags_hash(unit.raw_entry, transient_defines=DEFAULT_TRANSIENT_DEFINES)


def _entry(root: Path, unit, **overrides) -> dict:
    entry = {
        "file": str(unit.file.relative_to(root)),
        "source_hash": hashlib.sha256(unit.file.read_bytes()).hexdigest(),
        "flags_hash": _flags(unit),
        "headers": [],
    }
    entry.update(overrides)
    return entry


def _rows(units) -> dict:
    """One ``files`` row for each file of *units*, as get_file_hashes gives them."""
    return {str(u.file.relative_to(u.file.parents[1])): FileHashRecord(1, 0.0, "", "") for u in units}


def _key(root: Path, unit) -> tuple[str, str]:
    return str(unit.file.relative_to(root)), _flags(unit)


def _decide(root: Path, units, manifest, *, existing=None, force=False, stale=frozenset(), recorded=None):
    """Decide about *units*; by default the claims of each unit are recorded.

    The claims are a separate condition (see the test of it below), thus the
    other tests give them, and test only the hashes.
    """
    lookup = None
    if manifest is not None:
        lookup = {}
        for entry in manifest:
            lookup.setdefault(entry["file"], []).append(entry)
    return decide_units(
        units, root, existing if existing is not None else _rows(units), lookup, stale,
        force=force, hash_cache={}, header_table={}, transient_defines=DEFAULT_TRANSIENT_DEFINES,
        claims_recorded=(
            recorded if recorded is not None
            else set(number_listings(_key(root, u) for u in units))
        ),
    )


@pytest.fixture(autouse=True)
def _no_force_env(monkeypatch):
    monkeypatch.delenv("FW_CONTEXT_FORCE_REFINDEX", raising=False)


def test_a_unit_with_its_entry_is_unchanged(tmp_path: Path):
    unit = _unit(tmp_path, "src/a.c")

    [decision] = _decide(tmp_path, [unit], [_entry(tmp_path, unit)])

    assert decision.unchanged
    assert decision.hashes == (_entry(tmp_path, unit)["source_hash"], _flags(unit))


def test_a_unit_without_recorded_claims_is_parsed(tmp_path: Path):
    """An index from an older version knows no claims: the run must record them."""
    unit = _unit(tmp_path, "src/a.c")

    [decision] = _decide(tmp_path, [unit], [_entry(tmp_path, unit)], recorded=set())

    assert not decision.unchanged


def test_the_decision_names_the_listing_and_its_includes(tmp_path: Path):
    """The includes hold the headers of the entry and the source file itself."""
    unit = _unit(tmp_path, "src/a.c")

    [decision] = _decide(tmp_path, [unit], [_entry(tmp_path, unit, headers=["src/a.h"])])

    assert decision.key == _key(tmp_path, unit)
    assert decision.includes == {"src/a.h", "src/a.c"}


def test_two_listings_with_the_same_flags_get_two_keys(tmp_path: Path):
    """The second listing claims nothing new; its claims must not replace the first ones."""
    unit = _unit(tmp_path, "src/a.c")

    first, second = _decide(tmp_path, [unit, unit], [_entry(tmp_path, unit)])

    assert first.key == _key(tmp_path, unit)
    assert second.key == (first.key[0], first.key[1] + "#0002")


def test_without_a_manifest_every_unit_is_parsed(tmp_path: Path):
    unit = _unit(tmp_path, "src/a.c")

    assert [d.unchanged for d in _decide(tmp_path, [unit], None)] == [False]


def test_a_unit_without_a_row_is_parsed(tmp_path: Path):
    unit = _unit(tmp_path, "src/a.c")

    assert [d.unchanged for d in _decide(tmp_path, [unit], [_entry(tmp_path, unit)], existing={})] == [False]


@pytest.mark.parametrize("key", ["flags_hash", "source_hash"], ids=["no flags hash", "preliminary entry"])
def test_an_entry_that_proves_nothing_parses_the_unit(tmp_path: Path, key: str):
    """An entry of an old manifest has no flags hash, and a preliminary one no source hash."""
    unit = _unit(tmp_path, "src/a.c")
    entry = _entry(tmp_path, unit)
    del entry[key]

    assert [d.unchanged for d in _decide(tmp_path, [unit], [entry])] == [False]


def test_a_source_that_the_disk_lost_is_parsed(tmp_path: Path):
    """The build still lists it.  The parse fails and reports it, as it must."""
    unit = _unit(tmp_path, "src/a.c")
    entry = _entry(tmp_path, unit)
    unit.file.unlink()

    [decision] = _decide(tmp_path, [unit], [entry])

    assert not decision.unchanged
    assert decision.hashes[0] == ""


def test_an_edit_with_the_old_time_is_seen(tmp_path: Path):
    unit = _unit(tmp_path, "src/a.c")
    entry = _entry(tmp_path, unit)
    before = unit.file.stat()
    unit.file.write_text("int y;\n", encoding="utf-8")
    os.utime(unit.file, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert [d.unchanged for d in _decide(tmp_path, [unit], [entry])] == [False]


def test_a_header_stale_unit_is_parsed(tmp_path: Path):
    unit = _unit(tmp_path, "src/a.c")

    decisions = _decide(tmp_path, [unit], [_entry(tmp_path, unit)], stale=frozenset({"src/a.c"}))

    assert [d.unchanged for d in decisions] == [False]


def test_force_parses_every_unit(tmp_path: Path, monkeypatch):
    unit = _unit(tmp_path, "src/a.c")
    entry = _entry(tmp_path, unit)

    assert [d.unchanged for d in _decide(tmp_path, [unit], [entry], force=True)] == [False]
    monkeypatch.setenv("FW_CONTEXT_FORCE_REFINDEX", "1")
    assert [d.unchanged for d in _decide(tmp_path, [unit], [entry])] == [False]


def test_each_listing_finds_the_entry_of_its_flags(tmp_path: Path):
    a = _unit(tmp_path, "misc/empty.c", "-DA")
    b = _unit(tmp_path, "misc/empty.c", "-DB")
    entries = [_entry(tmp_path, b), _entry(tmp_path, a)]

    assert [d.unchanged for d in _decide(tmp_path, [a, a, b], entries)] == [True, True, True]


def test_a_changed_listing_parses_every_listing_of_the_file(tmp_path: Path):
    """The index holds one set of rows for a file: the rows of the listing parsed last.

    The flags of the second listing changed.  A run that parsed only that
    listing left rows that depend on the order of the listings.
    """
    a = _unit(tmp_path, "misc/empty.c", "-DA")
    b = _unit(tmp_path, "misc/empty.c", "-DB")
    other = _unit(tmp_path, "src/main.c", "-DA")
    entries = [_entry(tmp_path, a), _entry(tmp_path, b), _entry(tmp_path, other)]
    c = _unit(tmp_path, "misc/empty.c", "-DC")

    decisions = _decide(tmp_path, [a, c, other], entries)

    assert [d.unchanged for d in decisions] == [False, False, True]


def test_the_source_is_read_once(tmp_path: Path, monkeypatch):
    """The decision hashes each source once; check_tu_staleness takes that hash."""
    import fw_context_mcp.indexer._unit_processor as unit_processor
    import fw_context_mcp.indexer.manifest as manifest_mod

    unit = _unit(tmp_path, "src/a.c")
    entry = _entry(tmp_path, unit)
    reads: list[Path] = []
    original = unit_processor.compute_source_hash

    def counting(path):
        reads.append(Path(path))
        return original(path)

    monkeypatch.setattr(unit_processor, "compute_source_hash", counting)
    monkeypatch.setattr(manifest_mod, "compute_source_hash", counting)

    [decision] = _decide(tmp_path, [unit], [entry])

    assert decision.unchanged
    assert len(reads) == 1

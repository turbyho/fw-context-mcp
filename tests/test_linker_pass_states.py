"""The three answers of a backend, and the pass that does not run again.

`runner._store_linker_scripts` gets one of three answers from
`builders.link_record`: not known, a link with no script, or a link with
scripts.  Each answer does something else to the rows of an earlier run,
and a wrong choice either keeps an old memory map or deletes a correct one.
Both happened: a removal on every empty answer deleted the map of a Zephyr
sysbuild project on each `--build`.

The pass also skips a run whose inputs did not change, because each store
deletes the embeddings of the `ld:` rows.  Measured on an ESP32 project:
1805 symbols, about one minute of embedding work on each run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fw_context_mcp.indexer import _linker_pass, runner
from fw_context_mcp.indexer import builders as builders_module
from fw_context_mcp.indexer.builders._linker import LinkRecord
from fw_context_mcp.indexer.db import (
    get_memory_regions,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)

SCRIPT = """\
MEMORY
{
    RAM (rwx) : ORIGIN = 0x20000000, LENGTH = LD_RAM
}
ENTRY(Reset_Handler)
_estack = ORIGIN(RAM) + LENGTH(RAM);
_sdata = 0x10;
"""


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
    yield conn
    conn.close()


@pytest.fixture
def answer(monkeypatch):
    """Let each test set the answer of the backend."""
    state: dict[str, LinkRecord | None] = {"record": None}
    monkeypatch.setattr(builders_module, "link_record", lambda *_a, **_k: state["record"])
    return state


def _run(conn, tmp_path: Path, force: bool = False):
    return runner._store_linker_scripts(
        conn=conn,
        config_hash="ch",
        project_root=tmp_path,
        db_dir=tmp_path,
        compile_commands=tmp_path / "cc.json",
        build_system="platformio",
        variant="",
        units=[],
        vendor_patterns=[],
        project_patterns=[],
        force=force,
    )


def _script(tmp_path: Path, text: str = SCRIPT) -> Path:
    path = tmp_path / "app.ld"
    path.write_text(text, encoding="utf-8")
    return path


def _ld_rows(conn) -> dict[str, int]:
    return dict(conn.execute(
        "SELECT name, id FROM symbols WHERE config_hash='ch' AND usr LIKE 'ld:%'"
    ).fetchall())


def _entry(conn) -> str:
    return conn.execute("SELECT entry_point FROM build_configs WHERE config_hash='ch'").fetchone()[0]


def _embed(conn, symbol_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO embeddings (symbol_id, embedding, model) VALUES (?, ?, 'm')",
            (symbol_id, b"\0" * 4),
        )


def _embedding_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]


class TestThreeAnswers:
    def test_a_record_with_scripts_stores_them(self, db, tmp_path, answer):
        answer["record"] = LinkRecord(scripts=[_script(tmp_path)], defsyms={"LD_RAM": "0x8000"})
        result = _run(db, tmp_path)
        assert result is not None
        assert result.defsym_values == 1
        assert [(r["name"], r["length_value"]) for r in get_memory_regions(db, "ch")] == [("RAM", 0x8000)]
        assert set(_ld_rows(db)) == {"_estack", "_sdata"}
        assert _entry(db) == "Reset_Handler"
        assert _linker_pass.state_path(tmp_path, "ch").is_file()

    def test_not_known_keeps_the_rows(self, db, tmp_path, answer):
        # A Zephyr sysbuild index of the copy of the database finds no
        # build.ninja.  That must not delete a correct map.
        answer["record"] = LinkRecord(scripts=[_script(tmp_path)], defsyms={"LD_RAM": "0x8000"})
        _run(db, tmp_path)
        answer["record"] = None
        result = _run(db, tmp_path)
        assert len(get_memory_regions(db, "ch")) == 1
        assert set(_ld_rows(db)) == {"_estack", "_sdata"}
        assert _entry(db) == "Reset_Handler"
        # The coverage purge gets the paths, or it deletes the rows.
        assert result is not None
        assert result.paths == {"app.ld"}

    def test_not_known_with_no_earlier_run(self, db, tmp_path, answer):
        assert _run(db, tmp_path) is None
        assert get_memory_regions(db, "ch") == []

    def test_a_link_with_no_script_removes_the_rows(self, db, tmp_path, answer):
        # config_hash comes from the compile flags only: a link that now
        # names no script keeps the hash, and the old map is a wrong map.
        answer["record"] = LinkRecord(scripts=[_script(tmp_path)], defsyms={})
        _run(db, tmp_path)
        answer["record"] = LinkRecord(scripts=[], defsyms={})
        assert _run(db, tmp_path) is None
        assert get_memory_regions(db, "ch") == []
        assert _ld_rows(db) == {}
        assert not _entry(db)
        assert not _linker_pass.state_path(tmp_path, "ch").exists()


class TestUnchangedInputs:
    def _first_run(self, db, tmp_path, answer):
        answer["record"] = LinkRecord(scripts=[_script(tmp_path)], defsyms={"LD_RAM": "0x8000"})
        _run(db, tmp_path)
        _embed(db, _ld_rows(db)["_estack"])

    def test_an_unchanged_run_keeps_the_embeddings(self, db, tmp_path, answer, caplog):
        self._first_run(db, tmp_path, answer)
        ids = _ld_rows(db)
        with caplog.at_level("INFO"):
            result = _run(db, tmp_path)
        assert "unchanged since the last run" in caplog.text
        assert _ld_rows(db) == ids
        assert _embedding_count(db) == 1
        assert result is not None
        assert result.paths == {"app.ld"}

    @pytest.mark.parametrize("change", ["script", "defsym", "vendor"])
    def test_a_changed_input_runs_again(self, db, tmp_path, answer, change):
        self._first_run(db, tmp_path, answer)
        vendor: list[str] = []
        if change == "script":
            _script(tmp_path, SCRIPT.replace("0x10", "0x20"))
        elif change == "defsym":
            answer["record"] = LinkRecord(scripts=[tmp_path / "app.ld"], defsyms={"LD_RAM": "0x4000"})
        else:
            vendor = ["%app%"]
        runner._store_linker_scripts(
            conn=db, config_hash="ch", project_root=tmp_path, db_dir=tmp_path,
            compile_commands=tmp_path / "cc.json", build_system="platformio", variant="",
            units=[], vendor_patterns=vendor, project_patterns=[],
        )
        assert _embedding_count(db) == 0

    def test_a_defsym_that_no_script_uses_does_not_run_again(self, db, tmp_path, answer):
        # The Teensy platform passes `--defsym=__rtc_localtime=$UNIX_TIME`,
        # which changes on every build.  No script names it, thus it changes
        # no row, and a full run each time would cost the embeddings.
        self._first_run(db, tmp_path, answer)
        answer["record"] = LinkRecord(
            scripts=[tmp_path / "app.ld"], defsyms={"LD_RAM": "0x8000", "__rtc_localtime": "1700000001"},
        )
        _run(db, tmp_path)
        assert _embedding_count(db) == 1

    def test_a_defsym_that_a_used_defsym_names_runs_again(self, db, tmp_path, answer):
        # LD_RAM uses BASE, thus a change of BASE changes the region value.
        answer["record"] = LinkRecord(
            scripts=[_script(tmp_path)], defsyms={"BASE": "0x4000", "LD_RAM": "BASE * 2"},
        )
        _run(db, tmp_path)
        _embed(db, _ld_rows(db)["_estack"])
        answer["record"] = LinkRecord(
            scripts=[tmp_path / "app.ld"], defsyms={"BASE": "0x2000", "LD_RAM": "BASE * 2"},
        )
        _run(db, tmp_path)
        assert _embedding_count(db) == 0
        assert get_memory_regions(db, "ch")[0]["length_value"] == 0x4000

    def test_code_that_cannot_be_read_gives_a_full_run(self, db, tmp_path, answer, monkeypatch):
        # A zipimport or frozen install has no file to hash.  The pass must not stop
        # the index run, and without a fingerprint it cannot skip.
        self._first_run(db, tmp_path, answer)
        monkeypatch.setattr(_linker_pass, "code_files", lambda: [tmp_path / "gone.py"])
        result = _run(db, tmp_path)
        assert result is not None
        assert _embedding_count(db) == 0
        # The state stays with a fingerprint that never matches: a later
        # "not known" run needs its paths, or the coverage purge deletes
        # the rows of the scripts.
        state = _linker_pass.read_state(_linker_pass.state_path(tmp_path, "ch"))
        assert state is not None
        assert state.fingerprint == ""
        _embed(db, _ld_rows(db)["_estack"])
        _run(db, tmp_path)
        assert _embedding_count(db) == 0
        answer["record"] = None
        assert _run(db, tmp_path).paths == {"app.ld"}

    def test_a_new_c_definition_runs_again(self, db, tmp_path, answer):
        # store_scripts does not store a name that the compiled code
        # defines, thus a new C definition changes the rows.
        self._first_run(db, tmp_path, answer)
        with transaction(db):
            file_id = upsert_file(db, "ch", "main.c", "c")
            insert_symbols_batch(db, [(
                "ch", file_id, "main.c", "_sdata", "c:@_sdata", "_sdata", "_sdata",
                "varglobal", 1, 1, 1, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0,
            )])
        _run(db, tmp_path)
        assert set(_ld_rows(db)) == {"_estack"}

    def test_rows_that_are_gone_run_again(self, db, tmp_path, answer):
        # A reset or a replaced database keeps the state file next to it.
        self._first_run(db, tmp_path, answer)
        with transaction(db):
            db.execute("DELETE FROM memory_regions")
        _run(db, tmp_path)
        assert len(get_memory_regions(db, "ch")) == 1

    def test_a_broken_state_file_runs_again(self, db, tmp_path, answer):
        self._first_run(db, tmp_path, answer)
        _linker_pass.state_path(tmp_path, "ch").write_text("{", encoding="utf-8")
        _run(db, tmp_path)
        assert _embedding_count(db) == 0
        assert _linker_pass.read_state(_linker_pass.state_path(tmp_path, "ch")) is not None

    def test_force_runs_again(self, db, tmp_path, answer):
        # `fw-context index --force` is the documented repair of an old
        # index.  A fix in the code that writes the rows changes neither the
        # scripts nor the counts, thus only --force stores them again.
        self._first_run(db, tmp_path, answer)
        _run(db, tmp_path, force=True)
        assert _embedding_count(db) == 0
        assert set(_ld_rows(db)) == {"_estack", "_sdata"}
        assert _linker_pass.read_state(_linker_pass.state_path(tmp_path, "ch")) is not None

    @pytest.mark.parametrize("field", ["paths", "fingerprint", "entry", "files"])
    def test_a_state_field_of_a_wrong_type_runs_again(self, db, tmp_path, answer, field):
        # A string in `paths` would give one path per character, and the
        # coverage purge would then delete the rows of the real script.
        self._first_run(db, tmp_path, answer)
        path = _linker_pass.state_path(tmp_path, "ch")
        state = json.loads(path.read_text(encoding="utf-8"))
        state[field] = "abc" if field == "paths" else ["abc"]
        path.write_text(json.dumps(state), encoding="utf-8")
        assert _linker_pass.read_state(path) is None
        _run(db, tmp_path)
        assert _embedding_count(db) == 0

    def test_a_state_that_is_not_an_object_runs_again(self, db, tmp_path, answer):
        self._first_run(db, tmp_path, answer)
        path = _linker_pass.state_path(tmp_path, "ch")
        path.write_text("[1, 2]", encoding="utf-8")
        assert _linker_pass.read_state(path) is None


class TestStateCleanup:
    """A state file goes with the build it describes.

    Each `config_hash` has its own file, and every change of the compile
    flags gives a new hash.  Without a cleanup the files of the retired
    builds stay in the index directory for the life of the project.
    """

    @staticmethod
    def _states(db_dir: Path, *hashes: str) -> None:
        for config_hash in hashes:
            _linker_pass.state_path(db_dir, config_hash).write_text("{}", encoding="utf-8")

    def test_retention_removes_the_state_of_an_old_build(self, tmp_path):
        from fw_context_mcp.indexer._postprocess import cleanup_old_builds_multi

        conn = open_db(tmp_path / "index.db")
        try:
            with transaction(conn):
                upsert_project(conn, "pid", "p", str(tmp_path))
                upsert_build_config(conn, "old", "pid", "a.json", variant="v", image="")
                upsert_build_config(conn, "new", "pid", "b.json", variant="v", image="")
            self._states(tmp_path, "old", "new")
            assert cleanup_old_builds_multi(conn, "pid", tmp_path, [("v", "")]) == 1
        finally:
            conn.close()
        assert not _linker_pass.state_path(tmp_path, "old").exists()
        assert _linker_pass.state_path(tmp_path, "new").exists()

    def test_an_orphaned_state_is_removed_at_the_start_of_a_run(self, tmp_path):
        # A deleted build (`fw-context db delete`, a run that stopped before
        # retention) leaves its file.  The cleanup at the start of each run
        # removes every state file whose hash the database does not hold.
        from fw_context_mcp.indexer._embedding import _cleanup_orphaned_cc_artifacts

        conn = open_db(tmp_path / "index.db")
        try:
            with transaction(conn):
                upsert_project(conn, "pid", "p", str(tmp_path))
                upsert_build_config(conn, "kept", "pid", "a.json")
        finally:
            conn.close()
        self._states(tmp_path, "kept", "gone")
        (tmp_path / "linker_pass.txt").write_text("", encoding="utf-8")
        assert _cleanup_orphaned_cc_artifacts(tmp_path / "index.db", "pid") == 1
        assert _linker_pass.state_path(tmp_path, "kept").exists()
        assert not _linker_pass.state_path(tmp_path, "gone").exists()
        assert (tmp_path / "linker_pass.txt").exists()

    def test_a_database_that_cannot_be_read_removes_nothing(self, tmp_path, monkeypatch):
        # A locked or broken database gives no list of active builds.  An
        # empty list would delete the state of every build, and a later
        # "not known" run then loses the paths for the coverage purge.
        from fw_context_mcp.indexer import _embedding

        (tmp_path / "index.db").write_bytes(b"not a database")
        self._states(tmp_path, "a")
        (tmp_path / ("compile_commands." + "a" * 64 + ".json")).write_text("{}", encoding="utf-8")

        def locked(*_args, **_kwargs):
            raise _embedding.sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(_embedding, "open_db", locked)
        assert _embedding._cleanup_orphaned_cc_artifacts(tmp_path / "index.db", "pid") == 0
        assert _linker_pass.state_path(tmp_path, "a").exists()

    def test_no_database_removes_every_state(self, tmp_path):
        # reset_index deletes the database and keeps the directory.
        from fw_context_mcp.indexer._embedding import _cleanup_orphaned_cc_artifacts

        self._states(tmp_path, "a", "b")
        assert _cleanup_orphaned_cc_artifacts(tmp_path / "index.db", "pid") == 2
        assert not list(tmp_path.glob("linker_pass.*.json"))


class TestDeadTemporaryStates:
    def test_a_temporary_state_of_a_stopped_run_goes(self, tmp_path):
        # SIGKILL stops a run between the write and the rename, for any
        # config_hash.  The next write of any state removes it.
        from fw_context_mcp.utils import owner_token

        tag = owner_token().partition("@")[2]
        dead = tmp_path / f".linker_pass.other.2147483646@{tag}.json"
        foreign = tmp_path / ".linker_pass.other.2147483646@another-host-1.json"
        for path in (dead, foreign):
            path.write_text("{}", encoding="utf-8")
        _linker_pass.write_state(
            _linker_pass.state_path(tmp_path, "ch"),
            _linker_pass.PassState(fingerprint="f", files=0, symbols=0, regions=0, entry="", paths=[]),
        )
        assert not dead.exists()
        assert foreign.exists()
        assert _linker_pass.read_state(_linker_pass.state_path(tmp_path, "ch")) is not None


class TestFingerprintCode:
    def test_the_code_that_writes_the_rows_is_in_the_fingerprint(self):
        # `store_scripts` gives each row a path, an `is_project` flag and a
        # USR through these modules.  A fix in one of them changes the rows
        # for the same scripts, thus a run after it must not be skipped.
        from fw_context_mcp.indexer import linker_script, ops, sdk_detect
        from fw_context_mcp.indexer.db import _files, _memory, _symbols

        expected = {
            Path(module.__file__).resolve()
            for module in (_linker_pass, linker_script, ops, sdk_detect, _files, _memory, _symbols)
        }
        assert expected <= set(_linker_pass.code_files())

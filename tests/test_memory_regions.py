"""The memory map: storage, replacement, and reporting.

The map comes from the `MEMORY` command of the linker script and is the only
firmware knowledge of its kind in the index — it is not in
`compile_commands.json` and no translation unit holds it.

The rule these tests exist to pin: **the map belongs to ONE build.**
zbox-ecb-fw-v5 keeps nine configurations in one database, and three of them
on the same chip declare different addresses — `app` at flash 372736,
`mcuboot` at 110592, and `app_flpr` no flash region at all.  A map that
leaked between configurations would put a wrong address in front of a user
who has no way to tell.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer.db import (
    get_entry_point,
    get_memory_regions,
    get_memory_regions_by_config,
    open_db,
    replace_memory_regions,
    set_entry_point,
    transaction,
    upsert_build_config,
    upsert_project,
)
from fw_context_mcp.indexer.linker_script import store_scripts

FLASH = {
    "name": "FLASH", "attributes": "rx",
    "origin": "0x10200", "length": "0xefe00",
    "origin_value": 0x10200, "length_value": 0xEFE00,
    "file_path": "app.ld", "line": 3,
}
RAM = {
    "name": "RAM", "attributes": "rwx",
    "origin": "ORIGIN(FLASH) + 1", "length": "0x40000",
    "origin_value": None, "length_value": 0x40000,
    "file_path": "app.ld", "line": 4,
}


def _db(tmp_path: Path, *hashes: str):
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        for config_hash in hashes or ("ch",):
            upsert_build_config(conn, config_hash, "pid", "cc.json")
    return conn


class TestStorage:
    def test_a_region_round_trips(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                count = replace_memory_regions(conn, "ch", [FLASH, RAM])
            regions = get_memory_regions(conn, "ch")
        finally:
            conn.close()
        assert count == 2
        assert [r["name"] for r in regions] == ["FLASH", "RAM"]
        assert regions[0]["origin_value"] == 0x10200
        assert regions[0]["attributes"] == "rx"

    def test_an_expression_that_names_a_symbol_keeps_no_number(self, tmp_path):
        # The index does not evaluate a symbol, thus the text is the answer.
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                replace_memory_regions(conn, "ch", [RAM])
            region = get_memory_regions(conn, "ch")[0]
        finally:
            conn.close()
        assert region["origin"] == "ORIGIN(FLASH) + 1"
        assert region["origin_value"] is None
        assert region["length_value"] == 0x40000

    def test_the_order_follows_the_file(self, tmp_path):
        # A script lists the regions of a target in the order a reader
        # expects, usually flash before RAM.  Alphabetical would reverse it.
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                replace_memory_regions(conn, "ch", [RAM, FLASH])
            names = [r["name"] for r in get_memory_regions(conn, "ch")]
        finally:
            conn.close()
        assert names == ["FLASH", "RAM"]

    def test_no_region_at_all(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                assert replace_memory_regions(conn, "ch", []) == 0
            assert get_memory_regions(conn, "ch") == []
        finally:
            conn.close()


class TestReplacement:
    def test_a_region_the_script_dropped_is_gone(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                replace_memory_regions(conn, "ch", [FLASH, RAM])
            with transaction(conn):
                replace_memory_regions(conn, "ch", [FLASH])
            names = [r["name"] for r in get_memory_regions(conn, "ch")]
        finally:
            conn.close()
        assert names == ["FLASH"]

    def test_a_moved_region_gets_its_new_address(self, tmp_path):
        # A linker script is a build artifact and changes: measured on v5,
        # a rebuild moved RAM length from 424960 to 474112.
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                replace_memory_regions(conn, "ch", [FLASH])
            moved = dict(FLASH, origin="0x20000", origin_value=0x20000)
            with transaction(conn):
                replace_memory_regions(conn, "ch", [moved])
            region = get_memory_regions(conn, "ch")[0]
        finally:
            conn.close()
        assert region["origin_value"] == 0x20000

    def test_a_repeated_name_keeps_the_first(self, tmp_path):
        # One build can pass several scripts, and the caller puts the script
        # of the real link first.
        conn = _db(tmp_path)
        try:
            second = dict(FLASH, origin="0x99999", origin_value=0x99999)
            with transaction(conn):
                count = replace_memory_regions(conn, "ch", [FLASH, second])
            region = get_memory_regions(conn, "ch")[0]
        finally:
            conn.close()
        assert count == 1
        assert region["origin_value"] == 0x10200

    def test_replacing_twice_does_not_double_the_rows(self, tmp_path):
        conn = _db(tmp_path)
        try:
            for _ in range(3):
                with transaction(conn):
                    replace_memory_regions(conn, "ch", [FLASH, RAM])
            count = conn.execute(
                "SELECT COUNT(*) FROM memory_regions WHERE config_hash='ch'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 2


class TestOneBuildOnly:
    """The rule this whole feature depends on."""

    def test_two_configs_keep_their_own_map(self, tmp_path):
        conn = _db(tmp_path, "app", "mcuboot")
        try:
            with transaction(conn):
                replace_memory_regions(conn, "app", [
                    dict(FLASH, origin="372736", origin_value=372736),
                ])
            with transaction(conn):
                replace_memory_regions(conn, "mcuboot", [
                    dict(FLASH, origin="110592", origin_value=110592),
                ])
            app = get_memory_regions(conn, "app")
            mcuboot = get_memory_regions(conn, "mcuboot")
        finally:
            conn.close()
        assert app[0]["origin_value"] == 372736
        assert mcuboot[0]["origin_value"] == 110592

    def test_replacing_one_config_leaves_the_other(self, tmp_path):
        conn = _db(tmp_path, "app", "mcuboot")
        try:
            with transaction(conn):
                replace_memory_regions(conn, "app", [FLASH, RAM])
            with transaction(conn):
                replace_memory_regions(conn, "mcuboot", [FLASH])
            with transaction(conn):
                replace_memory_regions(conn, "app", [])
            assert get_memory_regions(conn, "app") == []
            assert len(get_memory_regions(conn, "mcuboot")) == 1
        finally:
            conn.close()

    def test_a_config_with_no_flash_region(self, tmp_path):
        # app_flpr on v5: a RISC-V image whose script declares RAM only.
        conn = _db(tmp_path, "app", "app_flpr")
        try:
            with transaction(conn):
                replace_memory_regions(conn, "app", [FLASH, RAM])
            with transaction(conn):
                replace_memory_regions(conn, "app_flpr", [RAM])
            flpr = get_memory_regions(conn, "app_flpr")
        finally:
            conn.close()
        assert [r["name"] for r in flpr] == ["RAM"]

    def test_the_batch_query_groups_by_config(self, tmp_path):
        conn = _db(tmp_path, "a", "b", "c")
        try:
            with transaction(conn):
                replace_memory_regions(conn, "a", [FLASH, RAM])
            with transaction(conn):
                replace_memory_regions(conn, "b", [FLASH])
            grouped = get_memory_regions_by_config(conn, ["a", "b", "c"])
        finally:
            conn.close()
        assert len(grouped["a"]) == 2
        assert len(grouped["b"]) == 1
        assert "c" not in grouped

    def test_the_batch_query_with_no_config(self, tmp_path):
        conn = _db(tmp_path)
        try:
            assert get_memory_regions_by_config(conn, []) == {}
        finally:
            conn.close()


class TestEntryPoint:
    def test_it_round_trips(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                set_entry_point(conn, "ch", "Reset_Handler")
            assert get_entry_point(conn, "ch") == "Reset_Handler"
        finally:
            conn.close()

    def test_the_default_is_empty(self, tmp_path):
        conn = _db(tmp_path)
        try:
            assert get_entry_point(conn, "ch") == ""
        finally:
            conn.close()

    def test_a_build_that_stopped_naming_one(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                set_entry_point(conn, "ch", "Reset_Handler")
            with transaction(conn):
                set_entry_point(conn, "ch", "")
            assert get_entry_point(conn, "ch") == ""
        finally:
            conn.close()

    def test_two_configs_keep_their_own(self, tmp_path):
        conn = _db(tmp_path, "arm", "riscv")
        try:
            with transaction(conn):
                set_entry_point(conn, "arm", "Reset_Handler")
            with transaction(conn):
                set_entry_point(conn, "riscv", "__start")
            assert get_entry_point(conn, "arm") == "Reset_Handler"
            assert get_entry_point(conn, "riscv") == "__start"
        finally:
            conn.close()

    def test_an_unknown_config(self, tmp_path):
        conn = _db(tmp_path)
        try:
            assert get_entry_point(conn, "absent") == ""
        finally:
            conn.close()


class TestThroughTheIndexingPass:
    """`store_scripts` writes the map, not only the symbols."""

    SCRIPT = """\
MEMORY
{
    FLASH (rx) : ORIGIN = ((372736)), LENGTH = ((673792) - 0xe6)
    RAM (wx) : ORIGIN = (536870912), LENGTH = 32K
}
ENTRY("__start")
_x = 1;
"""

    def _run(self, conn, tmp_path, text=None, name="app.ld"):
        path = tmp_path / name
        path.write_text(text or self.SCRIPT, encoding="utf-8")
        with transaction(conn):
            return store_scripts(conn, "ch", [path], tmp_path)

    def test_the_regions_reach_the_table(self, tmp_path):
        conn = _db(tmp_path)
        try:
            result = self._run(conn, tmp_path)
            regions = get_memory_regions(conn, "ch")
        finally:
            conn.close()
        assert result.regions == 2
        assert [r["name"] for r in regions] == ["FLASH", "RAM"]

    def test_mixed_radix_arithmetic_gets_a_number(self, tmp_path):
        # Measured on a Zephyr script: LENGTH = ((673792) - 0xe6).
        conn = _db(tmp_path)
        try:
            self._run(conn, tmp_path)
            flash = get_memory_regions(conn, "ch")[0]
        finally:
            conn.close()
        assert flash["length"] == "((673792) - 0xe6)"
        assert flash["length_value"] == 673562

    def test_the_documented_suffix_gets_a_number(self, tmp_path):
        conn = _db(tmp_path)
        try:
            self._run(conn, tmp_path)
            ram = get_memory_regions(conn, "ch")[1]
        finally:
            conn.close()
        assert ram["length"] == "32K"
        assert ram["length_value"] == 32768

    def test_the_region_carries_its_file_and_line(self, tmp_path):
        conn = _db(tmp_path)
        try:
            self._run(conn, tmp_path)
            flash = get_memory_regions(conn, "ch")[0]
        finally:
            conn.close()
        assert flash["file_path"] == "app.ld"
        assert flash["line"] == 3

    def test_the_quoted_entry_point_reaches_the_build(self, tmp_path):
        conn = _db(tmp_path)
        try:
            result = self._run(conn, tmp_path)
            stored = get_entry_point(conn, "ch")
        finally:
            conn.close()
        assert result.entry == "__start"
        assert stored == "__start"

    def test_a_re_index_replaces_the_map(self, tmp_path):
        conn = _db(tmp_path)
        try:
            self._run(conn, tmp_path)
            self._run(conn, tmp_path, text="MEMORY { RAM : ORIGIN = 0, LENGTH = 1 }\n")
            regions = get_memory_regions(conn, "ch")
            entry = get_entry_point(conn, "ch")
        finally:
            conn.close()
        assert [r["name"] for r in regions] == ["RAM"]
        # The new script names no entry point, thus the old name must go.
        assert entry == ""

    def test_a_script_with_no_memory_command(self, tmp_path):
        conn = _db(tmp_path)
        try:
            result = self._run(conn, tmp_path, text="_x = 1;\n")
            regions = get_memory_regions(conn, "ch")
        finally:
            conn.close()
        assert result.regions == 0
        assert regions == []

    def test_the_first_script_wins_for_the_entry_point(self, tmp_path):
        # Zephyr passes the script of the real link first.
        first = tmp_path / "final.ld"
        first.write_text('ENTRY("__start")\n', encoding="utf-8")
        second = tmp_path / "pre0.ld"
        second.write_text("ENTRY(Reset_Handler)\n", encoding="utf-8")
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                result = store_scripts(conn, "ch", [first, second], tmp_path)
        finally:
            conn.close()
        assert result.entry == "__start"

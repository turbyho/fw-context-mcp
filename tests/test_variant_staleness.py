"""Header staleness must be measured against the manifest of THIS build.

A project with build variants keeps one ``manifest.<config_hash>.json`` per
variant, because the variant name enters ``config_hash``.  The staleness
helper used to load the manifest without a config_hash, and
``manifest.load()`` then falls back to the most recently written file — the
variant indexed last.  The count reported for variant A could therefore
describe variant B's translation units, and it was cached under A's key.

Real shape of this: the Zephyr project declares nrf52840-dev and nrf54lm20a-dev.
Index one, then the other, and every query about the first was answered from
the second's manifest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import open_db, upsert_build_config, upsert_project
from fw_context_mcp.indexer.manifest import MANIFEST_FORMAT

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _write_manifest(
    db_dir: Path, config_hash: str, project_root: Path, tu: str, header: str, *, stored_hash: str
) -> Path:
    """Write a one-TU manifest whose single header carries *stored_hash*."""
    manifest = {
        "_format": MANIFEST_FORMAT,
        "config_hash": config_hash,
        "project_root": str(project_root),
        "arg_sets": [["gcc", "-c", tu]],
        "headers": {header: {"hash": stored_hash, "generated": False}},
        "entries": [{
            "file": tu,
            "directory": str(project_root),
            "arg_set": 0,
            "source_hash": hashlib.sha256((project_root / tu).read_bytes()).hexdigest(),
            "headers": [header],
        }],
    }
    path = db_dir / f"manifest.{config_hash}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.fixture
def two_variants(tmp_path: Path):
    """A project with two builds: A's header matches disk, B's does not.

    So a correct reading of A reports 0 stale TUs and a correct reading of B
    reports 1 — which makes a swapped manifest visible as a number.
    """
    project_root = tmp_path / "proj"
    (project_root / "src").mkdir(parents=True)
    tu = project_root / "src" / "main.c"
    tu.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    header = project_root / "src" / "shared.h"
    header.write_text("// current content\n", encoding="utf-8")
    on_disk = hashlib.sha256(header.read_bytes()).hexdigest()

    db_dir = tmp_path / "index"
    db_dir.mkdir()
    conn = open_db(db_dir / "index.db", skip_integrity_check=True)
    project_id = "cafe" * 8
    upsert_project(conn, project_id, "proj", str(project_root))
    for config_hash in (_HASH_A, _HASH_B):
        upsert_build_config(conn, config_hash, project_id, str(project_root / "cc.json"))

    # A agrees with the disk; B stores a hash that no longer matches.
    _write_manifest(db_dir, _HASH_A, project_root, "src/main.c", "src/shared.h",
                    stored_hash=on_disk)
    # Written second, so it is the newest file in the directory — the one the
    # config_hash-less fallback would return for BOTH builds.
    _write_manifest(db_dir, _HASH_B, project_root, "src/main.c", "src/shared.h",
                    stored_hash=hashlib.sha256(b"stale content").hexdigest())

    yield conn, project_root
    conn.close()


def _count(conn, config_hash: str, project_root: Path) -> int:
    from fw_context_mcp.mcp.shared.stale import _check_header_staleness

    count, _ = _check_header_staleness(
        conn, config_hash, project_root, use_cache=False
    )
    return count


class TestVariantHeaderStaleness:
    def test_the_newest_manifest_does_not_answer_for_another_variant(self, two_variants):
        """A's header is unchanged, so A must report 0 — not B's 1."""
        conn, project_root = two_variants
        assert _count(conn, _HASH_A, project_root) == 0

    def test_the_stale_variant_still_reports_its_own_staleness(self, two_variants):
        conn, project_root = two_variants
        assert _count(conn, _HASH_B, project_root) == 1

    def test_a_build_with_no_manifest_reports_nothing(self, two_variants):
        """No manifest for this build means no header-hash data for it.

        Reporting 0 is the honest answer; the alternative the fallback gave
        was another build's number.
        """
        conn, project_root = two_variants
        assert _count(conn, "f" * 64, project_root) == 0

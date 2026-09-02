"""Memory map storage — the `MEMORY` command and the entry point.

The linker script is the only source the index has for a memory map.  It is
not in `compile_commands.json`, and no translation unit holds it.

WHY a table keyed on config_hash and not a column on the project: the map
belongs to ONE build.  Measured on the Zephyr project, which keeps nine
configurations in one database — `app` starts at flash 372736, `mcuboot` at
110592, and `app_flpr` declares no flash region at all.  A map stored per
project would be wrong for eight of the nine.

WHY the raw text travels with the number: an mbed script writes
`LENGTH = 0xefe00` and a Zephyr script writes `LENGTH = ((673792) - 0xe6)`,
which mixes decimal and hexadecimal.  The text is always what the file says.
The number is filled in only when the expression is constant arithmetic and
is NULL for an expression that names a symbol, because the index does not
evaluate a symbol.
"""

import sqlite3

__all__ = [
    "get_entry_point",
    "get_entry_points_by_config",
    "get_memory_regions",
    "get_memory_regions_by_config",
    "replace_memory_regions",
    "set_entry_point",
]


def replace_memory_regions(
    conn: sqlite3.Connection,
    config_hash: str,
    regions: list[dict],
) -> int:
    """Replace every region of *config_hash* with *regions*.  Returns how many.

    Each dict takes ``name``, ``origin``, ``length``, and optionally
    ``attributes``, ``origin_value``, ``length_value``, ``file_path``, and
    ``line``.

    Replace and not merge.  The linker script of a build declares its whole
    map in one place, so a region the script no longer holds must disappear
    — and `UNIQUE(config_hash, name)` would otherwise keep the old row with
    its old address.  The delete is scoped to *config_hash*, thus one
    variant of a multi-build project cannot clear another.

    A name that appears twice keeps the FIRST entry, which is the rule the
    symbols of a script follow as well.  One build can pass several scripts
    — ESP-IDF passes about ten, Zephyr passes two — and the caller orders
    them so the script of the real link comes first.
    """
    conn.execute(
        "DELETE FROM memory_regions WHERE config_hash = ?", (config_hash,)
    )
    rows = []
    seen: set[str] = set()
    for region in regions:
        name = region["name"]
        if name in seen:
            continue
        seen.add(name)
        rows.append((
            config_hash,
            name,
            region.get("attributes", ""),
            region["origin"],
            region["length"],
            region.get("origin_value"),
            region.get("length_value"),
            region.get("file_path", ""),
            region.get("line", 0),
        ))
    if not rows:
        return 0
    # No ON CONFLICT clause: the delete above emptied this config and the
    # loop dropped every repeated name, thus nothing can conflict.  A
    # clause here would hide a future caller that stops deduplicating.
    conn.executemany(
        """INSERT INTO memory_regions
           (config_hash, name, attributes, origin, length,
            origin_value, length_value, file_path, line)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def get_memory_regions(
    conn: sqlite3.Connection, config_hash: str
) -> list[dict]:
    """Return the memory regions of *config_hash*, in file order.

    File order and not alphabetical: a linker script lists the regions of a
    target in the order a reader expects them, usually flash before RAM.

    An index written before this feature holds no such table, and neither
    read path of the MCP server migrates one: `_quick_open_readonly` skips
    the schema block by design, and `SyncQueryExecutor` connects with plain
    sqlite3.  So a missing table answers "no region recorded" rather than
    raising — the caller reports an empty map, which is already the answer
    for a build system that records no linker script.
    """
    try:
        rows = conn.execute(
            "SELECT name, attributes, origin, length, origin_value, "
            "length_value, file_path, line FROM memory_regions "
            "WHERE config_hash = ? ORDER BY line, name",
            (config_hash,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "name": row["name"],
            "attributes": row["attributes"],
            "origin": row["origin"],
            "length": row["length"],
            "origin_value": row["origin_value"],
            "length_value": row["length_value"],
            "file_path": row["file_path"],
            "line": row["line"],
        }
        for row in rows
    ]


def get_memory_regions_by_config(
    conn: sqlite3.Connection, config_hashes: list[str]
) -> dict[str, list[dict]]:
    """Return the regions of every config in *config_hashes*.

    One query rather than one per config.  `list_variants` reports every
    build of a project, and the Zephyr project has nine — a query each would
    make the tool nine times as slow for no gain.

    A config with no regions is absent from the result, so the caller uses
    ``result.get(hash, [])``.
    """
    if not config_hashes:
        return {}
    grouped: dict[str, list[dict]] = {}
    for start in range(0, len(config_hashes), 500):
        chunk = config_hashes[start:start + 500]
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"SELECT config_hash, name, attributes, origin, length, "  # noqa: S608
                f"origin_value, length_value, file_path, line "
                f"FROM memory_regions WHERE config_hash IN ({marks}) "
                f"ORDER BY config_hash, line, name",
                chunk,
            ).fetchall()
        except sqlite3.OperationalError:
            # No table: an index from before this feature.  See
            # `get_memory_regions`.
            return {}
        for row in rows:
            grouped.setdefault(row["config_hash"], []).append({
                "name": row["name"],
                "attributes": row["attributes"],
                "origin": row["origin"],
                "length": row["length"],
                "origin_value": row["origin_value"],
                "length_value": row["length_value"],
                "file_path": row["file_path"],
                "line": row["line"],
            })
    return grouped


def set_entry_point(
    conn: sqlite3.Connection, config_hash: str, entry_point: str
) -> None:
    """Store the `ENTRY()` of the linker script of this build.

    An empty *entry_point* is written as well.  A build whose script named
    an entry point and no longer does must not keep the old name.
    """
    conn.execute(
        "UPDATE build_configs SET entry_point = ? WHERE config_hash = ?",
        (entry_point, config_hash),
    )


def get_entry_point(conn: sqlite3.Connection, config_hash: str) -> str:
    """Return the entry point of *config_hash*, or an empty string.

    An index written before this feature holds no such column, and neither
    read path of the MCP server migrates one — see `get_memory_regions`.
    A missing column answers "not recorded".
    """
    try:
        row = conn.execute(
            "SELECT entry_point FROM build_configs WHERE config_hash = ?",
            (config_hash,),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row["entry_point"]) if row is not None else ""


def get_entry_points_by_config(
    conn: sqlite3.Connection, config_hashes: list[str]
) -> dict[str, str]:
    """Return the entry point of every config in *config_hashes*.

    One query rather than one per config, for the same reason as
    `get_memory_regions_by_config`.  A config with no entry point is absent
    from the result, thus the caller uses ``result.get(hash, "")``.

    A missing column answers with nothing — see `get_entry_point`.
    """
    if not config_hashes:
        return {}
    found: dict[str, str] = {}
    for start in range(0, len(config_hashes), 500):
        chunk = config_hashes[start:start + 500]
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"SELECT config_hash, entry_point FROM build_configs "  # noqa: S608
                f"WHERE config_hash IN ({marks})",
                chunk,
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        for row in rows:
            if row["entry_point"]:
                found[row["config_hash"]] = str(row["entry_point"])
    return found

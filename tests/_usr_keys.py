"""The file key of a USR (``indexer._usr.file_key``) in the facts that a test reads.

A USR of a symbol with no external name holds the key of its file
(`c:fp.c~3fa9c2e1@g_cb`), and the hash of the key comes from the path in the
temporary directory of the test.  ``base_names`` replaces the key of each
file that the test wrote with the base name of the file, thus an assertion
can name `c:fp.c@g_cb`.  It replaces ONLY the key of those files: a USR that
names an other file keeps its key, and the assertion fails.  A USR or a
lambda key of one of those files WITHOUT the key (`c:fp.c@g_cb` in the
input) stops it: the base name in the output must come from a key.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from fw_context_mcp.indexer._usr import file_key


def base_names(value: Any, *files: Path | str) -> Any:
    """Give *value* with the file key of each of *files* replaced by its base name, in each string it holds."""
    keys = {file_key(str(file)): Path(file).name for file in files}
    raw = [marker for name in keys.values() for marker in (f"c:{name}@", f"@lambda@{name}:")]

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            missing = [marker for marker in raw if marker in item]
            assert not missing, f"a USR without the file key: {item!r}"
            for key, name in keys.items():
                item = item.replace(key, name)
            return item
        if isinstance(item, tuple):
            return tuple(walk(part) for part in item)
        if isinstance(item, list):
            return [walk(part) for part in item]
        if isinstance(item, (set, frozenset)):
            return type(item)(walk(part) for part in item)
        if isinstance(item, dict):
            return {walk(key): walk(part) for key, part in item.items()}
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            return dataclasses.replace(item, **{
                field.name: walk(getattr(item, field.name))
                for field in dataclasses.fields(item) if field.init
            })
        return item

    return walk(value)

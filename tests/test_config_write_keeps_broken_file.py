"""A config writer never replaces a config file that is not valid TOML.

``set_key`` and ``merge_template`` read the file, change the data and
write it again.  A file that they could not parse counted as empty, thus
the new data alone replaced it.  ``fw-context init`` on a project whose
``config.toml`` had a syntax error wrote a new project ID over the file:
each setting of the team and the link to the index were lost.
"""

from __future__ import annotations

import pytest

BROKEN = '[project]\nid = "0123456789abcdef0123456789abcdef"\n\n[build]\nsystem = "zephyr"\n\n[index\nx = 1\n'


@pytest.fixture
def broken(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BROKEN, encoding="utf-8")
    return path


def test_set_key_keeps_a_file_that_is_not_valid_toml(broken):
    from fw_context_mcp.config import ConfigError
    from fw_context_mcp.config._toml_editor import set_key

    with pytest.raises(ConfigError, match=r"config\.toml: not valid TOML: .*line 7"):
        set_key(broken, "project", "id", "ffffffffffffffffffffffffffffffff")

    assert broken.read_text(encoding="utf-8") == BROKEN


def test_merge_template_keeps_a_file_that_is_not_valid_toml(broken):
    from fw_context_mcp.config import ConfigError
    from fw_context_mcp.config._toml_editor import merge_template

    with pytest.raises(ConfigError, match="not valid TOML"):
        merge_template(broken, "[index]\nindex_embeddings = true\n")

    assert broken.read_text(encoding="utf-8") == BROKEN


def test_a_file_that_is_not_utf8_is_kept(tmp_path):
    from fw_context_mcp.config import ConfigError
    from fw_context_mcp.config._toml_editor import set_key

    path = tmp_path / "config.toml"
    path.write_bytes(b'[build]\nboard = "caf\xe9"\n')

    with pytest.raises(ConfigError, match="not UTF-8"):
        set_key(path, "project", "id", "abc")

    assert path.read_bytes() == b'[build]\nboard = "caf\xe9"\n'


def test_the_writers_still_create_and_change_a_valid_file(tmp_path):
    import tomllib

    from fw_context_mcp.config._toml_editor import merge_template, set_key

    path = tmp_path / "new" / "config.toml"
    set_key(path, "project", "id", "abc")
    assert merge_template(path, "[index]\nindex_embeddings = true\n") == ["index_embeddings"]

    assert tomllib.loads(path.read_text(encoding="utf-8")) == {
        "project": {"id": "abc"}, "index": {"index_embeddings": True},
    }


def test_the_error_names_the_file_and_the_action(tmp_path):
    from fw_context_mcp.config import ConfigError

    error = ConfigError(tmp_path / "config.toml", "not valid TOML: x")

    assert str(error) == f"{tmp_path / 'config.toml'}: not valid TOML: x. Correct the file, then do the command again."
    assert isinstance(error, ValueError)

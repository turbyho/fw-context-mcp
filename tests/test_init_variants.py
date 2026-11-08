"""Tests for the ``init-variants`` CLI (add/remove/list of [[build.variants]])."""

import tomllib
from pathlib import Path

from fw_context_mcp.cli._init_variants import _add_variant, _list_variants, _remove_variant


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return None


class TestInitVariants:
    def _cfg(self, tmp_path: Path) -> Path:
        cfg = tmp_path / ".fw-context" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[project]\nid = \"x\"\n\n[build]\nsystem = \"zephyr\"\n")
        return cfg

    def test_add_and_parse(self, tmp_path, capsys):
        cfg = self._cfg(tmp_path)
        rc = _add_variant(
            cfg,
            _Args(name="nrf52840-dev", board="nrf52840dk/nrf52840", env=["BOARD_ENV=DEV"]),
        )
        assert rc == 0
        data = tomllib.loads(cfg.read_text())
        variants = data["build"]["variants"]
        assert len(variants) == 1
        assert variants[0]["name"] == "nrf52840-dev"
        assert variants[0]["board"] == "nrf52840dk/nrf52840"
        assert variants[0]["env"] == {"BOARD_ENV": "DEV"}

    def test_add_duplicate_fails(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _add_variant(cfg, _Args(name="a", board="b"))
        rc = _add_variant(cfg, _Args(name="a", board="b"))
        assert rc == 1

    def test_remove(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _add_variant(cfg, _Args(name="a", board="b"))
        _add_variant(cfg, _Args(name="c", board="d"))
        rc = _remove_variant(cfg, "a")
        assert rc == 0
        data = tomllib.loads(cfg.read_text())
        assert [v["name"] for v in data["build"]["variants"]] == ["c"]

    def test_remove_missing(self, tmp_path):
        cfg = self._cfg(tmp_path)
        assert _remove_variant(cfg, "nope") == 1

    def test_list_empty(self, tmp_path, capsys):
        cfg = self._cfg(tmp_path)
        assert _list_variants(cfg, tmp_path) == 0
        assert "No [[build.variants]]" in capsys.readouterr().out


class TestValuesThatNeedAnEscape:
    """A value with a backslash or a quote must stay a valid TOML value.

    Before, ``add`` wrote ``key = "<value>"`` as text.  A Windows path
    (``C:\\Users``: ``\\U`` starts a Unicode escape) or a quote in the
    description made the config not valid, on each system.
    """

    def _cfg(self, tmp_path: Path) -> Path:
        cfg = tmp_path / ".fw-context" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            '# the config of the project\n[project]\nid = "x"\n\n'
            '[build]\nsystem = "zephyr"  # keep this comment\n\n'
            "[index]\ndb_dir = 'C:\\Users\\x'\n",
            encoding="utf-8",
        )
        return cfg

    def test_add_writes_an_escape_for_each_value(self, tmp_path):
        cfg = self._cfg(tmp_path)
        description = 'the "dev" kit at C:\\Users\\a'
        rc = _add_variant(
            cfg, _Args(name='dev"1', board="nrf/x", description=description, env=["SDK=C:\\ncs", 'Q="']),
        )
        assert rc == 0
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert data["build"]["variants"] == [
            {"name": 'dev"1', "description": description, "board": "nrf/x", "env": {"SDK": "C:\\ncs", "Q": '"'}},
        ]
        assert data["index"]["db_dir"] == "C:\\Users\\x"

    def test_add_and_remove_keep_the_comments(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _add_variant(cfg, _Args(name="a", board="b"))
        _remove_variant(cfg, "a")
        text = cfg.read_text(encoding="utf-8")
        assert "# the config of the project" in text
        assert "# keep this comment" in text
        assert "variants" not in tomllib.loads(text)["build"]

    def test_remove_finds_a_name_with_an_escape(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _add_variant(cfg, _Args(name='a"b', board="b"))
        _add_variant(cfg, _Args(name="c", board="d"))
        assert _remove_variant(cfg, 'a"b') == 0
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
        assert [v["name"] for v in data["build"]["variants"]] == ["c"]

    def test_remove_finds_a_name_in_a_literal_string(self, tmp_path):
        cfg = self._cfg(tmp_path)
        cfg.write_text(
            cfg.read_text(encoding="utf-8") + "\n[[build.variants]]\nname = 'lit'  # a comment\nboard = \"b\"\n",
            encoding="utf-8",
        )
        assert _remove_variant(cfg, "lit") == 0
        assert "variants" not in tomllib.loads(cfg.read_text(encoding="utf-8"))["build"]


class TestShapesThatTomlkitCannotChange:
    """A result that is not the intended config is not written (check of the result)."""

    def test_an_inline_array_is_refused_and_the_file_stays(self, tmp_path, capsys):
        cfg = tmp_path / "config.toml"
        text = '[build]\nsystem = "z"\nvariants = [{name = "a"}]\n'
        cfg.write_text(text, encoding="utf-8")
        assert _add_variant(cfg, _Args(name="c", board="b")) == 1
        assert cfg.read_text(encoding="utf-8") == text
        assert "[[build.variants]]" in capsys.readouterr().err

    def test_remove_of_a_file_that_is_not_toml_is_an_error(self, tmp_path, capsys):
        cfg = tmp_path / "config.toml"
        cfg.write_text("[build\n", encoding="utf-8")
        assert _remove_variant(cfg, "a") == 1
        assert "cannot parse" in capsys.readouterr().err

    def test_a_build_that_is_not_a_table_is_an_error(self, tmp_path, capsys):
        cfg = tmp_path / "config.toml"
        cfg.write_text('build = "zephyr"\n', encoding="utf-8")
        assert _add_variant(cfg, _Args(name="c", board="b")) == 1
        assert _remove_variant(cfg, "c") == 1
        assert cfg.read_text(encoding="utf-8") == 'build = "zephyr"\n'

    def test_the_removal_of_the_only_variant_without_a_build_header(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[project]\nid = "x"\n\n[[build.variants]]\nname = "a"\n', encoding="utf-8")
        assert _remove_variant(cfg, "a") == 0
        assert tomllib.loads(cfg.read_text(encoding="utf-8")) == {"project": {"id": "x"}}

    def test_a_new_table_starts_after_a_blank_line(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[build]\nsystem = "z"\n[index]\nx = 1\n', encoding="utf-8")
        assert _add_variant(cfg, _Args(name="c", board="b")) == 0
        text = cfg.read_text(encoding="utf-8")
        assert '"z"\n\n[[build.variants]]\n' in text
        assert '"b"\n\n[index]\n' in text

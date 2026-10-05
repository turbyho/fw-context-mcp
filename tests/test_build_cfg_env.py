"""``[build] env``, ``extra_path`` and ``extra_env`` reach every build command.

The builders got them through ``run_build_command``; the ``pre_build`` hook
and the ``command`` override ran with ``build_env()`` only.  ``env`` is in
the ``config_hash``, thus two variants that differ in ``env`` got two
hashes and one environment.
"""

from __future__ import annotations

import os
from pathlib import Path

from fw_context_mcp.indexer.build import BuildConfig, _generate_into, _run_pre_build
from fw_context_mcp.utils import build_cfg_env


def _cfg(**fields) -> BuildConfig:
    return BuildConfig(env={"FW_TEST_ENV": "from-env"}, extra_path=["/opt/fw-test/bin"],
                       extra_env={"FW_TEST_EXTRA": "from-extra"}, **fields)


def test_build_cfg_env_merges_the_build_config():
    env = build_cfg_env(_cfg(), {"FW_TEST_CALLER": "caller"})

    assert env["FW_TEST_ENV"] == "from-env"
    assert env["FW_TEST_EXTRA"] == "from-extra"
    assert env["FW_TEST_CALLER"] == "caller"
    assert env["PATH"].split(os.pathsep)[0] == "/opt/fw-test/bin"


def test_the_pre_build_hook_gets_the_build_environment(tmp_path: Path):
    out = tmp_path / "env.txt"
    cfg = _cfg(pre_build=f'sh -c \'echo "$FW_TEST_ENV $FW_TEST_EXTRA ${{PATH%%:*}}" > {out}\'')

    _run_pre_build(cfg, tmp_path)

    assert out.read_text(encoding="utf-8").split() == ["from-env", "from-extra", "/opt/fw-test/bin"]


def test_the_command_override_gets_the_build_environment(tmp_path: Path):
    out = tmp_path / "env.txt"
    cfg = _cfg(command=f'sh -c \'echo "$FW_TEST_ENV" > {out}; echo [] > compile_commands.json\'')

    _generate_into(tmp_path.resolve(), cfg)

    assert out.read_text(encoding="utf-8").strip() == "from-env"

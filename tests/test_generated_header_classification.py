"""A generated header must be recognised wherever the build put it.

`header_is_trusted()` reads one thing: whether the manifest calls the header
`generated`.  A header the pipeline does not know is generated is re-hashed
on every index run, and a generated header changes with every build without
a change of meaning — so every unit that includes it is marked header-stale
and re-parsed, which is the opposite of what the three staleness tiers are
for.

An isolated automatic build writes those headers under
`.fw-context/autobuild/`, and the patterns of the backend do not reach
there.
"""

from __future__ import annotations

import pytest

from fw_context_mcp.indexer.manifest import _is_generated_header
from fw_context_mcp.utils import autobuild_dir, build_dir_patterns_with_fw_context

# The real values, read out of the manifests of the test projects.  They are
# the point: three of these do not match the fw-context directory at all,
# and the ones that do match only because ".fw-context/autobuild/" happens
# to hold the substring "build/".
REAL_PATTERNS = {
    "mbed_os": ["BUILD/"],
    "platformio": [".pio/build/"],
    "zephyr": ["build/nrf52840_sysbuild/"],
    "generic_cmake": ["build/", "cmake-build-"],
    "esp_idf": ["build/"],
    "arduino": ["build/"],
    "makefile": ["build/"],
    "iar": [],
    "keil": [],
    "manual": [],
}


class TestFwContextCountsAsBuildOutput:
    @pytest.mark.parametrize("backend", sorted(REAL_PATTERNS))
    def test_a_generated_header_in_the_isolated_directory(self, backend: str):
        """Parametrised on purpose: today only some of these match by luck."""
        patterns = build_dir_patterns_with_fw_context(REAL_PATTERNS[backend])
        header = f"{autobuild_dir()}/mbed_config.h"

        assert _is_generated_header(header, patterns), (
            f"{backend} gives {REAL_PATTERNS[backend]}, which does not reach "
            "the directory an isolated build writes to"
        )

    @pytest.mark.parametrize("backend", sorted(REAL_PATTERNS))
    def test_a_project_header_is_not_build_output(self, backend: str):
        patterns = build_dir_patterns_with_fw_context(REAL_PATTERNS[backend])
        assert not _is_generated_header("src/config.h", patterns)

    def test_the_patterns_of_the_backend_still_apply(self):
        """The helper adds, it does not replace."""
        patterns = build_dir_patterns_with_fw_context(["BUILD/"])
        assert _is_generated_header("BUILD/mbed_config.h", patterns)
        assert _is_generated_header(f"{autobuild_dir('nrf52')}/autoconf.h", patterns)

    def test_an_empty_pattern_list_still_covers_fw_context(self):
        """iar, keil and manual give no patterns at all."""
        assert _is_generated_header(
            f"{autobuild_dir()}/sdkconfig.h", build_dir_patterns_with_fw_context([])
        )

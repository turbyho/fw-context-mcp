"""A generated header must be recognised wherever the build put it.

The query layer and the coverage purge read whether the manifest calls a
header `generated`: a header of the build output is not code of the project,
and it is not a file that left the build when the build writes it again.
No staleness check reads the flag: every header is re-hashed.

Each build of fw-context writes those headers under
`.fw-context/build/<variant>/out/`, and the patterns of the backend do not
reach there.
"""

from __future__ import annotations

import pytest

from fw_context_mcp.indexer.manifest import _is_generated_header
from fw_context_mcp.utils import build_dir_patterns_with_fw_context

# The output directory of a build of fw-context, as build_layout names it.
_OUT = ".fw-context/build/default/out"

# The real values, read out of the manifests of the test projects.  They are
# the point: three of these do not match the fw-context directory at all,
# and the ones that do match only because ".fw-context/build/" happens to
# hold the substring "build/".
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
    def test_a_generated_header_in_the_output_directory(self, backend: str):
        """Parametrised on purpose: today only some of these match by luck."""
        patterns = build_dir_patterns_with_fw_context(REAL_PATTERNS[backend])
        header = f"{_OUT}/mbed_config.h"

        assert _is_generated_header(header, patterns), (
            f"{backend} gives {REAL_PATTERNS[backend]}, which does not reach "
            "the directory a build of fw-context writes to"
        )

    @pytest.mark.parametrize("backend", sorted(REAL_PATTERNS))
    def test_a_project_header_is_not_build_output(self, backend: str):
        patterns = build_dir_patterns_with_fw_context(REAL_PATTERNS[backend])
        assert not _is_generated_header("src/config.h", patterns)

    def test_the_patterns_of_the_backend_still_apply(self):
        """The helper adds, it does not replace."""
        patterns = build_dir_patterns_with_fw_context(["BUILD/"])
        assert _is_generated_header("BUILD/mbed_config.h", patterns)
        assert _is_generated_header(".fw-context/build/nrf52/out/zephyr/include/generated/autoconf.h", patterns)

    def test_an_empty_pattern_list_still_covers_fw_context(self):
        """iar, keil and manual give no patterns at all."""
        assert _is_generated_header(
            f"{_OUT}/config/sdkconfig.h", build_dir_patterns_with_fw_context([])
        )

# Test builds — how to provision and run

Each subdirectory under `tests/builds/` is a minimal firmware project fixture
for fw-context indexing tests.  This document describes the tools required,
how to install them, and how to exercise detection, build, and indexing for
each fixture.

## Overview

| Fixture | Build system | What is tested | Required tools |
|---|---|---|---|
| `bare/` | bare (manual) | init + build + index (`tests/test_builder_projects.py`) | `python -m tests.sdk.provision` |
| `generic_cmake/` | cmake | init + cross build + index (`tests/test_builder_projects.py`) | `python -m tests.sdk.provision` |
| `makefile/` | makefile | init + build + index (`tests/test_builder_projects.py`) | `make`, `python -m tests.sdk.provision` |
| `platformio_esp32c3/` | platformio | init + build + index (`tests/test_sdk_projects.py`) | `python -m tests.sdk.provision` |
| `platformio_esp32/`, `platformio_nucleo_l476rg/`, `platformio_uno/` | platformio | init + build + index (`tests/test_extra_platforms.py`) | `python -m tests.sdk.provision --extra` |
| `mbed_os/` | mbed-os | detection only (no test builds Mbed OS) | — |
| `arduino/` | arduino | init + build + index (`tests/test_extra_platforms.py`) | `python -m tests.sdk.provision --extra` |
| `zephyr/` | zephyr | init + sysbuild + index (`tests/test_sdk_projects.py`) | `python -m tests.sdk.provision` |
| `esp_idf/` | esp-idf | init + build + index (`tests/test_extra_platforms.py`) | `python -m tests.sdk.provision --extra` |
| `keil/` | keil-mdk | detection only | — |
| `iar/` | iar-ewarm | detection only | — |
| `stubs/` | ti-ccs | detection only | — |

## Test SDKs

The tests compile their fixtures with the toolchains of embedded SDKs.
`tests/sdk/versions.toml` pins the version of each SDK, and each SDK uses
its default toolchain:

| Toolchain | SDK | Board | Fixture |
|---|---|---|---|
| `arm-zephyr` (`arm-zephyr-eabi`) | Zephyr and the Zephyr SDK | `nrf52840dk/nrf52840` | `zephyr/` |
| `riscv-esp` (`riscv32-esp-elf`) | PlatformIO, `espressif32` | ESP32-C3 | `platformio_esp32c3/` |

Install them, from the root of the repository:

```bash
make test-sdk                       # or: python -m tests.sdk.provision
```

The script installs everything into `~/.fw-context-test-sdk`, and into
`C:\fwctx-sdk` on Windows (set `FW_CONTEXT_TEST_SDK` for a different
root).  WHY a short root on Windows: the longest path under the root has
235 characters, and the gcc of the toolchains cannot open a path longer
than 259 characters, also with `LongPathsEnabled`.  NCS installs into
`C:\ncs` for the same reason.  On Windows the root can have at most 23
characters, and the script and the pre-test refuse a longer one.

- The CPython of `[python]` of `versions.toml` (python-build-standalone),
  in `python/` of the root.  The script checks the SHA-256 of the archive.
  WHY: the build of Zephyr needs the Python of `PYTHON_MINIMUM_REQUIRED`
  of its tree (3.12), and a host can have an older one.
- A venv of that Python with the pinned tools of
  `tests/sdk/requirements.txt` (west, platformio, cmake, ninja).
- A west workspace of the Zephyr revision, with only the modules that
  `versions.toml` names.
- The Zephyr SDK of the version in the file `SDK_VERSION` of Zephyr, with
  only the GNU toolchains that `versions.toml` names.  The script checks
  the SHA-256 of each archive.  It does not register the SDK in the CMake
  package registry of the user.
- The PlatformIO packages, in a `PLATFORMIO_CORE_DIR` of the root.  The
  global `~/.platformio` does not change.

`tests/test_sdk_projects.py` copies `zephyr/` and `platformio_esp32c3/`
to a temporary directory, runs `fw-context init --quick` in the
environment of the SDKs, and then `fw-context index --build` without it,
as a build in the background runs.  The tests read the index: the image
of the sysbuild (the application, no MCUboot), Kconfig and devicetree,
the handler of `IRQ_CONNECT`, the macro values and the target of each
unit.  Marker `sdk_project`: `make test-all` runs them, `make test` does
not, and CI runs them in one job.

```bash
pytest tests/test_sdk_projects.py -m sdk_project
```

A second run installs only the parts that are missing.  The pre-test of
each test session compares the install with the pinned versions, and
stops the run when they are different.  A change of `versions.toml`,
`tests/sdk/requirements.txt` or `platformio_esp32c3/platformio.ini` thus
needs a new run of the script.

## The extended set

`[extra]` of `versions.toml` pins the SDKs of the architectures and the
builders that the set of CI does not have:

| Fixture | SDK | Target of the driver | What it adds |
|---|---|---|---|
| `platformio_esp32/` | PlatformIO `espressif32`, Arduino | `xtensa-esp-elf` | Xtensa |
| `platformio_nucleo_l476rg/` | PlatformIO `ststm32`, Arduino | `arm-none-eabi` | ARM with the newlib of PlatformIO |
| `platformio_uno/` | PlatformIO `atmelavr`, Arduino | `avr` | AVR, 8 bits |
| `esp_idf/` | ESP-IDF (release archive) | `xtensa-esp-elf` | the builder of ESP-IDF, the image `bootloader` |
| `arduino/` | Arduino CLI, `arduino:avr` | `avr` | the builder of Arduino |

Install them, and run their tests (marker `extra_platforms`, which the
other targets leave out):

```bash
make test-sdk-platforms             # or: python -m tests.sdk.provision --extra
make test-platforms                 # or: pytest tests/ -m extra_platforms
```

The script installs them into the same root, about 6 GB more: the
packages of the three PlatformIO projects, ESP-IDF with its tools and
Python env in `espressif/` (`IDF_TOOLS_PATH`), and Arduino CLI with its
core in `arduino/`.  It checks the SHA-256 of each archive.  The pre-test
checks the extended set only for a run that selects one of its tests.

## Tool installation

### make (system package)

The tests of the marker `make` and the Makefile project run `make`.  The
compilers, cmake and ninja come from the test SDKs.

```bash
sudo pacman -S make
```

## Running the tests

### Detection tests (always work, no tools required except Python)

```bash
python3 -m pytest tests/test_system_indexing.py -m system -x -v -k "detection or registry"
```

The default options of pytest (`pyproject.toml`) leave out the marker
`system`, thus each command here gives `-m system`.

### Build + index tests (need the tools listed above)

Run everything that the local machine supports:

```bash
python3 -m pytest tests/test_system_indexing.py -m system -x -v
```

The Zephyr project and the PlatformIO ESP32-C3 project are in
`tests/test_sdk_projects.py` (marker `sdk_project`, see Test SDKs).  The
bare, CMake and Makefile projects are in `tests/test_builder_projects.py`:
they build with the ARM toolchain of the test SDKs (a static library, no
program: a program of a bare-metal target needs a linker script), and
`make test` runs them.

The ESP32, Nucleo L476RG, Uno, ESP-IDF and Arduino projects are in
`tests/test_extra_platforms.py` (marker `extra_platforms`, see The
extended set).

### Testing individual fixtures by hand

Use the `fw-context` CLI directly against any fixture directory:

```bash
# Detection only
python3 -m fw_context_mcp.cli index --project tests/builds/<fixture>

# Full build + index
python3 -m fw_context_mcp.cli index --build --project tests/builds/<fixture>

# With references and analysis (slower)
python3 -m fw_context_mcp.cli index --build --project tests/builds/<fixture>
```

Build-system-specific incantations for each fixture:

```bash
# bare, generic_cmake, makefile — tests/test_builder_projects.py builds a
# copy of each with the ARM toolchain of the test SDKs; the test writes the
# config of the cross build (compiler, flags, toolchain file, make_vars).

# zephyr, platformio_esp32c3 — tests/test_sdk_projects.py builds and
# indexes a copy of each with the test SDKs (see Test SDKs).

# platformio_esp32, platformio_nucleo_l476rg, platformio_uno, esp_idf,
# arduino — tests/test_extra_platforms.py builds and indexes a copy of each
# with the extended set; the test writes the environment into local.toml.
```

## Analyzing test output

The tests index a copy of each project in a temporary directory, and pytest
removes it.  The commands here read the index of a project that you
indexed by hand (see "Testing individual fixtures by hand").  Replace
`<project>` with its directory.

### Check indexed symbols

```bash
# List all functions defined in the project
python3 -c "
from fw_context_mcp.indexer.db import open_db
from fw_context_mcp.config import derive_project_id, load
from pathlib import Path
proj = Path('<project>').resolve()
cfg = load(project_root=proj)
pid = derive_project_id(proj)
db  = cfg.index.db_dir / pid / 'index.db'
conn = open_db(db)
rows = conn.execute('''
    SELECT name, kind, file_path, line
    FROM symbols WHERE is_definition=1
    ORDER BY kind, name
''').fetchall()
for r in rows:
    print(f'[{r[\"kind\"]:12}] {r[\"name\"]:25} {Path(r[\"file_path\"]).name}:{r[\"line\"]}')
conn.close()
"
```

### Check build detection

```bash
python3 -c "
from fw_context_mcp.indexer.build import detect_build_system
from pathlib import Path
for d in sorted(Path('tests/builds').iterdir()):
    if d.is_dir():
        print(f'{d.name:20s} -> {detect_build_system(d)}')
"
```

### Inspect the SQLite index directly

```bash
# Find the database
python3 -c "
from fw_context_mcp.config import derive_project_id, load
from pathlib import Path
proj = Path('<project>').resolve()
cfg = load(project_root=proj)
pid = derive_project_id(proj)
print(cfg.index.db_dir / pid / 'index.db')
"

# Open with sqlite3 shell
sqlite3 <db_path> 'SELECT COUNT(*) FROM symbols;'
sqlite3 <db_path> '.schema symbols'
sqlite3 <db_path> "SELECT name, kind FROM symbols WHERE kind='function' AND is_definition=1;"
```

### Verify compile_commands.json

A build of fw-context writes the file under
`<project>/.fw-context/build/default/out/` (PlatformIO: in the directory of
the environment there).

```bash
python3 -c "
import json
from pathlib import Path
cc = Path('<project>/.fw-context/build/default/out/compile_commands.json')
data = json.loads(cc.read_text())
print(f'Entries: {len(data)}')
for e in data[:3]:
    print(f'  {e[\"file\"]}')
"
```

## Directory structure

```
tests/builds/
├── howto.md                  # this file
├── bare/                     # C project, manual mode (ARM toolchain of the test SDKs)
│   └── src/{main.c,lib.c,lib.h}
├── generic_cmake/            # CMake C library, cross build (ARM toolchain of the test SDKs)
│   ├── CMakeLists.txt
│   ├── cmake/cross-gcc.cmake  # toolchain file: CC and CFLAGS give the compiler
│   └── src/main.c
├── makefile/                 # Makefile C library (ARM toolchain of the test SDKs)
│   ├── Makefile
│   └── src/main.c
├── mbed_os/                  # Mbed OS 6 project, detection only
│   ├── .mbed
│   ├── main.cpp               # must be in root for mbed-cli detection
│   ├── mbed-os.lib
│   └── mbed_app.json
├── arduino/                  # Arduino Uno sketch (extended set)
│   ├── arduino.ino
│   └── sketch.yaml
├── zephyr/                   # Zephyr RTOS project (test SDKs)
│   ├── CMakeLists.txt
│   ├── prj.conf
│   ├── west.yml
│   └── src/main.c
├── platformio_esp32c3/       # PlatformIO ESP32-C3 Arduino project (test SDKs)
│   ├── platformio.ini
│   ├── include/sensor.h
│   └── src/{main.cpp,sensor.c}
├── platformio_esp32/         # PlatformIO ESP32, Nucleo L476RG and Uno Arduino projects (extended set)
├── platformio_nucleo_l476rg/ #   each: platformio.ini, include/blink.h, src/{main.cpp,blink.c}
├── platformio_uno/
├── esp_idf/                  # ESP-IDF ESP32 project (extended set)
│   ├── CMakeLists.txt
│   ├── sdkconfig
│   └── main/{CMakeLists.txt,main.c}
├── keil/                     # Keil MDK marker
│   └── .uvprojx
├── iar/                      # IAR EWARM marker
│   └── .ewp
└── stubs/                    # TI CCS marker
    └── .projectspec
```

# Test builds — how to provision and run

Each subdirectory under `tests/builds/` is a minimal firmware project fixture
for fw-context indexing tests.  This document describes the tools required,
how to install them, and how to exercise detection, build, and indexing for
each fixture.

## Overview

| Fixture | Build system | What is tested | Required tools |
|---|---|---|---|
| `bare/` | bare (manual) | build + index | `gcc` |
| `generic_cmake/` | cmake | build + index | `cmake`, `gcc` |
| `makefile/` | makefile | generate + index | `make`, `compiledb`, `gcc` |
| `platformio/` | platformio | build + index | `platformio` |
| `mbed_os/` | mbed-os | build + index | `mbed-cli` (pyenv 3.11), `bear`, `arm-none-eabi-gcc` |
| `arduino/` | arduino | build + index | `arduino-cli` |
| `zephyr/` | zephyr | build + index | `west`, Zephyr SDK, `arm-zephyr-eabi-gcc` |
| `esp_idf/` | esp-idf | build + index | `eim` (ESP-IDF installation manager), `xtensa-esp-elf-gcc` |
| `keil/` | keil-mdk | detection only | — |
| `iar/` | iar-ewarm | detection only | — |
| `stubs/` | ti-ccs | detection only | — |

## Tool installation

### gcc, cmake, make (system packages)

```bash
sudo pacman -S gcc cmake make
```

### compiledb (Python package)

```bash
pip install compiledb
# or in the fw-context venv:
uv pip install compiledb
```

### platformio

```bash
pip install platformio
# After install, PlatformIO downloads toolchains automatically on first build.
```

Verify:

```bash
platformio --version   # 6.1.19+
```

### arduino-cli

```bash
sudo pacman -S arduino-cli
```

Install board cores used by the test fixture:

```bash
arduino-cli core install arduino:avr
```

Verify:

```bash
arduino-cli version    # 1.4.1+
arduino-cli core list  # arduino:avr installed
```

### mbed-cli (Python 3.11 via pyenv) + bear

Mbed OS 6 is built with `mbed-cli` and the ARM GCC toolchain.
`bear` captures the build commands into `compile_commands.json`.

```bash
~/.pyenv/versions/3.11.8/bin/pip install mbed-cli mbed-tools 'setuptools<69'

# bear (for compile_commands.json capture)
sudo pacman -S bear
```

**Important:** mbed-cli 1.x detects a project as a "program" (buildable firmware)
only when `main.cpp` is in the project root.  Keep the application entry point
at the top level, not in a subdirectory.

ARM GCC toolchain is also required (see below).  Add to PATH permanently:

```bash
mkdir -p ~/.local/bin
for tool in arm-none-eabi-gcc arm-none-eabi-g++ arm-none-eabi-gcc-ar \
    arm-none-eabi-objcopy arm-none-eabi-objdump arm-none-eabi-size; do
    ln -sf ~/dev/sw/dev_tools/gcc-arm-none-eabi-9-2020-q2-update/bin/$tool ~/.local/bin/$tool
done
```

The `setuptools<69` constraint is needed because mbed-tools 7.59.0 imports
`pkg_resources`, removed in setuptools ≥69.

Verify:

```bash
arm-none-eabi-gcc --version   # 9.3.1
export PYENV_VERSION=3.11.8
mbed --version                # 1.10.5
which bear                    # /usr/bin/bear
```

### ARM GCC toolchain (for mbed-os)

```bash
# Download from ARM developer site:
# https://developer.arm.com/tools-and-software/open-source-software/developer-tools/gnu-toolchain/gnu-rm/downloads
mkdir -p ~/dev/sw/dev_tools
cd ~/dev/sw/dev_tools
tar xf gcc-arm-none-eabi-9-2020-q2-update-x86_64-linux.tar.bz2
```

Configure in `~/.mbed`:

```ini
GCC_ARM_PATH=/home/turbyho/dev/sw/dev_tools/gcc-arm-none-eabi-9-2020-q2-update/bin
```

Verify:

```bash
arm-none-eabi-gcc --version   # 9.3.1
```

### west + Zephyr SDK (Nordic Connect SDK)

On this machine the Nordic Connect SDK is installed at:

```
~/ncs/v3.2.3/zephyr          (Zephyr RTOS)
~/ncs/toolchains/2ac5840438/opt/zephyr-sdk   (Zephyr SDK toolchain)
~/ncs_tools/                  (environment setup scripts)
```

The `one_line_setup.sh` script sets `ZEPHYR_BASE`, `ZEPHYR_SDK_INSTALL_DIR`,
and `PATH` for the active NCS version.

Manual environment (without the setup script):

```bash
export PYENV_VERSION=3.11.8
export ZEPHYR_BASE=/home/turbyho/ncs/v3.2.3/zephyr
export ZEPHYR_SDK_INSTALL_DIR=/home/turbyho/ncs/toolchains/2ac5840438/opt/zephyr-sdk
export PATH="$ZEPHYR_SDK_INSTALL_DIR/arm-zephyr-eabi/bin:$HOME/.pyenv/versions/3.11.8/bin:$PATH"
```

Verify:

```bash
west --version          # 1.5.0
arm-zephyr-eabi-gcc --version
```

For a clean install from scratch, use Nordic's `nrfutil`:

```bash
nrfutil sdk-manager install --ncs-version 3.2.3
```

### ESP-IDF (via eim — Espressif Installation Manager)

Install `eim` from the Espressif pacman repository:

```bash
# Add the Espressif repository key
curl -fsSL https://dl.espressif.com/dl/eim/eim.asc | sudo pacman-key --add -

# Install eim-cli
sudo pacman -S eim-cli

# Install ESP-IDF with tools for esp32 target
eim install -i v5.2.5 -t esp32 -p ~/.espressif -n true
```

Activate the environment in each shell session before using `idf.py`:

```bash
source ~/.espressif/tools/activate_idf_v5.2.5.sh
```

Verify:

```bash
source ~/.espressif/tools/activate_idf_v5.2.5.sh
idf.py --version        # ESP-IDF v5.2.5
```

## Running the tests

### Detection tests (always work, no tools required except Python)

```bash
python3 -m pytest tests/test_system_indexing.py -x -v -k "detection or registry"
# 36 tests: 12 detection + 24 registry completeness
```

### Build + index tests (need the tools listed above)

Run everything that the local machine supports:

```bash
python3 -m pytest tests/test_system_indexing.py -x -v
# 49 tests: detection + bare + cmake + makefile + config hash
```

Run a single project's tests:

```bash
python3 -m pytest tests/test_system_indexing.py -x -v -k "BareProject"     # bare
python3 -m pytest tests/test_system_indexing.py -x -v -k "CMakeProject"    # cmake
python3 -m pytest tests/test_system_indexing.py -x -v -k "MakefileProject" # makefile
```

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
# bare — manual mode
python3 -m fw_context_mcp.cli index --build --no-refs --no-analyze \
    --project tests/builds/bare

# generic_cmake
python3 -m fw_context_mcp.cli index --build --no-refs --no-analyze \
    --project tests/builds/generic_cmake

# makefile (via compiledb)
python3 -m fw_context_mcp.cli index --build --no-refs --no-analyze \
    --project tests/builds/makefile

# platformio (may take ~15 s for the first build)
python3 -m fw_context_mcp.cli index --no-refs --no-analyze \
    --project tests/builds/platformio

# arduino
python3 -m fw_context_mcp.cli index --build --no-refs --no-analyze \
    --project tests/builds/arduino

# zephyr — build first, then index
export PYENV_VERSION=3.11.8
source ~/ncs_tools/one_line_setup.sh
cd tests/builds/zephyr
rm -rf build
west build -b nrf52840dk/nrf52840
cd ../../..
python3 -m fw_context_mcp.cli index --no-refs --no-analyze \
    --project tests/builds/zephyr tests/builds/zephyr/build/zephyr/compile_commands.json

# mbed-os — build with bear + mbed compile
export PYENV_VERSION=3.11.8
export PATH="$HOME/.pyenv/versions/3.11.8/bin:$HOME/.local/bin:$PATH"
cd tests/builds/mbed_os
bear -- mbed compile -t GCC_ARM -m NUCLEO_F429ZI --clean
cd ../../..
python3 -m fw_context_mcp.cli index --no-refs --no-analyze \
    --project tests/builds/mbed_os tests/builds/mbed_os/compile_commands.json

# esp-idf — build first, then index
source ~/.espressif/tools/activate_idf_v5.2.5.sh
cd tests/builds/esp_idf
rm -rf build
idf.py set-target esp32
idf.py build
cd ../../..
python3 -m fw_context_mcp.cli index --no-refs --no-analyze \
    --project tests/builds/esp_idf tests/builds/esp_idf/build/compile_commands.json
```

## Analyzing test output

### Check indexed symbols

```bash
# List all functions defined in the project
python3 -c "
from fw_context_mcp.indexer.db import open_db
from fw_context_mcp.config import derive_project_id, load
from pathlib import Path
proj = Path('tests/builds/bare').resolve()
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
proj = Path('tests/builds/bare').resolve()
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

```bash
python3 -c "
import json
from pathlib import Path
cc = Path('tests/builds/bare/compile_commands.json')
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
├── bare/                     # C project, manual mode
│   ├── .fw-context/config.toml
│   └── src/{main.c,lib.c,lib.h}
├── generic_cmake/            # CMake C project
│   ├── .fw-context/config.toml
│   ├── CMakeLists.txt
│   └── src/main.c
├── makefile/                 # Makefile C project
│   ├── .fw-context/config.toml
│   ├── Makefile
│   └── src/main.c
├── platformio/               # STM32L4 project
│   ├── platformio.ini
│   └── src/main.c
├── mbed_os/                  # Mbed OS 6 project
│   ├── .mbed
│   ├── CMakeLists.txt         # (alternative CMake build path)
│   ├── main.cpp               # must be in root for mbed-cli detection
│   ├── mbed-os.lib
│   ├── mbed_app.json
│   ├── mbed-os -> <symlink>
│   └── src/                   # additional source files
├── arduino/                  # Arduino AVR sketch
│   ├── .fw-context/config.toml
│   ├── arduino.ino
│   └── sketch.yaml
├── zephyr/                   # Zephyr RTOS project
│   ├── CMakeLists.txt
│   ├── prj.conf
│   ├── west.yml
│   └── src/main.c
├── esp_idf/                  # ESP-IDF v5.2.5 project
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

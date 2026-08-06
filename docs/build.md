# Build Configuration

`fw-context` generates `compile_commands.json` automatically for 11 build
systems.  Configure everything in `.fw-context/config.toml` under the
`[build]` section — no shell one-liners needed.

## How it works

When you run `fw-context index --build`, the system picks one of four paths:

| Path | Method | Used by |
|------|--------|---------|
| **Shell override** | `[build] command = "…"` | Any build system — highest priority |
| **Convert** | Parses project file, no build | Keil MDK, IAR EWARM |
| **Generate** | Generates `compile_commands.json` from flags | Makefile (compiledb), bare/manual |
| **Build** | Full build via build system | PlatformIO, Zephyr, Mbed OS, ESP-IDF, CMake, Arduino |

The path is chosen automatically based on the builder's capabilities and your
configuration.  You don't need to know which path will be used — just configure
the relevant parameters and `fw-context` does the rest.

### Detection order

When `system` is not set explicitly, `fw-context` auto-detects from project
markers in this order (higher = checked first):

1. Mbed OS (`.mbed`, `mbed-os/`, `mbed_app.json`)
2. PlatformIO (`platformio.ini`)
3. Zephyr (`west.yml`, `zephyr/`)
4. ESP-IDF (`sdkconfig` + `CMakeLists.txt` with `idf_build`)
5. Arduino (`.ino`, `sketch.yaml`)
6. Generic CMake (`CMakeLists.txt`)
7. Keil MDK (`*.uvprojx`)
8. IAR EWARM (`*.ewp`, `*.eww`)
9. Makefile (`Makefile`)
10. STM32CubeIDE (`.cproject`, `.project`)
11. TI CCS (`.projectspec`)

The first builder that matches wins.  Set `system` explicitly to skip
detection or force a specific builder.

## Configuration reference

### General

| Parameter | Type | Default | Systems | Description |
|-----------|------|---------|---------|-------------|
| `system` | `str` | (auto-detect) | all | Build system: `mbed-os`, `zephyr`, `platformio`, `esp-idf`, `arduino`, `cmake`, `keil-mdk`, `iar-ewarm`, `makefile`, `bare` |
| `clean` | `bool` | `true` | all | Clean build before generating. Recommended — ensures complete `compile_commands.json`. |
| `command` | `str` | — | all | Full shell command override. Highest priority — all other settings are ignored. |
| `python` | `str` | (auto-detect) | all | Python interpreter for pip-based CLI tools (`mbed-cli`, `platformio`, `keil2clangd`, `compiledb`). Auto-detected by `fw-context init` from pyenv, venv, and common install paths. Set manually when detection fails. |
| `activate` | `str` | (auto-detect) | all | Shell script sourced before build (e.g. `nordic_minimal_setup.sh` for NCS, `export.sh` for ESP-IDF). Auto-detected by `fw-context init` from common install paths. Set manually when detection fails. |
| `pre_build` | `str` | — | all | Shell command run BEFORE build/convert/generate. **Security: use only in `local.toml` (gitignored), never in committed `config.toml`.** |

### Mbed OS

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target` | `str` | (from `.mbed`) | Target board name, e.g. `"P_ECB_BOARD"` |
| `toolchain` | `str` | (from `.mbed`) | Toolchain, e.g. `"GCC_ARM"` |
| `profile` | `str` | `"develop"` | Build profile |
| `app_config` | `str` | `"mbed_app.json"` | Application config JSON |
| `extra_profiles` | `list[str]` | `["lto.json"]` | Additional profiles merged on top |
| `defines` | `list[str]` | `[]` | Extra `-D` macros passed to compiler |

### Zephyr

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `board` | `str` | — | **Required.** Board name, e.g. `"nrf52840dk_nrf52840"` |

### PlatformIO

No extra parameters needed — everything is in `platformio.ini`.

### ESP-IDF

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `idf_path` | `str` | (from `$IDF_PATH`) | Path to ESP-IDF installation |

### Arduino

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fqbn` | `str` | — | **Required.** Fully Qualified Board Name, e.g. `"arduino:avr:uno"` |

### Generic CMake

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `cmake_generator` | `str` | — | Generator, e.g. `"Ninja"`, `"Unix Makefiles"` |

### Keil MDK (convert path)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `keil_project` | `str` | (first `*.uvprojx`) | Path to `.uvprojx` file |
| `keil_target` | `str` | — | Target name within the project |
| `keil_cmsis_path` | `str` | — | Path to CMSIS headers |

### IAR EWARM (convert path)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `iar_project` | `str` | (first `*.ewp`) | Path to `.ewp` file |
| `iar_target` | `str` | — | Target name within the project |

### Makefile (generate via compiledb)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `makefile` | `str` | `"Makefile"` | Path to Makefile (relative to project root) |
| `make_target` | `str` | `"all"` | Build target |
| `make_vars` | `dict[str,str]` | `{}` | Extra variables, e.g. `{V: "1"}` |
| `make_dry_run` | `bool` | `true` | Use `make -n` — no real compilation |

### Manual / bare mode (generate from flags)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `source_dirs` | `list[str]` | — | Directories scanned for `.c`/`.cpp` files |
| `include_dirs` | `list[str]` | `[]` | Directories added via `-I` |
| `system_include_dirs` | `list[str]` | `[]` | Directories added via `-isystem` |
| `defines` | `list[str]` | `[]` | Preprocessor macros (`-D` flags) |
| `extra_flags` | `list[str]` | `[]` | Extra compiler flags (e.g. `-mcpu=cortex-m4`) |
| `compiler` | `str` | `"gcc"` | Compiler executable name |

### Toolchain (shared by Keil, IAR, Makefile)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `toolchain_path` | `str` | — | Path to toolchain bin directory |
| `toolchain_prefix` | `str` | — | Prefix, e.g. `"arm-none-eabi-"` |

## Examples

### 1. PlatformIO (ESP32)

Most PlatformIO projects need nothing — everything is auto-detected:

```toml
[build]
# Auto-detection from platformio.ini — almost nothing needed
# system = "platformio"   # optional, auto-detection works
clean = false              # incremental build is faster
```

`fw-context index --build` runs `pio run --target compiledb`.  Dependency
tracking (`-MMD`) is configured automatically.

### 2. Zephyr (nRF52840)

```toml
[build]
system = "zephyr"
board = "nrf52840dk_nrf52840"
clean = true
```

Build: `west build -b nrf52840dk_nrf52840`.  CMake generates
`compile_commands.json` automatically (`CMAKE_EXPORT_COMPILE_COMMANDS=ON`).

### 3. Mbed OS (custom target)

```toml
[build]
system = "mbed-os"
target = "P_ECB_BOARD"
toolchain = "GCC_ARM"
profile = "develop"
extra_profiles = ["lto.json"]
defines = ["VERSION_FW_MAJOR=4", "DEV"]
clean = true
```

`target` and `toolchain` are normally auto-detected from `.mbed`, override
here when needed.  `defines` adds `-D` flags for all compilation units.

### 4. Keil MDK (STM32F4)

```toml
[build]
system = "keil-mdk"
keil_project = "Project.uvprojx"
keil_target = "STM32F407VG"
keil_cmsis_path = "C:/Keil_v5/ARM/PACK/ARM/CMSIS/5.9.0/CMSIS"
toolchain_path = "C:/Keil_v5/ARM/ARMCLANG/bin"
```

**No build needed.**  `keil2clangd` parses the `.uvprojx` XML and generates
`compile_commands.json` directly.  Requires `pip install keil2clangd`.

### 5. Arduino CLI (AVR)

```toml
[build]
system = "arduino"
fqbn = "arduino:avr:uno"
clean = false
```

`fqbn` is required — find it with `arduino-cli board list`.  Build:
`arduino-cli compile --fqbn arduino:avr:uno --export-compile-commands`.

### 6. Manual / bare — ARM project without a build system

```toml
[build]
system = "bare"
compiler = "arm-none-eabi-gcc"
include_dirs = [
    "include",
    "lib/CMSIS/Core/Include",
    "lib/STM32F4xx_HAL_Driver/Inc",
]
system_include_dirs = [
    "/opt/gcc-arm-none-eabi/arm-none-eabi/include",
]
defines = [
    "USE_HAL_DRIVER",
    "STM32F407xx",
    "HSE_VALUE=8000000",
]
extra_flags = [
    "-mcpu=cortex-m4",
    "-mthumb",
    "-mfloat-abi=hard",
    "-mfpu=fpv4-sp-d16",
    "-std=c11",
]
source_dirs = ["src", "lib"]
```

`fw-context` generates one `compile_commands.json` entry per `.c`/`.cpp` file
in `source_dirs`, each with the same flags.  Good for small projects without
a real build system.

### 7. Makefile with compiledb (dry-run)

```toml
[build]
system = "makefile"
makefile = "Makefile"
make_target = "all"
make_vars = { V = "1", CROSS_COMPILE = "arm-none-eabi-" }
make_dry_run = true
```

Runs `compiledb -n make -C <root> V=1 CROSS_COMPILE=arm-none-eabi- all`.
Dry-run mode — no real compilation.  Set `make_dry_run = false` when the
project needs generated headers first.  Requires `pip install compiledb`.

### 8. ESP-IDF with explicit path

```toml
[build]
system = "esp-idf"
idf_path = "/home/user/esp/esp-idf"
clean = false
```

`idf_path` is optional when `idf.py` is on `$PATH`.  Set it here for
non-standard installations.

### 9. IAR EWARM with pre-build hook

```toml
# In .fw-context/local.toml (NOT config.toml — pre_build is potentially dangerous)
[build]
system = "iar-ewarm"
iar_project = "Project.ewp"
iar_target = "Debug"
toolchain_path = "C:/IAR/arm"
pre_build = "python3 tools/generate_version_header.py"
```

Before conversion, `pre_build` runs a script that generates `version.h`.
**`pre_build` must stay in `local.toml`** — a committed `config.toml` with
`pre_build` is a security risk (anyone with commit access could run arbitrary
commands on other developers' machines).

### 10. CMake with Ninja

```toml
[build]
system = "cmake"
cmake_generator = "Ninja"
clean = true
```

Generic CMake builder.  Runs:
`cmake -B build -G Ninja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`.
Works for any CMake project outside Zephyr/ESP-IDF.

### 11. Shell override — bear wrapping a custom build

```toml
[build]
command = "bear -- make -j8"
```

When nothing else fits, `command` runs as-is.  `bear` intercepts the build
and records compile commands.  All other `[build]` settings are ignored.
Requires `bear` (system package: `sudo pacman -S bear`).

### 12. Build environment — custom Python for mbed-cli

Mbed OS CLI requires Python 3.11 or older.  If your system Python is newer,
install mbed-cli into a pyenv or venv and point fw-context at it.

```toml
# In .fw-context/local.toml (gitignored, machine-specific)
[build]
python = "/home/user/.pyenv/versions/3.11.8/bin/python"
```

`fw-context init` auto-detects this from pyenv, `~/mbed_venv`, and
`~/.local/bin/mbed`.  Set manually only when detection fails.

With `python` set, the builder runs:
```
bear -- /path/to/python -m mbed compile -t GCC_ARM -m TARGET ...
```

### 13. Build environment — activation script for Zephyr/NCS

Nordic nRF Connect SDK (and similarly ESP-IDF) needs a toolchain setup script
sourced before `west build`.

```toml
# In .fw-context/local.toml (gitignored, machine-specific)
[build]
activate = "/home/user/ncs_tools/nordic_minimal_setup.sh"
```

`fw-context init` auto-detects this from paths like
`~/ncs_tools/nordic_minimal_setup.sh`, `west config zephyr.base`, and
`~/zephyr-sdk-*/environment-setup-*`.

With `activate` set, the builder wraps the command:
```
bash -c "source /path/to/setup.sh && west build -b nrf52840dk ..."
```

### 14. Build environment — ESP-IDF

```toml
# In .fw-context/local.toml
[build]
activate = "/home/user/esp/esp-idf/export.sh"
```

`fw-context init` checks `$IDF_PATH`, `~/esp/esp-idf/export.sh`, and
`idf.py` on PATH.

## Troubleshooting

### `compile_commands.json` is empty

The build produced no compilation units.  Try:
- Set `clean = true` and re-run
- Check that the project actually compiles with the configured toolchain
- For Makefile projects, try `make_dry_run = false` if the project needs
  a real build to generate headers

### `Keil project not found`

Check the `keil_project` path — it must be relative to the project root:
```toml
keil_project = "Project.uvprojx"   # file at <root>/Project.uvprojx
```
Also check that `*.uvprojx` exists with `ls *.uvprojx`.

### `arduino-cli: command not found`

```bash
pip install arduino-cli
```

### `west: command not found`

Install the Zephyr SDK and make sure `west` is on `$PATH`:
```bash
pip install west
west init ~/zephyrproject
```

### `bear: command not found`

`bear` is a system package, not a Python package:
```bash
sudo pacman -S bear        # Arch / Manjaro
sudo apt install bear      # Debian / Ubuntu
```

### `compiledb: command not found`

```bash
pip install compiledb
```

### `keil2clangd: command not found`

```bash
pip install keil2clangd
```

### `mbed: command not found` (Mbed OS)

Mbed CLI requires Python 3.11 or older.  If you see this error:
```
RuntimeError: bear is required ... (or: Build command failed: mbed compile)
```

**Auto-detection:** Run `fw-context init` — it scans pyenv versions
(`~/.pyenv/versions/*/bin/mbed`) and common venv paths for a working
mbed-cli installation and writes the path to `local.toml`.

**Manual setup:**
```bash
# Using pyenv:
pyenv install 3.11.8
pyenv shell 3.11.8
pip install mbed-cli

# Or using a venv:
python3.11 -m venv ~/mbed_venv
~/mbed_venv/bin/pip install mbed-cli
```
Then set in `.fw-context/local.toml`:
```toml
[build]
python = "/home/user/.pyenv/versions/3.11.8/bin/python"
```

### `west: command not found` (Zephyr/NCS)

Zephyr builds need an activated toolchain environment.  `fw-context init`
auto-detects common setup scripts (`~/ncs_tools/nordic_minimal_setup.sh`,
`west config zephyr.base`, `~/zephyr-sdk-*/environment-setup-*`).

If detection fails, set the activation script manually in `.fw-context/local.toml`:
```toml
[build]
activate = "/home/user/ncs_tools/nordic_minimal_setup.sh"
```

### Pre-build hook security warning

If you see this warning:
```
⚠ SECURITY: pre_build is set in .fw-context/config.toml (committed).
```
Move the `pre_build` line to `.fw-context/local.toml` (gitignored).  Committed
`pre_build` is a security risk — it runs arbitrary shell commands on every
developer's machine that clones the repository.

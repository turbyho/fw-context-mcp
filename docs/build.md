# Build Configuration

`fw-context` generates `compile_commands.json` automatically, for 11 build
systems. Configure everything in `.fw-context/config.toml`, under the
`[build]` section. You do not need shell one-liners.

## How it works

When you run `fw-context index --build`, the system picks one of four paths:

| Path | Method | Used by |
|------|--------|---------|
| **Shell override** | `[build] command = "…"` | Any build system, highest priority |
| **Convert** | Parses project file, no build | Keil MDK, IAR EWARM |
| **Generate** | Generates `compile_commands.json` from flags | Makefile (compiledb), bare/manual |
| **Build** | Full build via build system | PlatformIO, Zephyr, Mbed OS, ESP-IDF, CMake, Arduino |

`fw-context` chooses the path automatically, based on the builder's
capabilities and your configuration. You do not need to know which path
`fw-context` uses. Just configure the relevant parameters, and `fw-context`
does the rest.

The generated `compile_commands.json` is written to
`.fw-context/build/compile_commands.json` — a gitignored subdirectory of the
project config dir, so the build artifact stays out of the repository root.

A build does not write that file directly. It writes a staging file in the
same directory, `.compile_commands.<pid>@<tag>.json`, and one rename then
gives `compile_commands.json` the new content. Thus a build that fails keeps
the file that the last good build wrote. A reader never gets a database that
a build still writes.

A multi-variant project writes one file for each build, in the same
directory. A variant without sysbuild writes
`compile_commands.<variant>.json`, with the same staging rename. A Zephyr
sysbuild image gets `compile_commands.<variant>.<image>.json`, which
fw-context copies from the build directory of the image. The copy goes to
a temporary file first, and one rename then replaces the target, thus a
reader never gets a part of it.

Only a build that a signal stops leaves a staging file. The next build in
that directory deletes it when its process no longer runs. The `<tag>` is
the host name and the PID namespace of the build. A PID of a container
names no process on the host, thus a build deletes only the files of its
own PID namespace.

`fw-context init` writes the rules for it into `.gitignore` automatically:

```gitignore
**/.fw-context/*
!**/.fw-context/config.toml
```

Everything under `.fw-context/` stays out of the repository, and
`config.toml` is the one exception — it holds the build configuration of the
project, thus every developer gets the same index. Keep the two lines in this
order: a later line wins in `.gitignore`.

The `/*` matters. A rule `.fw-context/` excludes the DIRECTORY, git does not
descend into an excluded directory, and no later negation can then bring
`config.toml` back. The `**/` prefix gives the rules every depth, for a
repository that holds more than one initialized project. `init` removes a
line of the older form when it finds one.

### Detection order

When you do not set `system` explicitly, `fw-context` detects the build
system automatically, from project markers, in this order. `fw-context`
checks a higher item first:

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

The first builder that matches, wins. Set `system` explicitly, to skip
detection or to force a specific builder.

## Automatic build

A `fw-context index` run without `--build` can start a build on its own.
It does this only when an index exists and a build can repair something
that a reindex cannot:

- Source files are on disk that `compile_commands.json` does not cover.
- The tree is on a different git branch than the index.

A build that fw-context starts and that fails stops the automatic build
for the same files, or the same branch, for 30 minutes. An explicit
`fw-context index --build` ignores this backoff. For the triggers of the
background runs, see [`fw-context index`](tools.md#fw-context-index).

fw-context starts the build only for a backend that cannot damage the
output of your own build. Such a backend puts its artifacts into a
directory of its own, or it compiles nothing:

| Backend | Automatic build | Why |
|---------|-----------------|-----|
| Mbed OS | yes | `mbed compile --build <dir>` |
| PlatformIO | yes | `PLATFORMIO_BUILD_DIR` |
| Zephyr | yes | `west build -d <dir>` |
| ESP-IDF | yes | `idf.py -B <dir>` |
| Arduino | yes | `arduino-cli compile --build-path <dir>` |
| Generic CMake | yes | configure and build use the chosen build directory |
| Keil MDK, IAR EWARM | yes | convert only, no compilation |
| Manual / bare | yes | `.d` files go to a directory of fw-context |
| Makefile | only with `make_dry_run = true` (default) | a real `make` owns its output directory |
| STM32CubeIDE, TI CCS | never | fw-context cannot build these projects |

The automatic build writes into `.fw-context/autobuild/<variant>`, or
`.fw-context/autobuild/default` for a project without variants. fw-context
writes `.fw-context/autobuild/.gitignore` with the line `*`, thus the
output stays out of git also in a project that `fw-context init` set up
before this directory existed.

Exceptions:

- PlatformIO: `pio run -t compiledb` also rewrites
  `<project>/compile_commands.json`. fw-context cannot move that file
  without a change to `platformio.ini`.
- ESP-IDF: fw-context runs `idf.py set-target` only when the project has
  no `sdkconfig`. `set-target` renames `<project>/sdkconfig`, and `-B`
  does not move that file.
- Manual / bare: the `.d` files go to the isolated build directory. In a
  build that you start, they go to `.fw-context/build/deps`. They never go
  next to the source.

## Configuration reference

### General

| Parameter | Type | Default | Systems | Description |
|-----------|------|---------|---------|-------------|
| `system` | `str` | (auto-detect) | all | Build system: `mbed-os`, `zephyr`, `platformio`, `esp-idf`, `arduino`, `cmake`, `keil-mdk`, `iar-ewarm`, `makefile`, `bare` |
| `clean` | `bool` | `true` | all | Run a clean build before fw-context generates the file. **Recommended.** A clean build ensures a complete `compile_commands.json` file. |
| `command` | `str` | — | all | A full override for the shell command. Highest priority: fw-context ignores all other settings. |
| `python` | `str` | (auto-detect) | all | The Python interpreter for pip-based CLI tools, such as `mbed-cli`, `platformio`, `keil2clangd`, or `compiledb`. `fw-context init` detects this automatically, from pyenv, venv, and common install paths. Set this parameter manually when automatic detection fails. |
| `activate` | `str` | (auto-detect) | all | A shell script that fw-context sources before the build, for example `nordic_minimal_setup.sh` for NCS, or `export.sh` for ESP-IDF. `fw-context init` detects this automatically, from common install paths. Set this parameter manually when automatic detection fails. |
| `pre_build` | `str` | — | all | A shell command that fw-context runs before the build, the convert step, or the generate step. **Security: use this parameter only in `local.toml` (gitignored). Never use this parameter in a committed `config.toml` file.** |

The build process does not get an inherited `BASH_ENV`. fw-context removes
it from the build environment, because `bash -c` reads that file before
the command, and `activate` runs through `bash -c`. If a build needs
`BASH_ENV`, set it in `[build] env` or in `[build] extra_env`
(`local.toml`). A configured value is applied after the removal.

`[build] env`, `extra_path` and `extra_env` apply to every command that
the build runs: the builder commands, the `pre_build` hook, and the
`command` override. `activate` applies to the builder commands only.

### Mbed OS

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target` | `str` | (from `.mbed`) | The target board name, for example `"BOARD_V2_BOARD"` |
| `toolchain` | `str` | (from `.mbed`) | The toolchain, for example `"GCC_ARM"` |
| `profile` | `str` | `"develop"` | The build profile |
| `app_config` | `str` | `"mbed_app.json"` | The application configuration JSON file |
| `extra_profiles` | `list[str]` | `["lto.json"]` | Additional profiles that fw-context merges on top |
| `defines` | `list[str]` | `[]` | Extra `-D` macros that fw-context passes to the compiler |

### Zephyr

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `board` | `str` | — | **Required.** The board name, for example `"nrf52840dk_nrf52840"` |

### PlatformIO

This build system needs no extra parameters. Everything is in `platformio.ini`.

### ESP-IDF

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `idf_path` | `str` | (from `$IDF_PATH`) | The path to the ESP-IDF installation |

### Arduino

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fqbn` | `str` | — | **Required.** The Fully Qualified Board Name, for example `"arduino:avr:uno"` |

### Generic CMake

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `cmake_generator` | `str` | — | The CMake generator, for example `"Ninja"` or `"Unix Makefiles"` |

### Keil MDK (convert path)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `keil_project` | `str` | (first `*.uvprojx`) | The path to the `.uvprojx` file |
| `keil_target` | `str` | — | The target name within the project |
| `keil_cmsis_path` | `str` | — | The path to the CMSIS headers |

### IAR EWARM (convert path)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `iar_project` | `str` | (first `*.ewp`) | The path to the `.ewp` file |
| `iar_target` | `str` | — | The target name within the project |

### Makefile (generate via compiledb)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `makefile` | `str` | `"Makefile"` | The path to the Makefile, relative to the project root |
| `make_target` | `str` | `"all"` | The build target |
| `make_vars` | `dict[str,str]` | `{}` | Extra variables, for example `{V: "1"}` |
| `make_dry_run` | `bool` | `true` | Use `make -n`. This setting runs no real compilation |

### Manual / bare mode (generate from flags)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `source_dirs` | `list[str]` | — | Directories that fw-context scans for `.c` and `.cpp` files |
| `include_dirs` | `list[str]` | `[]` | Directories that fw-context adds with `-I` |
| `system_include_dirs` | `list[str]` | `[]` | Directories that fw-context adds with `-isystem` |
| `defines` | `list[str]` | `[]` | Preprocessor macros (`-D` flags) |
| `extra_flags` | `list[str]` | `[]` | Extra compiler flags, for example `-mcpu=cortex-m4` |
| `compiler` | `str` | `"gcc"` | The compiler executable name |

### Toolchain (shared by Keil, IAR, Makefile)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `toolchain_path` | `str` | — | The path to the toolchain `bin` directory |
| `toolchain_prefix` | `str` | — | The prefix, for example `"arm-none-eabi-"` |

## Multi-project and multi-image builds

One workspace can build several boards or images. Each board or image is
a **build variant**. You declare each variant with `[[build.variants]]` in
`.fw-context/config.toml`. Each `(variant, image)` pair becomes one
indexed build, with its own `config_hash`.

**Why use variants.** You index every board in one run. A query names one
variant, and one image of it. To compare two builds, ask once for each
build. You do not need one checkout per board.

### Concepts

- **Variant** — one build configuration, for one board, target, or environment.
- **Image** — one sysbuild image inside a variant (Zephyr only). An
  example is the `app` image and the `mcuboot` bootloader image.

### `[[build.variants]]` reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | `str` | — | **Required.** A unique key. The query tools and the CLI reference this key. |
| `board` | `str` | — | The board, target, or chip label for this variant. This value overrides `[build] board`. |
| `description` | `str` | — | A human-readable description. |
| `build_dir` | `str` | `build/<name>` | The build output directory for this variant. |
| `env` | `dict` | — | Build environment variables. fw-context folds these variables into the `config_hash`. |
| `images` | `list` | — | The sysbuild images (Zephyr only). |
| *(any other `[build]` key)* | — | — | Overrides the shared `[build]` value for this variant only. |

The `images` sub-table:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | `str` | — | The image name. |
| `dir` | `str` | — | The path to the image source. |
| `type` | `str` | `"project"` | `"project"` or `"sdk"`. |
| `board` | `str` | — | A per-image board override. |

Shared `[build]` keys for multi-variant projects:

| Key | Default | Description |
|-----|---------|-------------|
| `default_variant` | — | The variant that a query uses when it omits `variant`. |
| `default_image` | — | The image that `get_active_build` reports as `active_image`. A query does not use this key: when a variant has more than one image, the query must name `image`. |
| `sysbuild` | `false` | Use `west build --sysbuild` (Zephyr). |
| `source_dir` | — | The sysbuild input application directory (Zephyr). |

Variant overrides merge with the shared `[build]` section by type. A
scalar value (such as `board`) replaces the shared value. A list value
(such as `defines`) replaces the shared list. A dict value (such as
`env`) merges into the shared dict.

### Index the variants

```bash
fw-context index --build                          # build and index every variant
fw-context index --build --variant nrf52840-dev   # one variant
fw-context index --build --variants a,b           # a list of variants
fw-context index --image app                      # one image only
fw-context index --exclude-image mcuboot          # skip one image
```

With `--build`, a variant without sysbuild writes
`.fw-context/build/compile_commands.<variant>.json`. A Zephyr sysbuild
variant writes `.fw-context/build/compile_commands.<variant>.<image>.json`
for each image. A later run without `--build` reuses the files: the file of
each variant without sysbuild, and `<build_dir>/<image>/compile_commands.json`
for each sysbuild image.

A variant name that cannot be part of a file name, for example a name with
`/`, fails that variant only. The other variants build and index.

### Manage the variants

```bash
fw-context init-variants list
fw-context init-variants add --name <name> --board <board>
fw-context init-variants remove <name>
```

## Examples

### 1. PlatformIO (ESP32)

Most PlatformIO projects need no configuration. fw-context detects everything automatically:

```toml
[build]
# Auto-detection from platformio.ini — almost nothing needed
# system = "platformio"   # optional, auto-detection works
clean = false              # incremental build is faster
```

`fw-context index --build` runs `pio run --target compiledb`. fw-context
configures dependency tracking (`-MMD`) automatically.

The build also runs `pio run --target envdump`. This target compiles
nothing. It prints the SCons environment, and fw-context reads the link
options from it: the linker scripts (`-T`, `--default-script`) and the
`--defsym` values. SCons writes no file that holds the link command, thus
this step is the only source of the memory map and of the linker-script
symbols of a PlatformIO build. fw-context records the result in
`.fw-context/build/platformio_link.json`.

Each `pio run` also runs the `extra_scripts` of the project, thus the
`envdump` step runs them one more time. An index run without `--build`
reads the record of the last build and does not run `pio`.

### 2. Zephyr (nRF52840)

```toml
[build]
system = "zephyr"
board = "nrf52840dk_nrf52840"
clean = true
```

Build command: `west build -b nrf52840dk_nrf52840`. CMake generates
`compile_commands.json` automatically, with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`.

### 3. Mbed OS (custom target)

```toml
[build]
system = "mbed-os"
target = "BOARD_V2_BOARD"
toolchain = "GCC_ARM"
profile = "develop"
extra_profiles = ["lto.json"]
defines = ["VERSION_FW_MAJOR=4", "DEV"]
clean = true
```

fw-context normally detects `target` and `toolchain` automatically, from
`.mbed`. Override these parameters here when needed. `defines` adds `-D`
flags for all compilation units.

### 4. Keil MDK (STM32F4)

```toml
[build]
system = "keil-mdk"
keil_project = "Project.uvprojx"
keil_target = "STM32F407VG"
keil_cmsis_path = "C:/Keil_v5/ARM/PACK/ARM/CMSIS/5.9.0/CMSIS"
toolchain_path = "C:/Keil_v5/ARM/ARMCLANG/bin"
```

**This build system needs no build step.** `keil2clangd` parses the
`.uvprojx` XML file, and generates `compile_commands.json` directly. This
build system requires `pip install keil2clangd`.

### 5. Arduino CLI (AVR)

```toml
[build]
system = "arduino"
fqbn = "arduino:avr:uno"
clean = false
```

`fqbn` is required. Find your board's `fqbn` with `arduino-cli board list`.
Build command: `arduino-cli compile --fqbn arduino:avr:uno --export-compile-commands`.

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

`fw-context` generates one `compile_commands.json` entry for each `.c` or
`.cpp` file in `source_dirs`, with the same flags for each entry. This mode
works well for small projects that have no real build system.

### 7. Makefile with compiledb (dry-run)

```toml
[build]
system = "makefile"
makefile = "Makefile"
make_target = "all"
make_vars = { V = "1", CROSS_COMPILE = "arm-none-eabi-" }
make_dry_run = true
```

This example runs `compiledb -n make -C <root> V=1 CROSS_COMPILE=arm-none-eabi- all`.
Dry-run mode runs no real compilation. Set `make_dry_run = false` when the
project needs generated headers first. This build system requires
`pip install compiledb`.

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

Before the conversion step, `pre_build` runs a script that generates
`version.h`. **You must keep `pre_build` only in `local.toml`.** A committed
`config.toml` file with `pre_build` is a security risk. Anyone with commit
access could run arbitrary commands on another developer's machine.

`pre_build`, `command` and each build command run in a new session with no
controlling terminal. Thus fw-context can stop the whole build, the
compilers included. Output to the terminal still works. A hook that opens
`/dev/tty` fails: a password prompt (`sudo`, `ssh`) or a curses screen
(`menuconfig`) does not work there.

### 10. CMake with Ninja

```toml
[build]
system = "cmake"
cmake_generator = "Ninja"
clean = true
```

This is the generic CMake builder. This builder runs
`cmake -B build -G Ninja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`. This builder
works for any CMake project that is not Zephyr or ESP-IDF.

### 11. Shell override — bear wrapping a custom build

```toml
[build]
command = "bear -- make -j8"
```

When no other builder fits, `command` runs as-is. `bear` intercepts the
build, and records the compile commands. fw-context ignores all other
`[build]` settings. This example requires `bear`, as a system package:
`sudo pacman -S bear`.

### 12. Build environment — custom Python for mbed-cli

Mbed OS CLI requires Python 3.11 or older. If your system Python is newer,
install mbed-cli into a pyenv or a venv. Then point fw-context at that
Python interpreter.

```toml
# In .fw-context/local.toml (gitignored, machine-specific)
[build]
python = "/home/user/.pyenv/versions/3.11.8/bin/python"
```

`fw-context init` detects this automatically, from pyenv, `~/mbed_venv`, and
`~/.local/bin/mbed`. Set this parameter manually only when automatic
detection fails.

When you set `python`, the builder runs:
```
bear -- /path/to/python -m mbed compile -t GCC_ARM -m TARGET ...
```

### 13. Build environment — activation script for Zephyr/NCS

The Nordic nRF Connect SDK, and ESP-IDF in a similar way, needs a toolchain
setup script. fw-context must source this script before `west build`.

```toml
# In .fw-context/local.toml (gitignored, machine-specific)
[build]
activate = "/home/user/ncs_tools/nordic_minimal_setup.sh"
```

`fw-context init` detects this automatically, from paths such as
`~/ncs_tools/nordic_minimal_setup.sh`, `west config zephyr.base`, and
`~/zephyr-sdk-*/environment-setup-*`.

When you set `activate`, the builder wraps the command:
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

### 15. Multi-variant — one codebase, several boards (bare mode)

One `bare` build compiles the same sources for two boards. Each variant
overrides the board and the preprocessor defines:

```toml
[build]
system = "bare"
compiler = "arm-none-eabi-gcc"
source_dirs = ["src", "lib"]
include_dirs = ["include"]
default_variant = "board-a"

[[build.variants]]
name = "board-a"
board = "STM32F407xx"
defines = ["USE_HAL_DRIVER", "STM32F407xx"]

[[build.variants]]
name = "board-b"
board = "STM32F103xx"
defines = ["USE_HAL_DRIVER", "STM32F103xx"]
```

### 16. Multi-variant — Mbed OS targets

One Mbed OS project builds two targets. Each variant overrides the target
and the hardware revision define:

```toml
[build]
system = "mbed-os"
toolchain = "GCC_ARM"

[[build.variants]]
name = "target-a"
target = "BOARD_V2_BOARD"
defines = ["HW_REV=1"]

[[build.variants]]
name = "target-b"
target = "OTHER_BOARD"
defines = ["HW_REV=2"]
```

### 17. Multi-variant and multi-image — Zephyr sysbuild

A Zephyr sysbuild project builds one variant with two images: the `app`
image and the `mcuboot` bootloader image. Each image becomes a separate
indexed build:

```toml
[build]
system = "zephyr"
sysbuild = true
source_dir = "proj/app"
default_variant = "nrf52840-dev"

[[build.variants]]
name = "nrf52840-dev"
board = "nrf52840dk/nrf52840"
build_dir = "build/nrf52840_sysbuild"
env = { BOARD_ENV = "DEV" }
images = [
  { name = "app",      dir = "proj/app",                  type = "project" },
  { name = "mcuboot",  dir = "${NCS}/bootloader/mcuboot", type = "sdk" },
]
```

## Troubleshooting

### `compile_commands.json` is empty

The build produced no compilation units. Try:
- Set `clean = true`, and re-run the build
- Check that the project actually compiles with the configured toolchain
- For Makefile projects, try `make_dry_run = false` if the project needs
  a real build to generate headers

### `Keil project not found`

Check the `keil_project` path. This path must be relative to the project root:
```toml
keil_project = "Project.uvprojx"   # file at <root>/Project.uvprojx
```
Also check that `*.uvprojx` exists with `ls *.uvprojx`.

### `arduino-cli: command not found`

```bash
pip install arduino-cli
```

### `west: command not found`

Install the Zephyr SDK. Make sure `west` is on `$PATH`:
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

fw-context looks for `compiledb` in this order:

1. `<[build] python> -m compiledb`, when you set `[build] python`
2. `<interpreter that runs fw-context> -m compiledb`, when that interpreter
   can import `compiledb`
3. `compiledb` on `PATH`

When all three fail, the error names each of them. Install `compiledb`
into the environment that runs fw-context:

```bash
python -m pip install compiledb
```

Or use `bear` with a custom command: `[build] command = "bear -- make"`.

### A build fails or times out

The error quotes the last 2000 characters of `stdout` and of `stderr`. A
stream that is longer says how many characters it holds. An empty stream
is not shown. The cause of a failure is usually at the end of the log.

### `keil2clangd: command not found`

```bash
pip install keil2clangd
```

### `mbed: command not found` (Mbed OS)

Mbed CLI requires Python 3.11 or older. If you see this error:
```
RuntimeError: bear is required ... (or: Build command failed: mbed compile)
```

**Automatic detection:** Run `fw-context init`. This command scans pyenv
versions (`~/.pyenv/versions/*/bin/mbed`) and common venv paths, to find a
working mbed-cli installation. This command writes the path to `local.toml`.

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
Then set this value in `.fw-context/local.toml`:
```toml
[build]
python = "/home/user/.pyenv/versions/3.11.8/bin/python"
```

### `west: command not found` (Zephyr/NCS)

Zephyr builds need an active toolchain environment. `fw-context init`
detects common setup scripts automatically: `~/ncs_tools/nordic_minimal_setup.sh`,
`west config zephyr.base`, and `~/zephyr-sdk-*/environment-setup-*`.

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
Move the `pre_build` line to `.fw-context/local.toml`, which is gitignored.
A committed `pre_build` value is a security risk. This value runs arbitrary
shell commands, on the machine of every developer who clones the repository.

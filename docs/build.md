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

### Where the build goes

Each build that fw-context runs writes into one tree that fw-context owns:

```text
.fw-context/build/
├── .gitignore            "*"
└── <variant>/            one run of the build system; "default" without variants
    ├── out/              the output directory of the build system
    └── platformio_link.json
```

`<variant>` is the name of a `[[build.variants]]` entry. A project without
variants uses `default`. The build system owns `out/`: a pristine build, a
clean, or a PlatformIO checksum change can remove it as a whole. Thus a file
that fw-context writes for a build sits beside `out/`, not in it.

fw-context never reads the build directory of your own build (`build/`,
`.pio/build/`, `BUILD/`). WHY: your next build in an IDE, with `pio run`, or
in a CI script replaces that output, and the index would change without a
word. Also, the check for a missing build is the same for each build
system: the output directory is there, or it is not.

The index reads `compile_commands.json` where the build system writes it in
`out/`. The database stays beside `build.ninja`, which the linker pass reads:

| Build | Database |
|-------|----------|
| One program (CMake, Arduino, Mbed OS, Makefile, Keil, IAR, manual) | `out/compile_commands.json` |
| ESP-IDF, two images | `out/compile_commands.json` (the application), `out/bootloader/compile_commands.json` (the bootloader) |
| Zephyr without sysbuild (upstream Zephyr, or `west build --no-sysbuild`) | `out/compile_commands.json` |
| Zephyr sysbuild, one per image | `out/<image>/compile_commands.json`, for each image that `out/domains.yaml` names |

An older fw-context wrote its build output into `.fw-context/autobuild/`
and directly into `.fw-context/build/`: a copy of each database
(`compile_commands.json`, `compile_commands.<variant>.<image>.json`),
`platformio_link.json` and `deps/`. Each `fw-context index` run that
ends well removes that output, and it logs one line for each path that it
removes. A database that a build of the index reads stays, until a run
indexes that build again: a run that `--variant` narrows leaves the other
variants on their old copies. The file on the command line stays too. A
directory of the build root that holds `out/` is the directory of a
variant and stays, also when its name is `deps`. The first index run
gives the files of the index their new paths, thus it indexes the
project again.

A tool that fw-context gives the output path to (bear, compiledb,
keil2clangd, the manual backend) does not write the file directly. It writes
a staging file in the directory of the variant,
`<variant>/.compile_commands.<pid>@<tag>.json`, and one rename then gives
`out/compile_commands.json` the new content. Thus a reader never gets a
database that a build still writes. The staging file is beside `out/` and
not in it, because `mbed compile --clean` removes `out/` while bear writes.

Only a build that a signal stops leaves a staging file. The next build in
that directory deletes it when its process no longer runs. The `<tag>` is
the host name and the PID namespace of the build. A PID of a container
names no process on the host, thus a build deletes only the files of its
own PID namespace.

A variant name is a directory name. It must not be empty, it must not start
with `.`, and it must not hold `/ \ : * ? " < > |` or a control character.
Two names that differ only in case are an error, because they are one
directory on macOS and Windows.

`fw-context init` writes the rules for the build tree into `.gitignore`
automatically:

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

At the start of each run, fw-context makes sure that the build is there.
The build is not there when `compile_commands.json` does not exist, or when
a `directory` of its entries does not exist. The database is in the output
directory of the build, thus `rm -rf .fw-context/build` removes the two
together. WHY: fw-context asks the compiler of each unit for its system
headers in that directory, as the build runs it. Without the build, each
unit would get wrong system headers and macros.

- A run that you start builds as `--build` does.
- A background run builds when the backend can build on its own (see
  below).
- When fw-context cannot run the build, the run stops with an error and
  does not index. This is true for STM32CubeIDE and TI CCS, for a
  `[build] command` in a background run, and for a background run of a
  backend that compiles in your tree. Run the build yourself (in the IDE,
  or with `fw-context index --build`), then run `fw-context index`.

A run with an explicit `compile_commands.json`, or a project with
`[[build.variants]]`, does not do this check. `get_active_build` does not
do it for such an index either.

A run builds when an index exists and a build can repair something that a
reindex cannot:

- The build is not there. This applies to a background run.
- Source files are on disk that `compile_commands.json` does not cover.
- The tree is on a different git branch than the index.

When a run stops without an index (the build is not there, or the run
failed), fw-context keeps the reason next to the index. Each answer of a
query tool of the project then carries it, and `get_active_build` gives it
as a reason. The next run that ends well with the build there removes it.
When you build outside of fw-context, `get_active_build` finds the build
and removes a reason that said it is not there. A failed automatic build
is not paused: the next background run builds again. The daemon starts
that run when a C/C++ file changes; a change of a build file only (for
example `CMakeLists.txt`) starts no run. For the triggers of the background runs, see
[`fw-context index`](tools.md#fw-context-index).

fw-context starts the build on its own only for a backend that cannot
damage the output of your own build. Such a backend puts its artifacts into
the output directory of fw-context, or it compiles nothing:

| Backend | Automatic build | Output directory |
|---------|-----------------|------------------|
| Mbed OS | yes | `mbed compile --build <out>` |
| PlatformIO | yes | `PLATFORMIO_BUILD_DIR=<out>` |
| Zephyr | yes | `west build -d <out>` |
| ESP-IDF | yes | `idf.py -B <out>` |
| Arduino | yes | `arduino-cli compile --build-path <out>` |
| Generic CMake | yes | `cmake -B <out>` |
| Keil MDK, IAR EWARM | yes | convert only, no compilation |
| Manual / bare | yes | the `.d` files go to `<out>/deps` |
| Makefile | with `make_dry_run = true` (default), or with `out_dir_var` | `make <out_dir_var>=<out>`; a dry run compiles nothing |
| STM32CubeIDE, TI CCS | never | fw-context cannot build these projects |

A build that you start with `--build` uses the same output directory.
fw-context writes `.fw-context/build/.gitignore` with the line `*`, thus the
output stays out of git also in a project that `fw-context init` did not
set up.

Exceptions:

- PlatformIO: `pio run` installs the `lib_deps` into `.pio/libdeps`, as your
  build does. That directory holds the source of the libraries, not build
  output.
- ESP-IDF: fw-context runs `idf.py set-target` only when the project has
  no `sdkconfig`. `set-target` renames `<project>/sdkconfig`, and `-B`
  does not move that file.
- Makefile: a real `make` (`make_dry_run = false`) needs `out_dir_var`, the
  variable that names the output directory in your Makefile.

## Configuration reference

### General

| Parameter | Type | Default | Systems | Description |
|-----------|------|---------|---------|-------------|
| `system` | `str` | (auto-detect) | all | Build system: `mbed-os`, `zephyr`, `platformio`, `esp-idf`, `arduino`, `cmake`, `keil-mdk`, `iar-ewarm`, `makefile`, `bare` |
| `clean` | `bool` | `true` | all | Run a clean build before fw-context generates the file. **Recommended.** A clean build ensures a complete `compile_commands.json` file. |
| `command` | `str` | — | all | A full override for the shell command. Highest priority: fw-context ignores all other settings. |
| `python` | `str` | (auto-detect) | all | The Python interpreter for pip-based CLI tools, such as `mbed-cli`, `platformio`, `keil2clangd`, or `compiledb`. `fw-context init` detects this automatically, from pyenv, venv, and common install paths. Set this parameter manually when automatic detection fails. |
| `activate` | `str` | (auto-detect) | all | A shell script that fw-context sources before the build, for example `nordic_minimal_setup.sh` for NCS, or `export.sh` for ESP-IDF. `fw-context init` detects this automatically, from common install paths. Set this parameter manually when automatic detection fails. |
| `pre_build` | `str` | — | all | A shell command that fw-context runs before the build, the convert step, or the generate step. Put it in `config.toml` when the whole team uses it, or in `local.toml` when it is specific to your machine. |

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

NCS builds with sysbuild also without `--sysbuild`, and `sysbuild = true`
adds the flag for upstream Zephyr. A sysbuild writes `out/domains.yaml`,
which names each image and the default image. fw-context indexes each image
in that file. Each image has its database in `out/<image>/`. The application
image has the name of the application directory, not the name in
`project()`, and it is the default image: a query without `image` gets it.
For example, `hello_world` with `SB_CONFIG_BOOTLOADER_MCUBOOT=y` gives the
images `hello_world` and `mcuboot`. Without `domains.yaml`, the build made
one program, and its database is `out/compile_commands.json`.

While a pristine build has removed `domains.yaml`, a query without `image`
on a build of several images gets an error. Set `[build] default_image` to
remove this gap.

### PlatformIO

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `environment` | `str` | the variant name, or the only environment | The `[env:<name>]` of `platformio.ini` that the build builds with `pio run -e`. |

Each environment of `platformio.ini` is a build of its own, with its own
board and flags. Thus each environment is a variant:

- A project with one environment (the `default_envs`, or the only
  `[env:<name>]`) is a project without variants. Its queries need no
  `variant`.
- A project with more than one environment and no `[[build.variants]]`
  gets one variant for each environment, with the name of the environment.
  A query without `variant` then needs `[build] default_variant`.
- A `[[build.variants]]` entry builds the environment that `environment`
  names (in the entry, or in `[build]`), or else the environment with the
  name of the variant.
- A `compile_commands.json` that you name (on the command line, or in
  `[index] compile_commands`) and a `[build] command` make no variants: they
  name their build themselves.

A variant that has no build yet gets one in each run, also without
`--build`. A background run builds it only when the backend can build on
its own. A run over all variants (no `--variant`, `--image`) removes from
the index the builds that the project no longer makes: a variant that the
config or `platformio.ini` dropped, or an image that a variant no longer
builds. Thus a project that goes from two environments to one gets its new
build, and the queries of the old variants stop.

PlatformIO decides the set (`pio project config`), thus `extends`,
`extra_configs` and `default_envs` apply as in your build.

### ESP-IDF

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `idf_path` | `str` | (from `$IDF_PATH`) | The path to the ESP-IDF installation |

`idf.py build` makes two programs: the application and the second-stage
bootloader. fw-context indexes each one as an image of the build. The name
of an image is the `project_name` in the `project_description.json` of its
build directory: the project name for the application, and `bootloader`
for the bootloader. A query without `image` gets the application, unless
`[build] default_image` names the other image. Use `--image` or
`--exclude-image` to index one of the two.

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
| `make_dry_run` | `bool` | `true` | Use `make -n`. This setting runs no real compilation. `false` runs a real `make`, and it needs `out_dir_var` |
| `out_dir_var` | `str` | — | The variable of the Makefile that names its output directory, for example `"BUILD_DIR"`. A real build gets `<out_dir_var>=<project>/.fw-context/build/<variant>/out`, as an absolute path, thus a recursive `$(MAKE) -C` gets it too. fw-context does not guess the name: without it, `make_dry_run = false` stops the build with an error. Every output of the Makefile must come from this variable: a Makefile that sets it with `override`, or that writes nothing into it, stops the build with an error. A generated file that the Makefile writes elsewhere (for example into `src/`) still goes there. The project path must hold no space and no `$` |

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
- **Image** — one program that one build makes. A Zephyr sysbuild makes,
  for example, the `app` image and the `mcuboot` bootloader image. An
  ESP-IDF build makes the application and the `bootloader` image. A build
  without variants can make several images too.

### `[[build.variants]]` reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | `str` | — | **Required.** A unique key. The query tools and the CLI reference this key. |
| `board` | `str` | — | The board, target, or chip label for this variant. This value overrides `[build] board`. |
| `description` | `str` | — | A human-readable description. |
| `build_dir` | — | — | Retired, here and in `[build]`. Each variant builds into `.fw-context/build/<name>/out`. An index run stops with an error while the key is in the config. |
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

Shared `[build]` keys for multi-variant projects. `default_image` also
applies to a project without variants whose build makes several images
(ESP-IDF):

| Key | Default | Description |
|-----|---------|-------------|
| `default_variant` | — | The variant that a query uses when it omits `variant`. |
| `default_image` | — | The image that a query gets when it does not name `image` and the build holds more than one image. The key applies to each variant that holds an image of this name. Without the key, the query gets the application that the build system names: an ESP-IDF build names its application, and a Zephyr sysbuild names its default image in `domains.yaml`. A build with one image gives that image. In all other cases, the query must name `image`. `get_active_build` reports the result as `active_image`. |
| `sysbuild` | `false` | Use `west build --sysbuild` (Zephyr). |
| `source_dir` | — | The application directory that `west build` builds (Zephyr), relative to the project root. Without it, west builds the project root. |

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

With `--build`, each variant builds into `.fw-context/build/<variant>/out`.
A variant without sysbuild gives `out/compile_commands.json`. A Zephyr
sysbuild variant gives `out/<image>/compile_commands.json` for each image
that `out/domains.yaml` names, thus the images of each variant are the
images of its build. A later run without `--build` reads the same files, and it builds a
variant that has no build yet. A run over all variants removes from the
index the builds that the project no longer makes (see [PlatformIO](#platformio)).

`--variant` and `--variants` limit the build too: a Zephyr sysbuild run
builds only the variants that you name. `--image` limits the index only,
because one build makes all images of a variant (Zephyr sysbuild, ESP-IDF).

A variant name that cannot be a directory name, for example a name with
`/`, stops the run before any build. See [Where the build goes](#where-the-build-goes).

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

`fw-context index --build` runs `pio run -e <env> --target compiledb` with
two environment variables:

- `PLATFORMIO_BUILD_DIR` sends the build to `.fw-context/build/<variant>/out`.
  PlatformIO adds the directory `<env>`.
- `PLATFORMIO_EXTRA_SCRIPTS` adds the script
  `.fw-context/build/<variant>/fw_context_compiledb.py`. The script puts
  `compile_commands.json` into `out/<env>/`. PlatformIO adds it to the
  `extra_scripts` of `platformio.ini`, thus your scripts still run, and the
  `compile_commands.json` in the project root, which your clangd reads,
  stays as your build wrote it.

fw-context configures dependency tracking (`-MMD`) automatically.

The build also runs `pio run -e <env> --target envdump`. This target compiles
nothing. It prints the SCons environment, and fw-context reads the link
options from it: the linker scripts (`-T`, `--default-script`) and the
`--defsym` values. SCons writes no file that holds the link command, thus
this step is the only source of the memory map and of the linker-script
symbols of a PlatformIO build. fw-context records the result in
`.fw-context/build/<variant>/platformio_link.json`, beside `out/`.

Each `pio run` also runs the `extra_scripts` of the project, thus the
`envdump` step runs them one more time. Each index run asks
`pio project config` for the environments, also without `--build`, because
the environments decide the variants. An index run without `--build` reads
the link record of the last build.

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
project needs generated headers first, and name the output variable of the
Makefile in `out_dir_var`. This build system requires
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

fw-context reads the linker scripts from the link of the application in
`build/build.ninja`. `build/project_description.json` names the ELF of the
application. The link names each script with `-T` and no directory, and
the `-L` options give the directories that hold the scripts. When
fw-context cannot read a link input, the index keeps the memory map that
it has.

### 9. IAR EWARM with pre-build hook

```toml
# In .fw-context/config.toml
[build]
system = "iar-ewarm"
iar_project = "Project.ewp"
iar_target = "Debug"
toolchain_path = "C:/IAR/arm"
pre_build = "python3 tools/generate_version_header.py"
```

Before the conversion step, `pre_build` runs a script that generates
`version.h`. The script is part of the repository, as the build files are.

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
- For Makefile projects, try `make_dry_run = false` with `out_dir_var` if the project needs
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

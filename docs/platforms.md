# Platforms

fw-context runs on Linux, macOS and Windows. This page tells what was
tested on each platform, where fw-context keeps its files, and what is
different on macOS and Windows. For the installation steps of each
platform, see [Installation](installation.md).

## Support and tests

The CI of the project runs the test suite on each push, on three
platforms and two Python versions:

| Platform | CI runner | Python |
|---|---|---|
| Linux | `ubuntu-latest` | 3.11, 3.14 |
| macOS | `macos-latest` (Apple Silicon) | 3.11, 3.14 |
| Windows | `windows-latest` | 3.11, 3.14 |

The CI runs the tests without the markers `ollama`, `libclang`, `slow`
and `system`. The full test suite also passed by hand on Linux, on macOS 26
(Apple Silicon, APFS) and on Windows 11 (Python 3.11).

Three firmware projects were indexed on Linux, macOS and Windows, with
the same toolchain versions on each platform:

| Project | Build system | Target |
|---|---|---|
| Boiler control | PlatformIO, Arduino framework | ESP32 |
| Motion control | PlatformIO, Arduino framework | STM32L476 |
| Production firmware | Mbed OS 6 (Mbed CLI 1) | nRF52840 |

The indexes of each project were compared table by table. The files, the
symbols, the references, the macros and the call graph were the same on
the three platforms. The remaining differences come from the host, and
not from the platform support of fw-context:

- A toolchain package can have other files on another platform. The
  PlatformIO package of the ESP32 compiler for Windows has three more
  macros in `c++config.h`.
- A tool can write a generated file with CRLF line endings on Windows.
  Its text is the same, but its hash is different.
- The index holds absolute paths of the host. The name of an anonymous
  `struct` or `union` holds the path of its file, and the text of a
  vector embedding holds the path of a file outside the project. Thus
  these embeddings are a little different on each host. The LLM analysis
  of a member of an anonymous type also ran again: 14 of 3481 symbols of
  the Mbed OS project.

A project in `bare` mode with Arm GCC 12.2 and 14.2 was also indexed on
Windows, in a directory whose name has a space and characters above 127.

These build systems were not tried with a real project on macOS and
Windows: Zephyr and NCS, ESP-IDF, the generic CMake backend, Makefile,
Arduino CLI, Keil and IAR. The tests cover their code for these
platforms: the activate scripts, the lookup of the tools, and the command
lines.

## Paths

In this documentation, `~` is your home directory:

| Platform | `~` |
|---|---|
| Linux | `/home/<user>` |
| macOS | `/Users/<user>` |
| Windows | `%USERPROFILE%`, for example `C:\Users\<user>` |

fw-context keeps its global files in `~/.fw-context`: the global config
`config.toml`, the index directory `index`, the project registry
`projects.db`, the LLM cache `llm_cache.db` and the clang headers
`clang-resource`. The environment variable `FW_CONTEXT_HOME` moves the
global config and the LLM cache, but not the index, the registry or the
clang headers. These have their own variables (see
[Configuration](configuration.md)).

The programs of a Python environment are in `bin` on Linux and macOS, and
in `Scripts` on Windows. On Windows each program has the extension
`.exe`, for example `fw-context.exe`.

The index and the answers of the MCP tools use `/` in each path, also on
Windows. You can give a path with `\` or `/` to a tool on Windows.

## A Windows path in TOML

TOML reads a backslash in a basic string (`"..."`) as the start of an
escape sequence. Thus `"C:\Users\..."` is not valid TOML: `\U` starts an
escape. Write a Windows path in one of these forms:

```toml
[build]
# A literal string (single quotes): TOML reads each backslash as a backslash.
python = 'C:\Users\me\mbed_venv\Scripts\python.exe'
# Forward slashes:
# python = "C:/Users/me/mbed_venv/Scripts/python.exe"
# Two backslashes in a basic string:
# python = "C:\\Users\\me\\mbed_venv\\Scripts\\python.exe"
```

`fw-context init` writes each path in a form that TOML reads correctly.

## Activate scripts

`[build] activate` names a script that prepares the environment of the
build, for example the toolchain of an SDK:

- On Linux and macOS, fw-context runs the build as
  `bash -c "source <script> && <command>"`.
- On Windows, the script must be a `.bat`, `.cmd` or `.ps1` script.
  fw-context runs the script one time, records the environment that it
  makes, and runs the build in that environment. A script with another
  extension stops the build with an error.

## File watcher and processes

The file watcher daemon uses a loopback TCP connection on each platform
(`127.0.0.1`). It writes its port to `daemon.port` in the index directory
of the project.

fw-context stops its own processes (an index run, the daemon) and a
build in a different way on each platform:

| What stops | Linux and macOS | Windows |
|---|---|---|
| A process of fw-context | `SIGTERM`, then `SIGKILL` | A named stop event that the process reads, then `TerminateProcess` |
| A build | `SIGTERM` to the process group of the build, then `SIGKILL` after a time limit | The job object of the build stops each process in it |

A build starts compilers and other programs. On each platform, the stop
reaches these programs too.

To restart the daemon on each platform, run `fw-context watch restart`.

## Windows

- **Python.** Use Python 3.11 or later from python.org or from
  `winget install Python.Python.3.11`. The installer of python.org has
  the option "Add python.exe to PATH". It is off by default.
- **clang.** `winget install LLVM.LLVM` does not put LLVM on `PATH`.
  fw-context also finds `clang.exe` in `C:\Program Files\LLVM\bin`.
- **Long command lines.** Windows limits a command line to 32767
  characters, and the flags of one unit can be longer. fw-context gives
  these flags to clang in a response file (`@file`). You do not have to
  do anything.
- **GCC older than 13.** Such a GCC cannot open a file whose path has a
  character outside the ANSI code page of the system. Use GCC 13 or
  later, or keep the project in a path without these characters. For
  example, GCC 12.2 changed "ě" to "e" and did not find the file. GCC 13
  and later declare the code page UTF-8 in their manifest
  ([GCC PR108865](https://gcc.gnu.org/bugzilla/show_bug.cgi?id=108865)).
- **CMake.** Set `[build] cmake_generator = "Ninja"` (or
  `"MinGW Makefiles"`) for the generic CMake backend. Without `-G`, CMake
  uses a Visual Studio generator when Visual Studio is installed. Only the
  Makefile and Ninja generators write `compile_commands.json`.
- **bear.** No backend of fw-context needs `bear`. `bear` for Windows
  exists only as a build from source in MSYS2.
- **Text output.** fw-context writes its output in UTF-8. Windows
  PowerShell 5.1 reads a file in the ANSI code page by default. To read a
  log of fw-context there, run `Get-Content -Encoding utf8 <file>`.
- **Git Bash.** A `bash` on `PATH`, for example the one of Git for
  Windows, does not change how fw-context runs a build on Windows.

## macOS

- **clang.** The Command Line Tools of Xcode give `clang`
  (`xcode-select --install`). fw-context uses it for the macro values and
  for the assembly files. The macros of the compared projects were the
  same with Apple clang 21 and with LLVM clang 23 on Linux.
- **Apple Silicon.** Some toolchains exist only for x86_64, for example
  Arm GCC 9-2020-q2. Such a toolchain runs through Rosetta 2. To install
  Rosetta 2, run `softwareupdate --install-rosetta --agree-to-license`.
- **Case of file names.** APFS does not tell `Main.c` from `main.c` by
  default. fw-context records the spelling of each file on the disk, thus
  two spellings of one file give one entry in the index.
- **GPU memory.** On Apple Silicon the GPU uses a part of the RAM.
  fw-context counts two thirds of the RAM as GPU memory when it
  recommends a model of Ollama.
- **`~/.local/bin`.** This directory is not on `PATH` by default. Run
  `uv tool update-shell` (or `pipx ensurepath`) one time, then open a new
  terminal.

## Linux

- **pip and the system Python.** Debian, Ubuntu and other distributions
  mark their Python as externally managed (PEP 668). Then
  `pip install fw-context-mcp` stops with the error
  `externally-managed-environment`. Use `uv tool install` or
  `pipx install`, which make an environment of their own.
- **API keys.** fw-context stores an API key in the Secret Service
  (GNOME Keyring or KWallet). Without a D-Bus session, for example on a
  server, in WSL or in a container, it uses `secrets.toml`. See
  [API keys](configuration.md#api-keys).

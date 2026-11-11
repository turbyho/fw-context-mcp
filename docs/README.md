# Quick Start

This guide shows how to run **fw-context** on your project. The setup takes only a few minutes.

The main `README.md` file explains **why** the project exists, and which problem the project solves.

This document explains **how** to start using fw-context.

## Prerequisites

Before you index a project, you need:

-   Linux, macOS or Windows (see [Platforms](platforms.md))
-   Python 3.11+, and `uv` or `pipx`
-   `git`, and `clang` for the macro values and the assembly units.
    The installation of fw-context installs libclang.
-   The build tool of the project (fw-context runs it to get
    `compile_commands.json`), or a `compile_commands.json` of your own
-   Optional: an LLM service (for semantic search and symbol explanation):
    a local Ollama, or a cloud service such as OpenRouter, DeepSeek or the
    ChatGPT plan

For the steps of each platform, see [Installation](installation.md#install).

## Installation

Install fw-context. The same commands work on Linux, macOS and Windows
(PowerShell):

``` bash
uv tool install fw-context-mcp
uv tool update-shell        # one time, then open a new terminal
fw-context doctor --fix --only clang-resource
```

For the install from source, see
[From source](installation.md#from-source).

In your project, register the MCP server with your preferred LLM client:

``` bash
fw-context init
```

`fw-context init` also asks which LLM services you use. To change them
later, run:

``` bash
fw-context llm setup
```

For the details, see [LLM setup](installation.md#llm-setup).

## Index your first project

Go to your firmware project. Then choose one of these options.

To do a clean build of the project and index it, run:

``` bash
fw-context index --build
```

To index a project that fw-context can build, run:

``` bash
fw-context index
```

This run reads the `compile_commands.json` of each build under
`.fw-context/build/<variant>/out/`. See
[Where the build goes](build.md#where-the-build-goes). When the build is
not there, the run builds the project first.

To index an existing `compile_commands.json` file, give its path:

``` bash
fw-context index path/to/compile_commands.json
```

You can also set the path in `[index] compile_commands`. fw-context then
runs no build. For the details, see [Build Configuration](build.md#automatic-build).

The index is persistent and incremental. After the first indexing run, fw-context reprocesses only the translation units that changed.

## Start asking questions

After indexing finishes, your LLM can ask semantic questions. Your LLM does not need to read entire source files. For example, your LLM can ask:

-   Who calls this function?
-   Which callback registers this handler?
-   Which implementation is active for this build?
-   What will break if I change this API?
-   Show the code path from the ISR to this function.
-   Which functions are never used?

## Optional: Remote cache

If your team runs a [shared cache server](cache-server.md), configure fw-context to share LLM analysis results across developers:

``` bash
fw-context cache remote-init
```

The interactive wizard asks for your server URL and your token. The wizard verifies the connection. The wizard writes the URL to the global config `~/.fw-context/config.toml`, and the token to `~/.fw-context/.cache_token`. It writes no project file.

## Next steps

Continue with the detailed documentation:

-   [Installation](installation.md)
-   [Configuration](configuration.md)
-   [Tools Reference](tools.md)
-   [MCP Server](../README-MCP.md)

This directory contains reference documentation. New users should start with the repository `README.md` file instead.

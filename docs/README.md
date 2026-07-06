# Quick Start

This guide gets **fw-context** running on your project in just a few
minutes.

The main `README.md` explains **why** the project exists and the problem
it solves.

This document explains **how** to start using it.

## Prerequisites

Before indexing a project you need:

-   Python 3.11+
-   libclang
-   Bear (or another way to generate `compile_commands.json`)
-   Optional: Ollama (for semantic search and symbol explanation)

See the installation guide for platform-specific instructions.

## Installation

Clone the repository and install:

``` bash
git clone git@github.com:turbyho/fw-context-mcp.git ~/.fw-context/src
cd ~/.fw-context/src
make install
```

Register the MCP server with your preferred LLM client:

``` bash
fw-context init
```

## Index your first project

Go to your firmware project and either build and index it automatically:

``` bash
fw-context index --build
```

or reuse an existing `compile_commands.json`:

``` bash
fw-context index
```

The index is persistent and incremental. After the initial indexing only
changed translation units are reprocessed.

## Start asking questions

Once indexing is complete your LLM can ask semantic questions instead of
reading entire source files, for example:

-   Who calls this function?
-   Which callback registers this handler?
-   Which implementation is active for this build?
-   What will break if I change this API?
-   Show the code path from the ISR to this function.
-   Which functions are never used?

## Optional: Remote cache

If your team runs a [shared cache server](cache-server.md), configure
it to share LLM analysis results across developers:

``` bash
fw-context cache remote-init
```

The interactive wizard asks for your server URL and token, verifies
the connection, and writes the configuration to your project.

## Next steps

Continue with the detailed documentation:

-   [Installation](installation.md)
-   [Configuration](configuration.md)
-   [Tools Reference](tools.md)
-   [MCP Server](../README-MCP.md)

The documentation in this directory is intended as a reference. The
recommended starting point for new users is the repository `README.md`.

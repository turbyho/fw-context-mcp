# Quick Start

This guide shows how to run **fw-context** on your project. The setup takes only a few minutes.

The main `README.md` file explains **why** the project exists, and which problem the project solves.

This document explains **how** to start using fw-context.

## Prerequisites

Before you index a project, you need:

-   Python 3.11+
-   libclang
-   Bear (or another way to generate `compile_commands.json`)
-   Optional: Ollama (for semantic search and symbol explanation)

See the installation guide for platform-specific instructions.

## Installation

Clone the repository. Then install fw-context:

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

Go to your firmware project. Then choose one of two options.

To build and index the project automatically, run:

``` bash
fw-context index --build
```

To reuse an existing `compile_commands.json` file, run:

``` bash
fw-context index
```

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

The interactive wizard asks for your server URL and your token. The wizard verifies the connection. The wizard writes the configuration to your project.

## Next steps

Continue with the detailed documentation:

-   [Installation](installation.md)
-   [Configuration](configuration.md)
-   [Tools Reference](tools.md)
-   [MCP Server](../README-MCP.md)

This directory contains reference documentation. New users should start with the repository `README.md` file instead.

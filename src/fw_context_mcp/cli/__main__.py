"""Entry point for ``python -m fw_context_mcp.cli``.

The ``cli`` sub-package was converted from a standalone ``cli.py`` module
to a package for better code organization (splitting commands into separate
modules).  This ``__main__.py`` preserves backward compatibility so
``python -m fw_context_mcp.cli`` still works — it delegates to
``from fw_context_mcp.cli import main``.
"""
from fw_context_mcp.cli import main

main()


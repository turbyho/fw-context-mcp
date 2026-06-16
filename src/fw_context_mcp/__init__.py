"""fw-context-mcp — build-aware code intelligence for embedded projects."""

import importlib.metadata

try:
    __version__ = importlib.metadata.version("fw-context-mcp")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0-dev"

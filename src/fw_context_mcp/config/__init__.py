"""Configuration: global (~/.fw-context/config.toml), shared project
(.fw-context/config.toml), and local project (.fw-context/local.toml).

Three config layers, merged in order — later entries override earlier:

1. **Global** (``~/.fw-context/config.toml``) — per-developer defaults.
   Holds LLM API keys, personal preferences, tool registrations.
   Never committed to version control.

2. **Shared project** (``<root>/.fw-context/config.toml``) — per-project
   settings the whole team shares.  Holds index options (vendor_paths,
   index_refs), build detection overrides, and project metadata.
   Committed to git.

3. **Local project** (``<root>/.fw-context/local.toml``) — per-developer
   overrides for a specific project.  Holds per-project LLM config,
   cache server URLs, and developer-specific paths.  Gitignored
   (``.gitignore`` entry generated during ``fw-context init``).

The ``load()`` function merges these three layers.  The result is a
single ``Config`` object — callers never need to know which layer a
setting came from.

Subpackage layout:
- ``settings.py`` — dataclass definitions, validation, ``load()``
- ``_toml_editor.py`` — atomic TOML writes (init, configure-llm)
- ``global_db.py`` — cross-project registry at ``~/.fw-context/projects.db``
- ``tools.py`` — AI tool detection and instruction injection
"""

from .settings import (
    CacheServerConfig,
    Config,
    IndexConfig,
    LLMConfig,
    ProjectMeta,
    ProjectNotInitializedError,
    derive_project_id,
    generate_project_id,
    load,
)

__all__ = [
    "CacheServerConfig",
    "Config",
    "IndexConfig",
    "LLMConfig",
    "ProjectMeta",
    "ProjectNotInitializedError",
    "derive_project_id",
    "generate_project_id",
    "load",
]

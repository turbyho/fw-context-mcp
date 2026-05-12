"""Configuration: global (~/.fw-context/config.toml) and project (.fw-context/config.toml)."""

from .settings import Config, IndexConfig, LLMConfig, ProjectMeta, load

__all__ = ["Config", "IndexConfig", "LLMConfig", "ProjectMeta", "load"]

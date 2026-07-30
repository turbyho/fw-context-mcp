"""Abstract base class for pipeline phases."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class Phase(ABC):
    """One step in the search pipeline.

    Subclasses implement ``run(ctx)`` — they read whatever they need from
    the context and return a new context with their output fields populated.
    """

    name: str = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            raise TypeError(f"{cls.__name__} must set a non-empty 'name' class variable")

    @abstractmethod
    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Execute this phase and return the updated context."""
        ...

    def should_run(self, ctx: PipelineContext) -> bool:
        """Override to add skip conditions (e.g. LLM disabled)."""
        return True

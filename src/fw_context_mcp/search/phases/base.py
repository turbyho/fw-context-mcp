"""Abstract base class for pipeline phases.

Why an ABC?
    Every phase must implement ``run(ctx)`` and declare a ``name``.
    Forgetting either would produce runtime errors deep in the pipeline.
    The ABC enforces these requirements at class-definition time via
    ``__init_subclass__`` — a missing ``name`` is caught at import,
    not at runtime during a user's search.

Why ``should_run`` is optional?
    Most phases always run when included in a pipeline.  Only phases
    with external dependencies (LLM, embeddings) need skip conditions.
    The default ``True`` keeps simple phases concise.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class Phase(ABC):
    """One step in the search pipeline.

    Why immutable context?
        Phases receive a ``PipelineContext`` (frozen dataclass) and must
        return a new one via ``ctx.evolve(**updates)``.  This prevents
        phases from accidentally mutating state that earlier or later
        phases depend on.  The pipeline runner can reason about data
        flow without tracing mutation side effects.

    Subclasses implement ``run(ctx)`` — they read whatever they need from
    the context and return a new context with their output fields populated.
    """

    name: str = ""

    def __init_subclass__(cls, **kwargs):
        """Validate that every Phase subclass declares a ``name``.

        Why at class-definition time?
            A phase without a ``name`` can't be registered in the pipeline
            registry.  Catching this at import time gives a clear error
            message pointing to the offending class, rather than a cryptic
            KeyError when the pipeline tries to look up ``""``.
        """
        super().__init_subclass__(**kwargs)
        if not cls.name:
            raise TypeError(f"{cls.__name__} must set a non-empty 'name' class variable")

    @abstractmethod
    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Execute this phase and return the updated context.

        Why async?
            LLM calls (Ollama) and embedding generation are I/O-bound.
            Async allows the event loop to handle concurrent searches
            or other MCP tool calls while waiting for network responses.
        """
        ...

    def should_run(self, ctx: PipelineContext) -> bool:
        """Override to add skip conditions (e.g. LLM disabled).

        Why check the context, not just config?
            Some phases depend on outputs from earlier phases, not just
            config.  For example, ``RefinePhase`` checks ``ctx.ollama_warning``
            to skip if an earlier LLM call failed — not just whether LLM
            is enabled in config.
        """
        return True

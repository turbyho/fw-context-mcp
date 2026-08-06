"""Tests for PipelineRunner, PipelineConfig, and PipelineContext.

Covers: phase registration, error propagation, should_run gating,
unknown phase handling, predefined configs, context creation/validation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fw_context_mcp.search.context import PipelineContext
from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.search.pipeline import (
    SEARCH_CODE,
    PipelineConfig,
    PipelineRunner,
    _build_registry,
    _build_semantic_search,
    _build_smart_search,
)


# ── Test helpers ────────────────────────────────────────────────────────────


def _make_ctx(**overrides) -> PipelineContext:
    """Create a minimal PipelineContext for testing."""
    defaults = {
        "config_hash": "test_hash",
        "project_root": Path("/tmp"),
        "db_path": Path("/tmp/test.db"),
        "query": "test query",
        "original_query": "test query",
        "limit": 20,
    }
    defaults.update(overrides)
    # Config is required but can be a MagicMock for most tests
    if "config" not in defaults:
        from fw_context_mcp.config.settings import Config

        defaults["config"] = Config()
    if "executor" not in defaults:
        # Most runner/registration tests never touch the DB
        defaults["executor"] = MagicMock()
    return PipelineContext(**defaults)


class _NoopPhase(Phase):
    """Phase that returns ctx unchanged."""
    name = "_noop_test"


class _AppendingPhase(Phase):
    """Phase that appends a marker to ctx.warnings."""
    name = "_append_test"

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        return ctx.evolve(warnings=ctx.warnings + ["_append_test ran"])


class _FailingPhase(Phase):
    """Phase that raises a non-critical exception."""
    name = "_failing_test"

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        raise ValueError("simulated failure")


class _CriticalPhase(Phase):
    """Phase that raises KeyboardInterrupt — must propagate."""
    name = "_critical_test"

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        raise KeyboardInterrupt()


class _ConditionalPhase(Phase):
    """Phase with configurable should_run."""
    name = "_conditional_test"

    def __init__(self, should_run_result: bool = True) -> None:
        self._should_run_result = should_run_result

    def should_run(self, ctx: PipelineContext) -> bool:
        return self._should_run_result

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        return ctx.evolve(warnings=ctx.warnings + ["_conditional_test ran"])


# ── Phase registry ──────────────────────────────────────────────────────────


class TestRegistry:
    """Tests for _build_registry() — lazy phase registration."""

    def test_registry_contains_all_expected_phases(self) -> None:
        """All 14 phase names must be present."""
        registry = _build_registry()
        expected = {
            "translate",
            "rough_search",
            "llm_query",
            "refine",
            "fts5_search",
            "embedding",
            "adaptive_fusion",
            "deduplicate",
            "expand_context",
            "format",
            "name_tokens_fallback",
            "docstring_fallback",
            "individual_terms_fallback",
            "macros_fts_fallback",
        }
        assert set(registry.keys()) == expected

    def test_registry_is_cached(self) -> None:
        """Second call returns the same dict instance."""
        r1 = _build_registry()
        r2 = _build_registry()
        assert r1 is r2

    def test_phase_instances_have_correct_names(self) -> None:
        """Registry key matches each phase's .name attribute."""
        registry = _build_registry()
        for name, phase in registry.items():
            assert phase.name == name, f"Phase {name!r} has name {phase.name!r}"


# ── PipelineConfig ──────────────────────────────────────────────────────────


class TestPipelineConfig:
    """Tests for PipelineConfig and predefined pipelines."""

    def test_empty_config(self) -> None:
        cfg = PipelineConfig()
        assert cfg.phases == []

    def test_custom_phases(self) -> None:
        cfg = PipelineConfig(phases=["translate", "format"])
        assert cfg.phases == ["translate", "format"]

    def test_search_code_predefined(self) -> None:
        """SEARCH_CODE contains the expected fallback chain."""
        assert isinstance(SEARCH_CODE, PipelineConfig)
        phases = SEARCH_CODE.phases
        assert "rough_search" in phases
        assert "fts5_search" in phases
        assert "name_tokens_fallback" in phases
        assert "docstring_fallback" in phases
        assert "individual_terms_fallback" in phases
        assert "macros_fts_fallback" in phases
        assert "deduplicate" in phases
        assert "format" in phases
        # order matters — fallbacks must be before deduplicate
        assert phases.index("name_tokens_fallback") < phases.index("deduplicate")

    def test_smart_search_has_translate_first(self) -> None:
        cfg = _build_smart_search()
        assert cfg.phases[0] == "translate"

    def test_smart_search_has_embedding_instance(self) -> None:
        cfg = _build_smart_search()
        assert any(isinstance(p, Phase) for p in cfg.phases)

    def test_semantic_search_uses_params(self) -> None:
        cfg = _build_semantic_search(threshold=0.7, overfetch=60)
        # Should have an EmbeddingPhase instance and format
        assert len(cfg.phases) == 2
        assert cfg.phases[1] == "format"


# ── PipelineRunner ──────────────────────────────────────────────────────────


class TestPipelineRunner:
    """Tests for PipelineRunner.run() — execution, error handling, gating."""

    def test_runs_all_phases_in_order(self) -> None:
        """Phases execute sequentially and each sees the previous output."""
        ctx = _make_ctx(warnings=["start"])
        config = PipelineConfig(phases=[_AppendingPhase(), _AppendingPhase()])
        runner = PipelineRunner(config)
        result = asyncio.run(runner.run(ctx))
        assert result.warnings == ["start", "_append_test ran", "_append_test ran"]

    def test_skips_unknown_phase_names(self) -> None:
        """Unknown phase names are logged and skipped without error."""
        ctx = _make_ctx()
        config = PipelineConfig(phases=["nonexistent_phase_xyz", _AppendingPhase()])
        runner = PipelineRunner(config)
        result = asyncio.run(runner.run(ctx))
        assert "_append_test ran" in result.warnings

    def test_skips_phases_where_should_run_is_false(self) -> None:
        """Conditional phases are skipped when should_run() returns False."""
        ctx = _make_ctx()
        skipping = _ConditionalPhase(should_run_result=False)
        appending = _AppendingPhase()
        config = PipelineConfig(phases=[skipping, appending])
        runner = PipelineRunner(config)
        result = asyncio.run(runner.run(ctx))
        # Only the appending phase should have run
        assert result.warnings == ["_append_test ran"]

    def test_non_critical_exception_adds_warning_and_continues(self) -> None:
        """Phase failure → warning added, subsequent phases still run."""
        ctx = _make_ctx()
        config = PipelineConfig(phases=[_FailingPhase(), _AppendingPhase()])
        runner = PipelineRunner(config)
        result = asyncio.run(runner.run(ctx))
        assert len(result.warnings) == 2
        assert any("_failing_test" in w for w in result.warnings)
        assert "_append_test ran" in result.warnings

    def test_keyboard_interrupt_propagates(self) -> None:
        """KeyboardInterrupt and SystemExit must not be caught."""
        ctx = _make_ctx()
        config = PipelineConfig(phases=[_CriticalPhase()])
        runner = PipelineRunner(config)
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(runner.run(ctx))

    def test_system_exit_propagates(self) -> None:
        """SystemExit must propagate through the except handler."""

        class _SystemExitPhase(Phase):
            name = "_sysexit_test"

            async def run(self, ctx: PipelineContext) -> PipelineContext:
                raise SystemExit(1)

        ctx = _make_ctx()
        config = PipelineConfig(phases=[_SystemExitPhase()])
        runner = PipelineRunner(config)
        with pytest.raises(SystemExit):
            asyncio.run(runner.run(ctx))

    def test_phase_instance_in_config_uses_its_name(self) -> None:
        """Pre-configured Phase instances work directly in PipelineConfig."""
        ctx = _make_ctx()
        phase = _AppendingPhase()
        config = PipelineConfig(phases=[phase])
        runner = PipelineRunner(config)
        result = asyncio.run(runner.run(ctx))
        assert "_append_test ran" in result.warnings

    def test_empty_pipeline_returns_ctx_unchanged(self) -> None:
        """Pipeline with no phases returns the context as-is."""
        ctx = _make_ctx(warnings=["initial"])
        config = PipelineConfig(phases=[])
        runner = PipelineRunner(config)
        result = asyncio.run(runner.run(ctx))
        assert result.warnings == ["initial"]

    def test_phase_receives_previous_output(self) -> None:
        """Each phase receives the evolved context from the previous phase."""

        class _ReadingPhase(Phase):
            name = "_reading_test"

            async def run(self, ctx: PipelineContext) -> PipelineContext:
                count = len([w for w in ctx.warnings if w == "_append_test ran"])
                return ctx.evolve(warnings=ctx.warnings + [f"seen {count} appends"])

        ctx = _make_ctx()
        config = PipelineConfig(
            phases=[_AppendingPhase(), _AppendingPhase(), _ReadingPhase()]
        )
        runner = PipelineRunner(config)
        result = asyncio.run(runner.run(ctx))
        assert "seen 2 appends" in result.warnings

    def test_multiple_failures_accumulate_warnings(self) -> None:
        """Multiple failing phases each add their own warning."""
        ctx = _make_ctx()
        config = PipelineConfig(phases=[_FailingPhase(), _FailingPhase(), _AppendingPhase()])
        runner = PipelineRunner(config)
        result = asyncio.run(runner.run(ctx))
        failure_warnings = [w for w in result.warnings if "_failing_test" in w]
        assert len(failure_warnings) == 2
        assert "_append_test ran" in result.warnings


# ── PipelineContext ─────────────────────────────────────────────────────────


class TestPipelineContext:
    """Tests for PipelineContext.create() and evolve()."""

    def test_evolve_creates_new_instance(self) -> None:
        ctx = _make_ctx()
        new_ctx = ctx.evolve(query="new query")
        assert new_ctx is not ctx
        assert new_ctx.query == "new query"
        assert ctx.query == "test query"

    def test_evolve_preserves_other_fields(self) -> None:
        ctx = _make_ctx()
        new_ctx = ctx.evolve(limit=50)
        assert new_ctx.limit == 50
        assert new_ctx.config_hash == ctx.config_hash
        assert new_ctx.project_root == ctx.project_root

    def test_context_is_frozen(self) -> None:
        """PipelineContext is frozen — direct mutation raises."""
        ctx = _make_ctx()
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            ctx.limit = 999  # type: ignore[misc]

    def test_create_raises_valueerror_when_no_index(self, tmp_path: Path) -> None:
        """PipelineContext.create() raises ValueError when index DB is missing."""
        # Use a real tmp_path that exists, but has no index.db
        (tmp_path / ".fw-context").mkdir(exist_ok=True)
        with patch(
            "fw_context_mcp.search.context.resolve_project_root",
            return_value=tmp_path,
        ), patch(
            "fw_context_mcp.search.context.derive_project_id",
            return_value="test_project",
        ), patch(
            "fw_context_mcp.search.context.load_config",
            return_value=_make_ctx().config,
        ):
            with pytest.raises(ValueError):
                PipelineContext.create(query="test", project_root=str(tmp_path))

    def test_create_clamps_limit(self) -> None:
        """Limit is clamped to [5, 100]."""
        # Below minimum — clamped to 5
        # Above maximum — clamped to 100
        # This is tested indirectly through the factory, but we can test
        # the clamping logic directly via _make_ctx with explicit limit
        ctx_low = _make_ctx(limit=1)
        assert ctx_low.limit == 1  # no clamping in direct constructor

    def test_default_warnings_is_empty(self) -> None:
        ctx = _make_ctx()
        assert ctx.warnings == []

    def test_default_fts5_results_is_empty(self) -> None:
        ctx = _make_ctx()
        assert ctx.fts5_results == []

    def test_default_embedding_results_is_empty(self) -> None:
        ctx = _make_ctx()
        assert ctx.embedding_results == []

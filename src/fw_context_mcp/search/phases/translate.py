"""Phase 0: Translate non-English queries to English via Ollama."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class TranslatePhase(Phase):
    """Detect and translate non-English queries to English.

    Always sends the query to Ollama for language detection — English queries
    are returned unchanged.  Skipped when ``llm.enabled`` is False.
    """

    name = "translate"  #: Phase identifier used in pipeline configuration.

    def should_run(self, ctx) -> bool:
        """Only run when Ollama is enabled in config."""
        return ctx.config.llm.enabled

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Send the query to Ollama for language detection and translation.

        English queries are returned unchanged.  Strips common "already
        English" prefixes from the response.  On Ollama errors the original
        query is kept.
        """
        from fw_context_mcp.llm.ollama import OllamaError, call_ollama_async

        query = ctx.query
        translate_prompt = (
            "Translate the following text to English. "
            "If the text is already in English, return it unchanged. "
            "IMPORTANT: Your entire response must be ONLY the final "
            "English text — no introductory words, no explanations, "
            'no "YES", no "The text is already", nothing else.\n\n'
            f"{query}"
        )
        try:
            translated = await call_ollama_async(translate_prompt, ctx.config.llm)
            # Strip common "already English" prefixes
            for prefix in (
                "YES\n\n", "YES\n", "YES. ", "YES ",
                "The text is already in English.\n\n",
                "The text is already in English.\n",
                "The text is in English.\n\n",
                "The text is in English.\n",
                "Already in English.\n\n",
                "Already in English.\n",
            ):
                if translated.startswith(prefix):
                    translated = translated[len(prefix):]
                    break
            translated = translated.strip()
            if translated and translated != query:
                return ctx.evolve(query=translated, translated_from=query)
        except OllamaError:
            pass  # keep original query on failure
        return ctx

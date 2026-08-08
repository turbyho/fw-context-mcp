"""Phase: Translate non-English queries to English via Ollama.

Why translate before searching?
    The FTS5 and embedding indices are built from English source code.
    Non-English queries (Czech "síťová registrace", French "connexion
    modem") would match zero symbols because the codebase contains no
    French or Czech text.  Translating to English before indexing enables
    the search pipeline to work with native-language queries.

Why always send to Ollama?
    Language detection is non-trivial.  Checking for non-ASCII characters
    catches Czech, Russian, Chinese, etc. — but misses English sentences
    that are queries (which we want to pass through unchanged).  Ollama
    handles both detection and translation in one call, simplifying the
    code path.

Why skip for pure ASCII queries?
    Source code is written in English; queries typically follow suit.
    Checking for non-ASCII characters avoids an unnecessary LLM call
    for the 95%+ case of English queries.  The check is O(n) per
    character, which is microseconds for typical query lengths.

Why cap response at 200 characters?
    LLMs sometimes hallucinate long explanations or translations when
    asked to translate.  Capping prevents a 500-character hallucinated
    response from becoming the search query.  Real queries for code
    search rarely exceed 100 characters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class TranslatePhase(Phase):
    """Detect and translate non-English queries to English.

    Why this is Phase 0?
        All subsequent phases (rough search, LLM query, FTS5) depend on
        the query being in English.  Translation must run first, before
        any search work begins.

    Why pass English queries through unchanged?
        The translation prompt explicitly instructs Ollama to return
        English text unchanged.  Stripping common "already English"
        prefixes handles cases where the LLM adds explanatory text
        despite the instruction.

    Always sends the query to Ollama for language detection — English queries
    are returned unchanged.  Skipped when ``llm.enabled`` is False.
    """

    name = "translate"  #: Phase identifier used in pipeline configuration.

    def should_run(self, ctx) -> bool:
        """Only run when Ollama is enabled and query might not be English.

        Why the ASCII shortcut?
            Source code and programming queries are overwhelmingly ASCII.
            Checking ``any(ord(c) > 127)`` is O(n) and avoids an LLM call
            for the common case of English queries.  Chinese, Czech,
            Russian, etc. trigger the translation path.
        """
        if not ctx.config.llm.enabled:
            return False
        # Skip LLM translation for queries that are purely ASCII —
        # source code is written in English and queries follow suit.
        # NOTE: iterates entire query per search; microsecond cost, acceptable
        if not any(ord(c) > 127 for c in ctx.query):
            return False
        return True

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Send the query to Ollama for language detection and translation.

        The prompt instructs Ollama to return English text unchanged.
        Responses are cleaned of common "already English" prefixes that
        some LLMs add despite the instruction.  On Ollama errors, the
        original query is kept — the search pipeline may still find
        results via FTS5 word-matching or embedding similarity.

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
            # Strip common "already English" prefixes that some LLMs
            # add despite the prompt instructions
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
            # Guard against LLM hallucination — cap at 200 characters.
            # Real code search queries are typically 5-100 characters.
            translated = translated[:200]
            if translated and translated != query:
                return ctx.evolve(query=translated, translated_from=query)
        except OllamaError:
            pass  # keep original query on failure — better than no search
        return ctx

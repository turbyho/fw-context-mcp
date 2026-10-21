"""LLM analysis batch processing extracted from runner.py.

WHY separate module: the LLM analysis loop is complex (hash-based caching,
two-tier cache lookup, token counts and context selection, sentinel
management).  Extracting it from runner.py keeps the runner focused on the
parse→store pipeline and makes the analysis logic testable in isolation.

Handles reading symbol bodies, fetching callees/referencers,
enriching batches, and the main LLM analysis build phase.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cache_client import get_local_cache_db, local_cache_lookup, local_cache_upsert
from ..llm.client import ChatClient, ContextFit, get_chat_client
from ..llm.errors import LLMStopRun
from ..llm.ollama import OllamaPromptTooLong
from ..utils import SAFE_EXCEPT, is_fatal, read_file_lines
from ._embedding import _fmt_dur
from .db import transaction, upsert_llm_analysis_batch, write_lock
from .db._projects import ANALYZABLE_KINDS

log = logging.getLogger(__name__)


def _read_body(abs_path: str, start_line: int, end_line: int) -> str:
    """Read a function body from a source file using line numbers.

    Returns the body text or an empty string on any error (missing file, bad range).
    """
    lines = read_file_lines(abs_path)
    if lines is None:
        return ""
    if 0 < start_line <= end_line <= len(lines):
        return "".join(lines[start_line - 1 : end_line])
    return ""


def _fetch_callees(conn, symbol_usr: str, config_hash: str) -> list[str]:
    """Return the qualified names of functions called by *symbol_usr*."""
    rows = conn.execute(
        """SELECT DISTINCT s.qualified_name
           FROM refs r
           JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = ?
           WHERE r.from_usr = ?
             AND r.ref_kind = 'call'
             AND r.config_hash = ?
             AND s.qualified_name != ''
           ORDER BY s.qualified_name
           LIMIT 35""",
        (config_hash, symbol_usr, config_hash),
    ).fetchall()
    return [r[0] for r in rows]


def _fetch_referencers(conn, symbol_usr: str, config_hash: str) -> list[str]:
    """Return qualified names of functions that reference (read/write) *symbol_usr*."""
    rows = conn.execute(
        """SELECT DISTINCT s.qualified_name
           FROM refs r
           JOIN symbols s ON s.usr = r.from_usr AND s.config_hash = ?
           WHERE r.to_usr = ?
             AND r.config_hash = ?
             AND s.qualified_name != ''
           ORDER BY s.qualified_name
           LIMIT 35""",
        (config_hash, symbol_usr, config_hash),
    ).fetchall()
    return [r[0] for r in rows]


def _enrich_batch(conn, batch_rows, config_hash: str, *, project_root: Path | None = None) -> list[dict]:
    """Augment symbol rows with ``body``, ``callees`` and ``body_unavailable``.

    Takes the body from the ``source`` column and fetches callee names from
    the reference index.  A callee lookup that gives nothing is normal and
    stays an empty list.

    WHY the column and not the disk: ``source`` is ifdef-filtered, thus it
    holds only the code that compiles.  A body read from the disk holds every
    branch, and the model would then describe code that the build drops.  The
    column is also what the hash comparison in ``_build_llm_analysis`` uses,
    and two different texts there make every symbol with a dead branch look
    changed on each run.  The disk stays as the fallback for a row that has
    no stored body — a symbol indexed before the column existed.

    ``body_unavailable`` is True when the kind of the symbol must have a
    body, but no text was available.  There are three causes:

    * The stored body holds only blank lines.  Every line of the symbol is
      inside an inactive branch, thus the active build compiles none of it.
      The disk is no alternative here, because it holds every branch.
    * The source file disappeared.  This and the next cause apply only to a
      row that has no stored body, thus the disk had to answer.
    * The extent does not agree with the file on disk — for example when an
      editor shortens the file while a background reindex reads it.

    The caller must skip such a symbol.  The reason is not only that an
    analysis without the body is worthless: the hash of an empty body would
    reach the content-addressable caches.  See the note in
    ``_build_llm_analysis``.
    """
    from ..utils import abs_path as resolve_abs_path

    log = logging.getLogger(__name__)
    enriched: list[dict] = []
    for r in batch_rows:
        d = dict(r)
        body = ""
        body_unavailable = False
        callees: list[str] = []

        kind = d.get("kind", "")
        file_path = d.get("file_path", "")
        start_line = d.get("line", 0)
        end_line = d.get("end_line", 0)
        usr = d.get("usr", "")

        abs_file_path = resolve_abs_path(project_root, file_path) if project_root else file_path

        # Read body for symbols with meaningful extents.  Enums and typedefs
        # benefit from body text during LLM analysis (enum constants, type alias).
        #
        # `0 < start_line <= end_line` takes the one-line definition too, and
        # it keeps out a row with no extent.  The gate that asked for more
        # than one line let a stored body through to nothing:
        # `ops._read_body` now writes the text of an inline accessor, and
        # this gate would then throw it away.
        if (
            kind in ("function", "method", "constructor", "destructor", "class", "struct", "union", "enum", "typedef")
            and abs_file_path
            and 0 < start_line <= end_line
        ):
            # The stored body is ifdef-filtered — prefer it, see the
            # docstring.  A row from before the column existed holds nothing
            # here, and the disk then answers.
            stored = d.get("source") or ""
            body = stored
            if stored and not stored.strip():
                # The filter blanked every line of this symbol: the whole
                # body is inside an inactive branch.  The string is truthy,
                # thus it used to pass as a real body.
                #
                # WHY this must not read the disk as an alternative: the
                # disk holds every branch, so it would give the model the
                # dead code that the filter removed.
                #
                # WHY this must not reach the model: `compute_content_hash`
                # strips the body, thus `h` here is the hash of an EMPTY
                # body.  An answer stored under that hash is readable by
                # every later symbol with the same name, signature and
                # docstring — the cache hazard that the sentinel path below
                # exists to prevent.
                body = ""
                body_unavailable = True
                log.warning(
                    "[%s] body not available for %s — every line of %s:%d-%d "
                    "is in an inactive #if branch",
                    kind, d.get("qualified_name", "?"), abs_file_path,
                    start_line, end_line,
                )
            elif not body:
                if not os.path.exists(abs_file_path):
                    body_unavailable = True
                    log.warning(
                        "[%s] body not available for %s — file missing: %s",
                        kind, d.get("qualified_name", "?"), abs_file_path,
                    )
                else:
                    body = _read_body(abs_file_path, start_line, end_line)
                    if not body and start_line > 0:
                        body_unavailable = True
                        log.warning(
                            "[%s] empty body for %s at %s:%d-%d — "
                            "path resolution or extent mismatch",
                            kind, d.get("qualified_name", "?"), abs_file_path, start_line, end_line,
                        )

        # Fetch callees / referencers from the reference index
        if usr:
            if kind == "varglobal":
                callees = _fetch_referencers(conn, usr, config_hash)
            elif kind in ("varlocal", "variable", "field"):
                callees = []
            else:
                callees = _fetch_callees(conn, usr, config_hash)

        d["body"] = body
        d["callees"] = callees
        d["body_unavailable"] = body_unavailable
        enriched.append(d)
    return enriched

#: The lower bound of the context that the measured speed selects (decision
#: of the user, 2026-10-21): a smaller context sends too many prompts to the
#: second pass.
_LOWEST_CONTEXT = 4096


def _context_choice_on() -> bool:
    """Return False in the test session: a measurement loads models into the Ollama of the operator."""
    from ..llm.ollama import NO_RELEASE_ENV

    return not os.environ.get(NO_RELEASE_ENV)


class ContextChoice:
    """The context of the LLM analysis of one run, selected one time (``ChatClient.fit_context``, ``llm.calibration``).

    An index run or ``fw-context analyze`` makes one and gives it to each
    build of the run.  WHY a measurement in a manual run only: a full
    search measures up to 6 contexts for 16384 and up to 8 for 65536, a
    check one, each a load and a request of some seconds, and the
    background reindex and ``reindex_file`` usually analyse one or two
    symbols.  WHY one object for the
    run: a run of several builds (a Zephyr app, mcuboot and stage0) calls
    ``_build_llm_analysis`` for each build, and nothing that the search
    measures changes between the builds.

    *check* False (the background reindex, ``reindex_file``): the context
    of the stored calibration, without a measurement; without one,
    ``num_ctx``.
    """

    def __init__(self, *, check: bool = True) -> None:
        self.check = check
        self._fits: dict[tuple[str, int], ContextFit | None] = {}

    def select(self, client: ChatClient, model: str, upper: int) -> ContextFit | None:
        """Return the context of *model* below *upper*: searched at the first call, then the same answer.

        Raises:
            LLMStopRun: The model does not load at the lower bound for
                memory (``ModelDoesNotFit``); the run stops.  Not
                kept: the next build tries one load again.
        """
        key = (model, upper)
        if key not in self._fits:
            self._fits[key] = (client.fit_context(min(_LOWEST_CONTEXT, upper), upper, check=self.check)
                               if _context_choice_on() else None)
        return self._fits[key]


# The tokens of the answer (num_predict), and thus the tokens of the context
# that a prompt must leave free.  Measured with qwen2.5-coder:14b on 14 long
# bodies: an answer has 139 to 465 tokens; the budget is about twice the
# largest.  WHY a fixed budget: the budget was the rest of the context, and
# the largest prompt of the second pass, whose context is made to fit it,
# then had 300 tokens: a cut JSON, and a sentinel on each run.
_ANSWER_TOKENS = 1024


def _analysis_contexts(client: ChatClient, llm_config) -> tuple[int, int] | None:
    """Return the context of the run and the largest context of the model, or None.

    The context of the run is ``num_ctx`` of the config: the server loads
    the model with it.  The largest one is the context that the model was
    trained for (POST ``/api/show``); a prompt that does not fit the
    context of the run goes in a second pass with a larger one.

    Before, the context came from ``details.context_length`` of
    ``/api/tags``: the trained context (32768 for qwen2.5-coder:14b), while
    each request had ``num_ctx`` (16384).  The answer budget thus counted
    on a context that the server did not use.

    A backend that cannot count tokens (an OpenAI-compatible API,
    ``client.counts_tokens`` is False) gives no trained context: the run
    then has ``num_ctx`` only.  None when the Ollama server does not answer,
    or does not have the model: the run then makes no analysis.
    """
    if not client.counts_tokens:
        return llm_config.num_ctx, llm_config.num_ctx
    largest = client.trained_context()
    if largest is None and llm_config.auto_pull:
        # /api/show gives 404 for a model that is not installed.
        try:
            client.pull_model()
        except SAFE_EXCEPT as e:
            if is_fatal(e):
                raise
            log.warning("The pull of %s failed: %s", llm_config.model, e)
        else:
            largest = client.trained_context()
    if largest is None:
        log.warning("Ollama did not give the context of %s (not reachable, or the model is not installed) "
                    "— skipping LLM analysis generation", llm_config.model)
        return None
    return min(llm_config.num_ctx, largest), largest


@dataclass(frozen=True)
class _LongPrompt:
    """A symbol whose prompt does not fit the context of the run: the second pass analyses it."""

    position: int
    qname: str
    symbol: dict[str, Any]
    content_hash: str
    prompt: str
    prompt_tokens: int


def _clear_skip_sentinels(
    conn,
    model: str,
    model_ctx_size: int,
    retry_unparseable: bool,
) -> None:
    """Remove skip sentinels that are no longer applicable.

    - ``skip:toolarge:`` sentinels whose recorded context size is smaller
      than the current model's context window are cleared (the symbol may
      now fit).
    - ``skip:unparseable:`` sentinels are cleared when *retry_unparseable*
      is True, or when the stored model name doesn't match *model*.
    - ``skip:nobody`` sentinels stay.  They need no rule here, because the
      recorded hash is the hash of an empty body: a body that reads
      correctly gives a different hash, thus the next run re-analyses the
      symbol on its own.
    """
    conn.execute(
        """DELETE FROM llm_analysis
           WHERE model LIKE 'skip:toolarge:%'
             AND CAST(SUBSTR(model, 15) AS INTEGER) < ?""",
        (model_ctx_size,),
    )
    if retry_unparseable:
        conn.execute("DELETE FROM llm_analysis WHERE model LIKE 'skip:unparseable:%'")
    else:
        conn.execute(
            """DELETE FROM llm_analysis
               WHERE model LIKE 'skip:unparseable:%'
                 AND SUBSTR(model, 18) != ?""",
            (model,),
        )


class _Spent:
    """The usage of the requests of one analysis run, as the backend reports it.

    WHY: a run on a paid API must tell what it cost; the provider reports
    the tokens (and OpenRouter the cost) of each answer.  A value that a
    backend does not report stays out of the line: no estimate.
    """

    def __init__(self) -> None:
        self.requests = 0
        self.reported = 0
        self.prompt = 0
        self.cached = 0
        self.completion = 0
        self.cost: float | None = None

    def add(self, usage) -> None:
        self.requests += 1
        if usage.prompt_tokens is not None or usage.completion_tokens is not None:
            self.reported += 1
        self.prompt += usage.prompt_tokens or 0
        self.cached += usage.cached_tokens or 0
        self.completion += usage.completion_tokens or 0
        if usage.cost is not None:
            self.cost = (self.cost or 0.0) + usage.cost

    def line(self, seconds: float) -> str:
        parts = [f"{self.requests} requests"]
        if not self.reported:
            # A streamed answer can come without a usage object: "0 tokens"
            # would be a value that the backend did not give.
            parts.append("usage not reported")
        else:
            if self.reported < self.requests:
                parts.append(f"usage reported for {self.reported} of them")
            parts.append(f"{self.prompt} input tokens")
            if self.cached:
                parts[-1] += f" ({self.cached} from the cache of the provider)"
            parts.append(f"{self.completion} output tokens")
        if self.cost is not None:
            parts.append(f"cost {self.cost:.4f} USD (reported by the provider)")
        parts.append(f"{seconds:.0f} s")
        return ", ".join(parts)


def _select_unanalyzed_symbols(
    conn,
    config_hash: str,
    project_only: bool,
) -> list:
    """Return definition symbols that may need LLM analysis.

    LEFT JOINs ``llm_analysis`` so the caller can compare the
    content-addressable hash: if ``existing_hash`` matches the
    freshly-computed hash, the analysis is still current and the
    symbol can be skipped.  Symbols without any analysis have
    ``existing_hash = NULL`` and always proceed to cache lookup.
    """
    query = f"""SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                        s.signature, s.is_definition, s.docstring,
                        s.end_line, s.line, s.usr, s.source,
                        f.path as file_path,
                        a.content_hash as existing_hash
                 FROM symbols s
                 JOIN files f ON s.file_id = f.id
                 LEFT JOIN llm_analysis a ON a.symbol_id = s.id
                 WHERE {_CANDIDATES_WHERE.format(project=_project_clause(project_only))}
                 ORDER BY s.kind, s.file_path, s.line"""
    with transaction(conn, checkpoint=False):
        return conn.execute(query, (config_hash, *ANALYZABLE_KINDS)).fetchall()


#: The definitions that the analysis reads: one filter for the run and for its count.
_CANDIDATES_WHERE = (
    "s.config_hash = ? AND s.is_definition = 1 AND s.kind IN ("
    + ", ".join("?" * len(ANALYZABLE_KINDS))
    + ") AND s.name NOT LIKE '%(anonymous%' AND s.name NOT LIKE '%(unnamed%' {project}"
)


def _project_clause(project_only: bool) -> str:
    return "AND s.is_project = 1" if project_only else ""


def count_analysis_candidates(conn, config_hash: str, project_only: bool) -> tuple[int, int]:
    """Return (definitions that the analysis reads, those without an analysis) of a build.

    ``COUNT`` and not the rows of ``_select_unanalyzed_symbols``: those
    hold the bodies (measured: 247 MB for 50000 definitions).
    """
    query = f"""SELECT COUNT(*) AS total, SUM(a.symbol_id IS NULL) AS without
                 FROM symbols s
                 JOIN files f ON s.file_id = f.id
                 LEFT JOIN llm_analysis a ON a.symbol_id = s.id
                 WHERE {_CANDIDATES_WHERE.format(project=_project_clause(project_only))}"""
    row = conn.execute(query, (config_hash, *ANALYZABLE_KINDS)).fetchone()
    return int(row["total"] or 0), int(row["without"] or 0)


def _build_llm_analysis(
    conn,
    config_hash: str,
    llm_config,
    db_dir: Path,
    *,
    project_only: bool = True,
    project_root: Path | None = None,
    write_lock_held: bool = False,
    cache_client=None,
    retry_unparseable: bool = False,
    force_since: str | None = None,
    context_choice: ContextChoice | None = None,
) -> bool:
    """Generate structured LLM analysis (summary, inputs, outputs) for each
    project-definition symbol using Ollama, one symbol per request.

    Processes symbols individually — one Ollama request per symbol — for
    maximum isolation and retry-ability.  NOTE(turbyho, 2026-07-31): Ollama batching API would
    reduce ~10K symbols from ~10K HTTP round trips to O(batches).
    reliable format adherence. Only project symbols (non-SDK) are analyzed.

    The prompt includes the full function body (the stored ``source``,
    which is ifdef-filtered) and callee names (from the reference index),
    which dramatically improves description quality.

    *db_dir* is the directory containing the index database — used for the
    write lock that serializes DB access across processes.
    *project_only* when True (default) filters to symbols where
    ``s.is_project = 1`` (project code).  When False, all symbols
    including vendor/SDK are analyzed.  Uses the ``is_project`` column
    which is computed during indexing from vendor/project path patterns.
    *retry_unparseable* when True clears all ``skip:unparseable`` sentinels
    so previously-failed symbols are re-attempted. Set True for manual
    indexing, False for background reindex (safe: retries only on model change).

    The prompt holds the full body.  Before, a body over 5000 characters
    went to the model as its first 60 lines, and the summary told only what
    those lines do: the summary of a ``main`` of 42000 characters had the
    start-up and not the main loop.  The comment of that cut said that a
    model gives no JSON for a long prompt.  Measured with
    qwen2.5-coder:14b on 14 bodies of 5300 to 92000 characters: each
    answer was JSON, with the full body and with the cut one.

    The tokens of each prompt are counted by the tokenizer of the model
    (``ChatClient.count_tokens``), not estimated from its characters.  A prompt
    that does not fit the context of the run waits for a second pass, after
    each other symbol.  That pass has one context: the smallest one that
    holds each waiting prompt, and at most the trained context.  WHY one
    pass, and not a context for each prompt: a request with another context
    makes the server load the model again.  A prompt that does not fit the
    trained context gets the sentinel ``skip:toolarge:<context>``.

    *force_since* (``--force-llm`` of ``fw-context index`` and ``fw-context analyze``) makes each analysis
    again.  The stored analyses of the build and an entry of a cache from
    before that time do not apply, and the new analysis replaces the entry
    of the local cache.  An entry from after that time applies: a symbol
    of two builds gets one analysis in one command, and a command that
    continues a stopped one keeps what the stopped one made.  The value has
    the format of ``analyzed_at`` of the local cache (UTC, ``YYYY-MM-DD
    HH:MM:SS``).  Each analysis of the build is replaced when its new one
    is stored, thus a stopped run keeps the old analyses of the symbols
    that it did not reach.

    Each answer gets ``_ANSWER_TOKENS`` tokens, also from an
    OpenAI-compatible endpoint, which gives no token count.  A prompt that
    such an endpoint refuses is logged, and the next run tries again.

    Returns True when each symbol got its analysis or a sentinel, and False
    when the server did not answer, or a symbol got neither: a forced
    command then keeps its marker (``_force_llm_marker``).  In a forced run,
    an entry of the local cache applies only for the same model, because
    the content hash does not hold the model.
    """
    from ..indexer.prompts import build_analysis_prompt, parse_analysis_response
    from ..utils import compute_content_hash

    # Suppress httpx INFO logs (one per symbol — noisy during analysis)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Check Ollama reachability and resolve the contexts of the model.
    client = get_chat_client(llm_config)
    contexts = _analysis_contexts(client, llm_config)
    if contexts is None:
        return False
    run_ctx, largest_ctx = contexts
    token_counts = client.counts_tokens

    model = llm_config.model

    # Clear skip sentinels that are no longer applicable
    _clear_skip_sentinels(conn, model, largest_ctx, retry_unparseable)

    # Select symbols needing analysis
    rows = _select_unanalyzed_symbols(conn, config_hash, project_only)
    if not rows:
        log.info("All project symbols already analyzed — nothing to do")
        return True

    total_symbols = len(rows)
    total = 0

    log.info("LLM analysis: %d symbols (model=%s)", total_symbols, model)


    local_db = get_local_cache_db()  # single connection for all symbols
    long_prompts: list[_LongPrompt] = []
    # The symbols that got neither an analysis nor a sentinel.
    failed = 0
    released = False

    def _before_the_first_request() -> None:
        """Unload the embedding model, then select the context from the measured speed: before the first request.

        Lazy: a run whose analyses come from the cache keeps the embedding
        model loaded for the next query, and loads nothing.  WHY before the
        count too: the count loads the chat model, and a model loaded beside
        the embedding model keeps the split of the GPU that it got.  With a
        *context_choice*, the context of the run is the upper bound
        (``num_ctx``); the first pass uses the selected one,
        the second pass (long prompts) its own larger one.
        """
        nonlocal released, run_ctx
        if released:
            return
        released = True
        from ..llm.ollama import release_other_model

        release_other_model(llm_config, "analysis")
        fit = context_choice.select(client, llm_config.model, run_ctx) if context_choice is not None else None
        if fit is not None:
            run_ctx = fit.num_ctx
            log.info("LLM analysis: %s", fit.reason)

    def _count_tokens(prompt: str) -> int:
        """Count the tokens of *prompt*; a failed request goes again, three times in all, as a call of the model."""
        _before_the_first_request()
        for _c_attempt in range(3):
            try:
                return client.count_tokens(prompt, run_ctx)
            except LLMStopRun:
                raise
            except SAFE_EXCEPT as _e:
                if is_fatal(_e) or _c_attempt == 2:
                    raise
                time.sleep(2)
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _ask_model(idx: int, qname: str, d: dict[str, Any], h: str, prompt: str, num_ctx: int, t0: float) -> int:
        """Analyse one symbol with the model, store the answer, and return the count to add to the total.

        Raises:
            OllamaPromptTooLong: The prompt does not fit *num_ctx*; the
                caller sends it again with a larger context.
        """
        nonlocal failed
        _before_the_first_request()
        response = None
        _last_exc: BaseException | None = None
        for _a_attempt in range(3):
            try:
                result = client.generate(prompt, temperature=0.1, max_tokens=_ANSWER_TOKENS, num_ctx=num_ctx)
                spent.add(result.usage)
                response = result.text
                break
            except (OllamaPromptTooLong, LLMStopRun):
                # The same request fails again: the caller selects a larger
                # context, or stops the run (a usage limit of a plan).
                raise
            except SAFE_EXCEPT as _e:
                _last_exc = _e
                if is_fatal(_e):
                    raise
                if _a_attempt < 2:
                    time.sleep(2)
        if response is None:
            failed += 1
            elapsed = time.monotonic() - t0
            log.warning("[%d/%d] %s: err %s: %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed), _last_exc)
            return 0

        parsed = parse_analysis_response(response, [d])
        if not parsed:
            # Store sentinel in the per-build analysis table only —
            # NOT in the content-addressable cache.  If we store it in
            # the cache, the next run with a DIFFERENT prompt (e.g.
            # after an Ollama model upgrade or prompt fix) will hit
            # the cache and silently reuse the sentinel, skipping the
            # re-analysis.  The sentinel protects the CURRENT build
            # from infinite retries; the cache protects across builds
            # and should only hold successful analyses.
            with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                with transaction(conn, checkpoint=False):
                    sentinel = f"skip:unparseable:{model}"
                    upsert_llm_analysis_batch(conn, [(d["id"], "", "", "", sentinel, h)])
            elapsed = time.monotonic() - t0
            log.warning(
                "[%d/%d] %s: err %s: unparseable response", idx + 1, total_symbols, qname, _fmt_dur(elapsed)
            )
            return 1

        r = parsed[0]
        with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
            with transaction(conn, checkpoint=False):
                db_rows = [(r["symbol_id"], r["summary"], r["inputs"], r["outputs"], model, h)]
                inserted = upsert_llm_analysis_batch(conn, db_rows)
                entry = {"hash": h, "summary": r["summary"], "inputs": r["inputs"],
                         "outputs": r["outputs"], "model": model}
                # Store in local global cache
                try:
                    local_cache_upsert(local_db, [entry], overwrite=force_since is not None)
                except SAFE_EXCEPT as e:
                    if is_fatal(e):
                        raise
                    log.debug("Local global cache write failed: %s", e)
                # Store on remote cache server (fire-and-forget)
                if cache_client is not None:
                    try:
                        cache_client.batch_put([entry])
                    except SAFE_EXCEPT as e:
                        if is_fatal(e):
                            raise
                        log.debug("Remote cache write failed: %s", e)

        elapsed = time.monotonic() - t0
        log.info("[%d/%d] %s: ok %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
        log.debug("  summary: %s", r["summary"])
        log.debug("  inputs : %s", r["inputs"])
        log.debug("  outputs: %s", r["outputs"])
        return inserted

    stopped = False
    spent = _Spent()
    run_started = time.monotonic()
    # The usage line in finally: an interrupted run (Ctrl-C, SIGTERM, a
    # stop of the plan) is the run whose cost the user must see.
    try:
        for idx, row in enumerate(rows):
            t0 = time.monotonic()
            qname = row["qualified_name"] or row["name"]
            try:
                # ── Step 0: project DB hash check — skip unchanged symbols
                #    without disk I/O or callee fetch.  Uses the ``source``
                #    column stored during indexing, which is the same text that
                #    _enrich_batch gives as the body.  The two MUST agree: the
                #    hash below decides whether the model runs again, and a
                #    body from the disk would differ for every symbol that
                #    holds an inactive #ifdef branch.
                # A forced run makes each analysis again, thus the stored hash
                # does not apply.
                existing_hash = row.get("existing_hash") if force_since is None else None
                if existing_hash:
                    source_body = row["source"] or ""
                    if not source_body:
                        # source column is empty (legacy symbol from before
                        # the 'source' column was added).  Fall back to
                        # reading the body from disk for the hash comparison.
                        from ..utils import abs_path as _resolve_abs_path

                        file_path = row.get("file_path", "")
                        abs_file_path = _resolve_abs_path(project_root, file_path) if project_root else file_path
                        if abs_file_path:
                            source_body = _read_body(abs_file_path, row["line"], row["end_line"])
                    h = compute_content_hash(
                        source_body,
                        row["qualified_name"] or "",
                        row["signature"] or "",
                        row["docstring"] or "",
                    )
                    if existing_hash == h:
                        total += 1
                        elapsed = time.monotonic() - t0
                        log.debug("[%d/%d] %s: hash-matched %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
                        continue

                batch_dicts = _enrich_batch(conn, [row], config_hash, project_root=project_root)
                d = batch_dicts[0]

                # ── Cache check — 2 tiers: local global → remote ──
                h = compute_content_hash(d["body"], d["qualified_name"], d["signature"], d["docstring"])

                # Catch edge cases where source-H ≠ body-H (e.g. varglobal
                # with multi-line initializer — source column captured the
                # body but _enrich_batch left it empty).
                if existing_hash and existing_hash == h:
                    total += 1
                    elapsed = time.monotonic() - t0
                    log.debug("[%d/%d] %s: hash-matched %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
                    continue

                # ── Body unavailable: store a sentinel, do not call the model ──
                # An analysis without the body is worthless, because the model
                # then sees only the name and the signature.  It is also unsafe
                # to cache: ``h`` is the hash of an EMPTY body, thus a later
                # symbol with the same name, signature and docstring could read
                # the answer back from the content-addressable caches.  The
                # unparseable-response path below keeps its sentinel out of
                # those caches for the same reason.
                #
                # The sentinel goes into the per-build table only.
                # ``compute_analysis_coverage`` counts it as skipped, and
                # ``make_analysis_summary`` counts a skipped symbol as done.
                # Thus a file that a build step removed cannot keep the
                # analysis incomplete permanently.  The next run still repairs the
                # symbol: ``h`` came from an empty body, thus it cannot match
                # the hash of a body that reads correctly, and the code above
                # falls through to a real analysis.
                if d.get("body_unavailable"):
                    with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                        with transaction(conn, checkpoint=False):
                            upsert_llm_analysis_batch(conn, [(d["id"], "", "", "", "skip:nobody", h)])
                    total += 1
                    elapsed = time.monotonic() - t0
                    log.warning(
                        "[%d/%d] %s: skip %s (body unavailable — the next run retries)",
                        idx + 1, total_symbols, qname, _fmt_dur(elapsed),
                    )
                    continue

                # Tier 1: local global cache (~/.fw-context/llm_cache.db)
                cached = None
                try:
                    local_hits = local_cache_lookup(local_db, [h])
                    cached = local_hits.get(h)
                except SAFE_EXCEPT as e:
                    if is_fatal(e):
                        raise
                    log.debug("Local global cache lookup failed: %s", e)
                if cached and force_since is not None and (
                    cached["analyzed_at"] < force_since or cached["model"] != model
                ):
                    cached = None

                # Tier 2: remote cache server.  A forced run does not read it:
                # its entries have no time of this machine.
                if not cached and cache_client is not None and force_since is None:
                    try:
                        remote_hits = cache_client.batch_get([h])
                        cached = remote_hits.get(h)
                        if cached:
                            # Store in local global cache for next time
                            try:
                                local_cache_upsert(local_db, [{"hash": h, **cached}])
                            except SAFE_EXCEPT as e:
                                if is_fatal(e):
                                    raise
                                log.debug("Local global cache write failed: %s", e)
                        else:
                            log.debug("Remote cache miss for %s (hash=%s…)", qname, h[:12])
                    except SAFE_EXCEPT as e:
                        if is_fatal(e):
                            raise
                        log.debug("Remote cache lookup failed for %s: %s", qname, e)

                if cached:
                    # Cache hit — re-use existing analysis
                    with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                        with transaction(conn, checkpoint=False):
                            upsert_llm_analysis_batch(
                                conn,
                                [
                                    (
                                        d["id"],
                                        cached["summary"],
                                        cached["inputs"],
                                        cached["outputs"],
                                        cached["model"],
                                        h,
                                    )
                                ],
                            )
                    total += 1
                    elapsed = time.monotonic() - t0
                    log.info("[%d/%d] %s: ok %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed))
                    continue

                # Cache miss — build the prompt with the full body, count its
                # tokens, then call the model.
                prompt = build_analysis_prompt(batch_dicts)
                prompt_tokens = _count_tokens(prompt) if token_counts else None
                if prompt_tokens is None or prompt_tokens + _ANSWER_TOKENS <= run_ctx:
                    try:
                        total += _ask_model(idx, qname, d, h, prompt, run_ctx, t0)
                        continue
                    except OllamaPromptTooLong as e:
                        prompt_tokens = e.prompt_tokens
                long_prompts.append(_LongPrompt(idx, qname, d, h, prompt, prompt_tokens))
                log.info("[%d/%d] %s: later (prompt %d tokens, context %d)",
                         idx + 1, total_symbols, qname, prompt_tokens, run_ctx)
            except LLMStopRun as e:
                # Each next symbol fails the same way: the analysis stops, the
                # stored analyses stay, and a later run continues.
                log.error("LLM analysis stopped at %d/%d: %s", idx + 1, total_symbols, e)
                stopped = True
                break
            except SAFE_EXCEPT as e:
                failed += 1
                elapsed = time.monotonic() - t0
                log.warning("[%d/%d] %s: err %s: %s", idx + 1, total_symbols, qname, _fmt_dur(elapsed), e)
                continue

        # ── Second pass: the prompts that do not fit the context of the run ──
        if stopped:
            long_prompts = []
        too_large: list[_LongPrompt] = []
        fitting: list[_LongPrompt] = []
        for p in long_prompts:
            (too_large if p.prompt_tokens + _ANSWER_TOKENS > largest_ctx else fitting).append(p)
        for p in too_large:
            with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
                with transaction(conn, checkpoint=False):
                    upsert_llm_analysis_batch(
                        conn, [(p.symbol["id"], "", "", "", f"skip:toolarge:{largest_ctx}", p.content_hash)],
                    )
            total += 1
            log.warning("[%d/%d] %s: skip (prompt %d tokens, the model has a context of %d)",
                        p.position + 1, total_symbols, p.qname, p.prompt_tokens, largest_ctx)
        fitting.sort(key=lambda p: p.prompt_tokens)
        if fitting:
            long_ctx = fitting[-1].prompt_tokens + _ANSWER_TOKENS
            log.info("LLM analysis: %d symbols with a long prompt, context %d tokens", len(fitting), long_ctx)
            for p in fitting:
                t0 = time.monotonic()
                try:
                    total += _ask_model(p.position, p.qname, p.symbol, p.content_hash, p.prompt, long_ctx, t0)
                except LLMStopRun as e:
                    log.error("LLM analysis stopped: %s", e)
                    stopped = True
                    break
                except SAFE_EXCEPT as e:
                    if is_fatal(e):
                        raise
                    failed += 1
                    log.warning("[%d/%d] %s: err %s: %s", p.position + 1, total_symbols, p.qname,
                                _fmt_dur(time.monotonic() - t0), e)
    finally:
        if spent.requests:
            log.info("LLM usage: %s", spent.line(time.monotonic() - run_started))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log.info("LLM analysis stored: %d/%d symbols (model=%s)", total, total_symbols, model)
    except SAFE_EXCEPT as e:
        if is_fatal(e):
            raise
        pass  # non-fatal — continue
    finally:
        local_db.close()
    return failed == 0 and not stopped


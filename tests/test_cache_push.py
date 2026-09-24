"""``fw-context cache push`` uploads only what the server lacks, unless told otherwise.

It used to overwrite every server entry, always, on the grounds that a content
hash guarantees an identical analysis.  It does not: the model's output varies
from run to run, and the hash does not cover the prompt.  Observed: 16 entries
with the same hash and model held a different text locally and on the server.

With the overwrite header, a token without ``can_overwrite`` got 403, the
client marked the token read-only and skipped every later chunk, and the
command reported ``Done: 0/N`` with exit 0.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest


@dataclass
class _FakeCacheServerCfg:
    url: str = "https://cache.example"
    token: str = "t"
    # object, not int: the config loader keeps a bad TOML value as it is.
    batch_size: object = 2


@dataclass
class _FakeCfg:
    cache_server: _FakeCacheServerCfg = field(default_factory=_FakeCacheServerCfg)


class _FakeClient:
    """Stands in for CacheClient.  Keeps first-write-wins like the real server."""

    instances: list[_FakeClient] = []
    server: dict[str, dict] = {}
    caps: dict | None = {"can_write": True, "can_overwrite": False}
    refuse_after: int | None = None  # chunks accepted before a 403
    reject_token = False  # with caps=None: stats() got 401/403, not a network error
    stats_failure = ""  # with caps=None: "429" or "html" instead of a network error

    def __init__(self, url: str, token: str, force: bool = False, batch_size: int = 100) -> None:
        from fw_context_mcp.cache_client import _SERVER_MAX_BATCH

        self.force = force
        self.put_failures = 0
        self.auth_rejected = _FakeClient.caps is None and _FakeClient.reject_token
        self.rate_limited = _FakeClient.caps is None and _FakeClient.stats_failure == "429"
        self.invalid_response = _FakeClient.caps is None and _FakeClient.stats_failure == "html"
        self.calls = 0
        self.put_sizes: list[int] = []
        # The same limit as the real client: the CLI must read it from here.
        self.batch_size = max(1, min(batch_size, _SERVER_MAX_BATCH))
        _FakeClient.instances.append(self)

    def stats(self) -> dict | None:
        return self.caps

    def batch_put(self, entries: list[dict]) -> int:
        if self.refuse_after is not None and self.calls >= self.refuse_after:
            self.put_failures += 1
            return 0
        self.calls += 1
        self.put_sizes.append(len(entries))
        n = 0
        for e in entries:
            if e["hash"] not in self.server or self.force:
                self.server[e["hash"]] = e
                n += 1
        return n

    def close(self) -> None:
        pass


def _h(name: str) -> str:
    """A valid content hash for the short test name *name*: the server takes only SHA-256."""
    return hashlib.sha256(name.encode()).hexdigest()


def _local_db(path: Path, names: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE llm_analysis_cache (content_hash TEXT PRIMARY KEY, summary TEXT, "
        "inputs TEXT, outputs TEXT, model TEXT)"
    )
    conn.executemany(
        "INSERT INTO llm_analysis_cache VALUES (?, ?, '', '', 'm')", [(_h(n), f"local {n}") for n in names]
    )
    conn.commit()
    return conn


@pytest.fixture
def push(monkeypatch, tmp_path: Path):
    """Run cmd_cache_push against a fake server; return (exit code, server)."""
    import fw_context_mcp.cache_client as cache_client_mod
    import fw_context_mcp.config as config_mod
    import fw_context_mcp.utils as utils_mod

    _FakeClient.instances = []
    _FakeClient.server = {}
    _FakeClient.caps = {"can_write": True, "can_overwrite": False}
    _FakeClient.refuse_after = None
    _FakeClient.reject_token = False
    _FakeClient.stats_failure = ""

    monkeypatch.setattr(cache_client_mod, "CacheClient", _FakeClient)
    monkeypatch.setattr(utils_mod, "resolve_project_root", lambda arg: tmp_path)

    def _run(
        local: list[str],
        *,
        overwrite: bool = False,
        batch: int | None = None,
        config_batch_size: object = 2,
        extra_rows: tuple[tuple, ...] = (),
    ) -> int:
        cfg = _FakeCfg(cache_server=_FakeCacheServerCfg(batch_size=config_batch_size))
        monkeypatch.setattr(config_mod, "load", lambda project_root=None: cfg)
        db_path = tmp_path / "llm_cache.db"
        db_path.unlink(missing_ok=True)
        conn = _local_db(db_path, local)
        conn.executemany("INSERT INTO llm_analysis_cache VALUES (?, ?, ?, ?, ?)", extra_rows)
        conn.commit()
        conn.close()
        monkeypatch.setattr(
            cache_client_mod, "get_local_cache_db", lambda readonly=False: sqlite3.connect(db_path)
        )
        from fw_context_mcp.cli._cache import cmd_cache_push

        return cmd_cache_push(SimpleNamespace(project=None, batch=batch, overwrite=overwrite))

    return _run


def test_only_missing_entries_are_uploaded(push, capsys):
    _FakeClient.server = {_h("a"): {"hash": _h("a"), "summary": "server a"}}

    assert push(["a", "b", "c"]) == 0

    assert _FakeClient.instances[0].force is False, "no overwrite header without --overwrite"
    assert _FakeClient.server[_h("a")]["summary"] == "server a", "the server's entry must survive"
    assert set(_FakeClient.server) == {_h("a"), _h("b"), _h("c")}
    assert "2 inserted, 1 already on" in capsys.readouterr().out


def test_overwrite_replaces_server_entries(push):
    _FakeClient.caps = {"can_write": True, "can_overwrite": True}
    _FakeClient.server = {_h("a"): {"hash": _h("a"), "summary": "server a"}}

    assert push(["a", "b"], overwrite=True) == 0

    assert _FakeClient.instances[0].force is True
    assert _FakeClient.server[_h("a")]["summary"] == "local a"


def test_overwrite_without_the_permission_is_refused_up_front(push, capsys):
    """Used to end in `Done: 0/N` with exit 0 — every chunk silently skipped."""
    assert push(["a"], overwrite=True) == 1

    assert _FakeClient.instances[0].calls == 0, "nothing may be sent"
    assert "can_overwrite" in capsys.readouterr().err


def test_a_read_only_token_is_an_error(push, capsys):
    _FakeClient.caps = {"can_write": False, "can_overwrite": False}

    assert push(["a"]) == 1
    assert "read-only" in capsys.readouterr().err


def test_an_unreachable_server_is_an_error(push, capsys):
    _FakeClient.caps = None

    assert push(["a"]) == 1
    assert "not reachable" in capsys.readouterr().err


def test_a_rejected_token_is_not_reported_as_unreachable(push, capsys):
    """A revoked or mistyped token sent the user looking for a network fault."""
    _FakeClient.caps = None
    _FakeClient.reject_token = True

    assert push(["a"]) == 1
    err = capsys.readouterr().err
    assert "rejected the token" in err
    assert "not reachable" not in err


def test_a_write_refused_midway_is_an_error_not_already_on_server(push, capsys):
    """A failed chunk returns 0 like a chunk that was all present — the counter tells them apart."""
    _FakeClient.refuse_after = 1

    assert push(["a", "b", "c", "d"]) == 1

    err = capsys.readouterr().err
    assert "did not accept the write after 2/4 entries (2 inserted)" in err


def test_a_batch_larger_than_the_server_takes_is_one_request_per_step(push, capsys):
    """The CLI stepped by the requested size, and the client by the limited one.

    ``--batch 5000`` thus gave one ``batch_put`` of 2500 entries, which the
    client sent as three requests.  A failure in the third request lost the
    count of the first two, and the client sent the rest after a 403.
    """
    local = [f"h{i}" for i in range(2500)]

    assert push(local, batch=5000) == 0

    assert _FakeClient.instances[0].put_sizes == [1000, 1000, 500]
    assert "[2500/2500] inserted 500" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [0, -5, "100", True])
def test_a_bad_config_batch_size_is_an_error(push, capsys, bad):
    """-5 gave an empty loop and "Done: 0 inserted, N already on" with exit 0.

    0 gave a ValueError from ``range()`` and a traceback.
    """
    assert push(["a", "b"], config_batch_size=bad) == 1

    captured = capsys.readouterr()
    assert "batch_size must be a positive integer" in captured.err
    assert "Done" not in captured.out
    assert _FakeClient.instances == [], "no request may start"


@pytest.mark.parametrize("text", ["0", "-5", "x"])
def test_the_batch_option_refuses_a_value_below_one(text):
    import argparse

    from fw_context_mcp.cli import _positive_int

    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int(text)


def test_the_batch_option_takes_a_positive_value():
    from fw_context_mcp.cli import _positive_int

    assert _positive_int("5000") == 5000


# ── The real CacheClient, against a mock transport ──────────────────────────


def _client(handler, **kw):
    import httpx

    from fw_context_mcp.cache_client import CacheClient

    cc = CacheClient(url="https://cache.example", token="t", **kw)
    cc._session = httpx.Client(base_url=cc.url, transport=httpx.MockTransport(handler))
    return cc


def _stats_or(handler):
    """Answer /cache/stats with a writable token; hand everything else to *handler*."""
    import httpx

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cache/stats":
            return httpx.Response(200, json={"can_write": True, "can_overwrite": False})
        return handler(request)

    return wrapped


def _entries(n: int) -> list[dict]:
    return [{"hash": str(i), "summary": "", "inputs": "", "outputs": "", "model": "m"} for i in range(n)]


def test_the_batch_size_never_exceeds_what_the_server_takes():
    """The server keeps the first 1000 entries of a request and drops the rest.

    Observed shape: `cache push --batch 5000` would print `1000 inserted,
    4000 already on` with exit 0 — the 4000 were dropped, not present.
    """
    import json

    import httpx

    from fw_context_mcp.cache_client import _SERVER_MAX_BATCH

    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["entries"])
        sizes.append(n)
        return httpx.Response(200, json={"inserted": n, "total": n, "truncated": n > _SERVER_MAX_BATCH})

    cc = _client(_stats_or(handler), batch_size=5000)

    assert cc.batch_put(_entries(2500)) == 2500
    assert max(sizes) <= _SERVER_MAX_BATCH
    assert cc.put_failures == 0


def test_a_truncated_answer_counts_as_a_failure():
    """A server with a lower cap than ours must not be a silent loss."""
    import httpx

    cc = _client(_stats_or(lambda request: httpx.Response(200, json={"inserted": 1, "total": 1, "truncated": True})))

    cc.batch_put(_entries(2))
    assert cc.put_failures == 1


@pytest.mark.parametrize("status", [401, 403])
def test_stats_reports_a_rejected_token(status: int):
    import httpx

    cc = _client(lambda request: httpx.Response(status))
    assert cc.stats() is None
    assert cc.auth_rejected is True


def test_stats_does_not_blame_the_token_for_an_unreachable_server(monkeypatch):
    import httpx

    import fw_context_mcp.cache_client as cache_client_mod

    monkeypatch.setattr(cache_client_mod.time, "sleep", lambda s: None)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    cc = _client(unreachable)
    assert cc.stats() is None
    assert cc.auth_rejected is False


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("429", "limits the failed attempts (429)"), ("html", "body that is not a JSON object")],
)
def test_a_running_server_is_not_reported_as_unreachable(push, capsys, failure, expected):
    """429 and an HTML page both come from a server that runs."""
    _FakeClient.caps = None
    _FakeClient.stats_failure = failure

    assert push(["a"]) == 1
    err = capsys.readouterr().err
    assert expected in err
    assert "not reachable" not in err


def test_stats_reports_a_rate_limit(monkeypatch):
    """The middleware of the server answers 429 to too many failed attempts."""
    import httpx

    import fw_context_mcp.cache_client as cache_client_mod

    monkeypatch.setattr(cache_client_mod.time, "sleep", lambda s: None)
    cc = _client(lambda request: httpx.Response(429, json={"detail": "Too many auth attempts"}))

    assert cc.stats() is None
    assert cc.rate_limited is True
    assert cc.auth_rejected is False


_HTML = "<html><body>Sign in to the proxy</body></html>"


def test_stats_survives_an_html_answer():
    """A proxy answers 200 with HTML.  resp.json() raised JSONDecodeError here."""
    import httpx

    cc = _client(
        lambda request: httpx.Response(200, text=_HTML, headers={"content-type": "text/html"})
    )

    assert cc.stats() is None
    assert cc.invalid_response is True


def test_a_json_answer_that_is_not_an_object_is_invalid():
    import httpx

    cc = _client(lambda request: httpx.Response(200, json=["not", "an", "object"]))

    assert cc.stats() is None
    assert cc.invalid_response is True


def test_a_write_with_an_html_answer_counts_as_a_failure():
    """Nothing says that the server stored the chunk, thus it is not a success."""
    import httpx

    cc = _client(_stats_or(lambda request: httpx.Response(200, text=_HTML)))

    assert cc.batch_put(_entries(2)) == 0
    assert cc.put_failures == 1


def test_a_read_with_an_html_answer_gives_no_results():
    import httpx

    cc = _client(lambda request: httpx.Response(200, text=_HTML))

    assert cc.batch_get(["a", "b"]) == {"a": None, "b": None}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("429", "limits the failed attempts (429)"), ("html", "not a JSON object"), ("", "is not reachable")],
)
def test_cache_stats_remote_names_the_cause(push, capsys, failure, expected):
    """``cache stats --remote`` said "Server unreachable" for each of the causes."""
    push([])  # installs the fakes and the config
    _FakeClient.caps = None
    _FakeClient.stats_failure = failure
    from fw_context_mcp.cli._cache import cmd_cache_stats

    assert cmd_cache_stats(SimpleNamespace(remote=True, project="x")) == 0
    assert expected in capsys.readouterr().out


def test_stats_forgets_the_cause_of_an_earlier_call(monkeypatch):
    """A long-lived client must not explain a later failure with an old flag."""
    import httpx

    import fw_context_mcp.cache_client as cache_client_mod

    monkeypatch.setattr(cache_client_mod.time, "sleep", lambda s: None)
    answers = iter([httpx.Response(429)] * 3)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return next(answers)
        except StopIteration:
            raise httpx.ConnectError("down", request=request) from None

    cc = _client(handler)
    assert cc.stats() is None and cc.rate_limited is True
    assert cc.stats() is None
    assert cc.rate_limited is False, "the second failure is a network fault, not 429"


def test_a_proxy_that_answers_html_gives_one_warning(caplog):
    """The indexer asks once for each symbol: thousands of the same line."""
    import logging

    import httpx

    cc = _client(lambda request: httpx.Response(200, text=_HTML))
    with caplog.at_level(logging.DEBUG, logger="fw_context_mcp.cache_client"):
        for _ in range(5):
            cc.batch_get(["a"])

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "JSON object" in r.message]
    assert len(warnings) == 1


def _remote_init(monkeypatch, tmp_path: Path, stats_answer) -> int:
    """Run ``cache remote init`` against a mock server, with typed answers."""
    import httpx

    import fw_context_mcp.config.settings as settings_mod
    from fw_context_mcp.cli import _cache as cache_mod

    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "_ensure_global_config", lambda: config)
    answers = iter(["https://cache.example", "token"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return stats_answer

    real_client = httpx.Client
    monkeypatch.setattr(
        cache_mod.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    return cache_mod.cmd_cache_remote_init(SimpleNamespace())


def test_remote_init_survives_an_html_answer(monkeypatch, tmp_path: Path, capsys):
    """``auth_resp.json()`` raised JSONDecodeError past every handler."""
    import httpx

    answer = httpx.Response(200, text=_HTML, headers={"content-type": "text/html"})

    assert _remote_init(monkeypatch, tmp_path, answer) == 1
    err = capsys.readouterr().err
    assert "not a JSON object" in err
    assert "text/html" in err


def test_remote_init_names_the_token_for_a_429(monkeypatch, tmp_path: Path, capsys):
    import httpx

    assert _remote_init(monkeypatch, tmp_path, httpx.Response(429)) == 1
    assert "check your token" in capsys.readouterr().err


# ── The limits of the server, on the side of the client ────────────────────


def test_an_entry_that_breaks_a_limit_stays_local_and_the_rest_goes(push, capsys):
    """One summary of 5001 characters got 422 for its whole request, on each push.

    The entries after it thus never reached the server.
    """
    too_long = (_h("long"), "x" * 5001, "", "", "m")
    no_text = (_h("null"), None, "", "", "m")

    assert push(["a", "b"], extra_rows=(too_long, no_text)) == 0

    assert set(_FakeClient.server) == {_h("a"), _h("b")}
    captured = capsys.readouterr()
    assert "2 local entries break a limit of the server" in captured.err
    assert "summary has 5001 characters, more than 5000" in captured.err
    assert "summary is not a string" in captured.err
    assert "Done: 2 inserted, 0 already on" in captured.out


def test_large_entries_are_split_by_size():
    """1000 entries at the field limits are about 200 MB, and the server takes 10 MB."""
    import json

    import httpx

    from fw_context_mcp.cache_limits import MAX_BODY_BYTES

    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sizes.append(len(request.content))
        n = len(json.loads(request.content)["entries"])
        return httpx.Response(200, json={"inserted": n, "total": n, "truncated": False})

    big = [
        {"hash": _h(str(i)), "summary": "", "inputs": "é" * 90_000, "outputs": "", "model": "m"}
        for i in range(200)
    ]
    cc = _client(_stats_or(handler), batch_size=1000)

    assert cc.batch_put(big) == 200
    assert len(sizes) > 1
    assert max(sizes) <= MAX_BODY_BYTES


def test_no_request_goes_after_the_first_that_failed():
    """A 403 or an unreachable server fails each more request too."""
    import httpx

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(422, json={"detail": "bad"})

    cc = _client(_stats_or(handler), batch_size=2)

    assert cc.batch_put(_entries(6)) == 0
    assert len(calls) == 1
    assert cc.put_failures == 1


@pytest.mark.parametrize(
    ("field", "length", "ok"),
    [("summary", 5000, True), ("summary", 5001, False), ("model", 100, True), ("model", 101, False),
     ("inputs", 100_000, True), ("inputs", 100_001, False)],
)
def test_the_client_and_the_server_apply_the_same_limits(field, length, ok):
    """The two sides take their limits from one module, thus they cannot drift."""
    pytest.importorskip("fastapi")
    import pydantic

    from fw_context_mcp.cache_limits import entry_violation
    from fw_context_mcp.cache_server.app import CacheEntry

    entry = {"hash": _h("x"), "summary": "", "inputs": "", "outputs": "", "model": "m"}
    entry[field] = "y" * length

    assert (entry_violation(entry) is None) is ok
    try:
        CacheEntry(**entry)
        server_ok = True
    except pydantic.ValidationError:
        server_ok = False
    assert server_ok is ok


@pytest.mark.parametrize("bad_hash", ["a", "A" * 64, "g" * 64, "a" * 63])
def test_a_bad_hash_breaks_the_limit_on_both_sides(bad_hash):
    pytest.importorskip("fastapi")
    import pydantic

    from fw_context_mcp.cache_limits import entry_violation
    from fw_context_mcp.cache_server.app import CacheEntry

    entry = {"hash": bad_hash, "summary": "", "inputs": "", "outputs": "", "model": "m"}

    assert entry_violation(entry) is not None
    with pytest.raises(pydantic.ValidationError):
        CacheEntry(**entry)

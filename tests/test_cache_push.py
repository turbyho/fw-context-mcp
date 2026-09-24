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

    def __init__(self, url: str, token: str, force: bool = False, batch_size: int = 100) -> None:
        from fw_context_mcp.cache_client import _SERVER_MAX_BATCH

        self.force = force
        self.put_failures = 0
        self.auth_rejected = _FakeClient.caps is None and _FakeClient.reject_token
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


def _local_db(path: Path, hashes: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE llm_analysis_cache (content_hash TEXT PRIMARY KEY, summary TEXT, "
        "inputs TEXT, outputs TEXT, model TEXT)"
    )
    conn.executemany(
        "INSERT INTO llm_analysis_cache VALUES (?, ?, '', '', 'm')", [(h, f"local {h}") for h in hashes]
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

    monkeypatch.setattr(cache_client_mod, "CacheClient", _FakeClient)
    monkeypatch.setattr(utils_mod, "resolve_project_root", lambda arg: tmp_path)

    def _run(
        local: list[str],
        *,
        overwrite: bool = False,
        batch: int | None = None,
        config_batch_size: object = 2,
    ) -> int:
        cfg = _FakeCfg(cache_server=_FakeCacheServerCfg(batch_size=config_batch_size))
        monkeypatch.setattr(config_mod, "load", lambda project_root=None: cfg)
        db_path = tmp_path / "llm_cache.db"
        db_path.unlink(missing_ok=True)
        _local_db(db_path, local).close()
        monkeypatch.setattr(
            cache_client_mod, "get_local_cache_db", lambda readonly=False: sqlite3.connect(db_path)
        )
        from fw_context_mcp.cli._cache import cmd_cache_push

        return cmd_cache_push(SimpleNamespace(project=None, batch=batch, overwrite=overwrite))

    return _run


def test_only_missing_entries_are_uploaded(push, capsys):
    _FakeClient.server = {"a": {"hash": "a", "summary": "server a"}}

    assert push(["a", "b", "c"]) == 0

    assert _FakeClient.instances[0].force is False, "no overwrite header without --overwrite"
    assert _FakeClient.server["a"]["summary"] == "server a", "the server's entry must survive"
    assert set(_FakeClient.server) == {"a", "b", "c"}
    assert "2 inserted, 1 already on" in capsys.readouterr().out


def test_overwrite_replaces_server_entries(push):
    _FakeClient.caps = {"can_write": True, "can_overwrite": True}
    _FakeClient.server = {"a": {"hash": "a", "summary": "server a"}}

    assert push(["a", "b"], overwrite=True) == 0

    assert _FakeClient.instances[0].force is True
    assert _FakeClient.server["a"]["summary"] == "local a"


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

"""Tests for the cache server — auth middleware and API endpoints.

Uses FastAPI TestClient with a mock backend — no PostgreSQL required.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fw_context_mcp import __version__ as fw_context_version


class MockBackend:
    """In-memory mock of CacheBackend for testing."""

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._tokens: list[dict] = []
        self._next_token_id = 1

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def init_schema(self) -> None:
        pass

    async def validate_token(self, token: str) -> dict | None:
        for t in self._tokens:
            if t["token"] == token and t.get("revoked_at") is None:
                return {
                    "project_id": t["project_id"],
                    "can_read": t["can_read"],
                    "can_write": t["can_write"],
                    "can_overwrite": t["can_overwrite"],
                }
        return None

    async def batch_get(self, hashes: list[str]) -> dict[str, dict | None]:
        return {h: self._cache.get(h) for h in hashes}

    async def batch_put(self, entries: list[dict], *, can_overwrite: bool = False) -> int:
        inserted = 0
        for e in entries:
            h = e["hash"]
            if can_overwrite or h not in self._cache:
                self._cache[h] = {
                    "summary": e["summary"],
                    "inputs": e["inputs"],
                    "outputs": e["outputs"],
                    "model": e["model"],
                    "analyzed_at": "2025-01-01T00:00:00",
                }
                inserted += 1
        return inserted

    def add_token(self, token: str, project_id: str = "test/proj", *, can_read: bool = True,
                  can_write: bool = False, can_overwrite: bool = False) -> None:
        self._tokens.append({
            "id": self._next_token_id,
            "token": token,
            "project_id": project_id,
            "can_read": can_read,
            "can_write": can_write,
            "can_overwrite": can_overwrite,
            "revoked_at": None,
        })
        self._next_token_id += 1


@pytest.fixture
def mock_backend() -> MockBackend:
    return MockBackend()


@pytest.fixture
def client(mock_backend: MockBackend) -> TestClient:
    from fw_context_mcp.cache_server.app import create_app
    app = create_app(backend=mock_backend)
    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestHealthEndpoint:
    def test_health_public(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "version": fw_context_version}


class TestAuth:
    def test_missing_token_returns_401(self, client: TestClient) -> None:
        response = client.post("/cache/batch", json={"hashes": ["abc"]})
        assert response.status_code == 401
        assert "Missing Authorization" in response.json()["detail"]

    def test_invalid_token_returns_401(self, client: TestClient) -> None:
        response = client.post("/cache/batch", json={"hashes": ["abc"]}, headers=_auth("invalid"))
        assert response.status_code == 401
        assert "Invalid or revoked" in response.json()["detail"]

    def test_read_only_token_can_read(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("read-token", can_read=True, can_write=False)
        response = client.post("/cache/batch", json={"hashes": ["abc"]}, headers=_auth("read-token"))
        assert response.status_code == 200

    def test_read_only_token_cannot_write(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("read-token", can_read=True, can_write=False)
        response = client.put("/cache/batch", json={"entries": []}, headers=_auth("read-token"))
        assert response.status_code == 403
        assert "write permission" in response.json()["detail"]

    def test_overwrite_header_requires_can_overwrite(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("write-token", can_read=True, can_write=True, can_overwrite=False)
        headers = {**_auth("write-token"), "X-Cache-Overwrite": "true"}
        response = client.put("/cache/batch", json={"entries": []}, headers=headers)
        assert response.status_code == 403
        assert "overwrite" in response.json()["detail"]


class TestBatchGet:
    def test_empty_hashes(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("token", can_read=True)
        response = client.post("/cache/batch", json={"hashes": []}, headers=_auth("token"))
        assert response.status_code == 200
        assert response.json() == {"results": {}, "truncated": False}

    def test_cache_hit(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("token", can_read=True)
        mock_backend._cache["hash1"] = {"summary": "s", "inputs": "i", "outputs": "o", "model": "m",
                                         "analyzed_at": "2025-01-01"}
        response = client.post("/cache/batch", json={"hashes": ["hash1"]}, headers=_auth("token"))
        data = response.json()
        assert data["results"]["hash1"] is not None
        assert data["results"]["hash1"]["summary"] == "s"

    def test_cache_miss(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("token", can_read=True)
        response = client.post("/cache/batch", json={"hashes": ["hash1"]}, headers=_auth("token"))
        assert response.json()["results"]["hash1"] is None


class TestBatchPut:
    def test_put_requires_write(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("write-token", can_read=True, can_write=True)
        entry = {"hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "summary": "s", "inputs": "i", "outputs": "o", "model": "m"}
        response = client.put("/cache/batch", json={"entries": [entry]}, headers=_auth("write-token"))
        assert response.status_code == 200
        assert response.json()["inserted"] == 1

    def test_put_no_overwrite_by_default(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("write-token", can_read=True, can_write=True)
        mock_backend._cache["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"] = {"summary": "old", "inputs": "old", "outputs": "old", "model": "old",
                                      "analyzed_at": "2025-01-01"}
        entry = {"hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "summary": "new", "inputs": "new", "outputs": "new", "model": "new"}
        response = client.put("/cache/batch", json={"entries": [entry]}, headers=_auth("write-token"))
        assert response.json()["inserted"] == 0  # DO NOTHING
        assert mock_backend._cache["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]["summary"] == "old"

    def test_put_with_overwrite(self, client: TestClient, mock_backend: MockBackend) -> None:
        mock_backend.add_token("ow-token", can_read=True, can_write=True, can_overwrite=True)
        mock_backend._cache["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"] = {"summary": "old", "inputs": "old", "outputs": "old", "model": "old",
                                      "analyzed_at": "2025-01-01"}
        entry = {"hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "summary": "new", "inputs": "new", "outputs": "new", "model": "new"}
        headers = {**_auth("ow-token"), "X-Cache-Overwrite": "true"}
        response = client.put("/cache/batch", json={"entries": [entry]}, headers=headers)
        assert response.json()["inserted"] == 1
        assert mock_backend._cache["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]["summary"] == "new"


class TestNginxConfig:
    def test_generate_nginx_config(self) -> None:
        from fw_context_mcp.cache_server.nginx_config import generate_nginx_config
        config = generate_nginx_config("fw-cache.example.com")
        assert "fw-cache.example.com" in config
        assert "listen 443 ssl" in config
        assert "limit_req" in config


class TestInstall:
    def test_generate_systemd_unit(self) -> None:
        from fw_context_mcp.cache_server.install import generate_systemd_unit
        unit = generate_systemd_unit(user="fw-cache")
        assert "User=fw-cache" in unit
        assert "EnvironmentFile=/var/lib/fw-cache-server/db.env" in unit
        assert "ExecStart=" in unit

    def test_generate_launchd_plist(self) -> None:
        from fw_context_mcp.cache_server.install import generate_launchd_plist
        plist = generate_launchd_plist(host="0.0.0.0", port=9000)
        assert "com.fwcontext.cache-server" in plist
        assert "9000" in plist
        assert "FW_CACHE_DB_URL" not in plist  # credential leak regression guard

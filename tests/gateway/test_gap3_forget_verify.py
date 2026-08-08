"""Ad-hoc verification for forget_sessions wiring in _handle_delete_session (gap3)."""
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


class FakeStore:
    sessions_dir = "C:/tmp/sessions"

    def __init__(self, result=1, raise_on_forget=False):
        self.result = result
        self.raise_on_forget = raise_on_forget
        self.calls = []

    def forget_sessions(self, ids):
        self.calls.append(list(ids))
        if self.raise_on_forget:
            raise RuntimeError("boom")
        return self.result


def _make_app(adapter):
    app = web.Application()
    app.router.add_delete("/api/sessions/{session_id}", adapter._handle_delete_session)
    return app


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    try:
        yield d
    finally:
        close = getattr(d, "close", None)
        if callable(close):
            close()


def _adapter(db, store):
    a = APIServerAdapter(PlatformConfig(enabled=True))
    a._session_db = db
    a._session_store = store
    return a


@pytest.mark.asyncio
async def test_delete_calls_forget_sessions_and_logs_result(db):
    db.create_session("sess-1", "api_server")
    store = FakeStore(result=1)
    app = _make_app(_adapter(db, store))
    async with TestClient(TestServer(app)) as cli:
        with patch("gateway.platforms.api_server.logger") as mock_logger:
            resp = await cli.delete("/api/sessions/sess-1")
            assert resp.status == 200
            body = await resp.json()
            assert body == {"object": "hermes.session.deleted", "id": "sess-1", "deleted": True}
            assert store.calls == [["sess-1"]]
            info_args = [c.args[0] for c in mock_logger.info.call_args_list]
            assert any("forget_sessions removed" in a for a in info_args)
            assert mock_logger.warning.call_count == 0


@pytest.mark.asyncio
async def test_delete_store_without_forget_sessions_is_skipped(db):
    db.create_session("sess-2", "api_server")

    class NoForgetStore(FakeStore):
        forget_sessions = None  # attribute present but not callable

    store = NoForgetStore()
    app = _make_app(_adapter(db, store))
    async with TestClient(TestServer(app)) as cli:
        with patch("gateway.platforms.api_server.logger") as mock_logger:
            resp = await cli.delete("/api/sessions/sess-2")
            assert resp.status == 200
            assert (await resp.json())["deleted"] is True
            assert store.calls == []
            assert mock_logger.debug.call_count == 1
            assert mock_logger.warning.call_count == 0


@pytest.mark.asyncio
async def test_delete_forget_exception_never_fails_delete(db):
    db.create_session("sess-3", "api_server")
    store = FakeStore(raise_on_forget=True)
    app = _make_app(_adapter(db, store))
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.delete("/api/sessions/sess-3")
        assert resp.status == 200
        assert (await resp.json())["deleted"] is True
        assert store.calls == [["sess-3"]]


@pytest.mark.asyncio
async def test_delete_unknown_session_404_does_not_call_forget(db):
    store = FakeStore()
    app = _make_app(_adapter(db, store))
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.delete("/api/sessions/ghost-session")
        assert resp.status == 404
        assert store.calls == []

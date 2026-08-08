"""Tests for SessionStore.forget_sessions (GAP-3, TDD).

Contract under test (implemented in parallel in gateway/session.py):

    SessionStore.forget_sessions(session_ids: List[str]) -> int

* removes from ``_entries`` every entry whose ``entry.session_id`` is in
  ``session_ids``;
* re-persists the routing index (state.db ``gateway_routing`` table + the
  legacy sessions.json mirror) WITHOUT the dead entries;
* returns the number of entries removed;
* 0 removed => no save (durable files untouched).

Isolation: every test gets its own ``state.db`` (``DEFAULT_DB_PATH`` is
module-level and shared by every ``SessionDB()`` in the process), and its own
``sessions_dir`` under ``tmp_path``.  Never touches ``~/.hermes``.
"""

import json
import threading

import hermes_state
import pytest

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionSource, SessionStore


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Each test gets its own state.db — rows would otherwise leak between
    tests because DEFAULT_DB_PATH is module-level."""
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")


def _make_store(tmp_path) -> SessionStore:
    config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    return SessionStore(sessions_dir=tmp_path / "sessions", config=config)


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_name=f"chat {chat_id}",
        chat_type="dm",
        user_id=chat_id,
    )


def _routing_rows(store) -> dict:
    """Read back the gateway_routing table for this store's scope."""
    return store._db.load_gateway_routing_entries(scope=store._routing_scope())


def _sessions_json(store) -> dict:
    return json.loads((store.sessions_dir / "sessions.json").read_text(encoding="utf-8"))


class TestForgetSessions:
    def test_forget_removes_entry_and_repersists_both_layers(self, tmp_path):
        """(a) A real persisted entry is dropped from memory AND from both
        durable layers (gateway_routing table + sessions.json)."""
        store = _make_store(tmp_path)
        try:
            entry = store.get_or_create_session(_source("chat-1"))
            key, sid = entry.session_key, entry.session_id
            assert key in _routing_rows(store)
            assert key in _sessions_json(store)

            removed = store.forget_sessions([sid])

            assert removed == 1
            assert key not in store._entries
            assert key not in _routing_rows(store)
            data = _sessions_json(store)
            assert key not in data
            # The legacy-mirror sentinel survives the rewrite.
            assert "_README" in data
        finally:
            store._db.close()

    def test_forget_unknown_id_returns_zero_and_does_not_rewrite(
        self, tmp_path, monkeypatch
    ):
        """(b) Unknown id -> returns 0 and NO persistence happens at all:
        no _persist_routing_data call, sessions.json bytes+mtime untouched,
        routing table rows unchanged."""
        store = _make_store(tmp_path)
        try:
            entry = store.get_or_create_session(_source("chat-1"))
            sessions_file = store.sessions_dir / "sessions.json"
            json_before = sessions_file.read_bytes()
            mtime_before = sessions_file.stat().st_mtime_ns
            rows_before = _routing_rows(store)

            save_calls = []
            monkeypatch.setattr(
                store,
                "_persist_routing_data",
                lambda data, generation: save_calls.append(generation),
            )

            removed = store.forget_sessions(["20990101_000000_no_such_session"])

            assert removed == 0
            assert save_calls == [], "0 removals must not trigger a save"
            assert sessions_file.read_bytes() == json_before
            assert sessions_file.stat().st_mtime_ns == mtime_before
            assert _routing_rows(store) == rows_before
            assert store._entries[entry.session_key].session_id == entry.session_id
        finally:
            store._db.close()

    def test_forget_removes_only_target_among_multiple(self, tmp_path):
        """(c) Multiple entries, only one targeted -> only it falls, in memory
        and in both durable layers."""
        store = _make_store(tmp_path)
        try:
            entries = {}
            for chat in ("chat-1", "chat-2", "chat-3"):
                entry = store.get_or_create_session(_source(chat))
                entries[entry.session_key] = entry
            keys = list(entries)
            target_key = keys[1]

            removed = store.forget_sessions([entries[target_key].session_id])

            assert removed == 1
            assert target_key not in store._entries
            rows = _routing_rows(store)
            data = _sessions_json(store)
            for key, entry in entries.items():
                if key == target_key:
                    assert key not in rows
                    assert key not in data
                else:
                    assert key in rows  # survives in DB routing table
                    assert data[key]["session_id"] == entry.session_id  # and JSON mirror
        finally:
            store._db.close()

    def test_forget_concurrent_threads_consistent(self, tmp_path):
        """(d) Two threads calling forget_sessions concurrently on the same
        store with disjoint targets -> no exception, each returns its own
        count, both targets dropped from memory and both durable layers,
        survivor intact."""
        store = _make_store(tmp_path)
        try:
            entries = {}
            for chat in ("chat-1", "chat-2", "chat-3"):
                entry = store.get_or_create_session(_source(chat))
                entries[entry.session_key] = entry
            keys = list(entries)
            targets = [entries[keys[0]].session_id, entries[keys[1]].session_id]
            survivor_key = keys[2]

            barrier = threading.Barrier(2)
            results = {}
            results_lock = threading.Lock()

            def worker(i):
                barrier.wait()  # maximize contention
                try:
                    n = store.forget_sessions([targets[i]])
                    with results_lock:
                        results[i] = n
                except Exception as exc:  # pragma: no cover - failure path
                    with results_lock:
                        results[i] = f"ERR:{exc}"

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert results == {0: 1, 1: 1}, results
            assert keys[0] not in store._entries
            assert keys[1] not in store._entries
            assert survivor_key in store._entries
            rows = _routing_rows(store)
            assert keys[0] not in rows
            assert keys[1] not in rows
            assert survivor_key in rows
            data = _sessions_json(store)
            assert keys[0] not in data
            assert keys[1] not in data
            assert survivor_key in data
        finally:
            store._db.close()

    def test_forget_works_on_not_yet_loaded_store(self, tmp_path):
        """(e) Lazy store: forget_sessions on a store that has never loaded
        must load the index first, then remove and persist."""
        store = _make_store(tmp_path)
        entry = store.get_or_create_session(_source("chat-1"))
        key, sid = entry.session_key, entry.session_id
        store._db.close()

        restarted = _make_store(tmp_path)
        try:
            assert restarted._loaded is False

            removed = restarted.forget_sessions([sid])

            assert removed == 1
            assert key not in restarted._entries
            assert key not in _routing_rows(restarted)
            assert key not in _sessions_json(restarted)
        finally:
            restarted._db.close()

    def test_forget_returns_removed_count_and_ignores_unknown_ids(self, tmp_path):
        """(f) Return value counts only real removals: mixed list with unknown
        ids counts only the matched ones; a second forget of an already-removed
        id returns 0."""
        store = _make_store(tmp_path)
        try:
            entries = {}
            for chat in ("chat-1", "chat-2", "chat-3"):
                entry = store.get_or_create_session(_source(chat))
                entries[entry.session_key] = entry
            keys = list(entries)

            mixed = store.forget_sessions(
                [
                    entries[keys[0]].session_id,
                    "20990101_000000_ghost",
                    entries[keys[1]].session_id,
                ]
            )
            assert mixed == 2

            assert store.forget_sessions([entries[keys[2]].session_id]) == 1
            # Already removed -> 0, and the index is empty everywhere.
            assert store.forget_sessions([entries[keys[2]].session_id]) == 0
            assert store._entries == {}
            assert _routing_rows(store) == {}
            assert set(_sessions_json(store)) == {"_README"}
        finally:
            store._db.close()

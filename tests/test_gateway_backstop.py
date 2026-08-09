"""Multi-process backstop tests for the gateway routing index (GAP-3).

When several hermes processes share one ``state.db`` (gateway + desktop +
CLI + MCP workers), any of them can delete a session row through
``SessionDB.delete_session`` while the gateway still holds the matching
entry in its in-memory routing index (``SessionStore._entries``). A naive
``_save_entries()`` then re-persists that ghost entry into the
``gateway_routing`` table and the sessions.json mirror, resurrecting a
route to a session that no longer exists.

Contract under test (backstop implemented in ``gateway/session.py``, in
``_save_entries`` / ``_persist_routing_data``):

* Entries with ``db_persisted=True`` whose row no longer exists in
  ``state.db`` (deleted by ANOTHER process) are DROPPED before persistence
  — the implementation checks existence with a batch query
  (``SELECT id FROM sessions WHERE id IN (...)``).
* Legacy entries with ``db_persisted=False`` (pre-SQLite routing entries
  that never had a state.db row) are ALWAYS preserved — absence from the
  DB is expected for them, not evidence of deletion.
* A DB failure during the existence check must not break the save
  (try/except, fail-safe: entries are preserved, nothing is dropped).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore
from hermes_state import SessionDB


@pytest.fixture()
def _isolated_db(tmp_path, monkeypatch):
    """Point state.db at tmp_path so tests never touch ~/.hermes."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _make_store(tmp_path):
    return SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())


def _slack_source(chat_id):
    return SessionSource(
        platform=Platform.SLACK,
        chat_id=chat_id,
        chat_type="channel",
        user_id="U1",
    )


def _persisted_routing(tmp_path, store):
    """Read the routing index as a *fresh* process would (new SessionDB)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    return db.load_gateway_routing_entries(scope=store._routing_scope())


def _read_sessions_json(tmp_path):
    path = Path(tmp_path) / "sessions" / "sessions.json"
    assert path.exists(), "sessions.json mirror should have been written"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (a) db_persisted=True + row deleted by another process -> dropped
# ---------------------------------------------------------------------------

def test_persisted_entry_with_row_deleted_by_other_process_is_dropped(
    _isolated_db, tmp_path
):
    """A persisted routing entry whose state.db row was deleted by another
    process must NOT be re-persisted on the next _save_entries()."""
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_slack_source("C111"))
    assert entry.db_persisted is True, "freshly created session must be DB-persisted"

    # Simulate the other process (desktop/CLI/MCP) deleting the session.
    assert store._db.delete_session(entry.session_id) is True

    # Gateway's next whole-index save must not resurrect the ghost route.
    store._save_entries()

    persisted = _persisted_routing(tmp_path, store)
    assert entry.session_key not in persisted, (
        "ghost routing entry for a session deleted by another process must be "
        "dropped before persistence (backstop)"
    )
    sessions_json = _read_sessions_json(tmp_path)
    assert entry.session_key not in sessions_json, (
        "sessions.json mirror must not contain the ghost routing entry either"
    )


# ---------------------------------------------------------------------------
# (b) db_persisted=True + row EXISTS -> preserved
# ---------------------------------------------------------------------------

def test_persisted_entry_with_existing_row_is_preserved(_isolated_db, tmp_path):
    """A persisted entry whose row still exists in state.db is untouched."""
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_slack_source("C222"))
    assert entry.db_persisted is True

    store._save_entries()

    persisted = _persisted_routing(tmp_path, store)
    assert entry.session_key in persisted, "live persisted entry must be preserved"
    assert json.loads(persisted[entry.session_key])["session_id"] == entry.session_id
    assert entry.session_key in _read_sessions_json(tmp_path)


# ---------------------------------------------------------------------------
# (c) legacy db_persisted=False + row absent -> PRESERVED (legacy compat)
# ---------------------------------------------------------------------------

def test_legacy_entry_never_persisted_is_preserved_when_row_absent(
    _isolated_db, tmp_path
):
    """Pre-SQLite legacy entries (db_persisted=False) are always preserved,
    even when they have no row in state.db — absence is expected for them."""
    store = _make_store(tmp_path)
    key = "agent:main:slack:channel:C333"
    now = datetime.now(timezone.utc)
    legacy = SessionEntry(
        session_key=key,
        session_id="legacy-session-no-db-row",
        created_at=now,
        updated_at=now,
        origin=_slack_source("C333"),
        db_persisted=False,
    )
    store._entries[key] = legacy
    store._loaded = True

    # Sanity: no such row exists in state.db.
    assert store._db.get_session(legacy.session_id) is None

    store._save_entries()

    persisted = _persisted_routing(tmp_path, store)
    assert key in persisted, (
        "legacy entry (db_persisted=False) must be preserved even with no DB row"
    )
    assert json.loads(persisted[key])["session_id"] == legacy.session_id
    assert key in _read_sessions_json(tmp_path)


# ---------------------------------------------------------------------------
# (d) 3 entries, 1 dead -> only the dead one falls
# ---------------------------------------------------------------------------

def test_only_dead_entry_dropped_among_mixed_entries(_isolated_db, tmp_path):
    """With several persisted entries, only the one whose row was deleted by
    another process is dropped; the live ones survive the save."""
    store = _make_store(tmp_path)
    e1 = store.get_or_create_session(_slack_source("C441"))
    e2 = store.get_or_create_session(_slack_source("C442"))
    e3 = store.get_or_create_session(_slack_source("C443"))
    for e in (e1, e2, e3):
        assert e.db_persisted is True

    assert store._db.delete_session(e2.session_id) is True

    store._save_entries()

    persisted = _persisted_routing(tmp_path, store)
    assert e1.session_key in persisted
    assert e3.session_key in persisted
    assert e2.session_key not in persisted, (
        "only the entry whose row was deleted may be dropped"
    )

    sessions_json = _read_sessions_json(tmp_path)
    assert e1.session_key in sessions_json
    assert e3.session_key in sessions_json
    assert e2.session_key not in sessions_json


# ---------------------------------------------------------------------------
# (e) DB exception during the existence check -> save does not break
# ---------------------------------------------------------------------------

def test_db_error_during_existence_check_does_not_break_save(
    _isolated_db, tmp_path, monkeypatch
):
    """A DB exception raised by the existence check must be caught: the save
    must not raise, and the entries must be preserved (fail-safe — nothing
    is dropped on uncertainty)."""
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_slack_source("C555"))
    assert entry.db_persisted is True

    # Force the backstop's DB read to fail mid-check (monkeypatch do db).
    check_attempted = []
    real_db = store._db
    real_read_ctx = getattr(real_db, "_read_ctx", None)

    def _boom_read_ctx(*args, **kwargs):
        check_attempted.append("read_ctx")
        raise RuntimeError("simulated DB failure during existence check")

    def _boom_query(*args, **kwargs):
        check_attempted.append("query")
        raise RuntimeError("simulated DB failure during existence check")

    if real_read_ctx is not None:
        monkeypatch.setattr(real_db, "_read_ctx", _boom_read_ctx)
    else:
        # Alternative wiring: the batch query itself is the check.
        monkeypatch.setattr(store, "_query_existing_session_ids", _boom_query)

    # Must not raise, and the check must actually have been attempted (and
    # failed) — without this, a save that skips the check would false-pass.
    store._save_entries()
    assert check_attempted, "backstop existence check must have been exercised"

    # Fail-safe: nothing dropped on DB error; entries survive in memory.
    assert entry.session_key in store._entries
    data, _generation = store._snapshot_routing_locked()
    assert entry.session_key in data, "entry must survive a failed existence check"

    # Once the DB is healthy again, a normal save persists the entry intact.
    if real_read_ctx is not None:
        monkeypatch.setattr(real_db, "_read_ctx", real_read_ctx)
    else:
        monkeypatch.undo()
    store._save_entries()
    persisted = _persisted_routing(tmp_path, store)
    assert entry.session_key in persisted, (
        "entry preserved during the DB failure must be persistable afterwards"
    )

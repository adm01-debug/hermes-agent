"""TDD round-2 tests for gap-3 fixes N1/N3/N5/N9 (hermes-state campaign).

Hermetic tests: every ``SessionDB`` is built on ``tmp_path`` — never the
real ``~/.hermes`` state.db.

* N1 — token/usage accounting must NEVER resurrect a deleted session row:
  ``update_token_counts`` / ``_apply_token_batch`` on a deleted session must
  leave ``sessions`` with COUNT(*) == 0 and must not raise.
* N1b — control: on a LIVE session the same calls still update counters.
* N3 — ``delete_session`` must also remove ``async_delegations`` rows that
  belong to the deleted session (``origin_session``), while leaving rows of
  other sessions untouched.
* N5 — ``delete_session`` must also remove ``delivery_obligations`` rows for
  the deleted session (matched through the session's ``session_key``), while
  leaving obligations of other session keys untouched.
* N9 — the api_server title-conflict create path must not leave an orphaned
  ``gateway_routing`` entry behind when it rolls back the freshly inserted
  session row.
"""
import json
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _count_rows(db, table, where, params):
    with db._lock:
        row = db._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", params
        ).fetchone()
    return row[0]


# Real DDL mirrored from gateway/delivery_ledger.py::_initialize_schema —
# the delivery_obligations table is NOT part of the hermes_state schema, so
# tests create it with the production column set.
_DELIVERY_OBLIGATIONS_DDL = """
CREATE TABLE IF NOT EXISTS delivery_obligations (
    obligation_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    content TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    owner_pid INTEGER,
    owner_started_at INTEGER,
    last_error TEXT
)
"""


def _insert_async_delegation(
    db,
    delegation_id,
    origin_ui_session_id,
    *,
    parent_session_id=None,
    origin_session="",
    state="pending",
):
    """Mirror tools/async_delegation.py's real row shape: the session id
    lives in ``origin_ui_session_id`` / ``parent_session_id``, while
    ``origin_session`` carries the routing key."""
    now = time.time()
    with db._lock:
        db._conn.execute(
            """INSERT INTO async_delegations (
                 delegation_id, origin_session, origin_ui_session_id,
                 parent_session_id, state, dispatched_at, completed_at,
                 updated_at, event_json, result_json, delivery_state,
                 delivery_attempts, delivered_at, owner_pid, owner_started_at,
                 task_json, delivery_claim, delivery_claimed_at
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL,
                         'pending', 0, NULL, NULL, NULL, NULL, NULL, NULL)""",
            (
                delegation_id,
                origin_session,
                origin_ui_session_id,
                parent_session_id,
                state,
                now,
                now,
            ),
        )


def _insert_delivery_obligation(
    db, obligation_id, session_key, *, state="pending"
):
    now = time.time()
    with db._lock:
        db._conn.execute(
            """INSERT INTO delivery_obligations (
                 obligation_id, session_key, platform, chat_id, thread_id,
                 content, state, attempts, created_at, updated_at,
                 owner_pid, owner_started_at, last_error
               ) VALUES (?, ?, 'telegram', 'chat-1', NULL, 'final reply',
                         ?, 0, ?, ?, NULL, NULL, NULL)""",
            (obligation_id, session_key, state, now, now),
        )


# ---------------------------------------------------------------------------
# N1 — token/usage accounting must never resurrect a deleted session
# ---------------------------------------------------------------------------

class TestN1TokenAccountingNeverResurrectsDeletedSession:
    def test_update_token_counts_after_delete_leaves_no_row(self, db):
        """A queued token delta arriving AFTER delete_session() must not
        recreate the session row (and must not raise)."""
        db.create_session("s1", source="cli")
        db.append_message("s1", "user", content="hello")
        assert db.delete_session("s1") is True
        assert _count_rows(db, "sessions", "id = ?", ("s1",)) == 0

        db.update_token_counts(
            "s1", input_tokens=100, output_tokens=50, model="m1"
        )

        assert _count_rows(db, "sessions", "id = ?", ("s1",)) == 0

    def test_apply_token_batch_after_delete_leaves_no_row(self, db):
        """The async writer's _apply_token_batch path has the same contract:
        deltas for a deleted session are dropped, never re-inserted."""
        db.create_session("s2", source="cli")
        db.append_message("s2", "user", content="hello")
        assert db.delete_session("s2") is True

        db._apply_token_batch(
            [("s2", {"input_tokens": 10, "output_tokens": 5, "model": "m1"})]
        )

        assert _count_rows(db, "sessions", "id = ?", ("s2",)) == 0

    def test_absolute_batch_after_delete_leaves_no_row(self, db):
        """Gateway-style absolute (cumulative) deltas share the guard."""
        db.create_session("s3", source="gateway")
        assert db.delete_session("s3") is True

        db.update_token_counts(
            "s3", input_tokens=999, output_tokens=999, absolute=True
        )

        assert _count_rows(db, "sessions", "id = ?", ("s3",)) == 0

    def test_live_session_token_update_still_works(self, db):
        """Control (N1b): the guard must not break normal accounting on a
        session that still exists."""
        db.create_session("live", source="cli")
        db.update_token_counts(
            "live", input_tokens=100, output_tokens=50, model="m1"
        )
        sess = db.get_session("live")
        assert sess is not None
        assert sess["input_tokens"] == 100
        assert sess["output_tokens"] == 50


# ---------------------------------------------------------------------------
# N3 — delete_session cascades into async_delegations
# ---------------------------------------------------------------------------

class TestN3DeleteSessionRemovesAsyncDelegations:
    def test_delegation_rows_for_deleted_session_are_removed(self, db):
        db.create_session("s1", source="cli")
        # Real row shapes: id in origin_ui_session_id + parent_session_id.
        _insert_async_delegation(
            db, "del-1", "s1", parent_session_id="s1",
            origin_session="telegram:551199999999:default",
        )
        _insert_async_delegation(
            db, "del-2", "s1", parent_session_id="s1",
            origin_session="telegram:551199999999:default", state="running",
        )
        assert _count_rows(
            db, "async_delegations", "origin_ui_session_id = ?", ("s1",)
        ) == 2

        assert db.delete_session("s1") is True

        assert _count_rows(
            db, "async_delegations", "origin_ui_session_id = ?", ("s1",)
        ) == 0
        assert _count_rows(
            db, "async_delegations", "parent_session_id = ?", ("s1",)
        ) == 0

    def test_delegations_of_other_sessions_survive(self, db):
        db.create_session("victim", source="cli")
        db.create_session("other", source="cli")
        _insert_async_delegation(db, "del-victim", "victim")
        _insert_async_delegation(db, "del-other", "other")

        assert db.delete_session("victim") is True

        assert _count_rows(
            db, "async_delegations", "delegation_id = ?", ("del-victim",)
        ) == 0
        assert _count_rows(
            db, "async_delegations", "delegation_id = ?", ("del-other",)
        ) == 1


# ---------------------------------------------------------------------------
# N5 — delete_session cascades into delivery_obligations
# ---------------------------------------------------------------------------

class TestN5DeleteSessionRemovesDeliveryObligations:
    def test_obligations_for_deleted_session_key_are_removed(self, db):
        with db._lock:
            db._conn.execute(_DELIVERY_OBLIGATIONS_DDL)
        session_key = "telegram:551199999999:default"
        db.create_session("s1", source="gateway", session_key=session_key)
        _insert_delivery_obligation(db, "obl-1", session_key, state="pending")
        _insert_delivery_obligation(db, "obl-2", session_key, state="delivered")
        assert _count_rows(
            db, "delivery_obligations", "session_key = ?", (session_key,)
        ) == 2

        assert db.delete_session("s1") is True

        assert _count_rows(
            db, "delivery_obligations", "session_key = ?", (session_key,)
        ) == 0

    def test_obligations_of_other_session_keys_survive(self, db):
        with db._lock:
            db._conn.execute(_DELIVERY_OBLIGATIONS_DDL)
        victim_key = "telegram:551199999999:default"
        other_key = "telegram:558899999999:default"
        db.create_session("victim", source="gateway", session_key=victim_key)
        db.create_session("other", source="gateway", session_key=other_key)
        _insert_delivery_obligation(db, "obl-victim", victim_key)
        _insert_delivery_obligation(db, "obl-other", other_key)

        assert db.delete_session("victim") is True

        assert _count_rows(
            db, "delivery_obligations", "obligation_id = ?", ("obl-victim",)
        ) == 0
        assert _count_rows(
            db, "delivery_obligations", "obligation_id = ?", ("obl-other",)
        ) == 1


# ---------------------------------------------------------------------------
# N9 — api_server title-conflict create leaves no orphaned routing
# ---------------------------------------------------------------------------

class TestN9TitleConflictCreateLeavesNoOrphanRouting:
    """POST /api/sessions with a title already owned by another session must
    roll back the freshly inserted row AND purge any stale gateway_routing
    entry still pointing at the reused session id."""

    @pytest.mark.asyncio
    async def test_title_conflict_purges_stale_routing(self, tmp_path, monkeypatch):
        from gateway.config import PlatformConfig
        from gateway.platforms.api_server import APIServerAdapter

        db = SessionDB(tmp_path / "state.db")
        # Another session already owns the title the client wants to use.
        db.create_session("existing", source="api_server")
        db.set_session_title("existing", "Shared Title")
        # Stale routing entry that still points at the id the client reuses.
        db.save_gateway_routing_entry(
            "telegram:551199999999:default",
            json.dumps({"session_id": "reused-id", "source": "api_server"}),
            scope="",
        )
        assert _count_rows(
            db, "gateway_routing", "session_key = ?",
            ("telegram:551199999999:default",),
        ) == 1

        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_check_auth", lambda request: None)

        async def _session_db():
            return db

        monkeypatch.setattr(adapter, "_ensure_session_db_async", _session_db)

        async def _read_body(request):
            return {"id": "reused-id", "title": "Shared Title"}, None

        monkeypatch.setattr(adapter, "_read_json_body", _read_body)

        resp = await adapter._handle_create_session(object())

        assert resp.status == 400
        # The rolled-back row must not exist...
        assert _count_rows(db, "sessions", "id = ?", ("reused-id",)) == 0
        # ...and the stale routing entry must not be orphaned behind it.
        assert _count_rows(
            db, "gateway_routing", "session_key = ?",
            ("telegram:551199999999:default",),
        ) == 0
        # The rightful title owner is untouched.
        assert _count_rows(db, "sessions", "id = ?", ("existing",)) == 1

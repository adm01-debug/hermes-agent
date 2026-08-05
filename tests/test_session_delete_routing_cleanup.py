"""TC1-TC9: session deletion must purge gateway routing state.

Regression suite for the "sessions resurrect after delete" bug class
(plan: fix/hermes-session-delete-routing-v1).  Every delete path in
``SessionDB`` must remove:

1. ``gateway_routing`` rows whose embedded ``entry_json.session_id`` was
   deleted (matching by id, never by session_key alone, so a key repointed
   at a live session survives);
2. the matching entry in the legacy ``<sessions_dir>/sessions.json`` mirror
   (preserving ``_``-prefixed sentinels such as ``_README`` and foreign
   entries), while never raising or blocking the delete itself.
"""

import json
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    """SessionDB over a temp state.db (mirrors tests/test_hermes_state.py)."""
    session_db = SessionDB(db_path=tmp_path / "test_state.db")
    yield session_db
    session_db.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _routing_entry(session_id: str, session_key: str) -> str:
    """The entry_json shape the gateway persists (string-encoded dict)."""
    return json.dumps({"session_id": session_id, "session_key": session_key})


def _seed_routing(db, session_key: str, session_id: str, sessions_dir: Path):
    """Write one gateway_routing row scoped to *sessions_dir*."""
    db.save_gateway_routing_entry(
        session_key, _routing_entry(session_id, session_key),
        scope=str(sessions_dir),
    )


def _routing_keys_for_id(db, sessions_dir: Path, session_id: str) -> set:
    """Keys in *sessions_dir* scope whose entry_json points at *session_id*."""
    keys = set()
    for key, entry_json in db.load_gateway_routing_entries(
        scope=str(sessions_dir)
    ).items():
        try:
            entry = json.loads(entry_json)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("session_id") == session_id:
            keys.add(key)
    return keys


# ---------------------------------------------------------------------------
# TC1 — delete_session removes the gateway_routing row of the deleted id
# ---------------------------------------------------------------------------


class TestTc1DeleteSessionPurgesRouting:
    def test_routing_entry_removed_after_delete(self, db, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sid = "s1"
        key = "agent:main:telegram:111"
        db.create_session(sid, source="gateway", session_key=key)
        _seed_routing(db, key, sid, sessions_dir)

        scope = str(sessions_dir)
        assert key in db.load_gateway_routing_entries(scope=scope)

        assert db.delete_session(sid, sessions_dir=sessions_dir) is True

        entries = db.load_gateway_routing_entries(scope=scope)
        assert key not in entries
        assert _routing_keys_for_id(db, sessions_dir, sid) == set()
        assert db.get_session(sid) is None


# ---------------------------------------------------------------------------
# TC2 — anti-over-deletion: a key repointed at ANOTHER live session survives
# ---------------------------------------------------------------------------


class TestTc2NoOverDeletion:
    def test_key_pointing_at_live_session_is_not_removed(self, db, tmp_path):
        sessions_dir = tmp_path / "sessions"
        deleted_id = "s1"
        live_id = "s2"
        shared_key = "agent:main:telegram:111"
        # Both rows carry the same session_key; the routing row was repointed
        # at the LIVE session after a rotation.
        db.create_session(deleted_id, source="gateway", session_key=shared_key)
        db.create_session(live_id, source="gateway", session_key=shared_key)
        _seed_routing(db, shared_key, live_id, sessions_dir)

        assert db.delete_session(deleted_id, sessions_dir=sessions_dir) is True

        entries = db.load_gateway_routing_entries(scope=str(sessions_dir))
        # The key must survive because its entry_json still points at s2,
        # which is alive — purging by session_key alone would break it.
        assert shared_key in entries
        assert json.loads(entries[shared_key])["session_id"] == live_id
        assert db.get_session(deleted_id) is None
        assert db.get_session(live_id) is not None


# ---------------------------------------------------------------------------
# TC3 — multi-scope: same key in distinct scopes, same deleted id
# ---------------------------------------------------------------------------


class TestTc3MultiScope:
    def test_routing_purged_in_every_scope(self, db, tmp_path):
        scope_a_dir = tmp_path / "sessions"
        scope_b_dir = tmp_path / "other_sessions"
        sid = "s1"
        key = "agent:main:telegram:111"
        db.create_session(sid, source="gateway", session_key=key)
        _seed_routing(db, key, sid, scope_a_dir)
        _seed_routing(db, key, sid, scope_b_dir)
        # Unrelated entry in scope B must be untouched by the purge.
        other_id = "s2"
        other_key = "agent:main:discord:222"
        db.create_session(other_id, source="gateway", session_key=other_key)
        _seed_routing(db, other_key, other_id, scope_b_dir)

        assert db.delete_session(sid, sessions_dir=scope_a_dir) is True

        assert key not in db.load_gateway_routing_entries(scope=str(scope_a_dir))
        assert key not in db.load_gateway_routing_entries(scope=str(scope_b_dir))
        assert other_key in db.load_gateway_routing_entries(scope=str(scope_b_dir))


# ---------------------------------------------------------------------------
# TC4 — corrupted entry_json never blocks the delete nor removes foreign rows
# ---------------------------------------------------------------------------


class TestTc4CorruptedEntryJson:
    def test_corrupt_row_skipped_and_foreign_rows_kept(self, db, tmp_path):
        sessions_dir = tmp_path / "sessions"
        deleted_id = "s1"
        live_id = "s2"
        db.create_session(deleted_id, source="gateway", session_key="k1")
        db.create_session(live_id, source="gateway", session_key="k2")
        scope = str(sessions_dir)
        db.save_gateway_routing_entry(
            "k1", _routing_entry(deleted_id, "k1"), scope=scope
        )
        db.save_gateway_routing_entry(
            "k2", _routing_entry(live_id, "k2"), scope=scope
        )
        db.save_gateway_routing_entry("k3", "{not-json{{{", scope=scope)

        assert db.delete_session(deleted_id, sessions_dir=sessions_dir) is True

        entries = db.load_gateway_routing_entries(scope=scope)
        assert "k1" not in entries
        # Foreign row pointing at the live session survives.
        assert "k2" in entries
        assert json.loads(entries["k2"])["session_id"] == live_id
        # Invalid JSON rows are skipped (never block, never deleted).
        assert "k3" in entries
        assert db.get_session(live_id) is not None


# ---------------------------------------------------------------------------
# TC5 — cascade: delegate children get their routing purged with the parent
# ---------------------------------------------------------------------------


class TestTc5DelegateCascade:
    def test_child_routing_purged_with_parent(self, db, tmp_path):
        sessions_dir = tmp_path / "sessions"
        parent_id = "parent-1"
        child_id = "child-1"
        parent_key = "agent:main:telegram:111"
        child_key = "agent:main:telegram:111:delegate"
        db.create_session(parent_id, source="gateway", session_key=parent_key)
        db.create_session(
            child_id,
            source="tool",
            session_key=child_key,
            parent_session_id=parent_id,
            model_config={"_delegate_from": parent_id},
        )
        _seed_routing(db, parent_key, parent_id, sessions_dir)
        _seed_routing(db, child_key, child_id, sessions_dir)

        assert db.delete_session(parent_id, sessions_dir=sessions_dir) is True

        entries = db.load_gateway_routing_entries(scope=str(sessions_dir))
        assert parent_key not in entries
        assert child_key not in entries
        assert _routing_keys_for_id(db, sessions_dir, parent_id) == set()
        assert _routing_keys_for_id(db, sessions_dir, child_id) == set()
        assert db.get_session(child_id) is None


# ---------------------------------------------------------------------------
# TC6 — parity: every other delete path purges routing too
# ---------------------------------------------------------------------------


DELETE_METHODS = [
    pytest.param("delete_sessions", id="delete_sessions"),
    pytest.param("delete_session_if_empty", id="delete_session_if_empty"),
    pytest.param("delete_empty_sessions", id="delete_empty_sessions"),
    pytest.param("prune_sessions", id="prune_sessions"),
    pytest.param("prune_empty_ghost_sessions", id="prune_empty_ghost_sessions"),
]


def _prepare_session_for(db, method, sid, key):
    """Create *sid* with the preconditions each delete path requires."""
    if method == "prune_empty_ghost_sessions":
        # TUI ghost: empty, ended, >24h old.
        db.create_session(sid, source="tui", session_key=key)
        db.end_session(sid, "agent_close")
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 90000, sid),
            )
    elif method in ("delete_empty_sessions", "prune_sessions"):
        # Both target only ended sessions.
        db.create_session(sid, source="cli", session_key=key)
        db.end_session(sid, "agent_close")
    else:
        db.create_session(sid, source="cli", session_key=key)


def _invoke_delete(db, method, sid, sessions_dir):
    if method == "delete_sessions":
        return db.delete_sessions([sid], sessions_dir=sessions_dir)
    if method == "delete_session_if_empty":
        return db.delete_session_if_empty(sid, sessions_dir=sessions_dir)
    if method == "delete_empty_sessions":
        return db.delete_empty_sessions(sessions_dir=sessions_dir)
    if method == "prune_sessions":
        return db.prune_sessions(sessions_dir=sessions_dir, older_than_days=0)
    if method == "prune_empty_ghost_sessions":
        return db.prune_empty_ghost_sessions(sessions_dir=sessions_dir)
    raise AssertionError(f"unknown method {method}")


class TestTc6DeletePathParity:
    @pytest.mark.parametrize("method", DELETE_METHODS)
    def test_method_purges_routing(self, db, tmp_path, method):
        sessions_dir = tmp_path / "sessions"
        sid = f"s-{method}"
        key = f"k-{method}"
        _prepare_session_for(db, method, sid, key)
        _seed_routing(db, key, sid, sessions_dir)

        scope = str(sessions_dir)
        assert key in db.load_gateway_routing_entries(scope=scope)

        result = _invoke_delete(db, method, sid, sessions_dir)
        assert result  # the method must actually have deleted the session

        entries = db.load_gateway_routing_entries(scope=scope)
        assert key not in entries
        assert _routing_keys_for_id(db, sessions_dir, sid) == set()
        assert db.get_session(sid) is None


# ---------------------------------------------------------------------------
# TC7 — sessions.json: deleted id removed, _README and foreign entries kept
# ---------------------------------------------------------------------------


class TestTc7SessionsJsonPruned:
    def test_deleted_entry_removed_sentinels_and_foreign_kept(self, db, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        deleted_id = "s1"
        live_id = "s2"
        db.create_session(deleted_id, source="gateway", session_key="k1")
        db.create_session(live_id, source="gateway", session_key="k2")
        _seed_routing(db, "k1", deleted_id, sessions_dir)

        sessions_file = sessions_dir / "sessions.json"
        # Legacy sessions.json shape (same contract as the gateway suite):
        # routing key -> serialized entry, plus the "_README" sentinel.
        sessions_file.write_text(
            json.dumps(
                {
                    "_README": "LEGACY MIRROR of the gateway routing index",
                    "k1": _routing_entry(deleted_id, "k1"),
                    "k2": _routing_entry(live_id, "k2"),
                }
            ),
            encoding="utf-8",
        )

        assert db.delete_session(deleted_id, sessions_dir=sessions_dir) is True

        data = json.loads(sessions_file.read_text(encoding="utf-8"))
        assert "k1" not in data, "deleted session must leave sessions.json"
        assert data["_README"].startswith("LEGACY MIRROR")
        assert json.loads(data["k2"]) == {"session_id": live_id, "session_key": "k2"}

    def test_deleted_entry_removed_in_real_mirror_dict_format(self, db, tmp_path):
        """Regression pin for the REAL mirror format written by the gateway.

        ``SessionStore._save_sessions_json`` serializes ``{key: entry.to_dict()}``
        — values are DICTS, not JSON strings (the string form only exists in
        ``gateway_routing.entry_json``). The prune helper must handle the dict
        shape or it silently no-ops on production data.
        """
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        deleted_id = "s1"
        live_id = "s2"
        db.create_session(deleted_id, source="gateway", session_key="k1")
        db.create_session(live_id, source="gateway", session_key="k2")
        _seed_routing(db, "k1", deleted_id, sessions_dir)

        sessions_file = sessions_dir / "sessions.json"
        # Real gateway mirror: key -> entry DICT (to_dict shape), plus sentinel.
        sessions_file.write_text(
            json.dumps(
                {
                    "_README": "LEGACY MIRROR of the gateway routing index",
                    "k1": {
                        "session_key": "k1",
                        "session_id": deleted_id,
                        "created_at": "2026-08-05T10:00:00",
                        "updated_at": "2026-08-05T10:00:00",
                    },
                    "k2": {
                        "session_key": "k2",
                        "session_id": live_id,
                        "created_at": "2026-08-05T10:00:00",
                        "updated_at": "2026-08-05T10:00:00",
                    },
                }
            ),
            encoding="utf-8",
        )

        assert db.delete_session(deleted_id, sessions_dir=sessions_dir) is True

        data = json.loads(sessions_file.read_text(encoding="utf-8"))
        assert "k1" not in data, "deleted session must leave sessions.json (dict format)"
        assert data["_README"].startswith("LEGACY MIRROR")
        assert data["k2"]["session_id"] == live_id, "foreign dict entry must survive"


# ---------------------------------------------------------------------------
# TC8 — sessions.json missing / unreadable / invalid: delete still succeeds
# ---------------------------------------------------------------------------


class TestTc8SessionsJsonResilience:
    @pytest.mark.parametrize(
        "file_setup",
        [
            pytest.param("missing", id="missing"),
            pytest.param("unreadable", id="unreadable"),
            pytest.param("invalid_json", id="invalid_json"),
        ],
    )
    def test_delete_succeeds_regardless_of_sessions_json_state(
        self, db, tmp_path, file_setup
    ):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        if file_setup == "unreadable":
            # A directory squatting on the sessions.json name cannot be read.
            (sessions_dir / "sessions.json").mkdir()
        elif file_setup == "invalid_json":
            (sessions_dir / "sessions.json").write_text(
                "{definitely not json", encoding="utf-8"
            )
        # "missing": no sessions.json at all.

        sid = "s1"
        key = "k1"
        db.create_session(sid, source="gateway", session_key=key)
        _seed_routing(db, key, sid, sessions_dir)

        # Must return True and never raise, whatever the file state is.
        assert db.delete_session(sid, sessions_dir=sessions_dir) is True

        assert db.get_session(sid) is None
        assert key not in db.load_gateway_routing_entries(scope=str(sessions_dir))


# ---------------------------------------------------------------------------
# TC9 — no sessions_dir: file untouched, gateway_routing still purged
# ---------------------------------------------------------------------------


class TestTc9NoSessionsDir:
    def test_file_untouched_but_routing_purged(self, db, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        sid = "s1"
        key = "k1"
        db.create_session(sid, source="gateway", session_key=key)
        _seed_routing(db, key, sid, sessions_dir)

        sessions_file = sessions_dir / "sessions.json"
        sessions_file.write_text(
            json.dumps(
                {
                    "_README": "LEGACY MIRROR",
                    "k1": _routing_entry(sid, key),
                }
            ),
            encoding="utf-8",
        )
        before = sessions_file.read_bytes()

        # No sessions_dir → on-disk mirror must not be touched at all.
        assert db.delete_session(sid) is True

        assert db.get_session(sid) is None
        assert key not in db.load_gateway_routing_entries(scope=str(sessions_dir))
        assert sessions_file.read_bytes() == before

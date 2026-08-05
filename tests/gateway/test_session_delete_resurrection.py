"""Session delete-routing resurrection guard tests (TG1-TG9).

Plan: `fix/hermes-session-delete-routing-v1` — when a session is deleted
(``sessions`` row + ``gateway_routing`` row removed by the CLI/web/CLI-exit
delete paths), the gateway's in-memory routing index (``SessionStore._entries``)
keeps pointing at the dead session_id and silently swallows every subsequent
message in that channel until the next restart.

The fix uses the durable, shared signal — absence of the row in the
``sessions`` table — gated by the ``db_persisted`` discriminator on
``SessionEntry`` so the pre-persistence race window (entry created, but
``create_session`` not yet durable) is NOT mistaken for a deletion:

| Estado                        | db_persisted | linha no DB | Decisão          |
|-------------------------------|--------------|-------------|------------------|
| Recém-criada (corrida)        | False        | ausente     | manter           |
| Criada e persistida           | True         | presente    | manter           |
| Persistida e deletada         | True         | ausente     | dropar → nova    |
| Entrada legada (sessions.json)| False        | ausente     | manter           |
| Erro de DB no lookup          | qualquer     | desconhecido| manter (fail-safe)|

These tests pin the gateway-side behaviour (TG1-TG9).  They use the real
``SessionDB`` (``DEFAULT_DB_PATH`` redirected to a per-test tmp file) so the
delete path, routing table, and recovery finder all run against real SQLite —
no mocks of the DB layer.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import hermes_state
from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionSource, SessionStore


def _source(chat_id: str = "chat-1", user_id: str = "user-1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_name="Alice",
        chat_type="dm",
        user_id=user_id,
    )


def _make_store(tmp_path, monkeypatch, **config_kwargs) -> SessionStore:
    """Build a fully-wired SessionStore over a real, isolated state.db."""
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode="none"),
        **config_kwargs,
    )
    return SessionStore(sessions_dir=tmp_path / "sessions", config=config)


def _entry_dict(
    session_key: str,
    session_id: str,
    *,
    db_persisted: bool | None = None,
) -> dict:
    """Build a routing-table entry dict as SessionEntry.to_dict() would emit.

    ``db_persisted=None`` omits the key entirely — the shape written by
    gateways older than the delete-routing fix.
    """
    now = datetime.now()
    data = {
        "session_key": session_key,
        "session_id": session_id,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "display_name": None,
        "platform": "telegram",
        "chat_type": "dm",
        "metadata": {},
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "last_prompt_tokens": 0,
        "estimated_cost_usd": 0.0,
        "cost_status": "unknown",
        "expiry_finalized": False,
        "suspended": False,
        "resume_pending": False,
        "resume_reason": None,
        "last_resume_marked_at": None,
        "is_fresh_reset": False,
        "was_auto_reset": False,
        "auto_reset_reason": None,
        "reset_had_activity": False,
        "prev_session_id": None,
    }
    if db_persisted is not None:
        data["db_persisted"] = db_persisted
    return data


class TestSessionDeleteResurrection:
    """TG1-TG9: routing index must not resurrect deleted sessions."""

    def test_tg1_deleted_persisted_row_returns_new_session_and_drops_entry(
        self, tmp_path, monkeypatch
    ):
        """TG1 (regressão principal): db_persisted=True + linha deletada →
        get_or_create_session devolve session_id DIFERENTE e a entrada antiga
        some de _entries."""
        store = _make_store(tmp_path, monkeypatch)
        source = _source()
        try:
            first = store.get_or_create_session(source)
            key = first.session_key
            assert first.db_persisted is True

            # External deletion (CLI/web/exit delete path): row gone from
            # `sessions` AND its gateway_routing row purged.
            deleted = store._db.delete_session(first.session_id, sessions_dir=store.sessions_dir)
            assert deleted is True
            assert store._db.get_session(first.session_id) is None
            # The in-memory routing index is still pointing at the dead id.
            assert key in store._entries

            again = store.get_or_create_session(source)

            assert again.session_id != first.session_id
            assert again.session_key == key
            # The stale entry object is gone — not mutated in place.
            assert store._entries[key] is not first
            assert store._entries[key].session_id == again.session_id
            assert first.session_id not in {
                e.session_id for e in store._entries.values()
            }
        finally:
            store._db.close()

    def test_tg2_unpersisted_entry_without_row_is_preserved(
        self, tmp_path, monkeypatch
    ):
        """TG2 (anti-regressão da corrida): db_persisted=False + linha ausente
        → session_id PRESERVADO (entry created but create_session not yet
        durable must not be mistaken for a deletion)."""
        store = _make_store(tmp_path, monkeypatch)
        source = _source()
        key = store._generate_session_key(source)
        try:
            entry = SessionEntry(
                session_key=key,
                session_id="20260101_000000_deadbeef",
                created_at=datetime.now(),
                updated_at=datetime.now(),
                origin=source,
                platform=Platform.TELEGRAM,
                chat_type="dm",
                db_persisted=False,
            )
            store._loaded = True
            store._entries[key] = entry

            again = store.get_or_create_session(source)

            assert again.session_id == entry.session_id
            assert store._entries[key] is entry
            assert again.db_persisted is False
        finally:
            store._db.close()

    def test_tg3_present_row_with_null_end_reason_is_preserved(
        self, tmp_path, monkeypatch
    ):
        """TG3: linha presente com end_reason IS NULL → session_id preservado."""
        store = _make_store(tmp_path, monkeypatch)
        source = _source()
        try:
            first = store.get_or_create_session(source)
            assert first.db_persisted is True
            assert store._db.get_session(first.session_id)["end_reason"] is None

            again = store.get_or_create_session(source)

            assert again.session_id == first.session_id
            assert store._entries[first.session_key] is first
        finally:
            store._db.close()

    def test_tg4_ended_row_keeps_54878_recovery_by_origin(
        self, tmp_path, monkeypatch
    ):
        """TG4: linha presente com end_reason → comportamento #54878 preservado,
        incluindo recovery por origem: o entry é dropado, mas o finder reabre
        a linha `agent_close` (mesmo session_id, transcript preservado)."""
        store = _make_store(tmp_path, monkeypatch)
        source = _source()
        try:
            first = store.get_or_create_session(source)
            # Recovery finder only returns rows with content.
            store._db.append_message(
                session_id=first.session_id, role="user", content="hello"
            )
            store._db.end_session(first.session_id, "agent_close")
            assert store._db.get_session(first.session_id)["end_reason"] == "agent_close"

            calls = []
            orig = store._query_recoverable_session

            def spy(**kwargs):
                calls.append(kwargs)
                return orig(**kwargs)

            monkeypatch.setattr(store, "_query_recoverable_session", spy)

            again = store.get_or_create_session(source)

            # Recovery by origin ran and reopened the same transcript.
            assert calls, "#54878 recovery must run for ended rows"
            assert again.session_id == first.session_id
            assert store._entries[first.session_key].session_id == first.session_id
            assert store._db.get_session(first.session_id)["end_reason"] is None
        finally:
            store._db.close()

    def test_tg5_deleted_row_skips_recovery_lookup(self, tmp_path, monkeypatch):
        """TG5: no caso deletado, _query_recoverable_session NÃO é chamado
        (spy) — a ausência da linha é uma deleção, não um acidente recuperável."""
        store = _make_store(tmp_path, monkeypatch)
        source = _source()
        try:
            first = store.get_or_create_session(source)
            store._db.delete_session(first.session_id, sessions_dir=store.sessions_dir)

            calls = []
            orig = store._query_recoverable_session

            def spy(**kwargs):
                calls.append(kwargs)
                return orig(**kwargs)

            monkeypatch.setattr(store, "_query_recoverable_session", spy)

            again = store.get_or_create_session(source)

            assert again.session_id != first.session_id
            assert calls == [], (
                "deleted-row routing must not attempt recovery; got "
                f"{len(calls)} recovery call(s)"
            )
        finally:
            store._db.close()

    def test_tg6_stale_fast_record_does_not_resurrect_dropped_key(
        self, tmp_path, monkeypatch
    ):
        """TG6: dropar a chave + registro fast de revisão superior pendente →
        a chave NÃO reaparece em gateway_routing nem no sessions.json."""
        store = _make_store(tmp_path, monkeypatch)
        source = _source()
        try:
            first = store.get_or_create_session(source)
            key = first.session_key

            # External deletion removes the row AND the routing row.
            store._db.delete_session(first.session_id, sessions_dir=store.sessions_dir)

            # A fast-path save serialized before the deletion is still sitting
            # in _fast_persisted_entries with a revision ABOVE any generation
            # the drop's full rewrite will allocate. Without forgetting it,
            # the fold-in in _persist_routing_data re-inserts the dead key.
            store._fast_persisted_entries[key] = (
                999_999,
                json.dumps(first.to_dict()),
            )

            again = store.get_or_create_session(source)
            assert again.session_id != first.session_id

            # The stale fast record must have been forgotten at drop time.
            assert key not in store._fast_persisted_entries

            durable = store._db.load_gateway_routing_entries(
                scope=store._routing_scope()
            )
            assert key in durable
            assert json.loads(durable[key])["session_id"] == again.session_id

            sessions_file = store.sessions_dir / "sessions.json"
            assert sessions_file.exists()
            mirror = json.loads(sessions_file.read_text(encoding="utf-8"))
            assert mirror[key]["session_id"] == again.session_id
            assert first.session_id not in json.dumps(durable)
            assert first.session_id not in json.dumps(mirror)
        finally:
            store._db.close()

    def test_tg7_ensure_loaded_prunes_persisted_entry_without_row(
        self, tmp_path, monkeypatch
    ):
        """TG7: _ensure_loaded_locked poda entrada db_persisted=True sem linha;
        preserva quando db_persisted=False ou campo ausente (legado)."""
        store = _make_store(tmp_path, monkeypatch)
        try:
            # Live row for the entry that must survive.
            store._db.create_session("sid_live_d", source="telegram")

            entries = {
                "agent:main:telegram:dm:tg7-a": json.dumps(
                    _entry_dict(
                        "agent:main:telegram:dm:tg7-a",
                        "sid_deleted_a",
                        db_persisted=True,
                    )
                ),
                "agent:main:telegram:dm:tg7-b": json.dumps(
                    _entry_dict(
                        "agent:main:telegram:dm:tg7-b",
                        "sid_legacy_b",
                        db_persisted=False,
                    )
                ),
                # Old-format dict (no db_persisted key) -> defaults to False.
                "agent:main:telegram:dm:tg7-c": json.dumps(
                    _entry_dict("agent:main:telegram:dm:tg7-c", "sid_old_c")
                ),
                "agent:main:telegram:dm:tg7-d": json.dumps(
                    _entry_dict(
                        "agent:main:telegram:dm:tg7-d",
                        "sid_live_d",
                        db_persisted=True,
                    )
                ),
            }
            store._db.replace_gateway_routing_entries(
                entries, scope=store._routing_scope()
            )

            store._ensure_loaded()

            # db_persisted=True + no row -> pruned.
            assert "agent:main:telegram:dm:tg7-a" not in store._entries
            # db_persisted=False / absent flag + no row -> preserved.
            assert store._entries["agent:main:telegram:dm:tg7-b"].session_id == "sid_legacy_b"
            assert store._entries["agent:main:telegram:dm:tg7-b"].db_persisted is False
            assert store._entries["agent:main:telegram:dm:tg7-c"].session_id == "sid_old_c"
            assert store._entries["agent:main:telegram:dm:tg7-c"].db_persisted is False
            # db_persisted=True + row present -> preserved.
            assert store._entries["agent:main:telegram:dm:tg7-d"].session_id == "sid_live_d"
            assert store._entries["agent:main:telegram:dm:tg7-d"].db_persisted is True
        finally:
            store._db.close()

    def test_tg8_roundtrip_preserves_db_persisted_and_old_dict_defaults_false(
        self, tmp_path, monkeypatch
    ):
        """TG8: round-trip to_dict/from_dict preserva db_persisted; dict antigo
        (sem a chave) → False."""
        store = _make_store(tmp_path, monkeypatch)
        try:
            now = datetime.now()
            for flag in (True, False):
                entry = SessionEntry(
                    session_key="k",
                    session_id="s",
                    created_at=now,
                    updated_at=now,
                    db_persisted=flag,
                )
                restored = SessionEntry.from_dict(entry.to_dict())
                assert restored.db_persisted is flag

            old_style = _entry_dict("k", "s", db_persisted=None)
            assert "db_persisted" not in old_style
            legacy = SessionEntry.from_dict(old_style)
            assert legacy.db_persisted is False
        finally:
            store._db.close()

    def test_tg9_db_lookup_error_preserves_entry_fail_safe(
        self, tmp_path, monkeypatch
    ):
        """TG9: get_session levanta exceção → entrada preservada (fail-safe).

        A DB error means "unknown", never "deleted" — routing must keep the
        entry and return the same session_id instead of dropping it.
        """
        store = _make_store(tmp_path, monkeypatch)
        source = _source()
        key = store._generate_session_key(source)
        try:
            entry = SessionEntry(
                session_key=key,
                session_id="20260101_000000_failsafe",
                created_at=datetime.now(),
                updated_at=datetime.now(),
                origin=source,
                platform=Platform.TELEGRAM,
                chat_type="dm",
                db_persisted=True,
            )
            store._loaded = True
            store._entries[key] = entry

            def _boom(session_id):
                raise RuntimeError("state.db unavailable")

            monkeypatch.setattr(store._db, "get_session", _boom)

            again = store.get_or_create_session(source)

            assert again.session_id == entry.session_id
            assert store._entries[key] is entry
        finally:
            store._db.close()

    def test_tg1b_db_persisted_survives_create_reload_roundtrip(
        self, tmp_path, monkeypatch
    ):
        """TG1b (Bloqueador 2 pin): flag db_persisted=True deve ser DURÁVEL.

        Regression pin for the fix that re-saves the routing index right
        after ``create_session``: a freshly created session whose gateway
        restarts before the next per-turn save must reload with
        ``db_persisted=True``, otherwise the delete-routing staleness
        detection treats it as a legacy entry and never notices the
        deleted row.
        """
        store = _make_store(tmp_path, monkeypatch)
        source = _source()
        try:
            first = store.get_or_create_session(source)
            assert first.db_persisted is True
            key = first.session_key
            sid = first.session_id

            # Reconstruct a brand-new SessionStore over the SAME state.db —
            # simulates a gateway restart before any per-turn save.
            store._db.close()
            store2 = _make_store(tmp_path, monkeypatch)
            try:
                store2._ensure_loaded()
                reloaded = store2._entries.get(key)
                assert reloaded is not None, "routing entry must survive restart"
                assert reloaded.session_id == sid
                assert reloaded.db_persisted is True, (
                    "db_persisted must be durable across restart (persisted "
                    "right after create_session)"
                )
            finally:
                store2._db.close()
        finally:
            try:
                store._db.close()
            except Exception:
                pass

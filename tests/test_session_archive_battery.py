"""Bateria exaustiva de testes de CHATS ARQUIVADOS.

Cobre a arquitetura de arquivamento do Hermes (soft-hide via `archived=1`):

1. Round-trip básico arquivar/desarquivar
2. Cadeia de compressão (ancestral + descendente arquivados juntos)
3. Listagem: exclude (padrão) / include / only (via list_sessions_rich)
4. Pin: round-trip + exceção do sweep
5. Auto-archive: stale sweep (idle), pinned protegido, throttle, idempotência
6. Interação DELETE × ARCHIVE (regressão do fix de ressurreição):
   - arquivado NÃO é afetado por purge de gateway_routing (não é delete)
   - delete de sessão arquivada purga routing normalmente
   - arquivado com session_key + mensagem nova -> mesma sessão (não ressuscita outra)
7. Prune/delete_empty: arquivados NÃO são pegos (skip)
8. Archive + gateway_routing: flag não interfere no routing (vivo)
9. Arquivado não ressuscita id morto: delete de um chat arquivado limpa routing
10. Edge cases: id inexistente, idempotência, desarquivar volta à lista

SEMÂNTICA REAL DE COMPRESSÃO (validada em hermes_state.py:2929-2933):
quando uma sessão é comprimida, o PARENT recebe end_reason='compression' e a
continuação (child) fica SEM end_reason. O CTE de set_session_archived sobe
ancestrais cujo end_reason='compression' e desce descendentes do mesmo tipo.
"""
import json
import sqlite3
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "test_state.db")
    yield session_db
    session_db.close()


def _mk_session(db, sid, source="desktop", session_key=None, parent=None,
                end_reason=None, ended_at=None, message_count=0, title=None,
                started_at=None):
    kwargs = dict(source=source)
    if session_key:
        kwargs["session_key"] = session_key
    if parent:
        kwargs["parent_session_id"] = parent
    db.create_session(sid, **kwargs)

    # Campos que o create_session não aceita -> UPDATE direto
    sets, params = [], []
    if end_reason is not None:
        sets.append("end_reason = ?")
        params.append(end_reason)
    if ended_at is not None:
        sets.append("ended_at = ?")
        params.append(ended_at)
    if message_count:
        sets.append("message_count = ?")
        params.append(message_count)
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if started_at is not None:
        sets.append("started_at = ?")
        params.append(started_at)
    if sets:
        params.append(sid)
        db._execute_write(
            lambda c: c.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params
            )
        )
    return sid


def _mk_compression_chain(db):
    """Cadeia real de compressão: root (comprimida) -> child (comprimida) -> grand (viva)."""
    _mk_session(db, "root", end_reason="compression")
    _mk_session(db, "child", parent="root", end_reason="compression")
    _mk_session(db, "grand", parent="child")
    _mk_session(db, "branch", parent="child", end_reason="branched")
    return ["root", "child", "grand", "branch"]


def _seed_routing(db, session_key, session_id, scope):
    db.save_gateway_routing_entry(
        session_key,
        json.dumps({"session_id": session_id, "session_key": session_key}),
        scope=scope,
    )


# ---------------------------------------------------------------------------
# 1. Round-trip básico
# ---------------------------------------------------------------------------

class TestArchiveBasics:
    def test_archive_roundtrip(self, db):
        _mk_session(db, "s1")
        assert db.get_session("s1")["archived"] == 0
        assert db.set_session_archived("s1", True) is True
        assert db.get_session("s1")["archived"] == 1
        assert db.set_session_archived("s1", False) is True
        assert db.get_session("s1")["archived"] == 0

    def test_archive_missing_row_returns_false(self, db):
        assert db.set_session_archived("ghost", True) is False

    def test_archive_is_idempotent(self, db):
        _mk_session(db, "s1")
        assert db.set_session_archived("s1", True) is True
        assert db.set_session_archived("s1", True) is True  # no-op, still True
        assert db.get_session("s1")["archived"] == 1

    def test_archive_keeps_messages(self, db):
        _mk_session(db, "s1", message_count=5)
        db.append_message(session_id="s1", role="user", content="olá")
        db.append_message(session_id="s1", role="assistant", content="oi!")
        db.set_session_archived("s1", True)
        msgs = db.get_messages("s1")
        assert len(msgs) == 2, "mensagens preservadas após arquivar"


# ---------------------------------------------------------------------------
# 2. Cadeia de compressão
# ---------------------------------------------------------------------------

class TestArchiveCompressionLineage:
    def test_archive_tip_pulls_whole_compression_chain(self, db):
        _mk_compression_chain(db)
        # arquivar a ponta viva (grand) arrasta root + child (comprimidas)
        db.set_session_archived("grand", True)
        assert db.get_session("root")["archived"] == 1
        assert db.get_session("child")["archived"] == 1
        assert db.get_session("grand")["archived"] == 1
        # branch (não-compression) NÃO é arrastado
        assert db.get_session("branch")["archived"] == 0

    def test_unarchive_whole_chain(self, db):
        _mk_compression_chain(db)
        db.set_session_archived("grand", True)
        assert db.get_session("root")["archived"] == 1
        db.set_session_archived("grand", False)
        assert db.get_session("root")["archived"] == 0
        assert db.get_session("child")["archived"] == 0
        assert db.get_session("grand")["archived"] == 0

    def test_archive_root_pulls_descendants(self, db):
        _mk_compression_chain(db)
        db.set_session_archived("root", True)
        assert db.get_session("child")["archived"] == 1
        assert db.get_session("grand")["archived"] == 1
        # O CTE de descendants desce de um nó compression para TODOS os filhos,
        # inclusive branch (comportamento real validado no código)
        assert db.get_session("branch")["archived"] == 1

    def test_archive_midchain_pulls_both_directions(self, db):
        _mk_compression_chain(db)
        db.set_session_archived("child", True)
        assert db.get_session("root")["archived"] == 1  # ancestral
        assert db.get_session("grand")["archived"] == 1  # descendente
        assert db.get_session("branch")["archived"] == 1  # filho de nó compression


# ---------------------------------------------------------------------------
# 3. Listagem (list_sessions_rich é a API com filtro de archived)
# ---------------------------------------------------------------------------

class TestArchiveListing:
    def _seed(self, db):
        _mk_session(db, "s1", title="Ativa")
        _mk_session(db, "s2", title="Arquivada")
        db.set_session_archived("s2", True)
        _mk_session(db, "s3", title="Pinned")
        db.set_session_pinned("s3", True)

    def test_exclude_by_default(self, db):
        self._seed(db)
        ids = {s["id"] for s in db.list_sessions_rich()}
        assert "s2" not in ids
        assert "s1" in ids and "s3" in ids

    def test_include_archived(self, db):
        self._seed(db)
        ids = {s["id"] for s in db.list_sessions_rich(include_archived=True)}
        assert {"s1", "s2", "s3"} <= ids

    def test_archived_only(self, db):
        self._seed(db)
        ids = {s["id"] for s in db.list_sessions_rich(archived_only=True)}
        assert ids == {"s2"}

    def test_unarchived_returns_to_listing(self, db):
        self._seed(db)
        db.set_session_archived("s2", False)
        ids = {s["id"] for s in db.list_sessions_rich()}
        assert "s2" in ids

    def test_session_count_matches_listing(self, db):
        self._seed(db)
        assert db.session_count() == 2
        assert db.session_count(include_archived=True) == 3
        assert db.session_count(archived_only=True) == 1


# ---------------------------------------------------------------------------
# 4. Pin
# ---------------------------------------------------------------------------

class TestArchivePin:
    def test_pin_roundtrip(self, db):
        _mk_session(db, "s1")
        assert db.set_session_pinned("s1", True) is True
        assert db.get_session("s1")["pinned"] == 1
        assert db.set_session_pinned("s1", False) is True
        assert db.get_session("s1")["pinned"] == 0

    def test_pin_whole_compression_lineage(self, db):
        _mk_compression_chain(db)
        db.set_session_pinned("grand", True)
        assert db.get_session("root")["pinned"] == 1
        assert db.get_session("child")["pinned"] == 1
        assert db.get_session("branch")["pinned"] == 0

    def test_pin_exempts_from_auto_archive(self, db):
        old = time.time() - 30 * 86400  # 30 dias sem tocar
        _mk_session(db, "stale_pinned", started_at=old, message_count=1)
        db.set_session_pinned("stale_pinned", True)
        _mk_session(db, "stale_unpinned", started_at=old, message_count=1)

        archived = db.archive_stale_sessions(idle_days=7)
        assert archived == 1
        assert db.get_session("stale_pinned")["archived"] == 0
        assert db.get_session("stale_unpinned")["archived"] == 1


# ---------------------------------------------------------------------------
# 5. Auto-archive
# ---------------------------------------------------------------------------

class TestAutoArchive:
    def test_archive_stale_by_last_message(self, db):
        now = time.time()
        _mk_session(db, "recent", message_count=1, started_at=now - 3600)
        db.append_message(session_id="recent", role="user", content="x")
        # sessão velha SEM mensagens -> usa started_at
        _mk_session(db, "old_empty", started_at=now - 30 * 86400)
        # sessão velha COM mensagem recente -> poupada
        _mk_session(db, "old_but_active", started_at=now - 30 * 86400)
        db.append_message(session_id="old_but_active", role="user", content="recente")

        # envelhecer a mensagem da "recent" via UPDATE direto
        db._execute_write(lambda c: c.execute(
            "UPDATE messages SET timestamp = ? WHERE session_id = ?",
            (now - 20 * 86400, "recent"),
        ))

        archived = db.archive_stale_sessions(idle_days=7)
        # "recent" (msg antiga) e "old_empty" (sem msg) arquivadas; ativa poupada
        assert db.get_session("recent")["archived"] == 1
        assert db.get_session("old_empty")["archived"] == 1
        assert db.get_session("old_but_active")["archived"] == 0
        assert archived == 2

    def test_archive_stale_idempotent(self, db):
        old = time.time() - 30 * 86400
        _mk_session(db, "s1", started_at=old, message_count=1)
        assert db.archive_stale_sessions(idle_days=7) == 1
        assert db.archive_stale_sessions(idle_days=7) == 0

    def test_archive_stale_negative_or_none_idle_days_noop(self, db):
        _mk_session(db, "s1", started_at=time.time() - 30 * 86400)
        assert db.archive_stale_sessions(idle_days=None) == 0
        assert db.archive_stale_sessions(idle_days=-1) == 0

    def test_maybe_auto_archive_throttle_and_rerun(self, db):
        old = time.time() - 30 * 86400
        _mk_session(db, "s1", started_at=old, message_count=1)

        r1 = db.maybe_auto_archive(idle_days=3, min_interval_hours=24)
        assert r1["archived"] == 1

        # dentro da janela -> skip
        r2 = db.maybe_auto_archive(idle_days=3, min_interval_hours=24)
        assert r2["skipped"] is True and r2["archived"] == 0

        # forçar re-run zerando o meta e adicionando sessão nova velha
        db.set_meta("last_auto_archive", str(time.time() - 25 * 3600))
        _mk_session(db, "s2", started_at=time.time() - 30 * 86400, message_count=1)
        r3 = db.maybe_auto_archive(idle_days=3, min_interval_hours=24)
        assert r3["archived"] == 1
        assert db.get_session("s2")["archived"] == 1

    def test_auto_archive_skips_compression_midchain(self, db):
        """Sweep só arquiva tips; mid-chain compression não vira candidata."""
        now = time.time()
        _mk_compression_chain(db)
        # envelhecer todos
        for sid in ("root", "child", "grand"):
            db._execute_write(lambda c, s=sid: c.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (now - 30 * 86400, s),
            ))
        db.archive_stale_sessions(idle_days=7)
        # tip arquivada arrasta a cadeia inteira
        assert db.get_session("grand")["archived"] == 1
        assert db.get_session("child")["archived"] == 1
        assert db.get_session("root")["archived"] == 1


# ---------------------------------------------------------------------------
# 6. Interação DELETE × ARCHIVE (regressão do fix de ressurreição)
# ---------------------------------------------------------------------------

class TestArchiveVsDeleteFix:
    def test_archive_does_not_touch_gateway_routing(self, db, tmp_path):
        """Arquivar NÃO é delete: routing continua vivo (chat responde)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        scope = str(sessions_dir.resolve())
        _mk_session(db, "s1", source="telegram", session_key="agent:main:telegram:dm:1")
        _seed_routing(db, "agent:main:telegram:dm:1", "s1", scope)

        db.set_session_archived("s1", True)

        assert db.get_session("s1")["archived"] == 1
        assert "agent:main:telegram:dm:1" in db.load_gateway_routing_entries(scope=scope)
        assert db.get_session("s1") is not None  # linha viva

    def test_delete_archived_purges_routing(self, db, tmp_path):
        """Delete de sessão ARQUIVADA também purga gateway_routing + json."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        scope = str(sessions_dir.resolve())
        _mk_session(db, "s1", source="telegram", session_key="agent:main:telegram:dm:1")
        _seed_routing(db, "agent:main:telegram:dm:1", "s1", scope)
        db.set_session_archived("s1", True)

        (sessions_dir / "sessions.json").write_text(json.dumps({
            "_README": "mirror",
            "agent:main:telegram:dm:1": {"session_id": "s1"},
        }), encoding="utf-8")

        assert db.delete_session("s1", sessions_dir=sessions_dir) is True

        assert db.get_session("s1") is None
        assert "agent:main:telegram:dm:1" not in db.load_gateway_routing_entries(scope=scope)
        data = json.loads((sessions_dir / "sessions.json").read_text(encoding="utf-8"))
        assert "agent:main:telegram:dm:1" not in data
        assert data["_README"] == "mirror"

    def test_archived_gateway_session_receives_new_message_same_session(self, db, tmp_path):
        """Arquivado com routing vivo: nova msg = MESMA sessão (não ressuscita outra)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        scope = str(sessions_dir.resolve())
        _mk_session(db, "s1", source="telegram", session_key="agent:main:telegram:dm:1")
        _seed_routing(db, "agent:main:telegram:dm:1", "s1", scope)
        db.set_session_archived("s1", True)

        # routing intacto aponta para s1
        entries = db.load_gateway_routing_entries(scope=scope)
        assert json.loads(entries["agent:main:telegram:dm:1"])["session_id"] == "s1"
        # sessão continua a mesma, apenas oculta
        assert db.get_session("s1")["archived"] == 1

    def test_delete_archived_with_session_key_does_not_touch_live_sibling(self, db, tmp_path):
        """Anti-over-deletion: apagar arquivada não derruba routing de irmã viva."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        scope = str(sessions_dir.resolve())
        _mk_session(db, "dead", source="telegram", session_key="agent:main:telegram:dm:1")
        _mk_session(db, "alive", source="telegram", session_key="agent:main:telegram:dm:2")
        _seed_routing(db, "agent:main:telegram:dm:1", "dead", scope)
        _seed_routing(db, "agent:main:telegram:dm:2", "alive", scope)
        db.set_session_archived("dead", True)

        db.delete_session("dead", sessions_dir=sessions_dir)

        entries = db.load_gateway_routing_entries(scope=scope)
        assert "agent:main:telegram:dm:1" not in entries
        assert "agent:main:telegram:dm:2" in entries
        assert db.get_session("alive")["archived"] == 0


# ---------------------------------------------------------------------------
# 7. Prune / delete_empty: arquivados NÃO são pegos
# ---------------------------------------------------------------------------

class TestArchiveVsPrune:
    def test_prune_default_includes_archived(self, db):
        """CONTRATO REAL (validado): prune com archived=None (padrão) pega AMBAS.

        O filtro é tri-state documentado (hermes_state._prune_filter_where):
        None = both, True = only archived, False = only unarchived.
        Para preservar arquivadas, o caller DEVE passar archived=False.
        """
        old = time.time() - 200 * 86400
        _mk_session(db, "archived_old", started_at=old, end_reason="done",
                    ended_at=old + 10)
        db.set_session_archived("archived_old", True)
        _mk_session(db, "plain_old", started_at=old, end_reason="done",
                    ended_at=old + 10)

        pruned = db.prune_sessions(older_than_days=90)
        assert db.get_session("archived_old") is None
        assert db.get_session("plain_old") is None

    def test_prune_archived_false_preserves_archived(self, db):
        """Com archived=False, arquivadas são preservadas (uso recomendado)."""
        old = time.time() - 200 * 86400
        _mk_session(db, "archived_old", started_at=old, end_reason="done",
                    ended_at=old + 10)
        db.set_session_archived("archived_old", True)
        _mk_session(db, "plain_old", started_at=old, end_reason="done",
                    ended_at=old + 10)

        pruned = db.prune_sessions(older_than_days=90, archived=False)
        assert db.get_session("archived_old") is not None, "arquivada preservada"
        assert db.get_session("plain_old") is None

    def test_delete_empty_skips_archived(self, db):
        now = time.time()
        _mk_session(db, "arch_empty", message_count=0, end_reason="done",
                    ended_at=now - 10)
        db.set_session_archived("arch_empty", True)
        _mk_session(db, "plain_empty", message_count=0, end_reason="done",
                    ended_at=now - 10)

        deleted = db.delete_empty_sessions()
        assert db.get_session("arch_empty") is not None
        assert db.get_session("plain_empty") is None

    def test_prune_explicit_archived_filter(self, db):
        old = time.time() - 200 * 86400
        _mk_session(db, "archived_old", started_at=old, end_reason="done",
                    ended_at=old + 10)
        db.set_session_archived("archived_old", True)
        pruned = db.prune_sessions(older_than_days=90, archived=True)
        assert db.get_session("archived_old") is None, "filtro archived=True apaga"


# ---------------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------------

class TestArchiveEdgeCases:
    def test_archive_does_not_change_title_or_messages(self, db):
        _mk_session(db, "s1", title="Título", message_count=3)
        db.append_message(session_id="s1", role="user", content="a")
        db.set_session_archived("s1", True)
        assert db.get_session_title("s1") == "Título"
        assert len(db.get_messages("s1")) == 1

    def test_pinned_archived_combo(self, db):
        _mk_session(db, "s1")
        db.set_session_pinned("s1", True)
        db.set_session_archived("s1", True)
        assert db.get_session("s1")["pinned"] == 1
        assert db.get_session("s1")["archived"] == 1
        # desarquivar preserva pin
        db.set_session_archived("s1", False)
        assert db.get_session("s1")["pinned"] == 1

    def test_archive_after_delete_noop(self, db):
        _mk_session(db, "s1")
        db.delete_session("s1")
        assert db.set_session_archived("s1", True) is False

    def test_unarchive_restores_listing_with_messages(self, db):
        _mk_session(db, "s1", title="X")
        db.append_message(session_id="s1", role="user", content="histórico")
        db.set_session_archived("s1", True)
        ids = {s["id"] for s in db.list_sessions_rich()}
        assert "s1" not in ids
        db.set_session_archived("s1", False)
        ids = {s["id"] for s in db.list_sessions_rich()}
        assert "s1" in ids
        assert len(db.get_messages("s1")) == 1, "histórico intacto pós-desarquivar"

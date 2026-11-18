"""
test_history.py — Tests unitaires pour le module src/history.py
Teste NoteHistoryDB et ProtectedFieldsDB de façon isolée (in-memory SQLite).
"""

import os
import sys
import pytest
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from anki_import_diff.src.history import NoteHistoryDB, ProtectedFieldsDB, VersionEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """DB in-memory in a temp dir for isolation."""
    db_path = str(tmp_path / "test_history.db")
    db = NoteHistoryDB(db_path=db_path)
    yield db
    # cleanup singleton if set to this instance
    NoteHistoryDB._instance = None


@pytest.fixture
def protection_db(tmp_db):
    return ProtectedFieldsDB(db=tmp_db)


# ---------------------------------------------------------------------------
# NoteHistoryDB tests
# ---------------------------------------------------------------------------

class TestNoteHistoryDB:
    def test_store_and_retrieve_snapshot(self, tmp_db):
        vid = tmp_db.store_snapshot(
            note_id=1,
            fields={"Recto": "Hello", "Verso": "World"},
            source="import_csv",
            comment="Test snapshot",
        )
        assert isinstance(vid, str) and len(vid) == 32

        history = tmp_db.get_history(1)
        assert len(history) == 1
        assert history[0].version_id == vid
        assert history[0].note_id == 1
        assert history[0].source == "import_csv"
        assert history[0].comment == "Test snapshot"
        assert history[0].fields["Recto"] == "Hello"
        assert history[0].fields["Verso"] == "World"

    def test_multiple_snapshots_ordered_newest_first(self, tmp_db):
        tmp_db.store_snapshot(1, {"F": "v1"}, comment="first")
        tmp_db.store_snapshot(1, {"F": "v2"}, comment="second")
        tmp_db.store_snapshot(1, {"F": "v3"}, comment="third")

        history = tmp_db.get_history(1)
        assert len(history) == 3
        # Most recent first
        assert history[0].comment == "third"
        assert history[2].comment == "first"

    def test_get_version_by_id(self, tmp_db):
        vid = tmp_db.store_snapshot(2, {"X": "value"}, source="manual", comment="specific")
        entry = tmp_db.get_version(vid)
        assert entry is not None
        assert entry.fields["X"] == "value"
        assert entry.source == "manual"

    def test_get_version_nonexistent_returns_none(self, tmp_db):
        result = tmp_db.get_version("nonexistent-version-id")
        assert result is None

    def test_prune_old_versions(self, tmp_db):
        for i in range(10):
            tmp_db.store_snapshot(3, {"F": f"v{i}"}, comment=f"snap{i}")

        tmp_db.prune_old_versions(3, keep=5)
        history = tmp_db.get_history(3)
        assert len(history) == 5
        # Should keep the 5 most recent
        assert history[0].comment == "snap9"

    def test_auto_prune_at_50_versions(self, tmp_db):
        for i in range(55):
            tmp_db.store_snapshot(4, {"F": f"v{i}"}, comment=f"snap{i}")

        history = tmp_db.get_history(4)
        assert len(history) == 50

    def test_delete_note_history(self, tmp_db):
        tmp_db.store_snapshot(5, {"F": "v1"})
        tmp_db.store_snapshot(5, {"F": "v2"})
        assert len(tmp_db.get_history(5)) == 2

        tmp_db.delete_note_history(5)
        assert len(tmp_db.get_history(5)) == 0

    def test_clear_all(self, tmp_db):
        tmp_db.store_snapshot(6, {"F": "x"})
        tmp_db.store_snapshot(7, {"F": "y"})
        assert tmp_db.note_count() == 2

        tmp_db.clear_all()
        assert tmp_db.note_count() == 0
        assert tmp_db.version_count() == 0

    def test_note_count_and_version_count(self, tmp_db):
        tmp_db.store_snapshot(8, {"F": "v1"})
        tmp_db.store_snapshot(8, {"F": "v2"})
        tmp_db.store_snapshot(9, {"F": "v3"})

        assert tmp_db.note_count() == 2
        assert tmp_db.version_count() == 3

    def test_empty_fields_stored_correctly(self, tmp_db):
        tmp_db.store_snapshot(10, {})
        history = tmp_db.get_history(10)
        assert len(history) == 1
        assert history[0].fields == {}

    def test_unicode_fields(self, tmp_db):
        tmp_db.store_snapshot(11, {"Recto": "Hépatite A → B ≠ C", "Verso": "日本語テスト"})
        history = tmp_db.get_history(11)
        assert history[0].fields["Recto"] == "Hépatite A → B ≠ C"
        assert history[0].fields["Verso"] == "日本語テスト"

    def test_html_fields(self, tmp_db):
        html_val = "<b>Bold</b> <span class='cloze'>{{c1::test}}</span>"
        tmp_db.store_snapshot(12, {"Recto": html_val})
        history = tmp_db.get_history(12)
        assert history[0].fields["Recto"] == html_val

    def test_isolated_by_note_id(self, tmp_db):
        tmp_db.store_snapshot(20, {"F": "note20"})
        tmp_db.store_snapshot(21, {"F": "note21"})

        h20 = tmp_db.get_history(20)
        h21 = tmp_db.get_history(21)
        assert len(h20) == 1 and h20[0].fields["F"] == "note20"
        assert len(h21) == 1 and h21[0].fields["F"] == "note21"

    def test_version_entry_timestamp_str(self, tmp_db):
        tmp_db.store_snapshot(30, {"F": "v"})
        entry = tmp_db.get_history(30)[0]
        # Format: "YYYY-MM-DD HH:MM"
        ts = entry.timestamp_str
        assert len(ts) == 16
        assert ts[4] == "-" and ts[7] == "-"

    def test_version_entry_source_icon(self, tmp_db):
        tmp_db.store_snapshot(31, {"F": "v"}, source="import_csv")
        tmp_db.store_snapshot(32, {"F": "v"}, source="ankicollab")
        tmp_db.store_snapshot(33, {"F": "v"}, source="manual")

        e31 = tmp_db.get_history(31)[0]
        e32 = tmp_db.get_history(32)[0]
        e33 = tmp_db.get_history(33)[0]
        assert e31.source_icon == "[CSV]"
        assert e32.source_icon == "[Collab]"
        assert e33.source_icon == "[Manuel]"

    def test_tags_stored_and_retrieved(self, tmp_db):
        tmp_db.store_snapshot(
            note_id=40,
            fields={"F": "val"},
            source="manual",
            tags=["tag1", "tag2"],
        )
        history = tmp_db.get_history(40)
        assert len(history) == 1
        assert history[0].fields == {"F": "val"}
        assert history[0].tags == ["tag1", "tag2"]

        # If tags are empty
        tmp_db.store_snapshot(
            note_id=41,
            fields={"F": "val"},
            source="manual",
            tags=[],
        )
        history = tmp_db.get_history(41)
        assert len(history) == 1
        assert history[0].fields == {"F": "val"}
        assert history[0].tags == []

    def test_update_snapshot_fields(self, tmp_db):
        vid = tmp_db.store_snapshot(
            note_id=42,
            fields={"F": "original"},
            source="manual",
            comment="Initial",
            tags=["tagA"]
        )
        tmp_db.update_snapshot_fields(
            version_id=vid,
            fields={"F": "updated"},
            comment="Modified"
        )
        entry = tmp_db.get_version(vid)
        assert entry is not None
        assert entry.fields == {"F": "updated"}
        assert entry.comment == "Modified"
        # Verify tags are preserved
        assert entry.tags == ["tagA"]

    def test_exact_duplicate_not_stored(self, tmp_db):
        vid1 = tmp_db.store_snapshot(50, {"F": "same"}, tags=["t1"])
        vid2 = tmp_db.store_snapshot(50, {"F": "same"}, tags=["t1"])
        history = tmp_db.get_history(50)
        assert len(history) == 1
        assert history[0].version_id == vid1

    def test_session_coalescing(self, tmp_db):
        # Initial edit in session
        vid1 = tmp_db.store_snapshot(51, {"F": "draft 1"}, source="manual", comment="Sauvegarde automatique (édition)")
        # Second rapid edit within same session
        vid2 = tmp_db.store_snapshot(51, {"F": "draft 2"}, source="manual", comment="Sauvegarde automatique (édition)")
        history = tmp_db.get_history(51)
        assert len(history) == 1
        assert history[0].fields["F"] == "draft 2"

    def test_cleanup_redundant_duplicates(self, tmp_db):
        conn = tmp_db._conn()
        conn.execute("INSERT INTO note_versions VALUES ('v1', 52, '2026-09-01T10:00:00', 'manual', 'c1', '{\"F\": \"val\"}')")
        conn.execute("INSERT INTO note_versions VALUES ('v2', 52, '2026-09-01T10:01:00', 'manual', 'c2', '{\"F\": \"val\"}')")
        conn.commit()
        assert len(tmp_db.get_history(52)) == 2
        tmp_db.cleanup_redundant_duplicates()
        history = tmp_db.get_history(52)
        assert len(history) == 1

    def test_ensure_baseline_snapshot(self, tmp_db):
        # 1. Baseline initial created
        vid = tmp_db.ensure_baseline_snapshot(60, {"F": "original"}, tags=["t1"])
        assert vid is not None
        h = tmp_db.get_history(60)
        assert len(h) == 1
        assert h[0].source == "baseline"
        assert h[0].fields["F"] == "original"

        # 2. Calling ensure_baseline_snapshot again with same data does not duplicate
        vid2 = tmp_db.ensure_baseline_snapshot(60, {"F": "original"}, tags=["t1"])
        assert vid2 is None
        assert len(tmp_db.get_history(60)) == 1

        # 3. Store a manual edit: baseline is NOT overwritten
        tmp_db.store_snapshot(60, {"F": "edited 1"}, source="manual")
        h2 = tmp_db.get_history(60)
        assert len(h2) == 2
        # Most recent is manual edit
        assert h2[0].source == "manual" and h2[0].fields["F"] == "edited 1"
        # Older is baseline, intact
        assert h2[1].source == "baseline" and h2[1].fields["F"] == "original"

    def test_consolidate_intermediate_versions(self, tmp_db):
        from datetime import datetime, timedelta
        conn = tmp_db._conn()
        t0 = datetime(2026, 10, 5, 14, 0, 0)
        for i in range(6):
            ts = (t0 + timedelta(seconds=i * 2)).isoformat()
            conn.execute(
                "INSERT INTO note_versions VALUES (?, ?, ?, 'manual', 'burst', ?)",
                (f"v_burst_{i}", 70, ts, f'{{"F": "val_{i}"}}'),
            )
        conn.commit()
        assert len(tmp_db.get_history(70)) == 6
        deleted = tmp_db.consolidate_intermediate_versions(70)
        assert deleted == 4
        h = tmp_db.get_history(70)
        assert len(h) == 2
        # Kept the first (initial before typing) and last (final result)
        assert h[0].fields["F"] == "val_5"
        assert h[1].fields["F"] == "val_0"

    def test_get_history_with_deltas(self, tmp_db):
        tmp_db.store_snapshot(80, {"Recto": "Hello", "Verso": "World"}, tags=["tag1"])
        tmp_db.store_snapshot(80, {"Recto": "Hello!", "Verso": "World"}, tags=["tag1", "tag2"])
        
        deltas = tmp_db.get_history_with_deltas(80)
        assert len(deltas) == 2
        
        # Newest version is deltas[0]
        v_new = deltas[0]
        assert v_new.version_number == 2
        assert "Recto" in v_new.changed_fields
        assert v_new.changed_fields["Recto"].old_value == "Hello"
        assert v_new.changed_fields["Recto"].new_value == "Hello!"
        # Verso was not changed, so it should NOT be in changed_fields
        assert "Verso" not in v_new.changed_fields
        assert v_new.tags_changed is True
        assert v_new.new_tags == ["tag1", "tag2"]

    def test_close_releases_file_lock(self, tmp_path):
        db_path = str(tmp_path / "lock_test.db")
        db = NoteHistoryDB(db_path=db_path)
        db.store_snapshot(99, {"F": "data"})
        db.close()
        # The file should be removable immediately on Windows without PermissionError
        os.remove(db_path)
        assert not os.path.exists(db_path)

    def test_reset_instance_closes_connections(self, tmp_path):
        db_path = str(tmp_path / "reset_test.db")
        db = NoteHistoryDB(db_path=db_path)
        NoteHistoryDB._instance = db
        db.store_snapshot(100, {"F": "data"})
        NoteHistoryDB.reset_instance()
        assert NoteHistoryDB._instance is None
        os.remove(db_path)
        assert not os.path.exists(db_path)



# ---------------------------------------------------------------------------
# ProtectedFieldsDB tests
# ---------------------------------------------------------------------------

class TestProtectedFieldsDB:
    def test_protect_and_check(self, tmp_db, protection_db):
        assert not protection_db.is_protected(100, "Recto")
        protection_db.protect_field(100, "Recto", "Valeur protégée")
        assert protection_db.is_protected(100, "Recto")

    def test_get_protected_value(self, tmp_db, protection_db):
        protection_db.protect_field(101, "Verso", "Value locked")
        val = protection_db.get_protected_value(101, "Verso")
        assert val == "Value locked"

    def test_get_protected_value_nonexistent_returns_none(self, protection_db):
        assert protection_db.get_protected_value(999, "NoField") is None

    def test_unprotect_field(self, protection_db):
        protection_db.protect_field(102, "F1", "locked")
        assert protection_db.is_protected(102, "F1")
        protection_db.unprotect_field(102, "F1")
        assert not protection_db.is_protected(102, "F1")

    def test_get_all_protected(self, protection_db):
        protection_db.protect_field(103, "Recto", "val_recto")
        protection_db.protect_field(103, "Verso", "val_verso")

        all_p = protection_db.get_all_protected(103)
        assert all_p == {"Recto": "val_recto", "Verso": "val_verso"}

    def test_get_all_protected_empty(self, protection_db):
        assert protection_db.get_all_protected(999) == {}

    def test_protect_replace_existing(self, protection_db):
        protection_db.protect_field(104, "F", "old_value")
        protection_db.protect_field(104, "F", "new_value")
        assert protection_db.get_protected_value(104, "F") == "new_value"

    def test_isolated_by_note_id(self, protection_db):
        protection_db.protect_field(200, "F", "val200")
        protection_db.protect_field(201, "F", "val201")

        assert protection_db.get_protected_value(200, "F") == "val200"
        assert protection_db.get_protected_value(201, "F") == "val201"

    def test_clear_note_protections(self, protection_db):
        protection_db.protect_field(202, "F1", "v1")
        protection_db.protect_field(202, "F2", "v2")
        assert len(protection_db.get_all_protected(202)) == 2

        protection_db.clear_note_protections(202)
        assert len(protection_db.get_all_protected(202)) == 0

    def test_protection_info_contains_timestamp(self, protection_db):
        protection_db.protect_field(203, "Field", "locked_val")
        info = protection_db.get_protection_info(203, "Field")
        assert info is not None
        assert info["locked_value"] == "locked_val"
        assert isinstance(info["locked_at"], datetime)

    def test_protection_info_nonexistent_returns_none(self, protection_db):
        assert protection_db.get_protection_info(999, "NoField") is None

    def test_multiple_fields_per_note(self, protection_db):
        """Each field of a note can be independently protected."""
        fields = {"Recto": "r", "Verso": "v", "Extra": "e"}
        for name, val in fields.items():
            protection_db.protect_field(300, name, val)

        for name, val in fields.items():
            assert protection_db.is_protected(300, name)
            assert protection_db.get_protected_value(300, name) == val

        protection_db.unprotect_field(300, "Verso")
        assert not protection_db.is_protected(300, "Verso")
        assert protection_db.is_protected(300, "Recto")
        assert protection_db.is_protected(300, "Extra")


def test_history_panel_ui(qtbot, tmp_db):
    """Test le panneau d'historique personnel et le mode de comparaison des champs modifies."""
    from PyQt6 import QtWidgets
    from anki_import_diff.src.ankicollab_panel import (
        AnkiCollabHistoryPanel,
        FieldComparisonCard,
        TagsComparisonCard,
        UnchangedFieldsCollapsible,
    )

    NoteHistoryDB._instance = tmp_db

    # Inserer deux versions
    tmp_db.store_snapshot(
        note_id=500,
        fields={"Recto": "Ancien Recto", "Verso": "Meme Verso", "Extra": "Ancien Extra"},
        source="baseline",
        comment="Etat initial",
        tags=["tagA"],
    )
    tmp_db.store_snapshot(
        note_id=500,
        fields={"Recto": "Nouveau Recto", "Verso": "Meme Verso", "Extra": ""},
        source="manual",
        comment="Sauvegarde automatique (edition)",
        tags=["tagA", "tagB"],
    )

    current_fields = {"Recto": "Nouveau Recto", "Verso": "Meme Verso", "Extra": "", "__tags__": "tagA tagB"}
    field_names = ["Recto", "Verso", "Extra"]

    panel = AnkiCollabHistoryPanel(
        note_id=500,
        field_names=field_names,
        current_fields=current_fields,
    )
    qtbot.addWidget(panel)

    # Verifier que 2 versions sont dans la liste
    assert panel._list.count() == 2

    # Verifier que le premier element (v2) est selectionne
    assert panel._selected_delta is not None
    assert panel._selected_delta.version_number == 2

    # Verifier que seuls Recto et Extra (les champs modifies) ont des cartes, pas Verso !
    cards = panel._cards_container.findChildren(FieldComparisonCard)
    card_names = [c.findChild(QtWidgets.QLabel).text() for c in cards]
    assert any("Recto" in name for name in card_names)
    assert any("Extra" in name for name in card_names)
    assert not any("Verso" in name for name in card_names)

    # Verifier la carte de tags
    tags_cards = panel._cards_container.findChildren(TagsComparisonCard)
    assert len(tags_cards) == 1

    # Verifier le tiroir des champs inchanges (Verso doit etre dans la liste des inchanges)
    unchanged_coll = panel._cards_container.findChildren(UnchangedFieldsCollapsible)
    assert len(unchanged_coll) == 1
    assert "Verso" in unchanged_coll[0]._unchanged

    # Tester la restauration d'un champ individuel via signal
    received = []
    panel.version_retrieved.connect(lambda f: received.append(f))

    # Appeler directement la restauration de l'ancien Recto
    panel._on_restore_field("Recto", "Ancien Recto")
    assert len(received) == 1
    assert received[0] == {"Recto": "Ancien Recto"}

    # Tester la restauration de toute la version v1
    panel._list.setCurrentRow(1)  # v1
    panel._on_restore_full_version()
    assert len(received) == 2
    assert received[1]["Recto"] == "Ancien Recto"
    assert received[1]["Extra"] == "Ancien Extra"


def test_api_client(qtbot):
    import sys
    import importlib
    import aqt
    from aqt.qt import QMainWindow
    
    from unittest.mock import MagicMock
    
    # Mock mw
    class MockAddonManager:
        def getConfig(self, name):
            return {}
        def writeConfig(self, name, config):
            pass
            
    class MockMW(QMainWindow):
        def __init__(self):
            super().__init__()
            self.addonManager = MockAddonManager()
            self.pm = MagicMock()
            self.taskman = MagicMock()
            
    aqt.mw = MockMW()
    
    sys.path.insert(0, r'c:\Users\Pipou\AppData\Roaming\Anki2\addons21')
    try:
        mod_auth = importlib.import_module("1957538407.auth_manager")
        mod_api = importlib.import_module("1957538407.api_client")
        auth_manager = mod_auth.auth_manager
        api_client = mod_api.api_client
        print("\n[API] Modules imported!")
        print(f"[API] Logged in: {auth_manager.is_logged_in()}")
        token = auth_manager.get_token()
        if token:
            resp = api_client.get("/GetNotificationsHistory?offset=0&limit=20", auth=True, timeout=5)
            print(f"[API] GET /GetNotificationsHistory -> Status {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                print(f"[API] Found {len(items)} notification items.")
                for item in items[:5]:
                    print(f"  Item: {item}")
    except Exception as e:
        print(f"\n[API] Failed: {e}")


def test_field_comparison_card_copy_formatted_text(qtbot):
    """Copier un champ dans l'historique copie le texte formaté (HTML + texte brut lisible), pas le HTML brut en texte."""
    from PyQt6 import QtWidgets
    from anki_import_diff.src.ankicollab_panel import FieldComparisonCard

    card = FieldComparisonCard(
        field_name="Recto",
        old_val="<div><b>Titre</b><br>Contenu ancien</div>",
        new_val="<div><b>Titre</b><br>Contenu nouveau</div>",
        current_val="<div><b>Titre</b><br>Contenu nouveau</div>",
        old_title="Avant",
        new_title="Après",
        render_mode="diff",
    )
    qtbot.addWidget(card)

    copy_btns = [b for b in card.findChildren(QtWidgets.QPushButton) if b.text() == "Copier"]
    assert len(copy_btns) == 2
    copy_btns[0].click()

    app = QtWidgets.QApplication.instance()
    clipboard = app.clipboard()
    mime = clipboard.mimeData()
    assert mime.hasHtml()
    assert "<b>Titre</b>" in mime.html()
    assert "<div>" not in clipboard.text()
    assert "Titre\nContenu ancien" == clipboard.text()


def test_sync_editor_with_note_updates_in_memory_note():
    """_sync_editor_with_note met à jour editor.note en mémoire et appelle loadNote()."""
    from anki_import_diff.src.browser_integration import _sync_editor_with_note

    class FakeNote(dict):
        def __init__(self, nid, data, tags):
            super().__init__(data)
            self.id = nid
            self.tags = list(tags)

    class FakeEditor:
        def __init__(self, note):
            self.note = note
            self.loaded = False

        def loadNote(self):
            self.loaded = True

    ed_note = FakeNote(42, {"Recto": "Nouveau", "Verso": "V1"}, ["tag1"])
    target_note = FakeNote(42, {"Recto": "Ancien", "Verso": "V1"}, ["tag1", "tag2"])
    editor = FakeEditor(ed_note)

    _sync_editor_with_note(editor, target_note, {"Recto": "Ancien"})
    assert editor.note["Recto"] == "Ancien"
    assert editor.loaded is True


def test_scan_backups_cache_and_mod_skip(tmp_path, monkeypatch):
    """Vérifie le cache persistant backup_scans, le saut par b_mod et l'annulation."""
    import sqlite3
    import threading
    from datetime import datetime
    import anki._backend
    from anki_import_diff.src.history import NoteHistoryDB

    db = NoteHistoryDB(str(tmp_path / "hist.db"))
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # 3 sauvegardes :
    # - 2026-11-10 : note modifiée le 2026-11-01
    # - 2026-11-05 : entre 2026-11-01 (+1h) et 2026-11-10 -> doit être SAUTÉE sans décompression
    # - 2026-10-20 : antérieure au 2026-11-01 -> doit être inspectée
    b_new = backup_dir / "backup-2026-11-10-12.00.00.colpkg"
    b_mid = backup_dir / "backup-2026-11-05-12.00.00.colpkg"
    b_old = backup_dir / "backup-2026-10-20-12.00.00.colpkg"
    for p in (b_new, b_mid, b_old):
        p.write_bytes(b"fake")

    mod_nov01 = int(datetime(2026, 11, 1, 10, 0, 0).timestamp())
    mod_oct15 = int(datetime(2026, 10, 15, 10, 0, 0).timestamp())

    imported_calls = []

    class FakeBackend:
        def import_collection_package(self, col_path, backup_path, media_folder, media_db):
            imported_calls.append(os.path.basename(backup_path))
            c = sqlite3.connect(col_path)
            c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, mid INTEGER, mod INTEGER, flds TEXT, tags TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS fields (ntid INTEGER, ord INTEGER, name TEXT)")
            c.execute("DELETE FROM notes")
            c.execute("DELETE FROM fields")
            c.execute("INSERT INTO fields VALUES (1, 0, 'Recto')")
            if "2026-11-10" in backup_path:
                c.execute("INSERT INTO notes VALUES (999, 1, ?, 'Version Nov', 't1')", (mod_nov01,))
            elif "2026-10-20" in backup_path:
                c.execute("INSERT INTO notes VALUES (999, 1, ?, 'Version Oct', 't1')", (mod_oct15,))
            else:
                raise AssertionError(f"Sauvegarde intermédiaire non sautée: {backup_path}")
            c.commit()
            c.close()

    monkeypatch.setattr(anki._backend, "RustBackend", FakeBackend)

    progress_events = []
    versions = db.scan_backups_for_note(
        999,
        progress_callback=lambda curr, tot, found: progress_events.append((curr, tot, found)),
    )

    assert len(versions) == 2
    # b_mid a été sautée grâce à b_mod
    assert imported_calls == [
        "backup-2026-11-10-12.00.00.colpkg",
        "backup-2026-10-20-12.00.00.colpkg",
    ]
    assert progress_events == [(1, 3, 1), (2, 3, 1), (3, 3, 2)]

    # 2e appel : tout est en cache, 0 décompression
    imported_calls.clear()
    versions2 = db.scan_backups_for_note(999)
    assert len(versions2) == 2
    assert imported_calls == []

    # Test annulation immédiate sur une autre note
    cancel_ev = threading.Event()
    cancel_ev.set()
    versions3 = db.scan_backups_for_note(888, cancel_event=cancel_ev)
    assert versions3 == []
    assert imported_calls == []
    db.close()


def test_history_panel_live_streaming_preserves_selection(qtbot, tmp_db, monkeypatch):
    """Vérifie que le scan démarre automatiquement sans boutons et rafraîchit la liste en continu."""
    from PyQt6 import QtWidgets
    from anki_import_diff.src.ankicollab_panel import AnkiCollabHistoryPanel
    from anki_import_diff.src.history import NoteHistoryDB

    NoteHistoryDB._instance = tmp_db
    scan_calls = []

    def fake_scan(note_id, progress_callback=None, cancel_event=None):
        scan_calls.append(note_id)
        return tmp_db.get_history(note_id)

    monkeypatch.setattr(tmp_db, "scan_backups_for_note", fake_scan)

    tmp_db.store_snapshot(555, {"Recto": "V2"}, source="import", comment="V2")
    panel = AnkiCollabHistoryPanel(
        note_id=555,
        field_names=["Recto"],
        current_fields={"Recto": "V2"},
    )
    qtbot.addWidget(panel)

    # Les boutons "Rechercher dans les sauvegardes Anki" et "Actualiser" ont été retirés
    assert not hasattr(panel, "_btn_scan_backups")
    btn_texts = [b.text() for b in panel.findChildren(QtWidgets.QPushButton)]
    assert "Rechercher dans les sauvegardes Anki" not in btn_texts
    assert "Actualiser" not in btn_texts

    # Le scan automatique a démarré pour la note 555
    qtbot.waitUntil(lambda: 555 in scan_calls and not panel._is_scanning_backups, timeout=2000)

    assert panel._list.count() == 1
    v2_id = panel._selected_delta.version.version_id

    # Simuler l'insertion d'une version antérieure pendant le scan
    tmp_db.store_snapshot(555, {"Recto": "V1"}, source="backup", comment="Backup V1")
    panel._on_scan_progress(555, 2, 10, 1, True)

    assert panel._list.count() == 2
    assert "Recherche (2/10)... 1 version(s) trouvée(s)" in panel._scan_status_lbl.text()
    # La sélection reste sur V2
    assert panel._selected_delta is not None
    assert panel._selected_delta.version.version_id == v2_id

    panel._on_scan_finished(555, 1, False)
    assert "Sauvegardes indexées (1 nouvelle(s) version(s))." in panel._scan_status_lbl.text()

    # Changer de note relance le scan automatiquement
    tmp_db.store_snapshot(556, {"Recto": "Autre"}, source="import", comment="Autre")
    panel.set_note(556, ["Recto"], {"Recto": "Autre"})
    qtbot.waitUntil(lambda: 556 in scan_calls and not panel._is_scanning_backups, timeout=2000)


def test_refresh_reviewer_if_showing_uses_redraw_not_show(monkeypatch):
    """Vérifie que _refresh_reviewer_if_showing redessine sans appeler reviewer.show()."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import aqt
    from anki_import_diff.src.browser_integration import _refresh_reviewer_if_showing

    reviewer = MagicMock()
    reviewer.card = SimpleNamespace(nid=42, load=MagicMock())
    fake_mw = SimpleNamespace(state="review", reviewer=reviewer)
    monkeypatch.setattr(aqt, "mw", fake_mw)

    _refresh_reviewer_if_showing(42)
    reviewer._redraw_current_card.assert_called_once()
    reviewer.show.assert_not_called()

    # Ne doit rien faire si la note ne correspond pas
    reviewer.reset_mock()
    _refresh_reviewer_if_showing(99)
    reviewer._redraw_current_card.assert_not_called()
    reviewer.show.assert_not_called()

    # Ne doit rien faire hors état "review"
    reviewer.reset_mock()
    fake_mw.state = "overview"
    _refresh_reviewer_if_showing(42)
    reviewer._redraw_current_card.assert_not_called()
    reviewer.show.assert_not_called()

    # Fallback si _redraw_current_card est absent (question / answer)
    fallback_reviewer = SimpleNamespace(
        card=SimpleNamespace(nid=42, load=MagicMock()),
        state="answer",
        _showAnswer=MagicMock(),
        _showQuestion=MagicMock(),
        show=MagicMock(),
    )
    fake_mw.state = "review"
    fake_mw.reviewer = fallback_reviewer
    _refresh_reviewer_if_showing(42)
    fallback_reviewer.card.load.assert_called_once()
    fallback_reviewer._showAnswer.assert_called_once()
    fallback_reviewer._showQuestion.assert_not_called()
    fallback_reviewer.show.assert_not_called()

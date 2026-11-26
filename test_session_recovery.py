"""
Tests unitaires pour la persistance et la reprise après crash des sessions Diff Hunk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aqt
import pytest

try:
    from PyQt6 import QtCore, QtWidgets
except ImportError:
    from PySide6 import QtCore, QtWidgets

from anki_import_diff.src.savepoint import NoteDiff
from anki_import_diff.src.session_recovery import (
    RECOVERY_FILENAME,
    RECOVERY_VERSION,
    cancel_scheduled_save,
    clear_saved_session,
    deserialize_note_diff,
    get_recovery_file_path,
    load_saved_session,
    save_pending_session,
    schedule_session_save,
    serialize_note_diff,
)
import anki_import_diff.src.browser_integration as bi
import anki_import_diff.src.hooks as hooks


# ---------------------------------------------------------------------------
# Infrastructure légère de mock SQLite pour tester rollback / resume
# ---------------------------------------------------------------------------

class _FakeNote:
    def __init__(self, nid: int, mid: int, fields: list[str], tags: list[str], guid: str = ""):
        self.id = nid
        self.mid = mid
        self.fields = list(fields)
        self.tags = list(tags)
        self.guid = guid

    def card_ids(self):
        return [self.id * 10 + 1]


class _FakeModels:
    def __init__(self):
        self._models = {
            1: {"id": 1, "name": "Basic", "flds": [{"name": "Front"}, {"name": "Back"}]}
        }

    def get(self, mid):
        return self._models.get(mid)

    def by_name(self, name):
        for m in self._models.values():
            if m["name"] == name:
                return m
        return None


class _FakeDecks:
    def id(self, name: str) -> int:
        return 1

    def get(self, did: int, default=False):
        return {"id": did, "name": "Default"}


class _FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, mod INTEGER, usn INTEGER, tags TEXT, flds TEXT)")
        self.conn.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, ord INTEGER, mod INTEGER, usn INTEGER, type INTEGER, queue INTEGER)")
        self.conn.commit()

    def execute(self, sql, *args):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def scalar(self, sql, *args):
        cur = self.conn.execute(sql, args)
        row = cur.fetchone()
        return row[0] if row else None

    def first(self, sql, *args):
        cur = self.conn.execute(sql, args)
        return cur.fetchone()

    def all(self, sql, *args):
        cur = self.conn.execute(sql, args)
        return cur.fetchall()


class _FakeCollection:
    def __init__(self, col_path: str):
        self.path = col_path
        self.db = _FakeDB()
        self.models = _FakeModels()
        self.decks = _FakeDecks()
        self._next_id = 900001
        self.add_note_calls: list[int] = []
        self.removed_note_ids: list[int] = []

    def insert_raw_note(self, nid: int, fields: list[str], tags: list[str], mid: int = 1, guid: str = "") -> None:
        flds_str = "\x1f".join(fields)
        tags_str = " " + " ".join(tags) + " " if tags else ""
        self.db.execute(
            "INSERT INTO notes (id, guid, mid, mod, usn, tags, flds) VALUES (?, ?, ?, 0, -1, ?, ?)",
            nid, guid or f"g{nid}", mid, tags_str, flds_str,
        )
        self.db.execute(
            "INSERT INTO cards (id, nid, did, ord, mod, usn, type, queue) VALUES (?, ?, 1, 0, 0, -1, 0, 0)",
            nid * 10 + 1, nid,
        )

    def get_note(self, nid: int) -> _FakeNote:
        row = self.db.first("SELECT id, guid, mid, tags, flds FROM notes WHERE id = ?", nid)
        if not row:
            raise KeyError(f"No note {nid}")
        _id, guid, mid, tags_str, flds_str = row
        fields = flds_str.split("\x1f") if flds_str is not None else []
        tags = [t for t in (tags_str or "").strip().split(" ") if t]
        return _FakeNote(_id, mid, fields, tags, guid or "")

    def new_note(self, model) -> _FakeNote:
        fcount = len(model.get("flds", []))
        return _FakeNote(0, model["id"], [""] * fcount, [])

    def add_note(self, note: _FakeNote, deck_id: int) -> None:
        nid = self._next_id
        self._next_id += 1
        note.id = nid
        self.add_note_calls.append(nid)
        self.insert_raw_note(nid, note.fields, note.tags, mid=note.mid, guid=getattr(note, "guid", ""))

    def update_note(self, note: _FakeNote) -> None:
        flds_str = "\x1f".join(note.fields)
        tags_str = " " + " ".join(note.tags) + " " if note.tags else ""
        self.db.execute(
            "UPDATE notes SET flds = ?, tags = ?, usn = -1 WHERE id = ?",
            flds_str, tags_str, note.id,
        )

    def remove_notes(self, nids: list[int]) -> None:
        for nid in nids:
            self.removed_note_ids.append(nid)
            self.db.execute("DELETE FROM notes WHERE id = ?", nid)
            self.db.execute("DELETE FROM cards WHERE nid = ?", nid)


@pytest.fixture
def isolated_mw(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    col = _FakeCollection(str(profile_dir / "collection.anki2"))
    mw = SimpleNamespace(
        col=col,
        pm=SimpleNamespace(profileFolder=lambda: str(profile_dir)),
        reset=MagicMock(),
    )
    monkeypatch.setattr(aqt, "mw", mw)
    yield mw, col, profile_dir
    bi.clear_pending_diffs()
    clear_saved_session(str(profile_dir / RECOVERY_FILENAME))


# ---------------------------------------------------------------------------
# 1. Sérialisation / Désérialisation NoteDiff
# ---------------------------------------------------------------------------

def test_serialize_deserialize_round_trip():
    diff_upd = NoteDiff(
        note_id=1001,
        original_fields={"Front": "Old Q", "Back": "Old A"},
        updated_fields={"Front": "New Q", "Back": "New A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="UPDATE",
        tags=[],
        original_tags=["tag_old"],
        updated_tags=["tag_new"],
        merged_fields={"Front": "Custom Q", "Back": "New A"},
        custom_edit_fields={"Front": "Custom Q"},
        old_mod_fields={"Front": "Old Q", "Back": "Old A"},
        prop_mod_fields={"Front": "New Q", "Back": "New A"},
        field_active_target={"Front": "OLD", "Back": "PROP"},
        tags_active_target="OLD",
        excluded=True,
        model_id=1,
        guid="guid-upd",
    )
    diff_upd.tags = []  # volontairement vide après init (tous les tags retirés par l'utilisateur)
    setattr(diff_upd, "_db_synced_fields", {"Front": "Custom Q"})

    diff_ins = NoteDiff(
        note_id=2002,
        original_fields={},
        updated_fields={"Front": "Ins Q", "Back": "Ins A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="INSERT",
        tags=["ins_tag"],
        original_tags=[],
        updated_tags=["ins_tag"],
        model_id=1,
        deck_id=42,
        deck_name="EDN::Nephro",
        suspend_cards=True,
        staged_note_id=900123,
        guid="guid-ins",
    )

    rt_upd = deserialize_note_diff(serialize_note_diff(diff_upd))
    rt_ins = deserialize_note_diff(serialize_note_diff(diff_ins))

    assert rt_upd.note_id == 1001
    assert rt_upd.action == "UPDATE"
    assert rt_upd.tags == []
    assert rt_upd.original_tags == ["tag_old"]
    assert rt_upd.updated_tags == ["tag_new"]
    assert rt_upd.merged_fields == {"Front": "Custom Q", "Back": "New A"}
    assert rt_upd.custom_edit_fields == {"Front": "Custom Q"}
    assert rt_upd.field_active_target == {"Front": "OLD", "Back": "PROP"}
    assert rt_upd.tags_active_target == "OLD"
    assert rt_upd.excluded is True
    assert rt_upd.guid == "guid-upd"
    assert getattr(rt_upd, "_db_synced_fields", None) == {"Front": "Custom Q"}

    assert rt_ins.note_id == 2002
    assert rt_ins.action == "INSERT"
    assert rt_ins.staged_note_id == 900123
    assert rt_ins.deck_id == 42
    assert rt_ins.deck_name == "EDN::Nephro"
    assert rt_ins.suspend_cards is True
    assert rt_ins.guid == "guid-ins"


# ---------------------------------------------------------------------------
# 2. Sauvegarde atomique, chargement et nettoyage
# ---------------------------------------------------------------------------

def test_save_load_clear_session(tmp_path):
    rec_path = str(tmp_path / RECOVERY_FILENAME)
    diff = NoteDiff(
        note_id=101,
        original_fields={"Front": "A"},
        updated_fields={"Front": "B"},
        model_name="Basic",
        field_names=["Front"],
        action="UPDATE",
    )

    assert load_saved_session(rec_path) is None

    ok = save_pending_session({101: diff}, {90001: 202}, is_collab=True, path=rec_path)
    assert ok is True
    assert os.path.isfile(rec_path)
    assert not os.path.exists(rec_path + ".tmp")

    loaded = load_saved_session(rec_path)
    assert loaded is not None
    assert loaded["version"] == RECOVERY_VERSION
    assert loaded["is_collab"] is True
    assert loaded["staged_to_orig"] == {90001: 202}
    assert len(loaded["diffs"]) == 1
    assert loaded["diffs"][0].note_id == 101

    # Fichier corrompu -> None
    with open(rec_path, "w", encoding="utf-8") as f:
        f.write("{corrupt json")
    assert load_saved_session(rec_path) is None

    # Version incompatible -> None
    with open(rec_path, "w", encoding="utf-8") as f:
        json.dump({"version": 999, "diffs": [serialize_note_diff(diff)]}, f)
    assert load_saved_session(rec_path) is None

    # Diffs vides -> supprime le fichier existant
    assert save_pending_session({}, {}, False, path=rec_path) is True
    assert not os.path.exists(rec_path)


def test_get_recovery_file_path_no_home_fallback(tmp_path, monkeypatch):
    # mw is None -> None
    monkeypatch.setattr(aqt, "mw", None)
    assert get_recovery_file_path() is None

    # mw is MagicMock (comme dans certains tests) -> None
    monkeypatch.setattr(aqt, "mw", MagicMock())
    assert get_recovery_file_path() is None

    # mw avec dossier inexistant -> None
    fake_mw = SimpleNamespace(
        pm=SimpleNamespace(profileFolder=lambda: str(tmp_path / "missing")),
        col=SimpleNamespace(path=str(tmp_path / "missing" / "collection.anki2")),
    )
    monkeypatch.setattr(aqt, "mw", fake_mw)
    assert get_recovery_file_path() is None

    # mw avec vrai dossier existant via col.path seul -> résolu
    real_dir = tmp_path / "real_prof"
    real_dir.mkdir()
    fake_mw2 = SimpleNamespace(
        pm=None,
        col=SimpleNamespace(path=str(real_dir / "collection.anki2")),
    )
    monkeypatch.setattr(aqt, "mw", fake_mw2)
    assert get_recovery_file_path() == os.path.join(str(real_dir), RECOVERY_FILENAME)


# ---------------------------------------------------------------------------
# 3. Rollback & Resume après crash
# ---------------------------------------------------------------------------

def test_rollback_saved_diff_session(isolated_mw):
    mw, col, profile_dir = isolated_mw
    # Note UPDATE déjà modifiée en base avant le crash
    col.insert_raw_note(100, ["Edited Q", "Edited A"], ["dirty_tag"])
    # Note INSERT pré-insérée en base avant le crash
    col.insert_raw_note(900050, ["New Ins Q", "New Ins A"], ["ins_tag"])

    diff_upd = NoteDiff(
        note_id=100,
        original_fields={"Front": "Orig Q", "Back": "Orig A"},
        updated_fields={"Front": "Edited Q", "Back": "Edited A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="UPDATE",
        original_tags=["orig_tag"],
        updated_tags=["dirty_tag"],
        tags=["dirty_tag"],
    )
    diff_ins_present = NoteDiff(
        note_id=500,
        original_fields={},
        updated_fields={"Front": "New Ins Q", "Back": "New Ins A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="INSERT",
        staged_note_id=900050,
    )
    diff_ins_missing = NoteDiff(
        note_id=501,
        original_fields={},
        updated_fields={"Front": "Gone", "Back": "Gone"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="INSERT",
        staged_note_id=999999,
    )

    rec_path = str(profile_dir / RECOVERY_FILENAME)
    save_pending_session(
        {100: diff_upd, 500: diff_ins_present, 501: diff_ins_missing},
        {900050: 500, 999999: 501},
        is_collab=False,
        path=rec_path,
    )
    session = load_saved_session(rec_path)
    assert session is not None

    restored_updates, removed_inserts = bi.rollback_saved_diff_session(session)

    assert restored_updates == 1
    assert removed_inserts == 1
    restored_note = col.get_note(100)
    assert restored_note.fields == ["Orig Q", "Orig A"]
    assert restored_note.tags == ["orig_tag"]
    with pytest.raises(KeyError):
        col.get_note(900050)
    assert not os.path.exists(rec_path)


def test_resume_saved_diff_session_avoids_duplicate_inserts(isolated_mw):
    mw, col, profile_dir = isolated_mw
    # Note UPDATE existante + Note INSERT déjà pré-insérée avant le crash
    col.insert_raw_note(100, ["Orig Q", "Orig A"], ["t1"])
    col.insert_raw_note(900050, ["Staged Q", "Staged A"], ["t_ins"])
    # Note 200 absente (supprimée entre temps)

    diff_upd_ok = NoteDiff(
        note_id=100,
        original_fields={"Front": "Orig Q", "Back": "Orig A"},
        updated_fields={"Front": "Prop Q", "Back": "Orig A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="UPDATE",
    )
    diff_upd_deleted = NoteDiff(
        note_id=200,
        original_fields={"Front": "Deleted Q", "Back": ""},
        updated_fields={"Front": "Deleted Prop", "Back": ""},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="UPDATE",
    )
    diff_ins_existing = NoteDiff(
        note_id=500,
        original_fields={},
        updated_fields={"Front": "Staged Q", "Back": "Staged A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="INSERT",
        staged_note_id=900050,
    )
    diff_ins_missing = NoteDiff(
        note_id=501,
        original_fields={},
        updated_fields={"Front": "Restage Q", "Back": "Restage A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="INSERT",
        staged_note_id=0,
    )

    rec_path = str(profile_dir / RECOVERY_FILENAME)
    save_pending_session(
        {100: diff_upd_ok, 200: diff_upd_deleted, 500: diff_ins_existing, 501: diff_ins_missing},
        {900050: 500},
        is_collab=True,
        path=rec_path,
    )
    session = load_saved_session(rec_path)
    assert session is not None

    fake_browser = MagicMock()
    fake_browser.closeEvent = MagicMock()
    fake_browser.keyPressEvent = MagicMock()
    fake_browser.editor = None
    fake_panel = MagicMock()
    fake_panel.isVisible.return_value = True

    with patch.object(aqt, "dialogs", SimpleNamespace(open=MagicMock(return_value=fake_browser))), \
         patch.object(bi, "DiffBrowserPanel", return_value=fake_panel):
        resumed = bi.resume_saved_diff_session(session)

    assert resumed is True
    # 100 gardé, 200 filtré, 500 gardé avec son staged_id existant, 501 fraîchement staged
    assert set(bi._pending_diffs.keys()) == {100, 500, 501}
    assert diff_ins_existing.staged_note_id == 900050
    # Seul 501 a déclenché col.add_note (aucune duplication de 500 !)
    assert len(col.add_note_calls) == 1
    new_staged_id = col.add_note_calls[0]
    assert bi._pending_diffs[501].staged_note_id == new_staged_id
    assert bi._staged_to_orig == {900050: 500, new_staged_id: 501}
    assert os.path.isfile(rec_path)

    reloaded = load_saved_session(rec_path)
    assert reloaded is not None
    assert {d.note_id for d in reloaded["diffs"]} == {100, 500, 501}


def test_clear_pending_diffs_cancels_timer_and_removes_file(isolated_mw):
    mw, col, profile_dir = isolated_mw
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    rec_path = str(profile_dir / RECOVERY_FILENAME)
    diff = NoteDiff(
        note_id=111,
        original_fields={"Front": "A"},
        updated_fields={"Front": "B"},
        model_name="Basic",
        field_names=["Front"],
        action="UPDATE",
    )
    bi.set_pending_diffs([diff], on_commit=None, is_collab=False)
    save_pending_session(bi._pending_diffs, {}, False, path=rec_path)
    assert os.path.isfile(rec_path)

    # Planifier une sauvegarde différée qui ne doit PAS recréer le fichier après clear
    schedule_session_save(
        lambda: save_pending_session(bi._pending_diffs, {}, False, path=rec_path),
        delay_ms=40,
    )
    bi.clear_pending_diffs()
    assert not os.path.exists(rec_path)

    # Laisser passer largement le délai du timer
    deadline = time.monotonic() + 0.15
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert not os.path.exists(rec_path)


# ---------------------------------------------------------------------------
# 4. Invite au démarrage (_check_and_prompt_session_recovery)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("choice", ["resume", "rollback", "ignore"])
def test_startup_recovery_prompt_dispatch(isolated_mw, monkeypatch, choice):
    mw, col, profile_dir = isolated_mw
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    diff = NoteDiff(
        note_id=777,
        original_fields={"Front": "Old"},
        updated_fields={"Front": "New"},
        model_name="Basic",
        field_names=["Front"],
        action="UPDATE",
    )
    rec_path = str(profile_dir / RECOVERY_FILENAME)
    save_pending_session({777: diff}, {}, is_collab=False, path=rec_path)

    buttons = []

    class _FakeMsgBox:
        class Icon:
            Warning = 1

        class ButtonRole:
            AcceptRole = 0
            RejectRole = 1
            DestructiveRole = 2

        def __init__(self, parent=None):
            self._clicked = None

        def setIcon(self, icon):
            pass

        def setWindowTitle(self, title):
            pass

        def setText(self, text):
            pass

        def addButton(self, label, role):
            btn = SimpleNamespace(label=label, role=role)
            buttons.append(btn)
            return btn

        def setDefaultButton(self, btn):
            pass

        def exec(self):
            idx = {"resume": 0, "rollback": 1, "ignore": 2}[choice]
            self._clicked = buttons[idx]

        def clickedButton(self):
            return self._clicked

    mock_resume = MagicMock(return_value=True)
    mock_rollback = MagicMock(return_value=(1, 0))
    mock_clear = MagicMock()

    monkeypatch.setattr(QtWidgets, "QMessageBox", _FakeMsgBox)
    monkeypatch.setattr(hooks, "resume_saved_diff_session", mock_resume)
    monkeypatch.setattr(hooks, "rollback_saved_diff_session", mock_rollback)
    monkeypatch.setattr(hooks, "clear_saved_session", mock_clear)

    hooks._check_and_prompt_session_recovery()

    if choice == "resume":
        mock_resume.assert_called_once()
        mock_rollback.assert_not_called()
        mock_clear.assert_not_called()
    elif choice == "rollback":
        mock_rollback.assert_called_once()
        mock_resume.assert_not_called()
        mw.reset.assert_called_once()
    else:
        mock_clear.assert_called_once()
        mock_resume.assert_not_called()
        mock_rollback.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Reprise automatique après Alt+F4 -> Annuler et fusion à la réouverture
# ---------------------------------------------------------------------------

def test_auto_resume_flag_round_trip(tmp_path):
    rec_path = str(tmp_path / RECOVERY_FILENAME)
    diff = NoteDiff(
        note_id=101,
        original_fields={"Front": "A"},
        updated_fields={"Front": "B"},
        model_name="Basic",
        field_names=["Front"],
        action="UPDATE",
    )

    assert save_pending_session({101: diff}, {}, is_collab=False, path=rec_path) is True
    loaded_default = load_saved_session(rec_path)
    assert loaded_default is not None
    assert loaded_default["auto_resume"] is False

    assert save_pending_session({101: diff}, {}, is_collab=True, path=rec_path, auto_resume=True) is True
    loaded_auto = load_saved_session(rec_path)
    assert loaded_auto is not None
    assert loaded_auto["auto_resume"] is True


def test_alt_f4_cancel_preserves_session_for_auto_resume(isolated_mw, monkeypatch):
    mw, col, profile_dir = isolated_mw
    col.insert_raw_note(100, ["Edited Q", "Edited A"], ["tag1"])

    diff_upd = NoteDiff(
        note_id=100,
        original_fields={"Front": "Orig Q", "Back": "Orig A"},
        updated_fields={"Front": "Edited Q", "Back": "Edited A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="UPDATE",
        original_tags=["orig"],
        updated_tags=["tag1"],
    )
    diff_ins = NoteDiff(
        note_id=500,
        original_fields={},
        updated_fields={"Front": "Ins Q", "Back": "Ins A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="INSERT",
    )

    fake_browser = MagicMock()
    fake_browser.editor = None
    fake_browser._orig_diff_close_event = None
    fake_browser._orig_diff_close_with_callback = None
    fake_browser._orig_diff_key_press_event = None
    orig_close_event = MagicMock()
    fake_browser.closeEvent = orig_close_event

    def _orig_close_with_cb(onsuccess):
        # Simule Browser._closeWindow() appelant close() puis onsuccess()
        fake_browser.closeEvent(MagicMock())
        onsuccess()

    fake_browser.closeWithCallback = _orig_close_with_cb

    fake_panel = MagicMock()
    fake_panel.isVisible.return_value = True

    def _fake_panel_cleanup(preserve_session=False):
        bi.clear_pending_diffs(preserve_saved_session=preserve_session)

    fake_panel.cleanup.side_effect = _fake_panel_cleanup

    with patch.object(aqt, "dialogs", SimpleNamespace(open=MagicMock(return_value=fake_browser))), \
         patch.object(bi, "DiffBrowserPanel", return_value=fake_panel):
        bi.open_browser_diff([diff_upd, diff_ins], on_commit=None, is_collab=True)

    staged_id = diff_ins.staged_note_id
    assert staged_id > 0
    assert col.get_note(staged_id).fields == ["Ins Q", "Ins A"]

    monkeypatch.setattr(bi, "_decision_hook", lambda: "cancel")
    bi.set_profile_closing(True)
    onsuccess_mock = MagicMock()
    try:
        fake_browser.closeWithCallback(onsuccess_mock)
    finally:
        bi.set_profile_closing(False)

    onsuccess_mock.assert_called_once()
    orig_close_event.assert_called_once()
    # Ni rollback des UPDATE, ni suppression des INSERT staged
    assert col.get_note(100).fields == ["Edited Q", "Edited A"]
    assert col.get_note(staged_id).fields == ["Ins Q", "Ins A"]

    rec_path = str(profile_dir / RECOVERY_FILENAME)
    assert os.path.isfile(rec_path)
    loaded = load_saved_session(rec_path)
    assert loaded is not None
    assert loaded["auto_resume"] is True
    assert {d.note_id for d in loaded["diffs"]} == {100, 500}


def test_startup_auto_resume_skips_prompt(isolated_mw, monkeypatch):
    mw, col, profile_dir = isolated_mw
    diff = NoteDiff(
        note_id=777,
        original_fields={"Front": "Old"},
        updated_fields={"Front": "New"},
        model_name="Basic",
        field_names=["Front"],
        action="UPDATE",
    )
    rec_path = str(profile_dir / RECOVERY_FILENAME)
    save_pending_session({777: diff}, {}, is_collab=True, path=rec_path, auto_resume=True)

    mock_resume = MagicMock(return_value=True)
    mock_msgbox = MagicMock()
    monkeypatch.setattr(hooks, "resume_saved_diff_session", mock_resume)
    monkeypatch.setattr(QtWidgets, "QMessageBox", mock_msgbox)

    hooks._check_and_prompt_session_recovery()

    mock_resume.assert_called_once()
    mock_msgbox.assert_not_called()


def test_merge_on_reopen_after_resume_and_before_timer(isolated_mw):
    mw, col, profile_dir = isolated_mw
    col.insert_raw_note(100, ["Orig Q", "Orig A"], ["t1"])
    col.insert_raw_note(102, ["Orig Q2", "Orig A2"], ["t2"])
    col.insert_raw_note(900050, ["Staged Q", "Staged A"], ["t_ins"])

    saved_upd = NoteDiff(
        note_id=100,
        original_fields={"Front": "Orig Q", "Back": "Orig A"},
        updated_fields={"Front": "Prop Q", "Back": "Orig A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="UPDATE",
        merged_fields={"Front": "User Choice", "Back": "Orig A"},
    )
    saved_ins = NoteDiff(
        note_id=500,
        original_fields={},
        updated_fields={"Front": "Staged Q", "Back": "Staged A"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="INSERT",
        staged_note_id=900050,
    )
    rec_path = str(profile_dir / RECOVERY_FILENAME)

    # Ordre 1 : resume_saved_diff_session puis open_browser_diff avec chevauchement + nouveau diff
    save_pending_session(
        {100: saved_upd, 500: saved_ins},
        {900050: 500},
        is_collab=True,
        path=rec_path,
        auto_resume=True,
    )
    session = load_saved_session(rec_path)
    assert session is not None

    fake_browser = MagicMock()
    fake_browser.closeEvent = MagicMock()
    fake_browser.keyPressEvent = MagicMock()
    fake_browser.editor = None
    fake_panel = MagicMock()
    fake_panel.isVisible.return_value = True
    fake_panel._learned_cards_only = False

    with patch.object(aqt, "dialogs", SimpleNamespace(open=MagicMock(return_value=fake_browser))), \
         patch.object(bi, "DiffBrowserPanel", return_value=fake_panel):
        assert bi.resume_saved_diff_session(session) is True
        cb1 = MagicMock()
        # Sous-ensemble déjà présent -> attache on_commit si absent
        bi.open_browser_diff([saved_upd], on_commit=cb1, is_collab=True)
        assert bi._on_commit_cb is cb1

        new_upd = NoteDiff(
            note_id=102,
            original_fields={"Front": "Orig Q2", "Back": "Orig A2"},
            updated_fields={"Front": "Prop Q2", "Back": "Orig A2"},
            model_name="Basic",
            field_names=["Front", "Back"],
            action="UPDATE",
        )
        cb2 = MagicMock()
        bi.open_browser_diff([new_upd], on_commit=cb2, is_collab=True)

    assert set(bi._pending_diffs.keys()) == {100, 500, 102}
    assert bi._pending_diffs[100].merged_fields["Front"] == "User Choice"
    assert len(col.add_note_calls) == 0  # 500 pas dupliquée

    # Ordre 2 : open_browser_diff appelé avant le timer de reprise alors que auto_resume=True est sur disque
    bi.clear_pending_diffs()
    save_pending_session(
        {100: saved_upd, 500: saved_ins},
        {900050: 500},
        is_collab=True,
        path=rec_path,
        auto_resume=True,
    )
    new_upd_order2 = NoteDiff(
        note_id=102,
        original_fields={"Front": "Orig Q2", "Back": "Orig A2"},
        updated_fields={"Front": "Prop Q2", "Back": "Orig A2"},
        model_name="Basic",
        field_names=["Front", "Back"],
        action="UPDATE",
    )
    with patch.object(aqt, "dialogs", SimpleNamespace(open=MagicMock(return_value=fake_browser))), \
         patch.object(bi, "DiffBrowserPanel", return_value=fake_panel):
        bi.open_browser_diff([new_upd_order2], on_commit=MagicMock(), is_collab=True)

    assert set(bi._pending_diffs.keys()) == {100, 500, 102}
    assert bi._pending_diffs[100].merged_fields["Front"] == "User Choice"
    assert len(col.add_note_calls) == 0  # toujours aucune duplication de 500

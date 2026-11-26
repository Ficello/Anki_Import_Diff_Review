"""
session_recovery.py — Persistance atomique sur disque de la session Diff Hunk active.

Permet de détecter au redémarrage d'Anki une session de révision interrompue
par un crash natif (ex. Qt6WebEngineCore.dll) et d'offrir à l'utilisateur :
  - la reprise exacte de la révision en cours sans dupliquer les notes INSERT ;
  - l'annulation complète avec restauration de l'état initial des notes ;
  - ou l'ignorance (conservation de l'état actuel en base).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .savepoint import NoteDiff

try:
    from PyQt6 import QtCore
except ImportError:
    try:
        from PySide6 import QtCore
    except ImportError:
        QtCore = None  # type: ignore


RECOVERY_VERSION = 1
RECOVERY_FILENAME = "diff_hunk_pending_session.json"

_save_timer: Any = None
_save_timer_kind: str = ""  # "qt" | "thread" | ""
_save_timer_lock = threading.Lock()


def get_recovery_file_path(profile_folder: Optional[str] = None) -> Optional[str]:
    """
    Retourne le chemin du fichier de reprise dans le dossier du profil Anki actif.

    Ne retombe JAMAIS sur le répertoire personnel (~) afin de ne jamais polluer
    l'environnement lors des tests unitaires ou si aucun profil n'est ouvert.
    """
    if isinstance(profile_folder, str) and profile_folder:
        return os.path.join(profile_folder, RECOVERY_FILENAME)

    try:
        import aqt
        mw = getattr(aqt, "mw", None)
    except Exception:
        mw = None

    if mw is None:
        return None

    base: Optional[str] = None
    try:
        pm = getattr(mw, "pm", None)
        if pm is not None and hasattr(pm, "profileFolder"):
            candidate = pm.profileFolder()
            if isinstance(candidate, str) and candidate and os.path.isdir(candidate):
                base = candidate
    except Exception:
        base = None

    if base is None:
        try:
            col = getattr(mw, "col", None)
            col_path = getattr(col, "path", None) if col is not None else None
            if isinstance(col_path, str) and col_path:
                candidate = os.path.dirname(col_path)
                if candidate and os.path.isdir(candidate):
                    base = candidate
        except Exception:
            base = None

    if base is None:
        return None

    return os.path.join(base, RECOVERY_FILENAME)


def serialize_note_diff(diff: NoteDiff) -> Dict[str, Any]:
    """Sérialise un objet NoteDiff vers un dictionnaire JSON-compatible."""
    data: Dict[str, Any] = {
        "note_id": int(diff.note_id),
        "original_fields": dict(diff.original_fields or {}),
        "updated_fields": dict(diff.updated_fields or {}),
        "model_name": str(diff.model_name or ""),
        "field_names": list(diff.field_names or []),
        "action": str(diff.action or "UPDATE"),
        "tags": list(diff.tags or []),
        "original_tags": list(diff.original_tags or []),
        "updated_tags": list(diff.updated_tags or []),
        "merged_fields": dict(diff.merged_fields or {}),
        "custom_edit_fields": dict(diff.custom_edit_fields or {}),
        "old_mod_fields": dict(diff.old_mod_fields or {}),
        "prop_mod_fields": dict(diff.prop_mod_fields or {}),
        "field_active_target": dict(diff.field_active_target or {}),
        "tags_active_target": str(diff.tags_active_target or "PROP"),
        "excluded": bool(diff.excluded),
        "model_id": int(diff.model_id or 0),
        "deck_id": int(diff.deck_id or 0),
        "deck_name": str(diff.deck_name or ""),
        "suspend_cards": bool(diff.suspend_cards),
        "staged_note_id": int(diff.staged_note_id or 0),
        "guid": str(diff.guid or ""),
    }
    db_synced = getattr(diff, "_db_synced_fields", None)
    if isinstance(db_synced, dict):
        data["_db_synced_fields"] = dict(db_synced)
    return data


def deserialize_note_diff(data: Dict[str, Any]) -> NoteDiff:
    """Reconstruit fidèlement un NoteDiff depuis son dictionnaire sérialisé."""
    diff = NoteDiff(
        note_id=int(data["note_id"]),
        original_fields=dict(data.get("original_fields") or {}),
        updated_fields=dict(data.get("updated_fields") or {}),
        model_name=str(data.get("model_name") or ""),
        field_names=list(data.get("field_names") or []),
        action=str(data.get("action") or "UPDATE"),
        tags=list(data.get("tags") or []),
        original_tags=list(data.get("original_tags") or []),
        updated_tags=list(data.get("updated_tags") or []),
        merged_fields=dict(data.get("merged_fields") or {}),
        custom_edit_fields=dict(data.get("custom_edit_fields") or {}),
        old_mod_fields=dict(data.get("old_mod_fields") or {}),
        prop_mod_fields=dict(data.get("prop_mod_fields") or {}),
        field_active_target=dict(data.get("field_active_target") or {}),
        tags_active_target=str(data.get("tags_active_target") or "PROP"),
        excluded=bool(data.get("excluded", False)),
        model_id=int(data.get("model_id") or 0),
        deck_id=int(data.get("deck_id") or 0),
        deck_name=str(data.get("deck_name") or ""),
        suspend_cards=bool(data.get("suspend_cards", False)),
        staged_note_id=int(data.get("staged_note_id") or 0),
        guid=str(data.get("guid") or ""),
    )
    # Préserver les listes/dicts explicitement vides que __post_init__ aurait remplis
    if "tags" in data:
        diff.tags = list(data["tags"] or [])
    if "updated_tags" in data:
        diff.updated_tags = list(data["updated_tags"] or [])
    if "merged_fields" in data:
        diff.merged_fields = dict(data["merged_fields"] or {})
    if "old_mod_fields" in data:
        diff.old_mod_fields = dict(data["old_mod_fields"] or {})
    if "prop_mod_fields" in data:
        diff.prop_mod_fields = dict(data["prop_mod_fields"] or {})
    if "field_active_target" in data:
        diff.field_active_target = dict(data["field_active_target"] or {})
    if isinstance(data.get("_db_synced_fields"), dict):
        setattr(diff, "_db_synced_fields", dict(data["_db_synced_fields"]))
    return diff


def save_pending_session(
    diffs: Dict[int, NoteDiff],
    staged_to_orig: Dict[int, int],
    is_collab: Optional[bool],
    path: Optional[str] = None,
    auto_resume: bool = False,
) -> bool:
    """
    Sauvegarde de façon atomique l'état de la session diff en cours.
    Si `diffs` est vide, supprime le fichier de session existant.
    """
    resolved_path = path or get_recovery_file_path()
    if not resolved_path:
        return False

    if not diffs:
        clear_saved_session(resolved_path)
        return True

    payload = {
        "version": RECOVERY_VERSION,
        "saved_at": time.time(),
        "is_collab": bool(is_collab) if is_collab is not None else False,
        "auto_resume": bool(auto_resume),
        "staged_to_orig": {str(k): int(v) for k, v in (staged_to_orig or {}).items()},
        "diffs": [serialize_note_diff(d) for d in diffs.values()],
    }

    tmp_path = resolved_path + ".tmp"
    try:
        parent = os.path.dirname(resolved_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, resolved_path)
        return True
    except Exception as e:
        print(f"[anki-diff] Error saving recovery session: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def load_saved_session(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Charge le fichier de session interrompue s'il existe et est valide.
    Retourne None si absent, vide, incompatible ou corrompu.
    """
    resolved_path = path or get_recovery_file_path()
    if not resolved_path or not os.path.isfile(resolved_path):
        return None

    try:
        with open(resolved_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[anki-diff] Error reading recovery session file: {e}")
        return None

    if not isinstance(data, dict):
        return None
    if data.get("version") != RECOVERY_VERSION:
        return None

    raw_diffs = data.get("diffs")
    if not isinstance(raw_diffs, list) or not raw_diffs:
        return None

    try:
        diff_list: List[NoteDiff] = [deserialize_note_diff(item) for item in raw_diffs if isinstance(item, dict)]
    except Exception as e:
        print(f"[anki-diff] Error deserializing recovery diffs: {e}")
        return None

    if not diff_list:
        return None

    staged_map: Dict[int, int] = {}
    raw_staged = data.get("staged_to_orig")
    if isinstance(raw_staged, dict):
        for k, v in raw_staged.items():
            try:
                staged_map[int(k)] = int(v)
            except (TypeError, ValueError):
                continue

    return {
        "version": RECOVERY_VERSION,
        "saved_at": data.get("saved_at", 0),
        "is_collab": bool(data.get("is_collab", False)),
        "auto_resume": bool(data.get("auto_resume", False)),
        "staged_to_orig": staged_map,
        "diffs": diff_list,
    }


def _stop_timer_unlocked() -> None:
    global _save_timer, _save_timer_kind
    timer = _save_timer
    kind = _save_timer_kind
    _save_timer = None
    _save_timer_kind = ""
    if timer is None:
        return
    try:
        if kind == "qt":
            try:
                from aqt.qt import sip
                if sip.isdeleted(timer):
                    return
            except Exception:
                pass
            timer.stop()
            timer.deleteLater()
        elif kind == "thread":
            timer.cancel()
    except Exception:
        pass


def cancel_scheduled_save() -> None:
    """Annule toute sauvegarde débouncée en attente."""
    with _save_timer_lock:
        _stop_timer_unlocked()


def schedule_session_save(save_fn: Callable[[], None], delay_ms: int = 400) -> None:
    """Programme une sauvegarde différée (debounced) sur le thread Qt ou via threading.Timer."""
    with _save_timer_lock:
        _stop_timer_unlocked()

        use_qt = False
        app = None
        if QtCore is not None:
            try:
                app = QtCore.QCoreApplication.instance()
                if app is not None and QtCore.QThread.currentThread() == app.thread():
                    use_qt = True
            except Exception:
                use_qt = False

        if use_qt and QtCore is not None:
            global _save_timer, _save_timer_kind
            timer = QtCore.QTimer(app)
            timer.setSingleShot(True)

            def _on_timeout():
                with _save_timer_lock:
                    global _save_timer, _save_timer_kind
                    if _save_timer is timer:
                        _save_timer = None
                        _save_timer_kind = ""
                try:
                    save_fn()
                finally:
                    try:
                        timer.deleteLater()
                    except Exception:
                        pass

            timer.timeout.connect(_on_timeout)
            _save_timer = timer
            _save_timer_kind = "qt"
            timer.start(max(0, int(delay_ms)))
        else:
            t = threading.Timer(max(0.0, delay_ms / 1000.0), save_fn)
            t.daemon = True
            _save_timer = t
            _save_timer_kind = "thread"
            t.start()


def clear_saved_session(path: Optional[str] = None) -> None:
    """Annule tout timer de sauvegarde et supprime le fichier de reprise."""
    cancel_scheduled_save()
    resolved_path = path or get_recovery_file_path()
    if not resolved_path:
        return
    for candidate in (resolved_path, resolved_path + ".tmp"):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass
        except OSError:
            pass

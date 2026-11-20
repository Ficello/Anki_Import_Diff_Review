"""
editor_sync.py — Synchronisation bidirectionnelle entre l'éditeur natif Anki et les hunks de diff.

Deux responsabilités :
  1. sync_editor_from_hunks(editor, diff) — Injecte les valeurs mergées (selon les hunks acceptés)
     dans les champs de l'éditeur natif, via JavaScript, sans rechargement complet de la note.
  2. hook editor_did_unfocus_field — Capture les modifications manuelles de l'utilisateur et les
     stocke dans diff.merged_fields, en marquant le champ comme "custom edited".
"""
from __future__ import annotations

import json
import time
import html as _html
from typing import TYPE_CHECKING, Optional
from .diff import normalize_html_tags

# Mettre à True pour activer les logs verbeux de sync (un message par champ et par callback JS)
_DEBUG_SYNC = False

if TYPE_CHECKING:
    from .savepoint import NoteDiff

try:
    from aqt.editor import Editor  # type: ignore
except ImportError:
    Editor = None  # tests sans Anki

try:
    import aqt
except ImportError:
    aqt = None


def _get_editor_note(editor):
    """Extrait la note de l'editeur de facon securisee (Editor ou NewEditor)."""
    if not editor:
        return None
    note = getattr(editor, "note", None)
    if note is not None:
        return note
    nid = getattr(editor, "nid", None)
    if nid and aqt and getattr(aqt, "mw", None) and aqt.mw.col:
        try:
            return aqt.mw.col.get_note(nid)
        except Exception:
            pass
    return None

# ---------------------------------------------------------------------------
# JS template — mise à jour d'un champ sans rechargement complet
# ---------------------------------------------------------------------------

_SET_FIELD_JS = """
(function(fieldIdx, htmlValue) {
    try {
        // Tentative 1 : NoteEditor moderne (Anki Svelte / ProseMirror >= 2.1.55 / 24+ / 25+ / 26+)
        try {
            var ni = (typeof require === "function") ? require("anki/NoteEditor").instances : null;
            if (ni && ni[0] && ni[0].fields && ni[0].fields[fieldIdx]) {
                var f = ni[0].fields[fieldIdx];
                if (f.content && typeof f.content.set === "function") {
                    f.content.set(htmlValue);
                    return "ok:NoteEditor.field.content";
                }
                var area = f.editingArea;
                if (area && area.content && typeof area.content.set === "function") {
                    area.content.set(htmlValue);
                    return "ok:NoteEditor.area.content";
                }
                var ce = (area && (area.element || area.$el)) || f.element;
                if (ce) {
                    ce.innerHTML = htmlValue;
                    ce.dispatchEvent(new Event("input", { bubbles: true }));
                    return "ok:NoteEditor.element";
                }
            }
        } catch(e) { console.log("anki-diff NoteEditor err:", e); }

        // Tentative 2 : anki-editable avec support Shadow DOM (Web Components)
        try {
            var containers = document.querySelectorAll('.field-container, .rich-text-input, .rich-text-relative, [role="presentation"]');
            var editables = [];
            containers.forEach(function(cont) {
                var sr = cont.shadowRoot || cont;
                var el = sr.querySelector('anki-editable') || sr.querySelector('[contenteditable="true"]');
                if (el && editables.indexOf(el) === -1) {
                    editables.push(el);
                }
            });
            if (editables.length === 0) {
                editables = Array.from(document.querySelectorAll('anki-editable'));
            }
            if (editables && editables.length > fieldIdx) {
                var target = editables[fieldIdx];
                var sr = target.shadowRoot;
                var editableEl = (sr ? sr.querySelector('[contenteditable="true"]') : null)
                              || target.querySelector('[contenteditable="true"]')
                              || (sr ? sr.firstElementChild : null)
                              || target;
                if (editableEl) {
                    editableEl.innerHTML = htmlValue;
                    editableEl.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
                    target.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
                    return "ok:anki-editable";
                }
            }
        } catch(e) {}

        // Tentative 3 : rich-text-editable (Anki 2.1.50-2.1.54)
        try {
            var rte = document.querySelectorAll('.rich-text-editable');
            if (rte && rte.length > fieldIdx) {
                rte[fieldIdx].innerHTML = htmlValue;
                rte[fieldIdx].dispatchEvent(new Event("input", { bubbles: true }));
                return "ok:rich-text-editable";
            }
        } catch(e) {}

        // Tentative 4 : tout div[contenteditable] dans les champs
        try {
            var ces = Array.from(document.querySelectorAll('[contenteditable="true"]')).filter(function(el) {
                return el.tagName !== "INPUT" && el.tagName !== "TEXTAREA";
            });
            if (ces.length > fieldIdx) {
                ces[fieldIdx].innerHTML = htmlValue;
                ces[fieldIdx].dispatchEvent(new Event("input", { bubbles: true }));
                return "ok:contenteditable-fallback";
            }
        } catch(e) {}

        return "error:not_found";
    } catch(err) {
        return "error:" + err;
    }
})(%d, %s);
"""

_SET_FIELDS_BATCH_JS = """
(function(fieldsMap) {
    var results = {};

    function updateSingleField(fieldIdx, htmlValue) {
        // Tentative 1 : NoteEditor moderne (Anki Svelte / ProseMirror >= 2.1.55 / 24+ / 25+ / 26+)
        try {
            var ni = (typeof require === "function") ? require("anki/NoteEditor").instances : null;
            if (ni && ni[0] && ni[0].fields && ni[0].fields[fieldIdx]) {
                var f = ni[0].fields[fieldIdx];
                // 1a : f.content est le Svelte store direct
                if (f.content && typeof f.content.set === "function") {
                    f.content.set(htmlValue);
                    return "ok:NoteEditor.field.content";
                }
                // 1b : f.editingArea.content
                var area = f.editingArea;
                if (area && area.content && typeof area.content.set === "function") {
                    area.content.set(htmlValue);
                    return "ok:NoteEditor.area.content";
                }
                // 1c : area.element ou f.element
                var ce = (area && (area.element || area.$el)) || f.element;
                if (ce) {
                    ce.innerHTML = htmlValue;
                    ce.dispatchEvent(new Event("input", { bubbles: true }));
                    return "ok:NoteEditor.element";
                }
            }
        } catch(e) {
            console.log("anki-diff NoteEditor err:", e);
        }

        // Tentative 2 : anki-editable avec support Shadow DOM (Web Components)
        try {
            var containers = document.querySelectorAll('.field-container, .rich-text-input, .rich-text-relative, [role="presentation"]');
            var editables = [];
            containers.forEach(function(cont) {
                var sr = cont.shadowRoot || cont;
                var el = sr.querySelector('anki-editable') || sr.querySelector('[contenteditable="true"]');
                if (el && editables.indexOf(el) === -1) {
                    editables.push(el);
                }
            });
            if (editables.length === 0) {
                editables = Array.from(document.querySelectorAll('anki-editable'));
            }
            if (editables && editables.length > fieldIdx) {
                var target = editables[fieldIdx];
                var sr = target.shadowRoot;
                var editableEl = (sr ? sr.querySelector('[contenteditable="true"]') : null)
                              || target.querySelector('[contenteditable="true"]')
                              || (sr ? sr.firstElementChild : null)
                              || target;
                if (editableEl) {
                    editableEl.innerHTML = htmlValue;
                    editableEl.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
                    target.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
                    return "ok:anki-editable";
                }
            }
        } catch(e) {}

        // Tentative 3 : rich-text-editable (Anki 2.1.50-2.1.54)
        try {
            var rte = document.querySelectorAll('.rich-text-editable');
            if (rte && rte.length > fieldIdx) {
                rte[fieldIdx].innerHTML = htmlValue;
                rte[fieldIdx].dispatchEvent(new Event("input", { bubbles: true }));
                return "ok:rich-text-editable";
            }
        } catch(e) {}

        // Tentative 4 : tout div[contenteditable] dans les champs
        try {
            var ces = Array.from(document.querySelectorAll('[contenteditable="true"]')).filter(function(el) {
                return el.tagName !== "INPUT" && el.tagName !== "TEXTAREA";
            });
            if (ces.length > fieldIdx) {
                ces[fieldIdx].innerHTML = htmlValue;
                ces[fieldIdx].dispatchEvent(new Event("input", { bubbles: true }));
                return "ok:contenteditable-fallback";
            }
        } catch(e) {}

        return "error:not_found";
    }

    for (var idxStr in fieldsMap) {
        if (!Object.prototype.hasOwnProperty.call(fieldsMap, idxStr)) continue;
        var fieldIdx = parseInt(idxStr, 10);
        var htmlValue = fieldsMap[idxStr];
        results[fieldIdx] = updateSingleField(fieldIdx, htmlValue);
    }

    // Tentative de secours globale : globalThis.setFields si un champ a échoué
    try {
        var hasFailure = false;
        for (var k in results) {
            if (typeof results[k] === "string" && results[k].indexOf("error") !== -1) {
                hasFailure = true;
                break;
            }
        }
        if (hasFailure && typeof globalThis.setFields === "function") {
            var ni = (typeof require === "function") ? require("anki/NoteEditor").instances : null;
            if (ni && ni[0] && ni[0].fields) {
                var fCount = Object.keys(ni[0].fields).length;
                var allNames = [];
                var allVals = [];
                var allData = [];
                for (var i = 0; i < fCount; i++) {
                    var fn = (ni[0].fields[i] && ni[0].fields[i].name) || ("f" + i);
                    var fv = (fieldsMap[i] !== undefined) ? fieldsMap[i] : null;
                    if (fv === null && ni[0].fields[i] && ni[0].fields[i].content) {
                        try {
                            fv = ni[0].fields[i].content.get ? ni[0].fields[i].content.get() : "";
                        } catch(e2) { fv = ""; }
                    }
                    var finalVal = fv !== null ? fv : "";
                    allNames.push(fn);
                    allVals.push(finalVal);
                    allData.push([fn, finalVal]);
                }
                var sfArity = (typeof globalThis._ednSetFieldsArity === "number")
                    ? globalThis._ednSetFieldsArity
                    : globalThis.setFields.length;
                if (sfArity === 1) {
                    globalThis.setFields(allData);
                } else {
                    globalThis.setFields(allNames, allVals);
                }
            }
        }
    } catch(eG) {}

    // Maintenir le focus sur le champ actif si un champ était sélectionné
    try {
        var ni2 = (typeof require === "function") ? require("anki/NoteEditor").instances : null;
        var cf = (ni2 && ni2[0] && ni2[0].focusedField) ? (ni2[0].focusedField.get ? ni2[0].focusedField.get() : ni2[0].focusedField) : null;
        if (typeof cf === "number" && typeof globalThis.focusField === "function") {
            globalThis.focusField(cf);
        }
    } catch(eF) {}

    return results;
})(%s);
"""

_HIGHLIGHT_MODIFIED_FIELDS_JS = """
(function(modifiedIndices) {
    function getFieldContainers() {
        var fcs = document.querySelectorAll('.field-container');
        if (fcs && fcs.length > 0) {
            return Array.from(fcs);
        }
        var aes = document.querySelectorAll('anki-editable');
        if (aes && aes.length > 0) {
            return Array.from(aes);
        }
        var richFields = document.querySelectorAll('.field .rich-text-editable');
        if (richFields && richFields.length > 0) {
            return Array.from(richFields);
        }
        return Array.from(document.querySelectorAll('.field, [contenteditable="true"]'));
    }

    function apply() {
        var fields = getFieldContainers();
        fields.forEach(function(el, idx) {
            el.style.outline = '';
            el.style.background = '';
            var container = el.closest ? (el.closest('.field-container') || el.closest('.field') || el.parentElement) : el.parentElement;
            if (container) {
                var badge = container.querySelector('.anki-diff-badge');
                if (badge) badge.remove();
                container.style.position = '';
            }

            if (modifiedIndices.indexOf(idx) === -1) return;

            el.style.outline = '2px solid #ffc107';
            el.style.background = 'rgba(255,193,7,0.06)';
            el.style.transition = 'background 0.3s, outline 0.3s';

            if (container) {
                container.style.position = 'relative';
                var badge = document.createElement('div');
                badge.className = 'anki-diff-badge';
                badge.textContent = 'DIFF';
                badge.style.cssText = [
                    'position:absolute', 'top:4px', 'right:8px',
                    'font-size:9px', 'font-weight:700',
                    'padding:1px 6px', 'border-radius:8px',
                    'pointer-events:none', 'z-index:99',
                    'font-family:monospace', 'letter-spacing:0.5px',
                    'background:#ffc107', 'color:#000'
                ].join(';');
                container.appendChild(badge);
            }
        });
    }

    if (window._anki_diff_highlight_iv) {
        clearInterval(window._anki_diff_highlight_iv);
        window._anki_diff_highlight_iv = null;
    }

    var ok = (document.querySelectorAll('.field-container, anki-editable, .field').length > 0);
    if (ok) {
        apply();
    } else {
        var tries = 0;
        window._anki_diff_highlight_iv = setInterval(function() {
            var ready = (document.querySelectorAll('.field-container, anki-editable, .field').length > 0);
            if (ready || tries++ > 15) {
                clearInterval(window._anki_diff_highlight_iv);
                window._anki_diff_highlight_iv = null;
                if (ready) apply();
            }
        }, 50);
    }
})(%s);
"""

_CLEAR_FIELD_HIGHLIGHTS_JS = """
(function() {
    var els = document.querySelectorAll('.field-container, anki-editable, .field .rich-text-editable, [contenteditable="true"]');
    els.forEach(function(el) {
        el.style.outline = '';
        el.style.background = '';
        var container = el.closest ? (el.closest('.field-container') || el.closest('.field') || el.parentElement) : el.parentElement;
        if (container) {
            var badge = container.querySelector('.anki-diff-badge');
            if (badge) badge.remove();
        }
    });
})();
"""

_ACCEPT_FIELD_JS = """
(function(fieldIdx) {
    function getFieldContainers() {
        var fcs = document.querySelectorAll('.field-container');
        if (fcs && fcs.length > 0) return Array.from(fcs);
        var aes = document.querySelectorAll('anki-editable');
        if (aes && aes.length > 0) return Array.from(aes);
        var richFields = document.querySelectorAll('.field .rich-text-editable');
        if (richFields && richFields.length > 0) return Array.from(richFields);
        return Array.from(document.querySelectorAll('.field, [contenteditable="true"]'));
    }
    var els = getFieldContainers();
    if (els && els[fieldIdx]) {
        var el = els[fieldIdx];
        el.style.outline = '2px solid #28a745';
        el.style.background = 'rgba(40,167,69,0.08)';
        var container = el.closest ? (el.closest('.field-container') || el.closest('.field') || el.parentElement) : el.parentElement;
        if (container) {
            var badge = container.querySelector('.anki-diff-badge');
            if (badge) {
                badge.textContent = 'ACCEPTED';
                badge.style.background = '#28a745';
                badge.style.color = '#fff';
            }
        }
    }
})(%d);
"""

_REJECT_FIELD_JS = """
(function(fieldIdx) {
    function getFieldContainers() {
        var fcs = document.querySelectorAll('.field-container');
        if (fcs && fcs.length > 0) return Array.from(fcs);
        var aes = document.querySelectorAll('anki-editable');
        if (aes && aes.length > 0) return Array.from(aes);
        var richFields = document.querySelectorAll('.field .rich-text-editable');
        if (richFields && richFields.length > 0) return Array.from(richFields);
        return Array.from(document.querySelectorAll('.field, [contenteditable="true"]'));
    }
    var els = getFieldContainers();
    if (els && els[fieldIdx]) {
        var el = els[fieldIdx];
        el.style.outline = '2px solid #dc3545';
        el.style.background = 'rgba(220,53,69,0.06)';
        var container = el.closest ? (el.closest('.field-container') || el.closest('.field') || el.parentElement) : el.parentElement;
        if (container) {
            var badge = container.querySelector('.anki-diff-badge');
            if (badge) {
                badge.textContent = 'REJECTED';
                badge.style.background = '#dc3545';
                badge.style.color = '#fff';
            }
        }
    }
})(%d);
"""


# ---------------------------------------------------------------------------
# Synchronisation hunks → éditeur
# ---------------------------------------------------------------------------

_syncing_tags_depth: int = 0


def is_syncing_tags() -> bool:
    """Retourne True pendant qu'une synchronisation programmatique des tags est en cours."""
    return _syncing_tags_depth > 0


def sync_tags_to_editor(editor, tags: list[str]) -> None:
    """
    Synchronise les tags avec l'editeur Anki de facon exhaustive :
      1. Met a jour note.tags en memoire (ed_note et editor.note)
      2. Met a jour le widget Qt TagEdit (editor.tags.setText)
      3. Appelle editor.updateTags() si disponible
      4. Evalue setTags(...) dans le webview JS (Svelte Anki)
      5. Notifie gui_hooks.editor_did_update_tags si disponible
    """
    global _syncing_tags_depth
    if editor is None:
        return

    _syncing_tags_depth += 1
    try:
        tags_list = list(tags) if tags is not None else []
        ed_note = _get_editor_note(editor)
        if ed_note is not None:
            ed_note.tags = list(tags_list)
        note = getattr(editor, "note", None)
        if note is not None and note is not ed_note:
            note.tags = list(tags_list)

        target_note = ed_note or note
        tag_str = ""
        if target_note and hasattr(target_note, "string_tags"):
            try:
                tag_str = target_note.string_tags().strip()
            except Exception:
                tag_str = " ".join(tags_list)
        else:
            tag_str = " ".join(tags_list)

        # 1. Widget Qt TagEdit (QLineEdit)
        if hasattr(editor, "tags") and editor.tags is not None:
            try:
                if hasattr(editor.tags, "setText"):
                    editor.tags.setText(tag_str)
            except Exception:
                pass

        # 2. Methode native Anki updateTags
        if hasattr(editor, "updateTags"):
            try:
                editor.updateTags()
            except Exception:
                pass

        # 3. Webview JavaScript (Anki moderne Svelte / NoteEditor)
        if hasattr(editor, "web") and editor.web is not None:
            try:
                js = f"""
                (function(tags) {{
                    try {{
                        if (typeof setTags === 'function') {{
                            setTags(tags);
                        }}
                    }} catch(e) {{}}
                }})({json.dumps(tags_list)});
                """
                _eval_js(editor, js)
            except Exception:
                pass

        # 4. Hook Anki
        try:
            from aqt import gui_hooks
            if target_note is not None and hasattr(gui_hooks, "editor_did_update_tags"):
                gui_hooks.editor_did_update_tags(target_note)
        except Exception:
            pass
    finally:
        _syncing_tags_depth = max(0, _syncing_tags_depth - 1)


def record_in_flight_field_sync(
    diff: "NoteDiff",
    field_name: str,
    previous_val: str,
    expected_val: str,
    ttl: float = 1.5,
) -> None:
    """Enregistre une synchronisation JS en vol pour filtrer les événements blur/key périmés."""
    in_flight = getattr(diff, "_in_flight_sync_fields", None)
    if not isinstance(in_flight, dict):
        in_flight = {}
        setattr(diff, "_in_flight_sync_fields", in_flight)

    now = time.monotonic()
    existing = in_flight.get(field_name)
    if existing and now <= existing.get("deadline", 0.0):
        prev_to_keep = existing["previous"]
    else:
        if normalize_html_tags(previous_val) == normalize_html_tags(expected_val):
            return
        prev_to_keep = previous_val

    in_flight[field_name] = {
        "previous": prev_to_keep,
        "expected": expected_val,
        "js_done": False,
        "deadline": now + ttl,
    }


def should_drop_stale_editor_bridge_cmd(
    editor,
    cmd: str,
    get_diff_for_note,
) -> bool:
    """
    Détermine si une commande bridge `blur:` ou `key:` porte un état périmé
    antérieur à une synchronisation programmatique (rejet/acceptation/chargement).
    """
    if not cmd or not (cmd.startswith("blur:") or cmd.startswith("key:")):
        return False

    parts = cmd.split(":", 3)
    if len(parts) != 4:
        return False

    _, ord_str, nid_str, txt = parts
    try:
        ord_idx = int(ord_str)
        nid = int(nid_str)
    except ValueError:
        return False

    diff = get_diff_for_note(nid)
    if diff is None or ord_idx < 0 or ord_idx >= len(diff.field_names):
        return False

    in_flight = getattr(diff, "_in_flight_sync_fields", None)
    if not isinstance(in_flight, dict):
        return False

    field_name = diff.field_names[ord_idx]
    entry = in_flight.get(field_name)
    if not entry:
        return False

    if time.monotonic() > entry.get("deadline", 0.0):
        in_flight.pop(field_name, None)
        return False

    munged = txt
    if editor is not None and hasattr(editor, "mungeHTML"):
        try:
            res = editor.mungeHTML(txt)
            if isinstance(res, str):
                munged = res
        except Exception:
            pass

    norm_txt = normalize_html_tags(munged)
    norm_expected = normalize_html_tags(entry.get("expected", ""))
    norm_prev = normalize_html_tags(entry.get("previous", ""))

    if norm_txt == norm_expected:
        return False
    if not entry.get("js_done", False):
        return True
    if norm_prev != norm_expected and norm_txt == norm_prev:
        return True
    return False


def _escape_for_editor_js(editor, html_val: str) -> str:
    """Applique escape_media_filenames comme Anki loadNote si disponible et retourne une str."""
    try:
        col = getattr(editor, "mw", None) and getattr(editor.mw, "col", None)
        if col and getattr(col, "media", None) and hasattr(col.media, "escape_media_filenames"):
            escaped = col.media.escape_media_filenames(html_val)
            if isinstance(escaped, str):
                return escaped
    except Exception:
        pass
    return html_val


def _make_mark_js_done_cb(diff: "NoteDiff", expected_snapshot: dict[str, str]):
    def _mark_js_done() -> None:
        in_flight = getattr(diff, "_in_flight_sync_fields", None)
        if not isinstance(in_flight, dict):
            return
        now_t = time.monotonic()
        for fname, exp_val in expected_snapshot.items():
            entry = in_flight.get(fname)
            if entry and entry.get("expected") == exp_val:
                entry["js_done"] = True
                entry["deadline"] = now_t + 0.15
    return _mark_js_done


def record_db_synced_fields(diff: "NoteDiff", note) -> None:
    """Mémorise la dernière valeur de chaque champ écrite en SQLite par la session diff."""
    if diff is None or note is None:
        return
    fields = getattr(note, "fields", None)
    if not isinstance(fields, (list, tuple)):
        return
    db_synced = getattr(diff, "_db_synced_fields", None)
    if not isinstance(db_synced, dict):
        db_synced = {}
        setattr(diff, "_db_synced_fields", db_synced)
    for idx, fname in enumerate(diff.field_names):
        if idx < len(fields) and isinstance(fields[idx], str):
            db_synced[fname] = fields[idx]


def sync_editor_from_hunks(
    editor,
    diff: "NoteDiff",
    only_fields: Optional[set[str]] = None,
    sync_tags: bool = True,
) -> None:
    """
    Injecte les valeurs mergées (selon l'état courant des hunks) dans les champs
    de l'éditeur natif via JavaScript, sans rechargement complet de la note.

    Met à jour simultanément tous les champs modifiés (batch) pour garantir
    que le recto ET le verso s'actualisent toujours ensemble sans être ignorés.

    Si `only_fields` est fourni, seuls ces champs sont synchronisés dans l'éditeur.
    Si `sync_tags` est False, les tags de l'éditeur ne sont pas modifiés.
    """
    import sys
    if editor is None or not hasattr(editor, "web"):
        return

    note = _get_editor_note(editor)
    if note is None:
        note = getattr(editor, "note", None)

    # Sécurité : ne jamais écrire dans une note de l'éditeur qui ne correspond pas à ce diff
    note_id = getattr(note, "id", None) if note is not None else None
    if note_id and getattr(diff, "note_id", None):
        valid_ids = {diff.note_id}
        staged_id = getattr(diff, "staged_note_id", 0)
        if staged_id:
            valid_ids.add(staged_id)
        if note_id not in valid_ids:
            if _DEBUG_SYNC:
                print(f"[anki-diff] sync_editor_from_hunks skipped: editor note={note_id} != diff.note_id={diff.note_id}")
            return

    diff._syncing_to_editor = True
    diff._last_sync_time = time.monotonic()
    try:
        if _DEBUG_SYNC:
            print(f"[anki-diff] sync_editor_from_hunks: editor ok, note={getattr(note,'id',None)}, diff.note_id={diff.note_id}")

        if diff.excluded:
            if getattr(diff, "action", "") != "INSERT":
                fields_to_sync = {}
                expected_snapshot = {}
                for idx, field_name in enumerate(diff.field_names):
                    if only_fields is not None and field_name not in only_fields:
                        continue
                    old_val = diff.original_fields.get(field_name, "")
                    has_real_note_field = bool(
                        note
                        and isinstance(getattr(note, "fields", None), (list, tuple))
                        and idx < len(note.fields)
                        and isinstance(note.fields[idx], str)
                    )
                    if has_real_note_field:
                        record_in_flight_field_sync(diff, field_name, note.fields[idx], old_val)
                    fields_to_sync[idx] = _escape_for_editor_js(editor, old_val)
                    expected_snapshot[field_name] = old_val
                    if note and hasattr(note, "fields") and idx < len(note.fields):
                        note.fields[idx] = old_val
                if fields_to_sync:
                    js = _SET_FIELDS_BATCH_JS % json.dumps(fields_to_sync)
                    _eval_js(editor, js, on_complete=_make_mark_js_done_cb(diff, expected_snapshot))
                if only_fields is None:
                    clear_field_highlights(editor)
                if sync_tags:
                    tags_to_sync = diff.original_tags if diff.original_tags else diff.tags
                    sync_tags_to_editor(editor, tags_to_sync)
            return

        if only_fields is None:
            # Index des champs modifiés (pour le surlignage)
            modified_indices = []
            for idx, field_name in enumerate(diff.field_names):
                old_val = diff.original_fields.get(field_name, "")
                new_val = diff.updated_fields.get(field_name, "")
                if normalize_html_tags(old_val) != normalize_html_tags(new_val):
                    modified_indices.append(idx)

            # D'abord, surligner les champs modifiés (jaune)
            highlight_js = _HIGHLIGHT_MODIFIED_FIELDS_JS % json.dumps(modified_indices)
            _eval_js(editor, highlight_js)

        # Collecter tous les champs modifiés à synchroniser en un seul appel JS batch
        fields_to_sync = {}
        expected_snapshot = {}
        in_flight_map = getattr(diff, "_in_flight_sync_fields", None)
        for idx, field_name in enumerate(diff.field_names):
            if only_fields is not None and field_name not in only_fields:
                continue

            old_val = diff.original_fields.get(field_name, "")
            new_val = diff.updated_fields.get(field_name, "")
            merged_val = diff.merged_fields.get(field_name, "")

            has_real_note_field = bool(
                note
                and isinstance(getattr(note, "fields", None), (list, tuple))
                and idx < len(note.fields)
                and isinstance(note.fields[idx], str)
            )
            current_note_val = note.fields[idx] if has_real_note_field else None

            has_diff = (normalize_html_tags(old_val) != normalize_html_tags(new_val))
            is_custom = field_name in diff.custom_edit_fields
            if only_fields is None and is_custom:
                if not has_real_note_field or normalize_html_tags(current_note_val) == normalize_html_tags(merged_val):
                    continue

            if only_fields is None and not has_diff and not is_custom:
                if (
                    normalize_html_tags(merged_val) == normalize_html_tags(old_val)
                    and (
                        not has_real_note_field
                        or normalize_html_tags(current_note_val) == normalize_html_tags(merged_val)
                    )
                ):
                    continue

            has_in_flight = isinstance(in_flight_map, dict) and field_name in in_flight_map
            if has_real_note_field and (
                normalize_html_tags(current_note_val) != normalize_html_tags(merged_val)
                or has_in_flight
            ):
                prev_for_guard = current_note_val
                if has_in_flight:
                    existing_entry = in_flight_map.get(field_name)
                    if existing_entry and time.monotonic() <= existing_entry.get("deadline", 0.0):
                        prev_for_guard = existing_entry["previous"]
                record_in_flight_field_sync(diff, field_name, prev_for_guard, merged_val)
            elif (
                only_fields is None
                and has_diff
                and not is_custom
                and normalize_html_tags(old_val) != normalize_html_tags(merged_val)
            ):
                record_in_flight_field_sync(diff, field_name, old_val, merged_val)

            if note and hasattr(note, "fields") and idx < len(note.fields):
                note.fields[idx] = merged_val

            fields_to_sync[idx] = _escape_for_editor_js(editor, merged_val)
            expected_snapshot[field_name] = merged_val

        if fields_to_sync:
            js = _SET_FIELDS_BATCH_JS % json.dumps(fields_to_sync)
            _eval_js(editor, js, on_complete=_make_mark_js_done_cb(diff, expected_snapshot))

        # Synchroniser les tags dans l'éditeur si demandés
        if sync_tags:
            tags_to_sync = diff.tags if (hasattr(diff, "tags") and diff.tags is not None) else []
            sync_tags_to_editor(editor, tags_to_sync)

        # Mettre à jour la collection SQLite directement (synchrone 0.1ms, sans background Taskman spinner)
        if note and hasattr(note, "id") and note.id:
            try:
                col = getattr(editor, "mw", None) and getattr(editor.mw, "col", None)
                if col:
                    col.update_note(note)
                    record_db_synced_fields(diff, note)
            except Exception:
                pass

        # Rafraîchir l'aperçu de la carte (Card Previewer) du navigateur si ouvert
        try:
            parent_win = getattr(editor, "parentWindow", None)
            browser = parent_win if hasattr(parent_win, "_previewer") else None
            if browser and hasattr(browser, "_previewer") and browser._previewer:
                browser._previewer._last_state = None
                browser._renderPreview()
        except Exception:
            pass

    finally:
        pending_sync = getattr(diff, "_pending_sync_fields", None)
        if isinstance(pending_sync, set):
            if only_fields is None:
                pending_sync.clear()
            else:
                pending_sync.difference_update(only_fields)
        diff._synced_to_editor = True
        diff._syncing_to_editor = False
        diff._last_sync_time = time.monotonic()


def highlight_field_accepted(editor, field_idx: int) -> None:
    """Surligne un champ comme 'accepté' (vert) dans l'éditeur."""
    if editor and hasattr(editor, "web"):
        _eval_js(editor, _ACCEPT_FIELD_JS % field_idx)


def highlight_field_rejected(editor, field_idx: int) -> None:
    """Surligne un champ comme 'rejeté' (rouge) dans l'éditeur."""
    if editor and hasattr(editor, "web"):
        _eval_js(editor, _REJECT_FIELD_JS % field_idx)


def clear_field_highlights(editor) -> None:
    """Supprime tous les surlignages de champs dans l'éditeur."""
    if editor and hasattr(editor, "web"):
        _eval_js(editor, _CLEAR_FIELD_HIGHLIGHTS_JS)


def _eval_js(editor, js: str, on_complete=None) -> None:
    """Evalue du JS dans le webview de l'éditeur, avec logging du résultat."""
    def _finish():
        if on_complete is not None:
            try:
                on_complete()
            except Exception:
                pass

    try:
        # Logger le retour pour diagnostiquer les problèmes
        def _log_result(result):
            if result and 'error' in str(result):
                print(f"[anki-diff] JS sync failed: {result}")
            elif result and _DEBUG_SYNC:
                print(f"[anki-diff] JS sync ok: {result}")
            _finish()
        editor.web.evalWithCallback(js, _log_result)
    except AttributeError:
        # evalWithCallback pas dispo — fallback eval()
        try:
            editor.web.eval(js)
        except Exception:
            pass
        _finish()
    except Exception:
        _finish()


# ---------------------------------------------------------------------------
# Capture des modifications manuelles de l'utilisateur
# ---------------------------------------------------------------------------

def capture_editor_field_edits(note_or_editor, get_diff_for_note, on_field_edited_cb=None) -> bool:
    """
    Capture les modifications manuelles présentes dans `note.fields` pour une note en cours
    de revue de diff et les propage dans `diff.merged_fields`, `prop_mod_fields`/`old_mod_fields`
    et `custom_edit_fields`. Retourne True si au moins un champ a été mis à jour.
    """
    note = _get_editor_note(note_or_editor)
    if note is None:
        note = note_or_editor
    if note is None or not getattr(note, "id", None):
        return False

    diff = get_diff_for_note(note.id)
    if diff is None or getattr(diff, "_syncing_to_editor", False):
        return False

    fields = getattr(note, "fields", None)
    if not fields:
        return False

    pending_sync = getattr(diff, "_pending_sync_fields", None)
    if not isinstance(pending_sync, set):
        pending_sync = set()

    captured_any = False
    for idx, field_name in enumerate(diff.field_names):
        if field_name in pending_sync:
            continue
        if idx >= len(fields):
            continue
        new_val = fields[idx]
        if not isinstance(new_val, str):
            continue

        # Ignorer les états transitoires de "Lier les cartes" :
        # le marqueur edn-cursor-marker est inséré temporairement dans le DOM
        # pendant le traitement d'un collage de NID. Capturer cet état
        # contaminerait merged_fields et bloquerait la resync du champ.
        if new_val and "edn-cursor-marker" in new_val:
            continue

        # Ne jamais interpréter l'état DB pré-sync (original_fields) comme une édition utilisateur
        # avant que sync_editor_from_hunks n'ait synchronisé cette note dans l'éditeur.
        if (
            not getattr(diff, "_synced_to_editor", False)
            and normalize_html_tags(new_val) == normalize_html_tags(diff.original_fields.get(field_name, ""))
        ):
            continue

        target = getattr(diff, "field_active_target", {}).get(field_name, "PROP")
        curr_merged = diff.merged_fields.get(field_name, "")

        norm_new = normalize_html_tags(new_val)
        norm_curr = normalize_html_tags(curr_merged)

        if norm_new != norm_curr or (field_name in diff.custom_edit_fields and new_val != curr_merged):
            diff.merged_fields[field_name] = new_val
            if target == "PROP":
                diff.prop_mod_fields[field_name] = new_val
                if norm_new != normalize_html_tags(diff.updated_fields.get(field_name, "")):
                    diff.custom_edit_fields[field_name] = new_val
                else:
                    diff.custom_edit_fields.pop(field_name, None)
            elif target == "OLD":
                diff.old_mod_fields[field_name] = new_val
                if norm_new != normalize_html_tags(diff.original_fields.get(field_name, "")):
                    diff.custom_edit_fields[field_name] = new_val
                else:
                    diff.custom_edit_fields.pop(field_name, None)
            else:  # target == "PARTIAL"
                diff.custom_edit_fields[field_name] = new_val

            captured_any = True
            if on_field_edited_cb is not None:
                try:
                    on_field_edited_cb(diff, field_name, new_val)
                except Exception as e:
                    print(f"[anki-diff] on_field_edited_cb error: {e}")

    return captured_any


def capture_editor_tag_edits(note_or_editor, get_diff_for_note, on_tags_edited_cb=None) -> bool:
    """
    Capture les modifications manuelles de tags dans l'éditeur et met à jour `diff.tags`
    et `diff.tags_active_target`. Retourne True si les tags ont changé.
    """
    if is_syncing_tags():
        return False

    note = _get_editor_note(note_or_editor)
    if note is None:
        note = note_or_editor
    if note is None or not getattr(note, "id", None):
        return False

    diff = get_diff_for_note(note.id)
    if diff is None or getattr(diff, "_syncing_to_editor", False):
        return False

    raw_tags = getattr(note, "tags", None)
    if not isinstance(raw_tags, (list, tuple, set)):
        return False

    new_tags = list(raw_tags)
    if (
        not getattr(diff, "_synced_to_editor", False)
        and set(new_tags) == set(getattr(diff, "original_tags", None) or [])
    ):
        return False
    curr_tags = list(getattr(diff, "tags", None) or [])
    if new_tags == curr_tags:
        return False

    diff.tags = new_tags
    orig_set = set(getattr(diff, "original_tags", None) or [])
    upd_set = set(getattr(diff, "updated_tags", None) or [])
    new_set = set(new_tags)
    if new_set == orig_set and new_set != upd_set:
        diff.tags_active_target = "OLD"
    elif new_set == upd_set:
        diff.tags_active_target = "PROP"
    else:
        diff.tags_active_target = "CUSTOM"

    if on_tags_edited_cb is not None:
        try:
            on_tags_edited_cb(diff, new_tags)
        except Exception as e:
            print(f"[anki-diff] on_tags_edited_cb error: {e}")

    return True


def make_field_edit_hook(get_diff_for_note, on_field_edited_cb=None):
    """
    Retourne un hook compatible avec gui_hooks.editor_did_unfocus_field.

    get_diff_for_note(note_id) -> Optional[NoteDiff]
    on_field_edited_cb(diff, field_name, new_val) -> Optional callback

    Quand l'utilisateur édite manuellement un champ dans l'éditeur natif,
    la nouvelle valeur est capturée et stockée dans prop_mod ou old_mod
    selon le mode actif, et merged_fields est mis à jour.
    """
    def _on_field_unfocus(changed: bool, editor, current_field_idx: int) -> bool:
        """
        Appelé par Anki quand un champ perd le focus.
        Anki passe `changed=False` par défaut (accumulateur de filtre Python) même lorsque
        l'utilisateur a édité le champ en JS ; on inspecte donc toujours `note.fields`.
        Retourne `changed` tel quel pour ne pas déclencher de rechargement de note intempestif.
        """
        capture_editor_field_edits(editor, get_diff_for_note, on_field_edited_cb)
        return changed

    return _on_field_unfocus


def make_typing_edit_hook(get_diff_for_note, on_field_edited_cb=None):
    """Retourne un hook compatible avec gui_hooks.editor_did_fire_typing_timer(note)."""
    def _on_typing_timer(note) -> None:
        capture_editor_field_edits(note, get_diff_for_note, on_field_edited_cb)

    return _on_typing_timer


def make_tag_edit_hook(get_diff_for_note, on_tags_edited_cb=None):
    """Retourne un hook compatible avec gui_hooks.editor_did_update_tags(note)."""
    def _on_tags_updated(note) -> None:
        capture_editor_tag_edits(note, get_diff_for_note, on_tags_edited_cb)

    return _on_tags_updated

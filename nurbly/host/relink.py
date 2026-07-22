# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

# nurbly/host/relink.py
"""Relocate an assembly's out-of-tree linked parts INTO the clone (assembly v2).

FREECAD-COUPLED but FreeCAD-import-FREE. This module reads and WRITES
``Document``/``DocumentObject`` attributes, but it imports no FreeCAD: it is
fully duck-typed, and the one capability it cannot fake -- opening a relocated
part document to re-bind a link to it -- is INJECTED as an ``open_document``
callable (the FreeCAD layer passes ``App.openDocument``; the unit tests pass a
fake). That keeps this module unit-testable without a running FreeCAD (see
tests/test_relink.py). The pure planning of WHERE each part should move lives in
:mod:`nurbly.assembly.plan_relocation`. This module performs the move the plan
describes.

Two link styles
---------------
A linked child can be referenced two different ways, and a real assembly uses
whichever its workbench produces, so we handle BOTH:

* FileName style -- the link element exposes a writable ``FileName`` pointing at
  the child .FCStd (some assembly workbench link features). Re-pointing is just
  ``obj.FileName = dest``.
* LinkedObject style -- a native ``App::Link`` / ``App::LinkGroup`` has NO
  ``FileName``: it references an object in an external document, reachable via
  ``LinkedObject`` (or ``LinkedObjects``) ``-> .Document -> .FileName``.
  Re-pointing means OPENING the relocated copy, finding the object whose
  internal ``Name`` matches (FreeCAD preserves ``Name`` across a file copy), and
  setting the link's ``LinkedObject`` to it. Opening needs FreeCAD, so the
  caller injects ``open_document``; without it the LinkedObject path degrades to
  "could not re-link" (fail-safe) rather than guessing.

:func:`collect_dependency_records` is the read-side companion: it yields one
record per (link object, referenced source file), tagged with which style to
use, so the rewrite re-points the EXACT object rather than guessing.

Defensive by design
-------------------
EVERY host attribute is guarded with ``getattr``/``hasattr``. This module NEVER
raises: a per-target failure adds that target's ORIGINAL source to the returned
``unresolved`` list and processing continues; a catastrophic failure returns
EVERY source as unresolved. If a copy cannot be re-linked (no matching element,
or no opener for a LinkedObject link), the just-made copy is ROLLED BACK so a
present-but-unlinked orphan is never committed. An unresolved part falls through
to the existing v1 out-of-tree warning, never a crashed check-in.

FreeCAD APIs used (all getattr-guarded / injected):
    * ``Document.Objects``                          (scan for link elements)
    * ``DocumentObject.FileName``                   (FileName-style child path)
    * ``DocumentObject.LinkedObject`` / ``.LinkedObjects``  (LinkedObject style)
    * ``DocumentObject.Document.FileName``          (a linked object's owning file)
    * ``DocumentObject.Name``                       (match key across the copy)
    * ``DocumentObject.touch()``                    (mark for recompute)
    * ``Document.recompute()`` / ``Document.save()``  (apply + persist)
    * injected ``open_document(path) -> Document``  (open the relocated copy)
"""

from __future__ import annotations

import os
import shutil
from typing import List, NamedTuple


class DependencyRecord(NamedTuple):
    """One link reference, tagged with how to re-point it.

    ``obj``           -- the FreeCAD link ``DocumentObject``.
    ``file_name``     -- the child .FCStd path this reference currently points at
        (the element's own ``FileName`` for the FileName style, or the linked
        object's owning-document ``FileName`` for the LinkedObject style). Used
        to match a relocation target's source.
    ``via_file_name`` -- True: re-point by writing ``obj.FileName``. False:
        re-point by swapping ``obj``'s ``LinkedObject`` to the relocated copy.
    ``linked_obj``    -- the external object the link points at (LinkedObject
        style only; its ``Name`` is the match key in the copy), else ``None``.

    PURE DATA: a snapshot of intent read off the host, performing no move.
    """

    obj: object
    file_name: str
    via_file_name: bool
    linked_obj: object


def _file_name_of_obj(obj) -> str:
    """``obj.FileName`` if present and non-empty, else ``""`` (never raises)."""
    if obj is None:
        return ""
    return getattr(obj, "FileName", "") or ""


def _doc_file_name_of(linked) -> str:
    """``linked.Document.FileName`` if reachable, else ``""`` (never raises)."""
    if linked is None:
        return ""
    return getattr(getattr(linked, "Document", None), "FileName", "") or ""


def _linked_targets(obj) -> List:
    """Linked target object(s) of a possible App::Link / App::LinkGroup.

    Reads both the singular ``LinkedObject`` and the plural ``LinkedObjects``
    (different link element types expose one or the other). ``LinkedObject`` can
    occasionally be a ``(object, subname)`` tuple, so we take the object
    component. Anything missing or of an unexpected shape contributes nothing.
    Never raises. Mirrors :func:`nurbly.host.dependencies._linked_targets`.
    """
    targets: List = []

    single = getattr(obj, "LinkedObject", None)
    if single is not None:
        if isinstance(single, (list, tuple)):
            if single and single[0] is not None:
                targets.append(single[0])
        else:
            targets.append(single)

    plural = getattr(obj, "LinkedObjects", None)
    if plural:
        try:
            for item in plural:
                if item is not None:
                    targets.append(item)
        except TypeError:
            pass  # not iterable -> ignore rather than crash

    return targets


def collect_dependency_records(doc) -> List[DependencyRecord]:
    """Associate each link reference with the child file it points at.

    Scans ``doc.Objects`` and yields, for every reference it can re-point:

      * a FileName-style record for any object carrying a non-empty ``FileName``;
      * a LinkedObject-style record for every ``LinkedObject(s)`` target whose
        owning document has a ``FileName`` (one record per linked object, so an
        assembly that links several parts -- or the same part several times --
        is fully covered).

    Every host attribute is ``getattr``-guarded, so a missing/odd API yields an
    empty list rather than raising. Returns ``[]`` for ``None`` or a doc with no
    discoverable references.
    """
    if doc is None:
        return []

    objects = getattr(doc, "Objects", None) or []
    try:
        iterator = iter(objects)
    except Exception:  # noqa: BLE001 -- any odd Objects proxy yields [], not a raise
        return []

    records: List[DependencyRecord] = []
    for obj in iterator:
        file_name = _file_name_of_obj(obj)
        if file_name:
            records.append(
                DependencyRecord(
                    obj=obj, file_name=file_name, via_file_name=True, linked_obj=None
                )
            )
        for target in _linked_targets(obj):
            src = _doc_file_name_of(target)
            if src:
                records.append(
                    DependencyRecord(
                        obj=obj, file_name=src, via_file_name=False, linked_obj=target
                    )
                )

    return records


def _canon(path: str) -> str:
    """Absolute + normcase canonical form (case-insensitive on Windows).

    Mirrors :func:`nurbly.assembly._canon` so the host-side path match agrees
    with the pure planner's in/out-of-tree decision. Never raises: an empty or
    odd path canonicalises to ``""``.
    """
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:  # noqa: BLE001 -- a weird path must not crash a check-in
        return ""


def _touch(obj) -> None:
    """Best-effort ``obj.touch()`` so the recompute picks the edit up. No raise."""
    touch = getattr(obj, "touch", None)
    if callable(touch):
        try:
            touch()
        except Exception:  # noqa: BLE001 -- recompute below is the real apply step
            pass


def _set_file_name(obj, dest_abs: str) -> bool:
    """Re-point ``obj.FileName`` at ``dest_abs`` and mark it for recompute.

    Returns True if the FileName was rewritten, False if the object had no
    writable ``FileName``. Never raises.
    """
    if not hasattr(obj, "FileName"):
        return False
    try:
        obj.FileName = dest_abs
    except Exception:  # noqa: BLE001 -- a read-only/odd property is "could not re-link"
        return False
    _touch(obj)
    return True


def _find_by_name(doc, name: str):
    """The object in ``doc`` whose internal ``Name`` equals ``name``, else None.

    FreeCAD preserves ``Name`` across a byte-for-byte file copy, so the linked
    object in the relocated copy carries the same ``Name`` as the original.
    Never raises.
    """
    if doc is None or not name:
        return None
    objects = getattr(doc, "Objects", None) or []
    try:
        iterator = iter(objects)
    except Exception:  # noqa: BLE001
        return None
    for obj in iterator:
        if getattr(obj, "Name", None) == name:
            return obj
    return None


def _ensure_open(dest_abs: str, open_document, opened: dict):
    """Open ``dest_abs`` once via the injected ``open_document``, cached.

    Returns the opened document, or ``None`` if there is no opener or it failed
    (so the LinkedObject re-point degrades to "could not re-link"). Opening is
    cached per destination so a part linked several times is opened once, and so
    FreeCAD is not asked to re-open an already-open document. Never raises.
    """
    if dest_abs in opened:
        return opened[dest_abs]
    doc = None
    if callable(open_document):
        try:
            doc = open_document(dest_abs)
        except Exception:  # noqa: BLE001 -- a failed open is just "could not re-link"
            doc = None
    opened[dest_abs] = doc
    return doc


def _rebind_linked_object(record: DependencyRecord, new_doc) -> bool:
    """Re-point a LinkedObject-style record at the matching object in ``new_doc``.

    Finds the object in the relocated copy whose ``Name`` matches the original
    linked object, then swaps it in: ``obj.LinkedObject`` for a singular link, or
    the matching element of ``obj.LinkedObjects`` for a group. Returns True on a
    successful swap, False if no match or the property is not writable. Never
    raises.
    """
    linked = record.linked_obj
    name = getattr(linked, "Name", None)
    if not name:
        return False
    new_obj = _find_by_name(new_doc, name)
    if new_obj is None:
        return False

    obj = record.obj
    # Singular App::Link.LinkedObject.
    if getattr(obj, "LinkedObject", None) is linked:
        try:
            obj.LinkedObject = new_obj
        except Exception:  # noqa: BLE001 -- read-only/odd property is "could not re-link"
            return False
        _touch(obj)
        return True

    # Plural App::LinkGroup.LinkedObjects -- rebuild the list, swapping the one.
    current = getattr(obj, "LinkedObjects", None)
    if current:
        try:
            rebuilt = [new_obj if item is linked else item for item in current]
            obj.LinkedObjects = rebuilt
        except Exception:  # noqa: BLE001 -- not writable / odd shape -> could not re-link
            return False
        _touch(obj)
        return True

    return False


def _relink_matching(records, src: str, dest_abs: str, open_document, opened) -> bool:
    """Re-point every record referencing ``src`` at ``dest_abs``.

    Matches case-insensitively against the ORIGINAL source via :func:`_canon`.
    FileName-style records are re-pointed by writing ``FileName``; LinkedObject
    records by opening the relocated copy (injected ``open_document``) and
    swapping the link target. Returns True if AT LEAST ONE matching reference was
    successfully re-pointed, False otherwise. Never raises.
    """
    src_canon = _canon(src)
    if not src_canon:
        return False
    rewrote_any = False
    for record in records:
        if _canon(record.file_name) != src_canon:
            continue
        if record.via_file_name:
            if _set_file_name(record.obj, dest_abs):
                rewrote_any = True
        else:
            new_doc = _ensure_open(dest_abs, open_document, opened)
            if new_doc is not None and _rebind_linked_object(record, new_doc):
                rewrote_any = True
    return rewrote_any


def _copy_into_clone(src: str, dest_abs: str) -> None:
    """Copy ``src`` to ``dest_abs``, creating parent dirs. Raises on failure.

    Uses ``shutil.copy2`` to preserve metadata. The caller wraps this in a
    try/except so a copy failure becomes an unresolved entry rather than a crash.
    """
    parent = os.path.dirname(dest_abs)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    shutil.copy2(src, dest_abs)


def _remove_quietly(path: str) -> None:
    """Best-effort delete of a copy we could not re-link. Never raises.

    Rolls back an orphan copy so a present-but-unlinked part is never committed,
    which would ship a broken link and an inaccurate "not committed" warning.
    """
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:  # noqa: BLE001 -- a rollback failure must not crash check-in
        pass


def _save_document(doc) -> None:
    """Persist the re-linked assembly via ``doc.save()`` (best effort, no raise).

    The host owns the post-relocation save: after the links are rewritten we save
    so they are on disk before ``nrb add .`` stages them. A missing/failing
    ``save`` is swallowed -- the worst case is core's degrade-to-v1 warning.
    """
    if doc is None:
        return
    save = getattr(doc, "save", None)
    if not callable(save):
        return
    try:
        save()
    except Exception:  # noqa: BLE001 -- save failure must not crash check-in
        pass


def relocate_dependencies(doc, plan, open_document=None) -> List[str]:
    """Copy + re-link each planned out-of-tree part INTO the clone, then save.

    ``plan`` is a :func:`nurbly.assembly.plan_relocation` list of
    :class:`RelocationTarget`s. ``open_document`` is the injected
    ``App.openDocument`` (path -> Document), needed only to re-bind LinkedObject
    links; omit it and those links degrade to unresolved (fail-safe). For each
    target:

      1. Unless ``already_present``, ``shutil.copy2`` ``src`` to ``dest_abs``.
      2. Re-point every reference to ``src`` -- FileName links by rewriting
         ``FileName``, LinkedObject links by opening the copy and swapping the
         target -- to point in-tree.

    After every target is processed the document is recomputed once and RE-SAVED
    by this module (the host owns the save), so the rewritten links persist
    before ``nrb add .`` stages them.

    Returns the ``unresolved`` list: the ORIGINAL ``src`` of every target that
    could not be copied OR could not be re-linked (its just-made copy rolled
    back). On a catastrophic failure EVERY source is returned. NEVER raises.
    """
    try:
        targets = list(plan) if plan is not None else []
    except Exception:  # noqa: BLE001 -- a non-iterable/odd plan must not crash check-in
        return []
    if not targets:
        return []

    try:
        records = collect_dependency_records(doc)
        opened: dict = {}  # dest_abs -> opened Document (open each copy once)

        unresolved: List[str] = []
        for target in targets:
            src = getattr(target, "src", None)
            dest_abs = getattr(target, "dest_abs", None)
            already_present = getattr(target, "already_present", False)
            if not src or not dest_abs:
                # A malformed target with no source/dest: nothing to do, and we
                # cannot name it as unresolved either, so skip it defensively.
                if src:
                    unresolved.append(src)
                continue

            try:
                copied = False
                if not already_present:
                    _copy_into_clone(src, dest_abs)
                    copied = True
                # Re-point the link(s) from the original src to the new in-clone
                # path. already_present targets still need re-linking (the prior
                # run copied the file but the link may still point out of tree).
                if not _relink_matching(records, src, dest_abs, open_document, opened):
                    # Nothing matched, so the rewrite did nothing. If we just
                    # copied the file in, roll that copy back. Committing a
                    # present-but-unlinked orphan would ship a broken link AND
                    # make the downstream "not committed" warning untrue. Leaving
                    # src unresolved with no copy keeps that warning accurate.
                    if copied:
                        _remove_quietly(dest_abs)
                    unresolved.append(src)
            except Exception:  # noqa: BLE001 -- one bad target must not stop the rest
                unresolved.append(src)

        # Apply every rewrite in one recompute, then persist (host-owned save).
        recompute = getattr(doc, "recompute", None)
        if callable(recompute):
            try:
                recompute()
            except Exception:  # noqa: BLE001 -- the save still captures the edits
                pass
        _save_document(doc)

        return unresolved
    except Exception:  # noqa: BLE001 -- catastrophic: report every src as unresolved
        return _all_sources(targets)


def _all_sources(plan) -> List[str]:
    """Every target's ``src`` (best effort) for the catastrophic-failure return.

    Used only when :func:`relocate_dependencies` hits an error OUTSIDE the
    per-target loop. Reads each ``src`` defensively so the fallback never raises.
    """
    sources: List[str] = []
    try:
        for target in plan:
            src = getattr(target, "src", None)
            if src:
                sources.append(src)
    except Exception:  # noqa: BLE001 -- even the fallback must not raise
        return sources
    return sources

# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Discover the active document's dependency closure (its linked child files).

FREECAD-ONLY. Like the other modules under :mod:`nurbly.host`, this requires
a running FreeCAD (it reads ``Document``/``DocumentObject`` attributes). It is
NOT imported by the pure unit tests -- CI only ``py_compile``s it, and the user
smoke-tests it against a real assembly (see SMOKE_TEST.md). The pure
classification of the paths it returns lives in :mod:`nurbly.assembly`, which is
fully unit-tested.

:func:`collect_dependency_files` returns the absolute ``.FCStd`` paths the given
document depends on, EXCLUDING the document's own ``FileName``. The check-in flow
feeds these to ``nurbly.assembly.classify_dependencies`` to detect linked parts
that live outside the clone and would be silently dropped from the push.

:func:`repath_links_to_clone` is the v2 re-path step: AFTER the check-in flow has
COPIED each out-of-tree part into the clone (per
``nurbly.assembly.build_relocation_plan``), it re-points the document's link
targets at the in-clone copies so ``nrb add .`` commits a self-contained
assembly. It is FIRST-CUT and UNVALIDATED -- FreeCAD is not installed here, so
this code is only byte-compiled in CI and is exercised only by the manual smoke
test (see SMOKE_TEST.md).

Defensive by design
--------------------
FreeCAD's API surface for links varies across versions (``App::Link``,
``App::LinkGroup``, ``getDependentDocuments``), so EVERY host attribute is
guarded with ``getattr``/``hasattr``. An unexpected API shape degrades to "no
deps found" rather than raising -- a missed dependency just means the existing
(pre-feature) silent-drop behaviour, never a crashed check-in.

FreeCAD APIs used (all getattr-guarded):
    * ``Document.getDependentDocuments()`` -> list[Document]   (primary)
    * ``Document.FileName``                                     (path of each doc)
    * ``Document.Objects``                                      (fallback scan)
    * ``DocumentObject.LinkedObject`` / ``.LinkedObjects``      (App::Link[Group])
    * ``DocumentObject.Document``                               (owning doc of a link target)
    * ``Document.restoreLink`` / ``Document.FileName`` (re-path)  (v2 relocate)
"""

from __future__ import annotations

import os
from typing import List


def _file_name_of(doc) -> str:
    """``doc.FileName`` if present and non-empty, else ``""`` (never raises)."""
    if doc is None:
        return ""
    name = getattr(doc, "FileName", "") or ""
    return name


def _linked_targets(obj) -> List:
    """Linked target object(s) of a possible App::Link / App::LinkGroup.

    Returns a flat list of DocumentObjects this object links to, reading both
    the singular ``LinkedObject`` and the plural ``LinkedObjects`` attributes
    (different link element types expose one or the other). Anything missing or
    of an unexpected shape contributes nothing. Never raises.
    """
    targets: List = []

    single = getattr(obj, "LinkedObject", None)
    if single is not None:
        # LinkedObject can occasionally be a (object, subname) tuple; take the
        # object component defensively.
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
            # Not iterable for some reason -- ignore rather than crash.
            pass

    return targets


def collect_dependency_files(doc) -> List[str]:
    """Absolute ``.FCStd`` paths ``doc`` depends on (excluding its own FileName).

    Strategy (each step fully guarded so a missing/odd API yields no deps):

      1. PRIMARY: ``doc.getDependentDocuments()`` returns the closure of
         Document objects this document links into; collect each one's
         ``FileName``. This is the broad, version-stable path.
      2. FALLBACK: scan ``doc.Objects`` for ``App::Link`` / ``App::LinkGroup``
         elements, follow ``LinkedObject(s)`` -> ``.Document`` -> ``.FileName``.
         This catches link targets even if (1) is unavailable on this build.

    Empty FileNames (unsaved linked docs) are skipped; the active document's own
    FileName is excluded; results are de-duplicated by absolute path while
    preserving first-seen order. Returns ``[]`` for ``None`` or a doc with no
    discoverable dependencies.
    """
    if doc is None:
        return []

    own = _file_name_of(doc)
    own_abs = os.path.abspath(own) if own else None

    found: List[str] = []
    seen = set()  # abspaths already added (dedupe)

    def _add(file_name: str) -> None:
        if not file_name:
            return
        abs_path = os.path.abspath(file_name)
        if own_abs is not None and abs_path == own_abs:
            return  # never include the active doc itself
        if abs_path in seen:
            return
        seen.add(abs_path)
        found.append(abs_path)

    # 1. Primary: the document's dependency closure.
    get_deps = getattr(doc, "getDependentDocuments", None)
    if callable(get_deps):
        try:
            dep_docs = get_deps()
        except Exception:  # noqa: BLE001 -- any host hiccup -> fall through to scan
            dep_docs = None
        if dep_docs:
            try:
                for dep_doc in dep_docs:
                    _add(_file_name_of(dep_doc))
            except TypeError:
                pass  # not iterable -> ignore

    # 2. Fallback: walk App::Link / App::LinkGroup elements to their targets.
    objects = getattr(doc, "Objects", None) or []
    try:
        iterator = iter(objects)
    except TypeError:
        iterator = iter(())
    for obj in iterator:
        for target in _linked_targets(obj):
            target_doc = getattr(target, "Document", None)
            _add(_file_name_of(target_doc))

    return found


def _open_documents():
    """All currently-open documents as a list, or ``[]`` (never raises).

    FreeCAD exposes them via ``App.listDocuments()`` (a dict name -> Document).
    Imported lazily + guarded so the module still byte-compiles where FreeCAD is
    absent.
    """
    try:
        import FreeCAD  # noqa: PLC0415 -- lazy host import; absent in CI/byte-compile
    except Exception:  # noqa: BLE001 -- no FreeCAD here -> no open docs to re-path
        return []
    lister = getattr(FreeCAD, "listDocuments", None)
    if not callable(lister):
        return []
    try:
        docs = lister()
    except Exception:  # noqa: BLE001 -- host hiccup -> nothing to re-path
        return []
    if isinstance(docs, dict):
        return list(docs.values())
    try:
        return list(docs)
    except TypeError:
        return []


def repath_links_to_clone(doc, relocations) -> int:
    """Re-point ``doc``'s external links from their old paths to in-clone copies.

    FIRST-CUT + UNVALIDATED. FreeCAD is not installed in this environment, so
    this is only byte-compiled in CI and exercised by the manual smoke test (see
    SMOKE_TEST.md). EVERY host attribute is getattr-guarded; an unexpected API
    shape re-paths nothing rather than raising, so a failed re-path degrades to
    the v1 silent-drop behaviour instead of a crashed check-in.

    ``relocations`` is the plan from
    :func:`nurbly.assembly.build_relocation_plan` -- a list of objects exposing
    ``.src`` (the original out-of-tree absolute path) and ``.dest`` (the in-clone
    copy that the check-in flow has ALREADY written). For each open document
    whose ``FileName`` matches a relocation ``src`` (compared case-insensitively
    via the same canonicalisation the pure layer uses), we re-point that
    document's ``FileName`` at the ``dest`` so FreeCAD reloads/saves the link
    against the in-clone copy.

    Returns the number of documents successfully re-pathed. ``doc`` is accepted
    for parity with :func:`collect_dependency_files` and to allow a future,
    object-level re-path of ``App::Link`` elements within it; the first cut keys
    off the dependent documents' own ``FileName`` because external links resolve
    through the owning ``Document``.
    """
    # Map each relocation source to its destination, canonicalised so the
    # comparison matches the pure layer (case-insensitive on Windows). Built
    # without importing the pure module to keep this layer self-contained.
    src_to_dest = {}
    for op in relocations or []:
        src = getattr(op, "src", None)
        dest = getattr(op, "dest", None)
        if not src or not dest:
            continue
        src_to_dest[os.path.normcase(os.path.abspath(src))] = dest

    if not src_to_dest:
        return 0

    repathed = 0
    for open_doc in _open_documents():
        file_name = _file_name_of(open_doc)
        if not file_name:
            continue
        key = os.path.normcase(os.path.abspath(file_name))
        dest = src_to_dest.get(key)
        if not dest:
            continue
        # Re-point this dependent document at its in-clone copy. ``FileName`` is
        # the load/save target; setting it makes the next save write the copy's
        # path into the assembly's stored link. Guard the write so a read-only or
        # odd attribute can't crash the check-in.
        try:
            setattr(open_doc, "FileName", dest)
        except Exception:  # noqa: BLE001 -- skip this doc, keep re-pathing others
            continue
        repathed += 1

    return repathed

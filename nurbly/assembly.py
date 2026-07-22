# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Classify an assembly's dependency files as inside or outside the clone.

PURE LOGIC -- imports no FreeCAD. The FreeCAD layer discovers the active
document's dependency closure (its linked child .FCStd files) via
:mod:`nurbly.host.dependencies` and passes the resulting absolute paths in
here; this module decides which of them already live INSIDE the clone working
directory (and are therefore committed by ``nrb add .``) versus OUTSIDE it (and
are silently dropped from the push).

Why this matters
----------------
Check in stages the WHOLE clone working dir (``nrb add .`` with ``cwd =
clone_dir``). A dependency part that lives inside the clone is committed; one
that lives outside is NOT -- the user pushes an assembly whose linked parts are
missing on the server. v1 did not move or re-path anything; it just made the
silent drop LOUD by listing the out-of-tree files so the check-in could return a
soft-success warning.

v2 RELOCATES the out-of-tree parts into the clone before check-in instead of
merely warning. There are two pure planners here, both touching no filesystem:

* :func:`plan_relocation` builds a deterministic list of
  :class:`RelocationTarget`s that place each out-of-tree part into a FLAT
  ``<clone>/parts/`` folder keyed by basename, with deterministic numeric
  suffixing on basename collisions. It also exposes ``dest_repo_path`` (the
  repo-relative, forward-slash destination) and an ``already_present`` flag so a
  re-run re-links without re-copying. :func:`repo_relative_path` is the matching
  helper that turns an in-tree linked part into the repo-relative spelling nrb's
  lock/unlock verbs expect.
* :func:`build_relocation_plan` builds a list of :class:`RelocationOp` copy
  operations that place each out-of-tree part under ``<clone>/deps/`` instead,
  disambiguating basename collisions with a short stable hash of the source's
  parent directory.

The FreeCAD layer performs the copy + re-paths the document's links, and ``nrb
add .`` then commits the relocated parts alongside the assembly.

Path handling
-------------
Every path is canonicalised with ``os.path.abspath`` + ``os.path.normcase``
before comparison, so the in/out-of-tree test is case-insensitive on Windows and
robust to ``.``/``..`` and mixed separators. The active document itself is
excluded (it is always in the clone in the clone-first model and is not a
dependency of itself), and the result lists are de-duplicated while preserving
first-seen order.
"""

from __future__ import annotations

import hashlib
import os
from typing import List, NamedTuple, Optional


class DependencyClassification(NamedTuple):
    """Dependency paths split by where they live relative to the clone.

    ``in_tree``     -- deps under ``clone_dir`` (committed by ``nrb add .``).
    ``out_of_tree`` -- deps NOT under ``clone_dir`` (silently dropped today).
    Both lists hold the ORIGINAL (un-normalised) paths, de-duplicated and in
    first-seen order, so the UI can show the user the paths they recognise.
    """

    in_tree: List[str]
    out_of_tree: List[str]


def _canon(path: str) -> str:
    """Absolute + normcase canonical form for comparison (case-insensitive Win)."""
    return os.path.normcase(os.path.abspath(path))


def _is_under(child_canon: str, parent_canon: str) -> bool:
    """True if ``child_canon`` is ``parent_canon`` or nested beneath it.

    Compares via ``os.path.commonpath`` rather than a string ``startswith`` so a
    sibling dir like ``/repo-extras`` is not mistaken for being under ``/repo``.
    Both arguments must already be canonicalised. A ``ValueError`` (e.g. paths on
    different Windows drives, which share no common path) means "not under".
    """
    try:
        return os.path.commonpath([child_canon, parent_canon]) == parent_canon
    except ValueError:
        return False


def repo_relative_path(path: str, clone_dir: str) -> Optional[str]:
    """Repo-relative, forward-slash path of ``path`` under ``clone_dir`` or None.

    Used by per-part Check out to turn an in-tree linked part's absolute path
    into the repo-relative path nrb's ``lock``/``unlock`` verbs expect (the same
    spelling stored in the lock-id cache). Returns ``None`` when ``path`` is NOT
    at or below ``clone_dir`` (out-of-tree parts are never locked by this flow),
    or when it resolves to the clone root itself (no file component).

    PURE + NEVER RAISES: pure path math, no filesystem IO, no FreeCAD. A path on
    a different drive (no common path) is simply "not under" -> ``None``.
    """
    if not path or not clone_dir:
        return None
    child = _canon(path)
    parent = _canon(clone_dir)
    if not _is_under(child, parent):
        return None
    rel = os.path.relpath(path, clone_dir)
    if rel in (os.curdir, ""):
        return None  # the clone root itself has no repo-relative file component
    return rel.replace(os.sep, "/")


def classify_dependencies(
    active_doc_path: str,
    dependency_paths: List[str],
    clone_dir: str,
) -> DependencyClassification:
    """Split ``dependency_paths`` into in-tree vs out-of-tree relative to the clone.

    ``active_doc_path`` -- the assembly being checked in; excluded from BOTH
        result lists (it is not its own dependency and always lives in the clone).
    ``dependency_paths`` -- absolute (or relative) paths of the linked child
        files discovered in FreeCAD. ``None``/empty entries are skipped.
    ``clone_dir``       -- the local clone root; a dependency is "in tree" iff it
        is at or below this directory after canonicalisation.

    Returns a :class:`DependencyClassification`. Dedupe is by canonical path; the
    first original spelling of each path is the one kept in the output lists.
    """
    clone_canon = _canon(clone_dir)
    active_canon = _canon(active_doc_path) if active_doc_path else None

    in_tree: List[str] = []
    out_of_tree: List[str] = []
    seen = set()  # canonical paths already classified (dedupe)

    for dep in dependency_paths or []:
        if not dep:
            continue
        dep_canon = _canon(dep)
        if active_canon is not None and dep_canon == active_canon:
            continue  # never classify the active doc itself
        if dep_canon in seen:
            continue
        seen.add(dep_canon)
        if _is_under(dep_canon, clone_canon):
            in_tree.append(dep)
        else:
            out_of_tree.append(dep)

    return DependencyClassification(in_tree=in_tree, out_of_tree=out_of_tree)


# -- Assembly v2: relocation planning into a flat parts/ dir (pure) -----------


class RelocationTarget(NamedTuple):
    """A single out-of-tree part's planned move into the clone's flat parts/ dir.

    ``src``            -- the part's ORIGINAL absolute path (the spelling passed
        in), so callers can log/copy from where the user recognises it.
    ``dest_abs``       -- the absolute destination INSIDE the clone, i.e.
        ``<clone_dir>/parts/<name>`` localised for the filesystem.
    ``dest_repo_path`` -- the same destination expressed repo-relative with
        FORWARD slashes (``parts/<name>``), for re-linking + commit messages.
    ``already_present``-- True if ``src`` ALREADY canonically equals ``dest_abs``
        (a prior run relocated it), so the host should re-link but NOT re-copy.

    PURE: produced by :func:`plan_relocation` with no FreeCAD import and no
    filesystem access -- it is a description of intent, not a performed move.
    """

    src: str
    dest_abs: str
    dest_repo_path: str
    already_present: bool


def _suffix_name(name: str, n: int) -> str:
    """Return ``name`` for ``n == 1`` else its stem with ``-<n>`` before the ext.

    ``part.FCStd`` -> ``part.FCStd`` (n=1), ``part-2.FCStd`` (n=2), ... A name
    with no extension (``part``) becomes ``part-2``. A dotfile-style name with no
    stem (``.foo``) is treated as having no extension so it becomes ``.foo-2``.
    """
    if n <= 1:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        # No extension (or a leading-dot-only name): append the suffix to the end.
        return f"{name}-{n}"
    return f"{stem}-{n}{dot}{ext}"


def plan_relocation(
    active_doc_path: str,
    out_of_tree: List[str],
    clone_dir: str,
    repo_path: str,
) -> List["RelocationTarget"]:
    """Plan where each out-of-tree part should live inside the clone's parts/ dir.

    For every path in ``out_of_tree`` the destination is a FLAT ``parts/`` folder
    keyed by the part's basename: ``dest_repo_path = "parts/" + basename``. When
    two different sources share a basename the second and later collisions are
    deterministically suffixed (``name.FCStd``, ``name-2.FCStd``, ...), in the
    INPUT order, so the plan is stable across runs.

    ``active_doc_path`` -- the assembly itself, never relocated (it already lives
        in the clone in the clone-first model). Any entry equal to it is skipped.
    ``out_of_tree``     -- the out-of-tree subset (already classified by
        :func:`classify_dependencies`). ``None``/empty entries are skipped.
    ``clone_dir``       -- the local clone root. Destinations are built beneath
        its ``parts/`` subdirectory.
    ``repo_path``       -- the assembly's repo-relative path, accepted for
        symmetry with the host wiring and future per-subdir layouts. The flat
        layout does not consume it, so it does not affect the output today.

    If a source ALREADY canonically equals its computed destination (a prior run
    moved it), the target is marked ``already_present`` so the host re-links but
    skips the copy. De-dupe is by canonical source path, first spelling kept.

    PURE + NEVER RAISES: no FreeCAD, no filesystem IO. Returns the targets in
    first-seen input order.
    """
    clone_canon = _canon(clone_dir)
    parts_dir = os.path.join(clone_dir, "parts")
    active_canon = _canon(active_doc_path) if active_doc_path else None

    targets: List[RelocationTarget] = []
    seen = set()  # canonical source paths already planned (dedupe)
    used_names = {}  # normcased basename -> count already assigned (collision)

    for src in out_of_tree or []:
        if not src:
            continue
        src_canon = _canon(src)
        if active_canon is not None and src_canon == active_canon:
            continue  # never relocate the active assembly itself
        if src_canon in seen:
            continue
        seen.add(src_canon)

        base = os.path.basename(src.replace("\\", "/").rstrip("/"))
        if not base or base in (".", ".."):
            continue  # nothing safe to key a destination on

        key = os.path.normcase(base)
        n = used_names.get(key, 0) + 1
        used_names[key] = n
        name = _suffix_name(base, n)

        dest_abs = os.path.join(parts_dir, name)
        dest_repo_path = "parts/" + name
        already_present = _canon(dest_abs) == src_canon

        targets.append(
            RelocationTarget(
                src=src,
                dest_abs=dest_abs,
                dest_repo_path=dest_repo_path,
                already_present=already_present,
            )
        )

    # ``clone_canon`` is computed for parity with classify_dependencies' guards
    # and to anchor future under-clone validation. It intentionally does not
    # filter here because every dest is built UNDER the clone by construction.
    del clone_canon
    return targets


# -- v2: relocate out-of-tree parts into the clone's deps/ dir ----------------

# Sub-folder of the clone that out-of-tree parts are copied into. Kept as a
# single forward-slash segment; callers localise it via os.path.join.
RELOCATION_SUBDIR = "deps"


class RelocationOp(NamedTuple):
    """One copy operation that pulls an out-of-tree part into the clone.

    ``src``  -- the canonical absolute path of the original (out-of-tree) part.
    ``dest`` -- the canonical absolute path it should be copied to, under
        ``<clone>/deps/``. The FreeCAD layer copies ``src`` -> ``dest`` and then
        re-points the document's link to ``dest``.
    """

    src: str
    dest: str


def _parent_hash(src_canon: str) -> str:
    """Short stable hash of a source's PARENT dir, for collision disambiguation.

    Two different parts can share a basename (``frame/bolt.FCStd`` and
    ``panel/bolt.FCStd``); they must land at distinct dests. We derive a short
    deterministic tag from the canonical parent directory so the same source
    always maps to the same dest (idempotent re-copy) while distinct sources get
    distinct tags. Hashing the PARENT (not the full path) keeps the basename --
    which already differs only by directory -- as the human-readable part.
    """
    parent = os.path.dirname(src_canon)
    digest = hashlib.sha1(parent.encode("utf-8")).hexdigest()
    return digest[:8]


def build_relocation_plan(
    out_of_tree_paths: List[str],
    clone_dir: str,
    in_tree_paths: List[str] | None = None,
) -> List[RelocationOp]:
    """Plan copy operations that pull each out-of-tree part into the clone.

    PURE -- computes the plan only; performs NO filesystem access (the FreeCAD
    layer does the actual copy + re-path). Pass the already-classified
    ``out_of_tree_paths`` (parts that ``nrb add .`` would drop) and the
    ``clone_dir`` root; ``in_tree_paths`` (parts already committed by the clone)
    are accepted for symmetry and to skip any that, after canonicalisation, are
    really already inside the clone -- those need no relocation.

    Each out-of-tree part is placed at ``<clone>/deps/<basename>``. Collision
    handling is DETERMINISTIC:

      * Re-copying the IDENTICAL source path is idempotent -- it yields the same
        dest every time, so repeating the plan is safe (dedupe by canonical src).
      * Two DIFFERENT sources sharing a basename get distinct dests: the first
        keeps the plain ``<basename>`` and each subsequent colliding source has a
        short stable hash of its parent dir inserted before the extension
        (``bolt.FCStd`` -> ``bolt.<hash>.FCStd``). The hash is derived from the
        source, so the assignment is stable regardless of input ordering for any
        given set of sources.

    Returns a list of :class:`RelocationOp` (canonical absolute ``src``/``dest``)
    in first-seen order of the de-duplicated sources. An empty/``None`` input --
    e.g. the single-document case with no out-of-tree deps -- yields ``[]``.
    """
    clone_canon = _canon(clone_dir)

    # Canonical paths already living inside the clone never need relocation; we
    # also use this set so a dest can never collide with an existing in-tree file
    # location (dests are under <clone>/deps which in-tree parts are not, but we
    # stay defensive).
    in_tree_canon = {
        _canon(p) for p in (in_tree_paths or []) if p
    }

    deps_root = os.path.join(clone_canon, RELOCATION_SUBDIR.replace("/", os.sep))

    plan: List[RelocationOp] = []
    seen_src = set()  # canonical sources already planned (idempotent dedupe)
    taken_dest = set()  # canonical dests already assigned (collision guard)

    for src in out_of_tree_paths or []:
        if not src:
            continue
        src_canon = _canon(src)
        if src_canon in seen_src:
            continue  # identical source -> same dest, already planned
        if src_canon in in_tree_canon:
            continue  # already inside the clone -> nothing to relocate
        seen_src.add(src_canon)

        base = os.path.basename(src_canon)
        dest = os.path.join(deps_root, base)
        if dest in taken_dest:
            # Basename collision with a DIFFERENT source -> disambiguate with a
            # short stable hash of this source's parent dir, inserted before the
            # extension so the dest keeps a sensible suffix.
            stem, ext = os.path.splitext(base)
            tag = _parent_hash(src_canon)
            dest = os.path.join(deps_root, f"{stem}.{tag}{ext}")
            # The hashed dest can itself (extraordinarily) collide -- e.g. a
            # source literally named "<stem>.<tag><ext>". Append an incrementing
            # counter until the dest is unique so two distinct sources can never
            # be assigned the same dest.
            n = 2
            while dest in taken_dest:
                dest = os.path.join(deps_root, f"{stem}.{tag}.{n}{ext}")
                n += 1
        taken_dest.add(dest)

        plan.append(RelocationOp(src=src_canon, dest=dest))

    return plan

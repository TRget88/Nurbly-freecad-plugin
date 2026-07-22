# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Persist the document -> repo/path link (and cached lock ids).

PURE LOGIC -- imports no FreeCAD. This is the durable "link" between an open
CAD document and the Nurbly repo it lives in (PLUGIN_MVP.md: "Persist which
open document maps to which repo + path ... Plugin-side state").

Format + location
-----------------
A single JSON file at ``~/.nurbly/nurbly-plugin/doc-map.json``. We sit *next to*
nrb's own ``~/.nurbly/`` state but in our own sub-directory + file -- nrb never
reads or writes it, and the plugin never touches nrb's ``credentials.json`` /
``config.json`` (the spec forbids the plugin from storing credentials).

Why ``~/.nurbly`` and not FreeCAD document metadata: the link must survive even
if the user never saves the .FCStd, and must be reachable before a document is
open (e.g. to list all known mappings). FreeCAD's own prefs are per-install
and not visible to unit tests.

Key
---
The map is keyed by the **absolute, normalised filesystem path** of the
``.FCStd`` document (``doc.FileName`` in FreeCAD). That is stable across
renames-of-the-window and unique per file. A document that has never been
saved has no FileName and therefore cannot be linked yet -- the UI surfaces
that as "save the document first".

Lock ids (schema 2: per-part locking)
-------------------------------------
A single Check out can now lock the active file PLUS every in-tree linked part
(per-part locking), so a mapping caches a *dict* of lock ids keyed by the
repo-relative path that was locked::

    {
      "<abs FCStd path>": {
        "owner": "alice",
        "repo": "gizmo",
        "repo_path": "parts/widget.FCStd",  # path *inside* the repo, fwd slashes
        "clone_dir": "/home/alice/gizmo",    # local working copy root
        "lock_ids": {                          # repo-relative path -> lock id
          "parts/widget.FCStd": "550e8400-...",
          "parts/bracket.FCStd": "6ba7b810-..."
        }
      }
    }

``lock_ids`` is the WIRE CONTRACT. It replaces the schema-1 scalar ``lock_id``
field (one cached id). :meth:`MappingStore.load` MIGRATES an old single-lock_id
entry forward: a non-null scalar becomes a one-entry dict keyed by that entry's
``repo_path``, so a doc-map.json written by the previous plugin still loads.

The store is deliberately tiny and synchronous: it is read/written on the GUI
thread around single user actions, never in a hot path.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

# schema 1: per-entry scalar ``lock_id``. schema 2: per-entry ``lock_ids`` dict
# (per-part locking). load() migrates 1 -> 2 in memory, and save() writes 2.
_SCHEMA_VERSION = 2


@dataclass
class DocMapping:
    """The link for one document.

    ``lock_ids`` maps each LOCKED repo-relative path to its nrb lock id. It is
    populated on Check out (the active path plus each in-tree linked part) and
    cleared on a clean Check in. An empty dict means nothing is currently held.
    """

    owner: str
    repo: str
    repo_path: str  # path of the file relative to the repo root (forward slashes)
    clone_dir: str  # absolute path of the local clone (cwd for nrb add/commit/push)
    lock_ids: Dict[str, str] = field(default_factory=dict)

    @property
    def owner_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


def _resolve_state_path(new_path: str, legacy_path: str) -> str:
    """Return ``new_path``, migrating a pre-rebrand ``~/.vrd`` file to it once.

    The plugin's state dir was renamed ``~/.vrd`` -> ``~/.nurbly`` alongside
    the CLI.  If the new file does not exist yet but a legacy one does, copy
    it across (best-effort, non-destructive) so an upgrading user keeps their
    document links.  Any copy failure is swallowed -- the caller then reads an
    empty store, which must never block sign-in.
    """
    try:
        if not os.path.exists(new_path) and os.path.exists(legacy_path):
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            shutil.copy2(legacy_path, new_path)
    except OSError:
        pass
    return new_path


def default_store_path() -> str:
    """``~/.nurbly/nurbly-plugin/doc-map.json`` resolved against $HOME/%USERPROFILE%.

    A pre-rebrand ``~/.vrd/nurbly-plugin/doc-map.json`` is migrated to the new
    location on first resolve (see :func:`_resolve_state_path`).

    Mirrors nrb's own home resolution (paths.rs: ``$HOME`` then
    ``%USERPROFILE%``) so the plugin and CLI agree on "home" on Windows.
    """
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not home:
        raise RuntimeError("Cannot determine home directory -- set HOME or USERPROFILE")
    new_path = os.path.join(home, ".nurbly", "nurbly-plugin", "doc-map.json")
    legacy_path = os.path.join(home, ".vrd", "nurbly-plugin", "doc-map.json")
    return _resolve_state_path(new_path, legacy_path)


def _norm_key(doc_path: str) -> str:
    """Canonicalise a document path into the map key (absolute + normcase)."""
    return os.path.normcase(os.path.abspath(doc_path))


def _migrate_lock_ids(val: dict) -> Dict[str, str]:
    """Read a row's lock ids in BOTH the schema-2 dict and schema-1 scalar form.

    schema 2: ``lock_ids`` is a ``{repo-relative path: lock id}`` dict. We copy
    the string-keyed/string-valued pairs (skipping any odd shapes defensively).

    schema 1 (back-compat): a non-null scalar ``lock_id`` becomes a one-entry
    dict keyed by the row's ``repo_path`` so an old map still loads with its
    single held lock intact. A null/absent scalar yields an empty dict.
    """
    raw = val.get("lock_ids")
    if isinstance(raw, dict):
        return {
            str(path): str(lid)
            for path, lid in raw.items()
            if isinstance(path, str) and isinstance(lid, str) and path and lid
        }
    # schema-1 migration: lift the single scalar onto its repo_path.
    legacy = val.get("lock_id")
    repo_path = val.get("repo_path")
    if isinstance(legacy, str) and legacy and isinstance(repo_path, str) and repo_path:
        return {repo_path: legacy}
    return {}


class MappingStore:
    """A thin JSON-backed dict of ``abs doc path -> DocMapping``.

    Pass ``path`` to point at an arbitrary file (the tests use a tmp file);
    omit it to use :func:`default_store_path`.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_store_path()
        self._data: Dict[str, DocMapping] = {}
        self._loaded = False

    # ── load / save ──────────────────────────────────────────────────────────

    def load(self) -> "MappingStore":
        """Read the JSON file. A missing/empty/corrupt file yields an empty
        store rather than raising -- a first run has no file, and we never want
        a malformed map to block sign-in.

        Migrates schema-1 rows (scalar ``lock_id``) to the schema-2 ``lock_ids``
        dict in memory so an old doc-map.json still loads (see
        :func:`_migrate_lock_ids`)."""
        self._data = {}
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (FileNotFoundError, ValueError):
            return self
        entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
        for key, val in entries.items():
            try:
                self._data[key] = DocMapping(
                    owner=val["owner"],
                    repo=val["repo"],
                    repo_path=val["repo_path"],
                    clone_dir=val["clone_dir"],
                    lock_ids=_migrate_lock_ids(val),
                )
            except (KeyError, TypeError):
                # Skip a single bad row; keep the rest of the map usable.
                continue
        return self

    def save(self) -> None:
        """Atomically write the JSON file, creating the parent dir (0700-ish on
        POSIX via os.makedirs mode) if needed. Always writes the schema-2 shape
        (``lock_ids`` dict)."""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "schema": _SCHEMA_VERSION,
            "entries": {k: asdict(v) for k, v in self._data.items()},
        }
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)  # atomic on the same filesystem

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ── accessors ──────────────────────────────────────────────────────────

    def get(self, doc_path: str) -> Optional[DocMapping]:
        self._ensure_loaded()
        return self._data.get(_norm_key(doc_path))

    def set(self, doc_path: str, mapping: DocMapping) -> None:
        """Upsert the mapping for ``doc_path`` (does NOT save; call save())."""
        self._ensure_loaded()
        self._data[_norm_key(doc_path)] = mapping

    def _require(self, doc_path: str) -> DocMapping:
        """Return the mapping for ``doc_path`` or raise ``KeyError`` if unlinked."""
        self._ensure_loaded()
        key = _norm_key(doc_path)
        if key not in self._data:
            raise KeyError(f"document not linked: {doc_path}")
        return self._data[key]

    def set_lock_ids(self, doc_path: str, lock_ids: Dict[str, str]) -> None:
        """Replace the whole cached lock-id dict for an already-linked doc.

        Raises ``KeyError`` if the doc isn't linked yet. Does NOT save.
        """
        self._require(doc_path).lock_ids = dict(lock_ids)

    def clear_lock_ids(self, doc_path: str) -> None:
        """Drop every cached lock id (called after a clean Check in release).

        Raises ``KeyError`` if the doc isn't linked. Does NOT save.
        """
        self._require(doc_path).lock_ids = {}

    def rekey(self, old_doc_path: str, new_doc_path: str) -> None:
        """Move a mapping from one document path to another (after saveAs).

        ``doc.saveAs`` re-points the active document's ``FileName`` to its new
        location (the clone-first flow saves the .FCStd INTO the clone), so the
        map key must follow without losing owner/repo/repo_path/clone_dir/lock_ids.

        No-op if the key is unchanged. Raises ``KeyError`` if ``old`` isn't
        linked.
        """
        self._ensure_loaded()
        old = _norm_key(old_doc_path)
        new = _norm_key(new_doc_path)
        if old == new:
            return
        if old not in self._data:
            raise KeyError(f"document not linked: {old_doc_path}")
        self._data[new] = self._data.pop(old)

    def remove(self, doc_path: str) -> None:
        self._ensure_loaded()
        self._data.pop(_norm_key(doc_path), None)

    def all_mappings(self) -> Dict[str, DocMapping]:
        self._ensure_loaded()
        return dict(self._data)

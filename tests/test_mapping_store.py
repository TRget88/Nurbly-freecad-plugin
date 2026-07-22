# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.mapping_store -- the doc<->repo/path link.

Pure Python, no FreeCAD. Uses a tmp file so nothing touches the real
~/.nurbly/nurbly-plugin/doc-map.json.

Covers the schema-2 per-part lock cache (``lock_ids`` dict) plus the schema-1
backward-compat migration (an old scalar ``lock_id`` still loads).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly.mapping_store import (  # noqa: E402
    DocMapping,
    MappingStore,
    default_store_path,
)

UUID = "550e8400-e29b-41d4-a716-446655440000"
UUID2 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


class TestMappingStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sub", "doc-map.json")
        self.doc = os.path.join(self.tmp, "widget.FCStd")

    def _mapping(self, lock_ids=None):
        return DocMapping(
            owner="alice",
            repo="gizmo",
            repo_path="parts/widget.FCStd",
            clone_dir=os.path.join(self.tmp, "gizmo"),
            lock_ids=lock_ids or {},
        )

    def test_roundtrip(self):
        store = MappingStore(self.path).load()
        store.set(self.doc, self._mapping())
        store.save()

        reloaded = MappingStore(self.path).load()
        got = reloaded.get(self.doc)
        self.assertIsNotNone(got)
        self.assertEqual(got.owner_repo, "alice/gizmo")
        self.assertEqual(got.repo_path, "parts/widget.FCStd")
        self.assertEqual(got.lock_ids, {})

    def test_missing_file_is_empty(self):
        store = MappingStore(self.path).load()
        self.assertIsNone(store.get(self.doc))
        self.assertEqual(store.all_mappings(), {})

    def test_corrupt_file_is_empty_not_raised(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")
        store = MappingStore(self.path).load()  # must not raise
        self.assertEqual(store.all_mappings(), {})

    def test_set_and_clear_lock_ids_roundtrips(self):
        # Per-part: set_lock_ids caches the whole acquired set keyed by repo path
        # and survives a save/reload. clear_lock_ids drops it on a clean release.
        store = MappingStore(self.path).load()
        store.set(self.doc, self._mapping())
        store.set_lock_ids(
            self.doc,
            {"parts/widget.FCStd": UUID, "parts/bracket.FCStd": UUID2},
        )
        store.save()

        reloaded = MappingStore(self.path).load()
        self.assertEqual(
            reloaded.get(self.doc).lock_ids,
            {"parts/widget.FCStd": UUID, "parts/bracket.FCStd": UUID2},
        )
        reloaded.clear_lock_ids(self.doc)
        self.assertEqual(reloaded.get(self.doc).lock_ids, {})

    def test_set_lock_ids_replaces_whole_dict(self):
        store = MappingStore(self.path).load()
        store.set(self.doc, self._mapping(lock_ids={"old/path.FCStd": "stale"}))
        store.set_lock_ids(self.doc, {"parts/widget.FCStd": UUID})
        self.assertEqual(
            store.get(self.doc).lock_ids, {"parts/widget.FCStd": UUID}
        )
        # A copy is stored, not the caller's dict (no aliasing).
        src = {"parts/x.FCStd": UUID2}
        store.set_lock_ids(self.doc, src)
        src["parts/y.FCStd"] = "leak"
        self.assertEqual(store.get(self.doc).lock_ids, {"parts/x.FCStd": UUID2})

    def test_mutators_on_unlinked_raise(self):
        store = MappingStore(self.path).load()
        with self.assertRaises(KeyError):
            store.set_lock_ids(self.doc, {"p": "x"})
        with self.assertRaises(KeyError):
            store.clear_lock_ids(self.doc)

    def test_schema1_scalar_lock_id_migrates_on_load(self):
        # Backward-compat: a doc-map.json written by the OLD plugin (schema 1,
        # one scalar ``lock_id``) must still load, lifting the scalar onto a
        # one-entry ``lock_ids`` dict keyed by the row's repo_path.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        key = os.path.normcase(os.path.abspath(self.doc))
        legacy = {
            "schema": 1,
            "entries": {
                key: {
                    "owner": "alice",
                    "repo": "gizmo",
                    "repo_path": "parts/widget.FCStd",
                    "clone_dir": os.path.join(self.tmp, "gizmo"),
                    "lock_id": UUID,
                }
            },
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)

        got = MappingStore(self.path).load().get(self.doc)
        self.assertIsNotNone(got)
        # The scalar was migrated to a one-entry dict under its repo_path.
        self.assertEqual(got.lock_ids, {"parts/widget.FCStd": UUID})
        # No stale scalar attribute survives.
        self.assertFalse(hasattr(got, "lock_id"))

    def test_schema1_null_lock_id_migrates_to_empty_dict(self):
        # An old map whose lock_id is null (checked in / never checked out) loads
        # with an empty lock_ids dict, not a {repo_path: None} entry.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        key = os.path.normcase(os.path.abspath(self.doc))
        legacy = {
            "schema": 1,
            "entries": {
                key: {
                    "owner": "alice",
                    "repo": "gizmo",
                    "repo_path": "parts/widget.FCStd",
                    "clone_dir": os.path.join(self.tmp, "gizmo"),
                    "lock_id": None,
                }
            },
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)

        got = MappingStore(self.path).load().get(self.doc)
        self.assertEqual(got.lock_ids, {})

    def test_save_writes_schema_2(self):
        store = MappingStore(self.path).load()
        store.set(self.doc, self._mapping(lock_ids={"parts/widget.FCStd": UUID}))
        store.save()
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["schema"], 2)
        row = next(iter(raw["entries"].values()))
        # The on-disk shape carries ``lock_ids`` (a dict), not a scalar lock_id.
        self.assertEqual(row["lock_ids"], {"parts/widget.FCStd": UUID})
        self.assertNotIn("lock_id", row)

    def test_key_is_path_normalised(self):
        store = MappingStore(self.path).load()
        store.set(self.doc, self._mapping())
        # Look up via a non-normalised but equivalent path.
        weird = os.path.join(self.tmp, ".", "widget.FCStd")
        self.assertIsNotNone(store.get(weird))

    def test_rekey_moves_entry(self):
        store = MappingStore(self.path).load()
        store.set(self.doc, self._mapping(lock_ids={"parts/widget.FCStd": "abc-123"}))
        new_doc = os.path.join(self.tmp, "gizmo", "widget.FCStd")
        store.rekey(self.doc, new_doc)
        # Old key gone; new key carries the full mapping unchanged.
        self.assertIsNone(store.get(self.doc))
        got = store.get(new_doc)
        self.assertIsNotNone(got)
        self.assertEqual(got.owner_repo, "alice/gizmo")
        self.assertEqual(got.repo_path, "parts/widget.FCStd")
        self.assertEqual(got.clone_dir, os.path.join(self.tmp, "gizmo"))
        self.assertEqual(got.lock_ids, {"parts/widget.FCStd": "abc-123"})

    def test_rekey_noop_when_same(self):
        store = MappingStore(self.path).load()
        store.set(self.doc, self._mapping())
        store.rekey(self.doc, self.doc)  # must not raise
        self.assertIsNotNone(store.get(self.doc))

    def test_rekey_unlinked_raises(self):
        store = MappingStore(self.path).load()
        with self.assertRaises(KeyError):
            store.rekey(self.doc, os.path.join(self.tmp, "other.FCStd"))

    def test_default_path_points_at_nurbly_dir(self):
        # Post-rebrand the doc-map lives under ~/.nurbly, not ~/.vrd.
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        home = tempfile.mkdtemp()
        try:
            os.environ["HOME"] = home
            os.environ["USERPROFILE"] = home
            self.assertEqual(
                default_store_path(),
                os.path.join(home, ".nurbly", "nurbly-plugin", "doc-map.json"),
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_legacy_vrd_doc_map_is_migrated_to_nurbly(self):
        # A doc-map written before the rebrand lives at
        # ~/.vrd/nurbly-plugin/doc-map.json.  default_store_path() must copy it
        # forward to ~/.nurbly on first resolve (non-destructive) so cached
        # document links survive the upgrade.
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        home = tempfile.mkdtemp()
        try:
            os.environ["HOME"] = home
            os.environ["USERPROFILE"] = home
            legacy = os.path.join(home, ".vrd", "nurbly-plugin", "doc-map.json")
            os.makedirs(os.path.dirname(legacy), exist_ok=True)
            with open(legacy, "w", encoding="utf-8") as fh:
                json.dump({"schema": 2, "entries": {}}, fh)

            resolved = default_store_path()

            self.assertEqual(
                resolved,
                os.path.join(home, ".nurbly", "nurbly-plugin", "doc-map.json"),
            )
            self.assertTrue(os.path.exists(resolved), "legacy doc-map migrated")
            # Non-destructive: the legacy file is left in place.
            self.assertTrue(os.path.exists(legacy))
        finally:
            shutil.rmtree(home, ignore_errors=True)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()

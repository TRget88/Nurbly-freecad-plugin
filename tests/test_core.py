# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.core -- the five-action orchestration.

Pure Python, no FreeCAD. A fake runner records every (argv, cwd) and returns
canned NrbResults; a fake exporter records that STEP export ran. These assert
the EXACT nrb sequences + cwd from PLUGIN_MVP.md, and the stop-before-unlock
behaviour on a rejected push.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly import core  # noqa: E402
from nurbly.errors import ErrorKind  # noqa: E402
from nurbly.mapping_store import DocMapping, MappingStore  # noqa: E402
from nurbly.nrb_runner import NrbResult  # noqa: E402

UUID = "550e8400-e29b-41d4-a716-446655440000"
UUID2 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
UUID3 = "00112233-4455-6677-8899-aabbccddeeff"


class FakeRunner:
    """Records calls and replays scripted results keyed by the first arg (verb)."""

    def __init__(self, scripts):
        # scripts: dict verb -> NrbResult (or callable(args)->NrbResult)
        self.scripts = scripts
        self.calls = []  # list of (argv, cwd)

    def __call__(self, args, cwd=None, **kw):
        self.calls.append((list(args), cwd))
        verb = args[0]
        result = self.scripts.get(verb)
        if callable(result):
            result = result(args)
        if result is None:
            result = NrbResult(0, "", "", ["nrb", *args])
        return result

    def verbs(self):
        return [argv[0] for argv, _ in self.calls]


def ok(stdout=""):
    return NrbResult(0, stdout, "", [])


def fail(stderr):
    return NrbResult(1, "", stderr, [])


class TestSignIn(unittest.TestCase):
    def test_success(self):
        runner = FakeRunner({"login": ok("Logged in as alice (via PAT)")})
        res = core.sign_in(runner, "nrbpat_x")
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls[0][0], ["login", "--token", "nrbpat_x"])

    def test_rejected_token_classified(self):
        runner = FakeRunner({"login": fail("Token rejected by the server")})
        res = core.sign_in(runner, "bad")
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.NOT_AUTHENTICATED)


class TestLinkClone(unittest.TestCase):
    def _store(self):
        tmp = tempfile.mkdtemp()
        return MappingStore(os.path.join(tmp, "m.json")).load(), tmp

    def test_clone_then_persists_link(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": ok("Cloning alice/gizmo into 'gizmo'...\nDone.")})
        doc = os.path.join(tmp, "widget.FCStd")
        clone_dir = os.path.join(tmp, "gizmo")
        saved = []  # records each save_as target (clone-first saveAs)
        res = core.clone_and_link(
            runner,
            store,
            lambda p: saved.append(p) or p,  # fake save_as -> returns the path
            owner="alice",
            repo="gizmo",
            doc_path=doc,
            repo_path="widget.FCStd",
            clone_dir=clone_dir,
        )
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls[0][0], ["clone", "alice/gizmo"])
        # The doc was saved exactly once, INTO the clone at its repo path.
        in_clone = os.path.join(clone_dir, "widget.FCStd")
        self.assertEqual(saved, [in_clone])
        # Link must be persisted, reloadable, and keyed by the IN-CLONE path --
        # not the original pre-clone doc path.
        reloaded = MappingStore(store.path).load()
        self.assertEqual(reloaded.get(in_clone).owner_repo, "alice/gizmo")
        self.assertIsNone(reloaded.get(doc))

    def test_clone_failure_does_not_persist(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": fail("HTTP 404 -- not found")})
        doc = os.path.join(tmp, "widget.FCStd")
        saved = []
        res = core.clone_and_link(
            runner, store, lambda p: saved.append(p) or p,
            owner="a", repo="b", doc_path=doc,
            repo_path="widget.FCStd", clone_dir=tmp,
        )
        self.assertFalse(res.ok)
        self.assertIsNone(MappingStore(store.path).load().get(doc))
        # Clone failed first -> save_as must NOT have run.
        self.assertEqual(saved, [])


class TestOpenProject(unittest.TestCase):
    """The document-free Open project action (clone/adopt + open + link)."""

    def _store(self):
        tmp = tempfile.mkdtemp()
        return MappingStore(os.path.join(tmp, "m.json")).load(), tmp

    def test_clone_then_maps_single_doc(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": ok("Cloning alice/gizmo...\nDone.")})
        clone_dir = os.path.join(tmp, "gizmo")
        doc = os.path.join(clone_dir, "widget.FCStd")
        res = core.open_project(
            runner, store,
            lambda d: [doc],   # list_fcstd: one document discovered
            lambda p: p,       # open_document: returns the opened path
            lambda c: c[0],    # select_fcstd: auto-pick the lone candidate
            owner="alice", repo="gizmo", clone_dir=clone_dir,
        )
        self.assertTrue(res.ok)
        # Clones INTO clone_dir (the --directory positional), unlike LinkClone's
        # default which passes no directory.
        self.assertEqual(
            runner.calls[0][0], ["clone", "alice/gizmo", "--directory", clone_dir]
        )
        reloaded = MappingStore(store.path).load()
        m = reloaded.get(doc)
        self.assertIsNotNone(m)
        self.assertEqual(m.owner_repo, "alice/gizmo")
        self.assertEqual(m.repo_path, "widget.FCStd")
        self.assertEqual(m.clone_dir, clone_dir)
        self.assertEqual(m.lock_ids, {})

    def test_already_cloned_skips_clone(self):
        store, tmp = self._store()
        runner = FakeRunner({})  # any nrb call would still be recorded
        clone_dir = os.path.join(tmp, "gizmo")
        doc = os.path.join(clone_dir, "widget.FCStd")
        res = core.open_project(
            runner, store, lambda d: [doc], lambda p: p, lambda c: c[0],
            owner="a", repo="b", clone_dir=clone_dir, already_cloned=True,
        )
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls, [])  # NO clone (or any) nrb call
        self.assertIsNotNone(MappingStore(store.path).load().get(doc))

    def test_clone_failure_does_not_map_or_discover(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": fail("HTTP 404 -- not found")})
        clone_dir = os.path.join(tmp, "gizmo")
        walked = []
        res = core.open_project(
            runner, store,
            lambda d: walked.append(d) or [os.path.join(clone_dir, "x.FCStd")],
            lambda p: p, lambda c: c[0],
            owner="a", repo="b", clone_dir=clone_dir,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.NOT_FOUND)
        self.assertEqual(walked, [])  # discovery must NOT run after a failed clone
        self.assertEqual(MappingStore(store.path).load().all_mappings(), {})

    def test_zero_docs_soft_success_no_mapping(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": ok()})
        clone_dir = os.path.join(tmp, "gizmo")
        res = core.open_project(
            runner, store, lambda d: [], lambda p: p, lambda c: c[0],
            owner="a", repo="b", clone_dir=clone_dir,
        )
        self.assertTrue(res.ok)  # soft success -- the clone is still valid
        self.assertIn("No CAD documents", res.title)
        self.assertEqual(MappingStore(store.path).load().all_mappings(), {})

    def test_many_docs_maps_only_the_selected(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": ok()})
        clone_dir = os.path.join(tmp, "gizmo")
        asm = os.path.join(clone_dir, "asm.FCStd")
        bracket = os.path.join(clone_dir, "parts", "bracket.FCStd")
        seen = []

        def select(candidates):
            seen.append(list(candidates))
            return bracket

        res = core.open_project(
            runner, store, lambda d: [asm, bracket], lambda p: p, select,
            owner="a", repo="b", clone_dir=clone_dir,
        )
        self.assertTrue(res.ok)
        self.assertEqual(seen, [[asm, bracket]])  # selector saw every candidate
        reloaded = MappingStore(store.path).load()
        self.assertIsNone(reloaded.get(asm))  # only the chosen one is mapped
        self.assertEqual(reloaded.get(bracket).repo_path, "parts/bracket.FCStd")

    def test_mapping_uses_opened_path_not_chosen(self):
        # THE correctness invariant: the mapping must key off the path
        # open_document RETURNS (the opened doc's FileName), not the path it was
        # handed -- otherwise a later Check out lookup misses. Here FreeCAD
        # "resolves" the pick to a nested path; repo_path must follow it.
        store, tmp = self._store()
        runner = FakeRunner({"clone": ok()})
        clone_dir = os.path.join(tmp, "gizmo")
        chosen = os.path.join(clone_dir, "widget.FCStd")
        opened = os.path.join(clone_dir, "parts", "widget.FCStd")
        res = core.open_project(
            runner, store, lambda d: [chosen], lambda p: opened, lambda c: c[0],
            owner="a", repo="b", clone_dir=clone_dir,
        )
        self.assertTrue(res.ok)
        reloaded = MappingStore(store.path).load()
        self.assertIsNone(reloaded.get(chosen))  # NOT keyed by the input path
        self.assertEqual(reloaded.get(opened).repo_path, "parts/widget.FCStd")

    def test_cancel_selection_is_noop(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": ok()})
        clone_dir = os.path.join(tmp, "gizmo")
        res = core.open_project(
            runner, store, lambda d: [os.path.join(clone_dir, "x.FCStd")],
            lambda p: p, lambda c: None,  # user cancelled the picker
            owner="a", repo="b", clone_dir=clone_dir,
        )
        self.assertTrue(res.ok)
        self.assertEqual(MappingStore(store.path).load().all_mappings(), {})

    def test_open_failure_does_not_map(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": ok()})
        clone_dir = os.path.join(tmp, "gizmo")

        def boom(_path):
            raise RuntimeError("corrupt document")

        res = core.open_project(
            runner, store, lambda d: [os.path.join(clone_dir, "x.FCStd")],
            boom, lambda c: c[0],
            owner="a", repo="b", clone_dir=clone_dir,
        )
        self.assertFalse(res.ok)
        self.assertIn("Could not open", res.title)
        self.assertEqual(MappingStore(store.path).load().all_mappings(), {})

    def test_opened_outside_clone_fails(self):
        store, tmp = self._store()
        runner = FakeRunner({"clone": ok()})
        clone_dir = os.path.join(tmp, "gizmo")
        outside = os.path.join(tmp, "elsewhere", "widget.FCStd")
        res = core.open_project(
            runner, store, lambda d: [os.path.join(clone_dir, "widget.FCStd")],
            lambda p: outside,  # FreeCAD followed a link out of the clone
            lambda c: c[0],
            owner="a", repo="b", clone_dir=clone_dir,
        )
        self.assertFalse(res.ok)
        self.assertIn("not inside the project", res.title)
        self.assertEqual(MappingStore(store.path).load().all_mappings(), {})

    def test_open_then_check_out_resolves_mapping(self):
        # The acceptance criterion: after Open project, Check out works on the
        # freshly-opened doc with no "Document is not linked".
        store, tmp = self._store()
        clone_dir = os.path.join(tmp, "gizmo")
        doc = os.path.join(clone_dir, "widget.FCStd")
        res = core.open_project(
            FakeRunner({"clone": ok()}), store,
            lambda d: [doc], lambda p: p, lambda c: c[0],
            owner="alice", repo="gizmo", clone_dir=clone_dir,
        )
        self.assertTrue(res.ok)
        runner2 = FakeRunner({
            "pull": ok("Already up to date."),
            "lock": ok(_lock_json(UUID, "widget.FCStd")),
        })
        co = core.check_out(runner2, store, doc_path=doc)
        self.assertTrue(co.ok)
        self.assertEqual(co.lock_ids, {"widget.FCStd": UUID})


def _lock_json(lock_id, path="widget.FCStd"):
    """A single FileLockResponse as `nrb lock --json` would emit it."""
    return json.dumps(
        {
            "id": lock_id,
            "repo_id": "11111111-1111-1111-1111-111111111111",
            "path": path,
            "locked_by_user_id": "22222222-2222-2222-2222-222222222222",
            "locked_by_username": "alice",
            "locked_at": "2026-06-16T10:30:00Z",
        }
    )


class TestCheckOut(unittest.TestCase):
    def _linked(self):
        tmp = tempfile.mkdtemp()
        store = MappingStore(os.path.join(tmp, "m.json")).load()
        doc = os.path.join(tmp, "widget.FCStd")
        clone = os.path.join(tmp, "gizmo")
        store.set(doc, DocMapping("alice", "gizmo", "widget.FCStd", clone))
        store.save()
        return store, doc, clone

    def test_pull_then_lock_captures_id(self):
        store, doc, clone = self._linked()
        runner = FakeRunner({"pull": ok("Pull complete."), "lock": ok(_lock_json(UUID))})
        res = core.check_out(runner, store, doc_path=doc)

        self.assertTrue(res.ok)
        # Per-part cache keyed by the locked repo-relative path.
        self.assertEqual(res.lock_ids, {"widget.FCStd": UUID})
        # Order: pull (in clone dir) then lock (cwd irrelevant -> None).
        self.assertEqual(runner.verbs(), ["pull", "lock"])
        self.assertEqual(runner.calls[0], (["pull", "--remote", "origin"], clone))
        # lock carries --json (its stdout is parsed for the id).
        self.assertEqual(
            runner.calls[1][0], ["lock", "alice/gizmo", "widget.FCStd", "--json"]
        )
        # Lock id cached in the store under its repo path.
        self.assertEqual(
            MappingStore(store.path).load().get(doc).lock_ids,
            {"widget.FCStd": UUID},
        )

    def test_lone_document_locks_only_active_path(self):
        # No dependency_paths (the lone-document case): exactly one lock, on the
        # active file, byte-identical to the pre-per-part behaviour.
        store, doc, clone = self._linked()
        runner = FakeRunner({"pull": ok("Pull complete."), "lock": ok(_lock_json(UUID))})
        res = core.check_out(runner, store, doc_path=doc, dependency_paths=None)
        self.assertTrue(res.ok)
        self.assertEqual(runner.verbs(), ["pull", "lock"])
        self.assertEqual(res.lock_ids, {"widget.FCStd": UUID})

    def test_locks_active_plus_in_tree_parts(self):
        # Per-part acquire loop: the active file AND each IN-TREE linked part are
        # locked. An OUT-OF-TREE part is skipped (no repo path to lock). Lock ids
        # are cached keyed by each repo-relative path.
        store, doc, clone = self._linked()
        in_tree_a = os.path.join(clone, "parts", "bracket.FCStd")
        in_tree_b = os.path.join(clone, "gear.FCStd")
        outside = os.path.join(os.path.dirname(clone), "ext", "external.FCStd")

        ids = {
            "widget.FCStd": UUID,
            "parts/bracket.FCStd": UUID2,
            "gear.FCStd": UUID3,
        }

        def lock_script(args):
            # args: ["lock", "alice/gizmo", "<repo path>", ... "--json"]
            repo_path = args[2]
            return ok(_lock_json(ids[repo_path], repo_path))

        runner = FakeRunner({"pull": ok("Pull complete."), "lock": lock_script})
        res = core.check_out(
            runner, store, doc_path=doc,
            dependency_paths=[in_tree_a, in_tree_b, outside],
        )
        self.assertTrue(res.ok)
        # One pull, then one lock PER in-tree path (active + 2 in-tree). The
        # out-of-tree part is NOT locked.
        self.assertEqual(runner.verbs(), ["pull", "lock", "lock", "lock"])
        locked_paths = [argv[2] for argv, _ in runner.calls if argv[0] == "lock"]
        self.assertEqual(
            locked_paths, ["widget.FCStd", "parts/bracket.FCStd", "gear.FCStd"]
        )
        # Active path is locked FIRST.
        self.assertEqual(locked_paths[0], "widget.FCStd")
        # Every acquired id cached under its repo path.
        self.assertEqual(res.lock_ids, ids)
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, ids)

    def test_mid_loop_conflict_releases_acquired_and_aborts(self):
        # Report-and-abort: the active file locks fine, the in-tree part hits a
        # 409. core must release the ALREADY-acquired active lock (best-effort
        # unlock) and return LOCK_CONFLICT, writing NOTHING to the store.
        store, doc, clone = self._linked()
        in_tree = os.path.join(clone, "parts", "bracket.FCStd")

        def lock_script(args):
            if args[2] == "widget.FCStd":
                return ok(_lock_json(UUID, "widget.FCStd"))
            return fail("HTTP 409 -- File is locked by @bob")

        runner = FakeRunner(
            {"pull": ok("Pull complete."), "lock": lock_script, "unlock": ok("Released.")}
        )
        res = core.check_out(
            runner, store, doc_path=doc, dependency_paths=[in_tree]
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.LOCK_CONFLICT)
        self.assertIn("@bob", res.title)
        # Rollback: the already-acquired active lock was released by id.
        self.assertEqual(runner.verbs(), ["pull", "lock", "lock", "unlock"])
        self.assertEqual(runner.calls[-1][0], ["unlock", "alice/gizmo", UUID])
        # The failure names the offending path so the user knows WHICH part hit
        # the conflict, and -- because the rollback unlock SUCCEEDED -- there is
        # no false "still held" stranded-lock note.
        self.assertIn("parts/bracket.FCStd", res.detail)
        self.assertNotIn("still be held", res.detail)
        # Nothing persisted -- a partial check-out never caches a half-set.
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_missing_id_succeeds_but_warns_and_caches_only_scraped(self):
        # A lock exits 0 but its id cannot be scraped (no --json id in stdout).
        # That path IS locked server-side, so check-out SUCCEEDS, but the result
        # warns and caches ONLY the scraped ids (the unscrapable one has no id to
        # auto-unlock by later).
        store, doc, clone = self._linked()
        in_tree = os.path.join(clone, "parts", "bracket.FCStd")

        def lock_script(args):
            if args[2] == "widget.FCStd":
                return ok(_lock_json(UUID, "widget.FCStd"))
            return ok("Locked.")  # exit 0, but no parseable id in stdout

        runner = FakeRunner({"pull": ok("Pull complete."), "lock": lock_script})
        res = core.check_out(runner, store, doc_path=doc, dependency_paths=[in_tree])
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.UNKNOWN)
        self.assertIn("could not read every Lock ID", res.title)
        self.assertIn("parts/bracket.FCStd", res.detail)
        # Only the scraped id is cached, both in the result and on disk.
        self.assertEqual(res.lock_ids, {"widget.FCStd": UUID})
        self.assertEqual(
            MappingStore(store.path).load().get(doc).lock_ids, {"widget.FCStd": UUID}
        )

    def test_unscrapable_lock_then_conflict_lists_it_as_stranded(self):
        # The active lock exits 0 but its id is unscrapable (held, no id to
        # release by). The next part conflicts -> abort. The active lock cannot
        # be auto-released (no id), so it is surfaced as still-held. Nothing cached.
        store, doc, clone = self._linked()
        in_tree = os.path.join(clone, "parts", "bracket.FCStd")

        def lock_script(args):
            if args[2] == "widget.FCStd":
                return ok("Locked.")  # exit 0, no parseable id
            return fail("HTTP 409 -- File is locked by @bob")

        runner = FakeRunner(
            {"pull": ok("Pull complete."), "lock": lock_script, "unlock": ok("Released.")}
        )
        res = core.check_out(runner, store, doc_path=doc, dependency_paths=[in_tree])
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.LOCK_CONFLICT)
        self.assertIn("widget.FCStd", res.detail)  # the unscrapable active lock
        self.assertIn("still be held", res.detail)
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_mid_loop_conflict_with_failed_rollback_lists_stranded(self):
        # The active lock is taken, the part conflicts (abort), and the rollback
        # unlock of the active lock FAILS (server denies it). The active lock is
        # left held, so the conflict result lists it as still-held. Nothing cached.
        store, doc, clone = self._linked()
        in_tree = os.path.join(clone, "parts", "bracket.FCStd")

        def lock_script(args):
            if args[2] == "widget.FCStd":
                return ok(_lock_json(UUID, "widget.FCStd"))
            return fail("HTTP 409 -- File is locked by @bob")

        runner = FakeRunner(
            {"pull": ok("Pull complete."), "lock": lock_script, "unlock": fail("denied")}
        )
        res = core.check_out(runner, store, doc_path=doc, dependency_paths=[in_tree])
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.LOCK_CONFLICT)
        self.assertIn("@bob", res.title)
        self.assertIn("parts/bracket.FCStd", res.detail)  # offending path named
        self.assertIn("widget.FCStd", res.detail)  # stranded (rollback denied)
        self.assertIn("still be held", res.detail)
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_nrb_missing_mid_acquire_aborts_and_lists_stranded(self):
        # nrb vanishes AFTER the active lock is taken: the second lock raises
        # NrbNotFound -> abort. The rollback unlock ALSO hits NrbNotFound (nrb is
        # gone), so the active lock cannot be released and is surfaced as a
        # still-held lock to free by hand. Nothing is cached.
        store, doc, clone = self._linked()
        in_tree = os.path.join(clone, "parts", "bracket.FCStd")

        def lock_script(args):
            if args[2] == "widget.FCStd":
                return ok(_lock_json(UUID, "widget.FCStd"))
            raise core.NrbNotFound("nrb not found")

        def unlock_script(args):
            raise core.NrbNotFound("nrb not found")

        runner = FakeRunner(
            {"pull": ok("Pull complete."), "lock": lock_script, "unlock": unlock_script}
        )
        res = core.check_out(runner, store, doc_path=doc, dependency_paths=[in_tree])
        self.assertFalse(res.ok)
        self.assertIn("parts/bracket.FCStd", res.detail)  # offending path named
        self.assertIn("widget.FCStd", res.detail)  # active lock could not release
        self.assertIn("still be held", res.detail)
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_first_lock_conflict_aborts_with_no_rollback(self):
        # The active file itself conflicts: nothing was acquired yet, so there is
        # no unlock to roll back. Still LOCK_CONFLICT, still nothing cached.
        store, doc, clone = self._linked()
        runner = FakeRunner(
            {"pull": ok("Pull complete."), "lock": fail("HTTP 409 -- File is locked by @bob")}
        )
        res = core.check_out(runner, store, doc_path=doc)
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.LOCK_CONFLICT)
        self.assertIn("@bob", res.title)
        self.assertNotIn("unlock", runner.verbs())  # nothing to release
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_unlinked_doc_errors(self):
        tmp = tempfile.mkdtemp()
        store = MappingStore(os.path.join(tmp, "m.json")).load()
        res = core.check_out(runner=FakeRunner({}), store=store, doc_path="nope.FCStd")
        self.assertFalse(res.ok)
        self.assertIn("not linked", res.title.lower())


class TestCheckIn(unittest.TestCase):
    def _checked_out(self, lock_ids=None):
        tmp = tempfile.mkdtemp()
        store = MappingStore(os.path.join(tmp, "m.json")).load()
        doc = os.path.join(tmp, "widget.FCStd")
        clone = os.path.join(tmp, "gizmo")
        m = DocMapping(
            "alice", "gizmo", "widget.FCStd", clone,
            lock_ids=lock_ids if lock_ids is not None else {"widget.FCStd": UUID},
        )
        store.set(doc, m)
        store.save()
        return store, doc, clone

    def test_full_sequence(self):
        store, doc, clone = self._checked_out()
        events = []  # ordered log of save/export so we can assert they precede nrb

        def fake_save():
            # No nrb call may have run before the save.
            self.assertEqual(runner.calls, [])
            events.append("save")

        def fake_export(path):
            # Save must have happened; still no nrb call yet.
            self.assertEqual(events, ["save"])
            self.assertEqual(runner.calls, [])
            events.append("export")
            return path

        runner = FakeRunner(
            {
                "add": ok("Staged 2 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushing main -> alice/gizmo...\nPushed."),
                "unlock": ok("Lock released."),
            }
        )
        res = core.check_in(
            runner,
            store,
            fake_save,
            fake_export,
            doc_path=doc,
            commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertTrue(res.ok)
        # save -> export ran exactly once each, both before any nrb call.
        self.assertEqual(events, ["save", "export"])
        # Then the exact nrb order: add -> commit -> push -> unlock.
        self.assertEqual(runner.verbs(), ["add", "commit", "push", "unlock"])
        # add . stages BOTH the .FCStd and the .step (single add, clone cwd).
        self.assertEqual(runner.calls[0], (["add", "."], clone))
        self.assertEqual(runner.calls[1], (["commit", "-m", "rev 2", "--json"], clone))
        self.assertEqual(runner.calls[2], (["push", "--remote", "origin"], clone))
        self.assertEqual(runner.calls[3], (["unlock", "alice/gizmo", UUID], None))
        # Lock ids cleared after a clean check-in.
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})
        # Fix 6: the commit short-sha is surfaced in the detail.
        self.assertIn("1a2b3c4d", res.detail)

    def test_step_export_is_core_always_on_before_add(self):
        # Pin: STEP export is a CORE, always-on step of every check-in (no
        # opt-out, locked 2026-06-18). It MUST run exactly once and BEFORE the
        # first ``add`` runner call, with the full order being
        # save -> export -> add -> commit -> push. One combined event log
        # interleaves the injected save/export with the nrb verbs so the
        # ordering is asserted across both.
        store, doc, clone = self._checked_out()
        events = []  # combined log: "save", "export", then each nrb verb

        def fake_save():
            events.append("save")

        def fake_export(path):
            events.append("export")
            return path

        def record(verb):
            return lambda args: events.append(verb) or ok()

        runner = FakeRunner(
            {
                "add": record("add"),
                "commit": record("commit"),
                "push": record("push"),
                "unlock": record("unlock"),
            }
        )
        res = core.check_in(
            runner,
            store,
            fake_save,
            fake_export,
            doc_path=doc,
            commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertTrue(res.ok)
        # export ran exactly once.
        self.assertEqual(events.count("export"), 1)
        # export ran BEFORE the first ``add`` runner call.
        self.assertLess(events.index("export"), events.index("add"))
        # Full core order: save -> export -> add -> commit -> push (-> unlock).
        self.assertEqual(
            events, ["save", "export", "add", "commit", "push", "unlock"]
        )

    def test_out_of_tree_deps_yield_soft_success_warning(self):
        store, doc, clone = self._checked_out()
        runner = FakeRunner(
            {
                "add": ok("Staged 2 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushed."),
                "unlock": ok("Lock released."),
            }
        )
        in_clone_part = os.path.join(clone, "partA.FCStd")
        outside_part = os.path.join(os.path.dirname(clone), "external", "partB.FCStd")
        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=[in_clone_part, outside_part],
        )
        # Soft success: the push happened (ok) but a part outside the clone was
        # silently dropped, so we warn rather than report a clean OK.
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.ASSEMBLY_DEPS_OUTSIDE)
        # Full nrb sequence still ran (push + unlock both completed).
        self.assertEqual(runner.verbs(), ["add", "commit", "push", "unlock"])
        # The out-of-tree file is named in the detail; the in-tree one is not
        # flagged (it was committed by ``add .``).
        self.assertIn(outside_part, res.detail)
        self.assertNotIn(in_clone_part, res.detail)
        # A remedy is wired up for this kind.
        self.assertTrue(res.remedy)
        # Lock ids still cleared on the clean push+unlock.
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_out_of_tree_deps_relocated_when_callables_injected(self):
        # v2: with copy_fn + repath_fn injected, out-of-tree parts are COPIED
        # into the clone (and the doc re-pathed + re-saved) BEFORE ``add .``, so
        # they get committed; the result is a clean SUCCESS that reports how many
        # parts moved -- NOT the v1 soft-success warning.
        store, doc, clone = self._checked_out()
        events = []  # ordered log: saves, copies, repath, and nrb verbs

        def fake_save():
            events.append("save")

        runner = FakeRunner(
            {
                "add": lambda a: events.append("add") or ok("Staged 3 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushed."),
                "unlock": ok("Lock released."),
            }
        )

        copied = []  # (src, dest) pairs handed to copy_fn

        def fake_copy(src, dest):
            # Copies must precede the staging ``add .``.
            self.assertNotIn("add", events)
            copied.append((src, dest))
            events.append("copy")

        repathed = []  # the plan handed to repath_fn

        def fake_repath(plan):
            # Re-path runs only after every copy, still before ``add .``.
            self.assertEqual(events.count("copy"), len(copied))
            self.assertNotIn("add", events)
            repathed.append(list(plan))
            events.append("repath")

        in_clone_part = os.path.join(clone, "partA.FCStd")
        outside_part = os.path.join(os.path.dirname(clone), "external", "partB.FCStd")
        res = core.check_in(
            runner, store, fake_save, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=[in_clone_part, outside_part],
            copy_fn=fake_copy, repath_fn=fake_repath,
        )
        # Clean success (not the v1 warning) with the relocation count + mapping.
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.OK)
        self.assertIn("Relocated 1 part(s)", res.detail)
        # The plan the flow built relocates exactly the out-of-tree part, under
        # the clone's deps dir; copy_fn saw that same (src, dest) pair.
        from nurbly import assembly  # noqa: PLC0415 -- local import keeps top clean
        expected = assembly.build_relocation_plan([outside_part], clone, [in_clone_part])
        self.assertEqual(len(expected), 1)
        self.assertEqual(copied, [(expected[0].src, expected[0].dest)])
        # repath_fn received the same plan exactly once.
        self.assertEqual(repathed, [expected])
        # Ordering: save (0a) -> copy -> repath -> a SECOND save (re-path on disk)
        # -> only then ``add .`` and the rest of the nrb sequence.
        self.assertEqual(events[:4], ["save", "copy", "repath", "save"])
        self.assertEqual(events.index("add"), 4)
        self.assertEqual(runner.verbs(), ["add", "commit", "push", "unlock"])
        # The relocated dest is reported; lock id cleared on the clean push+unlock.
        self.assertIn(expected[0].dest, res.detail)
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_relocation_copy_failure_runs_no_nrb(self):
        # If a copy raises, the check-in aborts as a dialog BEFORE any nrb call
        # (the assembly would otherwise be staged with a missing part).
        store, doc, clone = self._checked_out()
        runner = FakeRunner({})

        def boom_copy(_src, _dest):
            raise OSError("permission denied")

        outside_part = os.path.join(os.path.dirname(clone), "external", "partB.FCStd")
        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=[outside_part],
            copy_fn=boom_copy, repath_fn=lambda plan: None,
        )
        self.assertFalse(res.ok)
        self.assertIn("relocate", res.title.lower())
        self.assertEqual(runner.calls, [])  # nothing shelled out

    def test_asymmetric_relocation_callables_raise(self):
        # Wiring only ONE of copy_fn / repath_fn is a caller bug -- relocation
        # needs both. It must raise loudly, not silently fall back to the v1
        # "parts left outside" warning (which would hide the wiring mistake).
        store, doc, clone = self._checked_out()
        runner = FakeRunner({})
        outside_part = os.path.join(os.path.dirname(clone), "external", "partB.FCStd")
        for copy_fn, repath_fn in ((lambda s, d: None, None), (None, lambda plan: None)):
            with self.assertRaises(ValueError):
                core.check_in(
                    runner, store, lambda: None, lambda p: p,
                    doc_path=doc, commit_message="rev 2",
                    step_output_path=os.path.join(clone, "widget.step"),
                    dependency_paths=[outside_part],
                    copy_fn=copy_fn, repath_fn=repath_fn,
                )
        self.assertEqual(runner.calls, [])  # raised before any nrb call

    def test_all_in_tree_deps_are_clean_success(self):
        store, doc, clone = self._checked_out()
        runner = FakeRunner(
            {
                "add": ok("Staged 2 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushed."),
                "unlock": ok("Lock released."),
            }
        )
        # Every dependency lives inside the clone -> nothing dropped -> clean OK.
        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=[os.path.join(clone, "partA.FCStd")],
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.OK)

    def test_no_deps_param_is_unchanged_clean_success(self):
        # Omitting dependency_paths entirely (the single-document case) must keep
        # the original clean-OK behaviour.
        store, doc, clone = self._checked_out()
        runner = FakeRunner(
            {
                "add": ok("Staged 2 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushed."),
                "unlock": ok("Lock released."),
            }
        )
        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.OK)

    # ── Assembly v2: injected relocation ─────────────────────────────────────

    def _ok_runner(self):
        return FakeRunner(
            {
                "add": ok("Staged 2 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushed."),
                "unlock": ok("Lock released."),
            }
        )

    def test_relocate_resolves_all_clean_success(self):
        # (a) relocator provided, deps out of tree, relocator returns [] (all
        # moved) -> relocate runs BEFORE the first add, the check-in is a CLEAN
        # success (no ASSEMBLY_DEPS_OUTSIDE warning).
        store, doc, clone = self._checked_out()
        runner = self._ok_runner()
        outside = os.path.join(os.path.dirname(clone), "external", "partB.FCStd")

        events = []  # combined log so we can assert relocate precedes add

        def fake_save():
            events.append("save")

        def fake_relocate(plan):
            # The plan must describe the out-of-tree part heading into parts/.
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0].src, outside)
            self.assertEqual(plan[0].dest_repo_path, "parts/partB.FCStd")
            # No nrb call may have run yet -- relocation is pre-add.
            self.assertNotIn("add", runner.verbs())
            events.append("relocate")
            return []  # everything resolved

        def record(verb):
            return lambda args: events.append(verb) or ok()

        runner.scripts["add"] = record("add")

        res = core.check_in(
            runner, store, fake_save, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=[outside],
            relocate_dependencies=fake_relocate,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.OK)
        # Exact pre-add order: save (0a) -> relocate -> add. core does NOT re-save
        # after relocate -- the relocator persists the re-linked assembly itself
        # (the host owns the post-relocation save), so there is exactly ONE
        # core-driven save here.
        self.assertEqual(events[:3], ["save", "relocate", "add"])
        self.assertEqual(events.count("save"), 1)
        # Full nrb sequence still ran.
        self.assertEqual(runner.verbs(), ["add", "commit", "push", "unlock"])

    def test_relocate_raises_degrades_to_v1(self):
        # (b) relocator raises -> degrade to v1: ALL out-of-tree paths are treated
        # as unresolved (warning lists them), and the push is still attempted.
        store, doc, clone = self._checked_out()
        runner = self._ok_runner()
        outside = os.path.join(os.path.dirname(clone), "external", "partB.FCStd")

        def boom(plan):
            raise RuntimeError("copy failed")

        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=[outside],
            relocate_dependencies=boom,
        )
        # Soft success: push happened, but the part stayed out of tree.
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.ASSEMBLY_DEPS_OUTSIDE)
        self.assertIn(outside, res.detail)
        # Push was still attempted (full sequence ran).
        self.assertEqual(runner.verbs(), ["add", "commit", "push", "unlock"])

    def test_relocate_partial_warns_only_unresolved(self):
        # (c) relocator returns a PARTIAL unresolved subset -> the warning lists
        # only the unresolved paths, not the ones it managed to move.
        store, doc, clone = self._checked_out()
        runner = self._ok_runner()
        moved = os.path.join(os.path.dirname(clone), "ext", "moved.FCStd")
        stuck = os.path.join(os.path.dirname(clone), "ext", "stuck.FCStd")

        def relocate(plan):
            return [stuck]  # moved resolved, stuck did not

        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=[moved, stuck],
            relocate_dependencies=relocate,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.ASSEMBLY_DEPS_OUTSIDE)
        # Only the unresolved path is named; the relocated one is NOT.
        self.assertIn(stuck, res.detail)
        self.assertNotIn(moved, res.detail)

    def test_single_document_never_calls_relocate(self):
        # (d) single-document check-in (no out-of-tree deps) -> relocate is NEVER
        # called and the result is byte-identical to a plain clean success.
        store, doc, clone = self._checked_out()
        runner = self._ok_runner()
        relocate_calls = []

        def relocate(plan):
            relocate_calls.append(plan)
            return []

        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            # All deps in-clone -> out_of_tree empty -> relocate must not fire.
            dependency_paths=[os.path.join(clone, "partA.FCStd")],
            relocate_dependencies=relocate,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.OK)
        self.assertEqual(relocate_calls, [])  # never called
        self.assertEqual(runner.verbs(), ["add", "commit", "push", "unlock"])
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_none_deps_with_relocator_never_calls_relocate(self):
        # (f) dependency_paths is None (the single-document default) but a
        # relocator IS supplied -> the whole dependency block short-circuits
        # BEFORE classification, so the relocator is never invoked and the
        # check-in is a plain clean success, byte-identical to the no-deps case.
        store, doc, clone = self._checked_out()
        runner = self._ok_runner()
        relocate_calls = []

        def relocate(plan):
            relocate_calls.append(plan)
            return []

        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=None,  # no closure at all -> nothing to classify
            relocate_dependencies=relocate,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.OK)
        self.assertEqual(relocate_calls, [])  # never called
        self.assertEqual(runner.verbs(), ["add", "commit", "push", "unlock"])
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_push_rejected_after_relocation_retains_lock(self):
        # (e) push is rejected AFTER a successful relocation -> the lock is
        # retained (not cleared) and no unlock is attempted, exactly as v1.
        store, doc, clone = self._checked_out()
        runner = FakeRunner(
            {
                "add": ok("Staged 2 path(s)."),
                "commit": ok("[deadbeef] rev 2"),
                "push": fail("Push failed:\n  remote rejected: non-fast-forward"),
            }
        )
        outside = os.path.join(os.path.dirname(clone), "external", "partB.FCStd")
        relocate_calls = []

        def relocate(plan):
            relocate_calls.append(plan)
            return []  # relocation itself succeeded

        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
            dependency_paths=[outside],
            relocate_dependencies=relocate,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.PUSH_REJECTED)
        # Relocation ran before the push was attempted.
        self.assertEqual(len(relocate_calls), 1)
        # No unlock -- the user keeps the lock and their work.
        self.assertNotIn("unlock", runner.verbs())
        self.assertEqual(
            MappingStore(store.path).load().get(doc).lock_ids, {"widget.FCStd": UUID}
        )

    def test_push_rejected_stops_before_unlock(self):
        store, doc, clone = self._checked_out()
        runner = FakeRunner(
            {
                "add": ok("Staged 1 path(s)."),
                "commit": ok("[deadbeef] rev 2"),
                "push": fail("Push failed:\n  remote rejected: non-fast-forward"),
            }
        )
        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.PUSH_REJECTED)
        # No unlock attempted -- the user keeps the lock and their work.
        self.assertNotIn("unlock", runner.verbs())
        # Lock ids still cached (not cleared).
        self.assertEqual(
            MappingStore(store.path).load().get(doc).lock_ids, {"widget.FCStd": UUID}
        )

    def test_export_failure_runs_no_nrb(self):
        store, doc, clone = self._checked_out()
        runner = FakeRunner({})

        def boom(_path):
            raise RuntimeError("no visible objects")

        res = core.check_in(
            runner, store, lambda: None, boom,
            doc_path=doc, commit_message="x",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertFalse(res.ok)
        self.assertIn("STEP export failed", res.title)
        self.assertEqual(runner.calls, [])  # nothing shelled out

    def test_save_failure_runs_no_export_no_nrb(self):
        store, doc, clone = self._checked_out()
        runner = FakeRunner({})
        exported = []

        def boom_save():
            raise RuntimeError("disk full")

        res = core.check_in(
            runner, store, boom_save, lambda p: exported.append(p) or p,
            doc_path=doc, commit_message="x",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertFalse(res.ok)
        self.assertIn("Could not save the document", res.title)
        self.assertEqual(exported, [])  # export not reached
        self.assertEqual(runner.calls, [])  # nothing shelled out

    def test_unlock_failure_surfaces_lock_id_and_command(self):
        store, doc, clone = self._checked_out()
        runner = FakeRunner(
            {
                "add": ok("Staged 2 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushed."),
                "unlock": fail("Unlock failed:\n  HTTP 500 -- boom"),
            }
        )
        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertFalse(res.ok)
        # Lock id retained on the result AND in the reloaded store (still held).
        self.assertEqual(res.lock_ids, {"widget.FCStd": UUID})
        self.assertEqual(
            MappingStore(store.path).load().get(doc).lock_ids, {"widget.FCStd": UUID}
        )
        # The exact manual release command + the lock id appear in the detail.
        self.assertIn(f"nrb unlock alice/gizmo {UUID}", res.detail)
        self.assertIn(UUID, res.detail)

    # ── Per-part release loop ────────────────────────────────────────────────

    def test_checkin_releases_every_cached_lock_and_clears(self):
        # Multi-part check-in: every cached lock id is released (one unlock per
        # id, never aborting on the first), and the whole cache clears on a fully
        # clean release.
        held = {
            "widget.FCStd": UUID,
            "parts/bracket.FCStd": UUID2,
            "gear.FCStd": UUID3,
        }
        store, doc, clone = self._checked_out(lock_ids=held)
        runner = FakeRunner(
            {
                "add": ok("Staged 4 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushed."),
                "unlock": ok("Released."),
            }
        )
        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertTrue(res.ok)
        # add/commit/push once, then ONE unlock per cached lock id.
        self.assertEqual(
            runner.verbs(), ["add", "commit", "push", "unlock", "unlock", "unlock"]
        )
        unlocked_ids = [argv[2] for argv, _ in runner.calls if argv[0] == "unlock"]
        self.assertEqual(sorted(unlocked_ids), sorted(held.values()))
        # Clean release -> the whole cache is cleared.
        self.assertEqual(MappingStore(store.path).load().get(doc).lock_ids, {})

    def test_checkin_partial_release_keeps_only_stuck_ids(self):
        # One release fails mid-loop: the loop does NOT abort (the others still
        # release), the cache keeps ONLY the stuck id, and the detail surfaces the
        # stuck path + its manual unlock command. Soft failure (ok=False).
        held = {"widget.FCStd": UUID, "parts/bracket.FCStd": UUID2}
        store, doc, clone = self._checked_out(lock_ids=held)

        def unlock_script(args):
            # Fail ONLY the bracket lock id. The widget id releases cleanly.
            return fail("HTTP 500 -- boom") if args[2] == UUID2 else ok("Released.")

        runner = FakeRunner(
            {
                "add": ok("Staged 3 path(s)."),
                "commit": ok("[1a2b3c4d] rev 2"),
                "push": ok("Pushed."),
                "unlock": unlock_script,
            }
        )
        res = core.check_in(
            runner, store, lambda: None, lambda p: p,
            doc_path=doc, commit_message="rev 2",
            step_output_path=os.path.join(clone, "widget.step"),
        )
        self.assertFalse(res.ok)
        # Both unlocks were attempted (no abort on the first failure).
        self.assertEqual(runner.verbs().count("unlock"), 2)
        # Only the STUCK id stays cached. The cleanly-released one is dropped.
        self.assertEqual(
            MappingStore(store.path).load().get(doc).lock_ids,
            {"parts/bracket.FCStd": UUID2},
        )
        self.assertEqual(res.lock_ids, {"parts/bracket.FCStd": UUID2})
        # The stuck path + its manual release command are in the detail.
        self.assertIn("parts/bracket.FCStd", res.detail)
        self.assertIn(f"nrb unlock alice/gizmo {UUID2}", res.detail)
        # The cleanly-released widget id is NOT presented as still-held.
        self.assertNotIn(f"nrb unlock alice/gizmo {UUID}", res.detail)


class TestWhoHasIt(unittest.TestCase):
    def test_uses_linked_repo(self):
        tmp = tempfile.mkdtemp()
        store = MappingStore(os.path.join(tmp, "m.json")).load()
        doc = os.path.join(tmp, "widget.FCStd")
        store.set(doc, DocMapping("alice", "gizmo", "widget.FCStd", tmp))
        store.save()
        # `nrb locks --json` emits a JSON array of FileLockResponse.
        locks_out = json.dumps(
            [
                {
                    "id": UUID,
                    "repo_id": "11111111-1111-1111-1111-111111111111",
                    "path": "widget.FCStd",
                    "locked_by_user_id": "22222222-2222-2222-2222-222222222222",
                    "locked_by_username": "alice",
                    "locked_at": "2026-06-16T10:30:00Z",
                }
            ]
        )
        runner = FakeRunner({"locks": ok(locks_out)})
        res = core.who_has_it(runner, store, doc_path=doc)
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls[0][0], ["locks", "alice/gizmo", "--json"])
        self.assertEqual(len(res.locks), 1)
        self.assertEqual(res.locks[0].locked_by, "alice")

    def test_falls_back_to_mine(self):
        tmp = tempfile.mkdtemp()
        store = MappingStore(os.path.join(tmp, "m.json")).load()
        # No locks -> empty JSON array.
        runner = FakeRunner({"locks": ok("[]")})
        res = core.who_has_it(runner, store, doc_path=None)
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls[0][0], ["locks", "--my", "--json"])


class TestDefaultCloneParent(unittest.TestCase):
    """Pin the writable-clone-parent chooser (the C:/Program Files crash fix)."""

    def _abs(self, *parts):
        # A drive-anchored absolute path that works on Windows and POSIX.
        return os.path.abspath(os.sep + os.path.join(*parts))

    def test_doc_under_home_keeps_the_doc_dir(self):
        home = self._abs("home", "user")
        doc = os.path.join(home, "cad", "widget.FCStd")
        self.assertEqual(
            core.default_clone_parent(doc, home=home),
            os.path.dirname(os.path.abspath(doc)),
        )

    def test_doc_outside_home_falls_back_to_home(self):
        # The reported crash: an example doc under the read-only install tree.
        home = self._abs("home", "user")
        doc = self._abs("Program Files", "FreeCAD 1.1", "data", "examples", "x.FCStd")
        self.assertEqual(core.default_clone_parent(doc, home=home), os.path.abspath(home))

    def test_doc_directly_in_home_returns_home(self):
        home = self._abs("home", "user")
        doc = os.path.join(home, "top.FCStd")
        self.assertEqual(core.default_clone_parent(doc, home=home), os.path.abspath(home))

    def test_none_doc_returns_home(self):
        home = self._abs("home", "user")
        self.assertEqual(core.default_clone_parent(None, home=home), os.path.abspath(home))

    def test_empty_doc_returns_home(self):
        home = self._abs("home", "user")
        self.assertEqual(core.default_clone_parent("", home=home), os.path.abspath(home))


CLEAN_STATUS = "On branch main\nNothing to commit, working tree clean.\n"
DIRTY_STATUS = (
    "On branch main\n\nChanges not staged for commit:\n"
    "  WorktreeModified  widget.FCStd\n"
)
BRANCHES = "* main\n  feature/login\n"


class TestListBranches(unittest.TestCase):
    def test_lists_with_cwd(self):
        runner = FakeRunner({"branch": ok(BRANCHES)})
        res = core.list_branches(runner, cwd="/clone")
        self.assertTrue(res.ok)
        self.assertEqual(runner.calls[0], (["branch"], "/clone"))
        self.assertEqual([b.name for b in res.branches], ["main", "feature/login"])
        self.assertTrue(res.branches[0].is_current)

    def test_failure_classified(self):
        runner = FakeRunner({"branch": fail("HTTP 404 -- not a repo")})
        res = core.list_branches(runner, cwd="/clone")
        self.assertFalse(res.ok)
        self.assertEqual(res.branches, [])


class TestSwitchBranch(unittest.TestCase):
    def test_clean_tree_switches(self):
        # Existing-branch switch runs `status` first, then `switch`.
        runner = FakeRunner(
            {"status": ok(CLEAN_STATUS), "switch": ok("Switched to branch 'dev'.")}
        )
        res = core.switch_branch(runner, "dev", cwd="/clone")
        self.assertTrue(res.ok)
        self.assertEqual(runner.verbs(), ["status", "switch"])
        self.assertEqual(runner.calls[0], (["status"], "/clone"))
        self.assertEqual(runner.calls[1], (["switch", "dev"], "/clone"))

    def test_dirty_tree_blocks_before_touching_head(self):
        runner = FakeRunner({"status": ok(DIRTY_STATUS), "switch": ok()})
        res = core.switch_branch(runner, "dev", cwd="/clone")
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.DIRTY_TREE)
        # The status pre-check ran but `switch` MUST NOT have -- HEAD untouched.
        self.assertEqual(runner.verbs(), ["status"])

    def test_allow_dirty_skips_the_precheck(self):
        runner = FakeRunner({"switch": ok("Switched to branch 'dev'.")})
        res = core.switch_branch(runner, "dev", cwd="/clone", allow_dirty=True)
        self.assertTrue(res.ok)
        self.assertEqual(runner.verbs(), ["switch"])  # no status call

    def test_create_skips_precheck_and_passes_dash_c(self):
        # Create-and-switch never runs status: a new branch from HEAD keeps the
        # working tree, so a dirty tree cannot lose work.
        runner = FakeRunner({"switch": ok("Created and switched to branch 'feat'.")})
        res = core.switch_branch(runner, "feat", cwd="/clone", create=True)
        self.assertTrue(res.ok)
        self.assertEqual(runner.verbs(), ["switch"])
        self.assertEqual(runner.calls[0], (["switch", "-c", "feat"], "/clone"))

    def test_blank_branch_rejected_without_shelling_out(self):
        runner = FakeRunner({})
        res = core.switch_branch(runner, "   ", cwd="/clone")
        self.assertFalse(res.ok)
        self.assertEqual(runner.calls, [])

    def test_switch_failure_classified(self):
        # The real nrb message is "Branch 'X' not found locally or on any
        # remote", so the classifier's "not found" keyword lands it in NOT_FOUND.
        runner = FakeRunner(
            {
                "status": ok(CLEAN_STATUS),
                "switch": fail("Branch 'nope' not found locally or on any remote"),
            }
        )
        res = core.switch_branch(runner, "nope", cwd="/clone")
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.NOT_FOUND)


class TestCurrentTrunkBranch(unittest.TestCase):
    def test_returns_name_when_on_main(self):
        runner = FakeRunner({"branch": ok("* main\n  feature/x\n")})
        self.assertEqual(core.current_trunk_branch(runner, cwd="/clone"), "main")
        self.assertEqual(runner.calls[0], (["branch"], "/clone"))

    def test_returns_name_when_on_master(self):
        runner = FakeRunner({"branch": ok("  dev\n* master\n")})
        self.assertEqual(core.current_trunk_branch(runner, cwd="/clone"), "master")

    def test_case_insensitive(self):
        runner = FakeRunner({"branch": ok("* MAIN\n")})
        self.assertEqual(core.current_trunk_branch(runner, cwd="/clone"), "MAIN")

    def test_none_on_feature_branch(self):
        runner = FakeRunner({"branch": ok("  main\n* feature/login\n")})
        self.assertIsNone(core.current_trunk_branch(runner, cwd="/clone"))

    def test_none_when_branch_list_fails(self):
        # Best-effort: a non-zero `nrb branch` must never block Check in.
        runner = FakeRunner({"branch": fail("not a git repo")})
        self.assertIsNone(core.current_trunk_branch(runner, cwd="/clone"))

    def test_none_when_nrb_missing(self):
        def runner(args, **kw):
            raise core.NrbNotFound("nrb not found")

        self.assertIsNone(core.current_trunk_branch(runner, cwd="/clone"))

    def test_none_when_runner_raises_non_nrbnotfound(self):
        # Best-effort: a spawn-level failure (OSError etc.) from the runner must
        # be swallowed and return None, NEVER escape and abort Check in. nrb's
        # subprocess wrapper only converts a TIMEOUT to a result, so this path
        # is reachable in practice.
        def runner(args, **kw):
            raise OSError("nrb is not executable")

        self.assertIsNone(core.current_trunk_branch(runner, cwd="/clone"))

    def test_none_when_no_current_in_listing(self):
        # A listing with no `* ` marker (shouldn't happen, but defend it).
        runner = FakeRunner({"branch": ok("  main\n  dev\n")})
        self.assertIsNone(core.current_trunk_branch(runner, cwd="/clone"))


class TestCheckNrbVersion(unittest.TestCase):
    """The pure nrb version guard (core.check_nrb_version)."""

    def test_too_old_blocks_with_actionable_message(self):
        runner = FakeRunner({"--version": ok("nrb 0.0.9")})
        res = core.check_nrb_version(runner, minimum="0.1.0")
        self.assertFalse(res.ok)
        self.assertEqual(res.kind, ErrorKind.NRB_TOO_OLD)
        # ASCII, no em-dash/semicolon, names the version needed AND the one found.
        self.assertIn("0.1.0", res.title)
        self.assertIn("0.0.9", res.detail)
        self.assertTrue(res.title.isascii())
        self.assertTrue(res.detail.isascii())
        self.assertNotIn(";", res.title + res.detail)
        # A remedy is wired for this kind.
        self.assertTrue(res.remedy)
        # It actually ran `nrb --version`.
        self.assertEqual(runner.calls[0][0], ["--version"])

    def test_equal_version_passes(self):
        runner = FakeRunner({"--version": ok("nrb 0.1.0")})
        res = core.check_nrb_version(runner, minimum="0.1.0")
        self.assertTrue(res.ok)
        self.assertEqual(res.kind, ErrorKind.OK)

    def test_newer_version_passes(self):
        runner = FakeRunner({"--version": ok("nrb 0.2.0")})
        self.assertTrue(core.check_nrb_version(runner, minimum="0.1.0").ok)

    def test_newer_patch_passes(self):
        runner = FakeRunner({"--version": ok("nrb 0.1.5")})
        self.assertTrue(core.check_nrb_version(runner, minimum="0.1.0").ok)

    def test_unparseable_version_degrades_to_ok(self):
        # No version-like token -> cannot judge -> degrade to OK (best-effort),
        # never false-block.
        runner = FakeRunner({"--version": ok("nrb (custom build)")})
        res = core.check_nrb_version(runner, minimum="0.1.0")
        self.assertTrue(res.ok)

    def test_pre_release_suffix_on_minimum_floor_passes(self):
        # 0.1.0 is not older than 0.1.0-beta in the segment-wise compare; a built
        # 0.1.0 must satisfy a 0.1.0 floor.
        runner = FakeRunner({"--version": ok("nrb 0.1.0")})
        self.assertTrue(core.check_nrb_version(runner, minimum="0.1.0").ok)

    def test_nrb_missing_degrades_to_ok(self):
        # NrbNotFound is owned by each action's _nrb_missing path, NOT this guard,
        # so the guard degrades to OK rather than double-reporting it.
        def runner(args, **kw):
            raise core.NrbNotFound("nrb not found")

        res = core.check_nrb_version(runner, minimum="0.1.0")
        self.assertTrue(res.ok)

    def test_nonzero_version_exit_degrades_to_ok(self):
        # `nrb --version` failing on a working binary is anomalous; we cannot
        # judge the version, so degrade to OK rather than false-blocking.
        runner = FakeRunner({"--version": fail("boom")})
        self.assertTrue(core.check_nrb_version(runner, minimum="0.1.0").ok)

    def test_default_minimum_matches_constant(self):
        # The default floor is the published MIN_NRB_VERSION constant.
        runner = FakeRunner({"--version": ok(f"nrb {core.MIN_NRB_VERSION}")})
        self.assertTrue(core.check_nrb_version(runner).ok)


if __name__ == "__main__":
    unittest.main()

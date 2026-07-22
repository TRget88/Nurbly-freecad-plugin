# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.assembly -- dependency split + relocation planning.

Pure Python, no FreeCAD. Exercises classify_dependencies with paths under and
outside a clone dir, the active-doc exclusion, dedupe, empties, and the
case-insensitive (Windows) normalisation; plus repo_relative_path and BOTH
relocation planners: plan_relocation (flat parts/ layout with deterministic
numeric suffixing) and build_relocation_plan (deps/ subdir layout with stable
hash-based collision disambiguation).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly.assembly import (  # noqa: E402
    RELOCATION_SUBDIR,
    build_relocation_plan,
    classify_dependencies,
    plan_relocation,
    repo_relative_path,
)

# A clone root and an unrelated dir on the same drive (so commonpath works).
CLONE = os.path.abspath(os.path.join("C:" + os.sep, "work", "gizmo"))
OUTSIDE_DIR = os.path.abspath(os.path.join("C:" + os.sep, "elsewhere"))


def _in_clone(*parts):
    return os.path.join(CLONE, *parts)


def _outside(*parts):
    return os.path.join(OUTSIDE_DIR, *parts)


class TestClassifyDependencies(unittest.TestCase):
    def test_all_in_tree(self):
        active = _in_clone("asm.FCStd")
        deps = [_in_clone("partA.FCStd"), _in_clone("sub", "partB.FCStd")]
        res = classify_dependencies(active, deps, CLONE)
        self.assertEqual(res.in_tree, deps)
        self.assertEqual(res.out_of_tree, [])

    def test_all_out_of_tree(self):
        active = _in_clone("asm.FCStd")
        deps = [_outside("partA.FCStd"), _outside("lib", "partB.FCStd")]
        res = classify_dependencies(active, deps, CLONE)
        self.assertEqual(res.in_tree, [])
        self.assertEqual(res.out_of_tree, deps)

    def test_mixed(self):
        active = _in_clone("asm.FCStd")
        inside = _in_clone("partA.FCStd")
        outside = _outside("partB.FCStd")
        res = classify_dependencies(active, [inside, outside], CLONE)
        self.assertEqual(res.in_tree, [inside])
        self.assertEqual(res.out_of_tree, [outside])

    def test_active_doc_excluded(self):
        # The active doc must never appear in either list, even if it is passed
        # in among the deps (and even via an equivalent non-normalised spelling).
        active = _in_clone("asm.FCStd")
        weird_active = os.path.join(CLONE, ".", "asm.FCStd")
        dep = _in_clone("partA.FCStd")
        res = classify_dependencies(active, [weird_active, dep], CLONE)
        self.assertEqual(res.in_tree, [dep])
        self.assertEqual(res.out_of_tree, [])

    def test_empty_deps(self):
        res = classify_dependencies(_in_clone("asm.FCStd"), [], CLONE)
        self.assertEqual(res.in_tree, [])
        self.assertEqual(res.out_of_tree, [])

    def test_none_deps_treated_as_empty(self):
        res = classify_dependencies(_in_clone("asm.FCStd"), None, CLONE)
        self.assertEqual(res.in_tree, [])
        self.assertEqual(res.out_of_tree, [])

    def test_skips_empty_path_entries(self):
        active = _in_clone("asm.FCStd")
        dep = _in_clone("partA.FCStd")
        res = classify_dependencies(active, ["", dep, None], CLONE)
        self.assertEqual(res.in_tree, [dep])
        self.assertEqual(res.out_of_tree, [])

    def test_dedupe_keeps_first_spelling(self):
        active = _in_clone("asm.FCStd")
        first = _in_clone("partA.FCStd")
        dup = os.path.join(CLONE, ".", "partA.FCStd")  # same canonical path
        res = classify_dependencies(active, [first, dup], CLONE)
        # Counted once, and the FIRST spelling is the one kept.
        self.assertEqual(res.in_tree, [first])
        self.assertEqual(res.out_of_tree, [])

    def test_case_insensitive_on_windows(self):
        # normcase lowercases on Windows, so a dep that differs only in case from
        # the clone dir must still be detected as in-tree. On POSIX normcase is a
        # no-op, so we only assert the Windows behaviour there.
        active = _in_clone("asm.FCStd")
        upper_dep = os.path.join(CLONE.upper(), "PARTA.FCStd")
        res = classify_dependencies(active, [upper_dep], CLONE)
        if sys.platform.startswith("win"):
            self.assertEqual(res.in_tree, [upper_dep])
            self.assertEqual(res.out_of_tree, [])
        else:
            # POSIX is case-sensitive; the upper-cased clone path is a different
            # directory, so the dep is (correctly) out of tree.
            self.assertEqual(res.in_tree, [])
            self.assertEqual(res.out_of_tree, [upper_dep])

    def test_sibling_dir_is_out_of_tree(self):
        # A dir whose name shares the clone's prefix (gizmo vs gizmo-extras) must
        # NOT be mistaken for being inside the clone -- commonpath, not startswith.
        active = _in_clone("asm.FCStd")
        sibling = os.path.abspath(
            os.path.join("C:" + os.sep, "work", "gizmo-extras", "partA.FCStd")
        )
        res = classify_dependencies(active, [sibling], CLONE)
        self.assertEqual(res.in_tree, [])
        self.assertEqual(res.out_of_tree, [sibling])


class TestRepoRelativePath(unittest.TestCase):
    def test_file_directly_in_clone(self):
        # A part at the clone root -> its basename, forward-slash.
        self.assertEqual(
            repo_relative_path(_in_clone("widget.FCStd"), CLONE), "widget.FCStd"
        )

    def test_nested_file_uses_forward_slashes(self):
        # A nested part -> repo-relative path is ALWAYS forward-slash regardless
        # of the OS separator the input used.
        self.assertEqual(
            repo_relative_path(_in_clone("parts", "bracket.FCStd"), CLONE),
            "parts/bracket.FCStd",
        )

    def test_out_of_tree_is_none(self):
        # A part outside the clone has no repo-relative path (it is not in repo).
        self.assertIsNone(repo_relative_path(_outside("partA.FCStd"), CLONE))

    def test_sibling_dir_is_none(self):
        # A sibling dir sharing the clone's name prefix is NOT under the clone.
        sibling = os.path.abspath(
            os.path.join("C:" + os.sep, "work", "gizmo-extras", "partA.FCStd")
        )
        self.assertIsNone(repo_relative_path(sibling, CLONE))

    def test_clone_root_itself_is_none(self):
        # The clone dir itself has no repo-relative file component.
        self.assertIsNone(repo_relative_path(CLONE, CLONE))

    def test_empty_inputs_are_none(self):
        self.assertIsNone(repo_relative_path("", CLONE))
        self.assertIsNone(repo_relative_path(_in_clone("widget.FCStd"), ""))

    def test_non_normalised_spelling_resolves(self):
        # A `.`-laden but equivalent path still resolves to the clean repo path.
        weird = os.path.join(CLONE, ".", "parts", "bracket.FCStd")
        self.assertEqual(repo_relative_path(weird, CLONE), "parts/bracket.FCStd")

    def test_case_insensitive_under_clone_on_windows(self):
        # normcase folds case on Windows, so an upper-cased clone prefix still
        # counts as under the clone. The returned rel uses the input's spelling.
        upper = os.path.join(CLONE.upper(), "parts", "bracket.FCStd")
        rel = repo_relative_path(upper, CLONE)
        if sys.platform.startswith("win"):
            self.assertEqual(rel, "parts/bracket.FCStd")
        else:
            # POSIX is case-sensitive. The upper-cased prefix is a different dir.
            self.assertIsNone(rel)


REPO_PATH = "asm.FCStd"  # the assembly's repo-relative path (flat layout ignores it)


def _parts(*parts):
    """A path under the clone's flat parts/ destination dir."""
    return os.path.join(CLONE, "parts", *parts)


class TestPlanRelocation(unittest.TestCase):
    def test_empty_input(self):
        # No out-of-tree parts -> no targets. Planner never raises.
        active = _in_clone("asm.FCStd")
        self.assertEqual(plan_relocation(active, [], CLONE, REPO_PATH), [])

    def test_none_input_treated_as_empty(self):
        active = _in_clone("asm.FCStd")
        self.assertEqual(plan_relocation(active, None, CLONE, REPO_PATH), [])

    def test_skips_empty_path_entries(self):
        active = _in_clone("asm.FCStd")
        src = _outside("partA.FCStd")
        plan = plan_relocation(active, ["", src, None], CLONE, REPO_PATH)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].src, src)

    def test_single_out_of_tree(self):
        active = _in_clone("asm.FCStd")
        src = _outside("partA.FCStd")
        plan = plan_relocation(active, [src], CLONE, REPO_PATH)
        self.assertEqual(len(plan), 1)
        t = plan[0]
        self.assertEqual(t.src, src)
        self.assertEqual(t.dest_abs, _parts("partA.FCStd"))
        # dest_repo_path is ALWAYS forward-slash, regardless of OS sep.
        self.assertEqual(t.dest_repo_path, "parts/partA.FCStd")
        self.assertFalse(t.already_present)

    def test_basename_collision_suffixes_deterministically(self):
        # Two different sources share a basename -> the 2nd (and 3rd) are
        # suffixed before the extension, in INPUT order.
        active = _in_clone("asm.FCStd")
        a = _outside("dirA", "part.FCStd")
        b = _outside("dirB", "part.FCStd")
        c = _outside("dirC", "part.FCStd")
        plan = plan_relocation(active, [a, b, c], CLONE, REPO_PATH)
        self.assertEqual([t.dest_repo_path for t in plan],
                         ["parts/part.FCStd", "parts/part-2.FCStd", "parts/part-3.FCStd"])
        # The original srcs are preserved 1:1 in input order.
        self.assertEqual([t.src for t in plan], [a, b, c])
        # dest_abs mirrors the suffixed names.
        self.assertEqual(plan[1].dest_abs, _parts("part-2.FCStd"))

    def test_same_source_deduped_no_false_collision(self):
        # The SAME canonical source listed twice is de-duped (one target), so it
        # must NOT be mistaken for a basename collision and suffixed.
        active = _in_clone("asm.FCStd")
        src = _outside("part.FCStd")
        dup = os.path.join(OUTSIDE_DIR, ".", "part.FCStd")  # same canonical path
        plan = plan_relocation(active, [src, dup], CLONE, REPO_PATH)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].dest_repo_path, "parts/part.FCStd")
        self.assertEqual(plan[0].src, src)  # first spelling kept

    def test_already_present_when_src_is_the_dest(self):
        # A prior run already moved the part to parts/<name>; this run sees it at
        # its computed dest, so it is flagged already_present (re-link, no copy).
        active = _in_clone("asm.FCStd")
        src = _parts("partA.FCStd")  # lives exactly where the plan would put it
        plan = plan_relocation(active, [src], CLONE, REPO_PATH)
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan[0].already_present)
        self.assertEqual(plan[0].dest_repo_path, "parts/partA.FCStd")

    def test_active_doc_never_relocated(self):
        # The assembly itself must never be a relocation target, even if it leaks
        # into the out-of-tree list via an equivalent non-normalised spelling.
        active = _in_clone("asm.FCStd")
        weird_active = os.path.join(CLONE, ".", "asm.FCStd")
        src = _outside("partA.FCStd")
        plan = plan_relocation(active, [weird_active, src], CLONE, REPO_PATH)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].src, src)

    def test_case_insensitive_collision_on_windows(self):
        # normcase folds case on Windows, so PART.FCStd and part.FCStd collide
        # there (and get suffixed); on POSIX they are distinct basenames.
        active = _in_clone("asm.FCStd")
        a = _outside("dirA", "part.FCStd")
        b = _outside("dirB", "PART.FCStd")
        plan = plan_relocation(active, [a, b], CLONE, REPO_PATH)
        if sys.platform.startswith("win"):
            self.assertEqual(plan[1].dest_repo_path, "parts/PART-2.FCStd")
        else:
            self.assertEqual(plan[1].dest_repo_path, "parts/PART.FCStd")

    def test_sibling_directory_source(self):
        # A source in a dir that shares the clone's name prefix (gizmo-extras vs
        # gizmo) is out of tree and relocates into the clone's flat parts/ dir.
        active = _in_clone("asm.FCStd")
        sibling = os.path.abspath(
            os.path.join("C:" + os.sep, "work", "gizmo-extras", "partA.FCStd")
        )
        plan = plan_relocation(active, [sibling], CLONE, REPO_PATH)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].src, sibling)
        self.assertEqual(plan[0].dest_abs, _parts("partA.FCStd"))
        self.assertEqual(plan[0].dest_repo_path, "parts/partA.FCStd")
        self.assertFalse(plan[0].already_present)

    def test_extensionless_basename_suffixing(self):
        # A name with no extension suffixes at the end (part -> part-2).
        active = _in_clone("asm.FCStd")
        a = _outside("dirA", "part")
        b = _outside("dirB", "part")
        plan = plan_relocation(active, [a, b], CLONE, REPO_PATH)
        self.assertEqual(plan[0].dest_repo_path, "parts/part")
        self.assertEqual(plan[1].dest_repo_path, "parts/part-2")

    def test_dotdot_basename_is_skipped(self):
        # A source whose basename resolves to ".." must NOT produce a parts/..
        # destination that escapes the parts/ dir. The planner skips it.
        active = _in_clone("asm.FCStd")
        tricky = os.path.join(OUTSIDE_DIR, "..")  # basename is ".."
        good = _outside("partA.FCStd")
        plan = plan_relocation(active, [tricky, good], CLONE, REPO_PATH)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].src, good)

    def test_repo_path_is_accepted_but_ignored(self):
        # repo_path is accepted for host-wiring symmetry but the flat parts/
        # layout does not consume it. Two runs that differ ONLY in repo_path must
        # therefore produce byte-identical plans (same dests, same flags, same
        # order), proving the argument never leaks into the output.
        active = _in_clone("asm.FCStd")
        a = _outside("dirA", "part.FCStd")
        b = _outside("dirB", "part.FCStd")
        c = _outside("widget.FCStd")
        srcs = [a, b, c]
        plan_one = plan_relocation(active, srcs, CLONE, "asm.FCStd")
        plan_two = plan_relocation(active, srcs, CLONE, "deeply/nested/asm.FCStd")
        # Guard against a future regression that silently drops every target:
        # an all-empty equality would pass vacuously, hiding the drop.
        self.assertEqual(len(plan_one), 3)
        self.assertEqual(plan_one, plan_two)

    def test_suffixed_collision_target_is_never_already_present(self):
        # A target whose dest was SUFFIXED by a real basename collision can never
        # be already_present: already_present needs the src to canonically equal
        # the dest, but the src that keyed n=2 has basename "part.FCStd" while its
        # dest is "part-2.FCStd", so they cannot match. This pins that the
        # suffix-vs-already_present interaction is unreachable, so no code path
        # needs to handle it (an earlier test claimed to cover it but could not).
        active = _in_clone("asm.FCStd")
        a = _outside("dirA", "part.FCStd")
        b = _outside("dirB", "part.FCStd")  # real collision: dest suffixed to part-2
        plan = plan_relocation(active, [a, b], CLONE, REPO_PATH)
        self.assertEqual(plan[1].dest_repo_path, "parts/part-2.FCStd")
        self.assertFalse(plan[1].already_present)


def _canon(path):
    """Mirror assembly._canon so tests can predict the plan's canonical paths."""
    return os.path.normcase(os.path.abspath(path))


def _deps_dir():
    """The canonical ``<clone>/deps`` root the plan places copies under."""
    return os.path.join(_canon(CLONE), RELOCATION_SUBDIR)


class TestBuildRelocationPlan(unittest.TestCase):
    def test_empty_is_no_op(self):
        # The single-document case: no out-of-tree deps -> no relocation at all.
        self.assertEqual(build_relocation_plan([], CLONE), [])
        self.assertEqual(build_relocation_plan(None, CLONE, []), [])

    def test_skips_empty_path_entries(self):
        out = [_outside("partA.FCStd"), "", None]
        plan = build_relocation_plan(out, CLONE)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].src, _canon(_outside("partA.FCStd")))

    def test_single_out_of_tree_goes_under_deps(self):
        src = _outside("partA.FCStd")
        plan = build_relocation_plan([src], CLONE)
        self.assertEqual(len(plan), 1)
        op = plan[0]
        # Canonical src, and dest is <clone>/deps/<basename> (also canonical).
        self.assertEqual(op.src, _canon(src))
        self.assertEqual(op.dest, os.path.join(_deps_dir(), "parta.fcstd"
                                               if sys.platform.startswith("win")
                                               else "partA.FCStd"))
        # Dest always lands under the deps dir of the clone.
        self.assertTrue(op.dest.startswith(_deps_dir() + os.sep))

    def test_mixed_only_plans_out_of_tree(self):
        # in_tree parts are passed for symmetry but must never appear in the plan
        # -- they're already committed by ``nrb add .``.
        inside = _in_clone("partA.FCStd")
        outside = _outside("partB.FCStd")
        plan = build_relocation_plan([outside], CLONE, in_tree_paths=[inside])
        self.assertEqual([op.src for op in plan], [_canon(outside)])

    def test_in_tree_path_passed_as_out_is_skipped(self):
        # Defensive: a path that is really inside the clone is never relocated,
        # even if it is (mistakenly) handed in as out-of-tree.
        inside = _in_clone("sub", "partA.FCStd")
        plan = build_relocation_plan([inside], CLONE, in_tree_paths=[inside])
        self.assertEqual(plan, [])

    def test_basename_collision_gets_distinct_dests(self):
        # Two DIFFERENT sources share a basename -> they must not overwrite each
        # other in <clone>/deps. The first keeps the plain name; the second is
        # disambiguated with a stable hash before the extension.
        a = _outside("frame", "bolt.FCStd")
        b = _outside("panel", "bolt.FCStd")
        plan = build_relocation_plan([a, b], CLONE)
        self.assertEqual(len(plan), 2)
        dests = [op.dest for op in plan]
        # Distinct dests, both under the deps dir, both keep the .fcstd suffix.
        self.assertNotEqual(dests[0], dests[1])
        self.assertEqual(len(set(dests)), 2)
        for d in dests:
            self.assertTrue(d.startswith(_deps_dir() + os.sep))
            self.assertTrue(d.lower().endswith(".fcstd"))
        # First source keeps the plain basename; second is the hashed variant.
        base = "bolt.fcstd" if sys.platform.startswith("win") else "bolt.FCStd"
        self.assertEqual(dests[0], os.path.join(_deps_dir(), base))
        self.assertNotEqual(dests[1], os.path.join(_deps_dir(), base))

    def test_collision_disambiguation_is_stable(self):
        # The disambiguating hash is derived from the SOURCE, so the same set of
        # sources yields the same dests regardless of how many times we build it.
        a = _outside("frame", "bolt.FCStd")
        b = _outside("panel", "bolt.FCStd")
        first = build_relocation_plan([a, b], CLONE)
        again = build_relocation_plan([a, b], CLONE)
        self.assertEqual(first, again)

    def test_hashed_dest_collision_falls_back_to_counter(self):
        # Extraordinarily contrived: one source's PLAIN basename is exactly the
        # HASHED dest a colliding source would otherwise take. The plan must
        # STILL give every source a distinct dest -- the counter fallback after
        # the hash guarantees two sources can never be assigned the same dest.
        from nurbly.assembly import _parent_hash  # noqa: PLC0415
        a = _outside("frame", "bolt.FCStd")           # -> deps/bolt.fcstd
        c = _outside("panel", "bolt.FCStd")           # collides with a -> hashed
        tag_c = _parent_hash(_canon(c))
        # p's basename is literally the hashed dest c would otherwise take.
        p = _outside("seed", f"bolt.{tag_c}.FCStd")   # -> deps/bolt.<tag_c>.fcstd
        plan = build_relocation_plan([a, p, c], CLONE)
        dests = [op.dest for op in plan]
        self.assertEqual(len(dests), 3)
        self.assertEqual(len(set(dests)), 3)          # all distinct, no overwrite
        for d in dests:
            self.assertTrue(d.startswith(_deps_dir() + os.sep))
        # c could not take the plain hashed name (p already holds it).
        self.assertNotIn(dests[2], dests[:2])

    def test_idempotent_recopy_same_source(self):
        # Re-copying the IDENTICAL source path (even via an equivalent spelling)
        # is idempotent: counted once, same dest -> repeating the plan is safe.
        src = _outside("partA.FCStd")
        dup = os.path.join(OUTSIDE_DIR, ".", "partA.FCStd")  # same canonical path
        plan = build_relocation_plan([src, dup], CLONE)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].src, _canon(src))

    def test_distinct_basenames_keep_plain_names(self):
        # No collision -> each part keeps its own basename under deps; no hashing.
        a = _outside("frame", "bolt.FCStd")
        c = _outside("panel", "nut.FCStd")
        plan = build_relocation_plan([a, c], CLONE)
        names = [os.path.basename(op.dest) for op in plan]
        self.assertEqual(
            names,
            ["bolt.fcstd", "nut.fcstd"]
            if sys.platform.startswith("win")
            else ["bolt.FCStd", "nut.FCStd"],
        )


if __name__ == "__main__":
    unittest.main()

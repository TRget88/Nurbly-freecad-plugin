# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

# tests/test_relink.py
"""Unit tests for nurbly.host.relink -- the assembly v2 host relocator.

relink.py manipulates FreeCAD documents but imports NO FreeCAD (it is fully
duck-typed against doc/object attributes), so its defensive contract IS
testable here with fake doc/link objects and real temp files. We load the
module by FILE PATH via importlib, NOT through the nurbly.host package, to
honor the CI invariant that the pure tests never import nurbly.host (whose
other modules do pull in FreeCAD).
"""

import importlib.util
import os
import tempfile
import unittest
from collections import namedtuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_RELINK_PATH = os.path.join(os.path.dirname(_HERE), "nurbly", "host", "relink.py")
_spec = importlib.util.spec_from_file_location("nurbly_relink_under_test", _RELINK_PATH)
relink = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relink)


# Stand-in for a RelocationTarget (relink reads src/dest_abs/already_present).
Target = namedtuple("Target", ["src", "dest_abs", "already_present"])


class FakeLink:
    """A fake FreeCAD link DocumentObject carrying a writable FileName."""

    def __init__(self, file_name):
        self.FileName = file_name
        self.touched = False

    def touch(self):
        self.touched = True


class FakeDoc:
    """A fake FreeCAD Document: an Objects attr plus recompute/save spies.

    Objects is stored verbatim (no list coercion) so tests can hand it a
    non-iterable or a raising iterator to exercise the defensive guards.
    """

    def __init__(self, objects):
        self.Objects = objects
        self.recomputed = False
        self.saved = False

    def recompute(self):
        self.recomputed = True

    def save(self):
        self.saved = True


class TestCollectDependencyRecords(unittest.TestCase):
    def test_picks_filename_bearing_links_only(self):
        link = FakeLink("/ext/partA.FCStd")
        plain = object()  # no FileName attribute
        records = relink.collect_dependency_records(FakeDoc([link, plain]))
        self.assertEqual(len(records), 1)
        self.assertIs(records[0].obj, link)
        self.assertEqual(records[0].file_name, "/ext/partA.FCStd")

    def test_none_doc_is_empty(self):
        self.assertEqual(relink.collect_dependency_records(None), [])

    def test_non_iterable_objects_is_empty(self):
        self.assertEqual(relink.collect_dependency_records(FakeDoc(42)), [])

    def test_iterator_that_raises_is_empty(self):
        class _Boom:
            def __iter__(self):
                raise RuntimeError("boom")

        # A non-TypeError from iteration must still degrade to [] (never raise).
        self.assertEqual(relink.collect_dependency_records(FakeDoc(_Boom())), [])


class TestRelocateDependencies(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.clone = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _make_src(self, name):
        ext = os.path.join(self.clone, "ext")
        os.makedirs(ext, exist_ok=True)
        path = os.path.join(ext, name)
        with open(path, "wb") as fh:
            fh.write(b"FCStd")
        return path

    def test_clean_copy_and_relink(self):
        src = self._make_src("partA.FCStd")
        dest = os.path.join(self.clone, "parts", "partA.FCStd")
        link = FakeLink(src)
        doc = FakeDoc([link])
        unresolved = relink.relocate_dependencies(doc, [Target(src, dest, False)])
        self.assertEqual(unresolved, [])
        self.assertTrue(os.path.isfile(dest))  # copied in
        self.assertEqual(link.FileName, dest)  # re-linked at the new path
        self.assertTrue(doc.saved)  # the host owns the post-relocation save

    def test_no_matching_link_rolls_back_the_copy(self):
        # B1: a copy with no matching link element is ROLLED BACK so a
        # present-but-unlinked orphan is never committed. src lands unresolved.
        src = self._make_src("partA.FCStd")
        dest = os.path.join(self.clone, "parts", "partA.FCStd")
        doc = FakeDoc([FakeLink("/some/other.FCStd")])  # nothing matches src
        unresolved = relink.relocate_dependencies(doc, [Target(src, dest, False)])
        self.assertEqual(unresolved, [src])
        self.assertFalse(os.path.exists(dest))  # the orphan copy was removed

    def test_already_present_skips_copy_but_relinks(self):
        # already_present: the part already sits at its dest (a prior run moved
        # it). No re-copy, but the link is still re-pointed.
        dest = os.path.join(self.clone, "parts", "partA.FCStd")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(b"FCStd")
        link = FakeLink(dest)
        doc = FakeDoc([link])
        unresolved = relink.relocate_dependencies(doc, [Target(dest, dest, True)])
        self.assertEqual(unresolved, [])
        self.assertTrue(os.path.isfile(dest))  # not deleted

    def test_copy_failure_is_unresolved_not_a_crash(self):
        missing = os.path.join(self.clone, "ext", "gone.FCStd")  # never created
        dest = os.path.join(self.clone, "parts", "gone.FCStd")
        doc = FakeDoc([FakeLink(missing)])
        unresolved = relink.relocate_dependencies(doc, [Target(missing, dest, False)])
        self.assertEqual(unresolved, [missing])
        self.assertFalse(os.path.exists(dest))

    def test_empty_plan(self):
        self.assertEqual(relink.relocate_dependencies(FakeDoc([]), []), [])

    def test_none_plan(self):
        self.assertEqual(relink.relocate_dependencies(FakeDoc([]), None), [])

    def test_non_iterable_plan(self):
        # A non-iterable plan must degrade to [] (never raise).
        self.assertEqual(relink.relocate_dependencies(FakeDoc([]), 5), [])


class FakeExtObj:
    """An object living inside a part document: a Name plus its owning Document.

    Models a real FreeCAD DocumentObject as seen through a link's LinkedObject:
    the only bits relink reads are ``Name`` (the match key, preserved across a
    file copy) and ``Document.FileName`` (which file it lives in).
    """

    def __init__(self, name, doc):
        self.Name = name
        self.Document = doc


class FakePartDoc:
    """A part document: a FileName plus its Objects (what open_document returns)."""

    def __init__(self, file_name, objects):
        self.FileName = file_name
        self.Objects = objects


class FakeAppLink:
    """A native App::Link: NO FileName, references an external object only.

    This is the shape a real FreeCAD App::Link presents (confirmed by the
    headless harness): the external reference lives in LinkedObject, there is no
    FileName attribute to rewrite. relink must re-point LinkedObject instead.
    """

    def __init__(self, linked):
        self.LinkedObject = linked
        self.touched = False

    def touch(self):
        self.touched = True


class TestRelocateLinkedObject(unittest.TestCase):
    """The LinkedObject re-bind path (native App::Link, no FileName)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.clone = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _make_src(self, name):
        ext = os.path.join(self.clone, "ext")
        os.makedirs(ext, exist_ok=True)
        path = os.path.join(ext, name)
        with open(path, "wb") as fh:
            fh.write(b"FCStd")
        return path

    def _copy_doc(self, dest, obj_name):
        # The document open_document() yields for the relocated copy: it carries
        # an object whose Name matches the original (FreeCAD preserves Name).
        obj = FakeExtObj(obj_name, None)
        doc = FakePartDoc(dest, [obj])
        obj.Document = doc
        return doc, obj

    def test_collect_tags_linkedobject_style(self):
        src = "/ext/partA.FCStd"
        link = FakeAppLink(FakeExtObj("Box", FakePartDoc(src, [])))
        records = relink.collect_dependency_records(FakeDoc([link]))
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].via_file_name)  # rebind, not FileName-rewrite
        self.assertEqual(records[0].file_name, src)  # the linked object's owning file

    def test_applink_rebinds_to_the_relocated_copy(self):
        src = self._make_src("partA.FCStd")
        dest = os.path.join(self.clone, "parts", "partA.FCStd")
        ext_box = FakeExtObj("Box", FakePartDoc(src, []))
        link = FakeAppLink(ext_box)
        asm = FakeDoc([link])
        copy_doc, copy_box = self._copy_doc(dest, "Box")
        unresolved = relink.relocate_dependencies(
            asm, [Target(src, dest, False)], open_document=lambda _p: copy_doc
        )
        self.assertEqual(unresolved, [])
        self.assertTrue(os.path.isfile(dest))  # copied in
        self.assertIs(link.LinkedObject, copy_box)  # re-bound to the in-clone object
        self.assertTrue(asm.saved)

    def test_multiple_applinks_to_same_part_all_rebind_opening_once(self):
        # Two separate App::Links reference the SAME part. Both must re-bind, and
        # the copy is opened exactly once (cached per destination).
        src = self._make_src("partA.FCStd")
        dest = os.path.join(self.clone, "parts", "partA.FCStd")
        ext_box = FakeExtObj("Box", FakePartDoc(src, []))
        link_a = FakeAppLink(ext_box)
        link_b = FakeAppLink(ext_box)
        asm = FakeDoc([link_a, link_b])
        copy_doc, copy_box = self._copy_doc(dest, "Box")
        opens = {"n": 0}

        def open_document(_p):
            opens["n"] += 1
            return copy_doc

        unresolved = relink.relocate_dependencies(
            asm, [Target(src, dest, False)], open_document=open_document
        )
        self.assertEqual(unresolved, [])
        self.assertIs(link_a.LinkedObject, copy_box)
        self.assertIs(link_b.LinkedObject, copy_box)
        self.assertEqual(opens["n"], 1)  # opened once, not per-link

    def test_no_opener_cannot_rebind_so_rolls_back(self):
        # An App::Link part with no injected open_document cannot be re-bound, so
        # the just-made copy is rolled back and the source is unresolved.
        src = self._make_src("partA.FCStd")
        dest = os.path.join(self.clone, "parts", "partA.FCStd")
        link = FakeAppLink(FakeExtObj("Box", FakePartDoc(src, [])))
        unresolved = relink.relocate_dependencies(FakeDoc([link]), [Target(src, dest, False)])
        self.assertEqual(unresolved, [src])
        self.assertFalse(os.path.exists(dest))

    def test_name_mismatch_in_copy_rolls_back(self):
        # The copy has no object with the linked Name (should never happen for a
        # real copy, but must fail safe): no re-bind, copy rolled back, unresolved.
        src = self._make_src("partA.FCStd")
        dest = os.path.join(self.clone, "parts", "partA.FCStd")
        link = FakeAppLink(FakeExtObj("Box", FakePartDoc(src, [])))
        copy_doc, _ = self._copy_doc(dest, "SomethingElse")
        unresolved = relink.relocate_dependencies(
            FakeDoc([link]), [Target(src, dest, False)], open_document=lambda _p: copy_doc
        )
        self.assertEqual(unresolved, [src])
        self.assertFalse(os.path.exists(dest))


if __name__ == "__main__":
    unittest.main()

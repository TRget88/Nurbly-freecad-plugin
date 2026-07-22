# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.nrb_runner.locate_nrb -- the binary-location slot.

Pure Python, no FreeCAD. Only covers the explicit-override precedence that the
first-run "locate nrb" flow (Fix 5) feeds the persisted config path into; the
PATH / ~/.cargo/bin fallbacks are environment-dependent and exercised in the
manual smoke test.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly import nrb_runner  # noqa: E402


class TestLocateNrb(unittest.TestCase):
    def test_config_override_is_returned_first(self):
        # A real file passed as config_override wins over PATH / cargo fallback.
        tmp = tempfile.mkdtemp()
        fake_nrb = os.path.join(tmp, "nrb")
        with open(fake_nrb, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        got = nrb_runner.locate_nrb(config_override=fake_nrb)
        self.assertEqual(got, os.path.abspath(fake_nrb))

    def test_nonexistent_override_is_ignored(self):
        # A configured path that no longer exists must not be returned; it falls
        # through to the other resolution slots (which may raise NrbNotFound).
        bogus = os.path.join(tempfile.mkdtemp(), "does-not-exist-nrb")
        try:
            got = nrb_runner.locate_nrb(config_override=bogus)
        except nrb_runner.NrbNotFound:
            return  # acceptable: nothing else on this machine resolved either
        self.assertNotEqual(got, bogus)


if __name__ == "__main__":
    unittest.main()

# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.errors -- failure classification.

Pure Python, no FreeCAD. The sample strings mimic nrb's real error surface:
`bail!("{op}:\\n  {decoded}")` where decoded is `format_server_error`'s
"HTTP <status> -- <inner>" for a generic envelope (server_error.rs). The
PROCESS exit code is 1; the HTTP code lives in the text.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly import errors  # noqa: E402
from nurbly.errors import ErrorKind  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_success(self):
        c = errors.classify(0, "Lock released.", "")
        self.assertEqual(c.kind, ErrorKind.OK)

    def test_lock_conflict_with_holder(self):
        # nrb lock against a locked file: 409 + holder name in the message.
        stderr = "Lock failed:\n  HTTP 409 -- File is locked by @bob"
        c = errors.classify(1, "", stderr)
        self.assertEqual(c.kind, ErrorKind.LOCK_CONFLICT)
        self.assertIn("@bob", c.title)

    def test_lock_conflict_without_holder(self):
        c = errors.classify(1, "", "HTTP 409 -- already locked")
        self.assertEqual(c.kind, ErrorKind.LOCK_CONFLICT)

    def test_not_authenticated_401(self):
        c = errors.classify(1, "", "Repo list failed:\n  HTTP 401 -- unauthorized")
        self.assertEqual(c.kind, ErrorKind.NOT_AUTHENTICATED)

    def test_not_authenticated_text(self):
        # PAT rejected path prints a "Token rejected by the server" chain.
        c = errors.classify(1, "", "Token rejected by the server -- check it was pasted")
        self.assertEqual(c.kind, ErrorKind.NOT_AUTHENTICATED)

    def test_push_rejected(self):
        c = errors.classify(1, "", "Push failed:\n  remote rejected: non-fast-forward")
        self.assertEqual(c.kind, ErrorKind.PUSH_REJECTED)

    def test_forbidden_403(self):
        c = errors.classify(1, "", "HTTP 403 -- Insufficient permissions: required write")
        self.assertEqual(c.kind, ErrorKind.FORBIDDEN)

    def test_forbidden_permission_denied_no_http(self):
        # server_error.rs's STRUCTURED branch (e.g. a lock-release 403) prints
        # "Permission denied." with NO "HTTP 403" token -- must still map to
        # FORBIDDEN, not fall through to UNKNOWN.
        c = errors.classify(1, "", "Lock release failed:\n  Permission denied.")
        self.assertEqual(c.kind, ErrorKind.FORBIDDEN)

    def test_forbidden_insufficient_permissions_no_http(self):
        c = errors.classify(1, "", "Insufficient permissions: required admin")
        self.assertEqual(c.kind, ErrorKind.FORBIDDEN)

    def test_not_found_404(self):
        c = errors.classify(1, "", "Unlock failed:\n  HTTP 404 -- lock not found")
        self.assertEqual(c.kind, ErrorKind.NOT_FOUND)

    def test_unknown_falls_through(self):
        c = errors.classify(1, "", "some unexpected disaster")
        self.assertEqual(c.kind, ErrorKind.UNKNOWN)


class TestRemedies(unittest.TestCase):
    def test_three_mvp_cases_have_remedies(self):
        for kind in (
            ErrorKind.LOCK_CONFLICT,
            ErrorKind.PUSH_REJECTED,
            ErrorKind.NOT_AUTHENTICATED,
        ):
            self.assertIn(kind, errors.REMEDIES)
            self.assertTrue(errors.REMEDIES[kind])


if __name__ == "__main__":
    unittest.main()

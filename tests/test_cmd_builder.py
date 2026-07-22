# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.cmd_builder -- the nrb argv assembler.

Pure Python, no FreeCAD. These pin the exact argv shapes against the clap
definitions in backend/crates/nurbly-cli/src/main.rs (+ feat/nrb-lock for
lock/unlock/locks).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly import cmd_builder  # noqa: E402


class TestOwnerRepo(unittest.TestCase):
    def test_joins(self):
        self.assertEqual(cmd_builder.owner_repo("alice", "gizmo"), "alice/gizmo")

    def test_strips_slashes_and_spaces(self):
        self.assertEqual(cmd_builder.owner_repo(" alice/", "/gizmo "), "alice/gizmo")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            cmd_builder.owner_repo("", "gizmo")

    def test_rejects_slash_in_repo(self):
        with self.assertRaises(ValueError):
            cmd_builder.owner_repo("alice", "a/b")


class TestSimpleVerbs(unittest.TestCase):
    def test_login(self):
        self.assertEqual(cmd_builder.login("nrbpat_abc"), ["login", "--token", "nrbpat_abc"])

    def test_login_rejects_empty(self):
        with self.assertRaises(ValueError):
            cmd_builder.login("   ")

    def test_whoami(self):
        self.assertEqual(cmd_builder.whoami(), ["whoami"])

    def test_version(self):
        # clap's built-in --version flag; the version guard parses its stdout.
        self.assertEqual(cmd_builder.version(), ["--version"])

    def test_repo_list_plain(self):
        # --json: the plugin parses this command's stdout (output_parser).
        self.assertEqual(cmd_builder.repo_list(), ["repo", "list", "--json"])

    def test_repo_list_org(self):
        self.assertEqual(
            cmd_builder.repo_list("acme"), ["repo", "list", "--org", "acme", "--json"]
        )

    def test_clone(self):
        self.assertEqual(cmd_builder.clone("alice", "gizmo"), ["clone", "alice/gizmo"])

    def test_clone_with_dir(self):
        self.assertEqual(
            cmd_builder.clone("alice", "gizmo", "/tmp/g"),
            ["clone", "alice/gizmo", "--directory", "/tmp/g"],
        )

    def test_pull_default_remote(self):
        self.assertEqual(cmd_builder.pull(), ["pull", "--remote", "origin"])

    def test_push_default_remote(self):
        self.assertEqual(cmd_builder.push(), ["push", "--remote", "origin"])

    def test_add_defaults_to_dot(self):
        self.assertEqual(cmd_builder.add(), ["add", "."])

    def test_add_explicit(self):
        self.assertEqual(cmd_builder.add(["a", "b"]), ["add", "a", "b"])

    def test_commit(self):
        # --json: the plugin parses the new commit's sha from this command's
        # stdout (output_parser.extract_commit_sha). The global flag goes last.
        self.assertEqual(cmd_builder.commit("msg"), ["commit", "-m", "msg", "--json"])

    def test_commit_rejects_blank(self):
        with self.assertRaises(ValueError):
            cmd_builder.commit("   ")


class TestLockVerbs(unittest.TestCase):
    def test_lock_minimal(self):
        # --json: core reads the new lock's id from this command's stdout.
        self.assertEqual(
            cmd_builder.lock("alice", "gizmo", "parts/w.FCStd"),
            ["lock", "alice/gizmo", "parts/w.FCStd", "--json"],
        )

    def test_lock_normalises_backslashes_and_leading_slash(self):
        self.assertEqual(
            cmd_builder.lock("alice", "gizmo", "/parts\\w.FCStd"),
            ["lock", "alice/gizmo", "parts/w.FCStd", "--json"],
        )

    def test_lock_with_expiry_and_message(self):
        # --json is the global flag, so it sits AFTER the per-command options.
        self.assertEqual(
            cmd_builder.lock("alice", "gizmo", "w.FCStd", expires_hours=24, message="rev2"),
            ["lock", "alice/gizmo", "w.FCStd", "--expires-hours", "24", "-m", "rev2", "--json"],
        )

    def test_unlock(self):
        # unlock's stdout is never parsed (core only checks its exit code),
        # so it carries NO --json flag.
        uid = "550e8400-e29b-41d4-a716-446655440000"
        self.assertEqual(
            cmd_builder.unlock("alice", "gizmo", f"  {uid}\n"),
            ["unlock", "alice/gizmo", uid],
        )

    def test_locks_repo(self):
        self.assertEqual(
            cmd_builder.locks("alice", "gizmo"), ["locks", "alice/gizmo", "--json"]
        )

    def test_locks_mine(self):
        self.assertEqual(cmd_builder.locks(mine=True), ["locks", "--my", "--json"])

    def test_locks_needs_repo_unless_mine(self):
        with self.assertRaises(ValueError):
            cmd_builder.locks()


class TestBranchVerbs(unittest.TestCase):
    def test_branch_list(self):
        # No --json mode exists; output_parser scrapes the "* main" lines.
        self.assertEqual(cmd_builder.branch_list(), ["branch"])

    def test_switch_existing(self):
        self.assertEqual(cmd_builder.switch("feature"), ["switch", "feature"])

    def test_switch_create(self):
        # -c cuts the branch from HEAD and switches in one step.
        self.assertEqual(
            cmd_builder.switch("feature", create=True), ["switch", "-c", "feature"]
        )

    def test_switch_trims_name(self):
        self.assertEqual(cmd_builder.switch("  feature\n"), ["switch", "feature"])

    def test_switch_rejects_blank(self):
        with self.assertRaises(ValueError):
            cmd_builder.switch("   ")

    def test_status(self):
        self.assertEqual(cmd_builder.status(), ["status"])


if __name__ == "__main__":
    unittest.main()

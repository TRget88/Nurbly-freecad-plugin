# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.output_parser.

Pure Python, no FreeCAD. The sample strings below are JSON matching the serde
serialisation of the nrb Rust response DTOs in
``backend/crates/nurbly-core/src/models/responses.rs`` -- the plugin now runs
the parsed commands with the global ``--json`` flag (see cmd_builder), so a
failure here means either nrb's JSON shape drifted or the parser regressed:

  * lock object  -> FileLockResponse (commands/lock.rs cmd_lock,
        ``serde_json::to_string_pretty(&lock)``): a single JSON object with
        ``id`` / ``path`` / ``locked_by_username`` and optional ``expires_at``
        / ``lock_message`` (serde omits the Options when None).
  * repo list    -> Vec<RepoResponse> (commands/repo.rs RepoCommands::List):
        a JSON array; the parser reads ``owner_slug`` / ``name`` / ``clone_url``.
  * locks array  -> Vec<FileLockResponse> (commands/lock.rs cmd_locks): a JSON
        array of the same lock objects as above.

``nrb commit`` has no ``--json`` mode, so extract_commit_sha still scrapes the
human ``[<sha>] msg`` line.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly import output_parser  # noqa: E402

# `nrb lock alice/gizmo parts/widget.FCStd -m 'rev 2' --json` stdout: a single
# FileLockResponse object (to_string_pretty -> pretty-printed JSON). Only the
# fields the parser consumes are pinned by the asserts; the rest mirror the DTO.
LOCK_STDOUT = json.dumps(
    {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "repo_id": "11111111-1111-1111-1111-111111111111",
        "path": "parts/widget.FCStd",
        "locked_by_user_id": "22222222-2222-2222-2222-222222222222",
        "locked_by_username": "alice",
        "locked_at": "2026-06-16T10:30:00Z",
        "expires_at": "2026-06-23T10:30:00Z",
        "lock_message": "rev 2",
    },
    indent=2,
)

# `nrb repo list --json` stdout: a JSON array of RepoResponse. Extra DTO fields
# are present to prove the parser ignores everything but owner_slug/name/clone_url.
REPO_LIST_STDOUT = json.dumps(
    [
        {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "owner_slug": "alice",
            "owner_kind": "user",
            "name": "gizmo",
            "description": None,
            "is_public": True,
            "default_branch": "main",
            "clone_url": "http://localhost:8090/alice/gizmo.git",
            "license_spdx": None,
        },
        {
            "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "owner_slug": "acme",
            "owner_kind": "org",
            "name": "rocket",
            "description": "the rocket",
            "is_public": False,
            "default_branch": "main",
            "clone_url": "http://localhost:8090/acme/rocket.git",
            "license_spdx": "MIT",
        },
    ]
)

# `nrb locks alice/gizmo --json` stdout: a JSON array of FileLockResponse.
LOCKS_STDOUT = json.dumps(
    [
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "repo_id": "11111111-1111-1111-1111-111111111111",
            "path": "parts/widget.FCStd",
            "locked_by_user_id": "22222222-2222-2222-2222-222222222222",
            "locked_by_username": "alice",
            "locked_at": "2026-06-16T10:30:00Z",
            "expires_at": "2026-06-23T10:30:00Z",
            "lock_message": "working on rev 2",
        }
    ]
)


class TestExtractLockId(unittest.TestCase):
    def test_captures_id(self):
        self.assertEqual(
            output_parser.extract_lock_id(LOCK_STDOUT),
            "550e8400-e29b-41d4-a716-446655440000",
        )

    def test_none_when_id_absent(self):
        # Valid JSON object but no `id` field -> None (caller falls back to locks).
        self.assertIsNone(output_parser.extract_lock_id('{"path": "w.FCStd"}'))

    def test_none_on_empty(self):
        self.assertIsNone(output_parser.extract_lock_id(""))

    def test_none_on_blank(self):
        self.assertIsNone(output_parser.extract_lock_id("   \n  "))

    def test_none_on_malformed_json(self):
        # Never crash on unexpected stdout: malformed JSON -> None.
        self.assertIsNone(output_parser.extract_lock_id("not json at all"))

    def test_none_when_not_an_object(self):
        # A bare JSON array is the wrong shape for a single lock -> None.
        self.assertIsNone(output_parser.extract_lock_id("[]"))


# `nrb commit --json` stdout: the ad-hoc `{ "commit", "short" }` object
# (commands/local_git.rs cmd_commit -> commit_json). The full sha is 40 chars,
# `short` its 8-char prefix.
COMMIT_JSON = json.dumps(
    {
        "commit": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
        "short": "1a2b3c4d",
    },
    indent=2,
)


class TestExtractCommitSha(unittest.TestCase):
    def test_reads_short_from_json(self):
        # Preferred path: read `short` straight from the --json object.
        self.assertEqual(output_parser.extract_commit_sha(COMMIT_JSON), "1a2b3c4d")

    def test_json_without_short_falls_back_to_commit_prefix(self):
        # Only the full sha present -> take its 8-char prefix.
        text = json.dumps({"commit": "abcdef0123456789abcdef0123456789abcdef01"})
        self.assertEqual(output_parser.extract_commit_sha(text), "abcdef01")

    def test_falls_back_to_human_line_for_old_nrb(self):
        # Back-compat: an older nrb with no commit --json mode still prints the
        # human "[<sha>] msg" line, which the scrape fallback reads.
        self.assertEqual(output_parser.extract_commit_sha("[1a2b3c4d] my message"), "1a2b3c4d")

    def test_captures_short_sha(self):
        self.assertEqual(output_parser.extract_commit_sha("[1a2b3c4d] my message"), "1a2b3c4d")

    def test_none_when_absent(self):
        self.assertIsNone(output_parser.extract_commit_sha("Staged 3 path(s)."))

    def test_none_on_empty(self):
        self.assertIsNone(output_parser.extract_commit_sha(""))

    def test_json_array_falls_through_to_scrape(self):
        # A JSON array is the wrong shape for the commit object; with no human
        # line either, the result is None (never raises).
        self.assertIsNone(output_parser.extract_commit_sha("[]"))


class TestParseRepoList(unittest.TestCase):
    def test_parses_rows(self):
        rows = output_parser.parse_repo_list(REPO_LIST_STDOUT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].owner, "alice")
        self.assertEqual(rows[0].repo, "gizmo")
        self.assertEqual(rows[0].owner_repo, "alice/gizmo")
        self.assertEqual(rows[0].clone_url, "http://localhost:8090/alice/gizmo.git")
        self.assertEqual(rows[1].owner_repo, "acme/rocket")
        self.assertEqual(rows[1].clone_url, "http://localhost:8090/acme/rocket.git")

    def test_empty_listing(self):
        # `nrb repo list --json` with no repos serialises an empty array.
        self.assertEqual(output_parser.parse_repo_list("[]"), [])

    def test_blank(self):
        self.assertEqual(output_parser.parse_repo_list(""), [])

    def test_malformed_json(self):
        # Never crash on unexpected stdout -> empty list.
        self.assertEqual(output_parser.parse_repo_list("No repositories found."), [])

    def test_skips_elements_missing_fields(self):
        text = json.dumps(
            [
                {"owner_slug": "alice", "name": "gizmo", "clone_url": "u"},
                {"owner_slug": "alice"},  # missing `name` -> skipped
            ]
        )
        rows = output_parser.parse_repo_list(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].owner_repo, "alice/gizmo")


class TestParseLocks(unittest.TestCase):
    def test_parses_row(self):
        rows = output_parser.parse_locks(LOCKS_STDOUT)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.path, "parts/widget.FCStd")
        self.assertEqual(row.locked_by, "alice")
        self.assertEqual(row.expires, "2026-06-23T10:30:00Z")
        self.assertEqual(row.message, "working on rev 2")  # multi-word survives

    def test_no_active_locks(self):
        # `nrb locks --json` with no locks serialises an empty array.
        self.assertEqual(output_parser.parse_locks("[]"), [])

    def test_blank(self):
        self.assertEqual(output_parser.parse_locks(""), [])

    def test_malformed_json(self):
        self.assertEqual(output_parser.parse_locks("No active locks."), [])

    def test_never_expiry_and_empty_message(self):
        # serde omits expires_at / lock_message when the Options are None.
        # The parser substitutes the literal "never" and an empty string so the
        # UI's table semantics are preserved.
        text = json.dumps(
            [
                {
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "repo_id": "11111111-1111-1111-1111-111111111111",
                    "path": "a.FCStd",
                    "locked_by_user_id": "22222222-2222-2222-2222-222222222222",
                    "locked_by_username": "bob",
                    "locked_at": "2026-06-16T10:30:00Z",
                }
            ]
        )
        rows = output_parser.parse_locks(text)
        self.assertEqual(rows[0].expires, "never")
        self.assertEqual(rows[0].message, "")
        self.assertEqual(rows[0].locked_by, "bob")

    def test_long_path_round_trips(self):
        # JSON carries the path verbatim regardless of length -- the old
        # fixed-width column scrape's failure mode is gone, but pin it anyway.
        long_path = "assemblies/subassembly/really-long-part-name.FCStd"
        text = json.dumps(
            [
                {
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "repo_id": "11111111-1111-1111-1111-111111111111",
                    "path": long_path,
                    "locked_by_user_id": "22222222-2222-2222-2222-222222222222",
                    "locked_by_username": "bob",
                    "locked_at": "2026-06-16T10:30:00Z",
                    "lock_message": "msg here",
                }
            ]
        )
        rows = output_parser.parse_locks(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].path, long_path)
        self.assertEqual(rows[0].locked_by, "bob")
        self.assertEqual(rows[0].expires, "never")
        self.assertEqual(rows[0].message, "msg here")

    def test_skips_elements_missing_fields(self):
        text = json.dumps(
            [
                {"path": "a.FCStd", "locked_by_username": "bob"},
                {"path": "b.FCStd"},  # missing locked_by_username -> skipped
            ]
        )
        rows = output_parser.parse_locks(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].path, "a.FCStd")


# `nrb branch` stdout: one local branch per line, current prefixed "* "
# (commands/local_git.rs cmd_branch). No --json mode -- this is a text scrape.
BRANCH_STDOUT = "* main\n  feature/login\n  release/v1.0\n"


class TestParseBranchList(unittest.TestCase):
    def test_parses_rows_and_marks_current(self):
        rows = output_parser.parse_branch_list(BRANCH_STDOUT)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].name, "main")
        self.assertTrue(rows[0].is_current)
        self.assertEqual(rows[1].name, "feature/login")
        self.assertFalse(rows[1].is_current)
        self.assertEqual(rows[2].name, "release/v1.0")
        self.assertFalse(rows[2].is_current)

    def test_only_one_current(self):
        rows = output_parser.parse_branch_list(BRANCH_STDOUT)
        self.assertEqual([r.name for r in rows if r.is_current], ["main"])

    def test_blank(self):
        self.assertEqual(output_parser.parse_branch_list(""), [])

    def test_skips_blank_lines(self):
        rows = output_parser.parse_branch_list("* main\n\n  dev\n")
        self.assertEqual([r.name for r in rows], ["main", "dev"])

    def test_unprefixed_line_degrades_to_non_current(self):
        # A future reformat that drops the two-space prefix still parses, trimmed.
        rows = output_parser.parse_branch_list("main\nfeature\n")
        self.assertEqual([r.name for r in rows], ["main", "feature"])
        self.assertFalse(any(r.is_current for r in rows))


class TestIsWorkingTreeClean(unittest.TestCase):
    def test_clean_tree(self):
        out = "On branch main\nNothing to commit, working tree clean.\n"
        self.assertTrue(output_parser.is_working_tree_clean(out))

    def test_dirty_tree(self):
        out = (
            "On branch main\n\nChanges not staged for commit:\n"
            "  WorktreeModified  widget.FCStd\n"
        )
        self.assertFalse(output_parser.is_working_tree_clean(out))

    def test_merge_in_progress_is_not_clean(self):
        out = "On branch main\n\nA merge is in progress.  Resolve...\n"
        self.assertFalse(output_parser.is_working_tree_clean(out))

    def test_empty_is_not_clean(self):
        # Fail safe: status always prints at least "On branch x", so empty is
        # anomalous and must NOT be treated as clean.
        self.assertFalse(output_parser.is_working_tree_clean(""))


if __name__ == "__main__":
    unittest.main()

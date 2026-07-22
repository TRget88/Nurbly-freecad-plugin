# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.links -- web-app deep links.

Pure Python, no FreeCAD. These pin the "Get an access key" deep link the Sign
in dialog opens so a mechanical engineer can mint a Personal Access Token in
the browser instead of on the command line. The env var is saved/restored so a
developer's real ``NURBLY_WEB_URL`` never leaks into (or out of) the tests.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly import links  # noqa: E402


class TestLinks(unittest.TestCase):
    def setUp(self):
        # Isolate from any real NURBLY_WEB_URL in the dev's environment.
        self._saved = os.environ.pop("NURBLY_WEB_URL", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("NURBLY_WEB_URL", None)
        else:
            os.environ["NURBLY_WEB_URL"] = self._saved

    # ── web_base ──────────────────────────────────────────────────────────────

    def test_default_when_nothing_set(self):
        self.assertEqual(links.web_base(), links.DEFAULT_WEB_BASE)
        self.assertEqual(links.web_base(), "https://nurbly.com")

    def test_override_wins(self):
        self.assertEqual(
            links.web_base("http://localhost:5173"), "http://localhost:5173"
        )

    def test_env_used_when_no_override(self):
        os.environ["NURBLY_WEB_URL"] = "https://staging.example.com"
        self.assertEqual(links.web_base(), "https://staging.example.com")

    def test_override_beats_env(self):
        os.environ["NURBLY_WEB_URL"] = "https://staging.example.com"
        self.assertEqual(
            links.web_base("http://localhost:5173"), "http://localhost:5173"
        )

    def test_trailing_slash_trimmed(self):
        self.assertEqual(
            links.web_base("http://localhost:5173/"), "http://localhost:5173"
        )

    def test_blank_override_falls_through_to_env(self):
        os.environ["NURBLY_WEB_URL"] = "https://staging.example.com"
        # A whitespace-only override must be skipped, not win and then blank out.
        self.assertEqual(links.web_base("   "), "https://staging.example.com")

    def test_blank_everything_falls_to_default(self):
        os.environ["NURBLY_WEB_URL"] = "   "
        self.assertEqual(links.web_base("  "), links.DEFAULT_WEB_BASE)

    def test_non_string_override_ignored(self):
        # A hand-edited config value of the wrong type must not crash the link.
        self.assertEqual(links.web_base(12345), links.DEFAULT_WEB_BASE)  # type: ignore[arg-type]

    # ── api_keys_url ────────────────────────────────────────────────────────────

    def test_api_keys_url_default(self):
        self.assertEqual(
            links.api_keys_url(), "https://nurbly.com/settings?tab=keys"
        )

    def test_api_keys_url_override(self):
        self.assertEqual(
            links.api_keys_url("http://localhost:5173/"),
            "http://localhost:5173/settings?tab=keys",
        )

    def test_api_keys_path_constant(self):
        # The web app's API Keys tab is SettingsPage ``?tab=keys`` (main.tsx
        # routes /settings -> SettingsPage; the tab key is "keys").
        self.assertEqual(links.API_KEYS_PATH, "/settings?tab=keys")

    # ── branching_guide_url ─────────────────────────────────────────────────────

    def test_branching_guide_url_default(self):
        self.assertEqual(
            links.branching_guide_url(),
            "https://nurbly.com/help#why-not-commit-straight-to-main",
        )

    def test_branching_guide_url_override(self):
        self.assertEqual(
            links.branching_guide_url("http://localhost:5173/"),
            "http://localhost:5173/help#why-not-commit-straight-to-main",
        )

    def test_branching_guide_path_constant(self):
        # The /help page (main.tsx -> HelpPage) renders docs/USER_GUIDE.md; the
        # fragment is the rehype-slug of the "Why not commit straight to main?"
        # heading. If that heading is renamed, this anchor must move with it.
        self.assertEqual(
            links.BRANCHING_GUIDE_PATH, "/help#why-not-commit-straight-to-main"
        )


if __name__ == "__main__":
    unittest.main()

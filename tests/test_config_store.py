# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.config_store -- the plugin's settings file.

Pure Python, no FreeCAD. Uses a tmp file so nothing touches the real
~/.nurbly/nurbly-plugin/config.json.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly import config_store  # noqa: E402
from nurbly.config_store import PluginConfig  # noqa: E402


class TestPluginConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sub", "config.json")

    def test_roundtrip(self):
        cfg = PluginConfig(self.path).load()
        cfg.set_nrb_path("/x/nrb")
        cfg.save()

        reloaded = PluginConfig(self.path).load()
        self.assertEqual(reloaded.get_nrb_path(), "/x/nrb")

    def test_missing_file_is_none(self):
        cfg = PluginConfig(self.path).load()
        self.assertIsNone(cfg.get_nrb_path())

    def test_corrupt_file_not_raised(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not valid json at all")
        cfg = PluginConfig(self.path).load()  # must not raise
        self.assertIsNone(cfg.get_nrb_path())

    def test_last_run_roundtrip(self):
        cfg = PluginConfig(self.path).load()
        cfg.set_last_run(["nrb", "whoami"], 0, stdout="@kirk\n", stderr="")
        cfg.save()

        reloaded = PluginConfig(self.path).load()
        run = reloaded.get_last_run()
        self.assertEqual(run["argv"], ["nrb", "whoami"])
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["stdout"], "@kirk\n")
        self.assertEqual(run["stderr"], "")

    def test_last_run_none_when_unset(self):
        cfg = PluginConfig(self.path).load()
        self.assertIsNone(cfg.get_last_run())

    def test_last_run_overwrites_previous(self):
        cfg = PluginConfig(self.path).load()
        cfg.set_last_run(["nrb", "whoami"], 0)
        cfg.set_last_run(["nrb", "config", "list"], 1, stderr="boom")
        cfg.save()

        run = PluginConfig(self.path).load().get_last_run()
        self.assertEqual(run["argv"], ["nrb", "config", "list"])
        self.assertEqual(run["exit_code"], 1)
        self.assertEqual(run["stderr"], "boom")

    def test_last_run_malformed_is_none(self):
        # A hand-edited / older config whose last_run has the wrong shape must
        # be treated as absent, never raise.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for bad in ('{"last_run": "nope"}', '{"last_run": {"argv": "x"}}',
                    '{"last_run": {"argv": ["a"]}}',  # missing exit_code
                    '{"last_run": {"argv": ["a"], "exit_code": true}}'):  # bool, not int
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(bad)
            cfg = PluginConfig(self.path).load()
            self.assertIsNone(cfg.get_last_run(), bad)

    def test_web_url_none_when_unset(self):
        cfg = PluginConfig(self.path).load()
        self.assertIsNone(cfg.get_web_url())

    def test_web_url_read_from_config(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"web_url": "http://localhost:5173"}')
        cfg = PluginConfig(self.path).load()
        self.assertEqual(cfg.get_web_url(), "http://localhost:5173")

    def test_web_url_blank_or_wrong_type_is_none(self):
        # A hand-edited blank string or non-string must read as unset rather
        # than producing a broken "Get an access key" link.
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for bad in ('{"web_url": "   "}', '{"web_url": 12345}', '{"web_url": true}'):
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(bad)
            cfg = PluginConfig(self.path).load()
            self.assertIsNone(cfg.get_web_url(), bad)

    def test_default_path_requires_home(self):
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        try:
            os.environ.pop("HOME", None)
            os.environ.pop("USERPROFILE", None)
            with self.assertRaises(RuntimeError):
                config_store.default_config_path()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_default_path_points_at_nurbly_dir(self):
        # Post-rebrand the plugin stores its config under ~/.nurbly, not the
        # internal-codename ~/.vrd dir.
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        home = tempfile.mkdtemp()
        try:
            os.environ["HOME"] = home
            os.environ["USERPROFILE"] = home
            self.assertEqual(
                config_store.default_config_path(),
                os.path.join(home, ".nurbly", "nurbly-plugin", "config.json"),
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_legacy_vrd_config_is_migrated_to_nurbly(self):
        # A user who configured the plugin before the rebrand has a config at
        # ~/.vrd/nurbly-plugin/config.json.  default_config_path() must copy it
        # forward to ~/.nurbly on first resolve (non-destructive) so their saved
        # nrb_path survives the upgrade.
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        home = tempfile.mkdtemp()
        try:
            os.environ["HOME"] = home
            os.environ["USERPROFILE"] = home
            legacy = os.path.join(home, ".vrd", "nurbly-plugin", "config.json")
            os.makedirs(os.path.dirname(legacy), exist_ok=True)
            with open(legacy, "w", encoding="utf-8") as fh:
                fh.write('{"schema": 1, "nrb_path": "/legacy/nrb"}')

            resolved = config_store.default_config_path()

            self.assertEqual(
                resolved, os.path.join(home, ".nurbly", "nurbly-plugin", "config.json")
            )
            self.assertTrue(os.path.exists(resolved), "legacy config migrated")
            self.assertEqual(PluginConfig(resolved).load().get_nrb_path(), "/legacy/nrb")
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

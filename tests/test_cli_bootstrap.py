# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for nurbly.cli_bootstrap -- the first-run nrb auto-acquire.

Pure Python, no FreeCAD, no network: every network/filesystem touch is an
injected callable, so these tests pass fakes. Covers platform detection, the
manifest-driven plan, the SHA-256 gate (a tampered download must never land on
disk), and the end-to-end orchestration.
"""

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nurbly import cli_bootstrap as cb  # noqa: E402


class TestDetectTarget(unittest.TestCase):
    def test_os_mapping(self):
        self.assertEqual(cb.detect_target("Linux", "x86_64")[0], "linux")
        self.assertEqual(cb.detect_target("Darwin", "arm64")[0], "macos")
        self.assertEqual(cb.detect_target("Windows", "AMD64")[0], "windows")
        self.assertEqual(cb.detect_target("FreeBSD", "x86_64")[0], "unknown")

    def test_arch_normalisation(self):
        self.assertEqual(cb.detect_target("Linux", "x86_64")[1], "x86_64")
        self.assertEqual(cb.detect_target("Linux", "AMD64")[1], "x86_64")
        self.assertEqual(cb.detect_target("Darwin", "arm64")[1], "aarch64")
        self.assertEqual(cb.detect_target("Linux", "aarch64")[1], "aarch64")
        self.assertEqual(cb.detect_target("Linux", "mips")[1], "unknown")


class TestSelectRelease(unittest.TestCase):
    MANIFEST = {
        "releases": [
            {"os": "linux", "arch": "x86_64", "sha256": "aa", "version": "0.1.0"},
            {"os": "macos", "arch": "aarch64", "sha256": "bb", "version": "0.1.0"},
        ]
    }

    def test_found(self):
        entry = cb.select_release(self.MANIFEST, "macos", "aarch64")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["sha256"], "bb")

    def test_not_found(self):
        self.assertIsNone(cb.select_release(self.MANIFEST, "windows", "x86_64"))

    def test_empty(self):
        self.assertIsNone(cb.select_release({}, "linux", "x86_64"))


class TestUrls(unittest.TestCase):
    def test_manifest_url_strips_trailing_slash(self):
        self.assertEqual(
            cb.manifest_url("https://nurbly.com/"),
            "https://nurbly.com/v1/cli/manifest.json",
        )

    def test_download_url_constructed_without_entry(self):
        self.assertEqual(
            cb.download_url("https://nurbly.com", "linux", "x86_64"),
            "https://nurbly.com/v1/cli/releases/linux/x86_64",
        )

    def test_download_url_relative_entry_joined(self):
        entry = {"download_url": "/v1/cli/releases/linux/x86_64"}
        self.assertEqual(
            cb.download_url("https://nurbly.com/", "linux", "x86_64", entry),
            "https://nurbly.com/v1/cli/releases/linux/x86_64",
        )

    def test_download_url_absolute_entry_used_verbatim(self):
        entry = {"download_url": "https://cdn.example.com/nrb"}
        self.assertEqual(
            cb.download_url("https://nurbly.com", "linux", "x86_64", entry),
            "https://cdn.example.com/nrb",
        )


class TestSha256(unittest.TestCase):
    def test_match_case_insensitive(self):
        data = b"hello nrb"
        digest = hashlib.sha256(data).hexdigest()
        self.assertTrue(cb.verify_sha256(data, digest.upper()))

    def test_mismatch(self):
        self.assertFalse(cb.verify_sha256(b"a", hashlib.sha256(b"b").hexdigest()))

    def test_empty_expected_is_false(self):
        self.assertFalse(cb.verify_sha256(b"a", ""))


class TestInstallPath(unittest.TestCase):
    def test_under_nurbly_bin(self):
        path = cb.nrb_install_path("/home/u")
        self.assertEqual(
            path, os.path.join("/home/u", ".nurbly", "bin", cb._exe_name())
        )
        self.assertIn(".nurbly", path)


class TestPlanFromManifest(unittest.TestCase):
    def _manifest(self, sha="deadbeef"):
        return {
            "releases": [
                {"os": "linux", "arch": "x86_64", "sha256": sha, "version": "0.2.0"}
            ]
        }

    def test_happy(self):
        plan = cb.plan_from_manifest(
            self._manifest(), "/home/u", target=("linux", "x86_64")
        )
        self.assertEqual(plan.version, "0.2.0")
        self.assertEqual(plan.sha256, "deadbeef")
        self.assertTrue(plan.download_url.endswith("/v1/cli/releases/linux/x86_64"))
        self.assertTrue(plan.install_path.endswith(cb._exe_name()))

    def test_unknown_platform_raises(self):
        with self.assertRaises(cb.BootstrapError):
            cb.plan_from_manifest(
                self._manifest(), "/home/u", target=("unknown", "unknown")
            )

    def test_no_build_for_platform_raises(self):
        with self.assertRaises(cb.BootstrapError):
            cb.plan_from_manifest(
                self._manifest(), "/home/u", target=("windows", "aarch64")
            )

    def test_missing_sha_raises(self):
        manifest = {"releases": [{"os": "linux", "arch": "x86_64", "version": "0.2.0"}]}
        with self.assertRaises(cb.BootstrapError):
            cb.plan_from_manifest(manifest, "/home/u", target=("linux", "x86_64"))


class TestAcquireNrb(unittest.TestCase):
    def _plan(self, data):
        return cb.BootstrapPlan(
            version="0.2.0",
            os="linux",
            arch="x86_64",
            download_url="https://nurbly.com/v1/cli/releases/linux/x86_64",
            sha256=hashlib.sha256(data).hexdigest(),
            install_path="/tmp/does-not-matter/nrb",
        )

    def test_happy_path_verifies_writes_and_chmods(self):
        data = b"\x7fELF fake nrb binary"
        plan = self._plan(data)
        written = {}
        chmodded = []

        def fake_get(url, token):
            self.assertEqual(url, plan.download_url)
            self.assertEqual(token, "nrbpat_xyz")
            return data

        def fake_write(path, blob):
            written[path] = blob

        def fake_chmod(path):
            chmodded.append(path)

        out = cb.acquire_nrb(
            plan, "nrbpat_xyz", get_bytes=fake_get, write_file=fake_write, make_executable=fake_chmod
        )
        self.assertEqual(out, plan.install_path)
        self.assertEqual(written[plan.install_path], data)
        self.assertEqual(chmodded, [plan.install_path])

    def test_sha_mismatch_raises_and_writes_nothing(self):
        plan = self._plan(b"real bytes")
        written = {}

        def fake_get(url, token):
            return b"TAMPERED bytes"  # hashes differently

        def fake_write(path, blob):
            written[path] = blob  # must NOT be called

        with self.assertRaises(cb.BootstrapError):
            cb.acquire_nrb(plan, "tok", get_bytes=fake_get, write_file=fake_write, make_executable=lambda p: None)
        self.assertEqual(written, {}, "a failed SHA check must never write the binary")

    def test_download_error_wrapped(self):
        plan = self._plan(b"x")

        def boom(url, token):
            raise OSError("connection reset")

        with self.assertRaises(cb.BootstrapError):
            cb.acquire_nrb(plan, "tok", get_bytes=boom, write_file=lambda p, b: None, make_executable=lambda p: None)


class TestBootstrapEndToEnd(unittest.TestCase):
    def test_fetches_manifest_then_installs(self):
        # Exercise the REAL default write + chmod against a temp home, with a
        # manifest entry matching THIS platform (so the flow reaches install).
        import stat
        import tempfile

        os_, arch = cb.detect_target()
        if os_ == "unknown" or arch == "unknown":
            self.skipTest("bootstrap not supported on this test platform")

        data = b"nrb bytes v0.2.0"
        manifest = {
            "releases": [
                {
                    "os": os_,
                    "arch": arch,
                    "version": "0.2.0",
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ]
        }
        seen = {}

        def fake_json(url):
            seen["manifest_url"] = url
            return manifest

        def fake_bytes(url, token):
            seen["download_url"] = url
            seen["token"] = token
            return data

        with tempfile.TemporaryDirectory() as home:
            path = cb.bootstrap_nrb(
                "nrbpat_1",
                home,
                api_base="https://nurbly.com",
                get_json=fake_json,
                get_bytes=fake_bytes,
            )
            self.assertEqual(
                seen["manifest_url"], "https://nurbly.com/v1/cli/manifest.json"
            )
            self.assertEqual(
                seen["download_url"],
                "https://nurbly.com/v1/cli/releases/{}/{}".format(os_, arch),
            )
            self.assertEqual(seen["token"], "nrbpat_1")
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), data)
            if os.name != "nt":
                self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()

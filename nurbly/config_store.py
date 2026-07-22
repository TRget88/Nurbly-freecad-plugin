# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Persist the plugin's own settings (the located nrb path + a last-run log).

PURE LOGIC -- imports no FreeCAD. A tiny JSON store at
``~/.nurbly/nurbly-plugin/config.json``, sitting next to the doc-map (see
:mod:`mapping_store`) but holding plugin *config* rather than per-document
links. Keys today:

  * ``nrb_path`` -- the binary location the user picked in the first-run
    "locate nrb" dialog, which :func:`nrb_runner.locate_nrb` consults FIRST via
    its explicit-override slot.
  * ``last_run`` -- a single most-recent ``nrb`` invocation (argv + exit code +
    stdout/stderr), recorded for the Diagnostics view so the user can read the
    last command and its raw output when troubleshooting. Only the latest run
    is kept; this is a debugging aid, not an audit trail.

Like :class:`mapping_store.MappingStore`, a missing/corrupt file yields an
empty config rather than raising -- a first run has no file, and a malformed
config must never block sign-in. Home resolution mirrors nrb's own
(``$HOME`` then ``%USERPROFILE%``) so plugin and CLI agree on "home" on Windows.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Dict, List, Optional

_SCHEMA_VERSION = 1


def _resolve_state_path(new_path: str, legacy_path: str) -> str:
    """Return ``new_path``, migrating a pre-rebrand ``~/.vrd`` file to it once.

    The plugin's state dir was renamed ``~/.vrd`` -> ``~/.nurbly`` alongside
    the CLI.  If the new file does not exist yet but a legacy one does, copy
    it across (best-effort, non-destructive) so an upgrading user keeps their
    settings/links.  Any copy failure is swallowed -- the caller then reads an
    empty store, which must never block sign-in.
    """
    try:
        if not os.path.exists(new_path) and os.path.exists(legacy_path):
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            shutil.copy2(legacy_path, new_path)
    except OSError:
        pass
    return new_path


def default_config_path() -> str:
    """``~/.nurbly/nurbly-plugin/config.json`` resolved against $HOME/%USERPROFILE%.

    A pre-rebrand ``~/.vrd/nurbly-plugin/config.json`` is migrated to the new
    location on first resolve (see :func:`_resolve_state_path`).

    Raises ``RuntimeError`` if neither env var is set, matching
    :func:`mapping_store.default_store_path`.
    """
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not home:
        raise RuntimeError("Cannot determine home directory -- set HOME or USERPROFILE")
    new_path = os.path.join(home, ".nurbly", "nurbly-plugin", "config.json")
    legacy_path = os.path.join(home, ".vrd", "nurbly-plugin", "config.json")
    return _resolve_state_path(new_path, legacy_path)


class PluginConfig:
    """A thin JSON-backed dict of plugin settings.

    Pass ``path`` to point at an arbitrary file (the tests use a tmp file);
    omit it to use :func:`default_config_path`.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_config_path()
        self._data: Dict[str, object] = {}
        self._loaded = False

    # ── load / save ──────────────────────────────────────────────────────────

    def load(self) -> "PluginConfig":
        """Read the JSON file. A missing/empty/corrupt file yields an empty
        config rather than raising (mirrors MappingStore.load)."""
        self._data = {}
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (FileNotFoundError, ValueError):
            return self
        if isinstance(raw, dict):
            self._data = dict(raw)
        return self

    def save(self) -> None:
        """Atomically write the JSON file, creating the parent dir if needed."""
        self._ensure_loaded()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {"schema": _SCHEMA_VERSION, **self._data}
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)  # atomic on the same filesystem

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ── accessors ──────────────────────────────────────────────────────────

    def get_nrb_path(self) -> Optional[str]:
        """The saved nrb binary path, or ``None`` if unset."""
        self._ensure_loaded()
        value = self._data.get("nrb_path")
        return value or None

    def set_nrb_path(self, path: str) -> None:
        """Set the nrb binary path (does NOT save; call save())."""
        self._ensure_loaded()
        self._data["nrb_path"] = path

    def get_web_url(self) -> Optional[str]:
        """The configured web-app base URL override, or ``None`` if unset.

        Lets a developer point the Sign in dialog's "Get an access key" deep
        link at a local stack (e.g. ``http://localhost:5173``). Real users never
        set this -- the managed default in :mod:`nurbly.links` is used. A
        non-string or blank value (a hand-edited/older config) reads as unset
        rather than producing a broken link.
        """
        self._ensure_loaded()
        value = self._data.get("web_url")
        if isinstance(value, str) and value.strip():
            return value
        return None

    def get_last_run(self) -> Optional[Dict[str, object]]:
        """The most-recent recorded nrb run, or ``None`` if none/malformed.

        Returns a dict with keys ``argv`` (``list[str]``), ``exit_code``
        (``int``), ``stdout`` and ``stderr`` (``str``). A stored value of the
        wrong shape is treated as absent rather than raising -- the Diagnostics
        view must tolerate a hand-edited or older config file.
        """
        self._ensure_loaded()
        value = self._data.get("last_run")
        if not isinstance(value, dict):
            return None
        argv = value.get("argv")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            return None
        exit_code = value.get("exit_code")
        # bool is a subclass of int in Python; a hand-edited "exit_code": true
        # must read as malformed, not as exit code 1.
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return None
        return {
            "argv": list(argv),
            "exit_code": exit_code,
            "stdout": value.get("stdout") if isinstance(value.get("stdout"), str) else "",
            "stderr": value.get("stderr") if isinstance(value.get("stderr"), str) else "",
        }

    def set_last_run(
        self,
        argv: List[str],
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        """Record the latest nrb invocation for Diagnostics (does NOT save).

        Overwrites any previous entry -- only the most recent run is kept.
        Call :meth:`save` to persist.
        """
        self._ensure_loaded()
        self._data["last_run"] = {
            "argv": [str(a) for a in argv],
            "exit_code": int(exit_code),
            "stdout": stdout or "",
            "stderr": stderr or "",
        }

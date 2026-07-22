# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Locate and shell out to the ``nrb`` CLI.

PURE LOGIC w.r.t. FreeCAD -- imports no FreeCAD/FreeCADGui/Part. It does import
the stdlib ``subprocess``/``shutil``, so its tests mock those. Everything the
plugin does to the outside world goes through here (PLUGIN_MVP.md: the plugin
embeds no git and stores no credentials -- it only runs ``nrb``).

Two responsibilities:
  1. ``locate_nrb`` -- find the binary: explicit override, then PATH, then
     Rust's default ``~/.cargo/bin``. Surfaces a clear error if missing so the
     UI can prompt first-run setup.
  2. ``run`` -- launch ``nrb <args>`` in a given cwd, capture exit/stdout/stderr,
     suppress the console window on Windows, and decode bytes defensively.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, NamedTuple, Optional


class NrbNotFound(Exception):
    """Raised when the nrb binary cannot be located."""


class NrbResult(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str
    argv: List[str]  # full argv that ran, for logging/diagnostics

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


def _exe_name() -> str:
    return "nrb.exe" if os.name == "nt" else "nrb"


def locate_nrb(config_override: Optional[str] = None) -> str:
    """Return an absolute path to the nrb binary.

    Precedence (PLUGIN_MVP.md "Locate nrb": config/env, then PATH):
      1. ``config_override`` (the plugin's saved setting / first-run dialog),
      2. ``$NRB_BINARY`` env var,
      3. ``shutil.which("nrb")`` (PATH),
      4. ``~/.cargo/bin/nrb[.exe]`` (Rust's default install dir; FreeCAD's
         bundled Python launched from the GUI may not inherit the user's PATH,
         so this fallback matters).

    Raises :class:`NrbNotFound` with an actionable message otherwise.
    """
    if config_override and os.path.isfile(config_override):
        return os.path.abspath(config_override)

    env_override = os.environ.get("NRB_BINARY")
    if env_override and os.path.isfile(env_override):
        return os.path.abspath(env_override)

    on_path = shutil.which("nrb")
    if on_path:
        return on_path

    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if home:
        cargo = os.path.join(home, ".cargo", "bin", _exe_name())
        if os.path.isfile(cargo):
            return cargo

    raise NrbNotFound(
        "Could not find the 'nrb' CLI.\n"
        "Looked in: the configured path, $NRB_BINARY, your PATH, and "
        "~/.cargo/bin.\n"
        "Install nrb, then set its full path in the Nurbly plugin's first-run "
        "dialog (or add it to PATH and restart your CAD application)."
    )


def _windows_no_window():
    """Return (startupinfo, creationflags) that hide the console on Windows.

    Both pieces are required: STARTF_USESHOWWINDOW+SW_HIDE on the startupinfo
    AND CREATE_NO_WINDOW (0x08000000) in creationflags. On non-Windows both are
    no-ops.
    """
    if sys.platform != "win32":
        return None, 0
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si, 0x08000000  # CREATE_NO_WINDOW


def run(
    args: List[str],
    cwd: Optional[str] = None,
    input_text: Optional[str] = None,
    timeout: int = 120,
    nrb_path: Optional[str] = None,
    config_override: Optional[str] = None,
) -> NrbResult:
    """Run ``nrb <args>`` and capture the result.

    ``args`` is the argv *after* the binary (e.g. ``["lock", "a/b", "p"]`` from
    ``cmd_builder``). ``cwd`` must be the clone dir for working-tree verbs
    (pull/add/commit/push); server-only verbs (login/repo list/lock/locks)
    don't care about cwd.

    Bytes are captured (``text=False``) and decoded as UTF-8 with
    ``errors="replace"`` so non-UTF-8 output never raises. A ``TimeoutExpired``
    is converted into a non-zero :class:`NrbResult` rather than propagating, so
    callers have one uniform success/failure shape.
    """
    binary = nrb_path or locate_nrb(config_override)
    argv = [binary, *args]
    startupinfo, creationflags = _windows_no_window()

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            text=False,
            timeout=timeout,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if exc.stdout else ""
        err = exc.stderr.decode("utf-8", "replace") if exc.stderr else ""
        return NrbResult(
            exit_code=124,  # conventional "timed out"
            stdout=out,
            stderr=(err + f"\nnrb timed out after {timeout}s").strip(),
            argv=argv,
        )

    stdout = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    return NrbResult(exit_code=proc.returncode, stdout=stdout, stderr=stderr, argv=argv)

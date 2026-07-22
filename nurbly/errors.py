# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Classify ``nrb`` failures into the user-facing categories the UI shows.

PURE LOGIC -- imports no FreeCAD. Drives error dialogs off the *exit code*
(non-zero = failure) and a light scrape of the CLI's message, per
PLUGIN_MVP.md ("Drive decisions off exit codes; show the CLI's message
verbatim").

How nrb actually reports errors (verified against the Rust source, NOT
assumed):

  * A failed REST call ends in ``bail!("{op}:\\n  {decoded}")`` where
    ``decoded`` comes from ``server_error::format_server_error``. For a
    generic envelope that string is ``"HTTP <status> -- <inner message>"``
    (server_error.rs). So the numeric HTTP status appears IN THE TEXT, e.g.
    ``"HTTP 409 -- File is locked by @alice"``.
  * anyhow prints that to **stderr** and the process exits **1** -- the
    PROCESS exit code is 1, NOT 409. (The research that said "exit code 409"
    was wrong; HTTP codes live in the message, not in the OS exit status.)

We therefore classify on the combined stdout+stderr text, looking for the
``HTTP <code>`` token first and falling back to keyword sniffing so a future
``--json`` mode or reworded message still lands in the right bucket.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple, Optional


class ErrorKind(Enum):
    """The categories the UI cares about. The first three are the MVP's
    mandated dialogs; the rest are sensible extras kept generic."""

    OK = "ok"
    LOCK_CONFLICT = "lock_conflict"  # 409 -- file locked by someone else
    PUSH_REJECTED = "push_rejected"  # non-fast-forward -- someone pushed first
    NOT_AUTHENTICATED = "not_authenticated"  # 401 / no credentials
    FORBIDDEN = "forbidden"  # 403 -- no Write/Admin
    NOT_FOUND = "not_found"  # 404 -- repo/lock missing
    NRB_NOT_FOUND = "nrb_not_found"  # the binary itself is missing
    # The nrb binary IS present but its version is older than the plugin needs
    # (e.g. it predates `nrb commit --json`). Not an nrb runtime failure --
    # classify() never returns this. core.check_nrb_version sets it directly off
    # the parsed `nrb --version` line so the UI says "update nrb" instead of
    # surfacing a raw clap error from a missing flag.
    NRB_TOO_OLD = "nrb_too_old"
    # Soft-success: the push succeeded, but the assembly links to part files that
    # live OUTSIDE the clone and were therefore not committed (silent drop). Not
    # an nrb failure -- classify() never returns this; core.check_in sets it
    # directly on an otherwise-ok result so the UI renders a "committed, but heads
    # up" warning.
    ASSEMBLY_DEPS_OUTSIDE = "assembly_deps_outside"
    # Soft-block: the user asked to switch to a different branch but the clone has
    # uncommitted changes. Not an nrb failure -- classify() never returns this;
    # core.switch_branch sets it directly (off a clean `nrb status` pre-check)
    # BEFORE touching HEAD, so the UI tells the user to commit or discard first.
    DIRTY_TREE = "dirty_tree"
    UNKNOWN = "unknown"  # surface the raw message verbatim


class Classified(NamedTuple):
    kind: ErrorKind
    # A short title for the dialog. The detail (verbatim nrb text) is supplied
    # by the caller, which holds the full stdout/stderr.
    title: str


_HTTP_RE = re.compile(r"HTTP\s+(\d{3})")


def _holder_from(text: str) -> Optional[str]:
    """Pull a ``@username`` out of a lock-conflict message if present."""
    m = re.search(r"locked by @([\w.\-]+)", text, re.IGNORECASE)
    return m.group(1) if m else None


def classify(exit_code: int, stdout: str = "", stderr: str = "") -> Classified:
    """Map an nrb result to an :class:`ErrorKind` + dialog title.

    ``exit_code == 0`` is always success regardless of text. For failures we
    look at the merged output. The caller is responsible for showing the
    verbatim message body underneath the title.
    """
    if exit_code == 0:
        return Classified(ErrorKind.OK, "")

    blob = f"{stdout}\n{stderr}".lower()
    status = None
    m = _HTTP_RE.search(f"{stdout}\n{stderr}")
    if m:
        status = int(m.group(1))

    # Lock conflict (409) -- the headline MVP case.
    if status == 409 or "already locked" in blob or "locked by @" in blob:
        holder = _holder_from(f"{stdout}\n{stderr}")
        if holder:
            return Classified(ErrorKind.LOCK_CONFLICT, f"File is locked by @{holder}")
        return Classified(ErrorKind.LOCK_CONFLICT, "File is already locked")

    # Not authenticated (401, or any "run nrb login" remedy text).
    if (
        status == 401
        or "not logged in" in blob
        or "nrb login" in blob
        or "token rejected" in blob
        or "credentials" in blob
    ):
        return Classified(ErrorKind.NOT_AUTHENTICATED, "You are not signed in")

    # Push rejected -- non-fast-forward. nrb surfaces git's reject wording.
    if (
        "rejected" in blob
        or "non-fast-forward" in blob
        or "fast-forward" in blob
        or "fetch first" in blob
        or "pull" in blob
        and "behind" in blob
    ):
        return Classified(ErrorKind.PUSH_REJECTED, "Someone pushed first -- pull latest")

    # Forbidden (403). server_error.rs's STRUCTURED branch prints
    # "Permission denied." / "Insufficient permissions: ..." with NO "HTTP 403"
    # token (e.g. a lock-release 403), so sniff those keywords too -- otherwise
    # such a failure falls through to UNKNOWN. This check sits AFTER the
    # push-rejected block on purpose; a permission message lacks the
    # pull/rejected/behind tokens, so it is not mis-stolen by that branch.
    if (
        status == 403
        or "insufficient permissions" in blob
        or "permission denied" in blob
        or "forbidden" in blob
    ):
        return Classified(ErrorKind.FORBIDDEN, "You do not have permission for this")

    if status == 404 or "not found" in blob:
        return Classified(ErrorKind.NOT_FOUND, "Repository or lock not found")

    return Classified(ErrorKind.UNKNOWN, "nrb reported an error")


# Plain-text remedies appended under the verbatim nrb message in the dialog.
# Kept here (pure) so they're testable and host-agnostic; the FreeCAD UI layer
# just renders them.
REMEDIES = {
    ErrorKind.LOCK_CONFLICT: (
        "Another user holds the lock. Coordinate with them, or wait for it to "
        "expire. Use 'Who has it?' to see the holder."
    ),
    ErrorKind.PUSH_REJECTED: (
        "The remote moved on. Run Check out again (which pulls) to get the "
        "latest, re-apply your changes, then Check in."
    ),
    ErrorKind.NOT_AUTHENTICATED: (
        "Run Sign in, then click 'Get an access key' to open your Nurbly API "
        "Keys page, create a Personal Access Token (nrbpat_...), and paste it in."
    ),
    ErrorKind.NRB_NOT_FOUND: (
        "The nrb CLI could not be found. Install it, or set its path in the "
        "plugin's first-run dialog."
    ),
    ErrorKind.NRB_TOO_OLD: (
        "Your nrb CLI is older than this plugin needs. Update nrb (run 'nrb "
        "update', or re-run the install command from the Nurbly CLI page), then "
        "retry. The version this plugin requires is named in the message above."
    ),
    ErrorKind.ASSEMBLY_DEPS_OUTSIDE: (
        "These linked part files live outside the repository clone, so they were "
        "NOT committed with this assembly -- the push contains the assembly but "
        "not those parts. Move each file into the repo clone folder and re-link it "
        "in FreeCAD (File -> ... or the assembly's link properties), then Check in "
        "again so the parts are committed alongside the assembly."
    ),
    ErrorKind.DIRTY_TREE: (
        "Your clone has uncommitted changes. Check in to commit and push them "
        "first, or discard them, then switch branches. Switching with a dirty "
        "working tree could mix edits across branches or fail mid-checkout."
    ),
}

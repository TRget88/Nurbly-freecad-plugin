# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Parse ``nrb``'s machine-readable ``--json`` stdout.

PURE LOGIC -- imports no FreeCAD. The plugin invokes the parsed commands with
the global ``--json`` flag (see :mod:`cmd_builder`), so ``nrb`` emits its
response DTOs as JSON instead of human-formatted tables. We decode those here
with the stdlib ``json`` module -- no more brittle table/line scraping.

The JSON shapes are the serde-serialised Rust response structs in
``backend/crates/nurbly-core/src/models/responses.rs`` (serde uses the Rust
field names verbatim -- none of the consumed fields are ``#[serde(rename)]``):

    nrb lock --json       ->  one FileLockResponse object
                              (commands/lock.rs cmd_lock -> to_string_pretty(&lock))
    nrb locks --json      ->  a JSON array of FileLockResponse
                              (commands/lock.rs cmd_locks -> &locks)
    nrb repo list --json  ->  a JSON array of RepoResponse
                              (commands/repo.rs RepoCommands::List -> Vec<RepoResponse>)
    nrb commit --json     ->  one ad-hoc object ``{ "commit", "short" }``
                              (commands/local_git.rs cmd_commit -> commit_json)

Consumed FileLockResponse fields: ``id``, ``path``, ``locked_by_username``,
``expires_at`` (Option -- absent when the lock never expires), ``lock_message``
(Option -- absent when no message was set).
Consumed RepoResponse fields: ``owner_slug``, ``name``, ``clone_url``.
Consumed commit object fields: ``short`` (preferred, 8-char prefix) with
``commit`` (full 40-char sha) as the fallback source.

:func:`extract_commit_sha` prefers the ``nrb commit --json`` object but KEEPS a
scrape of the human ``[<sha>] msg`` line as a fallback, so an older nrb without
a commit ``--json`` mode (which still prints the human line) keeps working.

Robustness contract: these parsers NEVER raise on unexpected stdout. Empty
output, blank strings, malformed JSON, or JSON of the wrong shape yield the
empty/``None`` result the callers (:mod:`core`) already handle -- a failed
lock-id read falls back to ``nrb locks`` (PLUGIN_MVP.md), and an unparseable
list simply shows no rows. The caller still has the verbatim ``res.combined``
for the dialog detail pane, and the non-zero exit path is handled separately by
:mod:`errors`.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, NamedTuple, Optional

# Commit line: "[<8-char-sha>] <first line of message>". Kept as the fallback
# scrape for an older nrb that has no commit --json mode (it still prints this
# human line); the JSON object is preferred when present.
_COMMIT_RE = re.compile(r"^\[([0-9a-fA-F]{6,40})\]\s*(.*)$", re.MULTILINE)


class RepoRow(NamedTuple):
    """One row of ``nrb repo list``: ``owner/name`` plus its clone URL."""

    owner: str
    repo: str
    clone_url: str

    @property
    def owner_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


class LockRow(NamedTuple):
    """One row of ``nrb locks``."""

    path: str
    locked_by: str  # without the leading '@'
    expires: str  # ISO-8601 string or the literal "never"
    message: str


class BranchRow(NamedTuple):
    """One row of ``nrb branch``: the branch name and whether it is current."""

    name: str
    is_current: bool


def _loads(stdout: str) -> Optional[Any]:
    """``json.loads`` ``stdout``, returning ``None`` on empty or malformed input.

    Centralises the "never crash on unexpected stdout" contract: a blank string
    or anything that isn't valid JSON yields ``None`` so each parser can return
    its empty result instead of raising.
    """
    if not stdout or not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def extract_lock_id(stdout: str) -> Optional[str]:
    """Return the lock id from ``nrb lock --json`` stdout, or ``None`` if absent.

    ``nrb lock --json`` prints a single ``FileLockResponse`` object; we read its
    ``id`` field. Capturing this is mandatory: ``nrb unlock`` needs the id. If it
    returns ``None`` on a successful lock (malformed/unexpected JSON), the caller
    falls back to ``nrb locks`` to re-derive it (PLUGIN_MVP.md).
    """
    obj = _loads(stdout)
    if not isinstance(obj, dict):
        return None
    lock_id = obj.get("id")
    return lock_id if isinstance(lock_id, str) and lock_id else None


def extract_commit_sha(stdout: str) -> Optional[str]:
    """Return the new commit's short sha from ``nrb commit`` stdout, or None.

    Prefers the ``nrb commit --json`` object ``{ "commit", "short" }``: read
    ``short`` (the 8-char prefix), falling back to ``commit[:8]`` when only the
    full sha is present. If the stdout is not that JSON object (an older nrb with
    no commit ``--json`` mode prints the human ``[<sha>] msg`` line instead), the
    regex scrape of that line is used. Never raises on unexpected stdout.
    """
    if not stdout:
        return None

    # Preferred path: the machine-readable object. Reuse the shared `_loads`
    # so empty / malformed stdout degrades to the scrape below instead of raising.
    obj = _loads(stdout)
    if isinstance(obj, dict):
        short = obj.get("short")
        if isinstance(short, str) and short:
            return short
        full = obj.get("commit")
        if isinstance(full, str) and full:
            return full[:8]

    # Back-compat fallback: scrape the human "[<sha>] msg" line.
    m = _COMMIT_RE.search(stdout)
    return m.group(1) if m else None


def parse_repo_list(stdout: str) -> List[RepoRow]:
    """Parse ``nrb repo list --json`` into rows.

    The stdout is a JSON array of ``RepoResponse`` objects; each contributes one
    :class:`RepoRow` built from ``owner_slug`` / ``name`` / ``clone_url``. An
    empty array (or empty / malformed stdout) yields an empty list. Elements
    missing the required fields are skipped rather than crashing the parse.
    """
    rows: List[RepoRow] = []
    data = _loads(stdout)
    if not isinstance(data, list):
        return rows
    for item in data:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner_slug")
        repo = item.get("name")
        if not isinstance(owner, str) or not isinstance(repo, str):
            continue
        clone_url = item.get("clone_url")
        rows.append(
            RepoRow(
                owner=owner,
                repo=repo,
                clone_url=clone_url if isinstance(clone_url, str) else "",
            )
        )
    return rows


def parse_locks(stdout: str) -> List[LockRow]:
    """Parse ``nrb locks --json`` into rows.

    The stdout is a JSON array of ``FileLockResponse`` objects; each contributes
    one :class:`LockRow`. ``locked_by`` is ``locked_by_username`` (the JSON
    carries the bare username -- no leading ``@`` to strip, unlike the old
    table). ``expires_at`` is omitted from the JSON when the lock never expires,
    so we render the literal ``"never"`` to match the table semantics the UI
    already expects. ``lock_message`` is omitted when unset; we substitute the
    empty string.

    An empty array (or empty / malformed stdout) yields an empty list. Elements
    missing the required ``path`` / ``locked_by_username`` are skipped.
    """
    rows: List[LockRow] = []
    data = _loads(stdout)
    if not isinstance(data, list):
        return rows
    for item in data:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        locked_by = item.get("locked_by_username")
        if not isinstance(path, str) or not isinstance(locked_by, str):
            continue
        expires = item.get("expires_at")
        message = item.get("lock_message")
        rows.append(
            LockRow(
                path=path,
                locked_by=locked_by.lstrip("@"),
                expires=expires if isinstance(expires, str) else "never",
                message=message if isinstance(message, str) else "",
            )
        )
    return rows


def parse_branch_list(stdout: str) -> List[BranchRow]:
    """Parse ``nrb branch`` stdout into rows.

    ``nrb branch`` has no ``--json`` mode; it prints one local branch per line,
    the current branch prefixed with ``* `` and every other with two spaces
    (commands/local_git.rs cmd_branch). We strip that two-char prefix and record
    whether the line was the current one. Blank lines and lines with no name
    after the prefix are skipped; empty / ``None`` stdout yields an empty list.
    A line with neither prefix (some future reformat) is still accepted as a
    non-current branch, trimmed, so the picker degrades gracefully.
    """
    rows: List[BranchRow] = []
    if not stdout:
        return rows
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        is_current = raw.startswith("* ")
        if is_current or raw.startswith("  "):
            name = raw[2:].strip()
        else:
            name = raw.strip()
        if not name:
            continue
        rows.append(BranchRow(name=name, is_current=is_current))
    return rows


def is_working_tree_clean(stdout: str) -> bool:
    """Return True when ``nrb status`` reports a clean working tree.

    ``nrb status`` (no ``--json``) prints the exact line ``Nothing to commit,
    working tree clean.`` only when there are no staged, unstaged, untracked, or
    conflicted entries and no merge/rebase in progress (commands/local_git.rs
    cmd_status). Any other body -- a change list or an in-progress banner -- means
    switching branches could lose or mix uncommitted work, so we treat it as
    dirty. Empty / ``None`` stdout is treated as NOT clean (fail safe: status
    always prints at least ``On branch <x>``, so empty output is anomalous).
    """
    if not stdout:
        return False
    return "working tree clean" in stdout

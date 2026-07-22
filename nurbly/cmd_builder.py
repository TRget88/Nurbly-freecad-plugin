# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Build ``nrb`` CLI argument lists.

PURE LOGIC -- imports no FreeCAD. Every function returns a ``list[str]``
of arguments *after* the ``nrb`` binary (the binary path is prepended by
``nrb_runner``). No subprocess is launched here; this module only assembles
argv so it can be unit-tested with plain Python.

The flags/positionals mirror the clap definitions in
``backend/crates/nurbly-cli/src/main.rs`` (lock/unlock/locks come from the
``feat/nrb-lock`` branch). ``owner/repo`` is always passed in full
``<owner>/<repo>`` form -- never the bare-name fallback -- per PLUGIN_MVP.md.

The commands whose stdout the plugin actually parses -- ``repo list``, ``lock``,
``locks`` and ``commit`` -- pass the global ``--json`` flag so nrb emits its
response (a serde DTO, or for commit the ad-hoc ``{ "commit", "short" }``
object) as JSON instead of human text (see :mod:`output_parser`). ``--json`` is
a GLOBAL clap flag declared on the top-level ``Cli`` (main.rs), so it is
accepted anywhere on the line; we append it after the subcommand for readable
argv. The remaining sync verbs (clone / pull / push / add) have NO ``--json``
mode, and ``unlock``'s stdout is never parsed (core only checks its exit code),
so none of those carry the flag.

``version()`` runs ``nrb --version`` (clap's built-in flag), whose stdout is the
plain ``nrb <version>`` line the version guard parses -- there is no ``--json``
form for it.
"""

from __future__ import annotations

from typing import List, Optional


def owner_repo(owner: str, repo: str) -> str:
    """Join ``owner`` and ``repo`` into the ``<owner>/<repo>`` token nrb expects."""
    owner = owner.strip().strip("/")
    repo = repo.strip().strip("/")
    if not owner or not repo:
        raise ValueError("owner and repo must both be non-empty")
    if "/" in repo:
        raise ValueError("repo name must not contain '/'")
    return f"{owner}/{repo}"


def login(token: str) -> List[str]:
    """``nrb login --token <PAT>``."""
    token = token.strip()
    if not token:
        raise ValueError("token must be non-empty")
    return ["login", "--token", token]


def whoami() -> List[str]:
    """``nrb whoami`` -- used by the first-run check (exit 0 = authenticated)."""
    return ["whoami"]


def version() -> List[str]:
    """``nrb --version`` -- clap's built-in version flag.

    Its stdout is the plain ``nrb <version>`` line (e.g. ``nrb 0.1.0``) that the
    version guard (:func:`core.check_nrb_version`) parses. There is no ``--json``
    form, so this carries no flag.
    """
    return ["--version"]


def repo_list(org: Optional[str] = None) -> List[str]:
    """``nrb repo list [--org <org>] --json``.

    Passes ``--json`` because the plugin parses this command's stdout
    (:func:`output_parser.parse_repo_list`). The global flag goes last.
    """
    args = ["repo", "list"]
    if org:
        args += ["--org", org]
    args.append("--json")
    return args


def clone(owner: str, repo: str, directory: Optional[str] = None) -> List[str]:
    """``nrb clone <owner>/<repo> [--directory <dir>]``."""
    args = ["clone", owner_repo(owner, repo)]
    if directory:
        args += ["--directory", directory]
    return args


def pull(remote: str = "origin", branch: Optional[str] = None) -> List[str]:
    """``nrb pull [--remote <r>] [--branch <b>]``.

    ``--remote`` defaults to ``origin`` in nrb, but we pass it explicitly so the
    emitted argv is unambiguous and self-documenting in logs.
    """
    args = ["pull", "--remote", remote]
    if branch:
        args += ["--branch", branch]
    return args


def push(remote: str = "origin", branch: Optional[str] = None) -> List[str]:
    """``nrb push [--remote <r>] [--branch <b>]`` (no ``--force`` from the plugin)."""
    args = ["push", "--remote", remote]
    if branch:
        args += ["--branch", branch]
    return args


def add(paths: Optional[List[str]] = None) -> List[str]:
    """``nrb add <path>...`` -- defaults to ``add .`` (whole working tree)."""
    if not paths:
        return ["add", "."]
    return ["add", *paths]


def commit(message: str) -> List[str]:
    """``nrb commit -m <message> --json``.

    Passes ``--json`` so nrb emits the ad-hoc ``{ "commit", "short" }`` object
    the plugin parses for the new commit's sha
    (:func:`output_parser.extract_commit_sha`). The global flag goes last. An
    older nrb without a commit ``--json`` mode ignores the unknown global flag's
    effect and still prints the human ``[<sha>] msg`` line, which the parser
    falls back to scraping, so this stays back-compat.
    """
    if not message or not message.strip():
        raise ValueError("commit message must be non-empty")
    return ["commit", "-m", message, "--json"]


def lock(
    owner: str,
    repo: str,
    path: str,
    expires_hours: Optional[int] = None,
    message: Optional[str] = None,
) -> List[str]:
    """``nrb lock <owner>/<repo> <path> [--expires-hours N] [-m MSG] --json``.

    ``path`` is the repo-relative path of the file to lock (forward slashes;
    a leading slash is normalised away server-side, but we strip it here too).

    Passes ``--json`` because the plugin parses this command's stdout to read
    back the new lock's id (:func:`output_parser.extract_lock_id`). The global
    flag goes last.
    """
    path = path.replace("\\", "/").lstrip("/")
    if not path:
        raise ValueError("lock path must be non-empty")
    args = ["lock", owner_repo(owner, repo), path]
    if expires_hours is not None:
        args += ["--expires-hours", str(expires_hours)]
    if message:
        args += ["-m", message]
    args.append("--json")
    return args


def unlock(owner: str, repo: str, lock_id: str) -> List[str]:
    """``nrb unlock <owner>/<repo> <lock-id>``.

    ``lock_id`` must be a bare UUID with no surrounding whitespace -- nrb parses
    it with clap's ``Uuid`` type, so a trailing newline makes it fail to parse.
    """
    lock_id = lock_id.strip()
    if not lock_id:
        raise ValueError("lock_id must be non-empty")
    return ["unlock", owner_repo(owner, repo), lock_id]


def branch_list() -> List[str]:
    """``nrb branch`` -- list the clone's local branches.

    The no-name form lists; the current branch is printed with a ``* `` prefix
    and the rest with two leading spaces (commands/local_git.rs cmd_branch).
    There is NO ``--json`` mode, so :func:`output_parser.parse_branch_list`
    scrapes those lines.
    """
    return ["branch"]


def switch(branch: str, create: bool = False) -> List[str]:
    """``nrb switch [-c] <branch>`` -- set the working branch in the clone.

    ``create=True`` adds ``-c`` so nrb cuts the branch from the current HEAD and
    switches to it in one step (matches ``git switch -c``). The plugin never
    parses this command's stdout -- core only checks the exit code -- so it
    carries no ``--json`` flag.
    """
    branch = branch.strip()
    if not branch:
        raise ValueError("branch name must be non-empty")
    args = ["switch"]
    if create:
        args.append("-c")
    args.append(branch)
    return args


def status() -> List[str]:
    """``nrb status`` -- working-tree state for the clone.

    No ``--json`` mode; the switch-branch dirty-tree pre-check
    (:func:`output_parser.is_working_tree_clean`) scrapes its text output.
    """
    return ["status"]


def locks(owner: Optional[str] = None, repo: Optional[str] = None, mine: bool = False) -> List[str]:
    """``nrb locks [<owner>/<repo>] [--my] --json``.

    With ``mine=True`` the repo args are omitted and ``--my`` lists every lock
    the user holds. Otherwise both ``owner`` and ``repo`` are required.

    Passes ``--json`` because the plugin parses this command's stdout
    (:func:`output_parser.parse_locks`). The global flag goes last.
    """
    if mine:
        return ["locks", "--my", "--json"]
    if not owner or not repo:
        raise ValueError("locks needs owner+repo unless mine=True")
    return ["locks", owner_repo(owner, repo), "--json"]

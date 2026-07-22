# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Host-agnostic orchestration of the five PDM actions.

PURE LOGIC -- imports no FreeCAD. This composes the other pure modules
(:mod:`cmd_builder`, :mod:`nrb_runner`, :mod:`output_parser`, :mod:`errors`,
:mod:`mapping_store`) into the exact nrb sequences from PLUGIN_MVP.md.

The single inherently per-CAD step -- exporting the active document to STEP --
is *injected* as a callable, so this module never imports the host API and is
fully testable with a fake runner + fake exporter. The FreeCAD layer wires in
the real ``freecad_export.export_active_document`` and a real
:class:`~nurbly.nrb_runner` callable.

Every action returns an :class:`ActionResult`: a success flag, the
:class:`errors.ErrorKind`, a dialog title, and the verbatim combined nrb
output for the detail pane. The UI layer turns that into a dialog; it makes no
nrb decisions of its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, NamedTuple, Optional

from . import assembly, cmd_builder, errors
from .errors import ErrorKind
from .mapping_store import DocMapping, MappingStore
from .nrb_runner import NrbNotFound, NrbResult
from .output_parser import (
    BranchRow,
    RepoRow,
    extract_commit_sha,
    extract_lock_id,
    is_working_tree_clean,
    parse_branch_list,
    parse_locks,
    parse_repo_list,
)

# A runner is anything with the signature of nrb_runner.run that returns an
# NrbResult. Injecting it (rather than importing the module function directly)
# keeps core unit-testable with a fake.
Runner = Callable[..., NrbResult]

# The STEP exporter: given an absolute output path, write the active doc to it.
# Injected by the FreeCAD layer; returns the path written, raises on failure.
StepExporter = Callable[[str], str]

# Save the active doc to an absolute path (FreeCAD doc.saveAs). Injected by the
# FreeCAD layer; returns the path actually written, raises on failure.
DocSaver = Callable[[str], str]

# List every .FCStd document inside a clone (recursively). Injected by the
# FreeCAD layer (it walks the filesystem) so core stays FreeCAD- AND
# filesystem-free and unit-testable with a scripted list. Returns absolute paths.
FcStdLister = Callable[[str], List[str]]

# Open a .FCStd document and return the OPENED document's ABSOLUTE path
# (``App.openDocument(path)`` then ``abspath(doc.FileName)``). Injected by the
# FreeCAD layer; raises on failure. Returning the opened path -- NOT the input
# path -- is load-bearing: ``open_project`` keys the mapping by it, so a later
# Check out (which looks the mapping up by the active document's FileName)
# resolves it instead of reporting "Document is not linked".
DocOpener = Callable[[str], str]

# Choose ONE document from a list of discovered candidates, or None to cancel.
# Injected by the FreeCAD layer: it auto-selects a lone candidate, shows a
# picker for several, and returns None on cancel. Keeps the selection UI in the
# host while core's orchestration stays pure + testable.
DocSelector = Callable[[List[str]], Optional[str]]

# Relocate out-of-tree assembly parts into the clone (assembly v2), one-callable
# shape. Injected by the FreeCAD layer. Given a plan_relocation() list it copies
# each part into the clone's parts/ dir, rewrites the assembly's links, PERSISTS
# the re-linked assembly itself (the host owns that save), and returns the
# UNRESOLVED subset -- the ORIGINAL out-of-tree paths it could NOT relocate (so
# core can still warn about those). A None default keeps check_in's v1 warning.
DependencyRelocator = Callable[[list], List[str]]

# Copy one file from src to dest (e.g. shutil.copy2). Injected by the FreeCAD
# layer so core never imports shutil; raises on failure. Used together with
# RepathFn as the split (copy + re-path) relocation shape to pull out-of-tree
# assembly parts into the clone before ``nrb add .``.
CopyFn = Callable[[str, str], None]

# Re-point the active document's links from their original out-of-tree paths to
# the in-clone copies. Injected by the FreeCAD layer (it touches the FreeCAD
# App::Link API); takes the list of RelocationOp items that were copied. Returns
# nothing, raises on failure. Core never imports FreeCAD.
RepathFn = Callable[[List["assembly.RelocationOp"]], None]


@dataclass
class ActionResult:
    ok: bool
    kind: ErrorKind = ErrorKind.OK
    title: str = ""
    detail: str = ""  # verbatim nrb stdout/stderr for the dialog body
    # Action-specific payloads:
    repos: List[RepoRow] = field(default_factory=list)
    # ``lock_ids`` maps each locked repo-relative path to its lock id. Populated
    # by Check out (the active file plus each in-tree part) and surfaced by Check
    # in's manual-release fallback when a release fails.
    lock_ids: Dict[str, str] = field(default_factory=dict)
    locks: list = field(default_factory=list)
    # ``branches`` is the parsed ``nrb branch`` listing, populated by
    # :func:`list_branches` so the Switch-branch picker can show every local
    # branch with the current one marked.
    branches: List[BranchRow] = field(default_factory=list)

    @property
    def remedy(self) -> str:
        return errors.REMEDIES.get(self.kind, "")


class _Failure(NamedTuple):
    result: NrbResult


def _check(res: NrbResult) -> Optional[ActionResult]:
    """If ``res`` failed, build the matching ActionResult; else return None."""
    if res.ok:
        return None
    cls = errors.classify(res.exit_code, res.stdout, res.stderr)
    return ActionResult(ok=False, kind=cls.kind, title=cls.title, detail=res.combined)


def _nrb_missing(exc: NrbNotFound) -> ActionResult:
    return ActionResult(
        ok=False,
        kind=ErrorKind.NRB_NOT_FOUND,
        title="nrb CLI not found",
        detail=str(exc),
    )


def default_clone_parent(doc_path: Optional[str], home: Optional[str] = None) -> str:
    """Pick a likely-writable directory to create the repo clone under.

    The clone-first model creates ``<parent>/<repo>`` and saves the active
    document into it, so ``<parent>`` MUST be writable. The document's own
    folder is the natural default, but FreeCAD example files (and other
    read-only docs) live under the install tree (for example
    ``C:/Program Files/FreeCAD .../data/examples``), where the clone fails with
    "Access is denied". When the document does not live under the user's home
    directory, fall back to home so the default is writable. The GUI still lets
    the user pick a different folder.
    """
    home_abs = os.path.abspath(home or os.path.expanduser("~"))
    doc_dir = os.path.dirname(os.path.abspath(doc_path)) if doc_path else ""
    if doc_dir:
        try:
            under_home = os.path.commonpath([doc_dir, home_abs]) == home_abs
        except (ValueError, TypeError):
            under_home = False
        if under_home:
            return doc_dir
    return home_abs


# ── Action 1: Sign in ────────────────────────────────────────────────────────


def sign_in(runner: Runner, token: str, **run_kw) -> ActionResult:
    """``nrb login --token <PAT>``.

    On success nrb prints ``Logged in as <user> (via PAT)``; we surface that
    verbatim. The plugin stores no credentials -- nrb persists them in
    ``~/.nurbly/credentials.json``.
    """
    try:
        res = runner(cmd_builder.login(token), **run_kw)
    except NrbNotFound as exc:
        return _nrb_missing(exc)
    fail = _check(res)
    if fail:
        return fail
    return ActionResult(ok=True, detail=res.combined)


def is_authenticated(runner: Runner, **run_kw) -> bool:
    """``nrb whoami`` exit 0? Cheap check for whether Sign in is needed.

    NOTE: this DOES shell out, so the UI must not call it from ``IsActive``
    (which FreeCAD polls constantly). Call it once on first user action.
    """
    try:
        return runner(cmd_builder.whoami(), **run_kw).ok
    except NrbNotFound:
        return False


# ── nrb version guard ────────────────────────────────────────────────────────

# The oldest nrb the plugin supports. The 0.1.0 workspace build is the first to
# carry `nrb commit --json` (the machine-readable commit object this plugin
# parses), so that is the floor. Bump this the same change you start relying on a
# newer nrb feature.
MIN_NRB_VERSION = "0.1.0"


def _parse_nrb_version(stdout: str) -> Optional[str]:
    """Pull the ``X.Y.Z`` version out of ``nrb --version`` stdout, or None.

    clap prints ``nrb <version>`` (e.g. ``nrb 0.1.0``). We take the first
    whitespace-separated token that starts with a digit so a pre-release suffix
    (``0.1.0-beta``) or a reworded line still parses its numeric core. Returns
    None when nothing version-like is found, so the caller degrades gracefully.
    """
    if not stdout:
        return None
    for token in stdout.replace("\n", " ").split():
        if token and token[0].isdigit():
            return token
    return None


def _version_is_older(have: str, minimum: str) -> bool:
    """Return True when ``have`` is strictly older than ``minimum``.

    A small pure segment-wise integer compare (mirrors the Rust CLI's
    ``update.rs is_newer``): split on ``.`` and compare each segment as an
    integer, treating a missing segment as 0 and falling back to a string
    compare for any non-numeric segment (pre-release suffixes). Equal or newer
    returns False. This NEVER raises -- an unparseable input simply does not
    register as older.
    """
    if have == minimum:
        return False
    have_parts = have.split(".")
    min_parts = minimum.split(".")
    for i in range(max(len(have_parts), len(min_parts))):
        hseg = have_parts[i] if i < len(have_parts) else "0"
        mseg = min_parts[i] if i < len(min_parts) else "0"
        try:
            hi, mi = int(hseg), int(mseg)
        except ValueError:
            if hseg != mseg:
                return hseg < mseg
            continue
        if hi != mi:
            return hi < mi
    return False


def check_nrb_version(
    runner: Runner, minimum: str = MIN_NRB_VERSION, **run_kw
) -> ActionResult:
    """Guard that the located nrb is new enough, via ``nrb --version``.

    Returns an ``ok`` :class:`ActionResult` when nrb is at or above ``minimum``,
    AND -- by design -- when the version cannot be determined at all (nrb missing,
    or an unparseable ``--version`` line). Degrading-to-OK on those cases matches
    :func:`current_trunk_branch`'s best-effort posture: a too-old nrb is the only
    thing this blocks. The downstream nrb call will surface a clear error for a
    truly broken binary, and ``NrbNotFound`` is handled by every action's own
    ``_nrb_missing`` path -- this guard must not double-report it.

    Only a CONFIRMED too-old version returns ``ok=False`` with
    :data:`ErrorKind.NRB_TOO_OLD`, an ASCII actionable title naming the version
    needed, and the wired remedy.

    Like :func:`is_authenticated`, this DOES shell out, so the host must call it
    once on first user action (alongside ``is_authenticated``), NEVER from a
    constantly-polled ``IsActive``.
    """
    try:
        res = runner(cmd_builder.version(), **run_kw)
    except NrbNotFound:
        # The binary itself is missing -- not THIS guard's job to report. Let the
        # real action's _nrb_missing path own that message. Degrade to OK here.
        return ActionResult(ok=True)

    if not res.ok:
        # `nrb --version` should never fail on a working binary. If it does we
        # cannot judge the version, so degrade to OK rather than false-blocking.
        return ActionResult(ok=True)

    have = _parse_nrb_version(res.stdout) or _parse_nrb_version(res.combined)
    if not have:
        return ActionResult(ok=True)

    if _version_is_older(have, minimum):
        return ActionResult(
            ok=False,
            kind=ErrorKind.NRB_TOO_OLD,
            title=f"Update nrb to v{minimum} or newer",
            detail=(
                f"This plugin needs nrb v{minimum} or newer, but the installed "
                f"nrb is v{have}. Update nrb, then retry."
            ),
        )
    return ActionResult(ok=True)


# ── Action 2: Link & clone ───────────────────────────────────────────────────


def list_repos(runner: Runner, org: Optional[str] = None, **run_kw) -> ActionResult:
    """``nrb repo list`` -> parsed rows for the picker dialog."""
    try:
        res = runner(cmd_builder.repo_list(org), **run_kw)
    except NrbNotFound as exc:
        return _nrb_missing(exc)
    fail = _check(res)
    if fail:
        return fail
    return ActionResult(ok=True, detail=res.combined, repos=parse_repo_list(res.stdout))


def clone_and_link(
    runner: Runner,
    store: MappingStore,
    save_as: DocSaver,
    *,
    owner: str,
    repo: str,
    doc_path: str,
    repo_path: str,
    clone_dir: str,
    directory: Optional[str] = None,
    **run_kw,
) -> ActionResult:
    """``nrb clone <owner>/<repo>``, save the doc INTO the clone, then link it.

    Clone-first working model: the native .FCStd must LIVE IN the clone so that
    Check in commits both it and the exported STEP together. After a successful
    clone we ``save_as`` the active document to ``<clone_dir>/<repo_path>`` (the
    injected callable wraps FreeCAD ``doc.saveAs``) and key the mapping by that
    in-clone path -- from then on the user edits the in-clone file.

    ``save_as``    -- callable(target_abs_path) -> path actually written.
    ``doc_path``   -- the document's current (pre-clone) path; not the map key.
    ``repo_path``  -- where the file lives *inside* the repo (forward slashes).
    ``clone_dir``  -- absolute local clone root (becomes cwd for later verbs).

    The clone runs FIRST so we never save into a directory that failed to clone;
    the link is saved only after both the clone and the save succeed.
    """
    try:
        res = runner(cmd_builder.clone(owner, repo, directory), **run_kw)
    except NrbNotFound as exc:
        return _nrb_missing(exc)
    fail = _check(res)
    if fail:
        return fail

    # Save the active document INTO the clone at its repo-relative location.
    # ``repo_path`` is forward-slash; localise it for the filesystem path.
    in_clone_path = os.path.join(clone_dir, repo_path.replace("/", os.sep))
    try:
        saved_path = save_as(in_clone_path)
    except Exception as exc:  # noqa: BLE001 -- surface any saveAs failure as a dialog
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="Could not save the document into the clone",
            detail=str(exc),
        )

    # Key the mapping by the NEW in-clone path. The pre-clone doc was never
    # linked (the link is created here, at clone time), so there is nothing to
    # re-key; rekey() exists for flows that move an already-linked doc.
    new_key = saved_path or in_clone_path
    store.set(
        new_key,
        DocMapping(owner=owner, repo=repo, repo_path=repo_path, clone_dir=clone_dir),
    )
    store.save()
    return ActionResult(ok=True, detail=res.combined)


# ── Action 2b: Open project (join an existing repo, no active document) ───────


def open_project(
    runner: Runner,
    store: MappingStore,
    list_fcstd: FcStdLister,
    open_document: DocOpener,
    select_fcstd: DocSelector,
    *,
    owner: str,
    repo: str,
    clone_dir: str,
    already_cloned: bool = False,
    **run_kw,
) -> ActionResult:
    """Clone (or adopt) an EXISTING repo, open one of its .FCStd files, and link it.

    The document-free counterpart to :func:`clone_and_link`: it needs NO active
    document, so an engineer can join a project that already has CAD files.

    Sequence: ``nrb clone <owner>/<repo>`` INTO ``clone_dir`` (SKIPPED when
    ``already_cloned`` -- the user already has the working copy), then the
    injected ``list_fcstd`` discovers every ``.FCStd`` in the clone,
    ``select_fcstd`` picks one (the host auto-selects a lone file, shows a picker
    for several, returns None on cancel), and ``open_document`` opens it.

    The mapping is keyed by the OPENED document's own absolute path (whatever
    ``open_document`` returns), NOT the path handed to it, so a later Check out
    -- which looks the mapping up by the active document's ``FileName`` --
    resolves it. ``repo_path`` is derived from the opened file via
    :func:`nurbly.assembly.repo_relative_path`; if that is None (the opened file
    is not inside the clone) NO mapping is written and the action fails, rather
    than fabricating a wrong repo-relative path the way the clone-first
    save-into-clone flow can. This action NEVER saves the document: the file
    already lives in the clone.

    Outcomes:
      * clone fails              -> classified failure, no discovery, no mapping.
      * no .FCStd in the clone   -> soft success (the clone is still valid), no mapping.
      * user cancels the picker  -> silent no-op success, no mapping.
      * open fails               -> failure, the clone is left on disk, no mapping.
      * opened file not in clone -> failure, no mapping.
      * success                  -> mapping persisted, keyed by the opened path.
    """
    if not already_cloned:
        try:
            res = runner(cmd_builder.clone(owner, repo, clone_dir), **run_kw)
        except NrbNotFound as exc:
            return _nrb_missing(exc)
        fail = _check(res)
        if fail:
            return fail

    candidates = list_fcstd(clone_dir)
    if not candidates:
        opened_or_cloned = "Opened" if already_cloned else "Cloned"
        return ActionResult(
            ok=True,
            kind=ErrorKind.UNKNOWN,
            title="No CAD documents found",
            detail=(
                f"{opened_or_cloned} {owner}/{repo}, but no CAD document was found "
                f"under {clone_dir}. Add or pull a document, then run Open project "
                f"again (or Check out once it exists)."
            ),
        )

    chosen = select_fcstd(candidates)
    if not chosen:
        # The user cancelled the document picker. The clone (if any) is left on
        # disk; a silent no-op success matches the other commands' cancel paths.
        return ActionResult(ok=True, detail="")

    try:
        opened_path = open_document(chosen)
    except Exception as exc:  # noqa: BLE001 -- surface any open failure as a dialog
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="Could not open the document",
            detail=(
                f"{exc}\n\nThe project is cloned at {clone_dir}; retry Open "
                f"project on it once the document opens."
            ),
        )

    repo_path = assembly.repo_relative_path(opened_path, clone_dir)
    if not repo_path:
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="Document is not inside the project folder",
            detail=(
                f"The opened document\n  {opened_path}\nis not inside the clone\n"
                f"  {clone_dir}\nso it cannot be linked to {owner}/{repo}."
            ),
        )

    store.set(
        opened_path,
        DocMapping(owner=owner, repo=repo, repo_path=repo_path, clone_dir=clone_dir),
    )
    store.save()
    return ActionResult(ok=True, detail=f"Opened {repo_path} from {owner}/{repo}.")


# ── Action 3: Check out (open for edit) ──────────────────────────────────────


def _lock_one(
    runner: Runner,
    mapping: DocMapping,
    repo_path: str,
    *,
    expires_hours: Optional[int],
    message: Optional[str],
    **run_kw,
):
    """Run a single ``nrb lock`` for one repo-relative path.

    Returns ``(ok, lock_id, result)``:
      * ``ok``      -- True if the lock command exited 0.
      * ``lock_id`` -- the scraped id on success (may be ``None`` if exit 0 but
        the id could not be read from ``--json`` stdout).
      * ``result``  -- the ``ActionResult`` to return on FAILURE (a classified
        conflict/error or an nrb-missing result), else ``None`` on success.

    Pure orchestration: never raises -- ``NrbNotFound`` is captured into the
    failure ``result`` so the acquire loop can roll back uniformly.
    """
    try:
        lock_res = runner(
            cmd_builder.lock(
                mapping.owner,
                mapping.repo,
                repo_path,
                expires_hours=expires_hours,
                message=message,
            ),
            **run_kw,
        )
    except NrbNotFound as exc:
        return False, None, _nrb_missing(exc)
    fail = _check(lock_res)
    if fail:
        return False, None, fail
    return True, extract_lock_id(lock_res.stdout), None


def check_out(
    runner: Runner,
    store: MappingStore,
    *,
    doc_path: str,
    dependency_paths: Optional[List[str]] = None,
    expires_hours: Optional[int] = None,
    message: Optional[str] = None,
    **run_kw,
) -> ActionResult:
    """``nrb pull`` (in the clone dir) then ``nrb lock`` the active file AND each
    in-tree linked part (per-part locking).

    The pull runs with ``cwd=clone_dir``. Locks are server-only. The active
    file's ``repo_path`` is locked first, then every dependency that lives
    INSIDE the clone (out-of-tree parts are skipped -- they are not in the repo,
    so there is nothing to lock). Each acquired lock id is cached in the mapping
    store keyed by its repo-relative path so Check in can release them all.

    Per-part closure (pure)
    -----------------------
    ``dependency_paths`` is the active assembly's linked child files, discovered
    by the FreeCAD layer (``nurbly.host.dependencies.collect_dependency_files``)
    and INJECTED so this module stays host-free. They are classified against
    ``mapping.clone_dir``. Only the in-tree subset is locked, each at the
    repo-relative path from :func:`nurbly.assembly.repo_relative_path`. With no
    ``dependency_paths`` (the lone-document case) only the active file is locked
    -- byte-identical to the pre-per-part behaviour.

    Report-and-abort
    ----------------
    The locks are acquired in a loop. If ANY lock fails mid-loop (a conflict, a
    403, etc.) every lock already acquired THIS call is released (best-effort
    ``nrb unlock``) before returning the classified failure, so a partial
    check-out never leaves the user holding a half-set of locks. The store is
    written only once, after the WHOLE set is acquired cleanly.

    If a lock exits 0 but its id cannot be scraped we still succeed for that
    path (it is locked on the server) but flag the result so the UI warns that
    automatic unlock for that file won't work until re-derived.
    """
    mapping = store.get(doc_path)
    if mapping is None:
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="Document is not linked",
            detail="Run Link & clone first so this document maps to a repo.",
        )

    pull_kw = {**run_kw, "cwd": mapping.clone_dir}
    try:
        pull_res = runner(cmd_builder.pull(), **pull_kw)
    except NrbNotFound as exc:
        return _nrb_missing(exc)
    fail = _check(pull_res)
    if fail:
        return fail

    # Build the ordered list of repo-relative paths to lock: the active file
    # first, then each IN-TREE linked part (out-of-tree parts have no repo path
    # to lock). De-dupe so a part that equals the active path is not locked
    # twice. classify_dependencies excludes the active doc itself.
    paths_to_lock: List[str] = [mapping.repo_path]
    if dependency_paths:
        classified = assembly.classify_dependencies(
            doc_path, dependency_paths, mapping.clone_dir
        )
        for dep in classified.in_tree:
            rel = assembly.repo_relative_path(dep, mapping.clone_dir)
            if rel and rel not in paths_to_lock:
                paths_to_lock.append(rel)

    acquired: Dict[str, str] = {}  # repo-relative path -> lock id (this call)
    missing_ids: List[str] = []  # locked but id not scraped (cannot auto-unlock)
    for repo_path in paths_to_lock:
        ok_lock, lock_id, failure = _lock_one(
            runner,
            mapping,
            repo_path,
            expires_hours=expires_hours,
            message=message,
            **run_kw,
        )
        if not ok_lock:
            # Report-and-abort: roll back every lock taken THIS call, then return
            # the classified failure NAMING the offending path. Any lock the
            # rollback could not release (an unlock that itself failed) plus any
            # lock taken exit-0 with an unscrapable id (no id to unlock by) is
            # surfaced so the user can free a stranded server lock by hand rather
            # than leaking it silently. Nothing is written to the store.
            release_failed = _release_locks(runner, mapping, acquired, **run_kw)
            return _abort_failure(failure, repo_path, release_failed, missing_ids)
        if lock_id:
            acquired[repo_path] = lock_id
        else:
            missing_ids.append(repo_path)

    # Whole set acquired cleanly -> cache every id under its repo-relative path.
    store.set_lock_ids(doc_path, acquired)
    store.save()

    detail = "\n".join(f"Locked {p}" for p in paths_to_lock)
    if missing_ids:
        listed = "\n".join(f"  - {p}" for p in missing_ids)
        return ActionResult(
            ok=True,
            kind=ErrorKind.UNKNOWN,
            title="Locked, but could not read every Lock ID",
            detail=(
                f"{detail}\n\n"
                f"These files are locked but their Lock ID could not be read, so "
                f"automatic unlock will not work for them until re-derived via "
                f"'Who has it?':\n{listed}"
            ),
            lock_ids=dict(acquired),
        )
    return ActionResult(ok=True, detail=detail, lock_ids=dict(acquired))


def _release_locks(
    runner: Runner,
    mapping: DocMapping,
    lock_ids: Dict[str, str],
    **run_kw,
) -> List[str]:
    """Best-effort ``nrb unlock`` of every id in ``lock_ids``.

    Returns the repo-relative paths whose release FAILED (or could not run).
    Used both by Check out's report-and-abort rollback and by Check in's release
    loop. NEVER raises: an ``NrbNotFound`` or a non-zero unlock is recorded as a
    failed path, not propagated, so one bad release cannot abort the rest.
    """
    failed: List[str] = []
    for repo_path, lock_id in lock_ids.items():
        try:
            res = runner(
                cmd_builder.unlock(mapping.owner, mapping.repo, lock_id), **run_kw
            )
        except NrbNotFound:
            failed.append(repo_path)
            continue
        if not res.ok:
            failed.append(repo_path)
    return failed


def _abort_failure(
    failure: ActionResult,
    offending_path: str,
    release_failed: List[str],
    stranded_ids: List[str],
) -> ActionResult:
    """Enrich a mid-loop lock failure with the offending path + stranded locks.

    The acquire loop rolls back every lock it took this call, but two kinds of
    lock can survive that rollback and stay held on the server: one whose own
    unlock failed (``release_failed``), and one that locked exit-0 but whose id
    could not be scraped, so there was no id to release it by (``stranded_ids``).
    Both are listed so the user can free them via 'Who has it?' rather than
    leaking a server lock. The classified failure (title + kind) is kept as-is,
    only the detail pane is enriched. Never raises.
    """
    detail = failure.detail or ""
    if offending_path:
        prefix = f"Could not lock {offending_path}."
        detail = f"{prefix}\n\n{detail}" if detail else prefix
    stranded = list(dict.fromkeys([*release_failed, *stranded_ids]))
    if stranded:
        listed = "\n".join(f"  - {p}" for p in stranded)
        detail = (
            f"{detail}\n\n"
            f"These locks may still be held on the server (their automatic "
            f"release did not complete). Free them via 'Who has it?' then "
            f"'nrb unlock':\n{listed}"
        )
    return replace(failure, detail=detail)


# ── Action 4: Check in (save & publish) ──────────────────────────────────────


def check_in(
    runner: Runner,
    store: MappingStore,
    save_document: Callable[[], None],
    export_step: StepExporter,
    *,
    doc_path: str,
    commit_message: str,
    step_output_path: str,
    dependency_paths: Optional[List[str]] = None,
    relocate_dependencies: Optional[DependencyRelocator] = None,
    copy_fn: Optional[CopyFn] = None,
    repath_fn: Optional[RepathFn] = None,
    **run_kw,
) -> ActionResult:
    """Save the in-clone doc, export STEP beside it, then ``add . -> commit -m
    -> push -> unlock``.

    Clone-first sequence (PLUGIN_MVP.md action 4, clone-first revision):
      0a. ``save_document()``           -- persist the in-clone .FCStd in place.
      0b. ``export_step(step_output_path)`` -- the only per-CAD geometry step.
      1.  ``nrb add .``        (cwd = clone dir) -- stages BOTH the .FCStd AND
          the .step, because the .FCStd now lives in the clone.
      2.  ``nrb commit -m``    (cwd = clone dir)
      3.  ``nrb push``         (cwd = clone dir)
      4.  ``nrb unlock <owner>/<repo> <lock-id>``  (server-only, per locked part)

    Both inherently-FreeCAD steps (save + export) are INJECTED so this stays
    host-free. Unlock releases EVERY lock cached by the matching Check out (the
    active file plus each in-tree part). If push is rejected we stop and DO NOT
    unlock (the user still holds the locks and their work is safe locally). On a
    clean push the released locks are cleared and the commit short-sha is
    surfaced in the result detail.

    Dependency closure (v2, auto-relocate)
    --------------------------------------
    ``dependency_paths`` is the active assembly's linked child files, discovered
    by the FreeCAD layer (``nurbly.host.dependencies.collect_dependency_files``)
    and passed in so this module stays pure. They are classified against
    ``mapping.clone_dir`` via :func:`nurbly.assembly.classify_dependencies`
    BEFORE the ``add`` step, because ``nrb add .`` only stages the clone working
    dir: deps INSIDE the clone are committed, but deps OUTSIDE it would be
    silently dropped from the push.

    Two relocation injection shapes are supported, tried in this order; either
    one pulls the out-of-tree parts into the clone so they get committed too:

      * Split shape (``copy_fn`` + ``repath_fn``): core builds the plan via
        :func:`nurbly.assembly.build_relocation_plan`, ``copy_fn(src, dest)``
        copies each part under ``<clone>/deps/`` (injected, e.g. ``shutil.copy2``
        -- core never imports shutil), ``repath_fn(plan)`` re-points the
        document's links to the in-clone copies (injected -- it touches the
        FreeCAD App::Link API), and core RE-SAVES so the re-pathed links are on
        disk before ``add .`` stages them. ``copy_fn`` and ``repath_fn`` MUST be
        supplied together: passing one without the other is a wiring bug and
        raises ``ValueError`` rather than silently dropping to the warning.
      * One-callable shape (``relocate_dependencies``): core hands the host a
        :func:`nurbly.assembly.plan_relocation` plan and the host copies each
        part into the clone's ``parts/`` dir, rewrites the assembly's links,
        PERSISTS the re-linked assembly itself (the host owns that save), and
        returns the UNRESOLVED subset it could not move. core does not re-save in
        this shape -- the rewritten links are already on disk. Any exception
        degrades to the v1 warning (every out-of-tree path stays unresolved).

    On a clean push the split shape reports a SUCCESS saying how many parts were
    relocated (``ok=True``, ``kind=OK``). Whatever stays out of tree (the split
    shape was not wired, the one-callable relocator returned unresolved paths, or
    no relocator was injected at all) feeds the v1 soft-success warning
    (``ok=True``, ``kind=ASSEMBLY_DEPS_OUTSIDE``) listing the dropped files. With
    no out-of-tree deps (the common single-document case) behaviour is unchanged
    and no relocator is ever called.
    """
    mapping = store.get(doc_path)
    if mapping is None:
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="Document is not linked",
            detail="Run Link & clone first so this document maps to a repo.",
        )

    # Asymmetric injection of the split relocation shape is a caller bug:
    # relocation needs BOTH the copy and the re-path callable. Fail loudly rather
    # than silently dropping to the v1 "parts left outside" warning, which would
    # hide the wiring mistake.
    if (copy_fn is None) != (repath_fn is None):
        raise ValueError(
            "check_in: copy_fn and repath_fn must be provided together "
            "(got one without the other)"
        )

    # Step 0a: save the in-clone document FIRST so the .FCStd on disk reflects
    # the latest edits before we export + stage it.
    try:
        save_document()
    except Exception as exc:  # noqa: BLE001 -- surface any save failure as a dialog
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="Could not save the document",
            detail=str(exc),
        )

    # Step 0b: export the STEP next to the saved .FCStd.
    try:
        export_step(step_output_path)
    except Exception as exc:  # noqa: BLE001 -- surface any exporter failure as a dialog
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="STEP export failed",
            detail=str(exc),
        )

    # Step 0c: classify deps BEFORE staging. ``add .`` only stages the clone, so
    # out-of-tree parts would be silently dropped. We then relocate them into the
    # clone (when a relocator is wired) so they get committed. ``relocated`` is
    # the split-shape plan we carried out (used for the success message), and
    # ``unresolved`` is whatever is STILL out of tree after relocation -- it
    # feeds the post-push warning below.
    #
    # v1 (no relocator wired): no relocation runs, so ``unresolved`` IS the
    # original out-of-tree set and behaviour is unchanged.
    relocated: List[assembly.RelocationOp] = []
    unresolved: List[str] = []
    if dependency_paths:
        classified = assembly.classify_dependencies(
            doc_path, dependency_paths, mapping.clone_dir
        )
        unresolved = list(classified.out_of_tree)
        if classified.out_of_tree:
            if copy_fn is not None and repath_fn is not None:
                # Split shape: core builds the plan, copies each part under
                # ``<clone>/deps/``, re-paths the links, then RE-SAVES so the new
                # links hit disk before ``add .`` stages them. Everything in the
                # plan is relocated; nothing is left unresolved on success.
                plan = assembly.build_relocation_plan(
                    classified.out_of_tree, mapping.clone_dir, classified.in_tree
                )
                try:
                    for op in plan:
                        copy_fn(op.src, op.dest)
                    repath_fn(plan)
                    # Re-save so the re-pathed links hit disk before ``add .``.
                    save_document()
                except Exception as exc:  # noqa: BLE001 -- surface as a dialog
                    return ActionResult(
                        ok=False,
                        kind=ErrorKind.UNKNOWN,
                        title="Could not relocate linked parts into the clone",
                        detail=str(exc),
                    )
                relocated = plan
                unresolved = []
            elif relocate_dependencies is not None:
                # One-callable shape: hand the host a plan_relocation() plan, let
                # it copy + re-link + persist the assembly, and take back the
                # subset it could not move. The host owns its own save here, so
                # core does not re-save. Any exception degrades to v1 (the whole
                # out-of-tree set is treated as unresolved).
                try:
                    plan = assembly.plan_relocation(
                        doc_path,
                        classified.out_of_tree,
                        mapping.clone_dir,
                        mapping.repo_path,
                    )
                    unresolved = list(relocate_dependencies(plan))
                except Exception:  # noqa: BLE001 -- any failure degrades to v1
                    unresolved = list(classified.out_of_tree)

    cwd_kw = {**run_kw, "cwd": mapping.clone_dir}
    try:
        # ``add .`` stages both the .FCStd and the .step in one shot.
        add_res = runner(cmd_builder.add(), **cwd_kw)
        fail = _check(add_res)
        if fail:
            return fail

        commit_res = runner(cmd_builder.commit(commit_message), **cwd_kw)
        fail = _check(commit_res)
        if fail:
            return fail

        push_res = runner(cmd_builder.push(), **cwd_kw)
        fail = _check(push_res)
        if fail:
            return fail
    except NrbNotFound as exc:
        return _nrb_missing(exc)

    held = dict(mapping.lock_ids)  # every lock acquired by the matching Check out
    if not held:
        # Pushed fine but we have no locks to release (ids were lost, or the file
        # was checked in without a prior check-out). Succeed, but tell the user
        # to release via 'Who has it?' if a stale lock lingers.
        return ActionResult(
            ok=True,
            kind=ErrorKind.UNKNOWN,
            title="Pushed, but no Lock ID was cached to unlock",
            detail="If a lock is still held, re-derive it via 'Who has it?'.",
        )

    # Release EVERY cached lock. Never abort on the first failure -- a later lock
    # may still release cleanly, and a stuck one must not strand the rest held.
    # _release_locks is best-effort and never raises (an unrunnable unlock, e.g.
    # nrb vanished, is recorded as a still-held path), so a single bad release
    # cannot abort the rest.
    failed = _release_locks(runner, mapping, held, **run_kw)

    sha = extract_commit_sha(commit_res.stdout)
    if failed:
        # Pushed OK but some releases failed. Clear only the ones that DID
        # release. Keep the stuck ids cached (we clear only on a clean release)
        # and surface each plus its exact manual command. Keep a classified kind
        # so REMEDIES still resolve.
        still_held = {p: held[p] for p in failed}
        store.set_lock_ids(doc_path, still_held)
        store.save()
        lines = []
        for repo_path in failed:
            lock_id = held[repo_path]
            cmd = f"nrb unlock {mapping.owner_repo} {lock_id}"
            lines.append(f"  {repo_path}\n    Lock ID: {lock_id}\n    {cmd}")
        listed = "\n".join(lines)
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="Pushed, but releasing the lock failed",
            detail=(
                f"Your changes were pushed, but these locks are still held.\n"
                f"Release each one manually:\n{listed}"
            ),
            lock_ids=still_held,
        )

    # Every lock released cleanly -> clear the whole cache.
    store.clear_lock_ids(doc_path)
    store.save()
    released = "\n".join(f"Released {p}" for p in held)
    detail = f"Commit {sha}\n{released}" if sha else released

    # Push + unlock both succeeded. Report the assembly dependency outcome:
    #   * relocated -- the split shape pulled the out-of-tree parts into the
    #     clone (step 0c) so they WERE committed; a clean success that says how
    #     many moved.
    #   * unresolved -- parts STILL out of tree (no relocator wired, or the
    #     one-callable relocator could not move them). ``add .`` dropped them, so
    #     fall back to the v1 soft-success warning so the user can move + re-link
    #     them by hand.
    if relocated:
        n = len(relocated)
        listed = "\n".join(f"  - {op.src} -> {op.dest}" for op in relocated)
        return ActionResult(
            ok=True,
            detail=(
                f"{detail}\n\n"
                f"Relocated {n} part(s) into the clone (now committed):\n{listed}"
            ),
        )

    if unresolved:
        listed = "\n".join(f"  - {p}" for p in unresolved)
        return ActionResult(
            ok=True,
            kind=ErrorKind.ASSEMBLY_DEPS_OUTSIDE,
            title="Checked in, but some linked parts were not committed",
            detail=(
                f"{detail}\n\n"
                f"These linked parts live OUTSIDE the repo clone and were "
                f"NOT committed:\n{listed}"
            ),
        )

    return ActionResult(ok=True, detail=detail)


# ── Action 5: Who has it? ────────────────────────────────────────────────────


def who_has_it(
    runner: Runner,
    store: MappingStore,
    *,
    doc_path: Optional[str] = None,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    mine: bool = False,
    **run_kw,
) -> ActionResult:
    """``nrb locks <owner>/<repo>`` (or ``--my``) -> parsed lock rows.

    Owner/repo come from the linked document when ``doc_path`` is given,
    otherwise from explicit ``owner``/``repo``, otherwise ``--my``.
    """
    if not mine and (owner is None or repo is None):
        if doc_path is not None:
            mapping = store.get(doc_path)
            if mapping is None:
                return ActionResult(
                    ok=False,
                    kind=ErrorKind.UNKNOWN,
                    title="Document is not linked",
                    detail="Run Link & clone first, or use 'my locks'.",
                )
            owner, repo = mapping.owner, mapping.repo
        else:
            mine = True  # nothing to scope by -> fall back to the user's own locks

    try:
        res = runner(cmd_builder.locks(owner, repo, mine=mine), **run_kw)
    except NrbNotFound as exc:
        return _nrb_missing(exc)
    fail = _check(res)
    if fail:
        return fail
    return ActionResult(ok=True, detail=res.combined, locks=parse_locks(res.stdout))


# ── Action 6: Switch branch ──────────────────────────────────────────────────


def list_branches(runner: Runner, **run_kw) -> ActionResult:
    """``nrb branch`` -> parsed rows for the Switch-branch picker.

    ``run_kw`` MUST carry ``cwd=<clone_dir>`` so the listing reflects the right
    working copy; the host resolves it from the active document's mapping. The
    current branch is flagged on its :class:`~nurbly.output_parser.BranchRow`.
    """
    try:
        res = runner(cmd_builder.branch_list(), **run_kw)
    except NrbNotFound as exc:
        return _nrb_missing(exc)
    fail = _check(res)
    if fail:
        return fail
    return ActionResult(
        ok=True, detail=res.combined, branches=parse_branch_list(res.stdout)
    )


def switch_branch(
    runner: Runner,
    branch: str,
    *,
    create: bool = False,
    allow_dirty: bool = False,
    **run_kw,
) -> ActionResult:
    """``nrb switch [-c] <branch>`` -- set the working branch in the clone.

    ``run_kw`` MUST carry ``cwd=<clone_dir>`` so the switch targets the right
    working copy; the host resolves it from the active document's mapping and
    reloads the active document afterwards (the files on disk change).

    Dirty-tree guard
    ----------------
    When switching to an EXISTING branch (``create=False``) and ``allow_dirty``
    is False, a ``nrb status`` pre-check runs FIRST. If the tree is not clean we
    return a ``DIRTY_TREE`` soft-block -- naming nothing destructive has happened
    yet -- so the user commits or discards before HEAD moves. Switching with
    uncommitted changes can silently carry edits onto the wrong branch or fail
    mid-checkout, so we stop before touching anything.

    Creating a branch (``create=True``) SKIPS the pre-check: the new branch is
    cut from the current commit, so the working tree is preserved in place and no
    edit can be lost. This mirrors ``nrb switch -c`` / ``git switch -c``.
    """
    branch = (branch or "").strip()
    if not branch:
        return ActionResult(
            ok=False,
            kind=ErrorKind.UNKNOWN,
            title="No branch name",
            detail="Enter or pick a branch to switch to.",
        )

    if not create and not allow_dirty:
        try:
            status_res = runner(cmd_builder.status(), **run_kw)
        except NrbNotFound as exc:
            return _nrb_missing(exc)
        fail = _check(status_res)
        if fail:
            return fail
        if not is_working_tree_clean(status_res.stdout):
            return ActionResult(
                ok=False,
                kind=ErrorKind.DIRTY_TREE,
                title="You have uncommitted changes",
                detail=(
                    "Commit or discard your changes before switching branches, "
                    "so your edits are not lost or mixed across branches.\n\n"
                    + status_res.combined
                ),
            )

    try:
        res = runner(cmd_builder.switch(branch, create=create), **run_kw)
    except NrbNotFound as exc:
        return _nrb_missing(exc)
    fail = _check(res)
    if fail:
        return fail
    return ActionResult(ok=True, detail=res.combined)


# Default-branch names a Nurbly repo is created with, plus git's historical
# default. A Check in on one of these pushes straight to the shared trunk, which
# the plugin WARNS about (never blocks) so a user new to git-style repos learns
# the branch + pull-request habit. Compared case-insensitively.
TRUNK_BRANCH_NAMES = ("main", "master")


def current_trunk_branch(runner: Runner, **run_kw) -> Optional[str]:
    """If the clone's current branch is a shared-trunk branch, return its name.

    Returns the current branch name (e.g. ``"main"``) when it is one of
    :data:`TRUNK_BRANCH_NAMES`, else ``None``. Used by the host's Check in flow
    to warn -- never block -- a push straight to the trunk.

    Best-effort by design: any failure to read the branch (``nrb`` missing, a
    non-zero ``nrb branch``, or no current branch in the listing) returns
    ``None``, so a detection problem can NEVER stop a Check in. ``run_kw`` MUST
    carry ``cwd=<clone_dir>`` so the listing reflects the right working copy.
    """
    # Broad catch on purpose: this is a best-effort advisory probe, so it must
    # NEVER raise out into the Check in flow (which runs it with no surrounding
    # try/except). NrbNotFound is the modelled case, but nrb_runner.run only
    # converts a subprocess TIMEOUT into a result -- a spawn-level OSError /
    # PermissionError / ValueError (nrb resolves but is not executable, a
    # transient OS failure) would otherwise escape and abort the Check in.
    try:
        res = runner(cmd_builder.branch_list(), **run_kw)
    except Exception:  # noqa: BLE001 -- detection must never block Check in
        return None
    if not res.ok:
        return None
    for row in parse_branch_list(res.stdout):
        if row.is_current:
            name = row.name.strip()
            return name if name.lower() in TRUNK_BRANCH_NAMES else None
    return None

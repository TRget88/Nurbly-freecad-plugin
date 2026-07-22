# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""FreeCADGui command classes -- the Nurbly toolbar/menu buttons.

FREECAD-ONLY. Each command is a small class FreeCAD registers via
``FreeCADGui.addCommand``. They translate a button click into a :mod:`core`
action, passing a real nrb runner and the real STEP exporter, then render the
:class:`core.ActionResult` through :mod:`gui_dialogs`.

Design rules pinned by the research gotchas:
  * ``IsActive`` is polled constantly by FreeCAD -- it MUST be O(1) with no
    subprocess or disk I/O. We only check ``ActiveDocument`` presence.
  * ``GetResources`` is cached, so labels are static (dynamic labels are a
    deferred feature).
  * Command ids are globally unique (``Nurbly_*``).

The auth check (``nrb whoami``) and all nrb calls happen inside ``Activated``,
never in ``IsActive``.
"""

from __future__ import annotations

import os

import FreeCAD as App  # noqa: WPS433 -- this whole module is FreeCAD-only
import FreeCADGui as Gui

from .. import core, links, nrb_runner
from ..config_store import PluginConfig
from ..errors import ErrorKind
from ..mapping_store import MappingStore
from . import dependencies, freecad_export, gui_dialogs, relink

# ── icons ────────────────────────────────────────────────────────────────────

# Toolbar/menu icons live at the addon root (resources/icons/), three dirs up
# from this file (nurbly/host/gui_commands.py). FreeCAD accepts an absolute
# file path as a command Pixmap, so we hand it the SVG path directly rather than
# registering named Qt resources (unregistered names render as a "?").
_ICON_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "resources",
    "icons",
)


def _icon(name: str) -> str:
    """Absolute path to a bundled toolbar icon SVG."""
    return os.path.join(_ICON_DIR, name)


# ── shared wiring ────────────────────────────────────────────────────────────


def _runner(args, **kw):
    """The concrete nrb runner handed to ``core`` (matches the Runner type).

    First-run "locate nrb": feed the persisted path (if any) into
    ``locate_nrb``'s explicit-override slot. If nrb still can't be found, open
    the file picker, PERSIST the chosen path to the plugin config, and retry so
    the location sticks across launches.
    """
    cfg = PluginConfig().load()
    saved = cfg.get_nrb_path()
    try:
        return nrb_runner.run(args, config_override=saved, **kw)
    except nrb_runner.NrbNotFound:
        chosen = gui_dialogs.prompt_nrb_path()
        if not chosen:
            raise
        cfg.set_nrb_path(chosen)
        cfg.save()
        return nrb_runner.run(args, config_override=chosen, **kw)


def _store() -> MappingStore:
    return MappingStore().load()


def _web_url_override():
    """The plugin config's ``web_url`` override for deep links, or None.

    A malformed/partial config must never break a deep link (Sign in's "Get an
    access key", Check in's "Open the guide"), so a read error falls back to
    None and ``links.*`` uses ``NURBLY_WEB_URL`` / the managed default.
    """
    try:
        return PluginConfig().load().get_web_url()
    except Exception:  # noqa: BLE001 -- a bad config must not block the action
        return None


def _active_doc_path():
    """Absolute path of the active document's .FCStd, or ``None`` if unsaved."""
    doc = App.ActiveDocument
    if doc is None or not doc.FileName:
        return None
    return os.path.abspath(doc.FileName)


def _list_fcstd(clone_dir):
    """Every .FCStd document under ``clone_dir`` (recursive), absolute + sorted.

    The concrete filesystem walk for ``core.open_project``'s injected lister.
    Skips dot-directories (``.git`` and friends), never follows symlinks out of
    the tree, and excludes FreeCAD backups (``*.FCStd1``) because the suffix
    test is the exact lowercased ``.fcstd``. Per-directory read errors are
    swallowed so a permission-denied subtree cannot abort discovery.
    """
    found = []
    for root, dirs, files in os.walk(clone_dir, onerror=lambda _e: None, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.lower().endswith(".fcstd"):
                found.append(os.path.join(root, name))
    return sorted(found)


def _open_document(path):
    """Open (or re-activate) the .FCStd at ``path``; return its absolute path.

    The concrete opener for ``core.open_project``. Returns the OPENED document's
    own ``FileName`` (canonicalised) so the caller keys the doc->repo mapping by
    exactly what ``_active_doc_path`` will later report -- otherwise Check out
    would miss the mapping and wrongly report "not linked". Re-uses an
    already-open document for the same file rather than opening a duplicate.
    """
    target = os.path.normcase(os.path.abspath(path))
    for doc in App.listDocuments().values():
        existing = doc.FileName
        if existing and os.path.normcase(os.path.abspath(existing)) == target:
            App.setActiveDocument(doc.Name)
            return os.path.abspath(existing)
    doc = App.openDocument(path)
    return os.path.abspath(doc.FileName or path)


def _reload_document(path):
    """Close + reopen the document at ``path`` after a branch switch.

    The on-disk .FCStd changed when nrb checked out the other branch, so the
    open document is stale. We close the matching open document (a clean working
    tree was enforced before the switch, so nothing unsaved is lost) and reopen
    the file. Returns ``""`` on success, or a short note if the document no
    longer exists on the new branch (so the caller can surface it). Never raises.
    """
    target = os.path.normcase(os.path.abspath(path))
    for doc in list(App.listDocuments().values()):
        existing = doc.FileName
        if existing and os.path.normcase(os.path.abspath(existing)) == target:
            App.closeDocument(doc.Name)
            break
    if not os.path.isfile(path):
        return "This document does not exist on the new branch; nothing reopened."
    try:
        reopened = App.openDocument(path)
        App.setActiveDocument(reopened.Name)
        return ""
    except Exception as exc:  # noqa: BLE001 -- a reopen failure must not crash the command
        return f"Could not reopen the document: {exc}"


def _render(result: core.ActionResult, *, success_msg: str) -> None:
    """Common result -> dialog rendering for the simple actions."""
    if result.ok and result.kind == ErrorKind.OK:
        gui_dialogs.show_success(success_msg, detail=result.detail)
    elif result.ok:
        # Soft-success with a caveat (e.g. lock id not captured).
        gui_dialogs.show_error(result.title, result.kind, result.detail, result.remedy)
    else:
        gui_dialogs.show_error(result.title, result.kind, result.detail, result.remedy)


def _require_saved_doc():
    """Return the abs doc path or show an error and return ``None``."""
    doc_path = _active_doc_path()
    if doc_path is None:
        gui_dialogs.show_error(
            "Save the document first",
            ErrorKind.UNKNOWN,
            "This action needs a saved .FCStd file. Use File -> Save, then retry.",
        )
    return doc_path


# ── 1. Sign in ───────────────────────────────────────────────────────────────


class SignInCommand:
    def GetResources(self):
        return {
            "Pixmap": _icon("sign-in.svg"),
            "MenuText": "Sign in",
            "ToolTip": "Authenticate the nrb CLI with a Personal Access Token",
        }

    def IsActive(self):
        return True  # always available

    def Activated(self):
        # Resolve the "Get an access key" deep link: an optional config override
        # (web_url, for a dev pointing at a local stack), else NURBLY_WEB_URL,
        # else the managed default. A malformed config must never block sign-in,
        # so fall back to the default link if reading it raises.
        keys_url = links.api_keys_url(_web_url_override())
        token = gui_dialogs.prompt_token(keys_url=keys_url)
        if not token:
            return
        result = core.sign_in(_runner, token)
        _render(result, success_msg="Signed in to Nurbly.")


# ── 2. Link & clone ──────────────────────────────────────────────────────────


class LinkCloneCommand:
    def GetResources(self):
        return {
            "Pixmap": _icon("link.svg"),
            "MenuText": "Link && clone",
            "ToolTip": "Pick a repo, clone it, and link the active document to it",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        doc_path = _require_saved_doc()
        if doc_path is None:
            return

        listing = core.list_repos(_runner)
        if not listing.ok:
            gui_dialogs.show_error(listing.title, listing.kind, listing.detail, listing.remedy)
            return

        chosen = gui_dialogs.pick_repo(listing.repos)
        if chosen is None:
            return

        # Clone-first model: create <parent>/<repo> and SAVE the active document
        # INTO it. <parent> MUST be writable -- example/unsaved docs live under
        # the read-only FreeCAD install tree (e.g. C:/Program Files/.../examples),
        # so default to a writable folder and let the user choose where the
        # working copy lands instead of failing with "Access is denied".
        default_parent = core.default_clone_parent(doc_path)
        parent_dir = gui_dialogs.prompt_clone_parent(default_parent)
        if parent_dir is None:
            return
        clone_dir = os.path.join(parent_dir, chosen.repo)
        # The file's repo-relative path defaults to its basename for the MVP
        # (single active document, no assembly graph).
        repo_path = os.path.basename(doc_path)

        doc = App.ActiveDocument

        def _save_as(target_path):
            # clone_dir exists post-clone, but repo_path may add subdirs.
            parent = os.path.dirname(target_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            doc.saveAs(target_path)
            # doc.FileName is now the in-clone path; that becomes the map key.
            return os.path.abspath(doc.FileName or target_path)

        result = core.clone_and_link(
            _runner,
            _store(),
            _save_as,
            owner=chosen.owner,
            repo=chosen.repo,
            doc_path=doc_path,
            repo_path=repo_path,
            clone_dir=clone_dir,
            directory=clone_dir,
        )
        _render(
            result,
            success_msg=f"Cloned {chosen.owner_repo} and linked this document.",
        )


# ── 2b. Open project ─────────────────────────────────────────────────────────


class OpenProjectCommand:
    """Join an EXISTING project: clone (or open an already-cloned) repo and open it.

    The document-free counterpart to Link & clone. It needs NO active document
    (``IsActive`` is always True), so an engineer can open a project that already
    has CAD files without first creating a part. It clones the picked repo into a
    chosen folder (skipping the clone when that folder is already a working
    copy), discovers the ``.FCStd`` documents, opens the one the user picks
    (auto for a single document), and persists the doc<->repo link so Check out /
    Check in / Who has it work immediately on the opened document.
    """

    def GetResources(self):
        return {
            "Pixmap": _icon("open-project.svg"),
            "MenuText": "Open project",
            "ToolTip": "Clone an existing project (or open one already cloned) and open its document",
        }

    def IsActive(self):
        return True  # document-free entry point -- never needs an open document

    def Activated(self):
        listing = core.list_repos(_runner)
        if not listing.ok:
            gui_dialogs.show_error(listing.title, listing.kind, listing.detail, listing.remedy)
            return

        chosen = gui_dialogs.pick_repo(listing.repos)
        if chosen is None:
            return

        # No active document, so default the clone parent to the user's home.
        parent_dir = gui_dialogs.prompt_clone_parent(core.default_clone_parent(None))
        if parent_dir is None:
            return
        clone_dir = os.path.join(parent_dir, chosen.repo)
        # Adopt an existing working copy instead of re-cloning into it (nrb/git
        # refuse a non-empty target dir). The .git probe lives HERE, never in
        # IsActive (which FreeCAD polls and which must stay O(1) with no IO).
        already_cloned = os.path.isdir(os.path.join(clone_dir, ".git"))

        def _select(candidates):
            return gui_dialogs.pick_document(candidates, clone_dir)

        result = core.open_project(
            _runner,
            _store(),
            _list_fcstd,
            _open_document,
            _select,
            owner=chosen.owner,
            repo=chosen.repo,
            clone_dir=clone_dir,
            already_cloned=already_cloned,
        )
        _render(result, success_msg=f"Opened {chosen.owner_repo}.")


# ── 3. Check out ─────────────────────────────────────────────────────────────


class CheckOutCommand:
    def GetResources(self):
        return {
            "Pixmap": _icon("check-out.svg"),
            "MenuText": "Check out",
            "ToolTip": "Pull latest and lock this file (and its in-tree parts) for editing",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        doc_path = _require_saved_doc()
        if doc_path is None:
            return
        # Gather the assembly's linked child files (empty for a single document)
        # and INJECT them so core can lock each in-tree part alongside the active
        # file (per-part locking). core classifies them against the clone dir and
        # locks only the in-tree subset. Out-of-tree parts have no repo path to
        # lock. The same discovery helper feeds Check in.
        dependency_paths = dependencies.collect_dependency_files(App.ActiveDocument)
        result = core.check_out(
            _runner,
            _store(),
            doc_path=doc_path,
            dependency_paths=dependency_paths,
        )
        _render(result, success_msg="Checked out -- you hold the lock(s).")


# ── 4. Check in ──────────────────────────────────────────────────────────────


class CheckInCommand:
    def GetResources(self):
        return {
            "Pixmap": _icon("check-in.svg"),
            "MenuText": "Check in",
            "ToolTip": "Export STEP, commit, push, and release the lock",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        doc_path = _require_saved_doc()
        if doc_path is None:
            return

        store = _store()
        mapping = store.get(doc_path)
        if mapping is None:
            gui_dialogs.show_error(
                "Document is not linked",
                ErrorKind.UNKNOWN,
                "Run Link & clone first.",
            )
            return

        # Inform (never block) a Check in that pushes straight to the shared
        # trunk. Done BEFORE the commit-message prompt so a user new to
        # git-style repos can bail early and make a branch instead. The check
        # is best-effort: current_trunk_branch returns None on any read error,
        # so a detection problem can't stop a Check in.
        trunk = core.current_trunk_branch(_runner, cwd=mapping.clone_dir)
        if trunk and not gui_dialogs.confirm_push_to_trunk(
            trunk, links.branching_guide_url(_web_url_override())
        ):
            return

        message = gui_dialogs.prompt_commit_message()
        if not message:
            return

        # STEP goes next to the file inside the clone, at the same repo-relative
        # location, with a .step extension.
        step_path = freecad_export.step_path_for(
            os.path.join(mapping.clone_dir, mapping.repo_path)
        )

        doc = App.ActiveDocument

        def _save():
            # In-place save of the in-clone .FCStd (clone-first: the active doc
            # already points at the file inside the clone).
            doc.save()

        def _export(path):
            return freecad_export.export_active_document(path)

        def _relocate(plan):
            # Assembly v2: core hands us a plan_relocation() list describing each
            # out-of-tree linked part's move into the clone's parts/ folder. We
            # ask for CONSENT first (copying + re-linking edits the user's
            # assembly), then perform the move. Return value is core's
            # ``unresolved`` list of ORIGINAL out-of-tree sources.
            if not plan:
                return []
            out_of_tree = [t.src for t in plan]
            if not gui_dialogs.confirm_relocation(out_of_tree):
                # Declined: leave the parts where they are so they flow through
                # the existing v1 out-of-tree warning. Every source is unresolved.
                return out_of_tree
            # Proceed: relink copies + re-points + persists the assembly itself,
            # returning only the subset it could not relocate. open_document lets
            # relink re-bind native App::Link parts (which carry no FileName) by
            # opening the relocated copy and swapping the link target to it.
            return relink.relocate_dependencies(
                doc, plan, open_document=App.openDocument
            )

        # Gather the assembly's linked child files (empty for a single document).
        # core classifies them against the clone dir and, for any that live
        # outside the clone, hands ``_relocate`` a plan to copy + re-link them
        # into the clone (assembly v2). Anything still unresolved is warned about.
        dependency_paths = dependencies.collect_dependency_files(doc)

        result = core.check_in(
            _runner,
            store,
            _save,
            _export,
            doc_path=doc_path,
            commit_message=message,
            step_output_path=step_path,
            dependency_paths=dependency_paths,
            relocate_dependencies=_relocate,
        )
        _render(result, success_msg="Checked in -- pushed and unlocked.")


# ── 5. Who has it? ───────────────────────────────────────────────────────────


class WhoHasItCommand:
    def GetResources(self):
        return {
            "Pixmap": _icon("who-has-it.svg"),
            "MenuText": "Who has it?",
            "ToolTip": "List active locks on this document's repository",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        doc_path = _active_doc_path()
        store = _store()
        result = core.who_has_it(_runner, store, doc_path=doc_path)
        if not result.ok:
            gui_dialogs.show_error(result.title, result.kind, result.detail, result.remedy)
            return
        mapping = store.get(doc_path) if doc_path else None
        scope = mapping.owner_repo if mapping else "your locks"
        gui_dialogs.show_locks(result.locks, scope)


# ── 6. Switch branch ─────────────────────────────────────────────────────────


class SwitchBranchCommand:
    """Set the working branch for the active document's clone.

    Lists the clone's local branches, lets the user pick one (or create a new
    one cut from the current branch), and runs ``nrb switch``. A dirty working
    tree blocks an existing-branch switch (core's pre-check) so uncommitted work
    is never mixed across branches. On success the active document is reloaded
    so FreeCAD shows the switched-to branch's version.
    """

    def GetResources(self):
        return {
            "Pixmap": _icon("switch-branch.svg"),
            "MenuText": "Switch branch",
            "ToolTip": "Set the working branch for this document's repository clone",
        }

    def IsActive(self):
        return App.ActiveDocument is not None

    def Activated(self):
        doc_path = _require_saved_doc()
        if doc_path is None:
            return

        store = _store()
        mapping = store.get(doc_path)
        if mapping is None:
            gui_dialogs.show_error(
                "Document is not linked",
                ErrorKind.UNKNOWN,
                "Run Link & clone (or Open project) first so this document maps "
                "to a repo clone, then Switch branch.",
            )
            return

        listing = core.list_branches(_runner, cwd=mapping.clone_dir)
        if not listing.ok:
            gui_dialogs.show_error(
                listing.title, listing.kind, listing.detail, listing.remedy
            )
            return

        current = next((b.name for b in listing.branches if b.is_current), "")
        picked = gui_dialogs.pick_branch(listing.branches, current)
        if picked is None:
            return
        branch, create = picked
        if not create and branch == current:
            return  # already on it -- nothing to do

        result = core.switch_branch(
            _runner, branch, create=create, cwd=mapping.clone_dir
        )
        if not result.ok:
            gui_dialogs.show_error(
                result.title, result.kind, result.detail, result.remedy
            )
            return

        # A create-and-switch keeps the same working tree, so only a switch to an
        # EXISTING branch needs the active document reloaded from the new files.
        note = "" if create else _reload_document(doc_path)
        verb = "Created and switched to" if create else "Switched to"
        msg = f"{verb} branch '{branch}'."
        if note:
            msg = f"{msg}\n{note}"
        gui_dialogs.show_success(msg, detail=result.detail)


# ── 7. Settings ──────────────────────────────────────────────────────────────


class SettingsCommand:
    """Open the Nurbly settings panel (nrb path, defaults).

    The dialog lives in :mod:`nurbly.host.settings_panel`. We import it lazily
    inside ``Activated`` so a missing/partial panel never breaks command
    registration at workbench load.
    """

    def GetResources(self):
        return {
            "Pixmap": _icon("settings.svg"),
            "MenuText": "Settings",
            "ToolTip": "Configure the Nurbly plugin (nrb path and defaults)",
        }

    def IsActive(self):
        return True  # always available

    def Activated(self):
        from nurbly.host.settings_panel import open_settings_dialog

        open_settings_dialog()


# ── 8. Diagnostics ───────────────────────────────────────────────────────────


class DiagnosticsCommand:
    """Open the Nurbly diagnostics panel (nrb version, auth, environment).

    Like :class:`SettingsCommand`, the dialog lives in
    :mod:`nurbly.host.settings_panel`, and the import is lazy for the same reason.
    """

    def GetResources(self):
        return {
            "Pixmap": _icon("diagnostics.svg"),
            "MenuText": "Diagnostics",
            "ToolTip": "Show Nurbly diagnostics (nrb version, auth, environment)",
        }

    def IsActive(self):
        return True  # always available

    def Activated(self):
        from nurbly.host.settings_panel import open_diagnostics_dialog

        open_diagnostics_dialog()


# Command-id -> instance table, consumed by the Workbench's Initialize().
COMMANDS = {
    "Nurbly_SignIn": SignInCommand(),
    "Nurbly_LinkClone": LinkCloneCommand(),
    "Nurbly_OpenProject": OpenProjectCommand(),
    "Nurbly_CheckOut": CheckOutCommand(),
    "Nurbly_CheckIn": CheckInCommand(),
    "Nurbly_WhoHasIt": WhoHasItCommand(),
    "Nurbly_SwitchBranch": SwitchBranchCommand(),
    "Nurbly_Settings": SettingsCommand(),
    "Nurbly_Diagnostics": DiagnosticsCommand(),
}

# Order for the toolbar + menu: the two repo-entry actions (Link & clone for a
# new part, Open project to join an existing project) sit together, then the
# MVP check-out/check-in loop, then Switch branch, then Settings + Diagnostics.
COMMAND_ORDER = [
    "Nurbly_SignIn",
    "Nurbly_LinkClone",
    "Nurbly_OpenProject",
    "Nurbly_CheckOut",
    "Nurbly_CheckIn",
    "Nurbly_WhoHasIt",
    "Nurbly_SwitchBranch",
    "Nurbly_Settings",
    "Nurbly_Diagnostics",
]


def register_commands() -> None:
    """Register every command with FreeCAD. Called from the Workbench."""
    for command_id, instance in COMMANDS.items():
        Gui.addCommand(command_id, instance)

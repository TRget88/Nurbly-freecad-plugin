# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""PySide dialogs for the Nurbly workbench.

FREECAD-ONLY. Imports FreeCAD's bundled Qt (``PySide``) and is loaded only
inside FreeCAD. All dialog *text/remedies* come from the pure ``errors`` module
so the wording is unit-tested; this file just renders it.

FreeCAD bundles PySide2 (0.20/0.21) or PySide6 (1.0+). We import from the
``PySide`` shim FreeCAD provides, which re-exports whichever is active, so this
file works across the supported version range.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PySide import QtCore, QtGui, QtWidgets  # FreeCAD's Qt shim (PySide2/6)

from ..assembly import repo_relative_path
from ..errors import ErrorKind
from ..output_parser import BranchRow, LockRow, RepoRow


def _main_window():
    import FreeCADGui as Gui  # noqa: WPS433

    return Gui.getMainWindow()


def _open_url(url: str) -> None:
    """Open ``url`` in the user's default browser via Qt (no console flash).

    Uses ``QDesktopServices`` rather than :mod:`webbrowser` so it stays inside
    FreeCAD's Qt event loop and never spawns a console window on Windows.
    """
    QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))


def prompt_token(parent=None, *, keys_url: Optional[str] = None) -> Optional[str]:
    """Modal Personal-Access-Token entry with a one-click "get a key" link.

    Self-service by design: a mechanical engineer never needs the command line.
    When ``keys_url`` is given, a **Get an access key** button opens the web
    app's API Keys page in the default browser, where they mint a key, copy it,
    and paste it into the masked field below.

    Returns the entered token (stripped) or ``None`` if cancelled. Format is not
    validated here -- nrb rejects anything that is not ``nrbpat_...``; we only
    avoid sending an empty string. ``keys_url`` is optional so the dialog still
    works (minus the button) if the deep link could not be resolved.
    """
    parent = parent or _main_window()
    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle("Sign in to Nurbly")
    layout = QtWidgets.QVBoxLayout(dlg)

    intro = QtWidgets.QLabel(
        "Paste a Personal Access Token to connect FreeCAD to Nurbly.\n\n"
        "1. Click 'Get an access key' to open your Nurbly API Keys page.\n"
        "2. Create a key and copy it (it starts with nrbpat_).\n"
        "3. Paste it below and click Sign in.",
        dlg,
    )
    intro.setWordWrap(True)
    layout.addWidget(intro)

    if keys_url:
        get_key = QtWidgets.QPushButton("Get an access key", dlg)
        get_key.setToolTip(keys_url)
        get_key.clicked.connect(lambda: _open_url(keys_url))
        layout.addWidget(get_key)

    field = QtWidgets.QLineEdit(dlg)
    field.setEchoMode(QtWidgets.QLineEdit.Password)
    field.setPlaceholderText("nrbpat_...")
    layout.addWidget(field)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, parent=dlg
    )
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Sign in")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    # Enter in the field accepts; the field gets focus on open.
    field.returnPressed.connect(dlg.accept)
    field.setFocus()

    if dlg.exec_() != QtWidgets.QDialog.Accepted:
        return None
    text = field.text().strip()
    return text or None


def prompt_nrb_path(parent=None) -> Optional[str]:
    """First-run: ask for the full path to the nrb binary when autolocate fails.

    ``getOpenFileName`` returns ``(path, selected_filter)`` -- the 2nd value is
    the chosen filter string, NOT a bool. On cancel ``path`` is ``""``, so we
    gate on the path itself.
    """
    parent = parent or _main_window()
    path, _filter = QtWidgets.QFileDialog.getOpenFileName(
        parent,
        "Locate the nrb CLI binary",
        "",
        "nrb executable (nrb nrb.exe);;All files (*)",
    )
    return path or None


def prompt_clone_parent(default_dir: str, parent=None) -> Optional[str]:
    """Ask WHERE to create the repo clone (the working copy lands in
    ``<chosen>/<repo>``).

    The clone-first model writes the working copy here, so it MUST be a
    writable folder. ``default_dir`` is a likely-writable suggestion (see
    :func:`nurbly.core.default_clone_parent`). ``getExistingDirectory`` returns
    an empty string on cancel, so we gate on the path. Returns the chosen
    directory or ``None``.
    """
    parent = parent or _main_window()
    chosen = QtWidgets.QFileDialog.getExistingDirectory(
        parent,
        "Choose a writable folder to clone the repository into",
        default_dir or "",
        QtWidgets.QFileDialog.ShowDirsOnly,
    )
    return chosen or None


def pick_repo(repos: List[RepoRow], parent=None) -> Optional[RepoRow]:
    """Single-select picker over ``nrb repo list`` rows. Returns the chosen
    :class:`RepoRow` or ``None`` if cancelled."""
    parent = parent or _main_window()
    if not repos:
        QtWidgets.QMessageBox.information(
            parent, "No repositories", "You have no repositories to clone yet."
        )
        return None
    labels = [r.owner_repo for r in repos]
    choice, ok = QtWidgets.QInputDialog.getItem(
        parent, "Link & clone", "Pick a repository:", labels, 0, False
    )
    if not ok or not choice:
        return None
    for r in repos:
        if r.owner_repo == choice:
            return r
    return None


def pick_document(candidates: List[str], clone_dir: str, parent=None) -> Optional[str]:
    """Single-select picker over the .FCStd documents discovered in a clone.

    Returns the chosen ABSOLUTE path, or None on cancel or empty input. A lone
    candidate is returned directly (no dialog) so the common single-document
    project opens straight away. The labels are the repo-relative paths (unique
    per file, forward-slash) so a project that nests parts in sub-folders reads
    clearly; the chosen label maps back to its absolute path.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    parent = parent or _main_window()
    by_label = {}
    for abs_path in candidates:
        label = repo_relative_path(abs_path, clone_dir) or os.path.basename(abs_path)
        by_label[label] = abs_path
    labels = list(by_label.keys())
    choice, ok = QtWidgets.QInputDialog.getItem(
        parent, "Open project", "Pick a document to open:", labels, 0, False
    )
    if not ok or not choice:
        return None
    return by_label.get(choice)


_CREATE_NEW_BRANCH = "[+ Create a new branch...]"


def pick_branch(
    branches: List[BranchRow], current: str, parent=None
) -> Optional[Tuple[str, bool]]:
    """Switch-branch picker. Returns ``(branch_name, create)`` or ``None``.

    Lists every local branch (the current one tagged ``(current)``) plus a
    "create a new branch" entry. Picking an existing branch returns
    ``(name, False)``; picking the create entry prompts for a name and returns
    ``(name, True)`` so the caller passes ``create=True`` (cut from the current
    branch). Returns ``None`` on cancel or empty input.

    Selecting the branch that is already current is a no-op the caller short-
    circuits, so we still return it rather than hiding it from the list (the user
    sees where they are).
    """
    parent = parent or _main_window()
    labels: List[str] = []
    label_to_name = {}
    current_index = 0
    for i, b in enumerate(branches):
        label = f"{b.name}  (current)" if b.is_current else b.name
        labels.append(label)
        label_to_name[label] = b.name
        if b.is_current:
            current_index = i
    labels.append(_CREATE_NEW_BRANCH)

    choice, ok = QtWidgets.QInputDialog.getItem(
        parent,
        "Switch branch",
        "Switch the working copy to:",
        labels,
        current_index,
        False,
    )
    if not ok or not choice:
        return None
    if choice == _CREATE_NEW_BRANCH:
        name, ok2 = QtWidgets.QInputDialog.getText(
            parent,
            "New branch",
            f"Name the new branch (cut from '{current}'):",
        )
        if not ok2:
            return None
        name = name.strip()
        return (name, True) if name else None
    return (label_to_name.get(choice, choice), False)


def confirm_push_to_trunk(branch: str, guide_url: str, parent=None) -> bool:
    """Warn (never block) before a Check in that pushes straight to the shared
    trunk (``main`` / ``master``). Returns True to proceed, False to cancel.

    Offers an "Open the guide" button that opens the User Guide's "Why not commit
    straight to main?" section in the browser (the dialog stays open, mirroring
    the Sign in dialog's "Get an access key" button), then a "Check in anyway" /
    Cancel choice. Pure presentation -- the decision to warn is made by
    :func:`nurbly.core.current_trunk_branch`; this only renders it.
    """
    parent = parent or _main_window()
    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle("Checking in to '{}'".format(branch))
    layout = QtWidgets.QVBoxLayout(dlg)

    intro = QtWidgets.QLabel(
        "You are about to check in directly on '{branch}', the branch everyone "
        "shares.\n\n"
        "That publishes your change as the official version straight away, with "
        "no review and nothing to fall back to if it turns out to be wrong. For "
        "team work the safer habit is to make a branch, check in there, and open "
        "a pull request.\n\n"
        "You can still check in here if you mean to.".format(branch=branch),
        dlg,
    )
    intro.setWordWrap(True)
    layout.addWidget(intro)

    if guide_url:
        guide = QtWidgets.QPushButton("Open the guide", dlg)
        guide.setToolTip(guide_url)
        guide.clicked.connect(lambda: _open_url(guide_url))
        layout.addWidget(guide)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, parent=dlg
    )
    ok_btn = buttons.button(QtWidgets.QDialogButtonBox.Ok)
    if ok_btn is not None:
        ok_btn.setText("Check in anyway")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    return dlg.exec_() == QtWidgets.QDialog.Accepted


def prompt_commit_message(parent=None) -> Optional[str]:
    """Multi-line commit message entry for Check in. Empty -> cancel."""
    parent = parent or _main_window()
    text, ok = QtWidgets.QInputDialog.getMultiLineText(
        parent, "Check in", "Describe this change:", ""
    )
    if not ok:
        return None
    text = text.strip()
    return text or None


def confirm_relocation(out_of_tree: List[str], parent=None) -> bool:
    """Consent prompt before relocating out-of-tree linked parts into the clone.

    Lists the parts that live OUTSIDE the repo clone and explains that, if the
    user proceeds, each will be COPIED into the clone's ``parts/`` folder and the
    assembly's links re-pointed at the in-clone copies (then committed alongside
    the assembly). Returns True if the user clicks Proceed, False on Cancel (or if
    there is nothing to relocate, in which case there is no decision to make).

    Pure presentation: the relocation itself is performed by
    :func:`nurbly.host.relink.relocate_dependencies`. This dialog only asks.
    """
    if not out_of_tree:
        return False
    parent = parent or _main_window()
    listed = "\n".join(f"  - {p}" for p in out_of_tree)
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Question)
    box.setWindowTitle("Nurbly")
    box.setText("Some linked parts live outside the repository clone.")
    box.setInformativeText(
        "These parts will be COPIED into the clone's parts/ folder and the "
        "assembly's links re-pointed at the in-clone copies, so they are "
        "committed alongside the assembly.\n\nProceed?"
    )
    box.setDetailedText(listed)
    box.setStandardButtons(QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel)
    box.button(QtWidgets.QMessageBox.Ok).setText("Proceed")
    box.setDefaultButton(QtWidgets.QMessageBox.Ok)
    return box.exec_() == QtWidgets.QMessageBox.Ok


def show_locks(rows: List[LockRow], scope: str, parent=None) -> None:
    """Show 'Who has it?' results as a small read-only table dialog."""
    parent = parent or _main_window()
    if not rows:
        QtWidgets.QMessageBox.information(
            parent, "Locks", f"No active locks for {scope}."
        )
        return

    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(f"Locks -- {scope}")
    layout = QtWidgets.QVBoxLayout(dlg)
    table = QtWidgets.QTableWidget(len(rows), 4, dlg)
    table.setHorizontalHeaderLabels(["Path", "Locked by", "Expires", "Message"])
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    for i, row in enumerate(rows):
        table.setItem(i, 0, QtWidgets.QTableWidgetItem(row.path))
        table.setItem(i, 1, QtWidgets.QTableWidgetItem(f"@{row.locked_by}"))
        table.setItem(i, 2, QtWidgets.QTableWidgetItem(row.expires))
        table.setItem(i, 3, QtWidgets.QTableWidgetItem(row.message))
    table.resizeColumnsToContents()
    layout.addWidget(table)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, parent=dlg)
    buttons.rejected.connect(dlg.reject)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)
    dlg.resize(720, 320)
    dlg.exec_()


def show_success(message: str, detail: str = "", parent=None) -> None:
    parent = parent or _main_window()
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Information)
    box.setWindowTitle("Nurbly")
    box.setText(message)
    if detail:
        box.setDetailedText(detail)
    box.exec_()


def show_text(title: str, body: str, parent=None) -> None:
    """Show ``body`` verbatim in a read-only, scrollable, monospaced text box.

    Used by the Settings/Diagnostics views for multi-line output (``nrb config
    list`` listings, captured stdout/stderr) where :class:`QMessageBox`'s small
    label and collapsed "Details" panel are too cramped. Plain text only -- the
    body is shown exactly as captured, never interpreted as HTML.
    """
    parent = parent or _main_window()
    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(title)
    layout = QtWidgets.QVBoxLayout(dlg)
    edit = QtWidgets.QPlainTextEdit(dlg)
    edit.setReadOnly(True)
    edit.setPlainText(body)
    # Monospace so aligned listings (nrb config list) and argv read cleanly.
    edit.setLineWrapMode(getattr(QtWidgets.QPlainTextEdit, "NoWrap", 0))
    layout.addWidget(edit)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, parent=dlg)
    buttons.rejected.connect(dlg.reject)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)
    dlg.resize(640, 380)
    dlg.exec_()


def show_error(title: str, kind: ErrorKind, detail: str, remedy: str = "", parent=None) -> None:
    """Render a failure. ``detail`` is the VERBATIM nrb output (per
    PLUGIN_MVP.md: "show the CLI's message verbatim"); ``remedy`` is the
    plain-language next step from :data:`errors.REMEDIES`."""
    parent = parent or _main_window()
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle("Nurbly")
    box.setText(title or "nrb reported an error")
    if remedy:
        box.setInformativeText(remedy)
    if detail:
        box.setDetailedText(detail)  # verbatim CLI output, expandable
    box.exec_()

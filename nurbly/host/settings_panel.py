# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Settings + Diagnostics dialogs for the Nurbly workbench.

FREECAD-ONLY. Imports FreeCAD's bundled Qt (``PySide``) and the ``nrb`` runner;
it is loaded only inside FreeCAD. Two public entry points, wired to toolbar
commands by ``gui_commands`` (do NOT rename them):

  * :func:`open_settings_dialog` -- view/edit the located nrb binary path (saved
    via the PURE :mod:`config_store`) and, on demand, show the *active* CLI
    config (``nrb config list`` -> auth_url / api_url / git_url) by shelling out
    through :mod:`nrb_runner`. The plugin never edits ``~/.nurbly/config.json``
    itself -- inspecting it is enough for troubleshooting, and ``nrb config
    set`` is the supported way to change it.
  * :func:`open_diagnostics_dialog` -- show the LAST ``nrb`` command the plugin
    ran plus its captured stdout/stderr, recorded in :mod:`config_store`, so the
    user can read the raw failure when a dialog's summary is not enough.

UNVALIDATED: FreeCAD is not installed in CI, so this module is only
byte-compiled, never executed, here. The runtime behaviour is exercised by the
manual smoke test in PLUGIN_MVP.md. Every PySide/FreeCAD access is therefore
lazy (imported inside functions) or ``getattr``-guarded so the module still
``py_compile``s without those packages present.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .. import nrb_runner
from ..config_store import PluginConfig


def _qt():
    """Return FreeCAD's ``QtWidgets`` shim, or ``None`` if unavailable.

    Guarded so the module imports/byte-compiles outside FreeCAD; callers treat
    ``None`` as "no GUI" and bail quietly.
    """
    try:
        from PySide import QtWidgets  # noqa: WPS433 -- FreeCAD's Qt shim

        return QtWidgets
    except Exception:  # pragma: no cover -- only hit outside FreeCAD
        return None


def _main_window():
    """The FreeCAD main window to parent dialogs on, or ``None`` if headless."""
    try:
        import FreeCADGui as Gui  # noqa: WPS433

        return Gui.getMainWindow()
    except Exception:  # pragma: no cover -- only hit outside FreeCAD
        return None


def _located_nrb(saved: Optional[str]) -> Tuple[str, bool]:
    """Resolve the nrb path the runner WOULD use, given the saved override.

    Returns ``(text, found)``: the absolute located path and ``True`` when nrb
    was found, or an explanatory message and ``False`` when it was not. Never
    raises -- :class:`nrb_runner.NrbNotFound` is turned into ``found=False`` so
    the Settings dialog can still open and let the user point at the binary.
    """
    try:
        return nrb_runner.locate_nrb(saved), True
    except nrb_runner.NrbNotFound as exc:
        return str(exc), False


def _run_config_list(saved: Optional[str]):
    """Shell out to ``nrb config list`` and record the run for Diagnostics.

    Returns the :class:`nrb_runner.NrbResult`. Persisting the invocation here
    (via :meth:`config_store.PluginConfig.set_last_run`) means the Diagnostics
    view can show what Settings just did, mirroring how the action commands
    would log their own runs.
    """
    result = nrb_runner.run(["config", "list"], config_override=saved)
    try:
        cfg = PluginConfig().load()
        cfg.set_last_run(result.argv, result.exit_code, result.stdout, result.stderr)
        cfg.save()
    except Exception:  # pragma: no cover -- never let logging break the view
        pass
    return result


def open_settings_dialog(parent=None) -> None:
    """View/edit the located nrb path and show the active ``nrb config``.

    A small form dialog: an editable nrb-path field (prefilled with the saved
    override, or the auto-located path if none is saved) with a Browse button,
    a Save button that persists the path via :mod:`config_store`, and a "Show
    active config" button that runs ``nrb config list`` and displays the
    auth_url / api_url / git_url it reports, verbatim.
    """
    QtWidgets = _qt()
    if QtWidgets is None:  # pragma: no cover -- outside FreeCAD
        return
    parent = parent or _main_window()

    cfg = PluginConfig().load()
    saved = cfg.get_nrb_path()
    located, found = _located_nrb(saved)

    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle("Nurbly settings")
    outer = QtWidgets.QVBoxLayout(dlg)

    form = QtWidgets.QFormLayout()
    path_edit = QtWidgets.QLineEdit(dlg)
    # Prefill with the saved override if set, else the auto-located binary so
    # the field shows what the plugin is actually using today.
    path_edit.setText(saved or (located if found else ""))
    path_edit.setPlaceholderText("Full path to the nrb CLI binary")

    browse = QtWidgets.QPushButton("Browse...", dlg)

    def _on_browse():
        from . import gui_dialogs  # noqa: WPS433 -- reuse the file picker

        chosen = gui_dialogs.prompt_nrb_path(parent)
        if chosen:
            path_edit.setText(chosen)

    browse.clicked.connect(_on_browse)

    path_row = QtWidgets.QHBoxLayout()
    path_row.addWidget(path_edit)
    path_row.addWidget(browse)
    form.addRow("nrb binary:", _wrap(QtWidgets, path_row))

    status = QtWidgets.QLabel(
        f"Located: {located}" if found else f"Not found: {located}", dlg
    )
    status.setWordWrap(True)
    form.addRow("Status:", status)
    outer.addLayout(form)

    show_config = QtWidgets.QPushButton("Show active config", dlg)

    def _on_show_config():
        from . import gui_dialogs  # noqa: WPS433

        saved_now = path_edit.text().strip() or None
        result = _run_config_list(saved_now)
        if result.ok:
            gui_dialogs.show_text("Active nrb config", result.stdout or result.combined, parent)
        else:
            gui_dialogs.show_text(
                "Could not read nrb config",
                result.combined or "nrb config list produced no output.",
                parent,
            )

    show_config.clicked.connect(_on_show_config)
    outer.addWidget(show_config)

    buttons = QtWidgets.QDialogButtonBox(
        getattr(QtWidgets.QDialogButtonBox, "Save", 0)
        | getattr(QtWidgets.QDialogButtonBox, "Cancel", 0),
        parent=dlg,
    )
    outer.addWidget(buttons)

    def _on_save():
        text = path_edit.text().strip()
        # Persist whatever the user typed (empty clears the override so the
        # runner falls back to PATH/$NRB_BINARY/~/.cargo/bin auto-location).
        cfg.set_nrb_path(text)
        cfg.save()
        dlg.accept()

    buttons.accepted.connect(_on_save)
    buttons.rejected.connect(dlg.reject)

    dlg.resize(560, 200)
    dlg.exec_()


def open_diagnostics_dialog(parent=None) -> None:
    """Show the last ``nrb`` command the plugin ran plus its raw output.

    Reads the most-recent run recorded in :mod:`config_store`. If nothing has
    been recorded yet (fresh install, or a config without a ``last_run`` entry),
    say so plainly rather than showing an empty box.
    """
    QtWidgets = _qt()
    if QtWidgets is None:  # pragma: no cover -- outside FreeCAD
        return
    parent = parent or _main_window()

    from . import gui_dialogs  # noqa: WPS433

    cfg = PluginConfig().load()
    run = cfg.get_last_run()
    if run is None:
        gui_dialogs.show_text(
            "Nurbly diagnostics",
            "No nrb command has been recorded yet.\n"
            "Run a Nurbly action (or Settings -> Show active config) first.",
            parent,
        )
        return

    gui_dialogs.show_text("Nurbly diagnostics", _format_run(run), parent)


def _wrap(QtWidgets, layout):
    """Box a sub-layout into a widget so it can be dropped into a QFormLayout row."""
    holder = QtWidgets.QWidget()
    holder.setLayout(layout)
    return holder


def _format_run(run: dict) -> str:
    """Render a recorded run dict as the verbatim diagnostics text body.

    ``run`` has the shape returned by
    :meth:`config_store.PluginConfig.get_last_run`.
    """
    argv = run.get("argv") or []
    exit_code = run.get("exit_code")
    stdout = run.get("stdout") or ""
    stderr = run.get("stderr") or ""
    lines = [
        "Last nrb command:",
        "  " + " ".join(argv),
        "",
        f"Exit code: {exit_code}",
        "",
        "stdout:",
        stdout if stdout else "  (empty)",
        "",
        "stderr:",
        stderr if stderr else "  (empty)",
    ]
    return "\n".join(lines)

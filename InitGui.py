# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""FreeCAD GUI entry point for the Nurbly workbench.

FreeCAD imports this top-level file on startup (GUI mode only) and expects it to
register a workbench. It is FREECAD-ONLY -- it imports ``FreeCADGui`` and the
``nurbly.host`` package. It is never imported by the unit tests. CI only
``py_compile``s it.

TWO load-killing gotchas this file is written around (both cost real debugging):

1. The host sub-package MUST NOT be named ``freecad``. FreeCAD 1.0+ reserves the
   ``freecad`` namespace for new-style add-ons, so a directory by that name
   hijacks the namespace. It is named ``nurbly.host`` for that reason.

2. FreeCAD execs this file with SEPARATE globals/locals, so any name ASSIGNED at
   module top level (e.g. ``_ADDON_DIR = ...``) is NOT visible inside the
   ``Workbench`` class body -- the class body only resolves true globals
   (``os``, ``sys``, ``FreeCAD``, ``FreeCADGui``, which FreeCAD injects) and
   builtins. Referencing a top-level variable in the class body raises
   ``NameError`` mid-exec, ``addWorkbench`` is never reached, and the workbench
   silently never appears in the selector. So the class body computes its paths
   from ``FreeCAD.getUserAppDataDir()`` (a global), never from a module variable.

Install: copy the whole ``freecad-nurbly`` directory into FreeCAD's per-version
``Mod`` folder. On FreeCAD 1.1 that is ``%APPDATA%/FreeCAD/v1-1/Mod/`` on
Windows; run ``FreeCAD.getUserAppDataDir()`` in the Python console to find yours.
Restart FreeCAD; the workbench appears in the selector as "Nurbly".
"""

import os
import sys

import FreeCAD
import FreeCADGui as Gui

# The add-on lives at ``<UserAppData>/Mod/freecad-nurbly/``. Put its root on
# ``sys.path`` so ``import nurbly...`` resolves. (Top-level code CAN read this
# top-level name; the class body below cannot -- see gotcha #2 in the docstring.)
_ADDON_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "freecad-nurbly")
if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)


class NurblyWorkbench(Gui.Workbench):
    """Thin PDM workbench: five buttons that shell out to ``nrb``."""

    MenuText = "Nurbly"
    ToolTip = "Check CAD files in and out of a Nurbly repository"
    # Computed from getUserAppDataDir() (a GLOBAL) -- NOT from ``_ADDON_DIR``,
    # which is invisible here (gotcha #2). FreeCAD tolerates a missing icon.
    Icon = os.path.join(
        FreeCAD.getUserAppDataDir(),
        "Mod",
        "freecad-nurbly",
        "resources",
        "icons",
        "nurbly.svg",
    )

    def Initialize(self):
        """Register commands + build the toolbar/menu (first activation)."""
        from nurbly.host import gui_commands

        gui_commands.register_commands()
        self.appendToolbar("Nurbly", gui_commands.COMMAND_ORDER)
        self.appendMenu("Nurbly", gui_commands.COMMAND_ORDER)

    def GetClassName(self):
        # Required for Python workbenches on FreeCAD 0.20+.
        return "Gui::PythonWorkbench"

    def Activated(self):
        FreeCAD.Console.PrintMessage("Nurbly workbench activated.\n")

    def Deactivated(self):
        pass


Gui.addWorkbench(NurblyWorkbench())

# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""FreeCAD-only sub-package: the host API lives here and ONLY here.

Importing most modules in this sub-package requires a running FreeCAD (they
pull in ``FreeCAD`` / ``FreeCADGui`` / ``Part``). The pure-logic unit tests
never import this package -- that is the whole point of the split mandated by
PLUGIN_MVP.md.

The one exception is ``relink``, which is fully DUCK-TYPED (it reads and writes
doc + object attributes but imports no FreeCAD), so it is unit-tested by
loading its file directly (see tests/test_relink.py) without importing this
package.

Modules:
    freecad_export -- export the active document to neutral STEP (per-CAD)
    dependencies   -- discover an assembly's linked-child closure
    gui_dialogs    -- PySide dialogs (token entry, repo picker, errors, locks)
    gui_commands   -- FreeCADGui command classes (the five toolbar buttons)
    relink         -- copy + re-link out-of-tree parts into the clone (v2)
"""

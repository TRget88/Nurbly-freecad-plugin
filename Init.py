# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

# FreeCAD console-mode entry point.
#
# FreeCAD imports Init.py in BOTH console and GUI modes; InitGui.py is GUI-only.
# The Nurbly workbench is a GUI feature (toolbar buttons + dialogs), so there is
# nothing to set up for headless/console runs. This file exists so FreeCAD does
# not warn about a missing Init.py and as the documented place to add any
# future console-mode hooks. Intentionally a no-op.

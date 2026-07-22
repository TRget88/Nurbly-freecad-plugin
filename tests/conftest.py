# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Make the plugin package importable when running tests from anywhere.

The tests import ``nurbly.*`` (the pure-logic package). Adding the plugin root
to ``sys.path`` lets ``python -m pytest`` (or plain unittest) find it without
an install step. These tests NEVER import ``nurbly.host`` -- that requires a
running FreeCAD.
"""

import os
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

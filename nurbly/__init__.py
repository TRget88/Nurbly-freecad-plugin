# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Nurbly FreeCAD plugin -- shared, host-agnostic package.

Everything in this package (except the :mod:`nurbly.host` sub-package)
imports NO FreeCAD and is unit-testable with plain Python:

    cmd_builder    -- build nrb argv lists
    output_parser  -- scrape nrb human output (lock id, repo list, locks table)
    errors         -- classify nrb failures into UI categories + remedies
    mapping_store  -- persist the doc<->repo/path link (+ cached lock id)
    nrb_runner     -- locate + subprocess the nrb binary
    core           -- compose the above into the five PDM actions

The FreeCAD-only code (STEP export + GUI command classes + dialogs) lives in
``nurbly.host`` and is the only place the host API is imported.
"""

__all__ = [
    "cmd_builder",
    "output_parser",
    "errors",
    "mapping_store",
    "nrb_runner",
    "core",
]

__version__ = "0.1.0"

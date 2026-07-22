# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Export the active FreeCAD document to neutral STEP.

FREECAD-ONLY. This is the single inherently per-CAD module in the whole plugin
(PLUGIN_MVP.md: "The only inherently per-CAD code is exporting the open native
model to neutral STEP via the host app's own API"). It is rewritten per host;
everything else is host-agnostic shell-out.

Cannot be imported, exercised, or syntax-meaningfully type-checked outside a
running FreeCAD -- ``import FreeCAD`` / ``import Part`` only resolve inside the
app. The pure tests never import this file; CI only ``py_compile``s it.
"""

from __future__ import annotations

import os
from typing import List, Optional


def step_path_for(fcstd_path: str) -> str:
    """Return the ``.step`` path that sits next to a ``.FCStd`` file.

    e.g. ``/clone/parts/widget.FCStd`` -> ``/clone/parts/widget.step``. Lower-
    case ``.step`` is what the Nurbly server differencer expects.
    """
    base, _ext = os.path.splitext(fcstd_path)
    return base + ".step"


def _visible_objects(doc):
    """All objects in ``doc`` that are currently visible.

    Filtering by ``ViewObject.Visibility`` keeps construction geometry and
    hidden helpers out of the STEP. In headless contexts ``ViewObject`` may be
    ``None``; we fall back to including the object so a console run still works.
    """
    objs = []
    for obj in doc.Objects:
        view = getattr(obj, "ViewObject", None)
        if view is None or getattr(view, "Visibility", True):
            objs.append(obj)
    return objs


def export_active_document(output_path: Optional[str] = None) -> str:
    """Export the active document to STEP and return the path written.

    If ``output_path`` is omitted it is derived from the active document's
    ``FileName`` via :func:`step_path_for`. Raises ``RuntimeError`` when no
    document is open, when the document was never saved (no FileName and no
    explicit path), or when there is no visible geometry to export.

    Uses ``Import.export([objects], path)`` -- the structured (non-GUI)
    exporter -- so the STEP keeps each object's Label as the PRODUCT name and,
    in the GUI, the per-object colours. That is what lets Nurbly's XCAF
    analyser show a real, NAMED, coloured assembly tree (the AP242/XDE Phase A
    work) instead of generic "Open CASCADE STEP translator" leaves.
    ``Import.export`` resolves ``App::Link`` geometry too, so the assembly case
    keeps working.

    Validated headless (FreeCADCmd + the sidecar XCAF reader, 2026-06-21): a
    two-body doc AND an ``App::Link`` instance both come back as 2 named solids
    (``BasePlate`` / ``PinInstance``) vs the unnamed leaves the old
    ``Compound.exportStep`` produced. Colours need a ViewObject, so they only
    export under the GUI -- the in-FreeCAD smoke test covers that.

    Falls back to the geometry-only ``Part.Compound(shapes).exportStep`` (the
    previous behaviour) if ``Import.export`` ever produces no file, so the
    export can only improve or no-op.
    """
    import FreeCAD as App  # noqa: WPS433 -- host import, intentionally local
    import Part  # noqa: WPS433

    doc = App.ActiveDocument
    if doc is None:
        raise RuntimeError("No FreeCAD document is open.")

    if output_path is None:
        if not doc.FileName:
            raise RuntimeError(
                "Save the document once (so it has a file on disk) before "
                "checking it in."
            )
        output_path = step_path_for(doc.FileName)

    objects: List = _visible_objects(doc)
    if not objects:
        raise RuntimeError("The document has no visible objects to export.")

    # Keep, in parallel: the OBJECTS that carry geometry (handed to
    # ``Import.export`` so it writes each Label as the STEP product name) and
    # their RESOLVED ``.Shape``s (the geometry guard + the compound fallback).
    # Reading ``.Shape`` is what makes the ASSEMBLY case work: a native
    # ``App::Link`` carries its child's placed geometry on ``.Shape``.
    geom_objects: List = []
    shapes = []
    for obj in objects:
        shape = getattr(obj, "Shape", None)
        if shape is not None and hasattr(shape, "isNull") and not shape.isNull():
            geom_objects.append(obj)
            shapes.append(shape)
    if not shapes:
        raise RuntimeError("The document has no visible geometry to export.")

    parent = os.path.dirname(output_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # Prefer the structured exporter: it preserves each object's Label as the
    # STEP product name (a real assembly tree in Nurbly) + GUI colours, and
    # resolves App::Link geometry. Fall back to the geometry-only compound if
    # it ever writes nothing, so the export can only improve or no-op.
    wrote = False
    try:
        import Import  # noqa: WPS433 -- host import, intentionally local

        Import.export(geom_objects, output_path)
        wrote = os.path.isfile(output_path) and os.path.getsize(output_path) > 0
    except Exception:  # noqa: BLE001 -- any failure falls back to the compound
        wrote = False
    if not wrote:
        Part.Compound(shapes).exportStep(output_path)

    if not os.path.isfile(output_path):
        raise RuntimeError(f"STEP export produced no file at {output_path}.")
    return output_path

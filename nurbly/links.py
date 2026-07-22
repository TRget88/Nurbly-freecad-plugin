# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Build deep links into the Nurbly web app.

PURE LOGIC -- imports no FreeCAD. The plugin opens these URLs in the user's
browser (e.g. the "Get an access key" button on the Sign in dialog) so a
mechanical engineer can mint a Personal Access Token (nrbpat_...) without ever
touching the command line.

Nurbly is a managed, single-deployment product, so the web app has ONE
canonical origin (``https://nurbly.com``). A developer pointing the plugin
at a local stack overrides it, in precedence order:

  1. an explicit ``override`` argument (the plugin config's ``web_url`` key,
     see :meth:`nurbly.config_store.PluginConfig.get_web_url`), then
  2. the ``NURBLY_WEB_URL`` environment variable, then
  3. the managed default below.

Real users set neither and land on the managed app.
"""

from __future__ import annotations

import os
from typing import Optional

# The managed web app's canonical origin. This is the SPA origin the backend
# CORS allow-list pins (nurbly-api router.rs), NOT the API/git host.
DEFAULT_WEB_BASE = "https://nurbly.com"

# The web app's API-keys management tab (SettingsPage ``?tab=keys``). This is
# where a user mints the nrbpat_... Personal Access Token the plugin pastes.
API_KEYS_PATH = "/settings?tab=keys"

# The User Guide section explaining why checking in directly on ``main`` is
# usually a bad habit. The ``/help`` page renders ``docs/USER_GUIDE.md`` and
# the ``#`` fragment is the rehype-slug of the "Why not commit straight to
# main?" heading. The CAD plugins' push-to-trunk warning links here so a user
# new to git-style repos can read the why in one click.
BRANCHING_GUIDE_PATH = "/help#why-not-commit-straight-to-main"


def _clean(value: Optional[str]) -> str:
    """Trim surrounding whitespace and any trailing slash from a base candidate.

    A non-string (e.g. a hand-edited config value of the wrong type) yields an
    empty string so it is skipped rather than crashing the deep link.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def web_base(override: Optional[str] = None) -> str:
    """The canonical web-app origin, honouring an override.

    Precedence: ``override`` (e.g. the plugin config's ``web_url``), then the
    ``NURBLY_WEB_URL`` environment variable, then :data:`DEFAULT_WEB_BASE`. Each
    candidate is whitespace-trimmed with a trailing slash removed so callers can
    append an absolute path; blank/whitespace candidates fall through to the
    next.
    """
    for candidate in (override, os.environ.get("NURBLY_WEB_URL"), DEFAULT_WEB_BASE):
        cleaned = _clean(candidate)
        if cleaned:
            return cleaned
    return DEFAULT_WEB_BASE  # DEFAULT is always truthy; here only for total-ness


def api_keys_url(override: Optional[str] = None) -> str:
    """Absolute URL of the web app's API Keys tab (mint a Personal Access Token).

    The Sign in dialog's "Get an access key" button opens this so the user can
    create a key, copy it, and paste it back -- no CLI required.
    """
    return f"{web_base(override)}{API_KEYS_PATH}"


def branching_guide_url(override: Optional[str] = None) -> str:
    """Absolute URL of the User Guide's "Why not commit straight to main?" section.

    The push-to-trunk warning shown on Check in (when the clone is on ``main`` /
    ``master``) links here so a user new to git-style repos can read why working
    on a branch + pull request is the better habit.
    """
    return f"{web_base(override)}{BRANCHING_GUIDE_PATH}"

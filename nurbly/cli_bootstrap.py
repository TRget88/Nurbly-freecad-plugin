# SPDX-FileCopyrightText: 2026 Nurbly
# SPDX-License-Identifier: Apache-2.0

"""Bootstrap the ``nrb`` CLI: detect the platform, pick the matching release
from the platform's manifest, download it (authenticated), verify its SHA-256,
and install it under ``~/.nurbly/bin`` -- so an Addon-Manager install of the
plugin (which ships only Python, never the Rust binary) becomes self-sufficient
after the user signs in.

PURE LOGIC w.r.t. FreeCAD *and* w.r.t. the network: every function here is
either pure or takes its IO (HTTP fetch, filesystem write, chmod) as an injected
callable, so the whole flow is unit-tested with no network and no FreeCAD. The
module also ships stdlib-only default IO (``urllib``) so nothing here adds a
third-party dependency -- keeping the addon dependency-free for the FreeCAD
Addon Manager.

Design constraints discovered from the server contract (do not "simplify" away):

  * **Authenticated download.** ``GET /v1/cli/releases/:os/:arch`` is
    auth-required (flipped from anonymous 2026-05-21). Only
    ``GET /v1/cli/manifest.json`` is anonymous, and for an anonymous caller it
    carries ``version`` + ``sha256`` but omits ``download_url``. So the bootstrap
    runs AFTER sign-in and reuses the user's PAT as the bearer token. It mirrors
    ``nrb update`` (backend/crates/nurbly-cli/src/commands/update.rs).
  * **SHA-256, not minisign.** Integrity is a SHA-256 check (from the manifest,
    fetched over HTTPS) over an HTTPS-downloaded binary. minisign verification is
    deliberately NOT done: stdlib Python has no ed25519 and the addon ships zero
    third-party deps. The digest + TLS is the integrity boundary here; the
    signed path stays the CLI's own ``nrb update``.

Host wiring (the remaining thin, FreeCAD-side piece): after a successful
``nrb login`` the host holds the PAT; if :func:`nrb_runner.locate_nrb` raises
``NrbNotFound``, show a consent dialog ("Download nrb now?") and, on yes, call
:func:`bootstrap_nrb` with that PAT + the resolved home dir, then persist the
returned path via ``config_store`` so ``locate_nrb`` finds it first next time.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

#: Default REST base, matching the CLI's own default (``nrb config`` ``api_url``).
DEFAULT_API_BASE = "https://nurbly.com"


class BootstrapError(Exception):
    """A bootstrap step failed with an actionable, user-facing message."""


def detect_target(
    system: Optional[str] = None, machine: Optional[str] = None
) -> Tuple[str, str]:
    """Map the running platform to the server's ``(os, arch)`` release labels.

    The labels MUST match the server matrix (handlers/cli.rs ``ALLOWED_OS`` /
    ``ALLOWED_ARCH`` and update.rs ``current_target``): os in
    ``{linux, macos, windows}``, arch in ``{x86_64, aarch64}``. Anything else
    returns ``"unknown"`` so the caller falls back to a manual install rather
    than fetching a wrong build. ``system`` / ``machine`` are injectable for
    tests (default to :mod:`platform`).
    """
    import platform as _platform

    sys_ = (system if system is not None else _platform.system()).lower()
    mach = (machine if machine is not None else _platform.machine()).lower()

    if sys_.startswith("linux"):
        os_ = "linux"
    elif sys_ == "darwin" or "mac" in sys_ or sys_ == "osx":
        os_ = "macos"
    elif sys_.startswith("win"):
        os_ = "windows"
    else:
        os_ = "unknown"

    if mach in ("x86_64", "amd64", "x64", "em64t"):
        arch = "x86_64"
    elif mach in ("aarch64", "arm64", "armv8", "armv8l"):
        arch = "aarch64"
    else:
        arch = "unknown"

    return os_, arch


def select_release(manifest: Dict, os_: str, arch: str) -> Optional[Dict]:
    """Return the manifest ``releases[]`` entry for ``(os_, arch)``, or ``None``."""
    releases: List[Dict] = manifest.get("releases", []) if manifest else []
    for entry in releases:
        if entry.get("os") == os_ and entry.get("arch") == arch:
            return entry
    return None


def manifest_url(api_base: str = DEFAULT_API_BASE) -> str:
    """The anonymous-readable manifest URL for ``api_base``."""
    return api_base.rstrip("/") + "/v1/cli/manifest.json"


def download_url(
    api_base: str, os_: str, arch: str, entry: Optional[Dict] = None
) -> str:
    """Resolve the binary download URL, mirroring ``nrb update``.

    Prefer the manifest entry's ``download_url`` when present (absolute stays
    as-is; a relative path is joined onto ``api_base``); otherwise construct
    ``/v1/cli/releases/{os}/{arch}`` directly off ``(os_, arch)`` -- the
    anonymous manifest omits ``download_url``, so this fallback is the norm.
    """
    base = api_base.rstrip("/")
    if entry:
        given = entry.get("download_url")
        if given:
            if given.startswith("http://") or given.startswith("https://"):
                return given
            return base + given
    return "{}/v1/cli/releases/{}/{}".format(base, os_, arch)


def verify_sha256(data: bytes, expected_hex: str) -> bool:
    """True iff ``data`` hashes to ``expected_hex`` (case-insensitive hex)."""
    if not expected_hex:
        return False
    return hashlib.sha256(data).hexdigest().lower() == expected_hex.strip().lower()


def _exe_name() -> str:
    return "nrb.exe" if os.name == "nt" else "nrb"


def nrb_install_path(home: str) -> str:
    """Where a bootstrapped ``nrb`` lands: ``<home>/.nurbly/bin/nrb[.exe]``.

    Under the plugin/CLI state dir (``~/.nurbly``) so it is discoverable and
    self-owned; :func:`nrb_runner.locate_nrb` finds it via the saved
    ``nrb_path`` override the host persists after a successful bootstrap.
    """
    return os.path.join(home, ".nurbly", "bin", _exe_name())


@dataclass
class BootstrapPlan:
    """A resolved, ready-to-execute download plan (pure data)."""

    version: str
    os: str
    arch: str
    download_url: str
    sha256: str
    install_path: str


def plan_from_manifest(
    manifest: Dict,
    home: str,
    api_base: str = DEFAULT_API_BASE,
    target: Optional[Tuple[str, str]] = None,
) -> BootstrapPlan:
    """Turn a manifest + home dir into a :class:`BootstrapPlan`, or raise.

    Raises :class:`BootstrapError` (actionable, points at manual install) when
    the platform is unsupported, no build exists for it, or the entry lacks a
    ``sha256`` (never download an unverifiable binary).
    """
    os_, arch = target if target is not None else detect_target()
    if os_ == "unknown" or arch == "unknown":
        raise BootstrapError(
            "Automatic nrb download isn't available for this platform "
            "({}/{}). Install nrb manually from {}.".format(os_, arch, DEFAULT_API_BASE)
        )
    entry = select_release(manifest, os_, arch)
    if entry is None:
        raise BootstrapError(
            "The server has no nrb build for {}/{} yet. Install nrb manually "
            "from {}.".format(os_, arch, DEFAULT_API_BASE)
        )
    sha = entry.get("sha256")
    if not sha:
        raise BootstrapError(
            "The manifest entry for {}/{} has no SHA-256; refusing to download "
            "an unverifiable binary.".format(os_, arch)
        )
    return BootstrapPlan(
        version=str(entry.get("version", "?")),
        os=os_,
        arch=arch,
        download_url=download_url(api_base, os_, arch, entry),
        sha256=sha,
        install_path=nrb_install_path(home),
    )


# ── Injectable IO (stdlib-only defaults; tests pass fakes) ────────────────────


def http_get_json(url: str, timeout: int = 30) -> Dict:
    """GET ``url`` and parse JSON. Anonymous (the manifest needs no auth)."""
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        url, headers={"User-Agent": "nurbly-freecad-plugin"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        return _json.loads(resp.read().decode("utf-8"))


def http_get_bytes(url: str, token: str, timeout: int = 300) -> bytes:
    """GET ``url`` with a bearer ``token`` and return the raw body.

    The download endpoints are auth-required; ``token`` is the user's PAT
    (already minted + pasted during sign-in). No request-level cap on size
    beyond ``timeout`` -- a release binary is a few MB.
    """
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "User-Agent": "nurbly-freecad-plugin",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        return resp.read()


def _default_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)  # atomic on the same filesystem; no half-written binary


def _default_make_executable(path: str) -> None:
    if os.name == "nt":
        return
    import stat

    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def acquire_nrb(
    plan: BootstrapPlan,
    token: str,
    get_bytes: Callable[[str, str], bytes] = http_get_bytes,
    write_file: Callable[[str, bytes], None] = _default_write,
    make_executable: Callable[[str], None] = _default_make_executable,
) -> str:
    """Download + verify + install per ``plan``; return the installed path.

    SHA-256 is checked BEFORE anything is written, so a tampered or truncated
    download never lands on disk. Raises :class:`BootstrapError` on mismatch or
    on any IO failure (wrapped with an actionable message).
    """
    try:
        data = get_bytes(plan.download_url, token)
    except Exception as exc:  # noqa: BLE001 -- surface a clean message to the UI
        raise BootstrapError(
            "Could not download nrb from {} ({}). Check your connection and that "
            "you're signed in, or install nrb manually.".format(plan.download_url, exc)
        ) from exc

    if not verify_sha256(data, plan.sha256):
        raise BootstrapError(
            "The downloaded nrb failed its SHA-256 check (expected {}...). "
            "Refusing to install a possibly-tampered binary; try again or "
            "install nrb manually.".format(plan.sha256[:12])
        )

    try:
        write_file(plan.install_path, data)
        make_executable(plan.install_path)
    except OSError as exc:
        raise BootstrapError(
            "Downloaded nrb but couldn't install it to {} ({}).".format(
                plan.install_path, exc
            )
        ) from exc
    return plan.install_path


def bootstrap_nrb(
    token: str,
    home: str,
    api_base: str = DEFAULT_API_BASE,
    get_json: Callable[[str], Dict] = http_get_json,
    get_bytes: Callable[[str, str], bytes] = http_get_bytes,
) -> str:
    """End-to-end: fetch manifest → plan → download+verify+install; return path.

    Every network/filesystem touch is an injected callable (defaults are the
    stdlib ones above), so this whole function is unit-tested with fakes.
    """
    manifest = get_json(manifest_url(api_base))
    plan = plan_from_manifest(manifest, home, api_base=api_base)
    return acquire_nrb(plan, token, get_bytes=get_bytes)

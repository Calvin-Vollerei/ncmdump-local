# -*- coding: utf-8 -*-
"""Single source of truth for the app's identity, version and update endpoint.

Kept dependency-free on purpose: the GUI and the CLI both import this, and it must not
pull anything heavy into the import path.
"""
from __future__ import annotations

import json
import re
import urllib.request

__all__ = [
    "APP_NAME", "DEVELOPER", "VERSION", "REPO_OWNER", "REPO_NAME", "REPO_URL",
    "RELEASES_URL", "API_LATEST_RELEASE", "parse_version", "is_newer",
    "check_for_update",
]

APP_NAME = "NCM 本地转换器"
DEVELOPER = "Calvin Vollerei Studio"
VERSION = "1.0.0"

# Where releases live. Set these once the repository exists; the update check simply
# reports "not available" until then, so nothing breaks before the first push.
REPO_OWNER = "Calvin-Vollerei"
REPO_NAME = "ncmdump-local"
REPO_URL = "https://github.com/%s/%s" % (REPO_OWNER, REPO_NAME)
RELEASES_URL = REPO_URL + "/releases"
API_LATEST_RELEASE = "https://api.github.com/repos/%s/%s/releases/latest" % (
    REPO_OWNER, REPO_NAME)


def parse_version(text: str):
    """Turn ``"v1.2.3"`` / ``"1.2.3-beta.1"`` into a comparable tuple.

    Returns ``None`` when there is no recognisable version in the string.
    """
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)*)", str(text))
    if not match:
        return None
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(candidate: str, current: str = VERSION) -> bool:
    """True when ``candidate`` is a strictly newer version than ``current``."""
    new, old = parse_version(candidate), parse_version(current)
    if new is None or old is None:
        return False
    return new > old


def check_for_update(timeout: int = 8, current: str = VERSION):
    """Ask GitHub for the latest release.

    Returns a dict::

        {"status": "update"|"current"|"unavailable",
         "latest": "1.2.0", "url": "...", "notes": "...", "error": "..."}

    Never raises: an unreachable API, a missing repository or a rate limit all come back
    as ``"unavailable"`` with a reason, because the caller is a UI button.
    """
    request = urllib.request.Request(API_LATEST_RELEASE, headers={
        # GitHub's API rejects requests without a User-Agent
        "User-Agent": "%s/%s" % (REPO_NAME, current),
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:                       # noqa: BLE001 - reported to the user
        return {"status": "unavailable", "error": "%s: %s" % (type(exc).__name__, exc)}

    tag = payload.get("tag_name") or payload.get("name") or ""
    url = payload.get("html_url") or RELEASES_URL
    notes = (payload.get("body") or "").strip()
    if not parse_version(tag):
        return {"status": "unavailable", "error": "release has no version tag",
                "url": url}
    return {
        "status": "update" if is_newer(tag, current) else "current",
        "latest": tag,
        "url": url,
        "notes": notes,
    }

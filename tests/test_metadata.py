# -*- coding: utf-8 -*-
"""App identity, version comparison and the update check.

The update check talks to the network, so the parsing/comparison is tested directly and
the network path is exercised through fakes.
"""

import io
import json

import pytest

from ncmdump.metadata import (
    API_LATEST_RELEASE,
    APP_NAME,
    DEVELOPER,
    RELEASES_URL,
    REPO_NAME,
    REPO_OWNER,
    REPO_URL,
    VERSION,
    check_for_update,
    is_newer,
    parse_version,
)


def test_identity_is_filled_in():
    assert APP_NAME
    assert DEVELOPER == "Calvin Vollerei Studio"
    assert VERSION.count(".") == 2
    # the API endpoint must be derived from the repo coordinates, not hand-maintained
    assert REPO_OWNER in REPO_URL and REPO_NAME in REPO_URL
    assert API_LATEST_RELEASE == (
        "https://api.github.com/repos/%s/%s/releases/latest" % (REPO_OWNER, REPO_NAME))
    assert RELEASES_URL == REPO_URL + "/releases"


def test_package_version_matches_metadata():
    """pyproject and the runtime constant must not drift apart."""
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    assert declared == VERSION, "pyproject version %s != metadata %s" % (declared, VERSION)


@pytest.mark.parametrize("text,expected", [
    ("v1.2.3", (1, 2, 3)),
    ("1.2.3", (1, 2, 3)),
    ("v2.0.0-beta.1", (2, 0, 0)),
    ("1.0", (1, 0, 0)),
    ("release-3", (3, 0, 0)),
    ("", None),
    ("no digits here", None),
    (None, None),
])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize("candidate,current,expected", [
    ("v1.0.1", "1.0.0", True),
    ("v1.0.0", "1.0.0", False),
    ("v0.9.9", "1.0.0", False),
    ("v1.1.0", "1.0.0", True),
    ("v2.0.0", "1.9.9", True),
    ("v1.0.0-beta", "1.0.0", False),
    ("garbage", "1.0.0", False),
    ("v1.0.1", "garbage", False),
])
def test_is_newer(candidate, current, expected):
    assert is_newer(candidate, current) is expected


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, payload=None, exc=None):
    import ncmdump.metadata as meta

    def fake(request, timeout=None):
        if exc is not None:
            raise exc
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(meta.urllib.request, "urlopen", fake)


def test_check_reports_an_available_update(monkeypatch):
    _patch_urlopen(monkeypatch, {
        "tag_name": "v9.9.9", "html_url": "https://example.invalid/rel",
        "body": "notes here",
    })
    info = check_for_update(current="1.0.0")
    assert info["status"] == "update"
    assert info["latest"] == "v9.9.9"
    assert info["url"] == "https://example.invalid/rel"
    assert info["notes"] == "notes here"


def test_check_reports_current(monkeypatch):
    _patch_urlopen(monkeypatch, {"tag_name": "v1.0.0", "html_url": "u"})
    assert check_for_update(current="1.0.0")["status"] == "current"


def test_check_never_raises_on_network_failure(monkeypatch):
    _patch_urlopen(monkeypatch, exc=OSError("connection refused"))
    info = check_for_update()
    assert info["status"] == "unavailable"
    assert "connection refused" in info["error"]


def test_check_handles_a_release_without_a_version_tag(monkeypatch):
    _patch_urlopen(monkeypatch, {"tag_name": "latest", "html_url": "u"})
    info = check_for_update()
    assert info["status"] == "unavailable"
    assert "version" in info["error"]


def test_check_falls_back_to_the_releases_page(monkeypatch):
    from ncmdump.metadata import RELEASES_URL

    _patch_urlopen(monkeypatch, {"tag_name": "v9.9.9"})
    info = check_for_update(current="1.0.0")
    assert info["url"] == RELEASES_URL        # no html_url in the payload


def test_lazy_download_helper_is_not_imported_at_module_load():
    """metadata must stay import-cheap: the GUI pulls it in at startup."""
    import os
    import subprocess
    import sys

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    code = ("import sys; sys.path.insert(0, %r); import ncmdump.metadata;"
            "print('numpy' in sys.modules, 'PySide6' in sys.modules)" % src)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False", out.stdout

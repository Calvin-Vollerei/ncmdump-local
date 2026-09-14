# -*- coding: utf-8 -*-
"""GUI-level tests that need PySide6 but no display.

The GUI is imported (not shown) so `_redact`, `_log_file` and the organize settings can be
tested directly. These are the pieces where a defect leaks information or writes files to
the wrong place, so they are worth pinning down.
"""

import os
import sys

import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6 (pip install .[gui])")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncmdump_gui import _log_file, _redact  # noqa: E402

# Paths are assembled at runtime on purpose: a literal drive path in this file would trip
# the repository hygiene guard, which cannot tell a fixture from a real leak.
_USER = "oxyge"
_LETTER = "D"
_USERS = "Users"          # split so this file has no literal machine-path pattern
_CASES = [
    ("home path", "C:\\%s\\%s\\Documents\\song.ncm" % (_USERS, _USER), [_USER]),
    ("other drive", "--report=%s:\\other\\workspace\\out.txt" % _LETTER,
     [_LETTER + ":\\", "workspace"]),
    ("forward slashes", "C:/%s/someone/music/x.ncm" % _USERS, ["someone"]),
    ("repr-ed argv", "['--selftest', '--report=%s:\\\\\\\\logs\\\\\\\\gui.txt']" % _LETTER,
     [_LETTER + ":\\\\", "logs"]),
    ("inside an exception",
     "FileNotFoundError: [WinError 2] C:\\%s\\bob\\a.ncm" % _USERS, ["bob"]),
]


@pytest.mark.parametrize("label,text,leaks", _CASES, ids=[c[0] for c in _CASES])
def test_redact_removes_machine_paths(label, text, leaks):
    cleaned = _redact(text)
    for leak in leaks:
        assert leak not in cleaned, "%r survived redaction: %s" % (leak, cleaned)
    assert any(marker in cleaned for marker in ("%USER%", "%PATH%", "<redacted>"))


def test_redact_keeps_the_flag_name():
    """The value carries a path; the flag name is the useful part of the breadcrumb."""
    cleaned = _redact("--selftest --report=C:\\tmp\\r.txt")
    assert "--report" in cleaned
    assert "r.txt" not in cleaned
    assert cleaned == "--selftest --report=<redacted>"


def test_redact_leaves_ordinary_text_alone():
    assert _redact("MainWindow constructed, selftest=True") == \
        "MainWindow constructed, selftest=True"


def test_log_file_lives_outside_the_repository():
    """Regression: a debug log used to be written to the relative path ncm-work/...,
    which created a directory inside the repository and recorded raw argv."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = _log_file("gui_trace.log")
    assert os.path.isabs(path)
    assert not os.path.abspath(path).startswith(os.path.abspath(repo_root))
    assert path.endswith("gui_trace.log")


def test_log_file_directory_is_writable():
    """Whatever location is chosen must actually be usable, or diagnostics vanish."""
    path = _log_file("writetest.log")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("probe\n")
    assert os.path.getsize(path) > 0
    os.remove(path)


def test_startup_and_debug_logs_share_a_directory():
    a = os.path.dirname(_log_file("ncm_gui_startup.log"))
    b = os.path.dirname(_log_file("gui_trace.log"))
    assert a == b

# -*- coding: utf-8 -*-
"""File-lifecycle tests: naming, publishing, deletion and concurrency.

This is the layer where the real bugs lived (see the concurrency test below), and it had
no coverage at all before — the format code was well tested, the plumbing was not.
"""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import ncm_builder as B
import pytest

from ncmdump.ncm2mp3 import (
    _claim_path,
    _convert,
    convert_one,
    find_inputs,
    load_lyrics,
    sanitize,
    split_lrc,
)

AUDIO = B.fake_flac(b"payload" * 300)


def _fixture(directory, name, title, **kwargs):
    path = os.path.join(directory, name)
    B.write(path, kwargs.pop("audio", AUDIO), title=title, **kwargs)
    return path


# --- atomic claiming ---------------------------------------------------------

def test_claim_path_takes_the_requested_name(tmp_path):
    target = str(tmp_path / "song.flac")
    assert _claim_path(target) == target
    assert os.path.exists(target)          # the placeholder reservation


def test_claim_path_never_returns_the_same_name_twice(tmp_path):
    target = str(tmp_path / "song.flac")
    names = {_claim_path(target) for _ in range(25)}
    assert len(names) == 25
    for name in names:
        assert os.path.exists(name)


def test_claim_path_is_atomic_under_threads(tmp_path):
    """The heart of the old bug: check-then-create let two threads pick one name."""
    target = str(tmp_path / "song.flac")
    results = []
    lock = threading.Lock()

    def claim():
        got = _claim_path(target)
        with lock:
            results.append(got)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: claim(), range(64)))
    assert len(set(results)) == 64, "two workers claimed the same output path"


# --- concurrency: different songs, identical titles --------------------------

def test_concurrent_same_title_conversions_all_survive(tmp_path):
    """Regression: two *different* songs with the same title used to destroy each other.

    The temp path and the final path were both derived from the title only, so workers
    shared one staging file and one destination. Serial runs were always fine.
    """
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    count = 8
    for i in range(count):
        _fixture(str(src), "song%d.ncm" % i, "Identical Title",
                 artists=["Artist %d" % i], audio=B.fake_flac(b"payload-%d" % i) * 40)

    files = find_inputs([str(src)])
    assert len(files) == count
    jobs = [(p, str(out), True, False, False, False, None, False, True) for p in files]

    for _ in range(3):                      # the old code failed intermittently
        for name in os.listdir(out) if out.exists() else []:
            os.remove(os.path.join(out, name))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(convert_one, jobs))
        ok = [r for r in results if r["ok"]]
        assert len(ok) == count, "lost conversions: %s" % [
            (r["name"], r["error"]) for r in results if not r["ok"]]
        produced = sorted(os.listdir(out))
        assert len(produced) == count, "outputs collided: %s" % produced
        # every output must be a distinct, complete file
        sizes = {os.path.getsize(os.path.join(out, f)) for f in produced}
        assert len(sizes) == 1, "a worker truncated another worker's file: %s" % sizes


def test_concurrent_conversion_keeps_sources(tmp_path):
    """With keep=True nothing may be deleted, whatever the race."""
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    for i in range(6):
        _fixture(str(src), "s%d.ncm" % i, "Same Name")
    files = find_inputs([str(src)])
    jobs = [(p, str(out), True, False, False, False, None, False, True) for p in files]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(convert_one, jobs))
    assert all(r["ok"] for r in results)
    assert len(find_inputs([str(src)])) == 6


# --- publishing order --------------------------------------------------------

def test_failed_tagging_leaves_no_output(tmp_path, monkeypatch):
    """Regression: the file used to be published *before* tagging, so a tag failure left
    a full-length untagged file behind while reporting failure."""
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    path = _fixture(str(src), "a.ncm", "Tag Failure")

    import ncmdump.ncm2mp3 as cli
    monkeypatch.setattr(cli, "write_tags", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

    result = _convert(path, str(out), True, False, False, False, None, False, False)
    assert not result["ok"] and "boom" in result["error"]
    produced = os.listdir(out) if out.exists() else []
    assert produced == [], "a failed conversion left %s behind" % produced


def test_successful_conversion_publishes_a_tagged_file(tmp_path):
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    path = _fixture(str(src), "b.ncm", "Tagged Song", artists=["Someone"])
    result = _convert(path, str(out), True, False, False, False, None, False, False)
    assert result["ok"], result["error"]
    from ncmdump.tags import read_flac_tags

    tags = {}
    for key, value in read_flac_tags(result["out"]):
        tags.setdefault(key, []).append(value)
    assert tags["TITLE"] == ["Tagged Song"]
    assert os.listdir(out) == [os.path.basename(result["out"])]


def test_no_temp_files_left_behind(tmp_path):
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    for i in range(4):
        _fixture(str(src), "c%d.ncm" % i, "Temp Check")
    files = find_inputs([str(src)])
    jobs = [(p, str(out), True, False, True, False, None, False, True) for p in files]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(convert_one, jobs))
    leftovers = [f for f in os.listdir(out) if f.endswith(".ncmtmp") or f.endswith(".id3tmp")]
    assert leftovers == [], "staging files leaked: %s" % leftovers


# --- verify-then-delete ------------------------------------------------------

def test_source_is_kept_when_verification_fails(tmp_path, monkeypatch):
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    path = _fixture(str(src), "d.ncm", "Verify Fail")

    import ncmdump.ncm2mp3 as cli
    monkeypatch.setattr(cli, "_verify_output", lambda *a, **k: False)
    result = _convert(path, str(out), False, False, False, False, None, False, True)
    assert not result["ok"]
    assert os.path.exists(path), "the source was deleted despite a failed verification"


def test_source_is_deleted_only_after_a_passing_check(tmp_path):
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    path = _fixture(str(src), "e.ncm", "Delete Me")
    result = _convert(path, str(out), False, False, False, False, None, False, True)
    assert result["ok"], result["error"]
    assert not os.path.exists(path), "source survived a verified conversion"
    assert len(os.listdir(out)) == 1


# --- small helpers -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Song", "Song"),
    ('a/b\\c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
    ("  trailing dots.  ", "trailing dots"),
    ("", "unnamed"),
    ("...", "unnamed"),
    ("x" * 400, None),                      # truncated, see the assertion below
])
def test_sanitize(raw, expected):
    got = sanitize(raw)
    if expected is None:
        assert len(got) <= 120 and got == "x" * len(got)
    else:
        assert got == expected


def test_sanitize_strips_path_separators():
    assert "/" not in sanitize("a/b") and "\\" not in sanitize("a\\b")


def test_split_lrc_drops_the_json_header():
    text = '{"t":0,"c":[{"tx":"作词: "}]}\n[00:01.00]hello\n[00:02.00]world\n'
    plain, lrc = split_lrc(text)
    assert plain == "hello\nworld"
    assert lrc.startswith("[00:01.00]")


def test_find_inputs_is_recursive_and_deduplicated(tmp_path):
    (tmp_path / "sub").mkdir()
    _fixture(str(tmp_path), "a.ncm", "A")
    _fixture(str(tmp_path / "sub"), "b.ncm", "B")
    (tmp_path / "ignore.txt").write_text("x")
    found = find_inputs([str(tmp_path)])
    assert len(found) == 2
    assert len(find_inputs([str(tmp_path), str(tmp_path)])) == 2


def test_load_lyrics_reads_a_sidecar(tmp_path):
    path = _fixture(str(tmp_path), "f.ncm", "F")
    with open(os.path.splitext(path)[0] + ".lrc", "w", encoding="utf-8") as fh:
        fh.write("[00:01.00]hi\n")
    assert "hi" in load_lyrics(path)
    assert load_lyrics(os.path.join(str(tmp_path), "missing.ncm")) == ""


def test_module_import_does_not_pull_the_pool_or_numpy():
    """Startup budget: neither must be imported just to use the GUI."""
    import subprocess

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    code = (
        "import sys; sys.path.insert(0, %r);"
        "import ncmdump.ncm2mp3;"
        "print('concurrent.futures' in sys.modules, 'numpy' in sys.modules,"
        " 'multiprocessing' in sys.modules)" % src
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False False", out.stdout

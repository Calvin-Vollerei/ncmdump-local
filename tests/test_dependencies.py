# -*- coding: utf-8 -*-
"""The package must stay standard-library only at import time.

The rule itself lives in scripts/stdlib_guard.py - the same file CI runs and the same file
scripts/verify_dependency_guard.py proves still rejects bad imports - so this test cannot
drift away from what the job enforces.

It exists as a test as well as a job because the CI version once used a hand-kept whitelist
that went stale and rejected a legitimate `import threading` on a real commit. A rule that
fails on correct code costs more than it catches, so the decoys below are checked in both
directions: bad imports must be rejected, and imports that are legitimately fine must not be.
"""

import os
import subprocess
import sys

import pytest
import stdlib_guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PACKAGE = os.path.join(SRC, "ncmdump")

STDLIB = stdlib_guard.stdlib_names()
needs_stdlib_list = pytest.mark.skipif(
    not STDLIB, reason="needs Python 3.10+ (sys.stdlib_module_names)")

# A module written the way this package is supposed to be written: stdlib and own-package
# imports at module scope, the optional accelerator deferred inside a function.
CLEAN = '''\
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ncmdump.ncm_core import NcmError
from . import tags


def _optional_accelerator():
    import numpy
'''

REJECTED = [
    pytest.param("\n\nimport requests\n", id="third-party-at-module-scope"),
    pytest.param("\n\nfrom yaml import safe_load\n", id="third-party-from-import"),
    pytest.param("\n\ntry:\n    import requests\nexcept ImportError:\n    pass\n",
                 id="third-party-in-module-level-try"),
    pytest.param("\n\ntry:\n    import numpy\nexcept ImportError:\n    pass\n",
                 id="optional-dep-in-module-level-try"),
    pytest.param("\n\nif os.environ.get('NCM_PROBE'):\n    import requests\n",
                 id="third-party-in-module-level-if"),
    pytest.param("\n\nclass _Probe:\n    import requests\n",
                 id="third-party-in-class-body"),
    pytest.param("\n\n\ndef _deferred():\n    import requests\n",
                 id="third-party-inside-a-function"),
]

ACCEPTED = [
    pytest.param("\n\n\ndef _deferred():\n    import numpy\n",
                 id="optional-dep-inside-a-function"),
    pytest.param("\n\nif TYPE_CHECKING:\n    import requests\n",
                 id="typing-only-import"),
]


def _module_files():
    return sorted(f for f in os.listdir(PACKAGE) if f.endswith(".py"))


def _probe(tmp_path, extra):
    path = tmp_path / "probe.py"
    path.write_text(CLEAN + extra, encoding="utf-8")
    return str(path)


@needs_stdlib_list
@pytest.mark.parametrize("name", _module_files())
def test_package_modules_are_stdlib_only(name):
    """Every real module: module-level imports stdlib only, deferred ones declared optional."""
    assert stdlib_guard.inspect(os.path.join(PACKAGE, name)) == []


@needs_stdlib_list
def test_the_probe_skeleton_itself_is_clean(tmp_path):
    """Guard against the decoys below passing because the skeleton is already broken."""
    assert stdlib_guard.inspect(_probe(tmp_path, "")) == []


@needs_stdlib_list
@pytest.mark.parametrize("decoy", REJECTED)
def test_rule_rejects(tmp_path, decoy):
    """A checker that rejects nothing would be worse than the whitelist it replaced.

    The module-level try/except cases are the regression this rule was rewritten for: the
    old check only walked top-level statements (a Try node is not an import node), and the
    runtime backstop could not help either, because the except swallowed the ImportError.
    """
    problems = stdlib_guard.inspect(_probe(tmp_path, decoy))
    assert problems, "the guard accepted %r" % decoy


@needs_stdlib_list
@pytest.mark.parametrize("decoy", ACCEPTED)
def test_rule_accepts_legitimate_imports(tmp_path, decoy):
    """Rejecting a legitimate import is the failure mode this rule was rewritten for."""
    problems = stdlib_guard.inspect(_probe(tmp_path, decoy))
    assert problems == [], "the guard rejected a legitimate import: %r" % problems


def test_package_imports_without_third_party_packages():
    """Import the whole package in a subprocess whose site-packages are disabled."""
    code = ("import sys; sys.path.insert(0, %r);"
            "import ncmdump, ncmdump.ncm_core, ncmdump.tags, ncmdump.ncm2mp3, ncmdump.metadata;"
            "print('ok')" % SRC)
    out = subprocess.run([sys.executable, "-S", "-c", code],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"

# -*- coding: utf-8 -*-
"""The package must stay standard-library only at import time.

This is the same rule the CI 'zero-dependency' job enforces, expressed as a test so it
fails locally too — the CI version once used a hand-kept whitelist that went stale and
rejected a legitimate `import threading` on a real commit.
"""

import ast
import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
PACKAGE = os.path.join(SRC, "ncmdump")

# Optional accelerators that may be imported, but only lazily inside a function.
OPTIONAL = {"numpy"}

STDLIB = set(getattr(sys, "stdlib_module_names", ()))
OWN = {"ncmdump", "ncmdump_gui"}


def _module_files():
    return sorted(f for f in os.listdir(PACKAGE) if f.endswith(".py"))


def _roots(node):
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".")[0]]
    return []


def _tree(name):
    path = os.path.join(PACKAGE, name)
    return ast.parse(open(path, encoding="utf-8").read())


@pytest.mark.skipif(not STDLIB, reason="needs Python 3.10+ (sys.stdlib_module_names)")
@pytest.mark.parametrize("name", _module_files())
def test_module_level_imports_are_stdlib_only(name):
    """A module-level third-party import would break `import ncmdump` without it installed."""
    for node in _tree(name).body:
        for root in _roots(node):
            assert root in STDLIB or root in OWN, (
                "%s: module-level import %r is not in the standard library" % (name, root))


@pytest.mark.skipif(not STDLIB, reason="needs Python 3.10+ (sys.stdlib_module_names)")
@pytest.mark.parametrize("name", _module_files())
def test_deferred_imports_are_declared_optional(name):
    """Anything imported inside a function must be a known optional dependency."""
    for node in ast.walk(_tree(name)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            for root in _roots(sub):
                if root in STDLIB or root in OWN:
                    continue
                assert root in OPTIONAL, (
                    "%s: %r is neither stdlib nor a declared optional dependency"
                    % (name, root))


def test_optional_dependencies_are_actually_optional():
    """numpy must be importable-absent: the core has to work without it."""
    for name in _module_files():
        for node in _tree(name).body:
            assert not (set(_roots(node)) & OPTIONAL), (
                "%s imports an optional dependency at module level" % name)


def test_package_imports_without_third_party_packages():
    """Import the whole package in a subprocess whose site-packages are disabled."""
    import subprocess

    code = ("import sys; sys.path.insert(0, %r);"
            "import ncmdump, ncmdump.ncm_core, ncmdump.tags, ncmdump.ncm2mp3, ncmdump.metadata;"
            "print('ok')" % SRC)
    out = subprocess.run([sys.executable, "-S", "-c", code],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"

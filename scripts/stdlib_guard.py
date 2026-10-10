# -*- coding: utf-8 -*-
"""The one implementation of the stdlib-only import rule.

CI, ``scripts/verify_dependency_guard.py`` and ``tests/test_dependencies.py`` all come here,
so the rule cannot drift into three slightly different copies that disagree about what it
rejects.

The rule, in full:

* an import that can run at module scope must be standard library (or this project), so that
  ``import ncmdump`` never needs a third-party package to be installed;
* an import inside a function may also be one of the declared optional accelerators, which
  is how ``numpy`` stays optional instead of becoming a requirement;
* an import under ``if TYPE_CHECKING:`` is exempt, because that block never executes.

"Module scope" means *anything that is not inside a function* - not merely a top-level
statement. A ``try: import numpy / except ImportError: pass`` block at module scope runs on
import exactly like a bare ``import numpy``; the try/except only hides the failure. That
construction used to satisfy this rule (the old check walked top-level statements only, and
the ``Try`` node is not an import node) *and* the runtime check that backs this rule up (the
swallowed ImportError) - two blind spots that lined up. Deferred imports are therefore
enforced by position, not by nesting depth.
"""
import ast
import os
import sys

# Optional accelerators: importable, but only lazily inside a function.
OPTIONAL = frozenset({"numpy"})
# This project's own modules, which a module-level import is allowed to name.
OWN = frozenset({"ncmdump", "ncmdump_gui"})

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
PACKAGE_DIR = os.path.join(REPO, "src", "ncmdump")


def stdlib_names():
    """The stdlib module names, or an empty set on Python 3.9 (which has no such list)."""
    return set(getattr(sys, "stdlib_module_names", ()))


def roots_of(node):
    """Top-level module names named by one Import/ImportFrom node.

    Relative imports (``from . import x``) resolve inside this package, so they are not a
    dependency question and are deliberately not reported.
    """
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".")[0]]
    return []


def _imports_under(nodes):
    """Ids of every Import/ImportFrom node reachable from ``nodes``."""
    ids = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                ids.add(id(sub))
    return ids


def _is_type_checking(test):
    """``TYPE_CHECKING`` or ``typing.TYPE_CHECKING`` - and nothing else.

    ``if not TYPE_CHECKING:`` is a UnaryOp, so it correctly does *not* match: that branch
    does run.
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _type_only_ids(tree):
    """Imports under ``if TYPE_CHECKING:`` - never executed, so never a runtime dependency.

    Only the ``body`` is exempt. The ``else`` branch of the same ``if`` does run.
    """
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            ids |= _imports_under(node.body)
    return ids


def _function_ids(tree):
    """Imports inside a function body, at any nesting depth."""
    return _imports_under(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))


def inspect(path, optional=OPTIONAL, own=OWN):
    """Violations in a single file, as human-readable lines (empty means clean)."""
    if not stdlib_names():
        raise RuntimeError("needs Python 3.10+ for sys.stdlib_module_names")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=str(path))
    allowed = stdlib_names() | set(own)
    type_only = _type_only_ids(tree)
    in_function = _function_ids(tree)
    name = os.path.basename(path)

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if id(node) in type_only:
            continue
        for root in roots_of(node):
            if root in allowed:
                continue
            if id(node) in in_function:
                if root not in optional:
                    problems.append(
                        "%s: %r is neither stdlib nor a declared optional dependency"
                        % (name, root))
            else:
                problems.append(
                    "%s: module-level import %r is not in the standard library"
                    % (name, root))
    return problems


def violations(package_dir=PACKAGE_DIR, optional=OPTIONAL, own=OWN):
    """Violations in every ``*.py`` of a package directory, each line naming its file."""
    problems = []
    for name in sorted(os.listdir(package_dir)):
        if name.endswith(".py"):
            problems.extend(inspect(os.path.join(package_dir, name), optional, own))
    return problems


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not stdlib_names():
        print("need Python 3.10+ for sys.stdlib_module_names", file=sys.stderr)
        return 2
    problems = violations(args[0] if args else PACKAGE_DIR)
    if problems:
        for line in problems:
            print(line)
        return 1
    print("stdlib-only import check: OK (module-level imports are stdlib only;"
          " optional: %s)" % ",".join(sorted(OPTIONAL)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

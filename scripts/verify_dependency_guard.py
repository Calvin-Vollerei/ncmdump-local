# -*- coding: utf-8 -*-
"""Run the stdlib-only guard locally, and prove it still rejects bad imports.

The guard once compared imports against a hand-kept whitelist, which went stale the moment a
new stdlib import arrived (it rejected 'threading'). It is now stdlib-driven - but a checker
that rejects nothing would be worse than the whitelist it replaced, so this script injects
decoy imports and asserts each one is caught. The rule itself lives in scripts/stdlib_guard.py,
shared with CI and tests/test_dependencies.py, so there is only one thing to keep honest.

Both directions are checked. Rejecting a *good* import is not a safe failure: that is the
exact bug this rule was rewritten for, so imports that are legitimately fine (a deferred
numpy, a typing-only import under `if TYPE_CHECKING:`) must come back clean.
"""
import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TARGET = os.path.join(REPO, "src", "ncmdump", "ncm_core.py")
GUARD = os.path.join(HERE, "stdlib_guard.py")

# The full CI check: the rule, plus a real import with site-packages disabled. The imports
# run first, so a leaked dependency fails here even if the static rule somehow missed it.
FULL = r"""
import os, sys, importlib.util
assert importlib.util.find_spec('numpy') is None, 'numpy leaked into the stdlib-only environment'
import ncmdump, ncmdump.ncm_core, ncmdump.tags, ncmdump.ncm2mp3, ncmdump.aes_lite, ncmdump.metadata
sys.path.insert(0, {scripts!r})
import stdlib_guard
problems = stdlib_guard.violations()
if problems:
    for line in problems:
        print(line)
    raise SystemExit(1)
print('stdlib-only import check: OK')
"""

# (injected code, token that must appear in the failure). The first five are module scope in
# five different shapes: the try/except pair is the one that used to slip through both the
# static check (a Try node is not an import node, so the old top-level walk never saw it) and
# the runtime backstop (the except swallowed the ImportError).
MUST_REJECT = [
    ("import requests", "requests"),                              # third party, module scope
    ("from yaml import safe_load", "yaml"),                       # third party, module scope
    ("\nimport numpy\n", "numpy"),                                # optional dep, module scope
    ("\ntry:\n    import requests\nexcept ImportError:\n    pass\n", "requests"),
    ("\ntry:\n    import numpy\nexcept ImportError:\n    pass\n", "numpy"),
    ("\n\ndef _deferred():\n    import requests\n", "requests"),  # third party, in a function
]

# (label, injected code) - legitimate, and the guard must say so.
MUST_ACCEPT = [
    ("deferred numpy inside a function",
     "\n\ndef _deferred_optional():\n    import numpy\n"),
    ("typing-only import under TYPE_CHECKING",
     "\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import requests\n"),
]


def _env():
    return dict(os.environ, PYTHONPATH=os.path.join(REPO, "src"))


def run_full():
    """Rule + runtime import, the way CI runs it."""
    out = subprocess.run([sys.executable, "-S", "-c", FULL.format(scripts=HERE)],
                         cwd=REPO, env=_env(), capture_output=True, text=True)
    return out.returncode, (out.stdout + out.stderr).strip()


def run_rule_only():
    """The rule alone, with no imports of its own.

    Injecting a decoy that can actually be imported (requests is absent) would otherwise
    fail on the ImportError before the rule ever ran, and the check would look like it
    worked for the wrong reason.
    """
    out = subprocess.run([sys.executable, "-S", GUARD],
                         cwd=REPO, env=_env(), capture_output=True, text=True)
    return out.returncode, (out.stdout + out.stderr).strip()


def inject(source, decoy):
    """Insert the decoy after the last module-level import.

    Prepending it would push `from __future__ import annotations` off the top of the file
    (a SyntaxError), and appending after a naive "last line starting with import" can land
    inside a parenthesised import. Either way the guard would fail for a parse error rather
    than for the rule under test, proving nothing. A decoy that starts with a blank line is
    meant to be a whole new block, so it goes at the end of the file instead. ast gives the
    real end of the import block.
    """
    if decoy.startswith("\n"):
        return source + decoy
    tree = ast.parse(source)
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = max(last, getattr(node, "end_lineno", 0))
    lines = source.splitlines(keepends=True)
    return "".join(lines[:last]) + decoy + "\n" + "".join(lines[last:])


def with_decoy(original, decoy, run):
    with open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(inject(original, decoy))
    return run()


def main():
    if not getattr(sys, "stdlib_module_names", None):
        print("need Python 3.10+ for sys.stdlib_module_names")
        return 2

    code, output = run_full()
    print("clean tree                 -> exit %d  %s"
          % (code, output.splitlines()[-1] if output else ""))
    if code != 0:
        print("FAIL: the guard rejects the real tree")
        print(output)
        return 1

    original = open(TARGET, encoding="utf-8").read()
    failures = 0
    try:
        for decoy, expect in MUST_REJECT:
            code, output = with_decoy(original, decoy, run_rule_only)
            # The rule must be what failed - not a parse error, and not the runtime import.
            by_rule = "standard library" in output or "neither stdlib" in output
            caught = code != 0 and expect in output and by_rule
            label = decoy.strip().replace("\n", " / ") or "(empty)"
            print("must reject  %-52s -> exit %d  caught=%s"
                  % (label.replace("    ", " "), code, caught))
            if not caught:
                failures += 1
                print("     !! NOT rejected - the guard has a hole")
                print("     actual output: %r" % output[-240:])

        for label, decoy in MUST_ACCEPT:
            code, output = with_decoy(original, decoy, run_rule_only)
            print("must accept  %-52s -> exit %d  %s"
                  % (label, code, "OK" if code == 0 else "FALSE POSITIVE"))
            if code != 0:
                failures += 1
                print("     !! a legitimate import was rejected")
                print("     actual output: %r" % output[-240:])
    finally:
        with open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(original)

    code, _ = run_full()
    print("restored tree              -> exit %d" % code)
    return 1 if (failures or code != 0) else 0


if __name__ == "__main__":
    sys.exit(main())

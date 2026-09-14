# -*- coding: utf-8 -*-
"""Run the CI stdlib-only guard locally, and prove it still rejects bad imports.

The guard used to compare module imports against a hand-kept whitelist. That whitelist went
stale the moment a new stdlib import arrived (it rejected 'threading'), so the guard is now
stdlib-driven. A checker that rejects nothing would be worse than the whitelist, so this
script injects decoy imports and asserts each one is caught.
"""
import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ncm2mp3"))
TARGET = os.path.join(REPO, "src", "ncmdump", "ncm_core.py")

GUARD = r"""
import sys, importlib.util
assert importlib.util.find_spec('numpy') is None, 'numpy leaked into the stdlib-only environment'
import ncmdump, ncmdump.ncm_core, ncmdump.tags, ncmdump.ncm2mp3, ncmdump.aes_lite, ncmdump.metadata
import ast, pathlib

stdlib = set(getattr(sys, 'stdlib_module_names', ()))
if not stdlib:
    raise SystemExit('need Python 3.10+ for sys.stdlib_module_names')
allowed = stdlib | {'ncmdump'}
OPTIONAL = {'numpy'}

def roots_of(node):
    if isinstance(node, ast.Import):
        return [a.name.split('.')[0] for a in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split('.')[0]]
    return []

for path in sorted(pathlib.Path('src/ncmdump').glob('*.py')):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in tree.body:
        for root in roots_of(node):
            assert root in allowed, (
                f'{path.name}: module-level import {root!r} is not in the standard library')
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                for root in roots_of(sub):
                    if root in allowed:
                        continue
                    assert root in OPTIONAL, (
                        f'{path.name}: {root!r} is neither stdlib nor a declared '
                        f'optional dependency')
print('stdlib-only import check: OK')
"""


def run():
    env = dict(os.environ, PYTHONPATH=os.path.join(REPO, "src"))
    out = subprocess.run([sys.executable, "-S", "-c", GUARD], cwd=REPO, env=env,
                         capture_output=True, text=True)
    return out.returncode, (out.stdout + out.stderr)


def inject(source: str, decoy: str) -> str:
    """Insert the decoy after the last module-level import.

    Prepending it would push `from __future__ import annotations` off the top of the file
    (a SyntaxError), and appending after a naive "last line starting with import" can land
    inside a parenthesised import. Either way the guard would fail for a parse error rather
    than for the rule under test, proving nothing. ast gives the real end of the block.
    """
    import ast

    tree = ast.parse(source)
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = max(last, getattr(node, "end_lineno", 0))
    lines = source.splitlines(keepends=True)
    if decoy.startswith("\n"):
        return source + decoy          # a trailing function definition
    return "".join(lines[:last]) + decoy + "\n" + "".join(lines[last:])


def main():
    code, output = run()
    last = output.strip().splitlines()[-1] if output.strip() else ""
    print("clean tree               -> exit %d  %s" % (code, last))
    if code != 0:
        print("FAIL: the guard rejects the real tree")
        print(output)
        return 1

    original = open(TARGET, encoding="utf-8").read()
    failures = 0
    decoys = [
        # (injected code, token that must appear in the failure)
        ("import requests", "requests"),                       # third party, module level
        ("from yaml import safe_load", "yaml"),                # third party, module level
        ("import numpy", "numpy"),                             # optional dep at module level
        ("\n\ndef _deferred():\n    import requests\n", "requests"),   # third party, deferred
    ]
    try:
        for decoy, expect in decoys:
            with open(TARGET, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(inject(original, decoy))
            code, output = run()
            caught = code != 0 and expect in output
            label = decoy.strip().replace("\n", " / ")
            print("decoy %-34s -> exit %d  caught=%s" % (label, code, caught))
            if not caught:
                failures += 1
                print("     !! NOT rejected - the guard has a hole")
                print("     actual output: %r" % output.strip()[-240:])
    finally:
        with open(TARGET, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(original)

    code, _ = run()
    print("restored tree            -> exit %d" % code)
    return 1 if (failures or code != 0) else 0


if __name__ == "__main__":
    sys.exit(main())

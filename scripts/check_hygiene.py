# -*- coding: utf-8 -*-
"""Run the same repository-hygiene rules the CI job enforces, but against the working
tree (before anything is committed). Catches leaks while they are still cheap to fix."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root
SKIP_DIRS = {".git", "__pycache__", "dist", "build", ".pytest_cache", ".ruff_cache",
             "ncm-work", ".agent-teams", "wheels"}

MEDIA = re.compile(
    r"\.(ncm|uc|kwm|qmc[0-9a-z]*|mflac[0-9]*|mgg[0-9]*|kgm[0-9a]*|tm[0-9]|bkc[a-z0-9]*|vpr"
    r"|flac|mp3|m4a|aac|ogg|wav|ape|dff|wma|lrc|jpg|jpeg|png|webp|gif)$", re.I)
ARTEFACT = re.compile(r"\.(pyc|pyo|whl|exe|dll|pyd|log)$", re.I)
SECRET = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,})")
# Windows user paths and unix home directories baked into source. Both separators are
# matched: a path typed with forward slashes ("C:/Users/...") leaks exactly as badly as the
# backslash form, and a checker that only knows about "\" gives false confidence.
MACHINE_PATH = re.compile(
    r"([A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+[A-Za-z0-9._-]+"
    r"|/home/[A-Za-z0-9._-]+/|/Users/[A-Za-z0-9._-]+/|/root/)")
TEXT_EXT = {".py", ".md", ".toml", ".yml", ".yaml", ".cfg", ".txt", ".spec", ".bat", ".sh", ""}
# documentation is allowed to *mention* example paths, source code is not
PATH_EXEMPT = re.compile(r"(AUDIT-.*\.md|README\.md|NOTICE\.md|SECURITY\.md)$")
# ...but an exempt report must never be *tracked*: that would publish the very leak it
# documents. The audit reports live outside the repository on purpose.
REPORT_EXEMPT = re.compile(r"AUDIT[-_].*\.md$", re.I)


def tracked_files():
    """Paths git tracks, or None when this is not a git checkout."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files"], check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [line for line in out.stdout.decode("utf-8", "replace").splitlines() if line]

problems = []


def walk():
    found = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            found.append(os.path.join(base, name))
    return found


FILES = walk()
# The guard necessarily contains the very patterns it hunts for, so it is the one source
# file excluded from the content scan (registered in SELF).
SELF = os.path.relpath(os.path.abspath(__file__), ROOT).replace("\\", "/")
for path in FILES:
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    if MEDIA.search(name := os.path.basename(path)):
        problems.append("media/container file: %s" % rel)
    if ARTEFACT.search(name):
        problems.append("build artefact or log: %s" % rel)
    if name in (".env", "id_rsa") or name.endswith((".pem", ".key", ".p12", ".pfx")):
        problems.append("credential file: %s" % rel)
    if rel == SELF:
        continue
    ext = os.path.splitext(name)[1].lower()
    if ext in TEXT_EXT and not PATH_EXEMPT.search(rel):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for match in SECRET.finditer(text):
            problems.append("possible credential in %s: %s..." % (rel, match.group(0)[:12]))
        for match in MACHINE_PATH.finditer(text):
            problems.append("machine path in %s: %s" % (rel, match.group(0)))

print("scanned:", len(FILES), "files")

# An audit report quotes machine paths as evidence, so it is exempt from the path scan —
# which makes it the one document that must never be committed. `git add -f` and a
# repository built from a copy are the two ways it sneaks in, so ask git directly.
tracked = tracked_files()
if tracked is None:
    print("note: not a git checkout, skipping the tracked-file check")
else:
    for rel in tracked:
        if REPORT_EXEMPT.search(rel):
            problems.append("an audit report is tracked by git (it quotes machine paths): %s" % rel)
        if rel.startswith("ncm-work/"):
            problems.append("a development-only path is tracked by git: %s" % rel)

# Stale copies of this project living NEXT TO the repository are a real hazard: they were
# the pre-refactor version (with a machine-specific default path) and copying one back
# would reintroduce the leak — or silently shadow the current package on sys.path. This is
# the one check that deliberately looks outside the repository.
def check_stale_neighbours():
    parent = os.path.dirname(ROOT)
    ours = {}
    for base, _dirs, names in os.walk(os.path.join(ROOT, "src")):
        for name in names:
            if name.endswith((".py", ".spec")):
                ours.setdefault(name, os.path.join(base, name))
    warnings = []
    for base, dirs, names in os.walk(parent):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.abspath(os.path.join(base, d)).startswith(ROOT)]
        for name in names:
            if name in ours:
                path = os.path.join(base, name)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        text = fh.read()
                except OSError:
                    continue
                baked = MACHINE_PATH.search(text)
                why = ("still hard-codes a machine path: %s" % baked.group(0)
                       if baked else "duplicate copy")
                warnings.append("%s (%s)" % (path, why))
    return warnings


for warning in check_stale_neighbours():
    print("\nWARNING: stale neighbour copy outside the repo: %s" % warning)
    print("         Do not copy it back over src/; delete it instead.")

if problems:
    print("\nPROBLEMS (%d):" % len(problems))
    for p in sorted(set(problems)):
        print("  -", p)
    sys.exit(1)
print("hygiene: clean (no media, artefacts, credentials or machine paths)")

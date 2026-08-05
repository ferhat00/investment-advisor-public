"""Publish a sanitized public snapshot of this (private) repo.

Copies every git-tracked file EXCEPT the exclude list below into a scratch
git repo and pushes it as a single fresh commit — the public repo's history
is a rolling snapshot, not a mirror of private commit history.

Excluded from the public copy:
  theses/            real thesis outputs (NAV, positions, trade fills) —
                     except theses/NVDA-smoketest/, a synthetic fixture the
                     test suite needs and that carries no real data.
  DECISION_LOG.md    running log of real decisions/trades/NAV.
  *_ARTICLE_*.md     private write-ups / model-comparison articles authored
                     off real runs (e.g. LJPY_MODEL_COMPARISON_ARTICLE_*.md).
  .cache/            yfinance runtime cache (cookies/timezone DBs). Gitignored
                     via /.cache but tracked from before that rule was added,
                     so exclude it here too — never mirror runtime artifacts.

Usage:
  python3 scripts/sync_public.py --remote <public-repo-git-url> [--branch main] [--push]

Without --push this is a dry run: it reports what would be copied/excluded
and leaves the staged tree on disk for inspection, without touching the
remote.
"""
import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

EXCLUDE_PREFIXES = ("theses/", ".cache/")
EXCLUDE_FILES = {"DECISION_LOG.md"}
# Private write-ups / model-comparison articles authored off real runs. Matched
# against the file's basename anywhere in the tree, so a dated article at the
# repo root (or moved into a subfolder later) is always kept out of the mirror.
EXCLUDE_GLOBS = ("*_ARTICLE_*.md",)
KEEP_UNDER_EXCLUDED_PREFIX = ("theses/NVDA-smoketest/",)

# Safety net: if these ever show up in the staged tree, something regressed
# upstream of this script (e.g. a name/email reference was reintroduced).
FORBIDDEN_STRINGS = ("Ferhat", "ferhat00@gmail.com")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def is_excluded(path):
    if path in EXCLUDE_FILES:
        return True
    basename = os.path.basename(path)
    if any(fnmatch.fnmatch(basename, glob) for glob in EXCLUDE_GLOBS):
        return True
    for prefix in EXCLUDE_PREFIXES:
        if path.startswith(prefix):
            if any(path.startswith(keep) for keep in KEEP_UNDER_EXCLUDED_PREFIX):
                return False
            return True
    return False


# Normalize to forward slashes: `git ls-files` always emits "/" separators, but
# os.path.relpath uses the platform separator ("\" on Windows), so without this
# the self-skip below never matches on Windows and the scan aborts on this file.
_SELF_PATH = os.path.relpath(os.path.abspath(__file__), ROOT).replace(os.sep, "/")


def scan_forbidden(staging, included):
    hits = []
    for rel in included:
        if rel == _SELF_PATH:
            continue  # this file legitimately quotes the forbidden strings as literals
        full = os.path.join(staging, rel)
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        for needle in FORBIDDEN_STRINGS:
            if needle in text:
                hits.append((rel, needle))
    return hits


def stage(files):
    staging = tempfile.mkdtemp(prefix="invadv-public-sync-")
    for rel in files:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(staging, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return staging


def push(staging, remote, branch):
    def run(*args):
        subprocess.run(["git", *args], cwd=staging, check=True)

    run("init", "-q")
    run("checkout", "-q", "-b", branch)
    run("add", "-A")
    run("-c", "user.name=investment-advisor-sync",
        "-c", "user.email=noreply@example.com",
        "commit", "-q", "-m", "Public snapshot sync")
    run("remote", "add", "origin", remote)
    run("push", "--force", "origin", f"{branch}:{branch}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remote", help="Public repo git URL (required with --push)")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--push", action="store_true",
                     help="Actually commit + force-push. Omit for a dry run.")
    args = ap.parse_args()

    if args.push and not args.remote:
        ap.error("--push requires --remote")

    all_files = tracked_files()
    included = [f for f in all_files if not is_excluded(f)]
    excluded = [f for f in all_files if is_excluded(f)]

    staging = stage(included)

    hits = scan_forbidden(staging, included)
    if hits:
        print("ABORT: forbidden strings found in staged files that would be published:")
        for rel, needle in hits:
            print(f"  {rel}: contains {needle!r}")
        shutil.rmtree(staging, ignore_errors=True)
        sys.exit(1)

    print(f"{len(included)} files staged, {len(excluded)} excluded.")
    print(f"Staged tree: {staging}")

    if not args.push:
        print("Dry run (pass --push --remote <url> to publish). Staged tree left on disk for inspection.")
        return

    push(staging, args.remote, args.branch)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"Pushed fresh snapshot to {args.remote} ({args.branch}).")


if __name__ == "__main__":
    main()

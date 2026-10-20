# Git hooks

Version-controlled Git hooks for this repository. Install them on a clone with
`./scripts/install-hooks.sh`; the helper sets `core.hooksPath=hooks` for this
clone only.

## pre-commit

The hook runs three fast checks against staged content:

1. scripts/check_file_size.py --staged warns above 500 lines and blocks source
   files above 800 lines unless .file-size-exceptions has a reasoned entry.
   Exceptions are read from the index; changing the exceptions file rechecks all
   tracked source files. The checker uses Git's NUL-separated paths and staged
   blobs.
2. Ruff checks staged Python blobs for unused imports (F401), so an unstaged
   working-tree fix cannot hide a staged error. A machine without Ruff skips
   this check with a notice. An intentional re-export carries
   # noqa: F401 - <who uses it>.
3. The PII guard scans added text lines against pii-patterns.txt and, when
   present, pii-patterns.local.txt. The local file contains real identifiers,
   stays gitignored, and cannot be staged. Matched content is never echoed;
   failures show a line count. Ruff's raw output is suppressed; failures report
   the file name, category and exit status.

The size checker and PII guard fail closed when they cannot inspect the index or
required inputs. If the hook blocks, fix the staged content; do not bypass it
with --no-verify.

## pre-push

Runs `venv/bin/python -m unittest discover -v` from the repository root before
Git updates a remote ref. A missing project interpreter or failing test suite
blocks the push and prints an actionable error. The hook performs no install,
network, model-server, or GUI work.

## Privacy patterns

One case-insensitive grep -E pattern per non-comment line. The tracked
pii-patterns.txt holds only generic patterns. Real names, case identifiers,
and client identifiers belong in the gitignored pii-patterns.local.txt. A
fresh clone without that local file runs only the generic patterns and prints
a notice.

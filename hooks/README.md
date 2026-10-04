# Git hooks

Version-controlled git hooks for this repository. The primary hook is a
**pre-commit PII guard** that blocks any staged content matching known
sensitive patterns — see the project's top-level `README.md` for the
"No PII in the project" rule.

## One-time installation

After cloning (or after running `git init` fresh):

```bash
git config core.hooksPath hooks
```

That tells git to look in this directory for all hooks. The setting is
recorded in `.git/config` for this clone only — no global side effects.
Verify with:

```bash
git config core.hooksPath
# → hooks
```

## Hooks

### `pre-commit`

Scans the staged diff for patterns from `pii-patterns.txt`. The check looks
only at ADDED lines (`^\+[^+]`), so removing forbidden content does not
trigger the guard. Typical runtime is well under a second.

It also runs `ruff check --select F401` on staged `*.py` files, so unused imports
(including leftover re-exports) cannot regrow. An intentional re-export carries
`# noqa: F401 - <who uses it>`. Without `ruff` on PATH the step is skipped with a notice.

If the hook blocks, **fix the staged content**. Do NOT use `--no-verify`
as a workaround — the entire point of the guard is to keep PII out of
commit history, and bypassing it defeats that purpose.

### `pii-patterns.txt` and `pii-patterns.local.txt`

One case-insensitive `grep -E` pattern per non-comment line. The tracked
`pii-patterns.txt` holds only generic patterns that identify no one. Real
names, case identifiers and client identifiers go in the gitignored
`pii-patterns.local.txt`, which the hook and `scripts/scan-pii.sh` load when it
exists. A fresh clone without it runs on the generic patterns and prints a
notice. The hook blocks any commit that stages the local file, and the scanner
fails if it is tracked.

When you discover a new leak vector, add a pattern to the local file and scrub
the existing content in the same commit.

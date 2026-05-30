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

If the hook blocks, **fix the staged content**. Do NOT use `--no-verify`
as a workaround — the entire point of the guard is to keep PII out of
commit history, and bypassing it defeats that purpose.

### `pii-patterns.txt`

One case-insensitive `grep -E` pattern per non-comment line. Patterns
intentionally contain the literal strings being guarded against — this
file is a curated block list, not engagement data. Keep patterns specific
enough to avoid false positives.

When you discover a new leak vector, add a pattern here in the same commit
that scrubs the existing content.
